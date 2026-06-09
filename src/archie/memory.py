"""Memory extraction — automatic fragment extraction from session logs.

Reads session JSONL files, sends unextracted turns to a cheap model (Haiku),
and writes structured memory fragments to daily per-project JSONL files.

Key design decisions:
- Watermark tracking via ULID of last extracted turn (crash-safe, idempotent)
- Daily per-project JSONL files for easy date-range pre-filtering on recall
- Liberal extraction — better to over-capture than miss something
- Last 3 fragments included in prompt for topic continuity
"""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from ulid import ULID

from archie.config import SESSIONS_DIR
from archie.llm.bedrock import BedrockClient

log = logging.getLogger(__name__)

_EXTRACTION_PROMPT = """\
Extract knowledge fragments from these conversation turns.

## What to capture

Capture LIBERALLY. It is better to over-capture than to miss something. Include:
- Decisions made (explicit or implicit) and the reasoning behind them
- Discussion about tradeoffs, even if no conclusion was reached
- Things that were tried and didn't work (and why)
- User preferences or working style observations
- Technical insights, patterns, or approaches discovered
- Project progress and state changes
- Questions raised that remain open
- Context that would help a future session understand what happened

## What to skip

Only skip turns that are purely mechanical with zero informational value:
- Running a linter/formatter with no discussion
- Fixing a single typo with no context
- Tool calls that just read files without any resulting insight

When in doubt, INCLUDE IT. A fragment that turns out to be low-value costs nothing. \
A missing fragment that was needed is unrecoverable.

## Output format

For each fragment provide: type (decision/learning/preference/state/context), \
topic (short label — reuse existing topics where the subject is the same), \
content (1-3 sentences capturing the key information), and tags.

Return an empty array ONLY if the turns are entirely mechanical (e.g. only tool calls \
with no discussion).

Return as a JSON array. Example:
[{"type": "decision", "topic": "session format", "content": "Use JSONL.", "tags": ["logging"]}]
"""


class MemoryExtractor:
    """Extracts memory fragments from session logs using a cheap LLM.

    Args:
        brain_dir: Root brain directory (memory lives at brain_dir/_memory/).
        extraction_model: Bedrock model ID for extraction (e.g. Haiku).
        region: AWS region for Bedrock API calls.
    """

    def __init__(self, brain_dir: Path, extraction_model: str, region: str) -> None:
        self._memory_dir = brain_dir / "_memory"
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        self._watermark_path = self._memory_dir / ".last_extracted"
        self._client = BedrockClient(extraction_model, region)

    def extract_all(self) -> int:
        """Process all unextracted turns from all session files. Returns fragment count."""
        watermarks = self._load_watermarks()
        total_fragments = 0

        if not SESSIONS_DIR.exists():
            return 0

        for session_file in sorted(SESSIONS_DIR.glob("*.jsonl")):
            session_id = session_file.stem
            last_id = watermarks.get(session_id, {}).get("turn_id", "")

            # Read turns from session, skip already-extracted
            turns = []
            last_turn_id = last_id
            for line in session_file.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("Skipping corrupt line in %s", session_file.name)
                    continue
                turn_id = entry.get("id", "")
                if last_id and turn_id <= last_id:
                    continue
                turns.append(entry)
                last_turn_id = turn_id

            if not turns:
                continue

            # Extract project from session_id (format: YYYY-MM-DD-{project}-{hash})
            parts = session_id.split("-")
            project = "-".join(parts[3:-1]) if len(parts) > 4 else "general"

            fragments = self._extract_turns(session_id, project, turns)
            if fragments:
                self._write_fragments(project, fragments)
                total_fragments += len(fragments)

            # Update watermark even if no fragments (turns were mechanical)
            watermarks[session_id] = {
                "turn_id": last_turn_id,
                "extracted_at": datetime.now(UTC).isoformat(),
            }

        self._save_watermarks(watermarks)
        return total_fragments

    def _extract_turns(self, session_id: str, project: str, turns: list[dict]) -> list[dict]:
        """Call extraction model on turns, parse JSON response into fragments."""
        # Build context from last 3 fragments
        last_fragments = self._get_last_fragments(project, n=3)
        context_text = ""
        if last_fragments:
            context_text = "Here are the most recent memory entries for context:\n"
            for f in last_fragments:
                context_text += f"[{f.get('type')}] {f.get('topic')}: {f.get('content')}\n"
            context_text += "\n"

        # Format turns for the prompt
        turns_text = ""
        for t in turns:
            user_msg = t.get("user", "")
            assistant_msg = t.get("assistant", "")
            tools = t.get("tools", [])
            if user_msg:
                turns_text += f"User: {user_msg}\n"
            if tools:
                for tool in tools:
                    turns_text += f"Tool [{tool.get('name')}]: {tool.get('summary', '')}\n"
            if assistant_msg:
                turns_text += f"Assistant: {assistant_msg}\n"
            turns_text += "\n"

        system = _EXTRACTION_PROMPT
        user_content = f"{context_text}Extract knowledge fragments from these conversation turns:\n\n{turns_text}"

        messages = [{"role": "user", "content": [{"text": user_content}]}]

        try:
            response_text = self._client.invoke(messages, system)
        except Exception:
            log.warning("Extraction call failed for session %s", session_id, exc_info=True)
            return []

        # Parse JSON response
        try:
            # Handle markdown code fences around JSON
            text = response_text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            raw_fragments = json.loads(text)
        except (json.JSONDecodeError, IndexError):
            log.warning("Malformed extraction response for session %s", session_id)
            return []

        if not isinstance(raw_fragments, list):
            return []

        # Build proper fragments with IDs
        fragments = []
        for raw in raw_fragments:
            if not isinstance(raw, dict) or "type" not in raw:
                continue
            fragments.append(
                {
                    "id": str(ULID()),
                    "session_id": session_id,
                    "type": raw.get("type", "context"),
                    "topic": raw.get("topic", ""),
                    "content": raw.get("content", ""),
                    "tags": raw.get("tags", []),
                }
            )
        return fragments

    def _get_last_fragments(self, project: str, n: int = 3) -> list[dict]:
        """Read last N fragments for a project (from most recent JSONL files)."""
        pattern = f"*-{project}.jsonl"
        files = sorted(self._memory_dir.glob(pattern), reverse=True)
        fragments: list[dict] = []
        for f in files:
            for line in reversed(f.read_text().splitlines()):
                if not line.strip():
                    continue
                try:
                    fragments.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
                if len(fragments) >= n:
                    return fragments
        return fragments

    def _write_fragments(self, project: str, fragments: list[dict]) -> None:
        """Append fragments to the daily per-project JSONL file."""
        date_str = datetime.now(UTC).strftime("%Y-%m-%d")
        path = self._memory_dir / f"{date_str}-{project}.jsonl"
        with path.open("a") as f:
            for fragment in fragments:
                f.write(json.dumps(fragment, ensure_ascii=False) + "\n")

    def _load_watermarks(self) -> dict:
        """Read .last_extracted JSON. Returns empty dict if missing/corrupt."""
        if not self._watermark_path.exists():
            return {}
        try:
            return json.loads(self._watermark_path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_watermarks(self, watermarks: dict) -> None:
        """Write .last_extracted JSON."""
        self._watermark_path.write_text(json.dumps(watermarks, indent=2))
