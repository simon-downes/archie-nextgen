"""edit_file tool — surgical search-and-replace edits to existing files.

This is the primary tool for modifying code. It uses literal string matching
(not regex) because:
- The model already knows the exact text from a prior read_file call
- Literal matching is deterministic — it either finds the text or fails cleanly
- Regex introduces escaping bugs and silent corruption from greedy/wrong matches
- For complex pattern-based edits, the model can use the shell tool (sed, rg --replace)

Key safety features:
- Unique match enforcement: if `old` text appears multiple times, the edit fails
  with an error telling the model to include more context. This is self-correcting.
- Atomic writes: if any edit in a batch fails, the file is unchanged on disk.
- replace_all opt-in: for intentional bulk renames, the model explicitly requests
  "replace all occurrences" — never happens by accident.
"""

from pathlib import Path

from archie.tools import ToolSpec, tool_error, tool_result, validate_path


def make_edit_file_spec(
    cwd: Path,
    allowed_directories: list[Path],
    mtime_cache: dict[tuple[str, int, int], tuple[float, str]],
) -> ToolSpec:
    """Create an edit_file ToolSpec bound to path constraints.

    Args:
        cwd: Working directory for resolving relative paths.
        allowed_directories: Additional directories the tool can edit.
        mtime_cache: Shared cache with read_file — invalidated on edit.
    """

    def handler(params: dict) -> str:
        path_str = params["path"]
        edits = params["edits"]

        # Security: enforce path allowlist
        try:
            resolved = validate_path(path_str, cwd, allowed_directories)
        except ValueError as e:
            return tool_error(str(e))

        # Must be an existing file — use write_file for new files
        if not resolved.is_file():
            return tool_error(f"File not found: {path_str} (use write_file to create new files)")

        # Read current content
        try:
            content = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return tool_error(f"Cannot read file: {e}")

        # Apply edits sequentially — each edit operates on the result of the previous.
        # If any edit fails, we bail out without writing (atomic guarantee).
        total_replacements = 0
        for i, edit in enumerate(edits):
            old = edit["old"]
            new = edit["new"]
            replace_all = edit.get("replace_all", False)

            # Guard against empty old string — str.replace("", ...) has unintuitive
            # behaviour in Python (inserts between every character).
            if not old:
                return tool_error(f"Edit {i + 1}: 'old' text cannot be empty.")

            count = content.count(old)

            if count == 0:
                return tool_error(
                    f"Edit {i + 1}: text not found in file. Ensure the 'old' text "
                    f"matches exactly (including whitespace and indentation)."
                )

            if count > 1 and not replace_all:
                return tool_error(
                    f"Edit {i + 1}: found {count} matches. Include more surrounding "
                    f"context to disambiguate, or set replace_all=true to replace all."
                )

            if replace_all:
                content = content.replace(old, new)
                total_replacements += count
            else:
                content = content.replace(old, new, 1)
                total_replacements += 1

        # All edits succeeded — write atomically
        try:
            resolved.write_text(content, encoding="utf-8")
        except OSError as e:
            return tool_error(f"Cannot write file: {e}")

        # Invalidate mtime cache so subsequent reads return fresh content
        resolved_str = str(resolved)
        stale_keys = [k for k in mtime_cache if k[0] == resolved_str]
        for k in stale_keys:
            del mtime_cache[k]

        # Confirmation message
        edit_count = len(edits)
        if total_replacements > edit_count:
            detail = f"{edit_count} edit(s), {total_replacements} replacements"
        else:
            detail = f"{edit_count} edit(s) applied"
        return tool_result(f"Edited: {path_str} ({detail})")

    return ToolSpec(
        name="edit_file",
        description=(
            "Apply search-and-replace edits to an existing file. Each edit specifies "
            "the exact text to find (old) and its replacement (new). The old text must "
            "match uniquely — if multiple matches exist, include more surrounding context "
            "or set replace_all=true. Use write_file to create new files."
        ),
        schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to edit",
                },
                "edits": {
                    "type": "array",
                    "description": "List of edits to apply sequentially",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old": {
                                "type": "string",
                                "description": "Exact text to find (must match uniquely unless replace_all=true)",
                            },
                            "new": {
                                "type": "string",
                                "description": "Replacement text",
                            },
                            "replace_all": {
                                "type": "boolean",
                                "description": "Replace all occurrences (default false)",
                            },
                        },
                        "required": ["old", "new"],
                    },
                },
            },
            "required": ["path", "edits"],
        },
        handler=handler,
    )
