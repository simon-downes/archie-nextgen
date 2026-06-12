"""Tool framework — registry, path validation, and utilities.

The tool framework enables the model to call tools during conversation.
Each tool is defined by a ToolSpec containing its name, description,
JSON Schema for input validation, and a handler function.

Architecture:
- Tools are registered explicitly in create_default_registry() — no auto-discovery
- Adding a new tool = one new file + one line in create_default_registry()
- Path validation is shared across all file-related tools
- Results are truncated to prevent context bloat
"""

from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from archie.artifact_store import ArtifactStore
    from archie.sandbox import Sandbox


# Set by the agent loop around each tool invocation. Handlers that need to
# associate state with the originating call (e.g. read_file's mtime cache
# remembering which tool result holds the cached content) read it here.
# Default "" keeps direct handler calls (tests, scripts) working.
current_tool_use_id: ContextVar[str] = ContextVar("current_tool_use_id", default="")


@dataclass
class ToolSpec:
    """Definition of a tool the model can call.

    Attributes:
        name: Unique tool identifier (e.g. "read_file").
        description: What the tool does — shown to the model so it knows when to use it.
        schema: JSON Schema dict describing the tool's input parameters.
            Must follow JSON Schema format with "type": "object" at the top level.
        handler: Function that executes the tool. Takes a dict of parsed input
            params and returns a string result.
    """

    name: str
    description: str
    schema: dict
    handler: Callable[[dict], str]
    self_truncating: bool = False  # If True, skip truncate_result — tool manages its own budget


class ToolRegistry:
    """Registry of available tools.

    The AgentLoop uses this to:
    1. Build the toolConfig sent to Bedrock (so the model knows what's available)
    2. Look up handlers when the model calls a tool
    """

    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        """Register a tool. Raises ValueError if name is already taken."""
        if spec.name in self._tools:
            raise ValueError(f"Tool '{spec.name}' already registered")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        """Look up a tool by name. Returns None if not found."""
        return self._tools.get(name)

    def to_tool_config(self) -> list[dict]:
        """Build Bedrock-format tool definitions for the API request.

        Returns a list of tool specs in the format Bedrock's toolConfig.tools expects.
        """
        return [
            {
                "toolSpec": {
                    "name": spec.name,
                    "description": spec.description,
                    "inputSchema": {"json": spec.schema},
                }
            }
            for spec in self._tools.values()
        ]


def validate_path(path: str, cwd: Path, allowed: list[Path]) -> Path:
    """Resolve a path and verify it's under an allowed directory.

    Security: prevents the model from reading arbitrary files on the system.
    Only files under cwd or explicitly configured allowed_directories are accessible.

    Args:
        path: The path to validate (absolute or relative to cwd).
        cwd: The current working directory (always allowed).
        allowed: Additional allowed directories from config.

    Returns:
        The resolved absolute Path.

    Raises:
        ValueError: If the path is outside all allowed directories.
    """
    resolved = Path(path).resolve() if Path(path).is_absolute() else (cwd / path).resolve()

    # Check if the resolved path is under any allowed directory
    allowed_dirs = [cwd.resolve()] + [p.resolve() for p in allowed]
    for allowed_dir in allowed_dirs:
        try:
            resolved.relative_to(allowed_dir)
            return resolved
        except ValueError:
            continue

    raise ValueError(
        f"Path '{path}' is outside allowed directories. Allowed: {[str(d) for d in allowed_dirs]}"
    )


def truncate_result(content: str, max_chars: int = 4000) -> str:
    """Truncate tool output to prevent context bloat.

    Long tool results eat into the model's context window. We cap them
    and add an indicator so the model knows content was cut off.

    Args:
        content: The full tool result string.
        max_chars: Maximum characters to keep (default 4000).

    Returns:
        The content, truncated with indicator if it exceeded max_chars.
    """
    if len(content) <= max_chars:
        return content
    return content[:max_chars] + f"\n\n[...truncated, {len(content)} chars total]"


def tool_result(content: str) -> str:
    """Format a successful tool result. Currently a passthrough but allows
    consistent formatting if we add structure later."""
    return content


def tool_error(message: str) -> str:
    """Format a tool error message consistently."""
    return f"Error: {message}"


def create_default_registry(
    cwd: Path,
    allowed_directories: list[Path],
    sandbox: "Sandbox | None" = None,
    brain_dir: Path | None = None,
    artifact_store: "ArtifactStore | None" = None,
    session_id_fn: "Callable[[], str] | None" = None,
    mtime_cache: "dict[tuple[str, int, int], tuple[float, str]] | None" = None,
) -> ToolRegistry:
    """Create a ToolRegistry with the standard tool set.

    This is the single place where tools are registered. Adding a new tool
    means importing its spec and calling registry.register().

    Args:
        cwd: Current working directory (for path validation).
        allowed_directories: Additional allowed paths from config.
        sandbox: Optional Sandbox instance. If provided, the shell tool is
            registered (allowing the model to execute commands in the container).
        brain_dir: Optional brain directory. If provided and exists, the brain
            tool is registered.
        artifact_store: Optional ArtifactStore. If provided, the retrieve_artifact
            tool is registered.
        session_id_fn: Optional callable returning the current session id. If
            provided, the self_debug tool is registered (model can inspect its
            own debug log, filtered to the current session by default).
        mtime_cache: Optional shared read-dedup cache. Pass the same dict to
            AgentLoop so context eviction can invalidate entries whose cached
            content is no longer in context. If None, a private dict is used
            (write/edit invalidation still works; eviction invalidation doesn't).
    """
    from archie.tools.code import make_code_spec
    from archie.tools.edit_file import make_edit_file_spec
    from archie.tools.list_files import make_list_files_spec
    from archie.tools.read_file import make_read_file_spec
    from archie.tools.search_files import make_search_files_spec
    from archie.tools.write_file import make_write_file_spec

    registry = ToolRegistry()

    # Shared mtime cache — read_file populates it, write/edit tools invalidate
    # entries when they modify files, and the agent loop invalidates entries
    # when the corresponding tool result is evicted from context. This ensures
    # reads return fresh content instead of useless "file unchanged" stubs.
    if mtime_cache is None:
        mtime_cache = {}

    registry.register(make_list_files_spec(cwd, allowed_directories))
    registry.register(make_read_file_spec(cwd, allowed_directories, mtime_cache))
    registry.register(make_search_files_spec(cwd, allowed_directories))
    registry.register(make_write_file_spec(cwd, allowed_directories, mtime_cache))
    registry.register(make_edit_file_spec(cwd, allowed_directories, mtime_cache))
    registry.register(make_code_spec(cwd, allowed_directories))

    # Shell tool: only registered if a sandbox is available.
    # The sandbox provides the execution environment (Docker container).
    if sandbox is not None:
        from archie.tools.shell import make_shell_spec

        registry.register(make_shell_spec(sandbox))

    # Brain tool: only registered if the brain directory exists.
    # Requires `archie init` to have been run first.
    if brain_dir is not None and brain_dir.exists():
        from archie.tools.brain_tool import make_brain_spec

        registry.register(make_brain_spec(brain_dir))

    # Recall tool: search memory fragments. Requires brain_dir with _memory/.
    if brain_dir is not None and (brain_dir / "_memory").exists():
        from archie.tools.recall import make_recall_spec

        registry.register(make_recall_spec(brain_dir))

    # Artifact retrieval tool: lets the model re-fetch evicted tool results.
    if artifact_store is not None:
        from archie.tools.retrieve_artifact import make_retrieve_artifact_spec

        registry.register(make_retrieve_artifact_spec(artifact_store))

    # Self-debug tool: lets the model inspect its own debug log.
    if session_id_fn is not None:
        from archie.logs import LOG_PATH
        from archie.tools.self_debug import make_self_debug_spec

        registry.register(make_self_debug_spec(LOG_PATH, session_id_fn))

    return registry
