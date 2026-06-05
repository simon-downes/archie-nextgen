"""Tests for session persistence."""

import json

import pytest

from archie.models import get_model_info
from archie.session import Session


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
    """Content > 500 chars gets a raw file."""
    big_content = "x" * 600
    session.add_turn("assistant", big_content, output_tokens=100)

    raw_file = session.dir / "raw" / "t0001.json"
    assert raw_file.exists()
    raw = json.loads(raw_file.read_text())
    assert raw["content"] == big_content


def test_add_turn_no_raw_for_small_content(session):
    """Content <= 500 chars does not get a raw file."""
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
    # Sonnet: $3/M input
    assert abs(session.total_cost - 3.0) < 0.001


def test_context_pct(session):
    """Context percentage estimates next request size."""
    # Assistant turn with input_tokens = what Bedrock reported for that request
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


def test_messages_format(session):
    """Messages returns Bedrock-compatible format."""
    session.add_turn("user", "hello")
    session.add_turn("assistant", "hi there")

    msgs = session.messages
    assert msgs[0] == {"role": "user", "content": [{"text": "hello"}]}
    assert msgs[1] == {"role": "assistant", "content": [{"text": "hi there"}]}


def test_summary_truncation(session):
    """JSONL summary truncates long content."""
    long_content = "a" * 300
    session.add_turn("user", long_content)

    jsonl = session.dir / "turns.jsonl"
    entry = json.loads(jsonl.read_text().strip())
    assert len(entry["summary"]) == 200
    assert entry["summary"].endswith("...")
