"""Structured JSONL logging infrastructure.

Every record in ~/.archie/nextgen.log is one JSON object per line:

    {"ts": "2025-01-01T12:00:00.123Z", "level": "INFO", "logger": "archie.agent",
     "event": "turn_end", "session": "abc123", "turn": 3, ...}

Design:
- JsonFormatter serialises LogRecords to JSON. Anything passed via `extra={}`
  becomes a top-level field. Free-text messages land in `msg`; machine events
  use an `event` field (via the log_event helper) and usually have no msg.
- ContextFilter injects ambient context (session, turn, iteration) from a
  ContextVar into every record, regardless of which module emitted it.
  AgentLoop binds the context at turn/iteration boundaries; modules don't
  need to know it exists.
- Payload logger: full Bedrock request dumps are large and O(n²) over a
  session, so they go to a separate payloads.log, enabled only when
  ARCHIE_LOG_PAYLOADS=1. It shares the ContextFilter so payload records
  are joinable to the main log via session/turn/iteration.

Usage:
    log = logging.getLogger(__name__)           # unchanged convention
    log_event(log, logging.INFO, "turn_end", status="complete", cost=0.01)
    log.warning("free text still works", extra={"detail": 42})
"""

import json
import logging
import os
from contextvars import ContextVar
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path.home() / ".archie"
LOG_PATH = LOG_DIR / "nextgen.log"
PAYLOAD_LOG_PATH = LOG_DIR / "payloads.log"

PAYLOAD_LOGGER_NAME = "archie.payloads"
PAYLOADS_ENV_VAR = "ARCHIE_LOG_PAYLOADS"

# Ambient context bound by AgentLoop (session/turn/iteration) and injected
# into every LogRecord by ContextFilter. ContextVar is thread-safe: the
# worker thread running the turn sees its own binding.
# Default is None (not a mutable {}) per ruff B039; treat None as empty.
_ctx: ContextVar[dict | None] = ContextVar("archie_log_ctx", default=None)

# LogRecord attributes that are NOT user-supplied extras. Anything on the
# record outside this set came from `extra={}` and is emitted as a JSON field.
_RECORD_DEFAULTS = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName", "message",
    }
)  # fmt: skip


_payloads_enabled = False


def payloads_enabled() -> bool:
    """True when ARCHIE_LOG_PAYLOADS=1 was set at setup time."""
    return _payloads_enabled


def bind(**fields) -> None:
    """Merge fields into the ambient logging context (e.g. session=…, turn=…).

    A value of None removes the key.
    """
    current = dict(_ctx.get() or {})
    for key, value in fields.items():
        if value is None:
            current.pop(key, None)
        else:
            current[key] = value
    _ctx.set(current)


def clear() -> None:
    """Drop all ambient logging context. Called at turn end."""
    _ctx.set(None)


# Keys that collide with reserved LogRecord attributes if passed via extra={}.
# log_event renames them rather than crashing (logging must never break the app).
_RESERVED_EXTRA = frozenset({"name", "msg", "args", "message", "asctime", "exc_info"})


def log_event(
    log: logging.Logger, level: int, event: str, exc_info: bool = False, **fields
) -> None:
    """Emit a structured event record. Sugar for log.log(level, "", extra=…).

    Fields colliding with reserved LogRecord attributes are prefixed with
    'field_' instead of raising.
    """
    safe = {(f"field_{k}" if k in _RESERVED_EXTRA else k): v for k, v in fields.items()}
    log.log(level, "", extra={"event": event, **safe}, exc_info=exc_info)


class ContextFilter(logging.Filter):
    """Copies the ambient context (session/turn/iteration) onto every record.

    Explicit extras win over ambient context — if a call site passes
    `extra={"turn": 5}`, the bound value doesn't overwrite it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in (_ctx.get() or {}).items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Never raises — falls back to repr on bad fields."""

    def format(self, record: logging.LogRecord) -> str:
        out: dict = {
            "ts": datetime.fromtimestamp(record.created, UTC).strftime("%Y-%m-%dT%H:%M:%S.")
            + f"{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
        }

        # Free-text message (printf-style args applied). Empty for pure events.
        msg = record.getMessage()
        if msg:
            out["msg"] = msg

        # Extras: everything not in the default LogRecord attribute set.
        # Context fields (session/turn/iteration) arrive here too, via ContextFilter.
        for key, value in record.__dict__.items():
            if key not in _RECORD_DEFAULTS and not key.startswith("_"):
                out[key] = value

        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            out["stack"] = record.stack_info

        try:
            return json.dumps(out, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            # Last resort: stringify everything. Logging must never crash the app.
            return json.dumps({k: repr(v) for k, v in out.items()}, ensure_ascii=False)


def setup_logging() -> None:
    """Configure always-on JSONL debug logging to a rotating file.

    Called before anything else so even startup failures leave a trace.
    Output goes to ~/.archie/nextgen.log, never to stdout/stderr (Textual owns
    the terminal). Botocore/urllib3 are suppressed to WARNING — their DEBUG
    output is auth noise.

    Also configures the payload logger (~/.archie/payloads.log) when
    ARCHIE_LOG_PAYLOADS=1 — full Bedrock request dumps, off by default
    because they grow O(n²) with conversation length.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    handler = RotatingFileHandler(
        LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(JsonFormatter())
    handler.addFilter(ContextFilter())
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)

    # Suppress noisy third-party loggers — only archie's own logs at DEBUG
    for noisy in ("botocore", "urllib3", "boto3", "markdown_it"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Payload logger: separate file, opt-in, never propagates to the main log.
    global _payloads_enabled
    payload_log = logging.getLogger(PAYLOAD_LOGGER_NAME)
    payload_log.propagate = False
    if os.environ.get(PAYLOADS_ENV_VAR) == "1":
        _payloads_enabled = True
        payload_handler = RotatingFileHandler(
            PAYLOAD_LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=1, encoding="utf-8"
        )
        payload_handler.setFormatter(JsonFormatter())
        payload_handler.addFilter(ContextFilter())
        payload_log.addHandler(payload_handler)
    else:
        # No handler + no propagation = records are dropped cheaply.
        payload_log.addHandler(logging.NullHandler())
