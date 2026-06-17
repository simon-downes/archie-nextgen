"""Artifact store — keeps full tool results for retrieval after eviction.

When tool results are evicted from context (replaced with summary stubs),
the full content is still available here. The retrieve_artifact tool lets
the model re-fetch evicted content when the stub isn't sufficient.
"""


class ArtifactStore:
    """In-memory store of full tool results keyed by tool_use_id.

    Deliberately not persisted — artifacts are only valid for the current session.
    If sessions become resumable, this would need a durable backend (e.g. SQLite).
    """

    def __init__(self):
        """Initialize empty in-memory storage."""
        self._store: dict[str, dict[str, str]] = {}

    def put(self, tool_use_id: str, content: str, summary: str) -> None:
        """Store full content and summary keyed by tool_use_id.

        Args:
            tool_use_id: Unique identifier from the LLM tool_use block.
            content: Full raw result string from the tool.
            summary: Brief human-readable summary for eviction stubs.

        """
        self._store[tool_use_id] = {"content": content, "summary": summary}

    def get(self, tool_use_id: str) -> dict[str, str] | None:
        """Retrieve stored content and summary by tool_use_id.

        Args:
            tool_use_id: Unique identifier from the LLM tool_use block.

        Returns:
            Dict with 'content' and 'summary' keys, or None if not found.

        """
        return self._store.get(tool_use_id)
