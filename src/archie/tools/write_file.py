"""write_file tool — creates or overwrites files with new content.

Use cases:
- Creating new files (code, config, docs, tests)
- Full rewrites of small files where the model generates all content
- For surgical edits to existing files, use edit_file instead

Design choices:
- Writes are always local (project directory only, same allowlist as read_file)
- Parent directories are created automatically (common when scaffolding)
- Binary files are protected from accidental overwrite
- The shared mtime cache is invalidated so subsequent read_file calls
  return the fresh content, not "file unchanged" stubs
"""

from pathlib import Path

from archie.tools import ToolSpec, tool_error, tool_result, validate_path


def make_write_file_spec(
    cwd: Path,
    allowed_directories: list[Path],
    mtime_cache: dict[tuple[str, int, int], tuple[float, str]],
    pre_content_stash: dict[str, str] | None = None,
) -> ToolSpec:
    """Create a write_file ToolSpec bound to path constraints.

    Args:
        cwd: Working directory for resolving relative paths.
        allowed_directories: Additional directories the tool can write to.
        mtime_cache: Shared cache with read_file — invalidated on write.
        pre_content_stash: Shared dict for UI diffs — write stashes original
            content here keyed by tool_use_id before overwriting.
    """

    def handler(params: dict) -> str:
        path_str = params["path"]
        content = params["content"]
        append = bool(params.get("append", False))

        # Security: enforce path allowlist
        try:
            resolved = validate_path(path_str, cwd, allowed_directories)
        except ValueError as e:
            return tool_error(str(e))

        # Refuse to overwrite binary files — they're almost certainly not
        # something the model should be touching (images, compiled code, etc.)
        if resolved.is_file():
            try:
                with resolved.open("rb") as f:
                    if b"\x00" in f.read(8192):
                        return tool_error(f"Refusing to overwrite binary file: {path_str}")
            except OSError:
                pass  # Can't read existing file — proceed, let write fail naturally

            # Stash pre-write content for UI diff generation (overwrites only)
            if pre_content_stash is not None:
                from archie.tools import current_tool_use_id

                tid = current_tool_use_id.get()
                if tid:
                    try:
                        pre_content_stash[tid] = resolved.read_text(
                            encoding="utf-8", errors="replace"
                        )
                    except OSError:
                        pass

        # Create parent dirs (common when scaffolding new packages/modules)
        resolved.parent.mkdir(parents=True, exist_ok=True)

        # Write the file
        try:
            if append and resolved.is_file():
                with resolved.open("a", encoding="utf-8") as f:
                    f.write(content)
            else:
                resolved.write_text(content, encoding="utf-8")
        except OSError as e:
            return tool_error(f"Cannot write file: {e}")

        # Invalidate mtime cache — any read_file entries for this path are now
        # stale. Without this, a read after a write would return "file unchanged".
        resolved_str = str(resolved)
        stale_keys = [k for k in mtime_cache if k[0] == resolved_str]
        for k in stale_keys:
            del mtime_cache[k]

        # Report line count for confirmation
        line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        if append:
            return tool_result(f"Appended: {path_str} (+{line_count} lines)")
        return tool_result(f"Written: {path_str} ({line_count} lines)")

    return ToolSpec(
        name="write_file",
        description=(
            "Create a new file or overwrite an existing file with the provided content. "
            "Use for new files or full rewrites. For surgical edits to existing files, "
            "use edit_file instead. For very large files, write in parts: first call "
            "without append, subsequent calls with append=true."
        ),
        schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to write (relative to working directory or absolute)",
                },
                "content": {
                    "type": "string",
                    "description": "Complete file content to write",
                },
                "append": {
                    "type": "boolean",
                    "description": (
                        "Append to the end of the file instead of overwriting "
                        "(default false). Use to write large files in parts."
                    ),
                },
            },
            "required": ["path", "content"],
        },
        handler=handler,
    )
