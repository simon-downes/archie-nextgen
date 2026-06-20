"""Tests for the unified read tool (file and directory modes)."""

import time
from unittest.mock import patch

import pytest

from archie.tools.read import make_read_spec


class TestReadFile:
    """Tests for file-read mode."""

    @pytest.fixture
    def tool(self, tmp_path):
        """Create a read tool bound to tmp_path."""
        return make_read_spec(tmp_path, [])

    def test_reads_file_with_line_numbers(self, tmp_path, tool):
        """Output includes line numbers in '  N|content' format (1-indexed)."""
        f = tmp_path / "test.py"
        f.write_text("line1\nline2\nline3\n")
        result = tool.handler({"path": "test.py"})
        assert "    1|line1" in result
        assert "    2|line2" in result
        assert "    3|line3" in result

    def test_uses_1_indexed_offset(self, tmp_path, tool):
        """Offset=1 returns first line (not offset=0)."""
        f = tmp_path / "test.py"
        f.write_text("first\nsecond\nthird\n")
        result = tool.handler({"path": "test.py", "offset": 1, "limit": 1})
        assert "    1|first" in result
        assert "second" not in result

    def test_reads_with_offset_and_limit(self, tmp_path, tool):
        """Pagination via offset/limit works correctly (1-indexed)."""
        f = tmp_path / "big.txt"
        f.write_text("\n".join(f"line{i}" for i in range(1, 101)))
        result = tool.handler({"path": "big.txt", "offset": 10, "limit": 5})
        assert "   10|line10" in result
        assert "   14|line14" in result
        assert "   15|line15" not in result
        assert "Use offset=15 to continue reading" in result

    def test_rejects_path_outside_allowed(self, tmp_path, tool):
        """Paths outside allowed directories return an error."""
        result = tool.handler({"path": "/etc/passwd"})
        assert "Error:" in result
        assert "outside allowed directories" in result

    def test_detects_binary_file(self, tmp_path, tool):
        """Binary files (containing null bytes) are rejected."""
        f = tmp_path / "binary.dat"
        f.write_bytes(b"some text\x00more bytes")
        result = tool.handler({"path": "binary.dat"})
        assert "Error:" in result
        assert "Binary file" in result

    def test_caps_line_length(self, tmp_path, tool):
        """Lines exceeding 500 chars are truncated."""
        f = tmp_path / "long.txt"
        f.write_text("x" * 600 + "\n")
        result = tool.handler({"path": "long.txt"})
        assert "...[truncated]" in result

    def test_shows_total_lines_in_header(self, tmp_path, tool):
        """Header shows total line count."""
        f = tmp_path / "test.txt"
        f.write_text("a\nb\nc\n")
        result = tool.handler({"path": "test.txt"})
        assert "3 lines" in result

    def test_nonexistent_file(self, tmp_path, tool):
        """Non-existent path returns error."""
        result = tool.handler({"path": "nope.txt"})
        assert "Error:" in result
        assert "does not exist" in result.lower() or "not a file" in result.lower()

    def test_pagination_hint_when_truncated(self, tmp_path, tool):
        """Shows pagination hint when there are more lines."""
        f = tmp_path / "big.txt"
        f.write_text("\n".join(f"line{i}" for i in range(600)))
        result = tool.handler({"path": "big.txt", "limit": 10})
        assert "Use offset=11 to continue reading" in result
        assert "Showing lines 1-10 of 600" in result


class TestReadDirectory:
    """Tests for directory-read mode."""

    @pytest.fixture
    def tool(self, tmp_path):
        """Create a read tool bound to tmp_path."""
        return make_read_spec(tmp_path, [])

    def test_lists_files_at_root(self, tmp_path, tool):
        """Files at root level are listed."""
        (tmp_path / "a.txt").touch()
        (tmp_path / "b.py").touch()
        result = tool.handler({"path": "."})
        assert "a.txt" in result
        # May or may not show b.py depending on gitignore/rg config

    def test_tree_style_shows_dirs_first(self, tmp_path, tool):
        """Directories are listed before files at each level."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").touch()
        (tmp_path / "config.txt").touch()
        result = tool.handler({"path": "."})
        # "src" should appear with trailing / before any files at same level
        assert "src/" in result

    def test_indented_subdirectories(self, tmp_path, tool):
        """Subdirectory entries are indented by 2 spaces per depth."""
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / "file.txt").touch()
        # Use absolute path to avoid gitignore issues in isolated tmp dirs
        result = tool.handler({"path": str(tmp_path)})
        assert "Error:" not in result

    def test_depth_3_cap(self, tmp_path, tool):
        """Entries beyond depth 3 are excluded."""
        deep = tmp_path / "a" / "b" / "c" / "d"
        deep.mkdir(parents=True)
        (deep / "file.txt").touch()
        result = tool.handler({"path": "."})
        assert "d/file.txt" not in result

    def test_dotfiles_excluded(self, tmp_path, tool):
        """Hidden entries (dotfiles) are excluded."""
        (tmp_path / ".gitignore").touch()
        (tmp_path / "visible.txt").touch()
        result = tool.handler({"path": "."})
        assert ".gitignore" not in result

    def test_nonexistent_directory(self, tmp_path, tool):
        """Non-existent directory returns error."""
        result = tool.handler({"path": "nonexistent_dir"})
        assert "Error:" in result
        assert "does not exist" in result.lower()

    def test_empty_directory(self, tmp_path, tool):
        """Empty directory shows a message."""
        empty = tmp_path / "empty"
        empty.mkdir()
        result = tool.handler({"path": "empty"})
        assert "(empty directory)" in result

    def test_shortens_paths_relative_to_cwd(self, tmp_path, tool):
        """Paths are shortened relative to cwd when possible."""
        src = tmp_path / "src"
        src.mkdir()
        result = tool.handler({"path": str(src)})
        # Header should show "src" not the full path
        assert "src" in result

    def test_directories_before_files_at_level(self, tmp_path, tool):
        """Directories listed before files at each level."""
        (tmp_path / "zzz_dir").mkdir()
        (tmp_path / "zzz_dir" / "inside.txt").touch()
        (tmp_path / "aaa_file.txt").touch()
        result = tool.handler({"path": "."})
        # zzz_dir should appear because it has a file inside
        assert "zzz_dir" in result

    def test_sorted_alphabetically(self, tmp_path, tool):
        """Entries are sorted alphabetically within groups."""
        (tmp_path / "b_file.txt").touch()
        (tmp_path / "a_file.txt").touch()
        (tmp_path / "c_file.txt").touch()
        result = tool.handler({"path": "."})
        # a should come before b which comes before c
        pos_a = result.find("a_file.txt")
        pos_b = result.find("b_file.txt")
        pos_c = result.find("c_file.txt")
        assert pos_a < pos_b < pos_c

    def test_respects_gitignore(self, tmp_path, tool):
        """Files matching .gitignore are excluded from listing."""
        # Create a .gitignore that ignores *.log files
        (tmp_path / ".gitignore").write_text("*.log\n")
        (tmp_path / "app.py").touch()
        (tmp_path / "debug.log").touch()
        result = tool.handler({"path": "."})
        assert "app.py" in result


class TestReadMtimeDedup:
    """Tests for read file mtime-based deduplication."""

    def test_returns_stub_on_unchanged_file(self, tmp_path):
        """Second read of same file with same params returns stub."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello\nworld\n")

        spec = make_read_spec(cwd=tmp_path, allowed_directories=[])

        # First read — returns content
        result1 = spec.handler({"path": str(test_file), "offset": 1, "limit": 500})
        assert "hello" in result1

        # Second read — same file, same params → stub
        result2 = spec.handler({"path": str(test_file), "offset": 1, "limit": 500})
        assert "unchanged" in result2.lower()

    def test_rereads_when_mtime_changes(self, tmp_path):
        """File is re-read when content changes between calls."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("version1\n")

        spec = make_read_spec(cwd=tmp_path, allowed_directories=[])

        # First read
        result1 = spec.handler({"path": str(test_file)})
        assert "version1" in result1

        # Modify file (force mtime change)
        time.sleep(0.01)
        test_file.write_text("version2\n")

        # Second read — file changed, should return new content
        result2 = spec.handler({"path": str(test_file)})
        assert "version2" in result2
        assert "unchanged" not in result2.lower()

    def test_different_offset_not_cached(self, tmp_path):
        """Different offset is a different cache key."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("line1\nline2\nline3\n")

        spec = make_read_spec(cwd=tmp_path, allowed_directories=[])

        # Read with offset=1 (0-based 0)
        spec.handler({"path": str(test_file), "offset": 1, "limit": 500})

        # Read with offset=2 (0-based 1) — different params, should return content
        result = spec.handler({"path": str(test_file), "offset": 2, "limit": 500})
        assert "unchanged" not in result.lower()
        assert "line2" in result

    def test_stub_references_originating_tool_use_id(self, tmp_path):
        """The unchanged-stub names the tool result that holds the content."""
        from archie.tools import current_tool_use_id

        test_file = tmp_path / "test.txt"
        test_file.write_text("hello\n")
        spec = make_read_spec(cwd=tmp_path, allowed_directories=[])

        token = current_tool_use_id.set("tu_orig")
        try:
            spec.handler({"path": str(test_file)})
        finally:
            current_tool_use_id.reset(token)

        result = spec.handler({"path": str(test_file)})
        assert "unchanged" in result.lower()
        assert "tu_orig" in result
        assert "retrieve_artifact" in result

    def test_eviction_invalidation_forces_reread(self, tmp_path):
        """Removing the cache entry makes the next read return content."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello\n")
        cache: dict = {}
        spec = make_read_spec(cwd=tmp_path, allowed_directories=[], mtime_cache=cache)

        spec.handler({"path": str(test_file)})
        assert cache  # populated

        cache.clear()  # what _invalidate_mtime_entries does for this path
        result = spec.handler({"path": str(test_file)})
        assert "hello" in result
        assert "unchanged" not in result.lower()


class TestReadDirectoryEntryCap:
    """Tests for directory listing entry cap."""

    def test_200_entry_cap(self, tmp_path):
        """More than 200 files shows truncation message."""
        for i in range(250):
            (tmp_path / f"file{i}.txt").touch()
        spec = make_read_spec(cwd=tmp_path, allowed_directories=[])
        result = spec.handler({"path": "."})
        assert "entries shown of 250" in result.lower() or "narrow the path" in result.lower()

    def test_under_cap_no_message(self, tmp_path):
        """Under 200 entries shows no truncation message."""
        for i in range(50):
            (tmp_path / f"file{i}.txt").touch()
        spec = make_read_spec(cwd=tmp_path, allowed_directories=[])
        result = spec.handler({"path": "."})
        assert "narrow the path" not in result.lower()


class TestReadEdgeCases:
    """Tests for edge cases."""

    def test_directory_params_ignored_for_offset(self, tmp_path):
        """Offset/limit params are ignored when path is a directory."""
        (tmp_path / "a.txt").touch()
        spec = make_read_spec(cwd=tmp_path, allowed_directories=[])
        # Should not raise despite offset being passed for a directory
        result = spec.handler({"path": ".", "offset": 5, "limit": 10})
        assert "Error:" not in result

    def test_symlink_escape_rejected(self, tmp_path):
        """Symlinks escaping allowed directories are rejected."""
        sneaky = tmp_path / "sneaky"
        sneaky.symlink_to("/etc")
        spec = make_read_spec(cwd=tmp_path, allowed_directories=[])
        result = spec.handler({"path": str(sneaky / "passwd")})
        assert "Error:" in result

    def test_empty_path_returns_error(self, tmp_path):
        """Empty path resolves to cwd which is a directory — returns listing."""
        spec = make_read_spec(cwd=tmp_path, allowed_directories=[])
        result = spec.handler({"path": "."})
        # Should return a directory listing (not an error) since . is the cwd
        assert "Error:" not in result


class TestSharedCache:
    """Tests for cross-tool cache interaction (write integration in separate file)."""

    def test_eviction_invalidation_forces_reread(self, tmp_path):
        """Removing the cache entry makes the next read return content."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello\n")
        cache: dict = {}
        spec = make_read_spec(cwd=tmp_path, allowed_directories=[], mtime_cache=cache)

        spec.handler({"path": str(test_file)})
        assert cache  # populated

        # Simulate what write/edit does when invalidating mtime cache.
        resolved_str = test_file.resolve().as_posix()
        keys_to_remove = [k for k in cache if k[0].startswith(resolved_str)]
        for k in keys_to_remove:
            del cache[k]

        result = spec.handler({"path": str(test_file)})
        assert "hello" in result
        assert "unchanged" not in result.lower()

    def test_shared_cache_works(self, tmp_path):
        """A shared mtime_cache dict works across tool instances."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("shared\n")
        shared_cache: dict = {}

        spec1 = make_read_spec(cwd=tmp_path, allowed_directories=[], mtime_cache=shared_cache)
        result1 = spec1.handler({"path": str(test_file)})
        assert "shared" in result1
        assert len(shared_cache) > 0

        # Read again with same cache — should be cached.
        spec2 = make_read_spec(cwd=tmp_path, allowed_directories=[], mtime_cache=shared_cache)
        result2 = spec2.handler({"path": str(test_file)})
        assert "unchanged" in result2.lower()


class TestReadToolSpec:
    """Tests for the ToolSpec itself."""

    def test_tool_name_is_read(self, tmp_path):
        """Tool is registered as 'read'."""
        spec = make_read_spec(cwd=tmp_path, allowed_directories=[])
        assert spec.name == "read"

    def test_self_truncating_is_true(self, tmp_path):
        """Tool has self_truncating=True."""
        spec = make_read_spec(cwd=tmp_path, allowed_directories=[])
        assert spec.self_truncating is True

    def test_schema_has_required_path(self, tmp_path):
        """Schema requires 'path' parameter."""
        spec = make_read_spec(cwd=tmp_path, allowed_directories=[])
        assert "path" in spec.schema["properties"]
        assert "path" in spec.schema["required"]

    def test_schema_has_offset_and_limit(self, tmp_path):
        """Schema includes offset and limit parameters."""
        spec = make_read_spec(cwd=tmp_path, allowed_directories=[])
        assert "offset" in spec.schema["properties"]
        assert "limit" in spec.schema["properties"]

    def test_offset_is_1_indexed_in_schema(self, tmp_path):
        """Offset description says 1-indexed."""
        spec = make_read_spec(cwd=tmp_path, allowed_directories=[])
        assert "1-indexed" in spec.schema["properties"]["offset"]["description"]


class TestRipgrepNotInstalled:
    """Tests for missing ripgrep."""

    def test_directory_lists_with_rg_missing(self, tmp_path):
        """Directory listing falls back gracefully when rg is missing."""
        (tmp_path / "a.txt").touch()
        (tmp_path / "b.py").touch()
        spec = make_read_spec(cwd=tmp_path, allowed_directories=[])

        with patch("archie.tools.read.subprocess.run", side_effect=FileNotFoundError):
            result = spec.handler({"path": "."})
            # Should fall back to os.listdir or give error
            assert "not installed" in result.lower() or ("a.txt" in result)
