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

Logging:
- request_end events carry duration, stop_reason, usage, and the AWS request id.
- Full request payloads go to the separate payload logger (archie.payloads),
  enabled via ARCHIE_LOG_PAYLOADS=1 — they're O(n²) over a session, too big
  for the main log.
"""

import json
import logging
import time
from collections.abc import Generator
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.config import Config

from archie.logs import PAYLOAD_LOGGER_NAME, log_event, payloads_enabled
from archie.session import Turn
from archie.types import TextBlock, ToolResultBlock, ToolUseBlock

log = logging.getLogger(__name__)
payload_log = logging.getLogger(PAYLOAD_LOGGER_NAME)


# --- Stream Event types ---


@dataclass
class TextDelta:
    """A chunk of generated text. Arrives incrementally as the model types."""

    text: str


@dataclass
class ToolUseEvent:
    """A complete tool call parsed from the stream.

    input_truncated is True when the args JSON didn't parse — typically because
    generation stopped at the max output token limit mid-call. The agent loop
    must not execute such a call; it pairs it with an error result instead.
    """

    tool_use_id: str
    name: str
    input: dict
    input_truncated: bool = False


@dataclass
class Usage:
    """Token usage stats with four billing categories.

    The split matters because cache_read tokens are ~10x cheaper than fresh input.
    Tracking them separately is how we prove prompt caching is working.
    """

    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_write_input_tokens: int = 0


@dataclass
class Done:
    """Generation finished. stop_reason tells you why."""

    stop_reason: str


type StreamEvent = TextDelta | ToolUseEvent | Usage | Done


def _turns_to_bedrock_messages(turns: list[Turn]) -> list[dict]:
    """Translate internal Turn objects to Bedrock's message format."""
    messages = []
    for turn in turns:
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
    - Prompt cache point placement (system + history tail)
    - Structured debug logging of requests and usage
    - Retrying on throttling (exponential backoff)
    - Failing fast on validation errors (context too large)
    """

    def __init__(self, model_id: str, region: str, max_output_tokens: int = 32_768):
        self.model_id = model_id
        self._region = region
        self.max_output_tokens = max_output_tokens
        self.client = boto3.client(
            "bedrock-runtime",
            region_name=region,
            config=Config(read_timeout=300, retries={"max_attempts": 0}),
        )
        self._cache_supported: bool = True

    def stream(
        self,
        messages: list[Turn],
        system: str,
        tool_config: list[dict] | None = None,
    ) -> Generator[StreamEvent]:
        """Send a conversation to Bedrock and yield response events."""
        bedrock_messages = _turns_to_bedrock_messages(messages)

        # System prompt cache point
        system_blocks = [{"text": system}]
        if self._cache_supported:
            system_blocks.append({"cachePoint": {"type": "default"}})

        # History tail cache point — appended to last message's content list
        if self._cache_supported and bedrock_messages:
            bedrock_messages[-1]["content"].append({"cachePoint": {"type": "default"}})

        params: dict = {
            "modelId": self.model_id,
            "messages": bedrock_messages,
            "system": system_blocks,
            "inferenceConfig": {"maxTokens": self.max_output_tokens},
        }

        if tool_config:
            params["toolConfig"] = {"tools": tool_config}

        self._log_request(params)
        t0 = time.time()
        response = self._call_with_retry(params)
        request_id = response.get("ResponseMetadata", {}).get("RequestId", "")
        stream = response["stream"]

        current_block_type: str | None = None
        current_tool_use_id: str = ""
        current_tool_name: str = ""
        current_tool_input_json: str = ""
        usage: Usage | None = None
        stop_reason: str = "unknown"

        for event in stream:
            if "contentBlockStart" in event:
                start = event["contentBlockStart"].get("start", {})
                if "toolUse" in start:
                    current_block_type = "tool_use"
                    current_tool_use_id = start["toolUse"]["toolUseId"]
                    current_tool_name = start["toolUse"]["name"]
                    current_tool_input_json = ""
                else:
                    current_block_type = "text"

            elif "contentBlockDelta" in event:
                delta = event["contentBlockDelta"]["delta"]
                if "text" in delta:
                    yield TextDelta(text=delta["text"])
                elif "toolUse" in delta:
                    current_tool_input_json += delta["toolUse"].get("input", "")

            elif "contentBlockStop" in event:
                if current_block_type == "tool_use":
                    input_truncated = False
                    try:
                        parsed_input = (
                            json.loads(current_tool_input_json) if current_tool_input_json else {}
                        )
                    except json.JSONDecodeError:
                        # Almost always means generation hit maxTokens mid-call.
                        # Flag it so the agent loop pairs it with an error result
                        # instead of executing a half-formed call.
                        log.warning(
                            "Failed to parse tool args JSON for %s (likely max_tokens): %s",
                            current_tool_name,
                            current_tool_input_json[:200],
                        )
                        parsed_input = {}
                        input_truncated = True
                    yield ToolUseEvent(
                        tool_use_id=current_tool_use_id,
                        name=current_tool_name,
                        input=parsed_input,
                        input_truncated=input_truncated,
                    )
                current_block_type = None

            elif "metadata" in event:
                raw = event["metadata"].get("usage", {})
                usage = Usage(
                    input_tokens=raw.get("inputTokens", 0),
                    output_tokens=raw.get("outputTokens", 0),
                    cache_read_input_tokens=raw.get("cacheReadInputTokens", 0),
                    cache_write_input_tokens=raw.get("cacheWriteInputTokens", 0),
                )
                yield usage

            elif "messageStop" in event:
                stop_reason = event["messageStop"].get("stopReason", "end_turn")
                yield Done(stop_reason=stop_reason)

        log_event(
            log,
            logging.INFO,
            "request_end",
            model=self.model_id,
            duration_s=round(time.time() - t0, 2),
            stop_reason=stop_reason,
            input=usage.input_tokens if usage else 0,
            output=usage.output_tokens if usage else 0,
            cache_read=usage.cache_read_input_tokens if usage else 0,
            cache_write=usage.cache_write_input_tokens if usage else 0,
            aws_request_id=request_id,
        )

    def invoke(self, messages: list[Turn], system: str) -> str:
        """Non-streaming call using boto3 converse(). Returns the response text."""
        params = {
            "modelId": self.model_id,
            "messages": _turns_to_bedrock_messages(messages),
            "system": [{"text": system}],
        }
        for attempt in range(3):
            try:
                response = self.client.converse(**params)
                break
            except self.client.exceptions.ThrottlingException:
                if attempt == 2:
                    raise
                delay = 2**attempt
                log.warning(
                    "Throttled by Bedrock (invoke), retrying",
                    extra={"delay_s": delay, "attempt": attempt + 1, "max_retries": 3},
                )
                time.sleep(delay)

        output = response.get("output", {}).get("message", {}).get("content", [])
        return "".join(block.get("text", "") for block in output)

    def _call_with_retry(self, params: dict, max_retries: int = 3) -> dict:
        """Call converse_stream with retry on throttling.

        If cachePoint is rejected (unsupported model — raised as either
        ValidationException or AccessDeniedException), retry without it once
        and disable caching for future calls.
        """
        for attempt in range(max_retries):
            try:
                return self.client.converse_stream(**params)
            except self.client.exceptions.ThrottlingException:
                if attempt == max_retries - 1:
                    raise
                delay = 2**attempt
                log.warning(
                    "Throttled by Bedrock, retrying",
                    extra={"delay_s": delay, "attempt": attempt + 1, "max_retries": max_retries},
                )
                time.sleep(delay)
            except (
                self.client.exceptions.ValidationException,
                self.client.exceptions.AccessDeniedException,
            ) as e:
                # Unsupported prompt caching surfaces as ValidationException on some
                # models and AccessDeniedException ("prompt caching") on others.
                msg_text = str(e)
                if self._cache_supported and (
                    "cachePoint" in msg_text or "prompt caching" in msg_text
                ):
                    log.warning("cachePoint not supported, disabling prompt caching")
                    self._cache_supported = False
                    # Strip all cachePoint blocks from system and messages
                    params["system"] = [
                        b for b in params.get("system", []) if "cachePoint" not in b
                    ]
                    for msg in params.get("messages", []):
                        msg["content"] = [
                            b for b in msg.get("content", []) if "cachePoint" not in b
                        ]
                    return self.client.converse_stream(**params)
                raise
        raise RuntimeError("Unreachable")

    # --- Logging helpers ---

    def _log_request(self, params: dict) -> None:
        """Log the full request to the payload logger (no-op unless enabled).

        Per-block truncation keeps the request *shape* visible (message count,
        cache point positions, tool declarations) even when individual content
        is huge. Goes to payloads.log, not the main log — payloads re-log the
        whole conversation every iteration and would burn the rotation budget.
        """
        if not payloads_enabled():
            return
        try:
            payload = self._truncate_blocks(json.loads(json.dumps(params, default=str)))
            payload_log.debug("", extra={"event": "request_payload", "payload": payload})
        except Exception:
            payload_log.debug("Could not serialize request for logging")

    def _truncate_blocks(self, obj: Any, limit: int = 2048) -> Any:
        """Recursively truncate string leaves so the request shape stays visible."""
        if isinstance(obj, dict):
            return {k: self._truncate_blocks(v, limit) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._truncate_blocks(v, limit) for v in obj]
        if isinstance(obj, str) and len(obj) > limit:
            return obj[:limit] + f"…[{len(obj) - limit} more chars]"
        return obj
