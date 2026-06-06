"""Engine — orchestration layer for the conversation loop.

The Engine sits between the UI and the LLM client. It handles:
- The tool-use loop (LLM calls tool → execute → send result → LLM continues)
- Token tracking across multiple LLM calls per user message
- Loop prevention (detecting repeated identical tool calls)
- Mtime-based deduplication for file reads

The Engine is a synchronous generator that runs in a Worker thread.
It yields EngineEvents that the UI consumes to update the display.

Flow:
    user message → add to session → loop:
        call LLM (stream) → accumulate blocks → yield text deltas
        if stop_reason == "end_turn": done
        if stop_reason == "tool_use":
            for each tool call: execute handler → yield result
            add results to session → continue loop
"""

import hashlib
import json
import logging
from collections.abc import Generator
from typing import TYPE_CHECKING

from archie.llm import BedrockClient, Done, ToolUseEvent, Usage
from archie.llm import TextDelta as LlmTextDelta
from archie.session import Session
from archie.tools import ToolRegistry, truncate_result
from archie.types import (
    EngineEvent,
    TextBlock,
    TextDelta,
    ToolCallResult,
    ToolCallStart,
    ToolResultBlock,
    ToolUseBlock,
    TurnComplete,
)

if TYPE_CHECKING:
    from archie.sandbox import Sandbox

log = logging.getLogger(__name__)


def _hash_args(args: dict) -> str:
    """Create a stable hash of tool arguments for repetition detection."""
    return hashlib.md5(json.dumps(args, sort_keys=True).encode()).hexdigest()


class Engine:
    """Orchestrates the LLM conversation loop with tool use.

    The Engine is stateful per-session — it maintains caches and counters
    that persist across multiple user messages within a session.

    Attributes:
        llm: The LLM client for making API calls.
        session: The conversation session (persists turns to disk).
        tools: Registry of available tools.
        system_prompt: System prompt sent with every LLM call.
        sandbox: Optional sandbox for cancelling running shell commands on interrupt.
    """

    def __init__(
        self,
        llm_client: BedrockClient,
        session: Session,
        tool_registry: ToolRegistry,
        system_prompt: str,
        sandbox: "Sandbox | None" = None,
    ):
        self.llm = llm_client
        self.session = session
        self.tools = tool_registry
        self.system_prompt = system_prompt
        # Stored so we can call sandbox.cancel() when the user interrupts (Esc).
        # This kills any in-progress docker exec process.
        self.sandbox = sandbox

        # --- Loop prevention state ---
        # Tracks consecutive identical tool calls: (tool_name, args_hash) → count
        # Resets when a different tool+args combination is called.
        self._last_call_key: tuple[str, str] | None = None
        self._consecutive_count: int = 0

    def run(self, user_message: str) -> Generator[EngineEvent]:
        """Process a user message through the full LLM + tool loop.

        This is the main entry point. It:
        1. Records the user message in the session
        2. Calls the LLM (potentially multiple times if tools are used)
        3. Yields events for each step (text, tool calls, results, completion)

        Args:
            user_message: The text the user typed.

        Yields:
            EngineEvent instances (TextDelta, ToolCallStart, ToolCallResult, TurnComplete).
        """
        # Record user message
        self.session.add_turn("user", user_message)

        # Token accumulators — summed across multiple LLM calls in this turn
        total_input_tokens = 0
        total_output_tokens = 0

        # Safety cap: prevent infinite tool-use loops (e.g. model alternates
        # between two different tool calls forever). 20 iterations is generous —
        # most real tasks need 1-5 LLM calls per user message.
        max_iterations = 20

        for _iteration in range(max_iterations):
            # --- Call the LLM ---
            text_chunks: list[str] = []
            tool_use_blocks: list[ToolUseBlock] = []
            stop_reason = "end_turn"
            turn_input = 0
            turn_output = 0

            for event in self.llm.stream(
                messages=self.session.turns,
                system=self.system_prompt,
                tool_config=self.tools.to_tool_config() or None,
            ):
                match event:
                    case LlmTextDelta(text=text):
                        text_chunks.append(text)
                        yield TextDelta(text=text)
                    case ToolUseEvent(tool_use_id=tid, name=name, input=inp):
                        tool_use_blocks.append(ToolUseBlock(tool_use_id=tid, name=name, input=inp))
                    case Usage(input_tokens=it, output_tokens=ot):
                        turn_input = it
                        turn_output = ot
                    case Done(stop_reason=sr):
                        stop_reason = sr

            total_input_tokens += turn_input
            total_output_tokens += turn_output

            # Build the assistant's content blocks for this LLM call
            assistant_content = []
            if text_chunks:
                assistant_content.append(TextBlock(text="".join(text_chunks)))
            assistant_content.extend(tool_use_blocks)

            # Record the assistant turn
            if assistant_content:
                self.session.add_turn(
                    "assistant",
                    assistant_content,
                    input_tokens=turn_input,
                    output_tokens=turn_output,
                )

            # --- If no tool use, we're done ---
            if stop_reason != "tool_use" or not tool_use_blocks:
                yield TurnComplete(
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                    stop_reason=stop_reason,
                )
                return

            # --- Execute tools ---
            tool_results: list[ToolResultBlock] = []

            for tool_block in tool_use_blocks:
                yield ToolCallStart(
                    tool_use_id=tool_block.tool_use_id,
                    name=tool_block.name,
                    input=tool_block.input,
                )

                result_content, is_error = self._execute_tool(tool_block.name, tool_block.input)

                # Truncate to prevent context bloat
                result_content = truncate_result(result_content)

                tool_results.append(
                    ToolResultBlock(
                        tool_use_id=tool_block.tool_use_id,
                        content=result_content,
                        is_error=is_error,
                    )
                )

                yield ToolCallResult(
                    tool_use_id=tool_block.tool_use_id,
                    name=tool_block.name,
                    content=result_content,
                    is_error=is_error,
                )

            # Record tool results as a user turn (Bedrock protocol requirement)
            self.session.add_turn("user", tool_results)

            # Loop continues — LLM will be called again with the tool results

        # Safety: if we exhausted MAX_ITERATIONS, force-complete the turn
        log.warning("Engine hit iteration cap (%d). Forcing turn completion.", max_iterations)
        yield TurnComplete(
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            stop_reason="max_iterations",
        )

    def _execute_tool(self, name: str, args: dict) -> tuple[str, bool]:
        """Execute a tool and return (result_content, is_error).

        Handles:
        - Tool lookup (unknown tool → error)
        - Consecutive-call detection (warn at 3, block at 4)
        - Exception handling (tool crashes → error result)
        """
        # --- Consecutive-call detection ---
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

        # --- Look up tool ---
        spec = self.tools.get(name)
        if spec is None:
            return f"Error: Unknown tool '{name}'", True

        # --- Execute handler ---
        try:
            result = spec.handler(args)
        except Exception as e:
            log.exception("Tool '%s' raised an exception", name)
            return f"Error: Tool execution failed: {e}", True

        # --- Append warning if at consecutive threshold ---
        if self._consecutive_count == 3:
            result += (
                "\n\n⚠️ Warning: This exact tool call has been made 3 times consecutively. "
                "Consider a different approach to avoid being blocked."
            )

        return result, False
