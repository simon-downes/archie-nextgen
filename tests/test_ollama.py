"""Tests for the Ollama LLM client.

Mocks the ollama.Client at the boundary — verifies message translation,
streaming event emission, tool-calling, and error handling.
"""

from unittest.mock import MagicMock

import httpx
import ollama as _ollama
import pytest

from archie.llm.bedrock import Done, TextDelta, ToolUseEvent, Usage
from archie.llm.ollama import (
    OllamaClient,
    _tool_config_to_ollama,
    _turns_to_ollama_messages,
)
from archie.session import Turn
from archie.types import TextBlock, ToolResultBlock, ToolUseBlock


class TestTurnsToOllamaMessages:
    """Tests for message translation from internal types to Ollama format."""

    def test_simple_user_message(self):
        turns = [Turn(id="t1", role="user", content=[TextBlock(text="hello")])]
        messages = _turns_to_ollama_messages(turns, system="you are helpful")
        assert messages == [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hello"},
        ]

    def test_multi_turn_conversation(self):
        turns = [
            Turn(id="t1", role="user", content=[TextBlock(text="hi")]),
            Turn(id="t2", role="assistant", content=[TextBlock(text="hello!")]),
            Turn(id="t3", role="user", content=[TextBlock(text="bye")]),
        ]
        messages = _turns_to_ollama_messages(turns, system="sys")
        assert len(messages) == 4  # system + 3 turns
        assert messages[1] == {"role": "user", "content": "hi"}
        assert messages[2] == {"role": "assistant", "content": "hello!"}
        assert messages[3] == {"role": "user", "content": "bye"}

    def test_assistant_with_tool_calls(self):
        turns = [
            Turn(
                id="t1",
                role="assistant",
                content=[
                    TextBlock(text="Let me check."),
                    ToolUseBlock(tool_use_id="abc", name="read_file", input={"path": "x.py"}),
                ],
            ),
        ]
        messages = _turns_to_ollama_messages(turns, system="sys")
        assert len(messages) == 2  # system + assistant
        msg = messages[1]
        assert msg["role"] == "assistant"
        assert msg["content"] == "Let me check."
        assert msg["tool_calls"] == [
            {"function": {"name": "read_file", "arguments": {"path": "x.py"}}}
        ]

    def test_tool_result_messages(self):
        turns = [
            Turn(
                id="t1",
                role="user",
                content=[
                    ToolResultBlock(
                        tool_use_id="abc", content="file contents here", is_error=False
                    ),
                ],
            ),
        ]
        messages = _turns_to_ollama_messages(turns, system="sys")
        assert len(messages) == 2  # system + tool result
        assert messages[1] == {"role": "tool", "content": "file contents here"}


class TestToolConfigTranslation:
    """Tests for Bedrock → Ollama tool schema translation."""

    def test_single_tool(self):
        bedrock_config = [
            {
                "toolSpec": {
                    "name": "read_file",
                    "description": "Read a file",
                    "inputSchema": {
                        "json": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                        }
                    },
                }
            }
        ]
        result = _tool_config_to_ollama(bedrock_config)
        assert result == [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
            }
        ]

    def test_empty_config(self):
        assert _tool_config_to_ollama([]) == []


class TestOllamaClientStream:
    """Tests for OllamaClient.stream() with mocked ollama.Client."""

    def setup_method(self):
        self.mock_client = MagicMock()
        self.client = OllamaClient(model_id="qwen3.6:35b")
        self.client.client = self.mock_client

    def _make_chunk(self, content="", tool_calls=None, done=False, **kwargs):
        """Create a mock streaming chunk."""
        chunk = MagicMock()
        chunk.message.content = content
        chunk.message.tool_calls = tool_calls
        chunk.done = done
        chunk.done_reason = kwargs.get("done_reason", "stop")
        chunk.prompt_eval_count = kwargs.get("prompt_eval_count", 0)
        chunk.eval_count = kwargs.get("eval_count", 0)
        return chunk

    def test_text_streaming(self):
        chunks = [
            self._make_chunk(content="Hello"),
            self._make_chunk(content=" world"),
            self._make_chunk(done=True, prompt_eval_count=10, eval_count=5),
        ]
        self.mock_client.chat.return_value = iter(chunks)

        turns = [Turn(id="t1", role="user", content=[TextBlock(text="hi")])]
        events = list(self.client.stream(turns, system="sys"))

        text_events = [e for e in events if isinstance(e, TextDelta)]
        assert len(text_events) == 2
        assert text_events[0].text == "Hello"
        assert text_events[1].text == " world"

        usage = next(e for e in events if isinstance(e, Usage))
        assert usage.input_tokens == 10
        assert usage.output_tokens == 5
        assert usage.cache_read_input_tokens == 0

        done = next(e for e in events if isinstance(e, Done))
        assert done.stop_reason == "end_turn"

    def test_tool_call_response(self):
        tc = MagicMock()
        tc.function.name = "read_file"
        tc.function.arguments = {"path": "test.py"}

        chunks = [
            self._make_chunk(content="Let me read that."),
            self._make_chunk(tool_calls=[tc], done=True, prompt_eval_count=20, eval_count=10),
        ]
        self.mock_client.chat.return_value = iter(chunks)

        turns = [Turn(id="t1", role="user", content=[TextBlock(text="read test.py")])]
        events = list(self.client.stream(turns, system="sys"))

        tool_events = [e for e in events if isinstance(e, ToolUseEvent)]
        assert len(tool_events) == 1
        assert tool_events[0].name == "read_file"
        assert tool_events[0].input == {"path": "test.py"}
        assert tool_events[0].tool_use_id  # ULID generated
        assert not tool_events[0].input_truncated

        done = next(e for e in events if isinstance(e, Done))
        assert done.stop_reason == "tool_use"

    def test_malformed_tool_args(self):
        tc = MagicMock()
        tc.function.name = "shell"
        tc.function.arguments = "not a dict"  # malformed

        chunks = [
            self._make_chunk(tool_calls=[tc], done=True, prompt_eval_count=5, eval_count=3),
        ]
        self.mock_client.chat.return_value = iter(chunks)

        turns = [Turn(id="t1", role="user", content=[TextBlock(text="run ls")])]
        events = list(self.client.stream(turns, system="sys"))

        tool_events = [e for e in events if isinstance(e, ToolUseEvent)]
        assert len(tool_events) == 1
        assert tool_events[0].input == {}
        assert tool_events[0].input_truncated is True

    def test_max_tokens_stop_reason(self):
        chunks = [
            self._make_chunk(content="partial"),
            self._make_chunk(done=True, done_reason="length", prompt_eval_count=10, eval_count=100),
        ]
        self.mock_client.chat.return_value = iter(chunks)

        turns = [Turn(id="t1", role="user", content=[TextBlock(text="write an essay")])]
        events = list(self.client.stream(turns, system="sys"))

        done = next(e for e in events if isinstance(e, Done))
        assert done.stop_reason == "max_tokens"

    def test_connection_error(self):
        self.mock_client.chat.side_effect = httpx.ConnectError("connection refused")

        turns = [Turn(id="t1", role="user", content=[TextBlock(text="hi")])]
        with pytest.raises(ConnectionError, match="not reachable"):
            list(self.client.stream(turns, system="sys"))

    def test_ollama_response_error(self):
        err = _ollama.ResponseError("model not found")
        self.mock_client.chat.side_effect = err

        turns = [Turn(id="t1", role="user", content=[TextBlock(text="hi")])]
        with pytest.raises(ConnectionError, match="Ollama error"):
            list(self.client.stream(turns, system="sys"))

    def test_tools_passed_to_chat(self):
        chunks = [self._make_chunk(content="ok", done=True, prompt_eval_count=5, eval_count=2)]
        self.mock_client.chat.return_value = iter(chunks)

        tool_config = [
            {
                "toolSpec": {
                    "name": "t",
                    "description": "d",
                    "inputSchema": {"json": {"type": "object"}},
                }
            }
        ]
        turns = [Turn(id="t1", role="user", content=[TextBlock(text="hi")])]
        list(self.client.stream(turns, system="sys", tool_config=tool_config))

        call_kwargs = self.mock_client.chat.call_args[1]
        assert "tools" in call_kwargs
        assert call_kwargs["tools"][0]["function"]["name"] == "t"


class TestOllamaClientInvoke:
    """Tests for OllamaClient.invoke()."""

    def setup_method(self):
        self.mock_client = MagicMock()
        self.client = OllamaClient(model_id="qwen3.6:35b")
        self.client.client = self.mock_client

    def test_returns_text(self):
        response = MagicMock()
        response.message.content = "extracted memories"
        self.mock_client.chat.return_value = response

        turns = [Turn(id="", role="user", content=[TextBlock(text="extract")])]
        result = self.client.invoke(turns, system="sys prompt")

        assert result == "extracted memories"

    def test_connection_error(self):
        self.mock_client.chat.side_effect = httpx.ConnectError("refused")

        turns = [Turn(id="", role="user", content=[TextBlock(text="hi")])]
        with pytest.raises(ConnectionError, match="not reachable"):
            self.client.invoke(turns, system="sys")
