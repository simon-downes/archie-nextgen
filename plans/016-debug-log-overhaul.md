# Plan 016: Debug Log Overhaul — Structured JSONL + self_debug Tool

## Objective

Convert the debug log from ad-hoc plain text to structured JSONL with consistent event names and ambient context (session, turn, iteration) on every record, fix level/coverage gaps, move the noisy per-request payload dumps into a separate opt-in log, and add a `self_debug` tool so the agent can inspect its own log to diagnose behaviour, verify cache hits, and measure timings.

The session JSONL (`~/.archie/sessions/*.jsonl`) is out of scope — it serves conversation persistence and stays as-is.

## Context

- Plan 013 added the rotating file log (`~/.archie/nextgen.log`, 10MB × 3, root at DEBUG). It works, but the format is a mix of pseudo-structured (`turn_end status=complete iterations=3`), prose ("Throttled by Bedrock…"), and tool-specific one-offs — not reliably greppable, not machine-parseable.
- `session_id` appears only on `turn_start`; no other record carries it. Records from `bedrock.py` (payloads, retries, usage) have no linkage to the turn/iteration that caused them. The only join key is `tool_use_id`, and only within `agent.py`.
- The full request payload is logged at DEBUG on **every** iteration — the conversation gets re-logged each request, growing per turn (O(n²) over a session). Long sessions burn the entire 30MB rotation budget, destroying the history needed to debug actual failures.
- Level problems: tool failures log at INFO; memory extraction failure at DEBUG (`app.py`); the non-streaming `invoke()` retry loop in `bedrock.py` retries silently.
- Coverage gaps: no `request_end` (LLM latency / stop_reason / AWS request id), nothing on startup config, sandbox lifecycle, session create/resume, context size, truncation/artifact decisions, or interrupts.
- Timestamps are local-time while session logs are UTC — friction when correlating.
- The agent has no good route to read its own log: the sandbox doesn't mount `~/.archie` (so `shell` can't tail it), and `read_file` reads from the front of a 10MB multi-session file. `search_files` can pattern-match but can't tail and caps at 50 matches.

## Requirements

### Structured JSONL format

- MUST emit one JSON object per line to `~/.archie/nextgen.log` (path unchanged, rotation unchanged)
  - AC: every record has `ts` (UTC, ISO-8601, ms precision), `level`, `logger`, `event` (machine name) or `msg` (free text), plus arbitrary structured fields
  - AC: `jq -c 'select(.event=="turn_end")' ~/.archie/nextgen.log` works
  - AC: stdlib only — custom `logging.Formatter`, no structlog/loguru dependency
- MUST attach ambient context to every record emitted during a turn
  - AC: `session`, `turn` (ordinal), and `iteration` (when inside the request loop) appear on records from **any** module, including `bedrock.py` and tool modules, without those modules passing them explicitly
  - AC: records emitted outside a turn (startup, shutdown) simply omit the fields
- MUST keep records human-skimmable
  - AC: free-text `msg` is preserved as a field; `tail -f | jq -r` renders a readable line

### Event taxonomy and levels

- MUST use consistent event names for lifecycle records: `startup`, `session_start`, `turn_start`, `request_start`, `request_end`, `tool_start`, `tool_end`, `turn_end`, `interrupt`
  - AC: each event documents its fields (see Design); no prose-style lifecycle messages remain in `agent.py`/`bedrock.py`
- MUST add `request_end` with `duration_s`, `stop_reason`, the four usage counters, and AWS request id when available
  - AC: LLM latency per iteration is derivable from the log alone
- MUST fix levels: tool errors → WARNING; memory-extraction failure (`app.py`) → WARNING; `invoke()` retry path logs throttle/retry at WARNING like the streaming path
  - AC: `jq 'select(.level=="WARNING" or .level=="ERROR")'` surfaces every failure-ish thing in a session
- MUST fold the `turn_end status=error` info line into a single ERROR record (currently split across `log.exception` + a separate INFO line)
- SHOULD remove redundant per-tool logging in `write_file.py`/`edit_file.py` (duplicates agent dispatch); instead tools MAY return structured detail that lands on `tool_end`

### Coverage additions

- MUST log at startup: resolved model, region, project dir, sandbox image/enabled, log level — one `startup` record
- MUST log `session_start` (session id, model, project) and sandbox lifecycle (created/reused/stopped)
- MUST log `interrupt` when an interrupt is requested, with the phase it landed in (streaming / tool execution)
- MUST log truncation/artifact decisions on `tool_end`: result byte size, whether truncated, artifact stored
- SHOULD log context size (message count, estimated tokens) on `request_start`

### Payload log separation

- MUST move full request-payload dumps out of the main log into `~/.archie/payloads.log` (rotating, 10MB × 1), **disabled by default**
  - AC: enabled via `ARCHIE_LOG_PAYLOADS=1` env var
  - AC: payload records carry the same context fields (`session`, `turn`, `iteration`) so they can be joined to the main log
  - AC: main log retains the compact usage breakdown per request (it's small and high-value)

### self_debug tool

- MUST add a host-side `self_debug` tool that reads the main debug log
  - AC: `tail` param (default 50, max 500) returns the most recent N records
  - AC: optional filters: `level` (minimum), `event`, `pattern` (regex matched against the whole record), `session` (`current` default | `all`)
  - AC: payload-log content is never returned (separate file, not read)
  - AC: output is compact JSONL, `self_truncating=True` (manages its own size budget)
  - AC: reads the current log file only (not rotated backups) in v1
- MUST add a brief system prompt section: the log exists, `self_debug` reads it, use it to diagnose own behaviour (errors, slow turns, cache misses, retries) before guessing
  - AC: one short paragraph, not a manual — the tool schema carries usage details

## Design

### Overview

A new `src/archie/logs.py` module owns formatter, context filter, and setup. `cli.py`'s `setup_logging()` delegates to it. A `contextvars`-based binding (`bind(session=…)`, `bind(turn=…)`) is set by `AgentLoop` at turn/iteration boundaries and injected into every `LogRecord` by a `logging.Filter` on the handler — modules keep using plain `log = logging.getLogger(__name__)` and pass structured fields via `extra={"event": …, …}`. A thin helper `log_event(log, level, event, **fields)` keeps call sites tidy.

### Code Structure

- `src/archie/logs.py` — **new**: `JsonFormatter`, `ContextFilter`, contextvars + `bind()`/`unbind()`, `log_event()` helper, `setup_logging()`, payload logger setup
- `src/archie/cli.py` — `setup_logging()` moves out; emit `startup` record after config load
- `src/archie/agent.py` — convert lifecycle logging to events; bind/unbind turn context; add `request_end`, `interrupt`, enrich `tool_end`
- `src/archie/llm/bedrock.py` — route `_log_request` to the payload logger; add request id + duration capture; log `invoke()` retries
- `src/archie/ui/app.py` — `session_start` on `_build_stack`; extraction failure → WARNING
- `src/archie/tools/self_debug.py` — **new**: the tool
- `src/archie/tools/__init__.py` — register `self_debug` in `create_default_registry`
- `src/archie/prompts/…` — system prompt section (wherever `SYSTEM_PROMPT` lives)
- `src/archie/tools/write_file.py`, `edit_file.py` — drop redundant `log.info` lines

### Event field reference

| event | fields |
|---|---|
| `startup` | model, region, project, sandbox_enabled, version |
| `session_start` | session, model, project |
| `turn_start` | turn, user (truncated 100) |
| `request_start` | iteration, messages, est_tokens |
| `request_end` | iteration, duration_s, stop_reason, input, output, cache_read, cache_write, aws_request_id |
| `tool_start` | tool_use_id, name, input (truncated 500, JSON not repr) |
| `tool_end` | tool_use_id, name, duration_s, status, result_bytes, truncated, error? |
| `turn_end` | turn, status (complete/interrupted/error), iterations, input, output, cache_read, cache_write, cost |
| `interrupt` | phase (stream/tools) |

### Key Decisions

- **stdlib, not structlog** — the formatter+filter is ~60 lines; a dependency buys nothing here and structlog's processor pipeline is overkill for one sink.
- **contextvars over passing loggers around** — `AgentLoop.run_turn` runs in a worker thread; contextvars are thread-local-safe and the filter approach means zero signature changes across modules.
- **Same filename (`nextgen.log`) despite format change** — rotation config, docs, and muscle memory stay; old rotated files age out naturally. The format flips in one commit; no migration.
- **Payloads opt-in via env var, not config file** — it's a developer-debugging switch flipped for one run, not a persistent preference. Avoids config schema churn.
- **`self_debug` reads the file directly, host-side** — no sandbox mount change (keeping `~/.archie` out of the sandbox is deliberate: sessions/config/keys live there). Tool filters on the JSONL fields, so it arrives *after* the format change.
- **Filter defaults to current session** — the dominant use case is "why did *that* just happen"; cross-session analysis (`session=all`) is the exception.
- **`log_event()` helper rather than subclassing Logger** — keeps `logging.getLogger(__name__)` convention from CLAUDE/conventions intact; helper is optional sugar, `extra={}` works anywhere.

## Milestones

### 1. logs.py infrastructure — JSONL + context

Approach:
- `JsonFormatter`: build dict from record (`ts` via `datetime.fromtimestamp(record.created, UTC)`, `level`, `logger`, `msg`), merge whitelisted `extra` fields (anything not a default LogRecord attribute), `json.dumps(default=str)`. Exception info → `exc` field (formatted traceback string).
- `ContextFilter`: reads `_ctx: ContextVar[dict]`, copies into record.
- `bind(**kw)` / `clear()` module functions mutate the ContextVar.
- `log_event(log, level, event, **fields)` → `log.log(level, "", extra={"event": event, **fields})`.
- Move `setup_logging()` here; keep the third-party suppression list.

Tasks:
- Implement `logs.py` with formatter, filter, bind helpers, setup
- Repoint `cli.py` imports; emit `startup` record after config load
- Unit tests: formatter output is valid JSON, context fields injected, exc captured

Deliverable: all existing log calls emit valid JSONL with UTC timestamps.

Verify: `uv run archie chat`, one message, quit. `jq . ~/.archie/nextgen.log` parses every line.

### 2. Agent loop event taxonomy

Approach:
- `run_turn`: `bind(session=…, turn=self._completed_turns + 1)` at entry, `clear()` in finally. Bind `iteration` inside the loop.
- Replace the printf-style lines with `log_event` calls per the field reference table. Single ERROR record on turn failure (exception + the turn_end fields together).
- `_run_one_tool`: tool errors at WARNING; input serialised as JSON (truncated), not `str(dict)`.
- `_execute_tools`: enrich `tool_end` with `result_bytes`, `truncated` (from the `truncate_result` decision), artifact id.
- `interrupt` event in `_check_interrupt` raise path (once per turn, with phase).

Tasks:
- Bind/clear context in `run_turn`
- Convert all `agent.py` log lines to taxonomy events
- Level fixes + single error record
- Tool result metadata on `tool_end`

Deliverable: a complete turn is reconstructable from the log: timings, tokens, tools, sizes, outcome.

Verify: trigger a tool-using turn and a failing tool; `jq 'select(.event)' shows the full sequence with `session`/`turn` on every record.

### 3. Bedrock client: payloads out, request_end in

Approach:
- Payload logger: `logging.getLogger("archie.payloads")`, `propagate=False`, own rotating handler attached only when `ARCHIE_LOG_PAYLOADS=1`, same `ContextFilter` so records join to the main log.
- `stream()`: capture `t0`, emit `request_end` after the metadata event with duration, stop_reason, usage, and `ResponseMetadata.RequestId` from the `converse_stream` response dict.
- `invoke()`: add WARNING logs to its silent retry loop (mirror `_call_with_retry`).
- Keep `_log_usage` content but fold it into `request_end` (drop the separate DEBUG line).

Tasks:
- Payload logger + env-var gate; route `_log_request` to it
- `request_end` emission with duration + request id (stream and invoke paths)
- `invoke()` retry logging

Deliverable: main log stays compact regardless of session length; payloads available on demand.

Verify: run a 10-turn session without the env var — no `Request payload` records, log growth is linear not quadratic. Re-run with `ARCHIE_LOG_PAYLOADS=1` — payloads.log populated with matching `session`/`turn` fields.

### 4. Coverage: lifecycle + misc levels

Approach:
- `session_start` in `ArchieApp._build_stack`; sandbox lifecycle events in `sandbox.py` (create/reuse/stop, image, duration).
- `app.py` extraction failure → WARNING.
- `request_start` gains `messages` count and `est_tokens` (chars/4 over the built context — cheap, already assembled).

Tasks:
- Add lifecycle events; fix `app.py` level
- Remove `write_file.py`/`edit_file.py` log lines

Deliverable: startup → session → turns → shutdown all traceable.

Verify: fresh start + quit produces `startup`, `session_start`, sandbox events; no tool-module log lines remain.

### 5. self_debug tool + prompt section

Approach:
- `tools/self_debug.py`: `make_self_debug_spec(log_path, session_id_fn)`. Read file, parse JSONL (skip malformed lines), apply filters (`session` default current — needs the live session id, pass a callable since sessions can be recreated), `event`, min-`level`, `pattern` (regex over the raw line), then `tail` N. Output: raw JSONL lines, newest last, prefixed with a count header. Cap output ~8KB internally (`self_truncating=True`): if over, drop oldest and note it.
- Registry: needs the session id callable — `create_default_registry` already takes per-session deps (sandbox, artifact_store); add `session_id_fn`.
- System prompt: one paragraph — "A debug log of your own operation (LLM calls, tool runs, timings, tokens, errors) is available via the `self_debug` tool. Use it to diagnose unexpected behaviour, failures, latency, or cost before speculating."
- ⚠️ Feedback-loop guard: `self_debug` output entering context will itself be logged in *payload* logs only (truncated per-leaf there), and `tool_end` logs sizes not content — no amplification in the main log.

Tasks:
- Implement tool + register; wire session id callable
- System prompt section
- Tests: filtering, tail, malformed-line tolerance, size cap

Deliverable: agent can answer "why was that turn slow / did the cache hit / what failed" from its own log.

Verify: in a live session ask "check your debug log — was the prompt cache hit on the last request?" — agent calls `self_debug`, cites `request_end` cache_read figures.

## Out of Scope

- Session JSONL changes (different purpose; revisit joinability later — `session`/`turn` fields now make the debug log joinable from that side anyway)
- Reading rotated backups in `self_debug`
- OpenTelemetry/tracing — JSONL events are convertible later if ever needed
- Log-based metrics/dashboards
