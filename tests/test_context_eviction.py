"""Tests for artifact store and context eviction."""

from unittest.mock import MagicMock

import pytest

from archie.artifact_store import ArtifactStore
from archie.engine import Engine
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


def _mock_llm_stream(*call_responses):
    mock = MagicMock()
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
        llm = _mock_llm_stream(
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
        engine = Engine(llm, session, registry, "system")
        list(engine.run("first message"))

        # Build context — nothing should be evicted (only 1 completed turn)
        messages = engine._build_context()
        # Find the tool result message
        tool_result_msg = next(
            m
            for m in messages
            if m["role"] == "user" and any("toolResult" in b for b in m["content"])
        )
        result_block = next(b for b in tool_result_msg["content"] if "toolResult" in b)
        assert "[evicted" not in result_block["toolResult"]["content"][0]["text"]

    def test_eviction_after_three_turns(self, session, registry):
        """Tool results from turn 1 are evicted after 3 completed turns."""
        store = ArtifactStore()
        # Simulate 3 completed user turns with tool use
        for i in range(3):
            llm = _mock_llm_stream(
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
            engine = Engine(llm, session, registry, "system", artifact_store=store)
            engine._completed_turns = i  # Set previous completed count
            list(engine.run(f"message {i}"))

        # After 3 turns, build context — turn 0's tool result should be evicted
        messages = engine._build_context()
        # Find the first tool result (tu_0)
        tool_results = []
        for m in messages:
            if m["role"] == "user":
                for b in m["content"]:
                    if "toolResult" in b:
                        tool_results.append(b)

        # First tool result should be evicted (stub)
        first_result_text = tool_results[0]["toolResult"]["content"][0]["text"]
        assert "[evicted:" in first_result_text
        assert "tu_0" in first_result_text

        # Last tool result should be full
        last_result_text = tool_results[-1]["toolResult"]["content"][0]["text"]
        assert "[evicted" not in last_result_text

    def test_eviction_stub_contains_tool_name_and_summary(self, session, registry):
        """Eviction stubs include tool name, summary, and tool_use_id."""
        store = ArtifactStore()
        store.put("tu_old", "full content", "5 lines")

        # Manually build session state to test _build_context directly
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
        # Add 2 more user text turns to push the first one past eviction
        session.add_turn("user", "second message")
        session.add_turn("assistant", [TextBlock(text="Ok")])
        session.add_turn("user", "third message")
        session.add_turn("assistant", [TextBlock(text="Sure")])

        engine = Engine(MagicMock(), session, registry, "system", artifact_store=store)
        engine._completed_turns = 3

        messages = engine._build_context()
        # Find the evicted tool result
        for m in messages:
            for b in m.get("content", []):
                if "toolResult" in b:
                    text = b["toolResult"]["content"][0]["text"]
                    if "[evicted:" in text:
                        assert "echo" in text
                        assert "5 lines" in text
                        assert "tu_old" in text
                        return
        pytest.fail("No evicted stub found in context")

    def test_recent_results_kept_full(self, session, registry):
        """Tool results from last 2 user turns are kept in full."""
        store = ArtifactStore()

        # Build a session with 3 user text turns + tool use
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

        engine = Engine(MagicMock(), session, registry, "system", artifact_store=store)
        engine._completed_turns = 3

        messages = engine._build_context()
        tool_results = []
        for m in messages:
            for b in m.get("content", []):
                if "toolResult" in b:
                    tool_results.append(b["toolResult"]["content"][0]["text"])

        # tu_1 (recent) and tu_2 (current) should be full
        assert tool_results[1] == "Echo: recent"
        assert tool_results[2] == "Echo: current"


class TestArtifactStoreIntegration:
    """Tests that the engine stores artifacts during tool execution."""

    def test_stores_artifact_on_tool_execution(self, session, registry):
        """Engine stores full tool result in artifact store after execution."""
        store = ArtifactStore()
        llm = _mock_llm_stream(
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
        engine = Engine(llm, session, registry, "system", artifact_store=store)
        list(engine.run("test"))

        artifact = store.get("tu_1")
        assert artifact is not None
        assert artifact["content"] == "Echo: test"
        assert artifact["summary"]  # Should have a summary

    def test_completed_turns_incremented(self, session, registry):
        """_completed_turns is incremented after each successful run."""
        llm = _mock_llm_stream(
            [
                LlmTextDelta(text="Hi"),
                Usage(input_tokens=10, output_tokens=5),
                Done(stop_reason="end_turn"),
            ]
        )
        engine = Engine(llm, session, registry, "system")
        assert engine._completed_turns == 0
        list(engine.run("hello"))
        assert engine._completed_turns == 1
