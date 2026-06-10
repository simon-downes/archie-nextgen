"""Artifact store — keeps full tool results for retrieval after eviction.

When tool results are evicted from context (replaced with summary stubs),
the full content is still available here. The retrieve_artifact tool lets
the model re-fetch evicted content when the stub isn't sufficient.
"""


class ArtifactStore:
    """In-memory store of full tool results keyed by tool_use_id."""

    def __init__(self):
        self._store: dict[str, dict[str, str]] = {}

    def put(self, tool_use_id: str, content: str, summary: str) -> None:
        self._store[tool_use_id] = {"content": content, "summary": summary}

    def get(self, tool_use_id: str) -> dict[str, str] | None:
        return self._store.get(tool_use_id)
