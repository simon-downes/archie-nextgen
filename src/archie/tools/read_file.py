"""read_file tool — reads file contents with line numbers.

This is one of the most-used tools. It reads files natively in Python
(not via shell commands like cat/head) because:
- We get proper typed exceptions (FileNotFoundError, PermissionError)
- No subprocess overhead or shell escaping concerns
- Full control over offset/limit/truncation in a single pass
- Binary detection is trivial in Python

Features:
- Line-numbered output (e.g. "   42|content") for easy reference
- Pagination via offset/limit params (reads entire file by default)
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

from archie.tools import ToolSpec, current_tool_use_id, tool_error, tool_result, validate_path

# Maximum characters per line before truncation.
# Lines longer than this are usually minified code, data, or generated content
# that isn't useful to read in full. We truncate to save context budget.
_LINE_LENGTH_CAP = 500
_CHAR_BUDGET = 32000  # ~500-800 lines of typical code


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
            invalidate entries when they modify files, and the agent loop to
            invalidate entries when the cached content is evicted from context.
    """
    # Mtime dedup cache: (resolved_path, offset, limit) → (mtime, tool_use_id)
    # If the file hasn't changed since last read with same params, return a stub
    # pointing at the tool result that already holds the content. The tool_use_id
    # lets the stub reference that result (and retrieve_artifact recover it), and
    # lets the agent loop invalidate entries when that result is evicted.
    _mtime_cache: dict[tuple[str, int, int], tuple[float, str]] = (
        mtime_cache if mtime_cache is not None else {}
    )

    def handler(params: dict) -> str:
        """Read a file and return line-numbered content.

        The response includes a metadata header (filename, total lines, pagination
        hint if truncated) followed by the numbered content. This gives the model
        all the context it needs to request more or different sections.
        """
        path_str = params.get("path", "")
        offset = params.get("offset", 0)
        limit = params.get("limit", None)  # None = read entire file up to char budget

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
            cached = _mtime_cache.get(cache_key)
            if cached is not None and cached[0] == current_mtime:
                cached_id = cached[1]
                hint = (
                    f" Content is in context at tool result {cached_id}; if it has been"
                    f' evicted, use retrieve_artifact with tool_use_id="{cached_id}".'
                    if cached_id
                    else ""
                )
                return tool_result(
                    f"File unchanged since last read: {path_str} "
                    f"(offset={offset}, limit={limit}).{hint}"
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
        if limit is not None:
            selected = lines[offset : offset + limit]
            truncated = (offset + limit) < total_lines
        else:
            selected = lines[offset:]
            truncated = False

        # --- Format with line numbers, respecting char budget ---
        # Stop emitting lines when approaching the budget so the pagination hint
        # is accurate (no silent truncation downstream). The budget is generous
        # enough for most files; only large ones get split.
        char_budget = _CHAR_BUDGET
        numbered = []
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
        header = f"File: {path_str} ({total_lines} lines)"
        if budget_hit or truncated:
            showing = f"Showing lines {offset + 1}-{actual_end_offset} of {total_lines}"
            hint = f"Use offset={actual_end_offset} to continue reading"
            header += f"\n{showing}\n{hint}"

        content = header + "\n\n" + "\n".join(numbered)

        # --- Update mtime cache on successful read ---
        # Record which tool call produced this content so future stubs can
        # point at it and eviction can invalidate this entry.
        try:
            _mtime_cache[cache_key] = (resolved.stat().st_mtime, current_tool_use_id.get())
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
                    "description": "Maximum number of lines to return (default: entire file)",
                },
            },
            "required": ["path"],
        },
        handler=handler,
        self_truncating=True,
    )
