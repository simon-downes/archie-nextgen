"""Tests for Bedrock LLM client."""

from unittest.mock import MagicMock, patch

import pytest

from archie.llm import BedrockClient, Done, TextDelta, Usage


@pytest.fixture
def mock_client():
    """Create a BedrockClient with mocked boto3."""
    with patch("archie.llm.boto3") as mock_boto3:
        mock_runtime = MagicMock()
        mock_boto3.client.return_value = mock_runtime

        # Set up exception classes that inherit from Exception
        class ThrottlingException(Exception):
            pass

        class ValidationException(Exception):
            pass

        mock_runtime.exceptions.ThrottlingException = ThrottlingException
        mock_runtime.exceptions.ValidationException = ValidationException

        client = BedrockClient(
            model_id="anthropic.claude-sonnet-4-20250514-v1:0",
            region="us-east-1",
        )
        yield client, mock_runtime


def _make_stream_response(events):
    """Create a mock converse_stream response with given events."""
    return {"stream": iter(events)}


def test_stream_text_deltas(mock_client):
    """Yields TextDelta events from content blocks."""
    client, runtime = mock_client
    runtime.converse_stream.return_value = _make_stream_response(
        [
            {"contentBlockDelta": {"delta": {"text": "Hello"}}},
            {"contentBlockDelta": {"delta": {"text": " world"}}},
            {"metadata": {"usage": {"inputTokens": 10, "outputTokens": 5}}},
            {"messageStop": {"stopReason": "end_turn"}},
        ]
    )

    events = list(
        client.stream(
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            system="Be helpful.",
        )
    )

    assert events[0] == TextDelta(text="Hello")
    assert events[1] == TextDelta(text=" world")
    assert events[2] == Usage(input_tokens=10, output_tokens=5)
    assert events[3] == Done(stop_reason="end_turn")


def test_stream_passes_system_and_messages(mock_client):
    """Verifies correct params passed to converse_stream."""
    client, runtime = mock_client
    runtime.converse_stream.return_value = _make_stream_response(
        [
            {"messageStop": {"stopReason": "end_turn"}},
        ]
    )

    messages = [{"role": "user", "content": [{"text": "test"}]}]
    list(client.stream(messages=messages, system="You are archie."))

    runtime.converse_stream.assert_called_once_with(
        modelId="anthropic.claude-sonnet-4-20250514-v1:0",
        messages=messages,
        system=[{"text": "You are archie."}],
    )


def test_stream_retry_on_throttle(mock_client):
    """Retries on ThrottlingException."""
    client, runtime = mock_client
    throttle_exc = runtime.exceptions.ThrottlingException

    call_count = 0

    def side_effect(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise throttle_exc()
        return _make_stream_response(
            [
                {"messageStop": {"stopReason": "end_turn"}},
            ]
        )

    runtime.converse_stream.side_effect = side_effect

    events = list(
        client.stream(
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            system="test",
        )
    )

    assert call_count == 3
    assert events[0] == Done(stop_reason="end_turn")


def test_stream_no_retry_on_validation_error(mock_client):
    """ValidationException (context too large) is not retried."""
    client, runtime = mock_client
    validation_exc = runtime.exceptions.ValidationException

    runtime.converse_stream.side_effect = validation_exc("context too large")

    with pytest.raises(validation_exc):
        list(
            client.stream(
                messages=[{"role": "user", "content": [{"text": "hi"}]}],
                system="test",
            )
        )

    assert runtime.converse_stream.call_count == 1


def test_usage_defaults_to_zero(mock_client):
    """Missing usage fields default to 0."""
    client, runtime = mock_client
    runtime.converse_stream.return_value = _make_stream_response(
        [
            {"metadata": {"usage": {}}},
            {"messageStop": {"stopReason": "end_turn"}},
        ]
    )

    events = list(
        client.stream(
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            system="test",
        )
    )

    assert events[0] == Usage(input_tokens=0, output_tokens=0)
