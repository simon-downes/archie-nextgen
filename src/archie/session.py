"""Session state and persistence.

A Session represents one conversation. It tracks:
- The sequence of turns (user messages and assistant responses)
- Cumulative token usage and cost
- Context window utilisation (to warn before hitting limits)

Persistence strategy:
- turns.jsonl: append-only log, one JSON line per turn (lightweight, greppable)
- raw/{turn-id}.json: full content for large turns (keeps turns.jsonl small)
- meta.json: summary stats, rewritten after each turn

The split means you can review a session quickly from turns.jsonl without
loading potentially huge assistant responses. The raw/ directory holds the
full content when you need it.
"""

import json
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from archie.config import SESSIONS_DIR
from archie.models import ModelInfo, calculate_cost


@dataclass
class Turn:
    """A single conversational turn (one message from user or assistant).

    Attributes:
        id: Sequential identifier (t0001, t0002, ...) for linking to raw files.
        role: "user" or "assistant" — maps to Bedrock's message roles.
        content: The full text of the message.
        input_tokens: Tokens Bedrock reported for this request's input.
            Only populated on assistant turns (that's when we get the API response).
            This number includes ALL prior messages re-sent, not just this turn.
        output_tokens: Tokens the model generated for this response.
        timestamp: ISO 8601 timestamp of when the turn was recorded.
        interrupted: True if the user cancelled generation mid-stream.
    """

    id: str
    role: str
    content: str
    input_tokens: int = 0
    output_tokens: int = 0
    timestamp: str = ""
    interrupted: bool = False


@dataclass
class Session:
    """Manages conversation state and persistence.

    Lifecycle:
    1. Created when the app starts (or when user hits Ctrl+N for new session)
    2. Session directory is NOT created until the first message is sent
       (avoids empty session dirs from just opening and closing the app)
    3. Each turn is appended to disk immediately (crash-safe)
    4. meta.json is rewritten after each turn with cumulative stats
    """

    model_id: str
    model_info: ModelInfo
    session_id: str = ""
    turns: list[Turn] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0

    # Private fields (not shown in repr, not part of the public interface)
    _dir: Path | None = field(default=None, repr=False)
    _last_input_tokens: int = field(default=0, repr=False)
    _turn_counter: int = field(default=0, repr=False)

    def __post_init__(self):
        """Generate a session ID if one wasn't provided.

        Format: YYYYMMDD-HHMM-xxxx (date, time, 4 random hex chars).
        Sortable by date, random suffix prevents collisions if you create
        two sessions in the same minute.
        """
        if not self.session_id:
            now = datetime.now(UTC)
            rand = f"{random.randint(0, 0xFFFF):04x}"
            self.session_id = f"{now.strftime('%Y%m%d')}-{now.strftime('%H%M')}-{rand}"

    @property
    def dir(self) -> Path:
        """Lazy-computed session directory path."""
        if self._dir is None:
            self._dir = SESSIONS_DIR / self.session_id
        return self._dir

    @property
    def total_cost(self) -> float:
        """Total USD spent in this session across all turns."""
        return calculate_cost(self.model_info, self.total_input_tokens, self.total_output_tokens)

    @property
    def context_pct(self) -> float:
        """Estimated context window usage for the NEXT request (0-100).

        Why _last_input_tokens and not a cumulative sum?
        Because Bedrock is stateless — each request re-sends the entire history.
        The input_tokens reported by the API already counts the full history.
        So the next request's context ≈ last_input + last_output (the new
        assistant response becomes part of the next request's input).
        """
        estimated_next = self._last_input_tokens + (
            self.turns[-1].output_tokens if self.turns else 0
        )
        return (estimated_next / self.model_info.max_context_tokens) * 100

    @property
    def context_warning(self) -> bool:
        """True if we're approaching the model's context limit."""
        estimated_next = self._last_input_tokens + (
            self.turns[-1].output_tokens if self.turns else 0
        )
        return (
            estimated_next
            > self.model_info.max_context_tokens * self.model_info.context_warning_threshold
        )

    @property
    def messages(self) -> list[dict]:
        """Return turns in Bedrock's converse API message format.

        Bedrock expects: [{"role": "user", "content": [{"text": "..."}]}, ...]
        The content is a list of "content blocks" — currently just text,
        but will include toolUse/toolResult blocks when we add tools.
        """
        return [{"role": t.role, "content": [{"text": t.content}]} for t in self.turns]

    def add_turn(
        self,
        role: str,
        content: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        interrupted: bool = False,
    ) -> Turn:
        """Record a new turn and persist it to disk.

        Called twice per exchange:
        1. User turn: role="user", content=what they typed, no token counts
        2. Assistant turn: role="assistant", content=model's response,
           input/output tokens from the API response
        """
        self._turn_counter += 1
        turn = Turn(
            id=f"t{self._turn_counter:04d}",
            role=role,
            content=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            timestamp=datetime.now(UTC).isoformat(),
            interrupted=interrupted,
        )
        self.turns.append(turn)
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens

        # Track the most recent input token count for context_pct calculation.
        # Only update when we actually have a count (assistant turns from Bedrock).
        if input_tokens > 0:
            self._last_input_tokens = input_tokens

        self._persist_turn(turn)
        self._save_meta()
        return turn

    def _ensure_dir(self) -> None:
        """Create session directory structure on first write."""
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "raw").mkdir(exist_ok=True)

    def _persist_turn(self, turn: Turn) -> None:
        """Append a turn to turns.jsonl and optionally write raw content.

        turns.jsonl gets a summary (first 200 chars) — enough to scan the
        session without loading full responses. If the content exceeds 500
        chars, the full text goes to raw/{turn-id}.json.
        """
        self._ensure_dir()

        # Truncate for the summary log — full content is in raw/ if needed
        summary = turn.content[:200] if len(turn.content) <= 200 else turn.content[:197] + "..."
        entry = {
            "id": turn.id,
            "ts": turn.timestamp,
            "role": turn.role,
            "summary": summary,
            "input_tokens": turn.input_tokens,
            "output_tokens": turn.output_tokens,
            "interrupted": turn.interrupted,
        }

        # Append-only write — crash-safe, no corruption of previous entries
        with (self.dir / "turns.jsonl").open("a") as f:
            f.write(json.dumps(entry) + "\n")

        # Write full content to a separate file for large turns.
        # 500 chars is roughly where a response becomes too long to skim in JSONL.
        if len(turn.content) > 500:
            raw_path = self.dir / "raw" / f"{turn.id}.json"
            raw_path.write_text(json.dumps({"content": turn.content}))

    def _save_meta(self) -> None:
        """Rewrite meta.json with current session stats.

        This is NOT append-only — it's rewritten every turn because it
        contains cumulative totals. It's the "at a glance" file for a session.
        """
        self._ensure_dir()
        meta = {
            "session_id": self.session_id,
            "model": self.model_id,
            "created_at": self.turns[0].timestamp if self.turns else None,
            "updated_at": self.turns[-1].timestamp if self.turns else None,
            "total_turns": len(self.turns),
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cost": round(self.total_cost, 6),
        }
        (self.dir / "meta.json").write_text(json.dumps(meta, indent=2))
