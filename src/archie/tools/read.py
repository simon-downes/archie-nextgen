"""read tool — unified file read and directory listing.

Merges ``read_file`` and ``list_files`` into a single tool that auto-detects
whether the path is a file or directory. Reduces tool count, simplifies
the model's decision-making, and provides a more consistent interface.

File reads: line-numbered content with pagination (1-indexed offset), binary
detection, line-length cap (500 chars), mtime dedup cache, char budget.

Directory reads: ripgrep-powered tree-style listing (depth 3, capped at 200
entries), .gitignore-aware, directories before files at each level.

Design decisions:
- 1-indexed offset for file reads matches displayed line numbers — simpler
  UX than the old 0-based scheme. Internally converted to 0-based for slicing.
- ``rg --files`` for directory listing respects ``.gitignore``, nested ignore
  files, and binary detection without reimplementing in Python.
- Depth 3 cap shows enough structure without drilling into subdirectories
  one at a time. The 200-entry cap kicks in for large projects.
- Drop glob parameter — the ``glob`` tool handles pattern-based discovery.
"""

import logging
import subprocess
from pathlib import Path

from archie.tools import ToolSpec, current_tool_use_id, tool_error, tool_result, validate_path

log = logging.getLogger(__name__)


class _TreeNode:
    """Node in the directory tree structure."""

    __slots__ = ("children", "is_file")

    def __init__(self) -> None:
        self.children: dict[str, _TreeNode] = {}
        self.is_file = False  # True if this node is a file (leaf)


# Maximum characters per line before truncation.
_LINE_LENGTH_CAP = 500
_CHAR_BUDGET = 32000  # ~500-800 lines of typical code

# Directory listing limits.
_MAX_DEPTH = 3
_MAX_ENTRIES = 200


def make_read_spec(
    cwd: Path, allowed_directories: list[Path], mtime_cache: dict | None = None
) -> ToolSpec:
    """Create a read ToolSpec bound to the given path constraints.

    Uses a closure pattern: the handler captures ``cwd``, ``allowed_directories``,
    and ``mtime_cache`` at registration time.

    Args:
        cwd: Working directory for resolving relative paths.
        allowed_directories: Additional directories the tool can access.
        mtime_cache: Optional shared cache dict for file-read mtime dedup.
            If None, creates a private one. Directory reads do not populate it.
    """
    _mtime_cache: dict[tuple[str, int, int], tuple[float, str]] = (
        mtime_cache if mtime_cache is not None else {}
    )

    def handler(params: dict) -> str:
        """Read a file or list a directory."""
        path_str = params.get("path", "")
        offset_raw = params.get("offset", 1)  # 1-indexed per schema
        limit_raw = params.get("limit", None)

        # --- Security: enforce path allowlist ---
        try:
            resolved = validate_path(path_str, cwd, allowed_directories)
        except ValueError as e:
            return tool_error(str(e))

        if resolved.is_dir():
            return _handle_directory(resolved, path_str, cwd)
        elif resolved.is_file():
            # Convert 1-indexed offset to 0-based internally.
            offset = max(offset_raw - 1, 0)
            return _handle_file(resolved, path_str, offset, limit_raw, offset_raw, _mtime_cache)
        else:
            return tool_error(f"Path does not exist: {path_str}")

    return ToolSpec(
        name="read",
        description=(
            "Read a file or list a directory. Files return content with line numbers (1-indexed).\n\n"
            "- Use `code` first to locate relevant line ranges, then read with offset/limit.\n"
            "- **Always include offset and limit** for files >200 lines.\n"
            "- Do not reread the same file range twice.\n"
            "- Use truncation hints to continue reading with the correct offset.\n"
            "- Prefer `grep` to locate content instead of scanning full files."
        ),
        schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File or directory path (relative to working directory or absolute)",
                },
                "offset": {
                    "type": "integer",
                    "description": "Start line, 1-indexed (files only, default: 1)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum lines to return (files only, default: entire file)",
                },
            },
            "required": ["path"],
        },
        handler=handler,
        self_truncating=True,
    )


# ---------------------------------------------------------------------------
# File read logic
# ---------------------------------------------------------------------------


def _handle_file(
    resolved: Path,
    path_str: str,
    offset: int,  # 0-based internal
    limit: int | None,
    offset_1indexed: int,
    mtime_cache: dict,
) -> str:
    """Read a file and return line-numbered content."""

    # --- Mtime dedup ---
    resolved_str = str(resolved)
    cache_key = (resolved_str, offset, limit)
    try:
        current_mtime = resolved.stat().st_mtime
        cached = mtime_cache.get(cache_key)
        if cached is not None and cached[0] == current_mtime:
            cached_id = cached[1]
            hint = (
                f" Content is in context at tool result {cached_id}; if it has been"
                f' evicted, use retrieve_artifact with tool_use_id="{cached_id}".'
                if cached_id
                else ""
            )
            return tool_result(
                "File unchanged since last read: "
                f"{path_str} (offset={offset_1indexed}, limit={limit}).{hint}"
            )
    except OSError:
        pass

    # --- Binary detection ---
    try:
        with resolved.open("rb") as f:
            chunk = f.read(8192)
        if b"\x00" in chunk:
            return tool_error(f"Binary file detected: {path_str}")
    except OSError as e:
        return tool_error(f"Cannot read file: {e}")

    # --- Read the file ---
    try:
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return tool_error(f"Cannot read file: {e}")

    lines = text.splitlines()
    total_lines = len(lines)

    # --- Apply pagination (offset is 0-based internally) ---
    if limit is not None:
        selected = lines[offset : offset + limit]
        truncated = (offset + limit) < total_lines
    else:
        selected = lines[offset:]
        truncated = False

    # --- Format with line numbers, respecting char budget ---
    char_budget = _CHAR_BUDGET
    numbered: list[str] = []
    chars_used = 0
    budget_hit = False
    for i, line in enumerate(selected, start=offset + 1):
        if len(line) > _LINE_LENGTH_CAP:
            line = line[:_LINE_LENGTH_CAP] + "...[truncated]"
        formatted = f"{i:>5}|{line}\n"
        if chars_used + len(formatted) > char_budget:
            budget_hit = True
            break
        numbered.append(formatted.rstrip("\n"))
        chars_used += len(formatted)

    lines_shown = len(numbered)
    actual_end_offset = offset + lines_shown

    # --- Build output with metadata header ---
    display_offset = offset + 1  # 1-indexed for display
    header = f"File: {path_str} ({total_lines} lines)"
    if budget_hit or truncated:
        showing = f"Showing lines {display_offset}-{actual_end_offset} of {total_lines}"
        hint_next = actual_end_offset + 1  # next 1-indexed offset
        header += f"\n{showing}\nUse offset={hint_next} to continue reading"

    content = header + "\n\n" + "\n".join(numbered)

    # --- Update mtime cache on successful read ---
    try:
        mtime_cache[cache_key] = (resolved.stat().st_mtime, current_tool_use_id.get())
    except OSError:
        pass

    return tool_result(content)


# ---------------------------------------------------------------------------
# Directory listing logic
# ---------------------------------------------------------------------------


def _handle_directory(resolved: Path, path_str: str, cwd: Path) -> str:
    """List a directory using rg --files with tree-style formatting."""

    # Run ripgrep to get all files (respects .gitignore).
    cmd = ["rg", "--files", "--sort=path"]
    cmd.append(str(resolved))

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        return tool_error("ripgrep (rg) is not installed")
    except subprocess.TimeoutExpired:
        return tool_error("Listing timed out after 15 seconds")

    if result.returncode not in (0, 1):
        return tool_error(f"ripgrep error: {result.stderr.strip()}")

    all_files_str = [f for f in result.stdout.strip().split("\n") if f]

    if not all_files_str:
        # Empty directory — fall back to os.listdir.
        try:
            entries = sorted(resolved.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            if not entries:
                return tool_result(f"{_shorten_path(path_str, cwd)}\n(empty directory)")
            lines: list[str] = []
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                name = entry.name
                suffix = "/" if entry.is_dir() else ""
                indent = "  " + "  "
                lines.append(f"{indent}{name}{suffix}")
            header = _shorten_path(path_str, cwd)
            return tool_result(header + "\n\n" + "\n".join(lines))
        except OSError as e:
            return tool_error(f"Cannot list directory: {e}")

    # Resolve all files relative to the target directory for depth calculation.
    resolved_base = resolved.resolve()
    rel_entries: list[tuple[Path, str]] = []
    for fp_str in all_files_str:
        fp = Path(fp_str)
        try:
            rp = fp.resolve().relative_to(resolved_base)
        except ValueError:
            rp = Path(fp_str)
        rel_entries.append((rp, str(rp)))

    # Filter to entries within max depth (count path components relative to target).
    filtered: list[tuple[Path, str]] = [
        (rp, name) for rp, name in rel_entries if len(rp.parts) <= _MAX_DEPTH
    ]

    total_count = len(filtered)
    capped = total_count > _MAX_ENTRIES
    display_entries = filtered[:_MAX_ENTRIES]

    # Build tree from flat entries.
    tree_lines = _format_tree_from_files(display_entries)

    # Summary: count files and unique directories
    file_count = len(display_entries)
    dir_set: set[tuple[str, ...]] = set()
    for rp, _ in display_entries:
        for i in range(len(rp.parts) - 1):
            dir_set.add(rp.parts[: i + 1])

    header = f"{_shorten_path(path_str, cwd)}/ ({file_count} files, {len(dir_set)} dirs)"
    output_lines = [header, ""] + tree_lines

    if capped:
        output_lines.append(
            f"{_MAX_ENTRIES} entries shown of {total_count}. Narrow the path for more."
        )

    return tool_result("\n".join(output_lines))


def _shorten_path(path_str: str, cwd: Path) -> str:
    """Shorten a path to be relative to cwd."""
    try:
        rel = Path(path_str).resolve().relative_to(cwd.resolve())
        return str(rel) if str(rel) != "." else "."
    except ValueError:
        return path_str


def _format_tree_from_files(entries: list[tuple[Path, str]]) -> list[str]:
    """Format flat file entries as indented directory listing."""
    if not entries:
        return []

    root: _TreeNode = _TreeNode()

    for rel_path, _display_name in entries:
        parts = list(rel_path.parts)
        current = root
        for i, part in enumerate(parts):
            if part not in current.children:
                current.children[part] = _TreeNode()
            is_last = i == len(parts) - 1
            node = current.children[part]
            if is_last:
                node.is_file = True
            current = node

    lines: list[str] = []
    _tree_print(root, "", lines)
    return lines


def _tree_print(node: _TreeNode, prefix: str, lines: list[str]) -> None:
    """Recursively print tree nodes with indentation."""
    if not node.children:
        return

    # Separate directories and files at this level.
    entries = list(node.children.items())
    dirs = [(name, child) for name, child in entries if not child.is_file]
    files = [(name, child) for name, child in entries if child.is_file]

    # Sort both groups alphabetically (case-insensitive).
    dirs.sort(key=lambda x: x[0].lower())
    files.sort(key=lambda x: x[0].lower())

    # Directories first, then files
    for name, child in dirs:
        lines.append(f"{prefix}{name}/")
        _tree_print(child, prefix + "  ", lines)
    for name, _child in files:
        lines.append(f"{prefix}{name}")
