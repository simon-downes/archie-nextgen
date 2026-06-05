"""Session state and persistence.

A Session represents one conversation. It tracks:
- The sequence of turns (user messages, assistant responses, tool results)
- Cumulative token usage and cost
- Context window utilisation (to warn before hitting limits)

Persistence strategy:
- turns.jsonl: append-only log, one JSON line per turn (lightweight, greppable)
- raw/{turn-id}.json: full content for large turns (keeps turns.jsonl small)
- meta.json: summary stats, rewritten after each turn

Turn content is stored as a list of ContentBlocks — this supports mixed content
like text + tool calls in a single assistant response, or tool results that
need to reference specific tool_use_ids.
"""

import json
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from archie.config import SESSIONS_DIR
from archie.models import ModelInfo, calculate_cost
from archie.types import ContentBlock, TextBlock, ToolResultBlock, ToolUseBlock


@dataclass
class Turn:
    """A single conversational turn.

    Attributes:
        id: Sequential identifier (t0001, t0002, ...) for linking to raw files.
        role: "user" or "assistant" — maps to the LLM's message roles.
        content: List of content blocks. A simple text message is [TextBlock(text)].
            An assistant response with tool calls might be [TextBlock, ToolUseBlock, ToolUseBlock].
            A tool result turn is [ToolResultBlock, ToolResultBlock, ...].
        input_tokens: Tokens the LLM reported for this request's input.
            Only populated on assistant turns (that's when we get the API response).
        output_tokens: Tokens the model generated for this response.
        timestamp: ISO 8601 timestamp of when the turn was recorded.
        interrupted: True if the user cancelled generation mid-stream.
    """

    id: str
    role: str
    content: list[ContentBlock]
    input_tokens: int = 0
    output_tokens: int = 0
    timestamp: str = ""
    interrupted: bool = False

    @property
    def text(self) -> str:
        """Extract the text content from this turn (first TextBlock, or empty)."""
        for block in self.content:
            if isinstance(block, TextBlock):
                return block.text
        return ""


@dataclass
class Session:
    """Manages conversation state and persistence.

    Lifecycle:
    1. Created when the app starts (or when user hits Ctrl+N for new session)
    2. Session directory is NOT created until the first message is sent
    3. Each turn is appended to disk immediately (crash-safe)
    4. meta.json is rewritten after each turn with cumulative stats
    """

    model_id: str
    model_info: ModelInfo
    session_id: str = ""
    turns: list[Turn] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0

    _dir: Path | None = field(default=None, repr=False)
    _last_input_tokens: int = field(default=0, repr=False)
    _turn_counter: int = field(default=0, repr=False)

    def __post_init__(self):
        """Generate a session ID if one wasn't provided."""
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
        """Estimated context window usage for the NEXT request (0-100)."""
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

    def add_turn(
        self,
        role: str,
        content: str | list[ContentBlock],
        input_tokens: int = 0,
        output_tokens: int = 0,
        interrupted: bool = False,
    ) -> Turn:
        """Record a new turn and persist it to disk.

        Args:
            role: "user" or "assistant"
            content: Either a plain string (wrapped in [TextBlock]) or a list
                of ContentBlocks for multi-block turns (tool calls, tool results).
            input_tokens: Token count from the LLM API response.
            output_tokens: Token count from the LLM API response.
            interrupted: Whether generation was cancelled mid-stream.
        """
        # Convenience: accept plain string and wrap in TextBlock
        if isinstance(content, str):
            blocks = [TextBlock(text=content)]
        else:
            blocks = content

        self._turn_counter += 1
        turn = Turn(
            id=f"t{self._turn_counter:04d}",
            role=role,
            content=blocks,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            timestamp=datetime.now(UTC).isoformat(),
            interrupted=interrupted,
        )
        self.turns.append(turn)
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens

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
        """Append a turn to turns.jsonl and optionally write raw content."""
        self._ensure_dir()

        # Build a summary for the JSONL log
        summary = self._summarise_content(turn.content)
        entry = {
            "id": turn.id,
            "ts": turn.timestamp,
            "role": turn.role,
            "summary": summary,
            "input_tokens": turn.input_tokens,
            "output_tokens": turn.output_tokens,
            "interrupted": turn.interrupted,
        }

        with (self.dir / "turns.jsonl").open("a") as f:
            f.write(json.dumps(entry) + "\n")

        # Write full content to raw/ for large turns
        serialized = _serialize_blocks(turn.content)
        if len(json.dumps(serialized)) > 500:
            raw_path = self.dir / "raw" / f"{turn.id}.json"
            raw_path.write_text(json.dumps({"content": serialized}, indent=2))

    def _save_meta(self) -> None:
        """Rewrite meta.json with current session stats."""
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

    @staticmethod
    def _summarise_content(blocks: list[ContentBlock], max_len: int = 200) -> str:
        """Create a short summary of content blocks for the JSONL log.

        For text: first 200 chars.
        For tool_use: "tool: <name>(<args summary>)"
        For tool_result: "result: <first 100 chars>"
        """
        parts = []
        for block in blocks:
            if isinstance(block, TextBlock):
                text = block.text[:max_len]
                if len(block.text) > max_len:
                    text = text[: max_len - 3] + "..."
                parts.append(text)
            elif isinstance(block, ToolUseBlock):
                args_str = json.dumps(block.input)[:80]
                parts.append(f"tool: {block.name}({args_str})")
            elif isinstance(block, ToolResultBlock):
                status = "error" if block.is_error else "ok"
                parts.append(f"result[{status}]: {block.content[:100]}")
        return " | ".join(parts)[:max_len]


def _serialize_blocks(blocks: list[ContentBlock]) -> list[dict]:
    """Serialize content blocks to JSON-compatible dicts with type discriminators.

    Format:
        TextBlock       → {"type": "text", "text": "..."}
        ToolUseBlock    → {"type": "tool_use", "id": "...", "name": "...", "input": {...}}
        ToolResultBlock → {"type": "tool_result", "id": "...", "content": "...", "is_error": bool}
    """
    result = []
    for block in blocks:
        if isinstance(block, TextBlock):
            result.append({"type": "text", "text": block.text})
        elif isinstance(block, ToolUseBlock):
            result.append(
                {
                    "type": "tool_use",
                    "id": block.tool_use_id,
                    "name": block.name,
                    "input": block.input,
                }
            )
        elif isinstance(block, ToolResultBlock):
            result.append(
                {
                    "type": "tool_result",
                    "id": block.tool_use_id,
                    "content": block.content,
                    "is_error": block.is_error,
                }
            )
    return result
