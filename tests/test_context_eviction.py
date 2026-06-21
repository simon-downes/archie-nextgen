"""Tests for artifact store and context eviction."""

from unittest.mock import MagicMock

import pytest

from archie.agent import AgentLoop
from archie.artifact_store import ArtifactStore
from archie.llm.bedrock import Done, ToolUseEvent, Usage
from archie.llm.bedrock import TextDelta as LlmTextDelta
from archie.models import ModelInfo
from archie.session import Session
from archie.tools import ToolRegistry, ToolSpec
from archie.tools.retrieve_artifact import make_retrieve_artifact_spec
from archie.types import TextBlock, ToolResultBlock, ToolUseBlock


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
    from unittest.mock import patch

    with patch("archie.session.SESSIONS_DIR", tmp_path):
        s = Session(model_id="test-model", model_info=model_info, project_name="test")
    s._log_path = tmp_path / f"{s.session_id}.jsonl"
    return s


@pytest.fixture
def registry():
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


def _mock_llm(*call_responses):
    mock = MagicMock()
    mock.model_id = "test-model"
    mock.stream = MagicMock(side_effect=[iter(r) for r in call_responses])
    return mock


class TestArtifactStore:
    def test_put_and_get(self):
        store = ArtifactStore()
        store.put("id1", "full content", "summary text")
        result = store.get("id1")
        assert result == {"content": "full content", "summary": "summary text"}

    def test_get_missing(self):
        store = ArtifactStore()
        assert store.get("nonexistent") is None


class TestRetrieveArtifactTool:
    def setup_method(self):
        self.store = ArtifactStore()
        self.spec = make_retrieve_artifact_spec(self.store)

    def test_retrieves_stored_artifact(self):
        self.store.put("tu_123", "full file content here", "42 lines")
        result = self.spec.handler({"tool_use_id": "tu_123"})
        assert result == "full file content here"

    def test_missing_artifact_returns_error(self):
        result = self.spec.handler({"tool_use_id": "tu_missing"})
        assert "Error" in result
        assert "tu_missing" in result

    def test_empty_id_returns_error(self):
        result = self.spec.handler({"tool_use_id": ""})
        assert "Error" in result


class TestEvictionLogic:
    """Tests for _build_context eviction of old tool results."""

    def test_no_eviction_on_first_turn(self, session, registry):
        """First turn's tool results are never evicted."""
        llm = _mock_llm(
            [
                ToolUseEvent(tool_use_id="tu_1", name="echo", input={"text": "hello"}),
                Usage(input_tokens=10, output_tokens=5),
                Done(stop_reason="tool_use"),
            ],
            [
                LlmTextDelta(text="Done"),
                Usage(input_tokens=20, output_tokens=5),
                Done(stop_reason="end_turn"),
            ],
        )
        agent = AgentLoop(llm, session, registry, "system", lambda _: None)
        agent.run_turn("first message")

        turns = agent._build_context()
        results = [b for t in turns for b in t.content if isinstance(b, ToolResultBlock)]
        assert results
        assert "[evicted" not in results[0].content

    def test_eviction_after_three_turns(self, session, registry):
        """Tool results from turn 1 are evicted after 3 completed turns."""
        store = ArtifactStore()
        for i in range(3):
            llm = _mock_llm(
                [
                    ToolUseEvent(tool_use_id=f"tu_{i}", name="echo", input={"text": f"msg{i}"}),
                    Usage(input_tokens=10, output_tokens=5),
                    Done(stop_reason="tool_use"),
                ],
                [
                    LlmTextDelta(text=f"Response {i}"),
                    Usage(input_tokens=20, output_tokens=5),
                    Done(stop_reason="end_turn"),
                ],
            )
            agent = AgentLoop(
                llm, session, registry, "system", lambda _: None, artifact_store=store
            )
            agent._completed_turns = i
            agent.run_turn(f"message {i}")

        messages = agent._build_context()
        tool_results = []
        for m in messages:
            if m.role == "user":
                for b in m.content:
                    if isinstance(b, ToolResultBlock):
                        tool_results.append(b)

        first_result_text = tool_results[0].content
        assert "[evicted:" in first_result_text
        assert "tu_0" in first_result_text

        last_result_text = tool_results[-1].content
        assert "[evicted" not in last_result_text

    def test_eviction_stub_contains_tool_name_and_summary(self, session, registry):
        """Eviction stubs include tool name, summary, and tool_use_id."""
        store = ArtifactStore()
        store.put("tu_old", "full content", "5 lines")

        session.add_turn("user", "first message")
        session.add_turn(
            "assistant",
            [ToolUseBlock(tool_use_id="tu_old", name="echo", input={"text": "hi"})],
        )
        session.add_turn(
            "user",
            [ToolResultBlock(tool_use_id="tu_old", content="Echo: hi", is_error=False)],
        )
        session.add_turn("assistant", [TextBlock(text="Got it")])
        session.add_turn("user", "second message")
        session.add_turn("assistant", [TextBlock(text="Ok")])
        session.add_turn("user", "third message")
        session.add_turn("assistant", [TextBlock(text="Sure")])

        agent = AgentLoop(
            MagicMock(model_id="test-model"),
            session,
            registry,
            "system",
            lambda _: None,
            artifact_store=store,
        )
        agent._completed_turns = 3

        messages = agent._build_context()
        for m in messages:
            for b in m.content:
                if isinstance(b, ToolResultBlock) and "[evicted:" in b.content:
                    assert "echo" in b.content
                    assert "5 lines" in b.content
                    assert "tu_old" in b.content
                    return
        pytest.fail("No evicted stub found in context")

    def test_recent_results_kept_full(self, session, registry):
        """Tool results from last 2 user turns are kept in full."""
        store = ArtifactStore()

        session.add_turn("user", "old message")
        session.add_turn(
            "assistant",
            [ToolUseBlock(tool_use_id="tu_0", name="echo", input={"text": "old"})],
        )
        session.add_turn(
            "user",
            [ToolResultBlock(tool_use_id="tu_0", content="Echo: old", is_error=False)],
        )
        session.add_turn("assistant", [TextBlock(text="Old response")])

        session.add_turn("user", "recent message")
        session.add_turn(
            "assistant",
            [ToolUseBlock(tool_use_id="tu_1", name="echo", input={"text": "recent"})],
        )
        session.add_turn(
            "user",
            [ToolResultBlock(tool_use_id="tu_1", content="Echo: recent", is_error=False)],
        )
        session.add_turn("assistant", [TextBlock(text="Recent response")])

        session.add_turn("user", "current message")
        session.add_turn(
            "assistant",
            [ToolUseBlock(tool_use_id="tu_2", name="echo", input={"text": "current"})],
        )
        session.add_turn(
            "user",
            [ToolResultBlock(tool_use_id="tu_2", content="Echo: current", is_error=False)],
        )

        agent = AgentLoop(
            MagicMock(model_id="test-model"),
            session,
            registry,
            "system",
            lambda _: None,
            artifact_store=store,
        )
        agent._completed_turns = 3

        messages = agent._build_context()
        tool_results = []
        for m in messages:
            for b in m.content:
                if isinstance(b, ToolResultBlock):
                    tool_results.append(b.content)

        assert tool_results[1] == "Echo: recent"
        assert tool_results[2] == "Echo: current"


class TestArtifactStoreIntegration:
    """Tests that the agent stores artifacts during tool execution."""

    def test_stores_artifact_on_tool_execution(self, session, registry):
        store = ArtifactStore()
        llm = _mock_llm(
            [
                ToolUseEvent(tool_use_id="tu_1", name="echo", input={"text": "test"}),
                Usage(input_tokens=10, output_tokens=5),
                Done(stop_reason="tool_use"),
            ],
            [
                LlmTextDelta(text="Done"),
                Usage(input_tokens=20, output_tokens=5),
                Done(stop_reason="end_turn"),
            ],
        )
        agent = AgentLoop(llm, session, registry, "system", lambda _: None, artifact_store=store)
        agent.run_turn("test")

        artifact = store.get("tu_1")
        assert artifact is not None
        assert artifact["content"] == "Echo: test"
        assert artifact["summary"]

    def test_completed_turns_incremented(self, session, registry):
        llm = _mock_llm(
            [
                LlmTextDelta(text="Hi"),
                Usage(input_tokens=10, output_tokens=5),
                Done(stop_reason="end_turn"),
            ]
        )
        agent = AgentLoop(llm, session, registry, "system", lambda _: None)
        assert agent._completed_turns == 0
        agent.run_turn("hello")
        assert agent._completed_turns == 1


def _tool_iteration_events(i):
    """One LLM response that calls echo once."""
    return [
        ToolUseEvent(tool_use_id=f"tu_{i}", name="echo", input={"text": f"msg{i}"}),
        Usage(input_tokens=10, output_tokens=5),
        Done(stop_reason="tool_use"),
    ]


class TestWithinTurnEviction:
    """Long single turns evict old tool results mid-turn."""

    def test_long_turn_evicts_old_results(self, session, registry):
        from archie.agent import WITHIN_TURN_EVICT_THRESHOLD, WITHIN_TURN_KEEP

        n_iterations = WITHIN_TURN_EVICT_THRESHOLD + 5
        responses = [_tool_iteration_events(i) for i in range(n_iterations)]
        responses.append(
            [
                LlmTextDelta(text="Done"),
                Usage(input_tokens=20, output_tokens=5),
                Done(stop_reason="end_turn"),
            ]
        )
        store = ArtifactStore()
        llm = _mock_llm(*responses)
        agent = AgentLoop(llm, session, registry, "system", lambda _: None, artifact_store=store)
        agent.run_turn("long task")

        context = agent._build_context()
        evicted = []
        full = []
        for t in context:
            for b in t.content:
                if isinstance(b, ToolResultBlock):
                    (evicted if "[evicted:" in b.content else full).append(b)

        assert evicted, "expected some mid-turn evictions"
        assert len(full) >= WITHIN_TURN_KEEP
        # Most recent results are never evicted
        assert f"tu_{n_iterations - 1}" in [b.tool_use_id for b in full]
        # Evicted stubs point at retrieve_artifact
        assert "retrieve_artifact" in evicted[0].content

    def test_short_turn_evicts_nothing(self, session, registry):
        responses = [_tool_iteration_events(i) for i in range(3)]
        responses.append(
            [
                LlmTextDelta(text="Done"),
                Usage(input_tokens=20, output_tokens=5),
                Done(stop_reason="end_turn"),
            ]
        )
        llm = _mock_llm(*responses)
        agent = AgentLoop(llm, session, registry, "system", lambda _: None)
        agent.run_turn("short task")

        context = agent._build_context()
        for t in context:
            for b in t.content:
                if isinstance(b, ToolResultBlock):
                    assert "[evicted:" not in b.content


class TestResentPromptDedup:
    """Re-sending the same prompt after an interrupt drops the superseded turn."""

    def test_resent_prompt_after_interrupt_is_deduped(self, session, registry):
        # Simulate an interrupted turn already in history
        session.add_turn("user", "do the thing")
        session.add_turn("assistant", [TextBlock(text="partial resp")])

        llm = _mock_llm(
            [
                LlmTextDelta(text="Full response"),
                Usage(input_tokens=10, output_tokens=5),
                Done(stop_reason="end_turn"),
            ]
        )
        agent = AgentLoop(llm, session, registry, "system", lambda _: None)
        agent._last_turn_interrupted = True

        agent.run_turn("do the thing")

        prompts = [
            t
            for t in session.turns
            if t.role == "user"
            and t.content
            and isinstance(t.content[0], TextBlock)
            and t.content[0].text == "do the thing"
        ]
        assert len(prompts) == 1
        # The partial response was dropped too
        assert all(
            b.text != "partial resp"
            for t in session.turns
            for b in t.content
            if isinstance(b, TextBlock)
        )

    def test_different_prompt_not_deduped(self, session, registry):
        session.add_turn("user", "do the thing")
        session.add_turn("assistant", [TextBlock(text="partial resp")])

        llm = _mock_llm(
            [
                LlmTextDelta(text="ok"),
                Usage(input_tokens=10, output_tokens=5),
                Done(stop_reason="end_turn"),
            ]
        )
        agent = AgentLoop(llm, session, registry, "system", lambda _: None)
        agent._last_turn_interrupted = True

        agent.run_turn("do something else")

        texts = [
            t.content[0].text
            for t in session.turns
            if t.role == "user" and t.content and isinstance(t.content[0], TextBlock)
        ]
        assert "do the thing" in texts
        assert "do something else" in texts

    def test_no_dedup_when_previous_turn_completed(self, session, registry):
        session.add_turn("user", "do the thing")
        session.add_turn("assistant", [TextBlock(text="done it")])

        llm = _mock_llm(
            [
                LlmTextDelta(text="again"),
                Usage(input_tokens=10, output_tokens=5),
                Done(stop_reason="end_turn"),
            ]
        )
        agent = AgentLoop(llm, session, registry, "system", lambda _: None)
        agent._last_turn_interrupted = False  # previous turn completed normally

        agent.run_turn("do the thing")

        prompts = [
            t
            for t in session.turns
            if t.role == "user"
            and t.content
            and isinstance(t.content[0], TextBlock)
            and t.content[0].text == "do the thing"
        ]
        assert len(prompts) == 2


class TestEvictionInvalidatesMtimeCache:
    """Evicting a read_file result must invalidate its mtime cache entry."""

    def test_evicted_read_invalidates_cache_entry(self, session, registry):
        store = ArtifactStore()
        cache = {("/some/file.py", 0, 0): (123.0, "tu_old")}

        session.add_turn("user", "first message")
        session.add_turn(
            "assistant",
            [ToolUseBlock(tool_use_id="tu_old", name="echo", input={"text": "hi"})],
        )
        session.add_turn(
            "user",
            [ToolResultBlock(tool_use_id="tu_old", content="Echo: hi", is_error=False)],
        )
        session.add_turn("assistant", [TextBlock(text="Got it")])
        session.add_turn("user", "second message")
        session.add_turn("assistant", [TextBlock(text="Ok")])
        session.add_turn("user", "third message")
        session.add_turn("assistant", [TextBlock(text="Sure")])

        agent = AgentLoop(
            MagicMock(model_id="test-model"),
            session,
            registry,
            "system",
            lambda _: None,
            artifact_store=store,
            mtime_cache=cache,
        )
        agent._completed_turns = 3

        agent._build_context()
        assert cache == {}
