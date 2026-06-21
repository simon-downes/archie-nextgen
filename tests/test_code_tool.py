"""Tests for the unified code tool interface."""

import tempfile
from pathlib import Path

import pytest

from archie.tools import create_default_registry


@pytest.fixture
def tmp_project():
    """Create a temporary test project."""
    with tempfile.TemporaryDirectory() as tmpdir:
        p = Path(tmpdir)

        # Create a simple Python file
        (p / "main.py").write_text('import os\n\ndef hello():\n    return "world"\n')

        # Create a larger file (>200 lines)
        large_content = "\n".join(f"# Line {i}\n" for i in range(250))
        (p / "large.py").write_text(large_content)

        # Create a subdirectory with a file
        subdir = p / "subdir"
        subdir.mkdir()
        (subdir / "utils.py").write_text("def helper(): pass\n")

        yield p


def test_code_tool_file_mode(tmp_project):
    """Test file mode returns outline for files >200 lines."""
    registry = create_default_registry(tmp_project, [])
    code_tool = registry.get("code")

    # Call the handler directly with a file path
    result = code_tool.handler({"path": "main.py"})
    assert "def hello" in result
    assert "import os" in result


def test_code_tool_directory_mode(tmp_project):
    """Test directory mode returns recursive outline."""
    registry = create_default_registry(tmp_project, [])
    code_tool = registry.get("code")

    # Call the handler directly with a directory path
    result = code_tool.handler({"path": "subdir"})
    assert "def helper" in result


def test_code_tool_search_mode(tmp_project):
    """Test search mode finds symbols by name."""
    registry = create_default_registry(tmp_project, [])
    code_tool = registry.get("code")

    # Search for function name
    result = code_tool.handler({"name": "hello"})
    assert "main.py" in result
    assert "def hello" in result


def test_code_tool_no_path_default_root(tmp_project):
    """Test that no path defaults to project root."""
    registry = create_default_registry(tmp_project, [])
    code_tool = registry.get("code")

    # Call with no arguments
    result = code_tool.handler({})
    assert "main.py" in result
    assert "def hello" in result
