"""Brain knowledge base — curated reference material.

The brain is a git-backed directory of markdown files with YAML frontmatter.
It provides structured read/write/search/commit operations for the model to
manage persistent knowledge (projects, domain knowledge, people).

Key design decisions:
- Frontmatter stores metadata (name, summary, tags) inline with content
- index.yaml provides fast search without parsing every file
- SQLite brain.db tracks access patterns (ref counting) for observability
- Two-phase search: index first (fast), ripgrep fallback for body matches
- Path validation prevents access outside the brain directory
"""

import logging
import re
import sqlite3
import subprocess
import time
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

# Words too common to be useful in search scoring
_STOPWORDS = frozenset(
    "a an and are as at be by for from has have in is it of on or that the to was with".split()
)


class BrainIndex:
    """Interface to the brain knowledge base.

    Provides CRUD operations on markdown files with YAML frontmatter,
    index-based search with scoring, and git commit integration.

    Args:
        brain_dir: Root directory of the brain (e.g. ~/.archie/new-brain).
    """

    def __init__(self, brain_dir: Path) -> None:
        self._brain_dir = brain_dir
        self._db_path = brain_dir / "brain.db"
        self._index_path = brain_dir / "index.yaml"
        self._init_db()

    def _init_db(self) -> None:
        """Create the refs table if it doesn't exist.

        The refs table records every read/search access for observability —
        enables "what's frequently accessed?" and "what's stale?" queries.
        """
        conn = sqlite3.connect(self._db_path)
        conn.execute("CREATE TABLE IF NOT EXISTS refs (path TEXT NOT NULL, ts INTEGER NOT NULL)")
        conn.commit()
        conn.close()

    def read(self, path: str) -> tuple[dict, str]:
        """Read a brain file and return (frontmatter_dict, body_text).

        Records an access in the refs table for observability.

        Args:
            path: Relative path within the brain directory.

        Raises:
            ValueError: If path is invalid or file doesn't exist.
        """
        resolved = self._validate_path(path)
        if not resolved.is_file():
            raise ValueError(f"Not found: {path}")

        text = resolved.read_text(encoding="utf-8", errors="replace")
        fm, body = self._parse_frontmatter(text)
        self._record_ref(path)
        return fm, body

    def write(self, path: str, name: str, summary: str, tags: list[str], content: str) -> None:
        """Create or update a brain file with frontmatter.

        On update: merges provided fields into existing frontmatter (preserves
        fields not in the request). On create: generates new frontmatter.
        Updates the index after writing.

        Args:
            path: Relative path within the brain directory.
            name: Item title.
            summary: Brief summary for the index.
            tags: List of tags for search/categorisation.
            content: Markdown body content.
        """
        resolved = self._validate_path(path, allow_new=True)

        # Merge frontmatter on update, create fresh on new
        if resolved.is_file():
            existing_text = resolved.read_text(encoding="utf-8", errors="replace")
            fm, _ = self._parse_frontmatter(existing_text)
        else:
            fm = {}

        # Update frontmatter fields
        fm["name"] = name
        fm["summary"] = summary
        fm["tags"] = tags

        # Build the file content: frontmatter + body
        fm_text = yaml.safe_dump(fm, default_flow_style=False, sort_keys=True)
        file_content = f"---\n{fm_text}---\n{content}\n"

        # Ensure parent directory exists
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(file_content, encoding="utf-8")

        # Update the index entry
        self._update_index(path, name, summary, tags)

    def search(self, query: str, scope: str | None = None) -> list[dict]:
        """Two-phase search: index.yaml first, ripgrep fallback for body matches.

        Phase 1: Score matches against index entries (name, tags, summary).
        Phase 2: Use ripgrep to find body-level matches for terms not found in index.
        Results are deduplicated and sorted by score descending.

        Args:
            query: Search terms (space-separated).
            scope: Optional subdirectory to limit search (e.g. "projects").

        Returns:
            List of result dicts with keys: path, name, summary, tags, score.
        """
        # Tokenise query, remove stopwords
        terms = [t.lower() for t in query.split() if t.lower() not in _STOPWORDS]
        if not terms:
            return []

        index = self._load_index()
        scores: dict[str, dict] = {}

        # Phase 1: Score against index entries
        for item_type, items in index.items():
            # Apply scope filter
            if scope and item_type != scope:
                continue
            for slug, entry in items.items():
                score = 0
                entry_name = entry.get("name", "").lower()
                entry_tags = [t.lower() for t in entry.get("tags", [])]
                entry_summary = entry.get("summary", "").lower()

                for term in terms:
                    if term in entry_name:
                        score += 3
                    if any(term in tag for tag in entry_tags):
                        score += 2
                    if term in entry_summary:
                        score += 1

                if score > 0:
                    scores[entry["path"]] = {
                        "path": entry["path"],
                        "name": entry.get("name", slug),
                        "summary": entry.get("summary", ""),
                        "tags": entry.get("tags", []),
                        "score": score,
                    }

        # Phase 2: ripgrep fallback for body matches
        search_dir = self._brain_dir / scope if scope else self._brain_dir
        if search_dir.is_dir():
            for term in terms:
                try:
                    result = subprocess.run(
                        ["rg", "-l", "--ignore-case", term, str(search_dir)],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if result.returncode == 0:
                        for line in result.stdout.strip().splitlines():
                            file_path = Path(line)
                            # Only count .md files, skip _memory/ and .git/
                            if file_path.suffix != ".md":
                                continue
                            try:
                                rel = str(file_path.relative_to(self._brain_dir))
                            except ValueError:
                                continue
                            if rel.startswith("_memory") or rel.startswith(".git"):
                                continue

                            if rel in scores:
                                scores[rel]["score"] += 1
                            else:
                                # New result from rg — add with minimal info
                                scores[rel] = {
                                    "path": rel,
                                    "name": file_path.stem,
                                    "summary": "",
                                    "tags": [],
                                    "score": 1,
                                }
                except FileNotFoundError:
                    # rg not installed — skip body search phase
                    break

        # Sort by score descending, return top 20
        results = sorted(scores.values(), key=lambda x: x["score"], reverse=True)
        return results[:20]

    def commit(self, message: str, paths: list[str] | None = None) -> str:
        """Stage and commit changes to the brain git repository.

        Args:
            message: Commit message.
            paths: Specific paths to stage. If None, stages all changes.

        Returns:
            Commit result message (short hash or error).
        """
        if paths:
            # Validate and stage specific paths
            for p in paths:
                self._validate_path(p)
            subprocess.run(
                ["git", "add"] + paths,
                cwd=str(self._brain_dir),
                capture_output=True,
                check=False,
            )
        else:
            subprocess.run(
                ["git", "add", "-A"],
                cwd=str(self._brain_dir),
                capture_output=True,
                check=False,
            )

        result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=str(self._brain_dir),
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode == 0:
            # Extract short hash from commit output
            first_line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
            return f"Committed: {first_line}"
        elif "nothing to commit" in result.stdout:
            return "Nothing to commit (working tree clean)"
        else:
            return f"Commit failed: {result.stderr.strip()}"

    def _load_index(self) -> dict:
        """Read and parse index.yaml. Returns empty dict if missing/invalid."""
        if not self._index_path.exists():
            return {}
        try:
            data = yaml.safe_load(self._index_path.read_text())
            return data if isinstance(data, dict) else {}
        except yaml.YAMLError:
            return {}

    def _update_index(self, path: str, name: str, summary: str, tags: list[str]) -> None:
        """Update a single entry in index.yaml after a write.

        Determines the type (parent directory) and slug (filename stem)
        from the path and upserts into the index.
        """
        index = self._load_index()

        rel = Path(path)
        parts = rel.parts
        item_type = parts[0] if len(parts) > 1 else "root"
        slug = rel.stem

        if item_type not in index:
            index[item_type] = {}

        index[item_type][slug] = {
            "name": name,
            "path": path,
            "summary": summary,
            "tags": tags,
        }

        self._index_path.write_text(yaml.safe_dump(index, default_flow_style=False, sort_keys=True))

    def _build_index(self) -> dict:
        """Full rebuild of the index from filesystem. Scans all .md files
        with frontmatter and extracts metadata."""
        index: dict[str, dict] = {}

        for md_file in self._brain_dir.rglob("*.md"):
            rel = md_file.relative_to(self._brain_dir)
            parts = rel.parts
            if parts[0] in ("_memory", ".git"):
                continue

            text = md_file.read_text(encoding="utf-8", errors="replace")
            fm, _ = self._parse_frontmatter(text)
            if not fm:
                continue

            item_type = parts[0] if len(parts) > 1 else "root"
            slug = md_file.stem

            if item_type not in index:
                index[item_type] = {}

            index[item_type][slug] = {
                "name": fm.get("name", slug),
                "path": str(rel),
                "summary": fm.get("summary", ""),
                "tags": fm.get("tags", []),
            }

        return index

    @staticmethod
    def _parse_frontmatter(text: str) -> tuple[dict, str]:
        """Parse YAML frontmatter from a markdown file.

        Frontmatter is delimited by --- at the start and end.
        Returns (frontmatter_dict, body_text). If no valid frontmatter
        is found, returns ({}, full_text).
        """
        if not text.startswith("---"):
            return {}, text

        # Find closing delimiter (must be on its own line after the opening ---)
        match = re.match(r"^---\n(.*?)\n---\n?(.*)", text, re.DOTALL)
        if not match:
            return {}, text

        try:
            fm = yaml.safe_load(match.group(1))
            if not isinstance(fm, dict):
                return {}, text
            return fm, match.group(2)
        except yaml.YAMLError:
            return {}, text

    def _record_ref(self, path: str) -> None:
        """Record an access to a brain file in the refs table.

        Uses integer timestamp (epoch seconds) for compact storage.
        """
        conn = sqlite3.connect(self._db_path)
        conn.execute("INSERT INTO refs (path, ts) VALUES (?, ?)", (path, int(time.time())))
        conn.commit()
        conn.close()

    def _validate_path(self, path: str, allow_new: bool = False) -> Path:
        """Validate a path is safe and within the brain directory.

        Security checks:
        - Must resolve under brain_dir (no escaping via symlinks)
        - Rejects '..' path components
        - Blocks access to .git/, brain.db, and _memory/ (read-only for brain tool)

        Args:
            path: Relative path within the brain.
            allow_new: If True, the file doesn't need to exist (for writes).

        Raises:
            ValueError: If the path is invalid or blocked.
        """
        if ".." in path:
            raise ValueError(f"Invalid path (contains '..'): {path}")

        # Block protected paths
        normalized = Path(path).parts
        if normalized and normalized[0] in (".git",):
            raise ValueError(f"Access denied: {path} (protected)")
        if path == "brain.db":
            raise ValueError(f"Access denied: {path} (protected)")
        if normalized and normalized[0] == "_memory":
            raise ValueError(f"Access denied: {path} (_memory is read-only via brain tool)")

        resolved = (self._brain_dir / path).resolve()

        # Ensure the resolved path is under brain_dir
        try:
            resolved.relative_to(self._brain_dir.resolve())
        except ValueError:
            raise ValueError(f"Path escapes brain directory: {path}") from None

        return resolved
