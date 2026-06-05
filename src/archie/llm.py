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
"""

import logging
import time
from collections.abc import Generator
from dataclasses import dataclass

import boto3

log = logging.getLogger(__name__)


# --- Event types ---
# These are the typed events our stream() generator yields.
# The caller doesn't need to understand Bedrock's raw event format —
# it just handles these three simple types.


@dataclass
class TextDelta:
    """A chunk of generated text. Arrives incrementally as the model types."""

    text: str


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
    - "tool_use": model wants to call a tool (Phase 2)
    """

    stop_reason: str


# Type alias for the union of all possible stream events.
# Callers can use isinstance() checks or match statements.
type StreamEvent = TextDelta | Usage | Done


class BedrockClient:
    """Wrapper around Bedrock's converse_stream API.

    Handles:
    - Building the correct request format
    - Parsing the EventStream into typed events
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
        messages: list[dict],
        system: str,
    ) -> Generator[StreamEvent]:
        """Send a conversation to Bedrock and yield response events.

        Args:
            messages: Full conversation history in Bedrock's format.
                Each message is {"role": "user"|"assistant", "content": [{"text": "..."}]}.
                This is the ENTIRE history — Bedrock has no memory between calls.
            system: The system prompt. Sent as a separate field (not in messages).

        Yields:
            TextDelta: As each chunk of text arrives.
            Usage: Once, after generation completes.
            Done: Once, indicating why generation stopped.

        This is synchronous — it blocks on each chunk from the EventStream.
        Run in a thread if you need async behaviour.
        """
        # Build the request. Bedrock expects system prompt as a list of text blocks.
        params = {
            "modelId": self.model_id,
            "messages": messages,
            "system": [{"text": system}],
        }

        response = self._call_with_retry(params)

        # The response contains a "stream" key which is an EventStream iterator.
        # Each iteration yields one event dict from the server.
        stream = response["stream"]

        for event in stream:
            # --- Text generation ---
            # contentBlockDelta events carry chunks of generated text.
            # These arrive frequently (every few tokens) enabling smooth streaming.
            if "contentBlockDelta" in event:
                delta = event["contentBlockDelta"]["delta"]
                if "text" in delta:
                    yield TextDelta(text=delta["text"])

            # --- Usage metadata ---
            # The metadata event arrives AFTER all content is generated.
            # It contains the token counts for billing/tracking.
            elif "metadata" in event:
                usage = event["metadata"].get("usage", {})
                yield Usage(
                    input_tokens=usage.get("inputTokens", 0),
                    output_tokens=usage.get("outputTokens", 0),
                )

            # --- Stop signal ---
            # messageStop tells us generation is complete and why.
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
        # This should be unreachable — the loop either returns or raises.
        # But the type checker can't prove the loop always terminates,
        # so we need this to satisfy the return type.
        raise RuntimeError("Unreachable")
