"""Tests for AgentLoop orchestration."""

from unittest.mock import MagicMock

import pytest

from archie.agent import (
    AgentLoop,
    TextDeltaEvent,
    ToolFinished,
    ToolStarted,
    TurnComplete,
    TurnError,
    TurnInterrupted,
    UsageUpdated,
)
from archie.llm.bedrock import Done, ToolUseEvent, Usage
from archie.llm.bedrock import TextDelta as LlmTextDelta
from archie.models import ModelInfo
from archie.session import Session
from archie.tools import ToolRegistry, ToolSpec


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
    s = Session(model_id="test-model", model_info=model_info, project_name="test")
    s._log_path = tmp_path / f"{s.session_id}.jsonl"
    return s


@pytest.fixture
def registry():
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="echo",
            description="Echoes input",
            schema={"type": "object", "properties": {"text": {"type": "string"}}},
            handler=lambda params: f"Echo: {params['text']}",
        )
    )
    return reg


def _mock_llm(*call_responses):
    """Mock LLM that returns different stream events on each call."""
    mock = MagicMock()
    mock.stream = MagicMock(side_effect=[iter(r) for r in call_responses])
    return mock


class TestPlainResponse:
    """Text-only responses (no tool use)."""

    def test_emits_text_deltas_and_turn_complete(self, session, registry):
        llm = _mock_llm(
            [
                LlmTextDelta(text="Hello "),
                LlmTextDelta(text="world!"),
                Usage(input_tokens=10, output_tokens=5),
                Done(stop_reason="end_turn"),
            ]
        )
        events = []
        agent = AgentLoop(llm, session, registry, "system", events.append)

        agent.run_turn("Hi")

        text_events = [e for e in events if isinstance(e, TextDeltaEvent)]
        assert [e.text for e in text_events] == ["Hello ", "world!"]
        assert any(isinstance(e, UsageUpdated) for e in events)
        assert events[-1] == TurnComplete(stop_reason="end_turn")

    def test_records_session_turn(self, session, registry):
        llm = _mock_llm(
            [
                LlmTextDelta(text="OK"),
                Usage(input_tokens=10, output_tokens=2),
                Done(stop_reason="end_turn"),
            ]
        )
        agent = AgentLoop(llm, session, registry, "system", lambda _: None)

        agent.run_turn("Hi")

        assert len(session.turns) == 2  # user + assistant
        assert session.turns[0].role == "user"
        assert session.turns[1].role == "assistant"


class TestToolUse:
    """Multi-tool loop responses."""

    def test_tool_call_loop(self, session, registry):
        """Model calls a tool, gets result, then finishes."""
        llm = _mock_llm(
            # First call: model requests tool
            [
                ToolUseEvent(tool_use_id="t1", name="echo", input={"text": "hi"}),
                Usage(input_tokens=20, output_tokens=10),
                Done(stop_reason="tool_use"),
            ],
            # Second call: model responds with text after getting result
            [
                LlmTextDelta(text="Done"),
                Usage(input_tokens=30, output_tokens=5),
                Done(stop_reason="end_turn"),
            ],
        )
        events = []
        agent = AgentLoop(llm, session, registry, "system", events.append)

        agent.run_turn("Do it")

        assert any(isinstance(e, ToolStarted) and e.name == "echo" for e in events)
        assert any(isinstance(e, ToolFinished) and e.name == "echo" for e in events)
        assert events[-1] == TurnComplete(stop_reason="end_turn")
        # Session should have: user, assistant(tool_use), user(tool_result), assistant(text)
        assert len(session.turns) == 4

    def test_iteration_cap(self, session, registry):
        """Hits the 50-iteration safety cap."""
        # Use distinct inputs to avoid the consecutive-call blocker
        tool_responses = [
            [
                ToolUseEvent(tool_use_id=f"t{i}", name="echo", input={"text": f"msg{i}"}),
                Usage(input_tokens=10, output_tokens=5),
                Done(stop_reason="tool_use"),
            ]
            for i in range(51)
        ]
        llm = _mock_llm(*tool_responses)
        events = []
        agent = AgentLoop(llm, session, registry, "system", events.append)

        agent.run_turn("Loop forever")

        # Should complete without crashing
        assert events[-1] == TurnComplete(stop_reason="tool_use")


class TestInterrupt:
    """Cooperative interruption scenarios."""

    def test_interrupt_mid_stream(self, session, registry):
        """Interrupt during streaming preserves partial text in session."""

        def stream_with_interrupt(*args, **kwargs):
            yield LlmTextDelta(text="Partial ")
            # Simulate interrupt being set after first token
            agent._interrupt.set()
            yield LlmTextDelta(text="text")
            yield Usage(input_tokens=10, output_tokens=3)
            yield Done(stop_reason="end_turn")

        llm = MagicMock()
        llm.stream = MagicMock(side_effect=stream_with_interrupt)
        events = []
        agent = AgentLoop(llm, session, registry, "system", events.append)

        agent.run_turn("Say something")

        assert any(isinstance(e, TurnInterrupted) for e in events)
        # Partial text should be committed to session as an assistant turn
        assistant_turns = [t for t in session.turns if t.role == "assistant"]
        assert len(assistant_turns) == 1
        assert assistant_turns[0].content[0].text == "Partial "

    def test_interrupt_before_response_removes_user_message(self, session, registry):
        """Interrupt before any response drops the orphan user message."""

        def stream_immediate_interrupt(*args, **kwargs):
            agent._interrupt.set()
            yield LlmTextDelta(text="x")
            yield Done(stop_reason="end_turn")

        llm = MagicMock()
        llm.stream = MagicMock(side_effect=stream_immediate_interrupt)
        events = []
        agent = AgentLoop(llm, session, registry, "system", events.append)

        agent.run_turn("Orphan")

        assert any(isinstance(e, TurnInterrupted) for e in events)
        # The orphan user message should be removed
        user_texts = [
            t
            for t in session.turns
            if t.role == "user"
            and t.content
            and hasattr(t.content[0], "text")
            and t.content[0].text == "Orphan"
        ]
        assert len(user_texts) == 0

    def test_interrupt_between_tools_preserves_completed(self, session, registry):
        """Completed tools stay; pending ones get synthetic results."""

        def first_tool_handler(params):
            # After this tool completes, set interrupt so it fires before tool b starts
            agent._interrupt.set()
            return "First done"

        reg = ToolRegistry()
        reg.register(ToolSpec("t", "test", {"type": "object"}, first_tool_handler))

        llm = _mock_llm(
            [
                ToolUseEvent(tool_use_id="a", name="t", input={}),
                ToolUseEvent(tool_use_id="b", name="t", input={}),
                Usage(input_tokens=10, output_tokens=5),
                Done(stop_reason="tool_use"),
            ]
        )
        events = []
        agent = AgentLoop(llm, session, reg, "system", events.append)

        agent.run_turn("Do two things")

        assert any(isinstance(e, TurnInterrupted) for e in events)
        # Tool "a" should have a real result, tool "b" should have synthetic
        from archie.types import ToolResultBlock

        all_results = []
        for turn in session.turns:
            for block in turn.content:
                if isinstance(block, ToolResultBlock):
                    all_results.append(block)
        result_a = next(r for r in all_results if r.tool_use_id == "a")
        result_b = next(r for r in all_results if r.tool_use_id == "b")
        assert "First done" in result_a.content
        assert "interrupted" in result_b.content


class TestErrorHandling:
    """Error scenarios."""

    def test_llm_exception_emits_turn_error(self, session, registry):
        llm = MagicMock()
        llm.stream = MagicMock(side_effect=RuntimeError("Connection lost"))
        events = []
        agent = AgentLoop(llm, session, registry, "system", events.append)

        agent.run_turn("Crash")

        assert events[-1] == TurnError(message="Connection lost")

    def test_unknown_tool_returns_error_result(self, session, registry):
        llm = _mock_llm(
            [
                ToolUseEvent(tool_use_id="t1", name="nonexistent", input={}),
                Usage(input_tokens=10, output_tokens=5),
                Done(stop_reason="tool_use"),
            ],
            [
                LlmTextDelta(text="OK"),
                Usage(input_tokens=10, output_tokens=2),
                Done(stop_reason="end_turn"),
            ],
        )
        events = []
        agent = AgentLoop(llm, session, registry, "system", events.append)

        agent.run_turn("Call missing")

        finished = [e for e in events if isinstance(e, ToolFinished)]
        assert finished[0].is_error is True
        assert events[-1] == TurnComplete(stop_reason="end_turn")


class TestCacheFieldThreading:
    """Verify cache tokens flow from LLM Usage → session → UsageUpdated event."""

    def test_cache_tokens_in_usage_event(self, session, registry):
        llm = _mock_llm(
            [
                LlmTextDelta(text="Hi"),
                Usage(
                    input_tokens=10,
                    output_tokens=5,
                    cache_read_input_tokens=80,
                    cache_write_input_tokens=20,
                ),
                Done(stop_reason="end_turn"),
            ]
        )
        events = []
        agent = AgentLoop(llm, session, registry, "system", events.append)

        agent.run_turn("Hello")

        usage_events = [e for e in events if isinstance(e, UsageUpdated)]
        assert len(usage_events) == 1
        assert usage_events[0].cache_read_tokens == 80
        assert usage_events[0].cache_write_tokens == 20
        assert session.total_cache_read_tokens == 80
        assert session.total_cache_write_tokens == 20
