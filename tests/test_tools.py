"""Tests for tool registry and file tools."""

from unittest.mock import patch

import pytest

from archie.tools import ToolRegistry, ToolSpec, truncate_result, validate_path
from archie.tools.read import make_read_spec
from archie.tools.search_files import make_search_files_spec

# --- validate_path tests ---


class TestValidatePath:
    def test_allows_file_under_cwd(self, tmp_path):
        """Files under cwd are allowed."""
        f = tmp_path / "test.py"
        f.touch()
        result = validate_path("test.py", tmp_path, [])
        assert result == f

    def test_allows_file_under_allowed_dir(self, tmp_path):
        """Files under explicitly allowed directories are allowed."""
        other = tmp_path / "other"
        other.mkdir()
        f = other / "file.txt"
        f.touch()
        result = validate_path(str(f), tmp_path, [other])
        assert result == f

    def test_rejects_file_outside_allowed(self, tmp_path):
        """Files outside all allowed directories raise ValueError."""
        with pytest.raises(ValueError, match="outside allowed directories"):
            validate_path("/etc/passwd", tmp_path, [])

    def test_resolves_relative_paths(self, tmp_path):
        """Relative paths are resolved relative to cwd."""
        sub = tmp_path / "src"
        sub.mkdir()
        f = sub / "main.py"
        f.touch()
        result = validate_path("src/main.py", tmp_path, [])
        assert result == f

    def test_blocks_symlink_escape(self, tmp_path):
        """Symlinks that resolve outside allowed dirs are blocked."""
        link = tmp_path / "sneaky"
        link.symlink_to("/etc")
        with pytest.raises(ValueError, match="outside allowed directories"):
            validate_path("sneaky/passwd", tmp_path, [])


# --- truncate_result tests ---


class TestTruncateResult:
    def test_short_content_unchanged(self):
        assert truncate_result("hello", max_chars=100) == "hello"

    def test_long_content_truncated(self):
        content = "x" * 5000
        result = truncate_result(content, max_chars=100)
        assert len(result) < 200
        assert "[...truncated, 5000 chars total]" in result

    def test_default_limit_is_4000(self):
        content = "x" * 4001
        result = truncate_result(content)
        assert "[...truncated" in result


# --- ToolRegistry tests ---


class TestToolRegistry:
    def test_register_and_get(self):
        registry = ToolRegistry()
        spec = ToolSpec(name="test", description="A test", schema={}, handler=lambda p: "ok")
        registry.register(spec)
        assert registry.get("test") is spec

    def test_get_unknown_returns_none(self):
        registry = ToolRegistry()
        assert registry.get("unknown") is None

    def test_duplicate_name_raises(self):
        registry = ToolRegistry()
        spec = ToolSpec(name="test", description="A test", schema={}, handler=lambda p: "ok")
        registry.register(spec)
        with pytest.raises(ValueError, match="already registered"):
            registry.register(spec)

    def test_to_tool_config_format(self):
        registry = ToolRegistry()
        spec = ToolSpec(
            name="my_tool",
            description="Does stuff",
            schema={"type": "object", "properties": {}},
            handler=lambda p: "ok",
        )
        registry.register(spec)
        config = registry.to_tool_config()
        assert len(config) == 1
        assert config[0]["toolSpec"]["name"] == "my_tool"
        assert config[0]["toolSpec"]["description"] == "Does stuff"
        assert config[0]["toolSpec"]["inputSchema"] == {
            "json": {"type": "object", "properties": {}}
        }


# --- read tool tests ---


class TestReadFile:
    @pytest.fixture
    def tool(self, tmp_path):
        """Create a read tool bound to tmp_path."""
        return make_read_spec(tmp_path, [])

    def test_reads_file_with_line_numbers(self, tmp_path, tool):
        """Output includes line numbers in '  N|content' format."""
        f = tmp_path / "test.py"
        f.write_text("line1\nline2\nline3\n")
        result = tool.handler({"path": "test.py"})
        assert "    1|line1" in result
        assert "    2|line2" in result
        assert "    3|line3" in result

    def test_reads_with_offset_and_limit(self, tmp_path, tool):
        """Pagination via offset/limit works correctly (1-indexed)."""
        f = tmp_path / "big.txt"
        f.write_text("\n".join(f"line{i}" for i in range(1, 101)))
        result = tool.handler({"path": "big.txt", "offset": 10, "limit": 5})
        assert "   10|line10" in result
        assert "   14|line14" in result
        assert "line15" not in result
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
        """Non-existent file returns error."""
        result = tool.handler({"path": "nope.txt"})
        assert "Error:" in result
        assert "does not exist" in result

    def test_pagination_hint_when_truncated(self, tmp_path, tool):
        """Shows pagination hint when there are more lines."""
        f = tmp_path / "big.txt"
        f.write_text("\n".join(f"line{i}" for i in range(600)))
        result = tool.handler({"path": "big.txt", "limit": 10})
        assert "Use offset=11 to continue reading" in result
        assert "Showing lines 1-10 of 600" in result


# --- search_files tool tests ---


class TestSearchFiles:
    @pytest.fixture
    def tool(self, tmp_path):
        """Create a search_files tool bound to tmp_path."""
        return make_search_files_spec(tmp_path, [])

    def test_finds_matches(self, tmp_path, tool):
        """Finds matching lines in files."""
        f = tmp_path / "test.py"
        f.write_text("def hello():\n    pass\n\ndef world():\n    pass\n")
        result = tool.handler({"pattern": "def", "path": "."})
        assert "def hello" in result
        assert "def world" in result

    def test_respects_glob_filter(self, tmp_path, tool):
        """Glob filter restricts search to matching files."""
        (tmp_path / "a.py").write_text("target\n")
        (tmp_path / "b.txt").write_text("target\n")
        result = tool.handler({"pattern": "target", "path": ".", "glob": "*.py"})
        assert "a.py" in result
        assert "b.txt" not in result

    def test_no_matches_message(self, tmp_path, tool):
        """Returns a clean message when no matches found."""
        (tmp_path / "test.py").write_text("nothing here\n")
        result = tool.handler({"pattern": "nonexistent_xyz", "path": "."})
        assert "No matches found" in result

    def test_rejects_path_outside_allowed(self, tmp_path, tool):
        """Search paths outside allowed directories return error."""
        result = tool.handler({"pattern": "test", "path": "/etc"})
        assert "Error:" in result
        assert "outside allowed directories" in result

    def test_empty_pattern_returns_error(self, tmp_path, tool):
        """Empty pattern returns an error."""
        result = tool.handler({"pattern": ""})
        assert "Error:" in result

    def test_pagination_cap(self, tmp_path, tool):
        """Results are capped and show pagination hint when truncated."""
        # Create a file with many matches
        content = "\n".join(f"match_{i}" for i in range(100))
        (tmp_path / "many.txt").write_text(content)
        result = tool.handler({"pattern": "match_", "path": ".", "limit": 5})
        assert "Use offset=" in result

    def test_rg_not_installed(self, tmp_path, tool):
        """Handles missing ripgrep gracefully."""
        with patch("archie.tools.search_files.subprocess.run", side_effect=FileNotFoundError):
            result = tool.handler({"pattern": "test", "path": "."})
        assert "Error:" in result
        assert "not installed" in result


class TestReadFileMtimeDedup:
    """Tests for read_file mtime-based deduplication."""

    def test_returns_stub_on_unchanged_file(self, tmp_path):
        """Second read of same file with same params returns stub."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello\nworld\n")

        from archie.tools.read import make_read_spec

        spec = make_read_spec(cwd=tmp_path, allowed_directories=[])

        # First read — returns content
        result1 = spec.handler({"path": str(test_file), "offset": 0, "limit": 500})
        assert "hello" in result1

        # Second read — same file, same params → stub
        result2 = spec.handler({"path": str(test_file), "offset": 0, "limit": 500})
        assert "unchanged" in result2.lower()

    def test_rereads_when_mtime_changes(self, tmp_path):
        """File is re-read when content changes between calls."""
        import time

        test_file = tmp_path / "test.txt"
        test_file.write_text("version1\n")

        from archie.tools.read import make_read_spec

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

        from archie.tools.read import make_read_spec

        spec = make_read_spec(cwd=tmp_path, allowed_directories=[])

        # Read with offset=1
        spec.handler({"path": str(test_file), "offset": 1, "limit": 500})

        # Read with offset=2 — different params, should return content
        result = spec.handler({"path": str(test_file), "offset": 2, "limit": 500})
        assert "unchanged" not in result.lower()
        assert "line2" in result

    def test_stub_references_originating_tool_use_id(self, tmp_path):
        """The unchanged-stub names the tool result that holds the content."""
        from archie.tools import current_tool_use_id
        from archie.tools.read import make_read_spec

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
        """Removing the cache entry (as eviction does) makes the next read return content."""
        from archie.tools.read import make_read_spec

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


class TestReadFileBudget:
    """Tests for char-budget pagination accuracy."""

    def setup_method(self, tmp_path=None):
        pass

    def test_budget_hit_gives_accurate_offset_hint(self, tmp_path):
        """Following the pagination hint produces contiguous lines with no gap."""
        from archie.tools.read import make_read_spec

        # Create a file large enough to exceed the 32KB budget
        lines = [f"line {i}: {'x' * 80}" for i in range(600)]
        big_file = tmp_path / "big.py"
        big_file.write_text("\n".join(lines))

        spec = make_read_spec(tmp_path, [])

        # First read
        result1 = spec.handler({"path": "big.py"})
        assert "Use offset=" in result1
        # Extract the suggested offset
        offset_line = [x for x in result1.split("\n") if "Use offset=" in x][0]
        next_offset = int(offset_line.split("offset=")[1].split(" ")[0])

        # Second read following the hint
        result2 = spec.handler({"path": "big.py", "offset": next_offset})

        # First line of result2 should be exactly next_offset (1-indexed)
        content_lines = [x for x in result2.split("\n") if "|" in x]
        first_line_num = int(content_lines[0].split("|")[0].strip())
        assert first_line_num == next_offset

    def test_small_file_no_pagination(self, tmp_path):
        """A small file fits entirely within budget — no pagination hint."""
        from archie.tools.read import make_read_spec

        small_file = tmp_path / "small.py"
        small_file.write_text("hello\nworld\n")

        spec = make_read_spec(tmp_path, [])
        result = spec.handler({"path": "small.py"})
        assert "Use offset=" not in result
        assert "2 lines" in result


class TestSelfTruncating:
    """Tests for self_truncating ToolSpec behaviour in the agent loop."""

    def test_self_truncating_tool_skips_truncation(self, tmp_path):
        """A self_truncating tool's result >4KB is not clipped by the agent."""
        from unittest.mock import MagicMock

        from archie.agent import AgentLoop
        from archie.llm.bedrock import Done, ToolUseEvent, Usage
        from archie.models import ModelInfo
        from archie.session import Session
        from archie.tools import ToolRegistry, ToolSpec

        big_output = "x" * 6000  # > 4KB default cap

        reg = ToolRegistry()
        reg.register(
            ToolSpec(
                "big", "big tool", {"type": "object"}, lambda p: big_output, self_truncating=True
            )
        )

        model = ModelInfo(
            name="T", max_context_tokens=100_000, input_price_per_m=1.0, output_price_per_m=1.0
        )
        session = Session(model_id="test", model_info=model)
        session._log_path = tmp_path / "test.jsonl"

        llm = MagicMock()
        llm.stream = MagicMock(
            side_effect=[
                iter(
                    [
                        ToolUseEvent(tool_use_id="t1", name="big", input={}),
                        Usage(input_tokens=10, output_tokens=5),
                        Done(stop_reason="tool_use"),
                    ]
                ),
                iter([Done(stop_reason="end_turn")]),
            ]
        )

        events = []
        agent = AgentLoop(llm, session, reg, "system", events.append)
        agent.run_turn("go")

        # Find the tool result in session — should be full 6000 chars, not truncated
        from archie.types import ToolResultBlock

        results = [b for t in session.turns for b in t.content if isinstance(b, ToolResultBlock)]
        assert len(results[0].content) == 6000

    def test_default_tool_still_truncated(self, tmp_path):
        """A normal tool's result >4KB is truncated."""
        from unittest.mock import MagicMock

        from archie.agent import AgentLoop
        from archie.llm.bedrock import Done, ToolUseEvent, Usage
        from archie.models import ModelInfo
        from archie.session import Session
        from archie.tools import ToolRegistry, ToolSpec

        big_output = "x" * 6000

        reg = ToolRegistry()
        reg.register(ToolSpec("normal", "normal tool", {"type": "object"}, lambda p: big_output))

        model = ModelInfo(
            name="T", max_context_tokens=100_000, input_price_per_m=1.0, output_price_per_m=1.0
        )
        session = Session(model_id="test", model_info=model)
        session._log_path = tmp_path / "test.jsonl"

        llm = MagicMock()
        llm.stream = MagicMock(
            side_effect=[
                iter(
                    [
                        ToolUseEvent(tool_use_id="t1", name="normal", input={}),
                        Usage(input_tokens=10, output_tokens=5),
                        Done(stop_reason="tool_use"),
                    ]
                ),
                iter([Done(stop_reason="end_turn")]),
            ]
        )

        events = []
        agent = AgentLoop(llm, session, reg, "system", events.append)
        agent.run_turn("go")

        from archie.types import ToolResultBlock

        results = [b for t in session.turns for b in t.content if isinstance(b, ToolResultBlock)]
        assert len(results[0].content) < 6000
        assert "truncated" in results[0].content
