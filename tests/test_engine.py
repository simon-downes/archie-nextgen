"""Tests for Engine orchestration loop."""

from unittest.mock import MagicMock

import pytest

from archie.engine import Engine
from archie.llm.bedrock import Done, ToolUseEvent, Usage
from archie.llm.bedrock import TextDelta as LlmTextDelta
from archie.models import ModelInfo
from archie.session import Session
from archie.tools import ToolRegistry, ToolSpec
from archie.types import TextDelta, ToolCallResult, ToolCallStart, TurnComplete


@pytest.fixture
def model_info():
    return ModelInfo(
        name="Test Model",
        max_context_tokens=100_000,
        input_price_per_m=1.0,
        output_price_per_m=5.0,
    )


@pytest.fixture
def session(tmp_path, model_info):
    """Create a session that writes to tmp_path."""
    from unittest.mock import patch

    with patch("archie.session.SESSIONS_DIR", tmp_path):
        s = Session(model_id="test-model", model_info=model_info, project_name="test")
    # Ensure SESSIONS_DIR is patched for flush_turn calls during tests
    s._log_path = tmp_path / f"{s.session_id}.jsonl"
    return s


@pytest.fixture
def registry():
    """Create a registry with a simple echo tool."""
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="echo",
            description="Echoes input back",
            schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            handler=lambda params: f"Echo: {params['text']}",
        )
    )
    return reg


def _mock_llm_stream(*call_responses):
    """Create a mock LLM client that returns different responses on each call.

    Each call_response is a list of stream events to yield for that call.
    """
    mock = MagicMock()
    mock.stream = MagicMock(side_effect=[iter(r) for r in call_responses])
    return mock


class TestEnginePlainResponse:
    """Tests for simple text-only responses (no tool use)."""

    def test_plain_text_response(self, session, registry):
        """Engine yields TextDelta events and TurnComplete for a plain response."""
        llm = _mock_llm_stream(
            [
                LlmTextDelta(text="Hello "),
                LlmTextDelta(text="world!"),
                Usage(input_tokens=10, output_tokens=5),
                Done(stop_reason="end_turn"),
            ]
        )

        engine = Engine(llm, session, registry, "Be helpful.")
        events = list(engine.run("Hi"))

        assert events[0] == TextDelta(text="Hello ")
        assert events[1] == TextDelta(text="world!")
        assert events[2] == TurnComplete(input_tokens=10, output_tokens=5, stop_reason="end_turn")

    def test_records_turns_in_session(self, session, registry):
        """Engine records user and assistant turns in the session."""
        llm = _mock_llm_stream(
            [
                LlmTextDelta(text="Response"),
                Usage(input_tokens=10, output_tokens=5),
                Done(stop_reason="end_turn"),
            ]
        )

        engine = Engine(llm, session, registry, "Be helpful.")
        list(engine.run("Hello"))

        assert len(session.turns) == 2
        assert session.turns[0].role == "user"
        assert session.turns[0].text == "Hello"
        assert session.turns[1].role == "assistant"
        assert session.turns[1].text == "Response"


class TestEngineToolUse:
    """Tests for the tool-use loop."""

    def test_single_tool_call(self, session, registry):
        """Engine calls tool, sends result back to LLM, yields all events."""
        llm = _mock_llm_stream(
            # First call: model wants to use a tool
            [
                LlmTextDelta(text="Let me echo."),
                ToolUseEvent(tool_use_id="tu_1", name="echo", input={"text": "hello"}),
                Usage(input_tokens=20, output_tokens=15),
                Done(stop_reason="tool_use"),
            ],
            # Second call: model responds with final text after seeing tool result
            [
                LlmTextDelta(text="Done!"),
                Usage(input_tokens=30, output_tokens=10),
                Done(stop_reason="end_turn"),
            ],
        )

        engine = Engine(llm, session, registry, "Be helpful.")
        events = list(engine.run("echo hello"))

        # Check event sequence
        assert events[0] == TextDelta(text="Let me echo.")
        assert events[1] == ToolCallStart(tool_use_id="tu_1", name="echo", input={"text": "hello"})
        assert events[2] == ToolCallResult(
            tool_use_id="tu_1", name="echo", content="Echo: hello", is_error=False
        )
        assert events[3] == TextDelta(text="Done!")
        assert events[4] == TurnComplete(input_tokens=50, output_tokens=25, stop_reason="end_turn")

    def test_multi_tool_calls_in_one_response(self, session):
        """Multiple tool calls in a single LLM response are all executed."""
        reg = ToolRegistry()
        reg.register(
            ToolSpec(name="tool_a", description="A", schema={}, handler=lambda p: "result_a")
        )
        reg.register(
            ToolSpec(name="tool_b", description="B", schema={}, handler=lambda p: "result_b")
        )

        llm = _mock_llm_stream(
            [
                ToolUseEvent(tool_use_id="tu_1", name="tool_a", input={}),
                ToolUseEvent(tool_use_id="tu_2", name="tool_b", input={}),
                Usage(input_tokens=10, output_tokens=10),
                Done(stop_reason="tool_use"),
            ],
            [
                LlmTextDelta(text="All done."),
                Usage(input_tokens=20, output_tokens=5),
                Done(stop_reason="end_turn"),
            ],
        )

        engine = Engine(llm, session, reg, "test")
        events = list(engine.run("do both"))

        # Both tool calls should have start + result events
        starts = [e for e in events if isinstance(e, ToolCallStart)]
        results = [e for e in events if isinstance(e, ToolCallResult)]
        assert len(starts) == 2
        assert len(results) == 2
        assert results[0].content == "result_a"
        assert results[1].content == "result_b"

    def test_tool_error_recovery(self, session):
        """Tool exceptions become error results sent to the model."""
        reg = ToolRegistry()
        reg.register(
            ToolSpec(
                name="broken",
                description="Always fails",
                schema={},
                handler=lambda p: (_ for _ in ()).throw(RuntimeError("oops")),
            )
        )

        llm = _mock_llm_stream(
            [
                ToolUseEvent(tool_use_id="tu_1", name="broken", input={}),
                Usage(input_tokens=10, output_tokens=5),
                Done(stop_reason="tool_use"),
            ],
            [
                LlmTextDelta(text="Sorry, that failed."),
                Usage(input_tokens=20, output_tokens=10),
                Done(stop_reason="end_turn"),
            ],
        )

        engine = Engine(llm, session, reg, "test")
        events = list(engine.run("try broken"))

        results = [e for e in events if isinstance(e, ToolCallResult)]
        assert len(results) == 1
        assert results[0].is_error is True
        assert "oops" in results[0].content

    def test_unknown_tool_returns_error(self, session, registry):
        """Calling a tool not in the registry returns an error result."""
        llm = _mock_llm_stream(
            [
                ToolUseEvent(tool_use_id="tu_1", name="nonexistent", input={}),
                Usage(input_tokens=10, output_tokens=5),
                Done(stop_reason="tool_use"),
            ],
            [
                LlmTextDelta(text="Ok"),
                Usage(input_tokens=20, output_tokens=5),
                Done(stop_reason="end_turn"),
            ],
        )

        engine = Engine(llm, session, registry, "test")
        events = list(engine.run("call unknown"))

        results = [e for e in events if isinstance(e, ToolCallResult)]
        assert results[0].is_error is True
        assert "Unknown tool" in results[0].content


class TestEngineTokenTracking:
    """Tests for token accumulation across LLM calls."""

    def test_tokens_summed_across_calls(self, session, registry):
        """TurnComplete reports total tokens from all LLM calls in this turn."""
        llm = _mock_llm_stream(
            [
                ToolUseEvent(tool_use_id="tu_1", name="echo", input={"text": "x"}),
                Usage(input_tokens=100, output_tokens=50),
                Done(stop_reason="tool_use"),
            ],
            [
                LlmTextDelta(text="Done"),
                Usage(input_tokens=200, output_tokens=30),
                Done(stop_reason="end_turn"),
            ],
        )

        engine = Engine(llm, session, registry, "test")
        events = list(engine.run("go"))

        complete = [e for e in events if isinstance(e, TurnComplete)][0]
        assert complete.input_tokens == 300
        assert complete.output_tokens == 80


class TestEngineConsecutiveCallDetection:
    """Tests for loop prevention."""

    def test_warns_at_3_consecutive(self, session, registry):
        """Third consecutive identical call gets a warning appended."""
        # We need 3 consecutive identical calls — mock 4 LLM calls total
        llm = _mock_llm_stream(
            [
                ToolUseEvent(tool_use_id="tu_1", name="echo", input={"text": "same"}),
                Usage(input_tokens=10, output_tokens=5),
                Done(stop_reason="tool_use"),
            ],
            [
                ToolUseEvent(tool_use_id="tu_2", name="echo", input={"text": "same"}),
                Usage(input_tokens=10, output_tokens=5),
                Done(stop_reason="tool_use"),
            ],
            [
                ToolUseEvent(tool_use_id="tu_3", name="echo", input={"text": "same"}),
                Usage(input_tokens=10, output_tokens=5),
                Done(stop_reason="tool_use"),
            ],
            [
                LlmTextDelta(text="Ok"),
                Usage(input_tokens=10, output_tokens=5),
                Done(stop_reason="end_turn"),
            ],
        )

        engine = Engine(llm, session, registry, "test")
        events = list(engine.run("repeat"))

        results = [e for e in events if isinstance(e, ToolCallResult)]
        # Third result should have warning
        assert "Warning" in results[2].content
        assert "3 times" in results[2].content

    def test_blocks_at_4_consecutive(self, session, registry):
        """Fourth consecutive identical call is hard-blocked."""
        llm = _mock_llm_stream(
            [
                ToolUseEvent(tool_use_id="tu_1", name="echo", input={"text": "same"}),
                Usage(input_tokens=10, output_tokens=5),
                Done(stop_reason="tool_use"),
            ],
            [
                ToolUseEvent(tool_use_id="tu_2", name="echo", input={"text": "same"}),
                Usage(input_tokens=10, output_tokens=5),
                Done(stop_reason="tool_use"),
            ],
            [
                ToolUseEvent(tool_use_id="tu_3", name="echo", input={"text": "same"}),
                Usage(input_tokens=10, output_tokens=5),
                Done(stop_reason="tool_use"),
            ],
            [
                ToolUseEvent(tool_use_id="tu_4", name="echo", input={"text": "same"}),
                Usage(input_tokens=10, output_tokens=5),
                Done(stop_reason="tool_use"),
            ],
            [
                LlmTextDelta(text="Ok"),
                Usage(input_tokens=10, output_tokens=5),
                Done(stop_reason="end_turn"),
            ],
        )

        engine = Engine(llm, session, registry, "test")
        events = list(engine.run("repeat"))

        results = [e for e in events if isinstance(e, ToolCallResult)]
        # Fourth result should be an error (blocked)
        assert results[3].is_error is True
        assert "Blocked" in results[3].content

    def test_counter_resets_on_different_call(self, session, registry):
        """Counter resets when a different tool+args combination is used."""
        llm = _mock_llm_stream(
            [
                ToolUseEvent(tool_use_id="tu_1", name="echo", input={"text": "a"}),
                Usage(input_tokens=10, output_tokens=5),
                Done(stop_reason="tool_use"),
            ],
            [
                ToolUseEvent(tool_use_id="tu_2", name="echo", input={"text": "a"}),
                Usage(input_tokens=10, output_tokens=5),
                Done(stop_reason="tool_use"),
            ],
            [
                # Different args — should reset counter
                ToolUseEvent(tool_use_id="tu_3", name="echo", input={"text": "b"}),
                Usage(input_tokens=10, output_tokens=5),
                Done(stop_reason="tool_use"),
            ],
            [
                ToolUseEvent(tool_use_id="tu_4", name="echo", input={"text": "b"}),
                Usage(input_tokens=10, output_tokens=5),
                Done(stop_reason="tool_use"),
            ],
            [
                LlmTextDelta(text="Done"),
                Usage(input_tokens=10, output_tokens=5),
                Done(stop_reason="end_turn"),
            ],
        )

        engine = Engine(llm, session, registry, "test")
        events = list(engine.run("go"))

        results = [e for e in events if isinstance(e, ToolCallResult)]
        # No warnings because counter reset between the two pairs
        for r in results:
            assert "Warning" not in r.content
            assert not r.is_error
