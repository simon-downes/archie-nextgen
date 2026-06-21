"""Tests for enriched Python extractor."""

import pytest

from archie.code_intel import CodeIndex


@pytest.fixture
def index(tmp_path):
    """Create a CodeIndex for testing."""
    return CodeIndex(tmp_path)


def test_python_imports(index, tmp_path):
    """Test that imports are captured as a single 'imports' symbol."""
    (tmp_path / "test.py").write_text(
        "import os\nfrom pathlib import Path\nimport sys\n\ndef foo():\n    pass\n"
    )

    symbols = index.outline(tmp_path / "test.py")

    # Find imports symbol
    imports = next((s for s in symbols if s.kind == "imports"), None)
    assert imports is not None
    assert imports.signature == "imports: [line 1-3]"
    assert len(imports.children) == 3


def test_python_constants(index, tmp_path):
    """Test that module-level constants are captured."""
    (tmp_path / "test.py").write_text(
        "PI = 3.14\nMAX_RETRIES: int = 3\n\nx = 1  # not a constant\n\ndef foo():\n    pass\n"
    )

    symbols = index.outline(tmp_path / "test.py")
    constant_names = [s.name for s in symbols if s.kind == "constant"]

    assert "PI" in constant_names
    assert "MAX_RETRIES" in constant_names
    assert "x" not in constant_names


def test_python_class_fields(index, tmp_path):
    """Test that class fields with type annotations are captured."""
    (tmp_path / "test.py").write_text(
        "class MyClass:\n"
        "    x: int = 1\n"
        '    y = "hello"  # no type annotation, not a field\n'
        "\n"
        "    def method(self):\n"
        "        pass\n"
    )

    symbols = index.outline(tmp_path / "test.py")
    cls = next((s for s in symbols if s.kind == "class"), None)
    assert cls is not None

    field_names = [s.name for s in cls.children if s.kind == "field"]
    assert "x" in field_names
    assert "y" not in field_names

    # Check field has type annotation in signature
    field_x = next(s for s in cls.children if s.name == "x")
    assert ": int" in field_x.signature


def test_python_decorators(index, tmp_path):
    """Test that decorators are included in function signature."""
    (tmp_path / "test.py").write_text('@route\n@cache\ndef my_handler(request):\n    return "ok"\n')

    symbols = index.outline(tmp_path / "test.py")
    func = next((s for s in symbols if s.kind == "function"), None)
    assert func is not None

    assert "@route" in func.signature
    assert "@cache" in func.signature
    assert "def my_handler" in func.signature


def test_python_end_line(index, tmp_path):
    """Test that symbols have accurate end_line."""
    (tmp_path / "test.py").write_text(
        "def simple():\n    pass\n\ndef multi():\n    x = 1\n    y = 2\n    return x + y\n"
    )

    symbols = index.outline(tmp_path / "test.py")

    simple = next(s for s in symbols if s.name == "simple")
    assert simple.line == 1
    assert simple.end_line == 2

    multi = next(s for s in symbols if s.name == "multi")
    assert multi.line == 4
    assert multi.end_line == 7
