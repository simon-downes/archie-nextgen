"""Agent loop: callback-based turn orchestrator with cooperative interruption.

This is the conductor of the agent layer. It owns the iteration cycle —
build context → stream from Bedrock → run any tools → repeat until the model stops
— and wires together the session (history), LLM client, tool registry, and sandbox.

Two design choices shape everything here:

1. Events, not return values. The loop communicates with the outside world ONLY by
   emitting frozen AgentEvent dataclasses through an injected callback. It never
   imports Textual or touches a widget. In the real app the callback marshals events
   to the UI thread; in tests it's just list.append.

2. Cooperative interruption. The turn runs in a worker thread. To abort it we can't
   just kill the thread — we'd leave history in an illegal state. Instead Esc sets a
   threading.Event; the loop checks it between stream events and around tool calls,
   raises _InterruptedError, and finalises history cleanly.
"""

import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from archie.artifact_store import ArtifactStore
from archie.llm import BedrockClient, Done, ToolUseEvent, Usage
from archie.llm import TextDelta as LlmTextDelta
from archie.session import Session, Turn, TurnLog, summarise_tool_output
from archie.tools import ToolRegistry, truncate_result
from archie.types import TextBlock, ToolResultBlock, ToolUseBlock

if TYPE_CHECKING:
    from archie.sandbox import Sandbox

log = logging.getLogger(__name__)


# --- Agent Events ---


@dataclass(frozen=True)
class TextDeltaEvent:
    """A chunk of streamed assistant text."""

    text: str


@dataclass(frozen=True)
class ToolStarted:
    """A tool call is about to run."""

    tool_use_id: str
    name: str
    input: dict


@dataclass(frozen=True)
class ToolFinished:
    """A tool call completed."""

    tool_use_id: str
    name: str
    summary: str
    is_error: bool


@dataclass(frozen=True)
class UsageUpdated:
    """Token usage snapshot emitted after each Bedrock request."""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost: float


@dataclass(frozen=True)
class TurnComplete:
    """The turn ended normally."""

    stop_reason: str


@dataclass(frozen=True)
class TurnInterrupted:
    """The turn was aborted by the user and history has been repaired."""

    pass


@dataclass(frozen=True)
class TurnError:
    """A terminal failure. Message is the raw exception string."""

    message: str


type AgentEvent = (
    TextDeltaEvent
    | ToolStarted
    | ToolFinished
    | UsageUpdated
    | TurnComplete
    | TurnInterrupted
    | TurnError
)


class _InterruptedError(Exception):
    """Internal control-flow signal for user abort — never escapes this module."""


def _hash_args(args: dict) -> str:
    """Create a stable hash of tool arguments for repetition detection."""
    return hashlib.md5(json.dumps(args, sort_keys=True).encode()).hexdigest()


class AgentLoop:
    """Drives the agentic turn loop with cooperative interruption."""

    def __init__(
        self,
        llm_client: BedrockClient,
        session: Session,
        tool_registry: ToolRegistry,
        system_prompt: str,
        emit: Callable[[AgentEvent], None],
        sandbox: "Sandbox | None" = None,
        artifact_store: ArtifactStore | None = None,
    ):
        self.llm = llm_client
        self.session = session
        self.tools = tool_registry
        self.system_prompt = system_prompt
        self._emit = emit
        self.sandbox = sandbox
        self.artifact_store = artifact_store or ArtifactStore()

        self._interrupt = threading.Event()
        self._last_call_key: tuple[str, str] | None = None
        self._consecutive_count: int = 0
        self._completed_turns: int = 0

    def interrupt(self) -> None:
        """Request abort of the in-flight turn. Thread-safe; called from the UI thread."""
        self._interrupt.set()

    def _check_interrupt(self) -> None:
        """Raise _InterruptedError if abort was requested."""
        if self._interrupt.is_set():
            raise _InterruptedError

    def run_turn(self, user_message: str) -> None:
        """Drive one full turn to completion, interruption, or error.

        All outcomes end with exactly one terminal event emitted.
        """
        self._interrupt.clear()
        self.session.add_turn("user", user_message)

        turn_log = TurnLog(
            when=datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            user=user_message,
            model=self.session.model_id,
        )
        total_input = 0
        total_output = 0

        try:
            for _ in range(50):
                (
                    text_chunks,
                    tool_use_blocks,
                    turn_input,
                    turn_output,
                    turn_cache_read,
                    turn_cache_write,
                    stop_reason,
                ) = self._do_request()
                total_input += turn_input
                total_output += turn_output

                # Record assistant turn
                assistant_content: list[TextBlock | ToolUseBlock] = []
                if text_chunks:
                    assistant_content.append(TextBlock(text="".join(text_chunks)))
                    turn_log.assistant_text += "".join(text_chunks)
                assistant_content.extend(tool_use_blocks)

                if assistant_content:
                    self.session.add_turn(
                        "assistant",
                        assistant_content,
                        input_tokens=turn_input,
                        output_tokens=turn_output,
                        cache_read_tokens=turn_cache_read,
                        cache_write_tokens=turn_cache_write,
                    )

                self._emit(
                    UsageUpdated(
                        input_tokens=self.session.total_input_tokens,
                        output_tokens=self.session.total_output_tokens,
                        cache_read_tokens=self.session.total_cache_read_tokens,
                        cache_write_tokens=self.session.total_cache_write_tokens,
                        cost=self.session.total_cost,
                    )
                )

                if stop_reason != "tool_use" or not tool_use_blocks:
                    break

                # Execute tools (each result committed to session immediately)
                self._execute_tools(tool_use_blocks, turn_log)
            else:
                log.warning("Agent hit iteration cap (50)")

            turn_log.input_tokens = total_input
            turn_log.output_tokens = total_output
            self.session.flush_turn(turn_log)
            self._completed_turns += 1
            self._emit(TurnComplete(stop_reason=stop_reason))

        except _InterruptedError:
            self._finalise_interrupted_turn()
            turn_log.input_tokens = total_input
            turn_log.output_tokens = total_output
            turn_log.interrupted = True
            self.session.flush_turn(turn_log)
            self._emit(TurnInterrupted())

        except Exception as e:
            log.exception("Turn failed")
            self._finalise_interrupted_turn()
            turn_log.input_tokens = total_input
            turn_log.output_tokens = total_output
            self.session.flush_turn(turn_log)
            self._emit(TurnError(message=str(e)))

    def _do_request(self) -> tuple[list[str], list[ToolUseBlock], int, int, int, int, str]:
        """Stream one LLM call. Returns (text, tools, in, out, cache_read, cache_write, stop).

        On interrupt mid-stream, commits any partial text to session before re-raising
        so the interrupted response is preserved in history.
        """
        self._check_interrupt()

        text_chunks: list[str] = []
        tool_use_blocks: list[ToolUseBlock] = []
        stop_reason = "end_turn"
        turn_input = 0
        turn_output = 0
        turn_cache_read = 0
        turn_cache_write = 0

        try:
            for event in self.llm.stream(
                messages=self._build_context(),
                system=self.system_prompt,
                tool_config=self.tools.to_tool_config() or None,
            ):
                self._check_interrupt()
                match event:
                    case LlmTextDelta(text=text):
                        text_chunks.append(text)
                        self._emit(TextDeltaEvent(text=text))
                    case ToolUseEvent(tool_use_id=tid, name=name, input=inp):
                        tool_use_blocks.append(ToolUseBlock(tool_use_id=tid, name=name, input=inp))
                    case Usage(
                        input_tokens=it,
                        output_tokens=ot,
                        cache_read_input_tokens=cr,
                        cache_write_input_tokens=cw,
                    ):
                        turn_input = it
                        turn_output = ot
                        turn_cache_read = cr
                        turn_cache_write = cw
                    case Done(stop_reason=sr):
                        stop_reason = sr
        except _InterruptedError:
            # Commit partial text to session before propagating so it's preserved
            if text_chunks:
                self.session.add_turn("assistant", [TextBlock(text="".join(text_chunks))])
            raise

        return (
            text_chunks,
            tool_use_blocks,
            turn_input,
            turn_output,
            turn_cache_read,
            turn_cache_write,
            stop_reason,
        )

    def _execute_tools(self, tool_blocks: list[ToolUseBlock], turn_log: TurnLog) -> None:
        """Run tool calls, batching results into a single user turn.

        Bedrock requires all toolResult blocks for a given assistant turn to be in
        one user message. We collect results and commit them together at the end.
        On interrupt, we commit whatever's been collected so far before propagating.
        """
        results: list[ToolResultBlock] = []
        try:
            for block in tool_blocks:
                self._check_interrupt()
                self._emit(
                    ToolStarted(tool_use_id=block.tool_use_id, name=block.name, input=block.input)
                )

                content, is_error = self._run_one_tool(block.name, block.input)
                summary = summarise_tool_output(block.name, block.input, content, is_error)
                self.artifact_store.put(block.tool_use_id, content, summary)

                turn_log.tools.append(
                    {
                        "id": block.tool_use_id,
                        "name": block.name,
                        "input": block.input,
                        "success": not is_error,
                        "summary": summary,
                        **({"error": content.split("\n")[0][:200]} if is_error else {}),
                    }
                )

                content = truncate_result(content)
                results.append(
                    ToolResultBlock(
                        tool_use_id=block.tool_use_id, content=content, is_error=is_error
                    )
                )

                self._emit(
                    ToolFinished(
                        tool_use_id=block.tool_use_id,
                        name=block.name,
                        summary=summary,
                        is_error=is_error,
                    )
                )
                self._check_interrupt()
        except _InterruptedError:
            # Commit completed results before propagating
            if results:
                self.session.add_turn("user", results)
            raise

        self.session.add_turn("user", results)

    def _run_one_tool(self, name: str, args: dict) -> tuple[str, bool]:
        """Execute a single tool. Returns (result_content, is_error)."""
        # Consecutive-call detection
        call_key = (name, _hash_args(args))
        if call_key == self._last_call_key:
            self._consecutive_count += 1
        else:
            self._last_call_key = call_key
            self._consecutive_count = 1

        if self._consecutive_count >= 4:
            return (
                "Error: Blocked — this exact tool call has been made 4 times consecutively. "
                "Try a different approach.",
                True,
            )

        spec = self.tools.get(name)
        if spec is None:
            return f"Error: Unknown tool '{name}'", True

        log.debug("tool %s input: %s", name, str(args)[:500])
        t0 = time.time()
        try:
            result = spec.handler(args)
        except Exception as e:
            duration = time.time() - t0
            log.info("tool %s completed in %.1fs (error: %s)", name, duration, str(e)[:100])
            return f"Error: Tool execution failed: {e}", True

        duration = time.time() - t0
        log.info("tool %s completed in %.1fs (success)", name, duration)

        if self._consecutive_count == 3:
            result += (
                "\n\n⚠️ Warning: This exact tool call has been made 3 times consecutively. "
                "Consider a different approach to avoid being blocked."
            )
        return result, False

    def _finalise_interrupted_turn(self) -> None:
        """Repair history so every toolUse has a matching toolResult."""
        # Collect all tool_use_ids that already have results
        result_ids = set()
        for turn in self.session.turns:
            for block in turn.content:
                if isinstance(block, ToolResultBlock):
                    result_ids.add(block.tool_use_id)

        # Find unpaired toolUse blocks and synthesise results
        unpaired = []
        for turn in self.session.turns:
            for block in turn.content:
                if isinstance(block, ToolUseBlock) and block.tool_use_id not in result_ids:
                    unpaired.append(block)

        if unpaired:
            self.session.add_turn(
                "user",
                [
                    ToolResultBlock(
                        tool_use_id=b.tool_use_id,
                        content="[interrupted by user]",
                        is_error=True,
                    )
                    for b in unpaired
                ],
            )

        # If the turn has only the user's original text message with nothing after it,
        # remove it — re-sending a prompt that got no response would confuse the model.
        if len(self.session.turns) >= 1:
            last_user_turns = [
                t
                for t in self.session.turns
                if t.role == "user" and t.content and isinstance(t.content[0], TextBlock)
            ]
            if last_user_turns:
                last = last_user_turns[-1]
                # Check if nothing followed this user message
                idx = self.session.turns.index(last)
                if idx == len(self.session.turns) - 1:
                    self.session.turns.remove(last)

    def _build_context(self) -> list[dict]:
        """Build Bedrock-format messages with old tool results replaced by stubs.

        Note: this duplicates the message→dict serialization from bedrock.py's
        _turns_to_bedrock_messages, but with eviction logic interleaved. The two
        must stay in sync on wire format.
        """
        turns = self.session.turns
        eviction_boundary = self._find_eviction_boundary(turns)

        messages = []
        for i, turn in enumerate(turns):
            content_blocks = []
            for block in turn.content:
                match block:
                    case TextBlock(text=text):
                        content_blocks.append({"text": text})
                    case ToolUseBlock(tool_use_id=tid, name=name, input=inp):
                        content_blocks.append(
                            {"toolUse": {"toolUseId": tid, "name": name, "input": inp}}
                        )
                    case ToolResultBlock(tool_use_id=tid, content=content, is_error=is_error):
                        if i < eviction_boundary:
                            stub = self._make_eviction_stub(tid, turns[:i])
                            content_blocks.append(
                                {
                                    "toolResult": {
                                        "toolUseId": tid,
                                        "content": [{"text": stub}],
                                        "status": "error" if is_error else "success",
                                    }
                                }
                            )
                        else:
                            content_blocks.append(
                                {
                                    "toolResult": {
                                        "toolUseId": tid,
                                        "content": [{"text": content}],
                                        "status": "error" if is_error else "success",
                                    }
                                }
                            )
            messages.append({"role": turn.role, "content": content_blocks})
        return messages

    def _find_eviction_boundary(self, turns: list[Turn]) -> int:
        """Find the turn index before which tool results should be evicted."""
        user_text_count = 0
        for i in range(len(turns) - 1, -1, -1):
            turn = turns[i]
            if turn.role == "user" and turn.content and isinstance(turn.content[0], TextBlock):
                user_text_count += 1
                if user_text_count >= 2:
                    return i
        return 0

    def _make_eviction_stub(self, tool_use_id: str, preceding_turns: list[Turn]) -> str:
        """Build a stub string for an evicted tool result."""
        artifact = self.artifact_store.get(tool_use_id)
        summary = artifact["summary"] if artifact else "unknown"

        tool_name = "unknown"
        for turn in reversed(preceding_turns):
            if turn.role == "assistant":
                for block in turn.content:
                    if isinstance(block, ToolUseBlock) and block.tool_use_id == tool_use_id:
                        tool_name = block.name
                        break
                if tool_name != "unknown":
                    break

        return f"[evicted: {tool_name} — {summary} | id: {tool_use_id}]"
