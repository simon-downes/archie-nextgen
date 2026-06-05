"""search_files tool — regex search via ripgrep.

Uses `rg` (ripgrep) rather than Python's built-in search because:
- Ripgrep is FAST (parallelized, uses memory maps, optimized regex engine)
- Respects .gitignore by default (skips node_modules, build dirs, etc.)
- Automatically detects and skips binary files
- Supports context lines (-C) natively
- JSON output mode (--json) gives structured, parseable results

We shell out to `rg` via subprocess rather than using a Python binding because:
- rg is already installed in our environment (host + sandbox containers)
- The --json output is well-documented and stable
- No additional Python dependency needed

Features:
- Case-insensitive by default (-i)
- 2 context lines around matches (-C 2) so the model can understand context
- Optional file glob filter (-g "*.py")
- Pagination via offset/limit (cap 50 matches per call)
- Pagination hint when results are truncated
- Path validation via allowlist (security)
"""

import json
import subprocess
from pathlib import Path

from archie.tools import ToolSpec, tool_error, tool_result, validate_path

# Maximum matches to return per call.
# 50 is enough for the model to find what it needs without overwhelming context.
# If there are more, the pagination hint tells the model how to get the next page.
_MAX_MATCHES = 50


def make_search_files_spec(cwd: Path, allowed_directories: list[Path]) -> ToolSpec:
    """Create a search_files ToolSpec bound to the given path constraints."""

    def handler(params: dict) -> str:
        """Search files for a regex pattern using ripgrep."""
        pattern = params.get("pattern", "")
        path_str = params.get("path", ".")
        glob_filter = params.get("glob", None)
        offset = params.get("offset", 0)
        limit = params.get("limit", _MAX_MATCHES)
        limit = min(limit, _MAX_MATCHES)  # Enforce cap even if model asks for more

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
            "-C",
            "2",  # 2 context lines around each match (gives the model context
            # without needing a separate read_file call for each match)
            "--max-count",
            "200",  # Per-file match limit (safety against pathological regexes)
        ]
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
        # rg --json outputs one JSON object per line. Each has a "type" field:
        # "begin" (new file), "match" (a hit), "context" (surrounding line), "end" (file done)
        matches = _parse_rg_json(result.stdout, offset, limit)
        total_matches = matches["total"]
        output_lines = matches["lines"]
        truncated = matches["truncated"]

        # --- Format the response ---
        header = f"Found {total_matches} match(es) for pattern: {pattern}"
        if truncated:
            shown_end = offset + limit
            header += (
                f"\nShowing matches {offset + 1}-{min(shown_end, total_matches)} of {total_matches}"
            )
            header += f"\nUse offset={shown_end} to see more results"

        content = header + "\n\n" + "\n".join(output_lines)
        return tool_result(content)

    return ToolSpec(
        name="search_files",
        description=(
            "Search file contents using a regex pattern (via ripgrep). "
            "Case-insensitive by default, respects .gitignore. "
            "Returns matches with surrounding context lines."
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
                "offset": {
                    "type": "integer",
                    "description": "Number of matches to skip (for pagination, default 0)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum matches to return (default/max 50)",
                },
            },
            "required": ["pattern"],
        },
        handler=handler,
    )


def _parse_rg_json(stdout: str, offset: int, limit: int) -> dict:
    """Parse ripgrep's --json output into formatted match lines.

    Ripgrep's JSON format (one object per line) has these message types:
    - "begin": {"data": {"path": {"text": "path/to/file.py"}}}
        → start of a file's matches
    - "match": {"data": {"line_number": 42, "lines": {"text": "matching content\n"}}}
        → a line that matched the pattern
    - "context": {"data": {"line_number": 41, "lines": {"text": "surrounding line\n"}}}
        → a context line (from -C flag)
    - "end": end of a file's matches
    - "summary": final statistics (ignored)

    We format the output as:
        path/to/file.py:
          42| matching line content    (pipe = match)
          43: context line content     (colon = context)

    The match/context distinction (| vs :) mirrors grep's own convention and
    helps the model distinguish actual hits from surrounding context.

    Returns:
        {"total": int, "lines": list[str], "truncated": bool}
    """
    # First pass: collect all match groups organized by file
    all_groups: list[dict] = []  # [{"file": str, "lines": [(lineno, text, is_match)]}]
    current_file = ""
    current_lines: list[tuple[int, str, bool]] = []
    match_count = 0

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
            if current_lines:
                all_groups.append({"file": current_file, "lines": current_lines})
                current_lines = []
            path_data = obj.get("data", {}).get("path", {})
            current_file = path_data.get("text", "")
        elif msg_type == "match":
            data = obj.get("data", {})
            lineno = data.get("line_number", 0)
            text = data.get("lines", {}).get("text", "").rstrip("\n")
            current_lines.append((lineno, text, True))
            match_count += 1
        elif msg_type == "context":
            data = obj.get("data", {})
            lineno = data.get("line_number", 0)
            text = data.get("lines", {}).get("text", "").rstrip("\n")
            current_lines.append((lineno, text, False))
        elif msg_type == "end":
            if current_lines:
                all_groups.append({"file": current_file, "lines": current_lines})
                current_lines = []

    # Flush any remaining (shouldn't happen but defensive)
    if current_lines:
        all_groups.append({"file": current_file, "lines": current_lines})

    # Second pass: apply pagination by counting matches across all groups.
    # We skip `offset` matches and then include `limit` matches (plus their context).
    output_lines: list[str] = []
    matches_seen = 0
    matches_included = 0
    truncated = False

    for group in all_groups:
        group_output: list[str] = []
        group_has_match = False

        for lineno, text, is_match in group["lines"]:
            if is_match:
                matches_seen += 1
                if matches_seen <= offset:
                    continue  # Skip matches before offset
                if matches_included >= limit:
                    truncated = True
                    break  # We've shown enough
                matches_included += 1
                group_has_match = True
                # Pipe separator for matches (stands out visually)
                group_output.append(f"  {lineno}| {text}")
            elif group_has_match or matches_seen > offset:
                # Include context lines when we're in the selected range.
                # Colon separator for context (less prominent than pipe).
                group_output.append(f"  {lineno}: {text}")

        if group_output:
            output_lines.append(f"{group['file']}:")
            output_lines.extend(group_output)
            output_lines.append("")  # Blank line between files for readability

        if truncated:
            break

    return {
        "total": match_count,
        "lines": output_lines,
        "truncated": truncated or match_count > offset + limit,
    }
