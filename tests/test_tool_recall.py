"""Tests for the recall tool — memory fragment search."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from archie.tools.recall import make_recall_spec


class TestRecallTool:
    """Tests for recall tool search, filtering, and scoring."""

    def _make_brain(self, tmp_path: Path) -> Path:
        """Create a brain directory with _memory/ and brain.db."""
        brain_dir = tmp_path / "brain"
        brain_dir.mkdir()
        memory_dir = brain_dir / "_memory"
        memory_dir.mkdir()
        # Create brain.db with refs table
        db = sqlite3.connect(brain_dir / "brain.db")
        db.execute("CREATE TABLE IF NOT EXISTS refs (path TEXT NOT NULL, ts INTEGER NOT NULL)")
        db.commit()
        db.close()
        return brain_dir

    def _write_fragments(self, brain_dir: Path, filename: str, fragments: list[dict]) -> None:
        """Write fragments to a memory JSONL file."""
        path = brain_dir / "_memory" / filename
        with path.open("w") as f:
            for frag in fragments:
                f.write(json.dumps(frag) + "\n")

    def test_basic_query_matches_topic(self, tmp_path):
        """Query terms matching topic score highest (+3)."""
        brain_dir = self._make_brain(tmp_path)
        self._write_fragments(
            brain_dir,
            "2026-06-09-proj.jsonl",
            [
                {
                    "id": "01ABC",
                    "type": "decision",
                    "topic": "logging format",
                    "content": "Use JSONL for session logs.",
                    "tags": ["logging"],
                },
                {
                    "id": "01DEF",
                    "type": "learning",
                    "topic": "unrelated",
                    "content": "Something else entirely.",
                    "tags": ["other"],
                },
            ],
        )

        spec = make_recall_spec(brain_dir)
        result = spec.handler({"query": "logging"})

        assert "logging format" in result
        assert "Use JSONL" in result

    def test_query_matches_tags(self, tmp_path):
        """Query terms matching tags contribute to score (+2)."""
        brain_dir = self._make_brain(tmp_path)
        self._write_fragments(
            brain_dir,
            "2026-06-09-proj.jsonl",
            [
                {
                    "id": "01ABC",
                    "type": "preference",
                    "topic": "style",
                    "content": "Prefers concise responses.",
                    "tags": ["writing", "style"],
                },
            ],
        )

        spec = make_recall_spec(brain_dir)
        result = spec.handler({"query": "writing"})

        assert "style" in result
        assert "Prefers concise" in result

    def test_query_matches_content(self, tmp_path):
        """Query terms matching content score +1."""
        brain_dir = self._make_brain(tmp_path)
        self._write_fragments(
            brain_dir,
            "2026-06-09-proj.jsonl",
            [
                {
                    "id": "01ABC",
                    "type": "context",
                    "topic": "tooling",
                    "content": "The project uses ruff for linting.",
                    "tags": ["python"],
                },
            ],
        )

        spec = make_recall_spec(brain_dir)
        result = spec.handler({"query": "ruff"})

        assert "ruff" in result

    def test_type_filter(self, tmp_path):
        """Type filter restricts results to matching fragment type."""
        brain_dir = self._make_brain(tmp_path)
        self._write_fragments(
            brain_dir,
            "2026-06-09-proj.jsonl",
            [
                {
                    "id": "01A",
                    "type": "decision",
                    "topic": "python testing",
                    "content": "Use pytest.",
                    "tags": ["testing"],
                },
                {
                    "id": "01B",
                    "type": "learning",
                    "topic": "python patterns",
                    "content": "Dataclasses are great.",
                    "tags": ["testing"],
                },
            ],
        )

        spec = make_recall_spec(brain_dir)
        result = spec.handler({"query": "testing", "type": "decision"})

        assert "pytest" in result
        assert "Dataclasses" not in result

    def test_project_filter(self, tmp_path):
        """Project filter limits search to matching project files."""
        brain_dir = self._make_brain(tmp_path)
        self._write_fragments(
            brain_dir,
            "2026-06-09-archie.jsonl",
            [
                {
                    "id": "01A",
                    "type": "decision",
                    "topic": "config",
                    "content": "Use YAML config.",
                    "tags": ["config"],
                },
            ],
        )
        self._write_fragments(
            brain_dir,
            "2026-06-09-other.jsonl",
            [
                {
                    "id": "01B",
                    "type": "decision",
                    "topic": "config",
                    "content": "Use TOML config.",
                    "tags": ["config"],
                },
            ],
        )

        spec = make_recall_spec(brain_dir)
        result = spec.handler({"query": "config", "project": "archie"})

        assert "YAML" in result
        assert "TOML" not in result

    def test_since_filter(self, tmp_path):
        """Since filter skips files with dates before the threshold."""
        brain_dir = self._make_brain(tmp_path)
        self._write_fragments(
            brain_dir,
            "2026-06-01-proj.jsonl",
            [
                {
                    "id": "01A",
                    "type": "state",
                    "topic": "old state",
                    "content": "Old information.",
                    "tags": ["state"],
                },
            ],
        )
        self._write_fragments(
            brain_dir,
            "2026-06-08-proj.jsonl",
            [
                {
                    "id": "01B",
                    "type": "state",
                    "topic": "new state",
                    "content": "Current information.",
                    "tags": ["state"],
                },
            ],
        )

        spec = make_recall_spec(brain_dir)
        result = spec.handler({"query": "state", "since": "2026-06-05"})

        assert "Current" in result
        assert "Old" not in result

    def test_recency_bonus(self, tmp_path):
        """Recent fragments get a score bonus over older ones."""
        brain_dir = self._make_brain(tmp_path)
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        old_date = (datetime.now(UTC) - timedelta(days=60)).strftime("%Y-%m-%d")

        # Both match on content equally, but today's gets recency bonus
        self._write_fragments(
            brain_dir,
            f"{old_date}-proj.jsonl",
            [
                {
                    "id": "01OLD",
                    "type": "decision",
                    "topic": "deployment",
                    "content": "Deploy with docker.",
                    "tags": ["deploy"],
                },
            ],
        )
        self._write_fragments(
            brain_dir,
            f"{today}-proj.jsonl",
            [
                {
                    "id": "01NEW",
                    "type": "decision",
                    "topic": "deployment",
                    "content": "Deploy with docker.",
                    "tags": ["deploy"],
                },
            ],
        )

        spec = make_recall_spec(brain_dir)
        result = spec.handler({"query": "deployment"})

        # Recent result should be first (higher score from recency bonus)
        lines = result.strip().split("\n")
        assert "(01NEW" in lines[0]

    def test_limit_parameter(self, tmp_path):
        """Limit parameter caps the number of results."""
        brain_dir = self._make_brain(tmp_path)
        fragments = [
            {
                "id": f"01{i:03d}",
                "type": "decision",
                "topic": "testing",
                "content": f"Fragment {i}.",
                "tags": ["test"],
            }
            for i in range(10)
        ]
        self._write_fragments(brain_dir, "2026-06-09-proj.jsonl", fragments)

        spec = make_recall_spec(brain_dir)
        result = spec.handler({"query": "testing", "limit": 3})

        lines = [line for line in result.strip().split("\n") if line.strip()]
        assert len(lines) == 3

    def test_no_matches_returns_message(self, tmp_path):
        """When nothing matches, return a friendly message."""
        brain_dir = self._make_brain(tmp_path)
        self._write_fragments(
            brain_dir,
            "2026-06-09-proj.jsonl",
            [
                {
                    "id": "01A",
                    "type": "decision",
                    "topic": "unrelated",
                    "content": "Nothing relevant.",
                    "tags": [],
                },
            ],
        )

        spec = make_recall_spec(brain_dir)
        result = spec.handler({"query": "quantum physics"})

        assert "No matching" in result

    def test_empty_query_returns_error(self, tmp_path):
        """Empty query returns an error."""
        brain_dir = self._make_brain(tmp_path)
        spec = make_recall_spec(brain_dir)
        result = spec.handler({"query": ""})
        assert "Error" in result

    def test_ref_tracking(self, tmp_path):
        """Recall queries are recorded in brain.db refs table."""
        brain_dir = self._make_brain(tmp_path)
        self._write_fragments(
            brain_dir,
            "2026-06-09-proj.jsonl",
            [
                {
                    "id": "01A",
                    "type": "decision",
                    "topic": "testing",
                    "content": "Test it.",
                    "tags": ["test"],
                },
            ],
        )

        spec = make_recall_spec(brain_dir)
        spec.handler({"query": "testing"})

        db = sqlite3.connect(brain_dir / "brain.db")
        rows = db.execute("SELECT path FROM refs").fetchall()
        db.close()
        assert len(rows) == 1
        assert "recall:testing" in rows[0][0]

    def test_output_format(self, tmp_path):
        """Output is compact: [type] topic — content (fragment_id)."""
        brain_dir = self._make_brain(tmp_path)
        self._write_fragments(
            brain_dir,
            "2026-06-09-proj.jsonl",
            [
                {
                    "id": "01ABCDEF12345678901234",
                    "type": "decision",
                    "topic": "format",
                    "content": "Keep it compact.",
                    "tags": ["format"],
                },
            ],
        )

        spec = make_recall_spec(brain_dir)
        result = spec.handler({"query": "format"})

        assert "[decision]" in result
        assert "format —" in result
        assert "Keep it compact." in result
        assert "(01ABCDEF)" in result
