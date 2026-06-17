"""Tests for Bedrock LLM client."""

from unittest.mock import MagicMock, patch

import pytest

from archie.llm import BedrockClient, Done, TextDelta, ToolUseEvent, Usage
from archie.session import Turn
from archie.types import TextBlock, ToolResultBlock, ToolUseBlock


def _user_turn(text: str) -> Turn:
    """Helper to create a simple user Turn for tests."""
    return Turn(id="t0001", role="user", content=[TextBlock(text=text)])


@pytest.fixture
def mock_client():
    """Create a BedrockClient with mocked boto3."""
    with patch("archie.llm.bedrock.boto3") as mock_boto3:
        mock_runtime = MagicMock()
        mock_boto3.client.return_value = mock_runtime

        class ThrottlingException(Exception):
            pass

        class ValidationException(Exception):
            pass

        class AccessDeniedException(Exception):
            pass

        mock_runtime.exceptions.ThrottlingException = ThrottlingException
        mock_runtime.exceptions.ValidationException = ValidationException
        mock_runtime.exceptions.AccessDeniedException = AccessDeniedException

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
            {"contentBlockStart": {"start": {}, "contentBlockIndex": 0}},
            {"contentBlockDelta": {"delta": {"text": "Hello"}}},
            {"contentBlockDelta": {"delta": {"text": " world"}}},
            {"contentBlockStop": {"contentBlockIndex": 0}},
            {"metadata": {"usage": {"inputTokens": 10, "outputTokens": 5}}},
            {"messageStop": {"stopReason": "end_turn"}},
        ]
    )

    events = list(
        client.stream(
            messages=[_user_turn("hi")],
            system="Be helpful.",
        )
    )

    assert events[0] == TextDelta(text="Hello")
    assert events[1] == TextDelta(text=" world")
    assert events[2] == Usage(input_tokens=10, output_tokens=5)
    assert events[3] == Done(stop_reason="end_turn")


def test_stream_accepts_turn_objects(mock_client):
    """stream() accepts list[Turn] and translates to Bedrock format."""
    client, runtime = mock_client
    runtime.converse_stream.return_value = _make_stream_response(
        [
            {"messageStop": {"stopReason": "end_turn"}},
        ]
    )

    turns = [
        Turn(id="t0001", role="user", content=[TextBlock(text="hello")]),
        Turn(
            id="t0002",
            role="assistant",
            content=[
                TextBlock(text="I'll read that"),
                ToolUseBlock(tool_use_id="tu_1", name="read_file", input={"path": "x.py"}),
            ],
        ),
        Turn(
            id="t0003",
            role="user",
            content=[ToolResultBlock(tool_use_id="tu_1", content="file content", is_error=False)],
        ),
    ]

    list(client.stream(messages=turns, system="test"))

    # Verify the translated messages passed to Bedrock
    call_kwargs = runtime.converse_stream.call_args.kwargs
    messages = call_kwargs["messages"]
    assert messages[0] == {"role": "user", "content": [{"text": "hello"}]}
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"][0] == {"text": "I'll read that"}
    assert messages[1]["content"][1] == {
        "toolUse": {"toolUseId": "tu_1", "name": "read_file", "input": {"path": "x.py"}}
    }
    assert messages[2]["content"][0] == {
        "toolResult": {
            "toolUseId": "tu_1",
            "content": [{"text": "file content"}],
            "status": "success",
        }
    }


def test_stream_tool_use_parsing(mock_client):
    """Parses a mixed text + tool_use response into correct events."""
    client, runtime = mock_client
    # Simulate: model outputs text, then calls a tool
    runtime.converse_stream.return_value = _make_stream_response(
        [
            # Text block
            {"contentBlockStart": {"start": {}, "contentBlockIndex": 0}},
            {"contentBlockDelta": {"delta": {"text": "Let me read that file."}}},
            {"contentBlockStop": {"contentBlockIndex": 0}},
            # Tool use block
            {
                "contentBlockStart": {
                    "start": {
                        "toolUse": {
                            "toolUseId": "tooluse_abc123",
                            "name": "read_file",
                        }
                    },
                    "contentBlockIndex": 1,
                }
            },
            # Tool input arrives as JSON string fragments
            {"contentBlockDelta": {"delta": {"toolUse": {"input": '{"path": "src/'}}}},
            {"contentBlockDelta": {"delta": {"toolUse": {"input": 'main.py", "offset": 0}'}}}},
            {"contentBlockStop": {"contentBlockIndex": 1}},
            {"metadata": {"usage": {"inputTokens": 50, "outputTokens": 30}}},
            {"messageStop": {"stopReason": "tool_use"}},
        ]
    )

    events = list(
        client.stream(
            messages=[_user_turn("read main.py")],
            system="test",
        )
    )

    # Text delta from the text block
    assert events[0] == TextDelta(text="Let me read that file.")
    # Parsed tool use event
    assert events[1] == ToolUseEvent(
        tool_use_id="tooluse_abc123",
        name="read_file",
        input={"path": "src/main.py", "offset": 0},
    )
    assert events[2] == Usage(input_tokens=50, output_tokens=30)
    assert events[3] == Done(stop_reason="tool_use")


def test_stream_multiple_tool_calls(mock_client):
    """Handles multiple tool calls in a single response."""
    client, runtime = mock_client
    runtime.converse_stream.return_value = _make_stream_response(
        [
            # First tool call
            {
                "contentBlockStart": {
                    "start": {"toolUse": {"toolUseId": "tu_1", "name": "read_file"}},
                    "contentBlockIndex": 0,
                }
            },
            {"contentBlockDelta": {"delta": {"toolUse": {"input": '{"path": "a.py"}'}}}},
            {"contentBlockStop": {"contentBlockIndex": 0}},
            # Second tool call
            {
                "contentBlockStart": {
                    "start": {"toolUse": {"toolUseId": "tu_2", "name": "search_files"}},
                    "contentBlockIndex": 1,
                }
            },
            {"contentBlockDelta": {"delta": {"toolUse": {"input": '{"pattern": "TODO"}'}}}},
            {"contentBlockStop": {"contentBlockIndex": 1}},
            {"metadata": {"usage": {"inputTokens": 20, "outputTokens": 10}}},
            {"messageStop": {"stopReason": "tool_use"}},
        ]
    )

    events = list(
        client.stream(
            messages=[_user_turn("find todos")],
            system="test",
        )
    )

    assert events[0] == ToolUseEvent(tool_use_id="tu_1", name="read_file", input={"path": "a.py"})
    assert events[1] == ToolUseEvent(
        tool_use_id="tu_2", name="search_files", input={"pattern": "TODO"}
    )


def test_stream_truncated_tool_args_flagged(mock_client):
    """Tool args cut off mid-JSON (max_tokens) yield input_truncated=True."""
    client, runtime = mock_client
    runtime.converse_stream.return_value = _make_stream_response(
        [
            {
                "contentBlockStart": {
                    "start": {"toolUse": {"toolUseId": "tu_cut", "name": "write_file"}},
                    "contentBlockIndex": 0,
                }
            },
            # JSON cut off mid-stream by the output token limit
            {"contentBlockDelta": {"delta": {"toolUse": {"input": '{"path": "src/big.py", "co'}}}},
            {"contentBlockStop": {"contentBlockIndex": 0}},
            {"metadata": {"usage": {"inputTokens": 20, "outputTokens": 10}}},
            {"messageStop": {"stopReason": "max_tokens"}},
        ]
    )

    events = list(
        client.stream(
            messages=[_user_turn("write it")],
            system="test",
        )
    )

    assert events[0] == ToolUseEvent(
        tool_use_id="tu_cut", name="write_file", input={}, input_truncated=True
    )
    assert events[-1] == Done(stop_reason="max_tokens")


def test_stream_tool_config_passed(mock_client):
    """tool_config is passed to Bedrock as toolConfig."""
    client, runtime = mock_client
    runtime.converse_stream.return_value = _make_stream_response(
        [
            {"messageStop": {"stopReason": "end_turn"}},
        ]
    )

    tool_config = [
        {
            "toolSpec": {
                "name": "read_file",
                "description": "Read a file",
                "inputSchema": {"json": {"type": "object", "properties": {}}},
            }
        }
    ]

    list(
        client.stream(
            messages=[_user_turn("hi")],
            system="test",
            tool_config=tool_config,
        )
    )

    call_kwargs = runtime.converse_stream.call_args.kwargs
    assert call_kwargs["toolConfig"] == {"tools": tool_config}


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
        return _make_stream_response([{"messageStop": {"stopReason": "end_turn"}}])

    runtime.converse_stream.side_effect = side_effect

    events = list(
        client.stream(
            messages=[_user_turn("hi")],
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
                messages=[_user_turn("hi")],
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
            messages=[_user_turn("hi")],
            system="test",
        )
    )

    assert events[0] == Usage(input_tokens=0, output_tokens=0)
