# Plan 013: Debug Logging, Prompt Caching Visibility, and Ctrl+G Editor

## Objective

Add always-on debug logging to a rotating file, expose prompt cache token breakdown in both logs and the status bar, place a second cache point on the history tail, and add Ctrl+G to open `$EDITOR` for composing prompts. These are the immediate cost-observability and UX improvements that follow the AgentLoop refactor (Plan 012).

## Context

- The project currently has **no logging infrastructure** — no file handler, no rotating log, nothing. When something goes wrong in a session there's no trace.
- Prompt caching has a single cache point on the system prompt but no cache point on the message history. Bedrock returns `cacheReadInputTokens` and `cacheWriteInputTokens` in its usage metadata, but we parse only `inputTokens` and `outputTokens` — so there's no way to tell if caching is working.
- The status bar shows a single input token count. Since caching is the primary cost lever (cache-read tokens are ~10x cheaper), the UI needs to show the split.
- There's no way to compose multi-line prompts comfortably other than Shift+Enter. `$EDITOR` support is a common pattern in terminal AI tools (claude code uses it).

Assumes Plan 012 (AgentLoop refactor) is complete. References `AgentLoop`, `_on_agent_event`, `UsageUpdated`, etc.

## Requirements

### Debug logging

- MUST configure a rotating file logger at DEBUG level on startup, before any other initialisation
  - AC: log file at `~/.archie/nextgen.log`, max 10MB, 3 backups
  - AC: no log output to stdout/stderr (Textual owns the terminal)
  - AC: logger is available to all modules via `logging.getLogger(__name__)`

- MUST log every Bedrock request with the full message structure visible (per-block truncation of string leaves, not whole-payload truncation)
  - AC: log line shows message count, role sequence, cache point positions, tool declarations — even when individual tool results are large
  - AC: string content blocks are truncated to 2KB each; structure is never truncated

- MUST log a per-request usage breakdown line after each Bedrock response
  - AC: log line contains `fresh=X cache_read=Y cache_write=Z output=W` for every request

- MUST log tool execution (name, duration, success/error) at INFO level
  - AC: `tool shell completed in 2.3s (success)` or `tool read_file failed: <error>`

### Prompt cache token breakdown

- MUST parse all four token categories from Bedrock's usage metadata: `inputTokens`, `outputTokens`, `cacheReadInputTokens`, `cacheWriteInputTokens`
  - AC: the `Usage` dataclass in `llm/bedrock.py` has all four fields
  - AC: values are 0 when not present in the response (non-caching models)

- MUST place a second cache point after the last content block of the last message in every request
  - AC: from the second request onward in a session, `cache_read_input_tokens > 0` in logs (for cache-supporting models)
  - AC: cache point is appended to the content list as its own block `{"cachePoint": {"type": "default"}}`, NOT as a key on an existing block

- MUST show the four-way token split in the status bar
  - AC: format: `{model} │ in:{fresh}/{cache_read}/{cache_write} out:{output} │ ctx:{pct}% │ ${cost} │ {project} ⎇ {branch}`
  - AC: token counts and cost update after each Bedrock request within a turn (progressive, not just at turn end)
  - AC: while streaming, output shows a `~N` estimate (chars/4); snaps to real value when usage arrives
  - AC: context percentage is preserved (total input tokens as fraction of model's context window)

- MUST compute cost using per-category pricing (fresh input, cache-read, cache-write, output each have different rates)
  - AC: `ModelInfo` gains `cache_read_price_per_m` and `cache_write_price_per_m` fields
  - AC: `calculate_cost()` uses all four rates
  - AC: cost displayed is always derived from reported usage, never from estimates

### Ctrl+G editor

- MUST open `$EDITOR` (defaulting to `vi`) with the current input contents on Ctrl+G
  - AC: saving non-empty content auto-submits the prompt
  - AC: saving empty content clears the input box without submitting
  - AC: quitting without saving (detected via mtime comparison) leaves input unchanged
  - AC: the file has a `.md` suffix so editors apply markdown highlighting
  - AC: the tempfile is cleaned up after the editor exits

## Design

### Overview

Three independent additions to the codebase: (1) logging infrastructure in `cli.py`'s startup path, plus structured logging in `bedrock.py` and `agent.py`; (2) four-category usage threading from Bedrock response → `Usage` → `UsageUpdated` event → status bar; (3) a new Textual binding + `action_editor` method on `ArchieApp`.

### Code Structure

- `src/archie/cli.py` — add `setup_logging()`, called before `load_config()` in both `chat()` and `build()` commands
- `src/archie/llm/bedrock.py` — extend `Usage` dataclass, add `_log_request()` and `_log_usage()` methods, add history-tail cache point in `stream()` (appended to the received messages before calling Bedrock)
- `src/archie/models.py` — extend `ModelInfo` with cache pricing, update `calculate_cost()`
- `src/archie/agent.py` — extend `UsageUpdated` event with four-way split, add tool execution logging
- `src/archie/ui/status.py` — rework to display the split format
- `src/archie/ui/app.py` — add `action_editor` method and Ctrl+G binding
- `src/archie/session.py` — extend token tracking to four categories

### Key Decisions

- **Logging goes in `cli.py` not a separate module** — it's 10 lines of setup code, no need for a module. Configured before anything else runs.
- **Cache point placement lives in `bedrock.py`** — the client already builds the system blocks and messages dict. Adding the cache point there (on the messages dict it's about to send) keeps cache-point logic in one place. The agent loop doesn't know about cache points.
- **Deep-copy before mutating for cache points** — appending a cache point to the last message's content list would mutate the caller's history. JSON round-trip the last message before appending (same as fable's approach, but only for the one message we're modifying).
- **No separate UsageTracker class** — the session already accumulates totals. We extend it with the four fields and keep `calculate_cost()` as a module function. A dedicated tracker would add indirection without value at this point.
- **`_refresh_display` pattern for status bar** — keep the existing reactive-based StatusBar but extend it with the cache fields. Fable's single-Static approach is simpler for this specific widget, but the current reactive pattern works and changing it is churn.
- **Git branch via .git/HEAD read (not subprocess)** — adopt from fable. Reading the file is instant; shelling out to `git` spawns a process. Implement in `_detect_git_branch` as a fast-path replacement.

## Milestones

### 1. Always-on debug logging

Approach:
- Add `setup_logging()` to `cli.py` — call it as the **first line** of both `chat()` and `build()` commands, before `load_config()`. If config loading fails, we still want a log trace.
- Use `logging.handlers.RotatingFileHandler` (stdlib). 10MB max, 3 backups, UTF-8.
- Format: `%(asctime)s %(levelname)s %(name)s: %(message)s`
- Set root logger to DEBUG. Do NOT add a StreamHandler.
- Every module already uses `log = logging.getLogger(__name__)` per project conventions — they just had nowhere to write.
- ⚠️ Create `~/.archie/` directory in `setup_logging()` if it doesn't exist (it should from config, but be defensive).

Tasks:
- Add `setup_logging()` function to `cli.py`
- Call it at the start of `chat()` and `build()` commands
- Verify existing `log.warning` / `log.info` calls in bedrock.py and sandbox.py now appear in the file

Deliverable: All log output across the application writes to `~/.archie/nextgen.log` at DEBUG level.

Verify: `uv run archie chat`, send one message, quit. `cat ~/.archie/nextgen.log` shows timestamped entries from multiple modules.

### 2. Structured request/response logging in Bedrock client

Approach:
- Add `_log_request(params)` method to `BedrockClient`. Serialise the request dict with per-leaf truncation (strings > 2KB get clipped with a `…[N more chars]` suffix). Log at DEBUG level.
- Add `_log_usage(usage, context)` static method. Logs `context usage: fresh=X cache_read=Y cache_write=Z output=W`. Called after parsing metadata from both stream and invoke paths.
- Use `_truncate_blocks(obj, limit=2048)` recursive helper — dict/list traversal, truncate only `str` leaves.
- ⚠️ Serialise via `json.dumps(obj, default=str)` to handle any non-JSON types (like datetime) without crashing the logger.

Tasks:
- Implement `_truncate_blocks()` helper (recursive, truncates string leaves)
- Implement `_log_request()` — called before `converse_stream` / `converse`
- Implement `_log_usage()` — called after parsing usage metadata from stream
- Add `logger.debug` calls at the appropriate points in `stream()` and `invoke()`

Deliverable: Every Bedrock request logs its full structure (with truncated leaves) and every response logs the usage breakdown.

Verify: Send a message that triggers a tool call. Check `~/.archie/nextgen.log` — find a `Request payload:` line showing message structure and a `usage:` line showing the four token categories.

### 3. Four-category Usage and cache pricing

Approach:
- Extend `Usage` in `llm/bedrock.py` with `cache_read_input_tokens: int = 0` and `cache_write_input_tokens: int = 0`. Parse from `cacheReadInputTokens` / `cacheWriteInputTokens` in the metadata event.
- Extend `ModelInfo` in `models.py` with `cache_read_price_per_m: float` and `cache_write_price_per_m: float`. Update all entries in `MODELS` dict.
- Update `calculate_cost()` to use all four rates.
- Extend `Session` token tracking: add `total_cache_read_tokens` and `total_cache_write_tokens`. Update `add_turn()` to accept them.
- Extend `UsageUpdated` event in `agent.py` with the four fields. The agent loop emits this after each LLM call with the session's running totals.
- ⚠️ Cache prices for current models: Sonnet 4 → input $3, cache_read $0.30, cache_write $3.75, output $15 (per million). Haiku → input $0.80, cache_read $0.08, cache_write $1.00, output $4. Opus → input $15, cache_read $1.50, cache_write $18.75, output $75. These are per-million.

Tasks:
- Extend `Usage` dataclass with cache fields; parse them from Bedrock metadata
- Extend `ModelInfo` with cache pricing fields; update all model entries
- Update `calculate_cost()` to use four-rate calculation
- Extend `Session` with cache token accumulators
- Extend `UsageUpdated` event and agent loop emission

Deliverable: Cost calculation uses all four token categories with correct per-category pricing.

Verify: `uv run pytest` passes. Add a unit test for `calculate_cost()` that asserts cache_read tokens are priced at the cache_read rate (not full input rate).

### 4. Second cache point on history tail

Approach:
- In `BedrockClient.stream()`, after receiving the `bedrock_messages` parameter but before calling `_call_with_retry`, append `{"cachePoint": {"type": "default"}}` to the content list of the **last message** in the messages list.
- The messages list is built fresh by `_build_context()` on the AgentLoop each call (confirmed: it constructs new dicts, doesn't share references with `session.turns`). No deep-copy is needed — just mutate the last message's content list directly.
- The system prompt cache point already exists (from plan 011). This second point caches the history prefix — everything before it becomes cache-read on subsequent requests.
- If `self._cache_supported` is False (model doesn't support caching), skip both cache points.
- ⚠️ The cache point must be appended as its own content block in the list — NOT as a key on an existing block. `messages[-1]["content"].append({"cachePoint": {"type": "default"}})`.

Tasks:
- Add history-tail cache point in `BedrockClient.stream()` (conditional on `_cache_supported`)
- Update the `_cache_supported` fallback logic to also strip history cache points on retry

Deliverable: From the second Bedrock request onward in a session, the response reports `cache_read_input_tokens > 0`.

Verify: `uv run archie chat`, send two messages. Check `~/.archie/nextgen.log` — the second request's usage line should show `cache_read > 0`.

### 5. Status bar with cache token split

Approach:
- Rework `StatusBar` to display: `{model} │ in:{fresh}/{cache_read}/{cache_write} out:{output} │ ctx:{pct}% │ ${cost} │ {project} ⎇ {branch}`
- Add reactive fields for `cache_read`, `cache_write` alongside existing `turn_input`/`turn_output`.
- The `_handle_event` dispatcher in app.py updates the status bar when `UsageUpdated` arrives.
- While streaming (before usage arrives), show `out:~{estimate}` using chars/4 heuristic. Snap to real value when `UsageUpdated` arrives.
- Replace `_detect_git_branch` subprocess call with direct `.git/HEAD` file read (faster, no process spawn).
- ⚠️ The status bar should show **session** totals (lifetime), not per-turn. This matches fable and gives a running cost that only climbs.

Tasks:
- Add `cache_read` and `cache_write` reactive fields to `StatusBar`
- Rework `_update_left()` (or equivalent) to show the four-column format
- Update `_handle_event` in app.py to feed `UsageUpdated` fields into the status bar
- Add streaming output estimate (chars/4) updated on each `TextDeltaEvent`, cleared on `UsageUpdated`
- Replace `_detect_git_branch` with `.git/HEAD` file read

Deliverable: The status bar shows the four-way token split and session cost, updating progressively through each turn.

Verify: `uv run archie chat`, send a message. Status bar shows `in:X/Y/Z out:W` format with cost. After the second message, the `cache_read` number is non-zero.

### 6. Ctrl+G editor binding

Approach:
- Add `Binding("ctrl+g", "editor", "Editor", show=True)` to `ArchieApp.BINDINGS`.
- Implement `action_editor()` on `ArchieApp`. Uses `app.suspend()` context manager to hand the terminal to the editor, then reclaim it.
- Workflow: write current input to tempfile (.md suffix) → record `st_mtime` → `suspend()` → `subprocess.run([$EDITOR, tmpfile])` → compare mtime → decide action.
- Three outcomes: (a) mtime unchanged = quit without save → do nothing; (b) content non-empty + mtime changed → clear input, post `PromptInput.Submitted`; (c) content empty + mtime changed → clear input.
- ⚠️ `delete=False` on the tempfile because we reopen it after the editor exits. Clean up with `Path.unlink(missing_ok=True)` after reading.
- ⚠️ Guard against running during an active turn — if `_turn_active` is True (the same boolean flag on `ArchieApp` that gates input submission and shows the Esc binding per Plan 012), do nothing.

Tasks:
- Add Ctrl+G binding to `BINDINGS` list
- Implement `action_editor()` with mtime-based save detection
- Handle the three outcomes (no-save, save-non-empty, save-empty)
- Add `_turn_active` guard

Deliverable: Ctrl+G opens `$EDITOR`, and saving non-empty content submits it as a prompt.

Verify: Launch `uv run archie chat`. Press Ctrl+G — editor opens. Type something, save and quit — prompt is submitted. Press Ctrl+G again, quit without saving — input unchanged.

### 7. Tool execution logging

Approach:
- In the agent loop's tool execution path, log at INFO level: tool name, duration (time the handler call), and outcome.
- Format: `tool {name} completed in {duration:.1f}s ({status})` where status is "success" or "error: {first line}".
- Also log at DEBUG level the tool input args (truncated to 500 chars).
- This goes in `_execute_tool()` (or equivalent) in `agent.py`.
- ⚠️ Don't log the full tool result — it can be huge and is already in the request log on the next LLM call.

Tasks:
- Add `time.time()` around tool handler invocation in the agent loop
- Log tool name, duration, and status at INFO
- Log tool input args at DEBUG (truncated)

Deliverable: Every tool execution is visible in the log with its timing and outcome.

Verify: Send a message that triggers tool calls. Check `~/.archie/nextgen.log` for `tool read_file completed in 0.0s (success)` lines.
