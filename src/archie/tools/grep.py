"""grep tool — regex search via ripgrep with match group limiting.

This tool replaces search_files with a cleaner interface:
- Case-insensitive by default
- Results sorted by file mtime (most recently modified first)
- Match groups (contiguous blocks) as the limiting unit instead of individual lines
- Configurable context lines (default 0)
- Line truncation at 500 bytes to prevent minified files from bloating output

The key differences from search_files:
- No pagination (offset) — simpler interface, model can narrow if needed
- Match groups = more meaningful unit than individual lines
- Mtime sort surfaces relevant files first
- Default context = 0 (model can request context when needed)
- Line truncation prevents output bloat from long lines
"""

import json
import os
import subprocess
from pathlib import Path

from archie.tools import ToolSpec, tool_error, tool_result, validate_path

# Maximum match groups to return per call.
# A match group is a contiguous block of matches (with context) in a file.
# If results are truncated, the model can narrow the search query.
_MAX_GROUPS = 50


def make_grep_spec(cwd: Path, allowed_directories: list[Path]) -> ToolSpec:
    """Create a grep ToolSpec bound to the given path constraints."""

    def handler(params: dict) -> str:
        """Search files for a regex pattern using ripgrep."""
        pattern = params.get("pattern", "")
        path_str = params.get("path", ".")
        glob_filter = params.get("glob", None)
        context = params.get("context", 0)
        limit = params.get("limit", _MAX_GROUPS)
        limit = min(limit, _MAX_GROUPS)  # Enforce cap even if model asks for more

        if not pattern:
            return tool_error("'pattern' is required")

        # --- Security: validate search root against allowlist ---
        try:
            search_path = validate_path(path_str, cwd, allowed_directories)
        except ValueError as e:
            return tool_error(str(e))

        # --- Build the rg command ---
        cmd = [
            "rg",
            "--json",  # Output one JSON object per line (structured, parseable)
            "-i",  # Case-insensitive (most searches are for identifiers)
        ]
        if context > 0:
            # -C yields context lines before AND after each match
            cmd.extend(["-C", str(context)])
        cmd.extend(["--max-count", "200"])  # Per-file match limit (safety against pathological regexes)
        if glob_filter:
            # -g filters by glob pattern (e.g. "*.py" for Python files only)
            cmd.extend(["-g", glob_filter])
        cmd.append(pattern)
        cmd.append(str(search_path))

        # --- Run ripgrep ---
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,  # Don't let pathological regexes hang forever
            )
        except FileNotFoundError:
            return tool_error("ripgrep (rg) is not installed")
        except subprocess.TimeoutExpired:
            return tool_error("Search timed out after 30 seconds")

        # rg exit codes: 0 = matches found, 1 = no matches, 2+ = error
        if result.returncode not in (0, 1):
            return tool_error(f"ripgrep error: {result.stderr.strip()}")

        if result.returncode == 1:
            return tool_result("No matches found.")

        # --- Parse the JSON output ---
        file_matches = _parse_rg_json(result.stdout)

        if not file_matches:
            return tool_result("No matches found.")

        # --- Collect files with matches and stat them for mtime ---
        files_with_mtime = []
        for file_path, matches in file_matches.items():
            try:
                mtime = os.path.getmtime(file_path)
                files_with_mtime.append((file_path, mtime, matches))
            except OSError:
                # Skip files we can't stat
                continue

        if not files_with_mtime:
            return tool_result("No matches found.")

        # --- Sort files by mtime descending (most recent first) ---
        files_with_mtime.sort(key=lambda x: x[1], reverse=True)

        # --- Format output with match groups and limit ---
        output_lines = _format_groups(files_with_mtime, context, limit)

        # --- Build result ---
        if not output_lines:
            return tool_result("No matches found.")

        content = "\n".join(output_lines)
        return tool_result(content)

    return ToolSpec(
        name="grep",
        description=(
            "Search file contents using regex. Case-insensitive. Results sorted by file modification time.\n"
            "\n"
            "- Respects .gitignore.\n"
            "- Do NOT wrap the pattern in quotes. Do NOT double-escape.\n"
            "- Use `context` param only when you need surrounding lines — default is match-only.\n"
            "- Prefer speculative parallel searches over sequential rounds of glob+grep."
        ),
        schema={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern to search for",
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search in (default: working directory)",
                },
                "glob": {
                    "type": "string",
                    "description": "File glob filter (e.g. '*.py')",
                },
                "context": {
                    "type": "integer",
                    "description": "Lines of context around matches (default: 0)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max match groups to return (default: 50)",
                },
            },
            "required": ["pattern"],
        },
        handler=handler,
    )


def _parse_rg_json(stdout: str) -> dict[str, list[tuple[int, str, bool]]]:
    """Parse ripgrep's --json output into per-file match data.

    Ripgrep's JSON format (one object per line) has these message types:
    - "begin": {"data": {"path": {"text": "path/to/file.py"}}}
        → start of a file's matches
    - "match": {"data": {"line_number": 42, "lines": {"text": "matching content\n"}}}
        → a line that matched the pattern
    - "context": {"data": {"line_number": 41, "lines": {"text": "surrounding line\n"}}}
        → a context line (from -C flag)
    - "end": end of a file's matches
    - "summary": final statistics (ignored)

    Args:
        stdout: Raw stdout from rg --json

    Returns:
        Dict mapping absolute file paths to list of (lineno, text, is_match) tuples,
        sorted by line number.
    """
    file_matches: dict[str, list[tuple[int, str, bool]]] = {}
    current_file = ""
    current_lines: list[tuple[int, str, bool]] = []

    for line in stdout.strip().split("\n"):
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_type = obj.get("type")
        if msg_type == "begin":
            # New file starting — flush the previous file's results
            if current_lines and current_file:
                file_matches[current_file] = current_lines
            current_lines = []
            path_data = obj.get("data", {}).get("path", {})
            current_file = path_data.get("text", "")
        elif msg_type == "match":
            data = obj.get("data", {})
            lineno = data.get("line_number", 0)
            text = data.get("lines", {}).get("text", "").rstrip("\n")
            current_lines.append((lineno, text, True))
        elif msg_type == "context":
            data = obj.get("data", {})
            lineno = data.get("line_number", 0)
            text = data.get("lines", {}).get("text", "").rstrip("\n")
            current_lines.append((lineno, text, False))
        elif msg_type == "end":
            if current_lines and current_file:
                file_matches[current_file] = current_lines
            current_lines = []

    # Flush any remaining (shouldn't happen but defensive)
    if current_lines and current_file:
        file_matches[current_file] = current_lines

    return file_matches


def _format_groups(
    files_with_mtime: list[tuple[str, float, list[tuple[int, str, bool]]]],
    context: int,
    limit: int,
) -> list[str]:
    """Format match groups with truncation at file and line level.

    Args:
        files_with_mtime: List of (file_path, mtime, matches) sorted by mtime desc
        context: Number of context lines to show around matches
        limit: Max match groups to output

    Returns:
        List of output lines (not joined with newlines yet).
    """
    output_lines: list[str] = []
    groups_counted = 0
    first_group = True

    for file_path, _mtime, matches in files_with_mtime:
        if groups_counted >= limit:
            break

        # Format this file's matches into groups
        file_output = _format_file_groups(file_path, matches, context, limit - groups_counted)
        if not file_output:
            continue

        # Add blank line separator between file groups (not before first)
        if not first_group:
            output_lines.append("")
        first_group = False
        output_lines.extend(file_output)

        # Count how many groups this file contributed
        file_group_count = _count_file_groups(matches, context)
        groups_counted += min(file_group_count, limit - groups_counted)

    return output_lines


def _format_file_groups(
    file_path: str,
    matches: list[tuple[int, str, bool]],
    context: int,
    max_groups: int,
) -> list[str]:
    """Format a single file's matches into groups.

    Args:
        file_path: Absolute path to the file
        matches: List of (lineno, text, is_match) tuples (already sorted by lineno)
        context: Number of context lines to show around matches
        max_groups: Max groups to output for this file

    Returns:
        List of output lines for this file.
    """
    if not matches:
        return []

    # Segment matches into groups
    groups = _segment_matches(matches, context)

    # Truncate to max_groups
    groups = groups[:max_groups]

    if not groups:
        return []

    output_lines: list[str] = []

    # Output header
    output_lines.append(f"{file_path}:")

    for group in groups:
        # Determine range and separator for this group
        match_lines = [lineno for lineno, text, is_match in group if is_match]

        if not match_lines:
            continue

        min_match = min(match_lines)
        max_match = max(match_lines)

        # Build the full range including context
        range_start = min_match - context if context > 0 else min_match
        range_end = max_match + context if context > 0 else max_match

        # Group has match + context lines; determine which separator to use
        # If context > 0, we use ':' for context lines and '|' for match lines
        # If context == 0, all lines in group are matches, use '|'

        if context > 0:
            # Build a map of line numbers in this group
            group_line_map = {lineno: (text, is_match) for lineno, text, is_match in group}

            for lineno in range(range_start, range_end + 1):
                if lineno in group_line_map:
                    text, is_match = group_line_map[lineno]
                    # Truncate if needed
                    if len(text) > 500:
                        text = text[:500] + " [...]"
                    sep = "|" if is_match else ":"
                    output_lines.append(f"  {lineno}{sep} {text}")
        else:
            # Match-only mode: only output match lines from this group
            for lineno, text, is_match in group:
                if is_match:
                    if len(text) > 500:
                        text = text[:500] + " [...]"
                    output_lines.append(f"  {lineno}| {text}")

    return output_lines


def _segment_matches(
    matches: list[tuple[int, str, bool]], context: int
) -> list[list[tuple[int, str, bool]]]:
    """Segment sorted matches into groups.

    contiguous matches (with context overlap) = 1 group
    gap > 1 + 2*context = new group

    Args:
        matches: Sorted list of (lineno, text, is_match) tuples
        context: Number of context lines to consider

    Returns:
        List of groups, where each group is a list of (lineno, text, is_match)
    """
    if not matches:
        return []

    groups: list[list[tuple[int, str, bool]]] = []
    current_group: list[tuple[int, str, bool]] = []

    for lineno, text, is_match in matches:
        if not is_match:
            # Context line — include it in the current group if available
            # If no group yet, start a new one (context line at start of file)
            if not current_group:
                current_group = [(lineno, text, False)]
            else:
                current_group.append((lineno, text, False))
            continue

        if not current_group:
            # New group starting
            current_group.append((lineno, text, True))
        else:
            # Check if this match belongs to current group or starts a new one
            # Get the last line number (match or context) in current group
            last_lineno = current_group[-1][0]

            # With context C, a new match separated by > 2*C+1 lines from the previous
            # starts a new group
            gap = lineno - last_lineno
            if gap > 1 + 2 * context:
                # Gap too large — start new group
                groups.append(current_group)
                current_group = [(lineno, text, True)]
            else:
                # Overlapping/adjacent — add to current group
                current_group.append((lineno, text, True))

    # Flush final group
    if current_group:
        groups.append(current_group)

    return groups


def _count_file_groups(matches: list[tuple[int, str, bool]], context: int) -> int:
    """Count how many groups are in a file's matches."""
    groups = _segment_matches(matches, context)
    return len(groups)
