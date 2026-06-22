"""Debug command — exercise tools with default or custom inputs.

Calls tool handlers directly against the current project directory
and prints inputs/outputs with colour. Useful for understanding
exactly what the model receives from each tool call.

Usage:
    archie debug --list              # list available tools
    archie debug read                # run all default exercises for read
    archie debug read --schema       # show read's JSON schema
    archie debug read '{"path":"x"}' # run with custom input
"""

import json
import time
from pathlib import Path

import click

# Default exercises per tool — covers parameter permutations.
# Each entry exercises a distinct combination of optional params.
EXERCISES: dict[str, list[dict]] = {
    "read": [
        # File: no offset, no limit (full file, budget-capped)
        {"path": "src/archie/cli.py"},
        # File: with offset, no limit
        {"path": "src/archie/agent.py", "offset": 50},
        # File: with offset and limit
        {"path": "src/archie/agent.py", "offset": 1, "limit": 10},
        # Directory: no options
        {"path": "src/archie/tools"},
    ],
    "grep": [
        # Pattern only (searches from project root)
        {"pattern": "def make_"},
        # Pattern + path
        {"pattern": "class Test", "path": "tests"},
        # Pattern + path + glob
        {"pattern": "def handler", "path": "src/archie/tools", "glob": "*.py"},
        # Pattern + path + context
        {"pattern": "validate_path", "path": "src/archie/tools/__init__.py", "context": 2},
        # Pattern + path + glob + limit
        {"pattern": "import", "path": "src", "glob": "*.py", "limit": 3},
    ],
    "glob": [
        # Pattern only (from project root)
        {"pattern": "*.py", "path": "src/archie/tools"},
        # Pattern + limit
        {"pattern": "**/*.py", "path": "src/archie", "limit": 5},
        # Pattern in nested path
        {"pattern": "test_tool_*.py", "path": "tests"},
    ],
    "code": [
        # File outline (no name filter)
        {"path": "src/archie/tools/__init__.py"},
        # Directory outline (no name, no language)
        {"path": "src/archie/tools"},
        # Search by name (no path — searches project root)
        {"name": "validate_path"},
        # Search by name + path
        {"name": "handler", "path": "src/archie/tools"},
        # Search by name + language
        {"name": "make_", "language": "python"},
    ],
    "web_search": [
        # Simple query
        {"query": "python tree-sitter"},
    ],
    "web_fetch": [
        # URL only
        {"url": "https://docs.python.org/3/library/dataclasses.html"},
    ],
    "self_debug": [
        # Default: recent records at INFO+
        {"level": "INFO", "tail": 10},
        # Errors only
        {"level": "ERROR"},
        # Specific event type
        {"event": "tool_end", "tail": 5},
    ],
    "write_file": [
        # Not exercised by default — would create files
    ],
    "edit_file": [
        # Not exercised by default — would modify files
    ],
    "shell": [
        # Simple command
        {"command": "echo hello"},
        # Multi-command pipeline
        {"command": "ls src/archie/tools/*.py | wc -l"},
        # Exit code handling
        {"command": "false"},
    ],
}


def _build_registry(cwd: Path):
    """Build a tool registry against the given cwd. Attempts sandbox if Docker available."""
    import os

    from archie.tools import create_default_registry

    sandbox = None
    try:
        from archie.config import load_config
        from archie.sandbox import Sandbox

        config = load_config()
        sandbox = Sandbox(
            config=config.sandbox,
            project_dir=cwd,
            session_id="debug",
            username=os.environ.get("USER", "archie"),
            uid=os.getuid(),
        )
    except Exception:
        pass

    registry = create_default_registry(cwd=cwd, allowed_directories=[], sandbox=sandbox)

    # Register self_debug if log file exists
    try:
        from archie.logs import LOG_PATH
        from archie.tools.self_debug import make_self_debug_spec

        if LOG_PATH.exists():
            registry.register(make_self_debug_spec(LOG_PATH, lambda: "2026-06-21-archie-nextgen-eb7df"))
    except Exception:
        pass

    return registry


def _run_exercise(tool_spec, params: dict, cwd: Path) -> None:
    """Run a single tool call and print input/output plus UI summary."""
    from rich.console import Console

    from archie.ui.tool_formatters import format_tool_complete

    console = Console(highlight=False)
    params_str = json.dumps(params, ensure_ascii=False)

    # Header
    click.echo(click.style(f"── {tool_spec.name} ", fg="cyan", bold=True), nl=False)
    click.echo(click.style(params_str, fg="white"))

    # Execute
    start = time.perf_counter()
    try:
        result = tool_spec.handler(params)
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        click.echo(click.style(f"[{elapsed:.1f}ms] EXCEPTION: {e}", fg="red"))
        click.echo()
        return
    elapsed = (time.perf_counter() - start) * 1000

    # Metadata
    click.echo(click.style(f"[{elapsed:.1f}ms, {len(result)} chars]", fg="yellow", dim=True))

    # Output — indent for visual separation
    click.echo()
    for line in result.split("\n"):
        click.echo(f"  {line}")

    # UI summary (what the TUI would show)
    is_error = result.startswith("Error:")
    summary = format_tool_complete(tool_spec.name, params, result, is_error, cwd)
    console.print(f"  [dim]ui:[/] {summary}")
    click.echo()


def run_debug(tool_name: str | None, params_json: str | None, show_schema: bool, list_tools: bool):
    """Main debug logic."""
    cwd = Path.cwd()
    registry = _build_registry(cwd)

    if list_tools:
        click.echo(click.style("Available tools:", bold=True))
        for spec in registry._tools.values():
            has_exercises = "✓" if spec.name in EXERCISES and EXERCISES[spec.name] else "○"
            click.echo(f"  {has_exercises} {spec.name}")
        return

    if not tool_name:
        raise click.UsageError("Specify a tool name, or use --list")

    spec = registry.get(tool_name)
    if not spec:
        raise click.UsageError(f"Unknown tool '{tool_name}'. Use --list to see available tools.")

    if show_schema:
        click.echo(click.style(f"Schema: {spec.name}", bold=True))
        click.echo(json.dumps(spec.schema, indent=2))
        click.echo()
        click.echo(click.style("Description:", bold=True))
        click.echo(spec.description)
        return

    if params_json:
        # Custom input
        try:
            params = json.loads(params_json)
        except json.JSONDecodeError as e:
            raise click.UsageError(f"Invalid JSON: {e}") from None
        _run_exercise(spec, params, cwd)
    else:
        # Default exercises
        exercises = EXERCISES.get(tool_name, [])
        if not exercises:
            click.echo(f"No default exercises for '{tool_name}'. Pass JSON params manually.")
            return
        for params in exercises:
            _run_exercise(spec, params, cwd)
