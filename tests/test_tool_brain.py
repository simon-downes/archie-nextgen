"""Tests for the BrainIndex class and brain tool handler."""

import sqlite3
from pathlib import Path
from unittest.mock import patch

import yaml

from archie.brain import BrainIndex
from archie.tools.brain import make_brain_spec


class TestBrainIndex:
    """Tests for BrainIndex CRUD operations."""

    def setup_method(self, tmp_path=None):
        """Set up is called by pytest with tmp_path via the fixture below."""

    def _make_brain(self, tmp_path: Path) -> BrainIndex:
        """Helper to create a brain directory and BrainIndex instance."""
        brain_dir = tmp_path / "brain"
        brain_dir.mkdir()
        (brain_dir / "projects").mkdir()
        (brain_dir / "knowledge").mkdir()
        (brain_dir / "index.yaml").write_text("{}\n")
        return BrainIndex(brain_dir)

    def test_write_creates_file_with_frontmatter(self, tmp_path):
        """Write creates a new file with YAML frontmatter and body."""
        brain = self._make_brain(tmp_path)

        brain.write("projects/archie.md", "Archie", "AI assistant", ["ai", "python"], "# Archie\n")

        file_path = tmp_path / "brain" / "projects" / "archie.md"
        assert file_path.exists()

        text = file_path.read_text()
        assert text.startswith("---\n")
        fm, body = brain._parse_frontmatter(text)
        assert fm["name"] == "Archie"
        assert fm["summary"] == "AI assistant"
        assert fm["tags"] == ["ai", "python"]
        assert "# Archie" in body

    def test_write_updates_index(self, tmp_path):
        """Write updates index.yaml with the new entry."""
        brain = self._make_brain(tmp_path)

        brain.write("projects/archie.md", "Archie", "AI assistant", ["ai"], "Body")

        index = yaml.safe_load((tmp_path / "brain" / "index.yaml").read_text())
        assert "projects" in index
        assert "archie" in index["projects"]
        assert index["projects"]["archie"]["name"] == "Archie"

    def test_write_merges_frontmatter_on_update(self, tmp_path):
        """Update preserves existing frontmatter fields not in the request."""
        brain = self._make_brain(tmp_path)

        # Write initial file with extra field in frontmatter
        file_path = tmp_path / "brain" / "projects" / "archie.md"
        file_path.write_text(
            "---\nname: Old Name\nsummary: Old\ntags: []\ncustom_field: preserved\n---\nOld body\n"
        )

        brain.write("projects/archie.md", "New Name", "New summary", ["new"], "New body")

        text = file_path.read_text()
        fm, _ = brain._parse_frontmatter(text)
        assert fm["name"] == "New Name"
        assert fm["custom_field"] == "preserved"

    def test_read_returns_frontmatter_and_body(self, tmp_path):
        """Read returns parsed frontmatter dict and body text."""
        brain = self._make_brain(tmp_path)

        (tmp_path / "brain" / "projects" / "test.md").write_text(
            "---\nname: Test\nsummary: A test\ntags:\n- testing\n---\nBody content here.\n"
        )

        fm, body = brain.read("projects/test.md")
        assert fm["name"] == "Test"
        assert fm["tags"] == ["testing"]
        assert "Body content here." in body

    def test_read_records_ref(self, tmp_path):
        """Read records an access in the refs SQLite table."""
        brain = self._make_brain(tmp_path)

        (tmp_path / "brain" / "projects" / "test.md").write_text(
            "---\nname: Test\nsummary: x\ntags: []\n---\nBody\n"
        )

        brain.read("projects/test.md")

        db_path = tmp_path / "brain" / "brain.db"
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT path FROM refs").fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][0] == "projects/test.md"

    def test_read_nonexistent_raises(self, tmp_path):
        """Read raises ValueError for missing files."""
        brain = self._make_brain(tmp_path)
        import pytest

        with pytest.raises(ValueError, match="Not found"):
            brain.read("projects/missing.md")

    def test_search_scores_name_highest(self, tmp_path):
        """Name matches score +3, higher than tags (+2) or summary (+1)."""
        brain = self._make_brain(tmp_path)
        index = {
            "projects": {
                "archie": {
                    "name": "Archie",
                    "path": "projects/archie.md",
                    "summary": "something else",
                    "tags": ["other"],
                },
                "other": {
                    "name": "Other Project",
                    "path": "projects/other.md",
                    "summary": "mentions archie",
                    "tags": ["archie"],
                },
            }
        }
        (tmp_path / "brain" / "index.yaml").write_text(yaml.safe_dump(index))

        # Mock rg to not find anything (isolate index scoring)
        with patch("archie.brain.subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"returncode": 1, "stdout": ""})()
            results = brain.search("archie")

        # First result should be the one with "Archie" in name (score 3)
        assert results[0]["path"] == "projects/archie.md"
        assert results[0]["score"] >= 3

    def test_search_with_scope(self, tmp_path):
        """Scope limits search to a specific subdirectory."""
        brain = self._make_brain(tmp_path)
        index = {
            "projects": {
                "archie": {
                    "name": "Archie",
                    "path": "projects/archie.md",
                    "summary": "",
                    "tags": ["ai"],
                }
            },
            "knowledge": {
                "ai-patterns": {
                    "name": "AI Patterns",
                    "path": "knowledge/ai-patterns.md",
                    "summary": "",
                    "tags": ["ai"],
                }
            },
        }
        (tmp_path / "brain" / "index.yaml").write_text(yaml.safe_dump(index))

        with patch("archie.brain.subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"returncode": 1, "stdout": ""})()
            results = brain.search("ai", scope="knowledge")

        paths = [r["path"] for r in results]
        assert "knowledge/ai-patterns.md" in paths
        assert "projects/archie.md" not in paths

    def test_search_removes_stopwords(self, tmp_path):
        """Stopwords are filtered from query terms."""
        brain = self._make_brain(tmp_path)
        index = {
            "projects": {
                "test": {
                    "name": "Test",
                    "path": "projects/test.md",
                    "summary": "",
                    "tags": ["the"],  # "the" is a stopword
                }
            }
        }
        (tmp_path / "brain" / "index.yaml").write_text(yaml.safe_dump(index))

        with patch("archie.brain.subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"returncode": 1, "stdout": ""})()
            results = brain.search("the")

        # "the" should be removed as stopword, yielding no results
        assert results == []

    def test_commit_stages_and_commits(self, tmp_path):
        """Commit calls git add + git commit."""
        brain = self._make_brain(tmp_path)

        with patch("archie.brain.subprocess.run") as mock_run:
            from subprocess import CompletedProcess

            mock_run.return_value = CompletedProcess([], 0, "[main abc1234] Update\n", "")
            result = brain.commit("Update brain")

        assert "Committed" in result
        # Verify git add -A was called
        calls = mock_run.call_args_list
        assert any("add" in str(c) for c in calls)
        assert any("commit" in str(c) for c in calls)

    def test_commit_nothing_to_commit(self, tmp_path):
        """Commit reports cleanly when nothing to commit."""
        brain = self._make_brain(tmp_path)

        with patch("archie.brain.subprocess.run") as mock_run:
            from subprocess import CompletedProcess

            mock_run.return_value = CompletedProcess([], 1, "nothing to commit", "")
            result = brain.commit("Update brain")

        assert "Nothing to commit" in result

    def test_validate_path_rejects_dotdot(self, tmp_path):
        """Path with '..' is rejected."""
        brain = self._make_brain(tmp_path)
        import pytest

        with pytest.raises(ValueError, match="contains '..'"):
            brain._validate_path("../etc/passwd")

    def test_validate_path_rejects_git_dir(self, tmp_path):
        """Access to .git/ is blocked."""
        brain = self._make_brain(tmp_path)
        import pytest

        with pytest.raises(ValueError, match="protected"):
            brain._validate_path(".git/config")

    def test_validate_path_rejects_brain_db(self, tmp_path):
        """Direct access to brain.db is blocked."""
        brain = self._make_brain(tmp_path)
        import pytest

        with pytest.raises(ValueError, match="protected"):
            brain._validate_path("brain.db")

    def test_validate_path_rejects_memory(self, tmp_path):
        """Access to _memory/ via brain tool is blocked."""
        brain = self._make_brain(tmp_path)
        import pytest

        with pytest.raises(ValueError, match="_memory"):
            brain._validate_path("_memory/fragment.md")

    def test_parse_frontmatter_valid(self, tmp_path):
        """Parses valid frontmatter correctly."""
        text = "---\nname: Test\ntags:\n- a\n- b\n---\nBody here."
        fm, body = BrainIndex._parse_frontmatter(text)
        assert fm["name"] == "Test"
        assert fm["tags"] == ["a", "b"]
        assert body == "Body here."

    def test_parse_frontmatter_missing(self, tmp_path):
        """Returns empty dict + full text when no frontmatter."""
        text = "Just regular content."
        fm, body = BrainIndex._parse_frontmatter(text)
        assert fm == {}
        assert body == text

    def test_parse_frontmatter_invalid_yaml(self, tmp_path):
        """Returns empty dict when frontmatter YAML is malformed."""
        text = "---\n: [invalid yaml\n---\nBody."
        fm, body = BrainIndex._parse_frontmatter(text)
        assert fm == {}


class TestBrainTool:
    """Tests for the brain tool spec and handler."""

    def setup_method(self):
        pass

    def _make_brain_dir(self, tmp_path: Path) -> Path:
        """Helper to create a brain directory structure."""
        brain_dir = tmp_path / "brain"
        brain_dir.mkdir()
        (brain_dir / "projects").mkdir()
        (brain_dir / "knowledge").mkdir()
        (brain_dir / "index.yaml").write_text("{}\n")
        return brain_dir

    def test_spec_metadata(self, tmp_path):
        """Tool spec has correct name and schema."""
        brain_dir = self._make_brain_dir(tmp_path)
        spec = make_brain_spec(brain_dir)

        assert spec.name == "brain"
        assert "operation" in spec.schema["properties"]
        assert spec.schema["properties"]["operation"]["enum"] == [
            "read",
            "write",
            "search",
            "commit",
        ]

    def test_read_operation(self, tmp_path):
        """Read operation returns formatted content."""
        brain_dir = self._make_brain_dir(tmp_path)
        (brain_dir / "projects" / "test.md").write_text(
            "---\nname: Test\nsummary: A test item\ntags:\n- testing\n---\nBody content.\n"
        )

        spec = make_brain_spec(brain_dir)
        result = spec.handler({"operation": "read", "path": "projects/test.md"})

        assert "Test" in result
        assert "Body content." in result
        assert "Error" not in result

    def test_read_missing_path(self, tmp_path):
        """Read without path returns error."""
        brain_dir = self._make_brain_dir(tmp_path)
        spec = make_brain_spec(brain_dir)

        result = spec.handler({"operation": "read", "path": ""})
        assert "Error" in result

    def test_write_operation(self, tmp_path):
        """Write operation creates file and returns success."""
        brain_dir = self._make_brain_dir(tmp_path)
        spec = make_brain_spec(brain_dir)

        result = spec.handler(
            {
                "operation": "write",
                "path": "projects/new.md",
                "name": "New Item",
                "summary": "A new item",
                "tags": ["new"],
                "content": "# New\n\nContent here.",
            }
        )

        assert "Written: projects/new.md" in result
        assert (brain_dir / "projects" / "new.md").exists()

    def test_write_missing_name(self, tmp_path):
        """Write without name returns error."""
        brain_dir = self._make_brain_dir(tmp_path)
        spec = make_brain_spec(brain_dir)

        result = spec.handler(
            {
                "operation": "write",
                "path": "projects/test.md",
                "name": "",
                "content": "Body",
            }
        )
        assert "Error" in result

    def test_search_operation(self, tmp_path):
        """Search returns scored results from index."""
        brain_dir = self._make_brain_dir(tmp_path)
        index = {
            "projects": {
                "archie": {
                    "name": "Archie",
                    "path": "projects/archie.md",
                    "summary": "AI assistant",
                    "tags": ["ai"],
                }
            }
        }
        (brain_dir / "index.yaml").write_text(yaml.safe_dump(index))

        spec = make_brain_spec(brain_dir)

        with patch("archie.brain.subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"returncode": 1, "stdout": ""})()
            result = spec.handler({"operation": "search", "query": "archie"})

        assert "Archie" in result
        assert "score=" in result

    def test_search_no_results(self, tmp_path):
        """Search returns friendly message when nothing found."""
        brain_dir = self._make_brain_dir(tmp_path)
        spec = make_brain_spec(brain_dir)

        with patch("archie.brain.subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"returncode": 1, "stdout": ""})()
            result = spec.handler({"operation": "search", "query": "nonexistent"})

        assert "No results" in result

    def test_commit_operation(self, tmp_path):
        """Commit operation calls git."""
        brain_dir = self._make_brain_dir(tmp_path)
        spec = make_brain_spec(brain_dir)

        with patch("archie.brain.subprocess.run") as mock_run:
            from subprocess import CompletedProcess

            mock_run.return_value = CompletedProcess([], 0, "[main abc123] msg\n", "")
            result = spec.handler({"operation": "commit", "message": "Update"})

        assert "Committed" in result

    def test_unknown_operation(self, tmp_path):
        """Unknown operation returns error."""
        brain_dir = self._make_brain_dir(tmp_path)
        spec = make_brain_spec(brain_dir)

        result = spec.handler({"operation": "invalid"})
        assert "Error" in result
        assert "Unknown operation" in result
