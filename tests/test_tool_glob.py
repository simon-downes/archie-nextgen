"""Tests for the glob tool — uses real filesystem and real rg execution."""

import os
import subprocess
from unittest.mock import patch

import pytest

from archie.tools.glob import make_glob_spec


class TestGlobTool:
    """Tests for glob tool using real files and real ripgrep."""

    @pytest.fixture
    def tool(self, tmp_path):
        """Create a glob tool bound to tmp_path."""
        return make_glob_spec(tmp_path, [])

    def test_pattern_match(self, tmp_path, tool):
        """Returns only files matching the glob pattern."""
        (tmp_path / "a.py").write_text("# a")
        (tmp_path / "b.py").write_text("# b")
        (tmp_path / "c.txt").write_text("# c")

        result = tool.handler({"pattern": "*.py"})
        assert "a.py" in result
        assert "b.py" in result
        assert "c.txt" not in result

    def test_mtime_sort_order(self, tmp_path, tool):
        """Results sorted by mtime descending (most recent first)."""
        for i, name in enumerate(["old.py", "mid.py", "new.py"]):
            (tmp_path / name).write_text(f"# {name}")
            os.utime(tmp_path / name, (1000000 + i, 1000000 + i))

        result = tool.handler({"pattern": "*.py"})
        lines = [x for x in result.split("\n") if x.endswith(".py")]
        assert lines == ["new.py", "mid.py", "old.py"]

    def test_limit_truncates(self, tmp_path, tool):
        """Respects limit and shows truncation summary."""
        for i in range(5):
            (tmp_path / f"file{i}.py").write_text(f"# {i}")

        result = tool.handler({"pattern": "*.py", "limit": 2})
        py_lines = [x for x in result.split("\n") if x.endswith(".py")]
        assert len(py_lines) == 2
        assert "2 files shown of 5" in result

    def test_no_summary_when_under_limit(self, tmp_path, tool):
        """No truncation summary when all results fit."""
        for i in range(3):
            (tmp_path / f"file{i}.py").write_text(f"# {i}")

        result = tool.handler({"pattern": "*.py"})
        assert "files shown" not in result

    def test_relative_paths_under_cwd(self, tmp_path, tool):
        """Files under cwd are shown as relative paths."""
        sub = tmp_path / "src"
        sub.mkdir()
        (sub / "main.py").write_text("# main")

        result = tool.handler({"pattern": "**/*.py"})
        assert "src/main.py" in result
        assert str(tmp_path) not in result

    def test_paths_relative_to_search_path(self, tmp_path):
        """When path != cwd, results are relative to search_path."""
        outside = tmp_path / "other"
        outside.mkdir()
        (outside / "lib.py").write_text("# lib")

        tool = make_glob_spec(tmp_path, [outside])
        result = tool.handler({"pattern": "*.py", "path": str(outside)})
        assert "lib.py" in result
        assert str(outside) not in result

    def test_recursive_pattern(self, tmp_path, tool):
        """Recursive glob patterns work through subdirectories."""
        deep = tmp_path / "a" / "b"
        deep.mkdir(parents=True)
        (deep / "deep.py").write_text("# deep")

        result = tool.handler({"pattern": "**/*.py"})
        assert "a/b/deep.py" in result

    def test_empty_results(self, tmp_path, tool):
        """No matches returns 'No files found.'."""
        result = tool.handler({"pattern": "*.nonexistent"})
        assert "No files found." in result

    def test_missing_pattern(self, tmp_path, tool):
        """Missing pattern returns error."""
        result = tool.handler({})
        assert "Error:" in result
        assert "Pattern is required" in result

    def test_nonexistent_directory(self, tmp_path, tool):
        """Non-existent path returns error."""
        result = tool.handler({"pattern": "*.py", "path": "no/such/dir"})
        assert "Error:" in result
        assert "does not exist" in result.lower()

    def test_path_outside_allowed_directories(self, tmp_path, tool):
        """Path outside cwd and allowed_directories is rejected."""
        result = tool.handler({"pattern": "*.py", "path": "/etc"})
        assert "Error:" in result
        assert "outside allowed directories" in result

    def test_ripgrep_not_installed(self, tmp_path, tool):
        """FileNotFoundError from subprocess gives clear error."""
        with patch("archie.tools.glob.subprocess.run", side_effect=FileNotFoundError):
            result = tool.handler({"pattern": "*.py"})
        assert "Error:" in result
        assert "ripgrep (rg) is not installed" in result

    def test_timeout(self, tmp_path, tool):
        """TimeoutExpired gives clear error."""
        with patch(
            "archie.tools.glob.subprocess.run",
            side_effect=subprocess.TimeoutExpired("rg", 15),
        ):
            result = tool.handler({"pattern": "*.py"})
        assert "Error:" in result
        assert "timed out" in result.lower()

    def test_ripgrep_error(self, tmp_path, tool):
        """Non-zero exit (not 1) reports ripgrep error."""
        with patch("archie.tools.glob.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=2, stdout="", stderr="bad regex"
            )
            result = tool.handler({"pattern": "*.py"})
        assert "Error:" in result
        assert "ripgrep error" in result.lower()
