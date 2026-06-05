"""Tests for session persistence."""

import json

import pytest

from archie.models import get_model_info
from archie.session import Session, _serialize_blocks
from archie.types import TextBlock, ToolResultBlock, ToolUseBlock


@pytest.fixture
def session(tmp_path, monkeypatch):
    """Create a session with tmp storage."""
    monkeypatch.setattr("archie.session.SESSIONS_DIR", tmp_path)
    model_id = "eu.anthropic.claude-sonnet-4-6"
    return Session(model_id=model_id, model_info=get_model_info(model_id))


def test_session_id_format(session):
    """Session ID is YYYYMMDD-HHMM-xxxx."""
    parts = session.session_id.split("-")
    assert len(parts) == 3
    assert len(parts[0]) == 8  # YYYYMMDD
    assert len(parts[1]) == 4  # HHMM
    assert len(parts[2]) == 4  # hex


def test_add_turn_with_string(session):
    """Adding a turn with a plain string wraps it in TextBlock."""
    turn = session.add_turn("user", "hello")
    assert len(turn.content) == 1
    assert isinstance(turn.content[0], TextBlock)
    assert turn.content[0].text == "hello"


def test_add_turn_with_content_blocks(session):
    """Adding a turn with explicit content blocks preserves them."""
    blocks = [
        TextBlock(text="Let me read that file."),
        ToolUseBlock(tool_use_id="tu_123", name="read_file", input={"path": "foo.py"}),
    ]
    turn = session.add_turn("assistant", blocks)
    assert len(turn.content) == 2
    assert isinstance(turn.content[0], TextBlock)
    assert isinstance(turn.content[1], ToolUseBlock)
    assert turn.content[1].name == "read_file"


def test_add_turn_persists_jsonl(session):
    """Adding a turn writes to turns.jsonl."""
    session.add_turn("user", "hello", input_tokens=10)

    jsonl = session.dir / "turns.jsonl"
    assert jsonl.exists()
    entry = json.loads(jsonl.read_text().strip())
    assert entry["role"] == "user"
    assert entry["summary"] == "hello"
    assert entry["input_tokens"] == 10


def test_add_turn_writes_raw_for_large_content(session):
    """Content > 500 chars serialized gets a raw file."""
    big_content = "x" * 600
    session.add_turn("assistant", big_content, output_tokens=100)

    raw_file = session.dir / "raw" / "t0001.json"
    assert raw_file.exists()
    raw = json.loads(raw_file.read_text())
    assert raw["content"][0]["type"] == "text"
    assert raw["content"][0]["text"] == big_content


def test_add_turn_no_raw_for_small_content(session):
    """Small content does not get a raw file."""
    session.add_turn("user", "short message", input_tokens=5)

    raw_file = session.dir / "raw" / "t0001.json"
    assert not raw_file.exists()


def test_meta_json_updated(session):
    """meta.json reflects cumulative stats."""
    session.add_turn("user", "hi", input_tokens=100)
    session.add_turn("assistant", "hello", output_tokens=50)

    meta = json.loads((session.dir / "meta.json").read_text())
    assert meta["total_turns"] == 2
    assert meta["total_input_tokens"] == 100
    assert meta["total_output_tokens"] == 50
    assert meta["total_cost"] > 0


def test_token_tracking(session):
    """Cumulative tokens are tracked correctly."""
    session.add_turn("user", "one", input_tokens=100)
    session.add_turn("assistant", "two", output_tokens=200)
    session.add_turn("user", "three", input_tokens=150)

    assert session.total_input_tokens == 250
    assert session.total_output_tokens == 200


def test_cost_calculation(session):
    """Cost uses model pricing."""
    session.add_turn("user", "test", input_tokens=1_000_000)
    # Sonnet 4.6: $3/M input
    assert abs(session.total_cost - 3.0) < 0.001


def test_context_pct(session):
    """Context percentage estimates next request size."""
    session.add_turn("user", "test")
    session.add_turn("assistant", "reply", input_tokens=500_000, output_tokens=500_000)
    # Next request ≈ last input (500K) + last output (500K) = 1M = 100%
    assert abs(session.context_pct - 100.0) < 0.1


def test_context_warning(session):
    """Warning triggers at threshold."""
    # Below threshold
    session.add_turn("user", "test")
    session.add_turn("assistant", "short", input_tokens=100_000, output_tokens=50_000)
    assert not session.context_warning

    # Above threshold: last input 700K + last output 200K = 900K > 80% of 1M
    session.add_turn("user", "more")
    session.add_turn("assistant", "big reply", input_tokens=700_000, output_tokens=200_000)
    assert session.context_warning


def test_summary_truncation(session):
    """JSONL summary truncates long content."""
    long_content = "a" * 300
    session.add_turn("user", long_content)

    jsonl = session.dir / "turns.jsonl"
    entry = json.loads(jsonl.read_text().strip())
    assert len(entry["summary"]) == 200
    assert entry["summary"].endswith("...")


def test_summary_tool_use(session):
    """JSONL summary for tool_use turns shows tool name."""
    blocks = [ToolUseBlock(tool_use_id="tu_1", name="read_file", input={"path": "x.py"})]
    session.add_turn("assistant", blocks)

    jsonl = session.dir / "turns.jsonl"
    entry = json.loads(jsonl.read_text().strip())
    assert "read_file" in entry["summary"]


def test_serialize_blocks():
    """Content blocks serialize with type discriminators."""
    blocks = [
        TextBlock(text="hello"),
        ToolUseBlock(tool_use_id="tu_1", name="read_file", input={"path": "x.py"}),
        ToolResultBlock(tool_use_id="tu_1", content="content", is_error=False),
    ]
    serialized = _serialize_blocks(blocks)

    assert serialized[0] == {"type": "text", "text": "hello"}
    assert serialized[1] == {
        "type": "tool_use",
        "id": "tu_1",
        "name": "read_file",
        "input": {"path": "x.py"},
    }
    assert serialized[2] == {
        "type": "tool_result",
        "id": "tu_1",
        "content": "content",
        "is_error": False,
    }


def test_turn_text_property(session):
    """Turn.text extracts first TextBlock content."""
    session.add_turn("user", "hello world")
    assert session.turns[0].text == "hello world"

    # Turn with no text blocks
    blocks = [ToolResultBlock(tool_use_id="tu_1", content="result", is_error=False)]
    session.add_turn("user", blocks)
    assert session.turns[1].text == ""
