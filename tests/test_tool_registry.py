"""Tests for tool registry, path validation, truncation, and framework behaviour."""

from unittest.mock import MagicMock

import pytest

from archie.tools import ToolRegistry, ToolSpec, truncate_result, validate_path


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


class TestSelfTruncating:
    """Tests for self_truncating ToolSpec behaviour in the agent loop."""

    def test_self_truncating_tool_skips_truncation(self, tmp_path):
        """A self_truncating tool's result >4KB is not clipped by the agent."""
        from archie.agent import AgentLoop
        from archie.llm.bedrock import Done, ToolUseEvent, Usage
        from archie.models import ModelInfo
        from archie.session import Session
        from archie.types import ToolResultBlock

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
        llm.model_id = "test-model"
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

        results = [b for t in session.turns for b in t.content if isinstance(b, ToolResultBlock)]
        assert len(results[0].content) == 6000

    def test_default_tool_still_truncated(self, tmp_path):
        """A normal tool's result >4KB is truncated."""
        from archie.agent import AgentLoop
        from archie.llm.bedrock import Done, ToolUseEvent, Usage
        from archie.models import ModelInfo
        from archie.session import Session
        from archie.types import ToolResultBlock

        big_output = "x" * 6000

        reg = ToolRegistry()
        reg.register(ToolSpec("normal", "normal tool", {"type": "object"}, lambda p: big_output))

        model = ModelInfo(
            name="T", max_context_tokens=100_000, input_price_per_m=1.0, output_price_per_m=1.0
        )
        session = Session(model_id="test", model_info=model)
        session._log_path = tmp_path / "test.jsonl"

        llm = MagicMock()
        llm.model_id = "test-model"
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

        results = [b for t in session.turns for b in t.content if isinstance(b, ToolResultBlock)]
        assert len(results[0].content) < 6000
        assert "truncated" in results[0].content
