"""Tests for the MemoryExtractor class."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from archie.memory import MemoryExtractor


class TestMemoryExtractor:
    """Tests for memory extraction, watermarks, and JSONL writing."""

    def _make_extractor(self, tmp_path: Path) -> MemoryExtractor:
        """Create an extractor with a mocked Bedrock client."""
        brain_dir = tmp_path / "brain"
        brain_dir.mkdir()
        (brain_dir / "_memory").mkdir()
        with patch("archie.memory.BedrockClient"):
            extractor = MemoryExtractor(brain_dir, "test-model", "us-east-1")
        return extractor

    def _write_session(self, tmp_path: Path, session_id: str, turns: list[dict]) -> None:
        """Write a mock session JSONL file."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(exist_ok=True)
        path = sessions_dir / f"{session_id}.jsonl"
        with path.open("w") as f:
            for turn in turns:
                f.write(json.dumps(turn) + "\n")

    def test_extract_all_processes_unextracted_turns(self, tmp_path, monkeypatch):
        """extract_all sends unextracted turns to the model and writes fragments."""
        monkeypatch.setattr("archie.memory.SESSIONS_DIR", tmp_path / "sessions")

        extractor = self._make_extractor(tmp_path)
        extractor._client = MagicMock()
        extractor._client.invoke.return_value = json.dumps(
            [
                {
                    "type": "decision",
                    "topic": "testing",
                    "content": "Use pytest for all tests.",
                    "tags": ["testing", "python"],
                }
            ]
        )

        self._write_session(
            tmp_path,
            "2026-06-09-myproject-abc12",
            [
                {"id": "01ABC", "user": "How should we test?", "assistant": "Use pytest."},
            ],
        )

        count = extractor.extract_all()

        assert count == 1
        # Verify fragment was written
        memory_dir = tmp_path / "brain" / "_memory"
        jsonl_files = list(memory_dir.glob("*.jsonl"))
        assert len(jsonl_files) == 1
        fragment = json.loads(jsonl_files[0].read_text().strip())
        assert fragment["type"] == "decision"
        assert fragment["topic"] == "testing"
        assert fragment["session_id"] == "2026-06-09-myproject-abc12"

    def test_watermark_prevents_reextraction(self, tmp_path, monkeypatch):
        """Turns already extracted (per watermark) are skipped."""
        monkeypatch.setattr("archie.memory.SESSIONS_DIR", tmp_path / "sessions")

        extractor = self._make_extractor(tmp_path)
        extractor._client = MagicMock()
        extractor._client.invoke.return_value = "[]"

        self._write_session(
            tmp_path,
            "2026-06-09-proj-abc12",
            [
                {"id": "01AAA", "user": "first", "assistant": "resp1"},
                {"id": "01BBB", "user": "second", "assistant": "resp2"},
            ],
        )

        # Set watermark to first turn
        watermark_path = tmp_path / "brain" / "_memory" / ".last_extracted"
        watermark_path.write_text(
            json.dumps(
                {
                    "2026-06-09-proj-abc12": {
                        "turn_id": "01AAA",
                        "extracted_at": "2026-06-09T10:00:00",
                    }
                }
            )
        )

        extractor.extract_all()

        # Should only process the second turn (after watermark)
        call_args = extractor._client.invoke.call_args
        user_text = call_args[0][0][0]["content"][0]["text"]
        assert "second" in user_text
        assert "first" not in user_text

    def test_watermark_updated_after_extraction(self, tmp_path, monkeypatch):
        """Watermark is updated to last processed turn after extraction."""
        monkeypatch.setattr("archie.memory.SESSIONS_DIR", tmp_path / "sessions")

        extractor = self._make_extractor(tmp_path)
        extractor._client = MagicMock()
        extractor._client.invoke.return_value = "[]"

        self._write_session(
            tmp_path,
            "2026-06-09-proj-abc12",
            [
                {"id": "01AAA", "user": "hello", "assistant": "hi"},
                {"id": "01ZZZ", "user": "bye", "assistant": "cya"},
            ],
        )

        extractor.extract_all()

        watermarks = json.loads((tmp_path / "brain" / "_memory" / ".last_extracted").read_text())
        assert watermarks["2026-06-09-proj-abc12"]["turn_id"] == "01ZZZ"

    def test_malformed_json_response_skips_batch(self, tmp_path, monkeypatch):
        """If Haiku returns invalid JSON, log warning and skip (no fragments written)."""
        monkeypatch.setattr("archie.memory.SESSIONS_DIR", tmp_path / "sessions")

        extractor = self._make_extractor(tmp_path)
        extractor._client = MagicMock()
        extractor._client.invoke.return_value = "not valid json {"

        self._write_session(
            tmp_path,
            "2026-06-09-proj-abc12",
            [
                {"id": "01AAA", "user": "hello", "assistant": "hi"},
            ],
        )

        count = extractor.extract_all()

        assert count == 0
        # No JSONL fragment files written
        jsonl_files = list((tmp_path / "brain" / "_memory").glob("*.jsonl"))
        assert len(jsonl_files) == 0

    def test_corrupt_session_lines_skipped(self, tmp_path, monkeypatch):
        """Invalid lines in session JSONL are skipped with a warning."""
        monkeypatch.setattr("archie.memory.SESSIONS_DIR", tmp_path / "sessions")

        extractor = self._make_extractor(tmp_path)
        extractor._client = MagicMock()
        extractor._client.invoke.return_value = json.dumps(
            [{"type": "learning", "topic": "test", "content": "works", "tags": []}]
        )

        # Write session with corrupt line
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        path = sessions_dir / "2026-06-09-proj-abc12.jsonl"
        path.write_text(
            "not json garbage\n"
            + json.dumps({"id": "01AAA", "user": "hello", "assistant": "hi"})
            + "\n"
        )

        count = extractor.extract_all()
        assert count == 1

    def test_empty_extraction_updates_watermark(self, tmp_path, monkeypatch):
        """Even if extraction returns empty array, watermark still advances."""
        monkeypatch.setattr("archie.memory.SESSIONS_DIR", tmp_path / "sessions")

        extractor = self._make_extractor(tmp_path)
        extractor._client = MagicMock()
        extractor._client.invoke.return_value = "[]"

        self._write_session(
            tmp_path,
            "2026-06-09-proj-abc12",
            [
                {"id": "01AAA", "user": "just a typo fix", "assistant": "done"},
            ],
        )

        count = extractor.extract_all()
        assert count == 0

        watermarks = json.loads((tmp_path / "brain" / "_memory" / ".last_extracted").read_text())
        assert "2026-06-09-proj-abc12" in watermarks

    def test_last_fragments_included_in_prompt(self, tmp_path, monkeypatch):
        """Last 3 fragments are included in the extraction prompt for context."""
        monkeypatch.setattr("archie.memory.SESSIONS_DIR", tmp_path / "sessions")

        extractor = self._make_extractor(tmp_path)
        extractor._client = MagicMock()
        extractor._client.invoke.return_value = "[]"

        # Pre-populate memory with existing fragments
        memory_dir = tmp_path / "brain" / "_memory"
        (memory_dir / "2026-06-09-myproject.jsonl").write_text(
            json.dumps(
                {
                    "id": "01X",
                    "type": "decision",
                    "topic": "old topic",
                    "content": "old content",
                    "tags": [],
                }
            )
            + "\n"
        )

        self._write_session(
            tmp_path,
            "2026-06-09-myproject-abc12",
            [
                {"id": "01AAA", "user": "new question", "assistant": "new answer"},
            ],
        )

        extractor.extract_all()

        call_args = extractor._client.invoke.call_args
        user_text = call_args[0][0][0]["content"][0]["text"]
        assert "old topic" in user_text
        assert "old content" in user_text

    def test_code_fenced_json_response_parsed(self, tmp_path, monkeypatch):
        """Extraction handles JSON wrapped in markdown code fences."""
        monkeypatch.setattr("archie.memory.SESSIONS_DIR", tmp_path / "sessions")

        extractor = self._make_extractor(tmp_path)
        extractor._client = MagicMock()
        extractor._client.invoke.return_value = (
            "```json\n"
            + json.dumps([{"type": "state", "topic": "progress", "content": "done", "tags": []}])
            + "\n```"
        )

        self._write_session(
            tmp_path,
            "2026-06-09-proj-abc12",
            [
                {"id": "01AAA", "user": "status?", "assistant": "all done"},
            ],
        )

        count = extractor.extract_all()
        assert count == 1
