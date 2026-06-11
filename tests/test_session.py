"""Tests for session state and persistence."""

import json
from unittest.mock import patch

import pytest

from archie.models import ModelInfo
from archie.session import Session, TurnLog, summarise_tool_output


@pytest.fixture
def model_info():
    return ModelInfo(
        name="Test Model",
        max_context_tokens=200_000,
        input_price_per_m=3.0,
        output_price_per_m=15.0,
        context_warning_threshold=0.8,
    )


@pytest.fixture
def session(tmp_path, model_info):
    """Create a session that writes to tmp_path."""
    with patch("archie.session.SESSIONS_DIR", tmp_path):
        return Session(model_id="test-model", model_info=model_info, project_name="myproject")


class TestSessionId:
    def test_format(self, session):
        """Session ID format: YYYY-MM-DD-{project}-{hash}."""
        parts = session.session_id.split("-")
        # YYYY-MM-DD-project-hash
        assert len(parts) >= 5
        assert parts[0].isdigit() and len(parts[0]) == 4  # year
        assert parts[1].isdigit() and len(parts[1]) == 2  # month
        assert parts[2].isdigit() and len(parts[2]) == 2  # day
        assert "myproject" in session.session_id

    def test_unique(self, tmp_path, model_info):
        """Two sessions get different IDs."""
        with patch("archie.session.SESSIONS_DIR", tmp_path):
            s1 = Session(model_id="m", model_info=model_info, project_name="p")
            s2 = Session(model_id="m", model_info=model_info, project_name="p")
        assert s1.session_id != s2.session_id


class TestAddTurn:
    def test_adds_to_memory(self, session):
        """add_turn appends to turns list (in-memory)."""
        session.add_turn("user", "hello")
        assert len(session.turns) == 1
        assert session.turns[0].role == "user"
        assert session.turns[0].text == "hello"

    def test_does_not_write_to_disk(self, session):
        """add_turn is memory-only — no file created."""
        session.add_turn("user", "hello")
        assert not session.log_path.exists()

    def test_tracks_tokens(self, session):
        """add_turn accumulates token counts."""
        session.add_turn("assistant", "hi", input_tokens=100, output_tokens=50)
        assert session.total_input_tokens == 100
        assert session.total_output_tokens == 50


class TestFlushTurn:
    def test_creates_file_on_first_flush(self, tmp_path, session):
        """flush_turn creates the JSONL file."""
        with patch("archie.session.SESSIONS_DIR", tmp_path):
            turn_log = TurnLog(when="2026-06-08 12:00:00", user="hello", model="test-model")
            session.flush_turn(turn_log)
        assert session.log_path.exists()

    def test_writes_valid_jsonl(self, tmp_path, session):
        """Each flush appends one valid JSON line."""
        with patch("archie.session.SESSIONS_DIR", tmp_path):
            turn_log = TurnLog(
                when="2026-06-08 12:00:00",
                user="hello",
                assistant_text="hi there",
                model="test-model",
                input_tokens=100,
                output_tokens=50,
            )
            session.flush_turn(turn_log)

        lines = session.log_path.read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["user"] == "hello"
        assert entry["assistant"] == "hi there"
        assert entry["metadata"]["model"] == "test-model"
        assert entry["metadata"]["input_tokens"] == 100
        assert entry["metadata"]["cost"] > 0

    def test_includes_ulid(self, tmp_path, session):
        """Each turn gets a ULID as its id."""
        with patch("archie.session.SESSIONS_DIR", tmp_path):
            session.flush_turn(TurnLog(when="2026-06-08 12:00:00", user="x", model="m"))

        entry = json.loads(session.log_path.read_text().strip())
        assert len(entry["id"]) == 26  # ULID length

    def test_tools_included_when_present(self, tmp_path, session):
        """Tools list is included when tools were used."""
        with patch("archie.session.SESSIONS_DIR", tmp_path):
            turn_log = TurnLog(when="2026-06-08 12:00:00", user="x", model="m")
            turn_log.tools = [
                {
                    "id": "t1",
                    "name": "shell",
                    "input": {"command": "ls"},
                    "success": True,
                    "summary": "exit 0, 2 lines",
                }
            ]
            session.flush_turn(turn_log)

        entry = json.loads(session.log_path.read_text().strip())
        assert "tools" in entry
        assert entry["tools"][0]["name"] == "shell"

    def test_tools_omitted_when_empty(self, tmp_path, session):
        """Tools key is absent when no tools were used."""
        with patch("archie.session.SESSIONS_DIR", tmp_path):
            session.flush_turn(TurnLog(when="2026-06-08 12:00:00", user="x", model="m"))

        entry = json.loads(session.log_path.read_text().strip())
        assert "tools" not in entry

    def test_assistant_omitted_when_empty(self, tmp_path, session):
        """Assistant key absent when no response generated."""
        with patch("archie.session.SESSIONS_DIR", tmp_path):
            session.flush_turn(TurnLog(when="2026-06-08 12:00:00", user="x", model="m"))

        entry = json.loads(session.log_path.read_text().strip())
        assert "assistant" not in entry

    def test_interrupted_flag(self, tmp_path, session):
        """Interrupted turn is recorded with the flag."""
        with patch("archie.session.SESSIONS_DIR", tmp_path):
            turn_log = TurnLog(
                when="2026-06-08 12:00:00",
                user="x",
                model="m",
                interrupted=True,
                assistant_text="Response was interrupted by the user",
            )
            session.flush_turn(turn_log)

        entry = json.loads(session.log_path.read_text().strip())
        assert entry["metadata"]["interrupted"] is True

    def test_multiple_turns_append(self, tmp_path, session):
        """Multiple flushes append to the same file."""
        with patch("archie.session.SESSIONS_DIR", tmp_path):
            session.flush_turn(TurnLog(when="2026-06-08 12:00:00", user="first", model="m"))
            session.flush_turn(TurnLog(when="2026-06-08 12:01:00", user="second", model="m"))

        lines = session.log_path.read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["user"] == "first"
        assert json.loads(lines[1])["user"] == "second"


class TestSummariseToolOutput:
    def test_read_file(self):
        output = "File: test.py (42 lines)\n\n    1|line1\n    2|line2\n    3|line3"
        assert summarise_tool_output("read_file", {"path": "test.py"}, output, False) == "4 lines"

    def test_shell_success(self):
        output = "$ ls\n[exit: 0]\nfile1\nfile2"
        result = summarise_tool_output("shell", {"command": "ls"}, output, False)
        assert "exit: 0" in result
        assert "2 lines" in result

    def test_list_files(self):
        output = "src/a.py\nsrc/b.py\nsrc/c.py"
        assert summarise_tool_output("list_files", {"glob": "*.py"}, output, False) == "3 files"

    def test_search_files(self):
        output = "src/a.py:1:match1\nsrc/b.py:2:match2\n"
        assert summarise_tool_output("search_files", {"pattern": "x"}, output, False) == "2 matches"

    def test_error(self):
        output = "Error: file not found\nsome detail"
        result = summarise_tool_output("read_file", {"path": "x"}, output, True)
        assert result == "Error: file not found"

    def test_write_file(self):
        output = "Written: src/app.py (42 lines)"
        assert summarise_tool_output("write_file", {"path": "x"}, output, False) == output

    def test_unknown_tool(self):
        output = "x" * 500
        assert summarise_tool_output("unknown_tool", {}, output, False) == "500 chars"


class TestContextTracking:
    def test_context_pct(self, session):
        """Context percentage based on last input tokens."""
        session.add_turn("user", "hello")
        session.add_turn("assistant", "hi", input_tokens=20_000, output_tokens=500)
        # context_pct = (last_input + last_output) / max * 100
        # = (20000 + 500) / 200000 * 100 = 10.25%
        assert abs(session.context_pct - 10.25) < 0.01

    def test_cost(self, session):
        """Cost calculation uses model pricing."""
        session.add_turn("assistant", "hi", input_tokens=1_000_000, output_tokens=1_000_000)
        # 1M input @ $3/M + 1M output @ $15/M = $18
        assert session.total_cost == 18.0


class TestCalculateCost:
    """Tests for four-rate cost calculation."""

    def test_cache_read_uses_cheaper_rate(self):
        from archie.models import ModelInfo, calculate_cost

        model = ModelInfo(
            name="Test",
            max_context_tokens=100_000,
            input_price_per_m=3.0,
            output_price_per_m=15.0,
            cache_read_price_per_m=0.30,
            cache_write_price_per_m=3.75,
        )
        # 1000 cache_read tokens should cost 10x less than 1000 fresh input tokens
        fresh_cost = calculate_cost(model, input_tokens=1000, output_tokens=0)
        cache_cost = calculate_cost(model, input_tokens=0, output_tokens=0, cache_read_tokens=1000)
        assert cache_cost == pytest.approx(fresh_cost / 10)

    def test_all_four_categories_sum(self):
        from archie.models import ModelInfo, calculate_cost

        model = ModelInfo(
            name="Test",
            max_context_tokens=100_000,
            input_price_per_m=1.0,
            output_price_per_m=2.0,
            cache_read_price_per_m=0.1,
            cache_write_price_per_m=1.5,
        )
        cost = calculate_cost(
            model,
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=1_000_000,
            cache_write_tokens=1_000_000,
        )
        assert cost == 1.0 + 2.0 + 0.1 + 1.5


class TestSessionCacheAccumulation:
    """Tests that session tracks cache tokens correctly."""

    def test_add_turn_accumulates_cache_tokens(self, tmp_path):
        from archie.models import ModelInfo

        model = ModelInfo(
            name="Test",
            max_context_tokens=100_000,
            input_price_per_m=1.0,
            output_price_per_m=1.0,
            cache_read_price_per_m=0.1,
            cache_write_price_per_m=1.5,
        )
        s = Session(model_id="test", model_info=model)
        s._log_path = tmp_path / "test.jsonl"

        s.add_turn(
            "assistant",
            "hi",
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=200,
            cache_write_tokens=30,
        )
        s.add_turn(
            "assistant",
            "bye",
            input_tokens=80,
            output_tokens=40,
            cache_read_tokens=150,
            cache_write_tokens=20,
        )

        assert s.total_cache_read_tokens == 350
        assert s.total_cache_write_tokens == 50


class TestDetectGitBranch:
    """Tests for .git/HEAD branch reading."""

    def test_reads_branch_from_head(self, tmp_path):
        from archie.ui.status import _detect_git_branch

        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/feat/my-branch\n")
        assert _detect_git_branch(tmp_path) == "feat/my-branch"

    def test_detached_head_returns_short_hash(self, tmp_path):
        from archie.ui.status import _detect_git_branch

        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("abc123def456\n")
        assert _detect_git_branch(tmp_path) == "abc123de"

    def test_no_git_dir_returns_dash(self, tmp_path):
        from archie.ui.status import _detect_git_branch

        assert _detect_git_branch(tmp_path) == "—"
