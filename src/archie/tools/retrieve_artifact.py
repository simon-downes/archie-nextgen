"""Retrieve artifact tool — re-fetch full content of evicted tool results.

When tool results are evicted from context (replaced with summary stubs),
the model can use this tool to retrieve the full content if the stub is
insufficient. Uses the tool_use_id from the eviction stub.
"""

from archie.artifact_store import ArtifactStore
from archie.tools import ToolSpec, tool_error, tool_result


def make_retrieve_artifact_spec(store: ArtifactStore) -> ToolSpec:
    """Create a ToolSpec for retrieving evicted tool results."""

    def handler(params: dict) -> str:
        tool_use_id = params.get("tool_use_id", "")
        if not tool_use_id:
            return tool_error("tool_use_id is required")
        artifact = store.get(tool_use_id)
        if artifact is None:
            return tool_error(f"No artifact found for id: {tool_use_id}")
        return tool_result(artifact["content"])

    return ToolSpec(
        name="retrieve_artifact",
        description=(
            "Retrieve the full content of a previously evicted tool result. "
            "Use when the summary stub is insufficient. "
            "Provide the tool_use_id from the eviction stub."
        ),
        schema={
            "type": "object",
            "properties": {
                "tool_use_id": {
                    "type": "string",
                    "description": "The tool_use_id from the eviction stub.",
                }
            },
            "required": ["tool_use_id"],
        },
        handler=handler,
    )
