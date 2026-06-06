"""Tests for project directory detection."""

from archie.project import detect_project_dir


def test_detect_from_nested_dir(tmp_path):
    """Finds project root when cwd is deep inside a project."""
    # Setup: project_root/myproject/src/lib/
    project_root = tmp_path / "dev"
    project_dir = project_root / "myproject"
    nested = project_dir / "src" / "lib"
    nested.mkdir(parents=True)

    result = detect_project_dir(nested, project_root)
    assert result == project_dir


def test_detect_at_project_root_child(tmp_path):
    """Finds project root when cwd IS the direct child."""
    project_root = tmp_path / "dev"
    project_dir = project_root / "myproject"
    project_dir.mkdir(parents=True)

    result = detect_project_dir(project_dir, project_root)
    assert result == project_dir


def test_fallback_when_not_under_project_root(tmp_path):
    """Falls back to cwd when it's not under project_root."""
    project_root = tmp_path / "dev"
    project_root.mkdir(parents=True)
    random_dir = tmp_path / "random" / "place"
    random_dir.mkdir(parents=True)

    result = detect_project_dir(random_dir, project_root)
    assert result == random_dir


def test_fallback_when_project_root_doesnt_exist(tmp_path):
    """Falls back to cwd when project_root doesn't exist on disk."""
    project_root = tmp_path / "nonexistent"
    cwd = tmp_path / "somewhere"
    cwd.mkdir(parents=True)

    result = detect_project_dir(cwd, project_root)
    assert result == cwd


def test_detect_resolves_symlinks(tmp_path):
    """Handles symlinked paths correctly by resolving them."""
    project_root = tmp_path / "dev"
    project_dir = project_root / "myproject" / "src"
    project_dir.mkdir(parents=True)

    # Create a symlink to the nested dir
    link = tmp_path / "link_to_src"
    link.symlink_to(project_dir)

    result = detect_project_dir(link, project_root)
    # Should resolve through the symlink and find the project
    assert result == (project_root / "myproject")


def test_detect_with_relative_paths(tmp_path, monkeypatch):
    """Works correctly even if paths aren't already absolute."""
    project_root = tmp_path / "dev"
    project_dir = project_root / "myproject" / "src"
    project_dir.mkdir(parents=True)

    # Both paths get resolved internally, so relative input is fine
    result = detect_project_dir(project_dir, project_root)
    assert result == (project_root / "myproject")
