"""recall tool — search memory fragments from past conversations.

Scans daily per-project JSONL files in {brain_dir}/_memory/ and scores
matches against topic, tags, and content. Uses date-range pre-filtering
from filenames and recency bonuses from ULID ordering.

Scoring: topic match (+3), tag match (+2), content match (+1).
Recency bonus: <7 days (+2), <30 days (+1).
"""

import json
import logging
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from archie.tools import ToolSpec, tool_error, tool_result

log = logging.getLogger(__name__)

_STOPWORDS = frozenset(
    "a an and are as at be by for from has have in is it of on or that the to was with".split()
)


def make_recall_spec(brain_dir: Path) -> ToolSpec:
    """Create a recall ToolSpec bound to the given brain directory.

    Uses the closure pattern: brain_dir is captured at registration time.
    """
    memory_dir = brain_dir / "_memory"
    db_path = brain_dir / "brain.db"

    def handler(params: dict) -> str:
        query = params.get("query", "")
        type_filter = params.get("type")
        project_filter = params.get("project")
        since = params.get("since")
        limit = params.get("limit", 20)

        if not query:
            return tool_error("'query' is required")

        if not memory_dir.exists():
            return tool_result("No memory fragments found.")

        # Tokenise query, remove stopwords
        terms = [t.lower() for t in query.split() if t.lower() not in _STOPWORDS]
        if not terms:
            return tool_result("No searchable terms in query.")

        # Date range pre-filter from filenames
        since_date = None
        if since:
            try:
                since_date = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=UTC).date()
            except ValueError:
                return tool_error("'since' must be YYYY-MM-DD format")

        # Collect candidate JSONL files
        files = sorted(memory_dir.glob("*.jsonl"), reverse=True)
        if project_filter:
            files = [f for f in files if f.stem.endswith(f"-{project_filter}")]
        if since_date:
            files = [f for f in files if _file_date(f) >= since_date]

        # Recency reference dates
        now = datetime.now(UTC).date()
        week_ago = now - timedelta(days=7)
        month_ago = now - timedelta(days=30)

        # Score fragments
        scored: list[tuple[float, dict]] = []
        for file in files:
            file_date = _file_date(file)
            for line in file.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    fragment = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Type filter
                if type_filter and fragment.get("type") != type_filter:
                    continue

                # Score against terms
                score = 0
                topic = fragment.get("topic", "").lower()
                tags = [t.lower() for t in fragment.get("tags", [])]
                content = fragment.get("content", "").lower()

                for term in terms:
                    if term in topic:
                        score += 3
                    if any(term in tag for tag in tags):
                        score += 2
                    if term in content:
                        score += 1

                if score == 0:
                    continue

                # Recency bonus
                if file_date and file_date >= week_ago:
                    score += 2
                elif file_date and file_date >= month_ago:
                    score += 1

                scored.append((score, fragment))

        if not scored:
            return tool_result("No matching memory fragments found.")

        # Sort by score descending, take top N
        scored.sort(key=lambda x: x[0], reverse=True)
        results = scored[:limit]

        # Record ref access
        _record_ref(db_path, f"_memory/recall:{query}")

        # Format output
        lines = []
        for _score, frag in results:
            frag_id = frag.get("id", "?")[:8]
            lines.append(
                f"[{frag.get('type')}] {frag.get('topic')} — {frag.get('content')} ({frag_id})"
            )

        return tool_result("\n".join(lines))

    return ToolSpec(
        name="recall",
        description=(
            "Search memory fragments from past conversations. "
            "Find decisions, learnings, preferences, and context "
            "by topic, type, project, or date range."
        ),
        schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search terms (matched against topic + content + tags)",
                },
                "type": {
                    "type": "string",
                    "enum": ["decision", "learning", "preference", "state", "context"],
                    "description": "Filter by fragment type",
                },
                "project": {
                    "type": "string",
                    "description": "Filter by project name",
                },
                "since": {
                    "type": "string",
                    "description": "Only fragments after this date (YYYY-MM-DD)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 20)",
                },
            },
            "required": ["query"],
        },
        handler=handler,
    )


def _file_date(path: Path) -> datetime.date | None:
    """Extract date from memory filename (YYYY-MM-DD-project.jsonl)."""
    parts = path.stem.split("-")
    if len(parts) >= 3:
        try:
            return datetime(int(parts[0]), int(parts[1]), int(parts[2]), tzinfo=UTC).date()
        except (ValueError, IndexError):
            return None
    return None


def _record_ref(db_path: Path, path: str) -> None:
    """Record a recall access in brain.db for observability."""
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO refs (path, ts) VALUES (?, ?)", (path, int(time.time())))
        conn.commit()
        conn.close()
    except (sqlite3.OperationalError, OSError):
        pass  # Non-critical — don't fail recall on ref tracking errors
