"""read_file tool — reads file contents with line numbers.

This is one of the most-used tools. It reads files natively in Python
(not via shell commands like cat/head) because:
- We get proper typed exceptions (FileNotFoundError, PermissionError)
- No subprocess overhead or shell escaping concerns
- Full control over offset/limit/truncation in a single pass
- Binary detection is trivial in Python

Features:
- Line-numbered output (e.g. "   42|content") for easy reference
- Pagination via offset/limit params (default limit=500 lines)
- Binary file detection (null bytes in first 8KB)
- Line-length cap (500 chars per line) to prevent context bloat
- Pagination hint when truncated ("Use offset=N to continue reading")
- Path validation via allowlist (security)

The line numbers are critical — they let the model reference specific code
locations without counting lines manually. The format "   42|content" uses
a right-aligned 5-char number with a pipe separator, matching common editor
conventions.
"""

from pathlib import Path

from archie.tools import ToolSpec, tool_error, tool_result, validate_path

# Maximum characters per line before truncation.
# Lines longer than this are usually minified code, data, or generated content
# that isn't useful to read in full. We truncate to save context budget.
_LINE_LENGTH_CAP = 500

# Default number of lines to return per call.
# 500 lines is roughly 15-25KB of typical code — enough to understand a module
# but not so much that we blow the context budget on a single read.
_DEFAULT_LIMIT = 500


def make_read_file_spec(
    cwd: Path, allowed_directories: list[Path], mtime_cache: dict | None = None
) -> ToolSpec:
    """Create a read_file ToolSpec bound to the given path constraints.

    Uses a closure pattern: the handler captures `cwd` and `allowed_directories`
    at registration time. This avoids needing to pass config through the tool
    dispatch system. The mtime cache also lives here as a closure variable —
    it's tool-specific state that doesn't belong in the agent loop.

    Args:
        cwd: Working directory for resolving relative paths.
        allowed_directories: Additional directories the tool can access.
        mtime_cache: Optional shared cache dict for mtime dedup. If None,
            creates a private one. Shared cache allows write tools to
            invalidate entries when they modify files.
    """
    # Mtime dedup cache: (resolved_path, offset, limit) → mtime
    # If the file hasn't changed since last read with same params, return a stub.
    # When shared with write tools, they can invalidate entries on file modification.
    _mtime_cache: dict[tuple[str, int, int], float] = mtime_cache if mtime_cache is not None else {}

    def handler(params: dict) -> str:
        """Read a file and return line-numbered content.

        The response includes a metadata header (filename, total lines, pagination
        hint if truncated) followed by the numbered content. This gives the model
        all the context it needs to request more or different sections.
        """
        path_str = params.get("path", "")
        offset = params.get("offset", 0)
        limit = params.get("limit", _DEFAULT_LIMIT)

        # --- Security: enforce path allowlist ---
        try:
            resolved = validate_path(path_str, cwd, allowed_directories)
        except ValueError as e:
            return tool_error(str(e))

        if not resolved.is_file():
            return tool_error(f"Not a file: {path_str}")

        # --- Mtime dedup ---
        # If the same file region was read before and hasn't changed, return
        # a stub to avoid re-sending content the model already has in context.
        resolved_str = str(resolved)
        cache_key = (resolved_str, offset, limit)
        try:
            current_mtime = resolved.stat().st_mtime
            if cache_key in _mtime_cache and _mtime_cache[cache_key] == current_mtime:
                return tool_result(
                    f"File unchanged since last read: {path_str} (offset={offset}, limit={limit})"
                )
        except OSError:
            pass  # Can't stat — proceed with the read, let it fail naturally

        # --- Binary detection ---
        # Check the first 8KB for null bytes. Binary files (images, compiled
        # code, etc.) produce garbage when read as text and waste context.
        try:
            with resolved.open("rb") as f:
                chunk = f.read(8192)
            if b"\x00" in chunk:
                return tool_error(f"Binary file detected: {path_str}")
        except OSError as e:
            return tool_error(f"Cannot read file: {e}")

        # --- Read the file ---
        # errors="replace" handles non-UTF8 bytes gracefully (replaces with ?)
        # rather than crashing on files with mixed encodings.
        try:
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return tool_error(f"Cannot read file: {e}")

        lines = text.splitlines()
        total_lines = len(lines)

        # --- Apply pagination ---
        # offset is 0-based (line 0 = first line of file).
        # The model can request arbitrary windows into the file.
        selected = lines[offset : offset + limit]
        truncated = (offset + limit) < total_lines

        # --- Format with line numbers + length cap ---
        numbered = []
        for i, line in enumerate(selected, start=offset + 1):
            # Cap long lines to save context. These are usually minified JS,
            # CSV data, or generated content that's not useful in full.
            if len(line) > _LINE_LENGTH_CAP:
                line = line[:_LINE_LENGTH_CAP] + "...[truncated]"
            # Right-align line number in 5 chars: "    1|", "   42|", "  999|"
            numbered.append(f"{i:>5}|{line}")

        # --- Build output with metadata header ---
        # The header gives the model context about the file and how to navigate it.
        header = f"File: {path_str} ({total_lines} lines)"
        if truncated:
            showing = f"Showing lines {offset + 1}-{offset + len(selected)} of {total_lines}"
            hint = f"Use offset={offset + limit} to continue reading"
            header += f"\n{showing}\n{hint}"

        content = header + "\n\n" + "\n".join(numbered)

        # --- Update mtime cache on successful read ---
        try:
            _mtime_cache[cache_key] = resolved.stat().st_mtime
        except OSError:
            pass

        return tool_result(content)

    return ToolSpec(
        name="read_file",
        description=(
            "Read a file's contents with line numbers. Supports pagination via offset/limit. "
            "Use limit=5-10 to check file purpose/structure, full read for editing. "
            "Prefer search_files to find specific content rather than reading entire files."
        ),
        schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file (relative to working directory or absolute)",
                },
                "offset": {
                    "type": "integer",
                    "description": "Line offset to start reading from (0-based, default 0)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of lines to return (default 500)",
                },
            },
            "required": ["path"],
        },
        handler=handler,
    )
