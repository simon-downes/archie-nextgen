"""list_files tool — list file paths matching a glob pattern.

Uses ripgrep's --files mode to list files (respects .gitignore, skips
binary files, fast). This is the tool the model should use when it needs
to know what files exist — NOT search_files with a wildcard pattern.

Returns just file paths, no content. Cheap in tokens, fast to execute.
"""

import subprocess
from pathlib import Path

from archie.tools import ToolSpec, tool_error, tool_result, validate_path

# Maximum files to return per call
_MAX_FILES = 200


def make_list_files_spec(cwd: Path, allowed_directories: list[Path]) -> ToolSpec:
    """Create a list_files ToolSpec bound to the given path constraints."""

    def handler(params: dict) -> str:
        """List files matching a glob pattern."""
        path_str = params.get("path", ".")
        glob_filter = params.get("glob", None)

        # Validate search path
        try:
            search_path = validate_path(path_str, cwd, allowed_directories)
        except ValueError as e:
            return tool_error(str(e))

        # Build rg --files command (lists files, respects .gitignore)
        cmd = ["rg", "--files", "--sort=path", "-g", "!.git/"]
        if glob_filter:
            cmd.extend(["-g", glob_filter])
        cmd.append(str(search_path))

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        except FileNotFoundError:
            return tool_error("ripgrep (rg) is not installed")
        except subprocess.TimeoutExpired:
            return tool_error("Listing timed out after 15 seconds")

        if result.returncode not in (0, 1):
            return tool_error(f"ripgrep error: {result.stderr.strip()}")

        # Parse file list
        files = [f for f in result.stdout.strip().split("\n") if f]

        if not files:
            return tool_result("No files found.")

        total = len(files)
        truncated = total > _MAX_FILES
        shown = files[:_MAX_FILES]

        header = f"Found {total} file(s)"
        if glob_filter:
            header += f" matching '{glob_filter}'"
        if truncated:
            header += f"\nShowing first {_MAX_FILES} of {total}"

        content = header + "\n\n" + "\n".join(shown)
        return tool_result(content)

    return ToolSpec(
        name="list_files",
        description=(
            "List files in a directory. Use the glob parameter to filter by type "
            "(e.g. '*.py', '**/*.ts'). Always provide a glob when looking for specific "
            "file types. Returns file paths only, no content. Respects .gitignore."
        ),
        schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory to list (default: working directory)",
                },
                "glob": {
                    "type": "string",
                    "description": "File glob filter (e.g. '*.py', '*.ts')",
                },
            },
        },
        handler=handler,
    )
