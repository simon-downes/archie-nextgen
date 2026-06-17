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
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from archie.artifact_store import ArtifactStore
from archie.llm import Done, LLMClient, ToolUseEvent, Usage
from archie.llm import TextDelta as LlmTextDelta
from archie.logs import bind, clear, log_event
from archie.session import Session, Turn, TurnLog, summarise_tool_output
from archie.tools import ToolRegistry, current_tool_use_id, truncate_result
from archie.tools.ui_summary import format_tool_complete, format_tool_detail, format_tool_pending
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
    ui_summary: str


@dataclass(frozen=True)
class ToolFinished:
    """A tool call completed."""

    tool_use_id: str
    name: str
    summary: str
    is_error: bool
    ui_summary: str
    ui_detail: list[str] | None


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


# --- Constants ---

MAX_TOOL_ITERATIONS = 50
CONSECUTIVE_WARN = 3
CONSECUTIVE_BLOCK = 4

# Within-turn eviction: long agentic turns accumulate tool results that inflate
# every subsequent request in the turn. Once more than THRESHOLD un-evicted
# tool-result turns exist in the current turn, all but the most recent KEEP are
# evicted. The hysteresis gap (THRESHOLD - KEEP) means the prompt-cache prefix
# is only broken once every 10 iterations, not on every request.
WITHIN_TURN_EVICT_THRESHOLD = 20
WITHIN_TURN_KEEP = 10


def _hash_args(args: dict) -> str:
    """Create a stable hash of tool arguments for repetition detection."""
    return hashlib.md5(json.dumps(args, sort_keys=True).encode()).hexdigest()


_SHELL_EXIT_RE = re.compile(r"\[exit: (\d+)\]")


def _is_tool_error(result: str) -> bool:
    """Determine if a tool result represents an error.

    Checks for:
    - tool_error() prefix ("Error: ...")
    - Shell non-zero exit codes ("[exit: N]" where N > 0)
    """
    if not isinstance(result, str):
        return False
    if result.startswith("Error:"):
        return True
    m = _SHELL_EXIT_RE.search(result)
    if m and int(m.group(1)) != 0:
        return True
    return False


@dataclass
class RequestResult:
    """Result of one LLM streaming call."""

    text_chunks: list[str]
    tool_use_blocks: list[ToolUseBlock]
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    stop_reason: str
    truncated_tool_ids: set[str] = field(default_factory=set)


class AgentLoop:
    """Drives the agentic turn loop with cooperative interruption."""

    def __init__(
        self,
        llm_client: LLMClient,
        session: Session,
        tool_registry: ToolRegistry,
        system_prompt: str,
        emit: Callable[[AgentEvent], None],
        sandbox: "Sandbox | None" = None,
        artifact_store: ArtifactStore | None = None,
        mtime_cache: dict[tuple[str, int, int], tuple[float, str]] | None = None,
        cwd: Path | None = None,
        pre_content_stash: dict[str, str] | None = None,
    ):
        self.llm = llm_client
        self.session = session
        self.tools = tool_registry
        self.system_prompt = system_prompt
        self._emit = emit
        self.sandbox = sandbox
        self.artifact_store = artifact_store or ArtifactStore()
        self.cwd = cwd or Path.cwd()
        # Shared with read_file (see create_default_registry). Entries record
        # which tool result holds the cached content; when that result is
        # evicted from context we invalidate the entry so the next read returns
        # real content instead of a useless "file unchanged" stub.
        self._mtime_cache = mtime_cache if mtime_cache is not None else {}

        self._interrupt = threading.Event()
        self._interrupt_logged: bool = False
        self._last_call_key: tuple[str, str] | None = None
        self._consecutive_count: int = 0
        self._completed_turns: int = 0
        self._last_turn_interrupted: bool = False
        # Turn ids evicted mid-turn (see _maybe_evict_within_turn)
        self._within_turn_evicted: set[str] = set()
        # Pre-content stash for UI diffs: tool_use_id → file content before edit/write.
        # Populated by edit_file/write_file handlers, consumed by _execute_tools.
        self._pre_content_stash = pre_content_stash if pre_content_stash is not None else {}

    def interrupt(self) -> None:
        """Request abort of the in-flight turn. Thread-safe; called from the UI thread."""
        self._interrupt.set()

    def _check_interrupt(self, phase: str = "") -> None:
        """Raise _InterruptedError if abort was requested. Logs once per turn."""
        if self._interrupt.is_set():
            if not self._interrupt_logged:
                self._interrupt_logged = True
                log_event(log, logging.INFO, "interrupt", phase=phase or "unknown")
            raise _InterruptedError

    def run_turn(self, user_message: str) -> None:
        """Drive one full turn to completion, interruption, or error.

        All outcomes end with exactly one terminal event emitted.
        """
        self._interrupt.clear()
        self._interrupt_logged = False
        self._dedupe_resent_prompt(user_message)
        self.session.add_turn("user", user_message)
        bind(session=self.session.session_id, turn=self._completed_turns + 1)
        log_event(log, logging.INFO, "turn_start", user=user_message[:100])

        turn_log = TurnLog(
            when=datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            user=user_message,
            model=self.session.model_id,
        )
        total_input = 0
        total_output = 0
        total_cache_read = 0
        total_cache_write = 0

        try:
            for iteration in range(MAX_TOOL_ITERATIONS):
                bind(iteration=iteration + 1)
                r = self._do_request()
                total_input += r.input_tokens
                total_output += r.output_tokens
                total_cache_read += r.cache_read_tokens
                total_cache_write += r.cache_write_tokens

                # Record assistant turn
                assistant_content: list[TextBlock | ToolUseBlock] = []
                if r.text_chunks:
                    assistant_content.append(TextBlock(text="".join(r.text_chunks)))
                    turn_log.assistant_text += "".join(r.text_chunks)
                assistant_content.extend(r.tool_use_blocks)

                if assistant_content:
                    self.session.add_turn(
                        "assistant",
                        assistant_content,
                        input_tokens=r.input_tokens,
                        output_tokens=r.output_tokens,
                        cache_read_tokens=r.cache_read_tokens,
                        cache_write_tokens=r.cache_write_tokens,
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

                if not r.tool_use_blocks or r.stop_reason not in ("tool_use", "max_tokens"):
                    break

                # Execute tools (each result committed to session immediately).
                # On max_tokens, tool calls whose args were truncated mid-stream
                # are paired with error results (never executed) so history stays
                # valid and the model is told to split the work into smaller pieces.
                self._execute_tools(r.tool_use_blocks, turn_log, truncated_ids=r.truncated_tool_ids)
                self._maybe_evict_within_turn()
            else:
                log_event(log, logging.WARNING, "iteration_cap", cap=MAX_TOOL_ITERATIONS)

            bind(iteration=None)
            turn_log.input_tokens = total_input
            turn_log.output_tokens = total_output
            turn_log.cache_read_tokens = total_cache_read
            turn_log.cache_write_tokens = total_cache_write
            self.session.flush_turn(turn_log)
            self._completed_turns += 1
            self._last_turn_interrupted = False
            log_event(
                log,
                logging.INFO,
                "turn_end",
                status="complete",
                iterations=iteration + 1,
                input=total_input,
                output=total_output,
                cache_read=total_cache_read,
                cache_write=total_cache_write,
                cost=round(self.session.total_cost, 4),
            )
            self._emit(TurnComplete(stop_reason=r.stop_reason))

        except _InterruptedError:
            bind(iteration=None)
            self._last_turn_interrupted = True
            self._finalise_interrupted_turn()
            turn_log.input_tokens = total_input
            turn_log.output_tokens = total_output
            turn_log.cache_read_tokens = total_cache_read
            turn_log.cache_write_tokens = total_cache_write
            turn_log.interrupted = True
            self.session.flush_turn(turn_log)
            log_event(
                log,
                logging.INFO,
                "turn_end",
                status="interrupted",
                iterations=iteration + 1,
                input=total_input,
                output=total_output,
                cache_read=total_cache_read,
                cache_write=total_cache_write,
            )
            self._emit(TurnInterrupted())

        except Exception as e:
            self._last_turn_interrupted = True
            self._finalise_interrupted_turn()
            turn_log.input_tokens = total_input
            turn_log.output_tokens = total_output
            turn_log.cache_read_tokens = total_cache_read
            turn_log.cache_write_tokens = total_cache_write
            self.session.flush_turn(turn_log)
            # Single ERROR record: traceback + turn fields together
            log_event(
                log,
                logging.ERROR,
                "turn_end",
                exc_info=True,
                status="error",
                error=str(e)[:200],
                iterations=iteration + 1,
                input=total_input,
                output=total_output,
            )
            self._emit(TurnError(message=str(e)))

        finally:
            clear()

    def _do_request(self) -> RequestResult:
        """Stream one LLM call.

        On interrupt mid-stream, commits any partial text to session before re-raising
        so the interrupted response is preserved in history.
        """
        self._check_interrupt("pre_request")

        text_chunks: list[str] = []
        tool_use_blocks: list[ToolUseBlock] = []
        truncated_tool_ids: set[str] = set()
        stop_reason = "end_turn"
        turn_input = 0
        turn_output = 0
        turn_cache_read = 0
        turn_cache_write = 0

        context = self._build_context()
        est_chars = sum(
            len(block.text) if isinstance(block, TextBlock) else len(str(block))
            for turn in context
            for block in turn.content
        )
        log_event(
            log,
            logging.DEBUG,
            "request_start",
            messages=len(context),
            est_tokens=est_chars // 4,
        )

        try:
            for event in self.llm.stream(
                messages=context,
                system=self.system_prompt,
                tool_config=self.tools.to_tool_config() or None,
            ):
                self._check_interrupt("stream")
                match event:
                    case LlmTextDelta(text=text):
                        text_chunks.append(text)
                        self._emit(TextDeltaEvent(text=text))
                    case ToolUseEvent(
                        tool_use_id=tid, name=name, input=inp, input_truncated=truncated
                    ):
                        tool_use_blocks.append(ToolUseBlock(tool_use_id=tid, name=name, input=inp))
                        if truncated:
                            truncated_tool_ids.add(tid)
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

        return RequestResult(
            text_chunks=text_chunks,
            tool_use_blocks=tool_use_blocks,
            input_tokens=turn_input,
            output_tokens=turn_output,
            cache_read_tokens=turn_cache_read,
            cache_write_tokens=turn_cache_write,
            stop_reason=stop_reason,
            truncated_tool_ids=truncated_tool_ids,
        )

    def _execute_tools(
        self,
        tool_blocks: list[ToolUseBlock],
        turn_log: TurnLog,
        truncated_ids: set[str] | None = None,
    ) -> None:
        """Run tool calls, batching results into a single user turn.

        Bedrock requires all toolResult blocks for a given assistant turn to be in
        one user message. We collect results and commit them together at the end.
        On interrupt, we commit whatever's been collected so far before propagating.

        Blocks listed in truncated_ids had their args JSON cut off by the output
        token limit; they are never executed — each gets an error result telling
        the model to split the work into smaller pieces.
        """
        truncated_ids = truncated_ids or set()
        results: list[ToolResultBlock] = []
        try:
            for block in tool_blocks:
                self._check_interrupt("tools")

                if block.tool_use_id in truncated_ids:
                    content = (
                        "Tool call not executed: your response hit the maximum output "
                        "token limit before the arguments were complete. Retry in "
                        "smaller pieces — e.g. write large files in parts (write_file "
                        "for the first part, then write_file with append=true for the "
                        "rest), and split large batches of tool calls across responses."
                    )
                    log_event(
                        log,
                        logging.WARNING,
                        "tool_truncated",
                        tool_use_id=block.tool_use_id,
                        tool=block.name,
                    )
                    turn_log.tools.append(
                        {
                            "id": block.tool_use_id,
                            "name": block.name,
                            "input": block.input,
                            "success": False,
                            "summary": "args truncated at output token limit — not executed",
                            "error": "tool args truncated at max output tokens",
                        }
                    )
                    results.append(
                        ToolResultBlock(
                            tool_use_id=block.tool_use_id, content=content, is_error=True
                        )
                    )
                    self._emit(
                        ToolFinished(
                            tool_use_id=block.tool_use_id,
                            name=block.name,
                            summary="args truncated at output token limit — not executed",
                            is_error=True,
                            ui_summary=format_tool_complete(
                                block.name, block.input, content, True, self.cwd
                            ),
                            ui_detail=None,
                        )
                    )
                    continue

                self._emit(
                    ToolStarted(
                        tool_use_id=block.tool_use_id,
                        name=block.name,
                        input=block.input,
                        ui_summary=format_tool_pending(block.name, block.input, self.cwd),
                    )
                )
                log_event(
                    log,
                    logging.DEBUG,
                    "tool_start",
                    tool_use_id=block.tool_use_id,
                    tool=block.name,
                    input=json.dumps(block.input, default=str)[:500],
                )
                t0 = time.time()

                content, is_error = self._run_one_tool(block.name, block.input, block.tool_use_id)
                duration = time.time() - t0
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

                spec = self.tools.get(block.name)
                result_bytes = len(content)
                if not spec or not spec.self_truncating:
                    content = truncate_result(content)
                log_event(
                    log,
                    logging.WARNING if is_error else logging.INFO,
                    "tool_end",
                    tool_use_id=block.tool_use_id,
                    tool=block.name,
                    duration_s=round(duration, 2),
                    status="error" if is_error else "success",
                    result_bytes=result_bytes,
                    truncated=len(content) < result_bytes,
                    **({"error": content.split("\n")[0][:200]} if is_error else {}),
                )
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
                        ui_summary=format_tool_complete(
                            block.name, block.input, content, is_error, self.cwd
                        ),
                        ui_detail=format_tool_detail(
                            block.name,
                            block.input,
                            content,
                            is_error,
                            self.cwd,
                            pre_content=self._pre_content_stash.pop(block.tool_use_id, None),
                        ),
                    )
                )
                self._check_interrupt("tools")
        except _InterruptedError:
            # Commit completed results before propagating
            if results:
                self.session.add_turn("user", results)
            raise

        self.session.add_turn("user", results)

    def _run_one_tool(self, name: str, args: dict, tool_use_id: str) -> tuple[str, bool]:
        """Execute a single tool. Returns (result_content, is_error)."""
        # Consecutive-call detection
        call_key = (name, _hash_args(args))
        if call_key == self._last_call_key:
            self._consecutive_count += 1
        else:
            self._last_call_key = call_key
            self._consecutive_count = 1

        if self._consecutive_count >= CONSECUTIVE_BLOCK:
            return (
                "Error: Blocked — this exact tool call has been made 4 times consecutively. "
                "Try a different approach.",
                True,
            )

        spec = self.tools.get(name)
        if spec is None:
            return f"Error: Unknown tool '{name}'", True

        try:
            token = current_tool_use_id.set(tool_use_id)
            try:
                result = spec.handler(args)
            finally:
                current_tool_use_id.reset(token)
        except Exception as e:
            log.debug("Tool handler raised", exc_info=True)
            return f"Error: Tool execution failed: {e}", True

        if self._consecutive_count == CONSECUTIVE_WARN:
            result += (
                "\n\n⚠️ Warning: This exact tool call has been made 3 times consecutively. "
                "Consider a different approach to avoid being blocked."
            )
        is_error = _is_tool_error(result)
        return result, is_error

    def _finalise_interrupted_turn(self) -> None:
        """Repair history so every toolUse has a matching toolResult.

        Synthetic results must land in the user turn immediately AFTER the
        assistant turn containing the unpaired toolUse — a toolResult that
        appears before its toolUse violates the Bedrock protocol (this exact
        mis-ordering once made a session unrecoverable). When interrupted
        mid-tool-batch, partial results are already committed in that following
        user turn, so we extend it rather than create a second one (consecutive
        user messages are also a protocol violation).
        """
        # Collect all tool_use_ids that already have results
        result_ids = set()
        for turn in self.session.turns:
            for block in turn.content:
                if isinstance(block, ToolResultBlock):
                    result_ids.add(block.tool_use_id)

        # Walk assistant turns; pair any unpaired toolUse with synthetic results
        # placed directly after the turn that contains it.
        i = 0
        while i < len(self.session.turns):
            turn = self.session.turns[i]
            if turn.role == "assistant":
                unpaired = [
                    b
                    for b in turn.content
                    if isinstance(b, ToolUseBlock) and b.tool_use_id not in result_ids
                ]
                if unpaired:
                    synthetic = [
                        ToolResultBlock(
                            tool_use_id=b.tool_use_id,
                            content="[interrupted by user]",
                            is_error=True,
                        )
                        for b in unpaired
                    ]
                    next_turn = (
                        self.session.turns[i + 1] if i + 1 < len(self.session.turns) else None
                    )
                    if next_turn is not None and next_turn.role == "user":
                        next_turn.content = list(next_turn.content) + synthetic
                    else:
                        new_turn = self.session.add_turn("user", synthetic)
                        if self.session.turns[i + 1] is not new_turn:
                            self.session.turns.remove(new_turn)
                            self.session.turns.insert(i + 1, new_turn)
            i += 1

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

    def _dedupe_resent_prompt(self, user_message: str) -> None:
        """Drop superseded history when the user re-sends a prompt after an interrupt.

        Interrupting and re-sending the same prompt would otherwise leave the
        earlier copy (plus partial response and tool activity) in history for
        the rest of the session, bloating every subsequent request. If the
        previous turn was interrupted/errored and its user prompt matches the
        new message exactly, everything from that prompt onward is removed —
        the re-run supersedes it. Dropping from the user text turn to the end
        keeps toolUse/toolResult pairing intact (both sides are dropped).
        """
        if not self._last_turn_interrupted:
            return
        turns = self.session.turns
        for i in range(len(turns) - 1, -1, -1):
            t = turns[i]
            if t.role == "user" and t.content and isinstance(t.content[0], TextBlock):
                if t.content[0].text == user_message:
                    dropped = turns[i:]
                    # Invalidate mtime entries pointing at dropped tool results —
                    # their content is leaving context, so "file unchanged" stubs
                    # referencing them would be useless.
                    for dt in dropped:
                        for b in dt.content:
                            if isinstance(b, ToolResultBlock):
                                self._invalidate_mtime_entries(b.tool_use_id)
                    del turns[i:]
                    log_event(
                        log,
                        logging.INFO,
                        "resent_prompt_deduped",
                        dropped_turns=len(dropped),
                    )
                break

    def _build_context(self) -> list[Turn]:
        """Build the message list with old tool results replaced by stubs.

        Returns Turn objects — bedrock.py owns all wire-format serialization via
        _turns_to_bedrock_messages. For evicted results we create new Turn objects
        with stub ToolResultBlocks (ContentBlocks are frozen, can't mutate in place).
        """
        turns = self.session.turns
        eviction_boundary = self._find_eviction_boundary(turns)

        result = []
        for i, turn in enumerate(turns):
            boundary_evict = (
                i < eviction_boundary
                and turn.role == "user"
                and any(isinstance(b, ToolResultBlock) for b in turn.content)
            )
            if boundary_evict or turn.id in self._within_turn_evicted:
                new_content = []
                for block in turn.content:
                    if isinstance(block, ToolResultBlock):
                        stub = self._make_eviction_stub(block.tool_use_id, turns[:i])
                        self._invalidate_mtime_entries(block.tool_use_id)
                        new_content.append(
                            ToolResultBlock(
                                tool_use_id=block.tool_use_id,
                                content=stub,
                                is_error=block.is_error,
                            )
                        )
                    else:
                        new_content.append(block)
                result.append(
                    Turn(id=turn.id, role=turn.role, content=new_content, timestamp=turn.timestamp)
                )
            else:
                result.append(turn)
        return result

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

    def _maybe_evict_within_turn(self) -> None:
        """Evict old tool results accumulated within the current turn.

        Long agentic turns (many tool iterations) never cross the user-turn
        eviction boundary, so results pile up and inflate every request in the
        turn. Once more than WITHIN_TURN_EVICT_THRESHOLD un-evicted tool-result
        turns have accumulated since the current user message, evict all but
        the most recent WITHIN_TURN_KEEP. The full content remains recoverable
        via retrieve_artifact.

        Hysteresis: evicting in batches of (THRESHOLD - KEEP) breaks the
        prompt-cache prefix once per batch rather than on every request.
        """
        turns = self.session.turns
        # Start of the current turn = last user text turn
        start = 0
        for i in range(len(turns) - 1, -1, -1):
            t = turns[i]
            if t.role == "user" and t.content and isinstance(t.content[0], TextBlock):
                start = i
                break

        candidates = [
            t
            for t in turns[start:]
            if t.role == "user"
            and t.id not in self._within_turn_evicted
            and any(isinstance(b, ToolResultBlock) for b in t.content)
        ]
        if len(candidates) <= WITHIN_TURN_EVICT_THRESHOLD:
            return
        to_evict = candidates[: len(candidates) - WITHIN_TURN_KEEP]
        for t in to_evict:
            self._within_turn_evicted.add(t.id)
        log_event(
            log,
            logging.INFO,
            "within_turn_eviction",
            evicted_turns=len(to_evict),
            kept_turns=WITHIN_TURN_KEEP,
        )

    def _invalidate_mtime_entries(self, tool_use_id: str) -> None:
        """Remove read-dedup cache entries whose content lives in an evicted result.

        Without this, a re-read after eviction returns "file unchanged" pointing
        at content the model can no longer see — forcing it to dodge the cache
        with varied offsets (observed in session analysis).
        """
        stale = [k for k, v in self._mtime_cache.items() if v[1] == tool_use_id]
        for k in stale:
            del self._mtime_cache[k]

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

        return (
            f"[evicted: {tool_name} — {summary} | id: {tool_use_id} — "
            f"use retrieve_artifact to recover if needed]"
        )
