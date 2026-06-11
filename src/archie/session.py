"""Session state and persistence.

A Session represents one conversation. It tracks:
- The sequence of turns (for building LLM context)
- Cumulative token usage and cost
- Context window utilisation

Persistence: single JSONL file per session at ~/.archie/sessions/{id}.jsonl
- One line per user turn (everything from prompt → tools → response)
- Append-only — each turn is flushed when the agent loop completes it
- No header — session metadata derivable from filename and turn data

The session ID format is: YYYY-MM-DD-{project}-{hash}
(e.g. 2026-06-08-archie-nextgen-d8c3b)

In-memory state (session.turns) is separate from persistence (flush_turn).
add_turn() updates memory for context building. flush_turn() writes to disk.
"""

import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ulid import ULID

from archie.config import SESSIONS_DIR
from archie.models import ModelInfo, calculate_cost
from archie.types import ContentBlock, TextBlock


@dataclass
class Turn:
    """A single conversational turn (in-memory, for LLM context building).

    Attributes:
        id: Sequential identifier (t0001, t0002, ...) for ordering.
        role: "user" or "assistant" — maps to the LLM's message roles.
        content: List of content blocks.
        input_tokens: Tokens reported for this request's input.
        output_tokens: Tokens the model generated.
        timestamp: ISO 8601 timestamp.
        interrupted: True if the user cancelled generation.
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
class TurnLog:
    """Accumulated data for one user turn, written to the JSONL log.

    Built up by the agent loop during run_turn, then passed to session.flush_turn().
    """

    when: str
    user: str
    assistant_text: str = ""
    tools: list[dict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    model: str = ""
    interrupted: bool = False


@dataclass
class Session:
    """Manages conversation state and persistence.

    In-memory state (turns list) is used for building LLM context.
    Persistence (flush_turn) writes completed turns to a JSONL file.
    These are decoupled — add_turn is memory-only, flush_turn is disk-only.
    """

    model_id: str
    model_info: ModelInfo
    project_name: str = ""
    session_id: str = ""
    turns: list[Turn] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_write_tokens: int = 0

    _last_input_tokens: int = field(default=0, repr=False)
    _turn_counter: int = field(default=0, repr=False)
    _log_path: Path | None = field(default=None, repr=False)

    def __post_init__(self):
        """Generate a session ID if one wasn't provided."""
        if not self.session_id:
            now = datetime.now(UTC)
            date_str = now.strftime("%Y-%m-%d")
            project = self.project_name or "general"
            short_hash = secrets.token_hex(3)[:5]
            self.session_id = f"{date_str}-{project}-{short_hash}"

    @property
    def log_path(self) -> Path:
        """Path to the JSONL log file."""
        if self._log_path is None:
            self._log_path = SESSIONS_DIR / f"{self.session_id}.jsonl"
        return self._log_path

    @property
    def total_cost(self) -> float:
        """Total USD spent in this session across all turns."""
        return calculate_cost(
            self.model_info,
            self.total_input_tokens,
            self.total_output_tokens,
            self.total_cache_read_tokens,
            self.total_cache_write_tokens,
        )

    @property
    def _estimated_next_input(self) -> int:
        """Rough estimate of the next request's input token count."""
        return self._last_input_tokens + (self.turns[-1].output_tokens if self.turns else 0)

    @property
    def context_pct(self) -> float:
        """Estimated context window usage for the NEXT request (0-100)."""
        return (self._estimated_next_input / self.model_info.max_context_tokens) * 100

    @property
    def context_warning(self) -> bool:
        """True if we're approaching the model's context limit."""
        return (
            self._estimated_next_input
            > self.model_info.max_context_tokens * self.model_info.context_warning_threshold
        )

    def add_turn(
        self,
        role: str,
        content: str | list[ContentBlock],
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        interrupted: bool = False,
    ) -> Turn:
        """Record a turn in memory (for LLM context building). Does NOT write to disk.

        Args:
            role: "user" or "assistant"
            content: Plain string (wrapped in [TextBlock]) or list of ContentBlocks.
            input_tokens: Fresh input token count from LLM response.
            output_tokens: Output token count from LLM response.
            cache_read_tokens: Cache-read input tokens (cheap).
            cache_write_tokens: Cache-write input tokens (premium).
            interrupted: Whether generation was cancelled.
        """
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
        self.total_cache_read_tokens += cache_read_tokens
        self.total_cache_write_tokens += cache_write_tokens

        if input_tokens > 0:
            self._last_input_tokens = input_tokens

        return turn

    def flush_turn(self, turn_log: TurnLog) -> None:
        """Write a completed turn to the JSONL log file. Append-only.

        Called by the agent loop at the end of each user turn.
        Creates the sessions directory and file on first write.
        """
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

        cost = calculate_cost(
            self.model_info,
            turn_log.input_tokens,
            turn_log.output_tokens,
            turn_log.cache_read_tokens,
            turn_log.cache_write_tokens,
        )

        entry = {
            "id": str(ULID()),
            "when": turn_log.when,
            "user": turn_log.user,
            "assistant": turn_log.assistant_text or None,
            "metadata": {
                "model": turn_log.model or self.model_id,
                "input_tokens": turn_log.input_tokens,
                "output_tokens": turn_log.output_tokens,
                "cache_read_tokens": turn_log.cache_read_tokens,
                "cache_write_tokens": turn_log.cache_write_tokens,
                "cost": round(cost, 6),
                "interrupted": turn_log.interrupted,
            },
        }

        # Only include tools if there were any
        if turn_log.tools:
            entry["tools"] = turn_log.tools

        # Remove None assistant (omit rather than null)
        if entry["assistant"] is None:
            del entry["assistant"]

        with self.log_path.open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def summarise_tool_output(name: str, tool_input: dict, output: str, is_error: bool) -> str:
    """Produce a short summary of tool output for the session log.

    Captures the *result* in a compact form — the full input is stored separately.
    Called before truncation so we have access to the complete output.
    """
    if is_error:
        # First line of error, capped
        return output.split("\n")[0][:100]

    match name:
        case "read_file":
            lines = output.count("\n")
            return f"{lines} lines"
        case "write_file" | "edit_file":
            # These already return concise messages like "Written: path (42 lines)"
            return output[:100]
        case "list_files":
            file_count = output.strip().count("\n") + 1 if output.strip() else 0
            return f"{file_count} files"
        case "search_files":
            match_count = output.count("\n")
            return f"{match_count} matches"
        case "shell":
            # Parse exit code from our format "[exit: N]"
            lines = output.strip().split("\n")
            exit_line = next((line for line in lines if "[exit:" in line), "[exit: ?]")
            output_lines = max(0, len(lines) - 2)
            return f"{exit_line.strip()}, {output_lines} lines"
        case _:
            return f"{len(output)} chars"
