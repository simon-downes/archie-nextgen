"""brain tool — read, write, search, and commit to the knowledge base.

Provides the model with structured access to the brain: a curated collection
of markdown files with YAML frontmatter covering projects, domain knowledge,
and people. Each operation maps to a BrainIndex method.

Operations:
- read: Get a specific item (frontmatter + body)
- write: Create or update an item (merges frontmatter on update)
- search: Two-phase scored search (index + ripgrep fallback)
- commit: Stage and commit changes to the brain's git repo
"""

from pathlib import Path

from archie.brain import BrainIndex
from archie.tools import ToolSpec, tool_error, tool_result


def make_brain_spec(brain_dir: Path) -> ToolSpec:
    """Create a brain ToolSpec bound to the given brain directory.

    Uses the closure pattern: the BrainIndex is created at registration time
    and captured by the handler. This keeps the tool self-contained.

    Args:
        brain_dir: Root directory of the brain (e.g. ~/.archie/new-brain).
    """
    brain = BrainIndex(brain_dir)

    def handler(params: dict) -> str:
        """Dispatch to the appropriate brain operation based on the 'operation' field."""
        operation = params.get("operation", "")

        match operation:
            case "read":
                return _handle_read(brain, params)
            case "write":
                return _handle_write(brain, params)
            case "search":
                return _handle_search(brain, params)
            case "commit":
                return _handle_commit(brain, params)
            case _:
                return tool_error(
                    f"Unknown operation: {operation}. Use: read, write, search, commit"
                )

    return ToolSpec(
        name="brain",
        description=(
            "Knowledge base for reference material. Use 'read' to get a specific item, "
            "'write' to create/update, 'search' to find items, 'commit' to save to git."
        ),
        schema={
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["read", "write", "search", "commit"],
                    "description": "Operation to perform",
                },
                "path": {
                    "type": "string",
                    "description": "Path within brain (for read/write)",
                },
                "query": {
                    "type": "string",
                    "description": "Search terms (for search)",
                },
                "scope": {
                    "type": "string",
                    "description": "Limit search to subdirectory (e.g. 'projects')",
                },
                "content": {
                    "type": "string",
                    "description": "Markdown body (for write)",
                },
                "name": {
                    "type": "string",
                    "description": "Item title (for write)",
                },
                "summary": {
                    "type": "string",
                    "description": "Brief summary (for write)",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags (for write)",
                },
                "message": {
                    "type": "string",
                    "description": "Commit message (for commit)",
                },
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Paths to stage (for commit, omit for all)",
                },
            },
            "required": ["operation"],
        },
        handler=handler,
    )


def _handle_read(brain: BrainIndex, params: dict) -> str:
    """Handle a brain read operation. Returns formatted frontmatter + body."""
    path = params.get("path", "")
    if not path:
        return tool_error("'path' is required for read")

    try:
        fm, body = brain.read(path)
    except ValueError as e:
        return tool_error(str(e))

    # Format: show frontmatter as key-value pairs, then body
    header_parts = []
    if fm.get("name"):
        header_parts.append(f"Name: {fm['name']}")
    if fm.get("summary"):
        header_parts.append(f"Summary: {fm['summary']}")
    if fm.get("tags"):
        header_parts.append(f"Tags: {', '.join(fm['tags'])}")

    header = "\n".join(header_parts)
    return tool_result(f"{header}\n\n{body.strip()}" if header else body.strip())


def _handle_write(brain: BrainIndex, params: dict) -> str:
    """Handle a brain write operation. Creates or updates a file."""
    path = params.get("path", "")
    name = params.get("name", "")
    summary = params.get("summary", "")
    tags = params.get("tags", [])
    content = params.get("content", "")

    if not path:
        return tool_error("'path' is required for write")
    if not name:
        return tool_error("'name' is required for write")

    try:
        brain.write(path, name, summary, tags, content)
    except ValueError as e:
        return tool_error(str(e))

    return tool_result(f"Written: {path}")


def _handle_search(brain: BrainIndex, params: dict) -> str:
    """Handle a brain search operation. Returns scored results."""
    query = params.get("query", "")
    scope = params.get("scope")

    if not query:
        return tool_error("'query' is required for search")

    results = brain.search(query, scope)

    if not results:
        return tool_result("No results found.")

    # Format results as a readable list
    lines = []
    for r in results:
        tags_str = f" [{', '.join(r['tags'])}]" if r["tags"] else ""
        lines.append(f"- {r['name']} ({r['path']}) score={r['score']}{tags_str}")
        if r["summary"]:
            lines.append(f"  {r['summary']}")

    return tool_result("\n".join(lines))


def _handle_commit(brain: BrainIndex, params: dict) -> str:
    """Handle a brain commit operation. Stages and commits to git."""
    message = params.get("message", "")
    paths = params.get("paths")

    if not message:
        return tool_error("'message' is required for commit")

    result = brain.commit(message, paths)
    return tool_result(result)
