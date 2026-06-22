"""Tests for write_file and edit_file tools."""

import pytest

from archie.tools.edit_file import make_edit_file_spec
from archie.tools.write_file import make_write_file_spec


class TestWriteFile:
    @pytest.fixture
    def cache(self):
        return {}

    @pytest.fixture
    def tool(self, tmp_path, cache):
        return make_write_file_spec(tmp_path, [], cache)

    def test_creates_new_file(self, tmp_path, tool):
        result = tool.handler({"path": "new.py", "content": "print('hello')\n"})
        assert "Written: new.py (1 lines)" in result
        assert (tmp_path / "new.py").read_text() == "print('hello')\n"

    def test_overwrites_existing_file(self, tmp_path, tool):
        f = tmp_path / "exist.py"
        f.write_text("old content\n")
        result = tool.handler({"path": "exist.py", "content": "new content\n"})
        assert "Written" in result
        assert f.read_text() == "new content\n"

    def test_creates_parent_directories(self, tmp_path, tool):
        result = tool.handler({"path": "src/pkg/__init__.py", "content": ""})
        assert "Written" in result
        assert (tmp_path / "src" / "pkg" / "__init__.py").exists()

    def test_append_to_existing_file(self, tmp_path, tool):
        f = tmp_path / "big.py"
        f.write_text("part one\n")
        result = tool.handler({"path": "big.py", "content": "part two\n", "append": True})
        assert "Appended: big.py (+1 lines)" in result
        assert f.read_text() == "part one\npart two\n"

    def test_append_to_missing_file_creates_it(self, tmp_path, tool):
        result = tool.handler({"path": "fresh.py", "content": "hello\n", "append": True})
        assert "fresh.py" in result
        assert (tmp_path / "fresh.py").read_text() == "hello\n"

    def test_rejects_path_outside_allowed(self, tmp_path, tool):
        result = tool.handler({"path": "/etc/evil.conf", "content": "bad"})
        assert "Error:" in result
        assert "outside allowed directories" in result

    def test_refuses_binary_overwrite(self, tmp_path, tool):
        f = tmp_path / "binary.dat"
        f.write_bytes(b"data\x00here")
        result = tool.handler({"path": "binary.dat", "content": "text"})
        assert "Error:" in result
        assert "binary" in result.lower()
        # File should be unchanged
        assert f.read_bytes() == b"data\x00here"

    def test_empty_content_creates_empty_file(self, tmp_path, tool):
        result = tool.handler({"path": "empty.txt", "content": ""})
        assert "0 lines" in result
        assert (tmp_path / "empty.txt").read_text() == ""

    def test_multiline_content_reports_line_count(self, tmp_path, tool):
        content = "line1\nline2\nline3\n"
        result = tool.handler({"path": "multi.txt", "content": content})
        assert "3 lines" in result

    def test_invalidates_mtime_cache(self, tmp_path, cache):
        """Write invalidates any read_file cache entries for that path."""
        f = tmp_path / "cached.txt"
        f.write_text("original\n")
        resolved = str(f.resolve())

        # Simulate a prior read_file cache entry
        cache[(resolved, 0, 500)] = f.stat().st_mtime

        tool = make_write_file_spec(tmp_path, [], cache)
        tool.handler({"path": "cached.txt", "content": "updated\n"})

        # Cache entry should be gone
        assert (resolved, 0, 500) not in cache

    def test_writes_to_allowed_directory_outside_cwd(self, tmp_path):
        """Can write to an explicitly allowed directory that isn't cwd."""
        other = tmp_path / "other"
        other.mkdir()
        cwd = tmp_path / "project"
        cwd.mkdir()

        tool = make_write_file_spec(cwd, [other], {})
        result = tool.handler({"path": str(other / "file.txt"), "content": "hello\n"})
        assert "Written" in result
        assert (other / "file.txt").read_text() == "hello\n"


class TestEditFile:
    @pytest.fixture
    def cache(self):
        return {}

    @pytest.fixture
    def tool(self, tmp_path, cache):
        return make_edit_file_spec(tmp_path, [], cache)

    def test_single_edit(self, tmp_path, tool):
        f = tmp_path / "test.py"
        f.write_text("def hello():\n    pass\n")
        result = tool.handler(
            {
                "path": "test.py",
                "edits": [{"old": "    pass", "new": "    return 'hello'"}],
            }
        )
        assert "1 edit(s) at lines" in result
        assert f.read_text() == "def hello():\n    return 'hello'\n"

    def test_multiple_edits_sequential(self, tmp_path, tool):
        f = tmp_path / "test.py"
        f.write_text("x = 1\ny = 2\n")
        result = tool.handler(
            {
                "path": "test.py",
                "edits": [
                    {"old": "x = 1", "new": "x = 10"},
                    {"old": "y = 2", "new": "y = 20"},
                ],
            }
        )
        assert "2 edit(s) at lines" in result
        assert f.read_text() == "x = 10\ny = 20\n"

    def test_unique_match_enforcement(self, tmp_path, tool):
        """Fails when old text matches multiple times without replace_all."""
        f = tmp_path / "test.py"
        f.write_text("foo\nbar\nfoo\n")
        result = tool.handler(
            {
                "path": "test.py",
                "edits": [{"old": "foo", "new": "baz"}],
            }
        )
        assert "Error:" in result
        assert "found 2 matches" in result
        # File unchanged
        assert f.read_text() == "foo\nbar\nfoo\n"

    def test_replace_all(self, tmp_path, tool):
        f = tmp_path / "test.py"
        f.write_text("foo\nbar\nfoo\n")
        result = tool.handler(
            {
                "path": "test.py",
                "edits": [{"old": "foo", "new": "baz", "replace_all": True}],
            }
        )
        assert "2 replacements" in result or "2 edits" in result
        assert f.read_text() == "baz\nbar\nbaz\n"

    def test_text_not_found(self, tmp_path, tool):
        f = tmp_path / "test.py"
        f.write_text("hello world\n")
        result = tool.handler(
            {
                "path": "test.py",
                "edits": [{"old": "nonexistent", "new": "replacement"}],
            }
        )
        assert "Error:" in result
        assert "not found" in result

    def test_atomicity_partial_failure(self, tmp_path, tool):
        """If edit 2 fails, edit 1 is NOT written to disk."""
        f = tmp_path / "test.py"
        f.write_text("aaa\nbbb\n")
        result = tool.handler(
            {
                "path": "test.py",
                "edits": [
                    {"old": "aaa", "new": "ccc"},  # would succeed
                    {"old": "nonexistent", "new": "ddd"},  # fails
                ],
            }
        )
        assert "Error:" in result
        # File unchanged — first edit was NOT applied
        assert f.read_text() == "aaa\nbbb\n"

    def test_file_not_found(self, tmp_path, tool):
        result = tool.handler(
            {
                "path": "nope.py",
                "edits": [{"old": "x", "new": "y"}],
            }
        )
        assert "Error:" in result
        assert "File not found" in result
        assert "write_file" in result

    def test_rejects_path_outside_allowed(self, tmp_path, tool):
        result = tool.handler(
            {
                "path": "/etc/passwd",
                "edits": [{"old": "x", "new": "y"}],
            }
        )
        assert "Error:" in result
        assert "outside allowed directories" in result

    def test_empty_old_string_rejected(self, tmp_path, tool):
        f = tmp_path / "test.py"
        f.write_text("content\n")
        result = tool.handler(
            {
                "path": "test.py",
                "edits": [{"old": "", "new": "replacement"}],
            }
        )
        assert "Error:" in result
        assert "cannot be empty" in result

    def test_invalidates_mtime_cache(self, tmp_path, cache):
        """Edit invalidates any read_file cache entries for that path."""
        f = tmp_path / "cached.txt"
        f.write_text("original text\n")
        resolved = str(f.resolve())

        cache[(resolved, 0, 500)] = f.stat().st_mtime

        tool = make_edit_file_spec(tmp_path, [], cache)
        tool.handler(
            {
                "path": "cached.txt",
                "edits": [{"old": "original", "new": "modified"}],
            }
        )

        assert (resolved, 0, 500) not in cache

    def test_edits_chain_sequentially(self, tmp_path, tool):
        """Edit 2 sees the result of edit 1."""
        f = tmp_path / "test.py"
        f.write_text("aaa bbb ccc\n")
        result = tool.handler(
            {
                "path": "test.py",
                "edits": [
                    {"old": "aaa", "new": "xxx"},
                    {"old": "xxx bbb", "new": "yyy"},  # depends on edit 1's result
                ],
            }
        )
        assert "2 edit(s) at lines" in result
        assert f.read_text() == "yyy ccc\n"

    def test_replace_all_single_match(self, tmp_path, tool):
        """replace_all with only 1 occurrence still works and reports as regular edit."""
        f = tmp_path / "test.py"
        f.write_text("unique_string here\n")
        result = tool.handler(
            {
                "path": "test.py",
                "edits": [{"old": "unique_string", "new": "replaced", "replace_all": True}],
            }
        )
        # 1 replacement == 1 edit, so uses "edit(s) applied" not "replacements"
        assert "1 edit(s) at lines" in result
        assert f.read_text() == "replaced here\n"
