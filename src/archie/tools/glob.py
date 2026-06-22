"""glob tool — recursive file discovery by glob pattern, sorted by mtime.

Discovers files matching a glob pattern via ripgrep, then sorts by modification
time descending (most recent first). Returns relative paths for files under cwd,
absolute paths otherwise.

Design decisions:
- Uses `rg --files -g <pattern>` for fast, .gitignore-aware enumeration.
- mtime sorting via os.stat() after collection (rg doesn't support it).
- No pagination — model narrows pattern if over limit.
- Relative paths when search is under cwd, absolute otherwise for clarity.
"""

import logging
import os
import subprocess
from pathlib import Path

from archie.tools import ToolSpec, tool_error, tool_result, validate_path

log = logging.getLogger(__name__)


def make_glob_spec(cwd: Path, allowed_directories: list[Path]) -> ToolSpec:
    """Create a glob ToolSpec bound to the given path constraints.

    Args:
        cwd: Working directory for resolving relative paths and computing relative outputs.
        allowed_directories: Additional directories the tool can access.
    """

    def handler(params: dict) -> str:
        """Find files by glob pattern, sorted by mtime descending."""
        pattern = params.get("pattern", "")
        path_str = params.get("path", "")
        limit = params.get("limit", 100)

        # Validate pattern parameter
        if not pattern:
            return tool_error("Pattern is required")

        # Validate path and determine search directory
        try:
            if path_str:
                search_path = validate_path(path_str, cwd, allowed_directories)
            else:
                search_path = cwd.resolve()
        except ValueError as e:
            return tool_error(str(e))

        # Ensure search_path exists and is a directory
        if not search_path.exists():
            return tool_error(f"Directory does not exist: {path_str or str(cwd)}")
        if not search_path.is_dir():
            return tool_error(f"Not a directory: {path_str or str(cwd)}")

        # Run ripgrep to enumerate files matching the pattern
        # Excluding .git/ directory to avoid noise
        cmd = ["rg", "--files", "-g", pattern, "-g", "!.git/", str(search_path)]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        except FileNotFoundError:
            return tool_error("ripgrep (rg) is not installed")
        except subprocess.TimeoutExpired:
            return tool_error("Glob search timed out after 15 seconds")

        if result.returncode not in (0, 1):
            return tool_error(f"ripgrep error: {result.stderr.strip()}")

        # Parse file list
        files_str = [f for f in result.stdout.strip().split("\n") if f]

        if not files_str:
            return tool_result("No files found.")

        # Collect mtime for each file and sort descending
        files_with_mtime = []
        for file_path in files_str:
            try:
                st = os.stat(file_path)
                files_with_mtime.append((file_path, st.st_mtime))
            except OSError:
                continue

        # Sort by mtime descending (most recent first)
        files_with_mtime.sort(key=lambda x: x[1], reverse=True)

        # Truncate to limit and format output
        total_count = len(files_with_mtime)
        truncated = total_count > limit
        display_files = files_with_mtime[:limit]

        # Compute relative paths based on search context
        output_lines = []
        for file_path, _ in display_files:
            try:
                if search_path.resolve() == cwd.resolve():
                    rel_path = os.path.relpath(file_path, cwd)
                else:
                    rel_path = os.path.relpath(file_path, search_path)
                output_lines.append(rel_path)
            except ValueError:
                output_lines.append(file_path)

        # Header with count and sort order
        if truncated:
            header = f"{limit} files shown of {total_count}, most recent first. Narrow the pattern for more."
        else:
            header = f"{total_count} files, most recent first"

        result_str = header + "\n\n" + "\n".join(output_lines)
        return tool_result(result_str)

        result_str = "\n".join(output_lines)

        # Add summary line if truncated
        if truncated:
            result_str += f"\n\n{limit} files shown of {total_count}. Narrow the pattern for more."

    return ToolSpec(
        name="glob",
        description=(
            "Find files by glob pattern. Results sorted by most recently modified.\n\n"
            "- Respects .gitignore.\n"
            "- Narrow the pattern if too many results — no pagination.\n"
            "- Use for discovering which files exist, not for reading content."
        ),
        schema={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern (e.g. '**/*.py', 'src/**/*.ts', '*.md')",
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search from (default: working directory)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum files to return (default: 100)",
                },
            },
            "required": ["pattern"],
        },
        handler=handler,
    )
