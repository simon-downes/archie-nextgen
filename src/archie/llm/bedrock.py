"""AWS Bedrock converse_stream wrapper.

This module handles all communication with AWS Bedrock. It wraps the low-level
boto3 EventStream API into a simple generator that yields typed Python objects.

Key concepts:
- Bedrock's converse_stream API is stateless — every call sends the FULL
  conversation history. There's no server-side session. We manage all state.
- The API returns an EventStream (a server-sent-events style iterator) that
  yields chunks as the model generates them. This enables streaming to the UI.
- This is a SYNCHRONOUS generator. Textual runs it in a background thread
  via Workers, keeping the UI responsive while we block waiting for chunks.

Translation responsibility:
- This client accepts internal types (list[Turn] with ContentBlocks) and
  translates them to Bedrock's wire format. No Bedrock-specific types leak
  out to the rest of the application.
- Tool-use responses are parsed from Bedrock's streaming format and emitted
  as ToolUseEvent objects.
"""

import json
import logging
import time
from collections.abc import Generator
from dataclasses import dataclass

import boto3

from archie.session import Turn
from archie.types import TextBlock, ToolResultBlock, ToolUseBlock

log = logging.getLogger(__name__)


# --- Stream Event types ---
# These are what stream() yields to callers. Provider-agnostic from the
# caller's perspective (though they originate from Bedrock's event stream).


@dataclass
class TextDelta:
    """A chunk of generated text. Arrives incrementally as the model types."""

    text: str


@dataclass
class ToolUseEvent:
    """A complete tool call parsed from the stream.

    Emitted when a tool_use content block finishes streaming. The input
    field contains the fully parsed JSON arguments (accumulated from
    multiple contentBlockDelta events).
    """

    tool_use_id: str
    name: str
    input: dict


@dataclass
class Usage:
    """Token usage stats. Arrives once at the end of generation.

    input_tokens: how many tokens were in the request (system + messages).
        This INCLUDES the full conversation history re-sent every turn.
    output_tokens: how many tokens the model generated in this response.
    """

    input_tokens: int
    output_tokens: int


@dataclass
class Done:
    """Generation finished. stop_reason tells you why.

    Common stop reasons:
    - "end_turn": model finished naturally
    - "max_tokens": hit the output token limit
    - "tool_use": model wants to call a tool
    """

    stop_reason: str


# Type alias for the union of all possible stream events.
type StreamEvent = TextDelta | ToolUseEvent | Usage | Done


def _turns_to_bedrock_messages(turns: list[Turn]) -> list[dict]:
    """Translate internal Turn objects to Bedrock's message format.

    Bedrock expects messages as:
        [{"role": "user"|"assistant", "content": [<content blocks>]}]

    Content block formats:
        Text:       {"text": "..."}
        ToolUse:    {"toolUse": {"toolUseId": "...", "name": "...", "input": {...}}}
        ToolResult: {"toolResult": {"toolUseId": "...", "content": [{"text": "..."}], "status": "success"|"error"}}
    """
    messages = []
    for turn in turns:
        content_blocks = []
        for block in turn.content:
            match block:
                case TextBlock(text=text):
                    content_blocks.append({"text": text})
                case ToolUseBlock(tool_use_id=tid, name=name, input=inp):
                    content_blocks.append(
                        {
                            "toolUse": {
                                "toolUseId": tid,
                                "name": name,
                                "input": inp,
                            }
                        }
                    )
                case ToolResultBlock(tool_use_id=tid, content=content, is_error=is_error):
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


class BedrockClient:
    """Wrapper around Bedrock's converse_stream API.

    Handles:
    - Translating internal types to Bedrock wire format
    - Parsing the EventStream into typed events (including tool-use)
    - Tracking content block state during streaming (for JSON arg accumulation)
    - Retrying on throttling (exponential backoff)
    - Failing fast on validation errors (context too large)
    """

    def __init__(self, model_id: str, region: str):
        self.model_id = model_id
        # boto3 client is created once and reused for all requests.
        # It handles connection pooling and credential refresh internally.
        self.client = boto3.client("bedrock-runtime", region_name=region)

    def stream(
        self,
        messages: list[Turn] | list[dict],
        system: str,
        tool_config: list[dict] | None = None,
    ) -> Generator[StreamEvent]:
        """Send a conversation to Bedrock and yield response events.

        Args:
            messages: Conversation history as internal Turn objects (preferred)
                or raw Bedrock-format dicts (for backward compat during transition).
            system: The system prompt. Sent as a separate field (not in messages).
            tool_config: Optional list of tool definitions in Bedrock's format.
                Passed directly as the toolConfig.tools field.

        Yields:
            TextDelta: As each chunk of text arrives.
            ToolUseEvent: When a complete tool call has been parsed from the stream.
            Usage: Once, after generation completes.
            Done: Once, indicating why generation stopped.
        """
        # Translate internal types to Bedrock format if needed
        if messages and isinstance(messages[0], Turn):
            bedrock_messages = _turns_to_bedrock_messages(messages)
        else:
            bedrock_messages = messages

        # Build the request
        params: dict = {
            "modelId": self.model_id,
            "messages": bedrock_messages,
            "system": [{"text": system}],
        }

        # Add tool configuration if tools are available
        if tool_config:
            params["toolConfig"] = {"tools": tool_config}

        response = self._call_with_retry(params)
        stream = response["stream"]

        # --- Content block state tracking ---
        # Bedrock streams tool call arguments as JSON string fragments across
        # multiple contentBlockDelta events. We must accumulate them and parse
        # the complete JSON when contentBlockStop arrives.
        current_block_type: str | None = None  # "text" or "tool_use"
        current_tool_use_id: str = ""
        current_tool_name: str = ""
        current_tool_input_json: str = ""  # Accumulated JSON string fragments

        for event in stream:
            # --- Content block start ---
            # Tells us what type of block is beginning (text or tool_use).
            if "contentBlockStart" in event:
                start = event["contentBlockStart"].get("start", {})
                if "toolUse" in start:
                    # A tool_use block is starting — track its metadata
                    current_block_type = "tool_use"
                    current_tool_use_id = start["toolUse"]["toolUseId"]
                    current_tool_name = start["toolUse"]["name"]
                    current_tool_input_json = ""
                else:
                    current_block_type = "text"

            # --- Content block delta ---
            # Carries incremental content for the current block.
            elif "contentBlockDelta" in event:
                delta = event["contentBlockDelta"]["delta"]
                if "text" in delta:
                    yield TextDelta(text=delta["text"])
                elif "toolUse" in delta:
                    # Tool input arrives as JSON string fragments — accumulate them
                    current_tool_input_json += delta["toolUse"].get("input", "")

            # --- Content block stop ---
            # The current block is complete. If it was a tool_use, parse and emit.
            elif "contentBlockStop" in event:
                if current_block_type == "tool_use":
                    # Parse the accumulated JSON arguments
                    try:
                        parsed_input = (
                            json.loads(current_tool_input_json) if current_tool_input_json else {}
                        )
                    except json.JSONDecodeError:
                        log.warning(
                            "Failed to parse tool args JSON for %s: %s",
                            current_tool_name,
                            current_tool_input_json[:200],
                        )
                        parsed_input = {}
                    yield ToolUseEvent(
                        tool_use_id=current_tool_use_id,
                        name=current_tool_name,
                        input=parsed_input,
                    )
                # Reset block state
                current_block_type = None

            # --- Usage metadata ---
            elif "metadata" in event:
                usage = event["metadata"].get("usage", {})
                yield Usage(
                    input_tokens=usage.get("inputTokens", 0),
                    output_tokens=usage.get("outputTokens", 0),
                )

            # --- Stop signal ---
            elif "messageStop" in event:
                yield Done(stop_reason=event["messageStop"].get("stopReason", "end_turn"))

    def _call_with_retry(self, params: dict, max_retries: int = 3) -> dict:
        """Call converse_stream with retry on throttling.

        Bedrock throttles when you exceed your account's requests-per-second.
        We retry with exponential backoff (1s, 2s, 4s) before giving up.

        ValidationException (usually "context too large") is NOT retried —
        it's a permanent error that won't resolve by waiting.
        """
        for attempt in range(max_retries):
            try:
                return self.client.converse_stream(**params)
            except self.client.exceptions.ThrottlingException:
                if attempt == max_retries - 1:
                    raise
                delay = 2**attempt
                log.warning(
                    "Throttled by Bedrock, retrying in %ds (attempt %d/%d)",
                    delay,
                    attempt + 1,
                    max_retries,
                )
                time.sleep(delay)
            except self.client.exceptions.ValidationException:
                raise
        raise RuntimeError("Unreachable")
