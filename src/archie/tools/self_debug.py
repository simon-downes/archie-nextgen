"""self_debug tool — lets the model inspect its own debug log.

The debug log (~/.archie/nextgen.log) is structured JSONL: every record has
ts/level/logger plus event fields (turn_start, request_end, tool_end, …) and
ambient context (session, turn, iteration). This tool gives the model a
filtered tail of that log so it can diagnose its own behaviour — errors,
slow requests, cache misses, retries — instead of speculating.

Design choices:
- Host-side file read: the sandbox deliberately doesn't mount ~/.archie
  (sessions/config live there), so this can't go through the shell tool.
- Current log file only — rotated backups are out of scope (v1).
- Defaults to the current session's records; session="all" widens the view.
- self_truncating: output is capped internally by dropping oldest records,
  so a large tail never blows the context budget.
- The payload log (payloads.log) is never read — full request dumps entering
  context would amplify themselves on the next request.
"""

import json
import re
from collections.abc import Callable
from pathlib import Path

from archie.tools import ToolSpec, tool_error, tool_result

_DEFAULT_TAIL = 50
_MAX_TAIL = 500
_OUTPUT_BUDGET = 8000  # chars — keep well under typical context budgets

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


def make_self_debug_spec(log_path: Path, session_id_fn: Callable[[], str]) -> ToolSpec:
    """Create the self_debug ToolSpec.

    Args:
        log_path: Path to the JSONL debug log.
        session_id_fn: Callable returning the *current* session id. A callable
            (not a value) because the app can recreate the session (new_session)
            while the registry lives on.
    """

    def handler(params: dict) -> str:
        tail = min(int(params.get("tail", _DEFAULT_TAIL)), _MAX_TAIL)
        level_name = str(params.get("level", "DEBUG")).upper()
        event = params.get("event", "")
        pattern = params.get("pattern", "")
        session = params.get("session", "current")

        if level_name not in _LEVELS:
            return tool_error(f"Unknown level '{level_name}'. One of: {', '.join(_LEVELS)}")
        min_level = _LEVELS[level_name]

        try:
            regex = re.compile(pattern) if pattern else None
        except re.error as e:
            return tool_error(f"Invalid regex: {e}")

        if not log_path.exists():
            return tool_error(f"Debug log not found: {log_path}")

        current_session = session_id_fn()
        matched: list[str] = []
        skipped_malformed = 0

        try:
            with log_path.open(encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        skipped_malformed += 1
                        continue

                    if _LEVELS.get(rec.get("level", ""), 0) < min_level:
                        continue
                    if session == "current" and rec.get("session") != current_session:
                        continue
                    if event and rec.get("event") != event:
                        continue
                    if regex and not regex.search(line):
                        continue
                    matched.append(line)
        except OSError as e:
            return tool_error(f"Could not read debug log: {e}")

        total_matched = len(matched)
        matched = matched[-tail:]

        # Enforce the output budget by dropping oldest records.
        dropped_for_budget = 0
        while matched and sum(len(line) + 1 for line in matched) > _OUTPUT_BUDGET:
            matched.pop(0)
            dropped_for_budget += 1

        if not matched:
            note = f" ({skipped_malformed} malformed lines skipped)" if skipped_malformed else ""
            return tool_result(f"No matching log records.{note}")

        header = f"{len(matched)} of {total_matched} matching records (newest last)"
        if dropped_for_budget:
            header += f"; {dropped_for_budget} more dropped to fit output budget"
        if skipped_malformed:
            header += f"; {skipped_malformed} malformed lines skipped"

        return tool_result(header + "\n" + "\n".join(matched))

    return ToolSpec(
        name="self_debug",
        description=(
            "Read your own debug log (structured JSONL): LLM requests (timing, tokens, "
            "cache hits, stop reasons), tool executions (duration, result size, errors), "
            "turn lifecycle, retries, and failures. Use this to diagnose unexpected "
            "behaviour, latency, cost, or errors in your own operation. "
            "Defaults to the current session's records."
        ),
        schema={
            "type": "object",
            "properties": {
                "tail": {
                    "type": "integer",
                    "description": f"Return the most recent N matching records "
                    f"(default {_DEFAULT_TAIL}, max {_MAX_TAIL})",
                },
                "level": {
                    "type": "string",
                    "enum": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                    "description": "Minimum level (default DEBUG)",
                },
                "event": {
                    "type": "string",
                    "description": "Filter to one event type, e.g. 'request_end', "
                    "'tool_end', 'turn_end', 'interrupt'",
                },
                "pattern": {
                    "type": "string",
                    "description": "Regex matched against the raw JSON line",
                },
                "session": {
                    "type": "string",
                    "enum": ["current", "all"],
                    "description": "Restrict to the current session (default) or all sessions",
                },
            },
        },
        handler=handler,
        self_truncating=True,
    )
