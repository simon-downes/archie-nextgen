"""Tests for archie init and archie brain reindex CLI commands."""

from unittest.mock import patch

import yaml
from click.testing import CliRunner

from archie.cli import main


class TestInitCommand:
    """Tests for `archie init` — brain directory structure creation."""

    def setup_method(self):
        self.runner = CliRunner()

    def test_creates_brain_structure(self, tmp_path, monkeypatch):
        """Creates all expected subdirectories and files."""
        monkeypatch.setattr("archie.config.ARCHIE_DIR", tmp_path)
        monkeypatch.setattr("archie.config.CONFIG_PATH", tmp_path / "nextgen.yaml")

        brain_dir = tmp_path / "brain"
        (tmp_path / "nextgen.yaml").write_text(
            yaml.dump({"model": "eu.anthropic.claude-sonnet-4-6", "brain_dir": str(brain_dir)})
        )

        with patch("archie.cli.subprocess.run") as mock_run:
            from subprocess import CompletedProcess

            mock_run.return_value = CompletedProcess([], 0, "", "")
            result = self.runner.invoke(main, ["init"])

        assert result.exit_code == 0
        assert (brain_dir / "_memory").is_dir()
        assert (brain_dir / "projects").is_dir()
        assert (brain_dir / "knowledge").is_dir()
        assert (brain_dir / "people").is_dir()
        assert (brain_dir / "index.yaml").exists()
        assert (brain_dir / ".gitignore").exists()

        # Verify index.yaml content is empty dict
        assert yaml.safe_load((brain_dir / "index.yaml").read_text()) == {}

        # Verify .gitignore content
        gitignore = (brain_dir / ".gitignore").read_text()
        assert "brain.db" in gitignore
        assert ".last_extracted" in gitignore

    def test_idempotent(self, tmp_path, monkeypatch):
        """Running init twice doesn't overwrite existing files."""
        monkeypatch.setattr("archie.config.ARCHIE_DIR", tmp_path)
        monkeypatch.setattr("archie.config.CONFIG_PATH", tmp_path / "nextgen.yaml")

        brain_dir = tmp_path / "brain"
        (tmp_path / "nextgen.yaml").write_text(
            yaml.dump({"model": "eu.anthropic.claude-sonnet-4-6", "brain_dir": str(brain_dir)})
        )

        # Create brain dir with custom index content
        brain_dir.mkdir(parents=True)
        (brain_dir / "_memory").mkdir()
        (brain_dir / "index.yaml").write_text("custom: content\n")
        (brain_dir / ".gitignore").write_text("custom-ignore\n")
        (brain_dir / ".git").mkdir()  # Simulate existing git repo

        result = self.runner.invoke(main, ["init"])

        assert result.exit_code == 0
        # Existing files should NOT be overwritten
        assert (brain_dir / "index.yaml").read_text() == "custom: content\n"
        assert (brain_dir / ".gitignore").read_text() == "custom-ignore\n"
        # But missing dirs should be created
        assert (brain_dir / "projects").is_dir()
        assert (brain_dir / "knowledge").is_dir()
        assert (brain_dir / "people").is_dir()


class TestBrainReindexCommand:
    """Tests for `archie brain reindex` — index rebuild from frontmatter."""

    def setup_method(self):
        self.runner = CliRunner()

    def test_reindex_builds_correct_index(self, tmp_path, monkeypatch):
        """Scans .md files with frontmatter and builds index.yaml."""
        monkeypatch.setattr("archie.config.ARCHIE_DIR", tmp_path)
        monkeypatch.setattr("archie.config.CONFIG_PATH", tmp_path / "nextgen.yaml")

        brain_dir = tmp_path / "brain"
        (tmp_path / "nextgen.yaml").write_text(
            yaml.dump({"model": "eu.anthropic.claude-sonnet-4-6", "brain_dir": str(brain_dir)})
        )

        # Create brain structure with sample files
        (brain_dir / "projects").mkdir(parents=True)
        (brain_dir / "knowledge").mkdir(parents=True)
        (brain_dir / "index.yaml").write_text("{}\n")

        # Project file with frontmatter
        (brain_dir / "projects" / "archie.md").write_text(
            "---\nname: Archie\nsummary: AI assistant\ntags:\n- ai\n- python\n---\nBody content.\n"
        )

        # Knowledge file with frontmatter
        (brain_dir / "knowledge" / "testing.md").write_text(
            "---\nname: Testing Patterns\nsummary: How to test\ntags:\n- testing\n---\nMore.\n"
        )

        # File without frontmatter — should be skipped
        (brain_dir / "knowledge" / "notes.md").write_text("No frontmatter here.\n")

        result = self.runner.invoke(main, ["brain", "reindex"])

        assert result.exit_code == 0
        assert "2 items" in result.output

        index = yaml.safe_load((brain_dir / "index.yaml").read_text())
        assert "projects" in index
        assert "archie" in index["projects"]
        assert index["projects"]["archie"]["name"] == "Archie"
        assert index["projects"]["archie"]["tags"] == ["ai", "python"]
        assert "knowledge" in index
        assert "testing" in index["knowledge"]

    def test_reindex_skips_memory_dir(self, tmp_path, monkeypatch):
        """Files in _memory/ are not indexed."""
        monkeypatch.setattr("archie.config.ARCHIE_DIR", tmp_path)
        monkeypatch.setattr("archie.config.CONFIG_PATH", tmp_path / "nextgen.yaml")

        brain_dir = tmp_path / "brain"
        (tmp_path / "nextgen.yaml").write_text(
            yaml.dump({"model": "eu.anthropic.claude-sonnet-4-6", "brain_dir": str(brain_dir)})
        )

        (brain_dir / "_memory").mkdir(parents=True)
        (brain_dir / "index.yaml").write_text("{}\n")

        # Memory file — should be skipped
        (brain_dir / "_memory" / "fragment.md").write_text(
            "---\nname: Fragment\nsummary: x\ntags: []\n---\nBody.\n"
        )

        result = self.runner.invoke(main, ["brain", "reindex"])

        assert result.exit_code == 0
        assert "0 items" in result.output

    def test_reindex_missing_brain_dir(self, tmp_path, monkeypatch):
        """Error when brain dir doesn't exist."""
        monkeypatch.setattr("archie.config.ARCHIE_DIR", tmp_path)
        monkeypatch.setattr("archie.config.CONFIG_PATH", tmp_path / "nextgen.yaml")

        (tmp_path / "nextgen.yaml").write_text(
            yaml.dump(
                {
                    "model": "eu.anthropic.claude-sonnet-4-6",
                    "brain_dir": str(tmp_path / "nonexistent"),
                }
            )
        )

        result = self.runner.invoke(main, ["brain", "reindex"])
        assert result.exit_code != 0
        assert "archie init" in result.output
