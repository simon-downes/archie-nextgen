# Plan 012: AgentLoop Refactor — Callback-Based Turn Loop with Cooperative Interruption

## Objective

Replace the generator-based Engine with a callback-based AgentLoop that owns all history mutation, supports fine-grained cooperative interruption, and repairs history on abort. This makes the UI a pure display layer, fixes the split-brain history problem on interrupt, and establishes the architecture for future features (caching, compaction, model switching).

## Context

The current Engine is a synchronous generator that yields EngineEvents. The UI worker iterates it and checks `worker.is_cancelled` between yields. This creates three problems:

1. **Coarse interrupt granularity** — the engine can't abort between stream events within a single LLM call or between tool executions within a batch. It only checks cancellation when control returns to the UI's `for event in engine.run()` loop.
2. **Split-brain history on interrupt** — when the worker is cancelled, the engine's generator just stops. The UI patches up the turn log in `on_stream_complete`, but the session's turn list (what gets sent to Bedrock next time) isn't repaired. Dangling `ToolUseBlock`s without matching `ToolResultBlock`s make the history API-illegal.
3. **Tangled ownership** — the UI knows about engine internals (`engine.current_turn_log`, session token counts) to do post-interrupt cleanup.

The fable variant solves these with a callback-based loop + `threading.Event` for interruption + `finalise_interrupted_turn()` for history repair. We adopt that architecture.

## Requirements

### Agent loop

- MUST replace the generator-based Engine with a class (`AgentLoop`) that accepts an `emit` callback and drives the turn loop as a plain method (`run_turn(text) -> None`)
  - AC: `run_turn` is a blocking synchronous method; the UI runs it in a `run_worker(thread=True)` call
  - AC: all events are delivered via the `emit` callback, not yielded

- MUST support cooperative interruption via a `threading.Event`
  - AC: calling `interrupt()` from the UI thread sets the event
  - AC: the loop checks the event before each LLM request, between stream events, and before/after each tool execution
  - AC: when interruption is detected, the loop repairs history and emits a terminal event without the UI needing to touch history

- MUST repair history on interrupt so it remains API-legal
  - AC: any `ToolUseBlock` in the session that lacks a matching `ToolResultBlock` gets a synthetic `[interrupted by user]` result appended
  - AC: if the turn produced only the user message (interrupted before any response), that orphan message is removed from the session
  - AC: partial streamed text is preserved in history if non-empty

- MUST emit a distinct `TurnInterrupted` event (separate from `TurnComplete`) so the UI can mark interruptions
  - AC: the UI re-enables input only in response to a terminal event (`TurnComplete`, `TurnInterrupted`, `TurnError`), never on the Esc keypress itself

### Error handling

- MUST catch `ClientError` (and any unexpected exception) at the top of `run_turn` and emit a `TurnError` event with the raw exception message
  - AC: a Bedrock validation error surfaces as a red block in the conversation showing the full boto error string
  - AC: the app does not crash; input is re-enabled

### Events

- MUST define all agent events as frozen dataclasses in a dedicated module
  - AC: events are the sole communication channel from the agent to the UI
  - AC: the UI never mutates session history or repairs agent state (no accessing `engine.current_turn_log` or patching turn logs in the UI's interrupt handler)
  - AC: the status bar is updated from `UsageUpdated` event fields, not by reading session internals

### Eviction (carried forward)

- MUST preserve the existing eviction logic (replacing old tool results with stubs) in the new loop
  - AC: behaviour is identical to the current `_build_context()` in `engine.py`

### Turn logging (carried forward)

- MUST preserve session JSONL logging with the same schema
  - AC: the agent loop builds the `TurnLog` internally and flushes it on every terminal path (complete, interrupted, error)

## Design

### Overview

Replace `Engine` (a generator) with `AgentLoop` (a class with a callback). The loop is a plain `while True` with explicit interrupt checks. History repair and turn logging move entirely into the loop — the UI becomes a pure event consumer.

### Code Structure

- `src/archie/agent.py` — new file. Contains `AgentLoop`, all `AgentEvent` dataclasses, and `_Interrupted` (internal exception for unwinding).
- `src/archie/engine.py` — deleted after migration.
- `src/archie/types.py` — remove `EngineEvent` types (they move to `agent.py` as `AgentEvent`). Keep `ContentBlock` types.
- `src/archie/ui/app.py` — reworked to construct `AgentLoop`, pass a callback, run in worker.
- `tests/test_agent.py` — new, replaces `tests/test_engine.py`.

### Architecture

```
UI (ArchieApp)
  │
  ├─ constructs AgentLoop(llm, session, tools, system_prompt, sandbox, emit=callback)
  │
  ├─ on user submit: run_worker(lambda: self._agent.run_turn(text), thread=True)
  │
  ├─ on Esc: self._agent.interrupt(); self.sandbox.cancel()
  │
  └─ _on_agent_event(event):  ← the emit callback
       call_from_thread(self._handle_event, event)  ← marshal to main thread
```

### Patterns

- **Interrupt flow**: `threading.Event` checked at safe points. When set, raise `_Interrupted` → caught at the top of `run_turn` → call `_finalise_interrupted_turn()` → emit `TurnInterrupted`.
- **Terminal event guarantee**: every code path through `run_turn` ends with exactly one terminal event (`TurnComplete`, `TurnInterrupted`, or `TurnError`). The UI uses these to re-enable input.
- **History ownership**: only `AgentLoop` mutates `session.turns`. The UI reads session state only for display (status bar), never writes.
- **`_Interrupted` is internal**: private exception, never escapes the module. It's control flow for unwinding nested stream/tool loops to the single handler.

### Key Decisions

- Keep `Session` and `Turn` as-is — no changes to the data model. The agent loop operates on the same `session.add_turn()` / `session.turns` API.
- Keep the `BedrockClient` API unchanged — it stays a synchronous generator yielding stream events. The agent loop iterates it, checking interrupt between events.
- Keep `ToolRegistry` unchanged — the loop calls `spec.handler(args)` the same way.
- No `ContextManager` class (unlike fable). The eviction logic stays as a method on the agent loop (`_build_context`), operating on `session.turns` directly. We don't need a separate abstraction yet.
- `ArtifactStore` stays on the agent loop (same as it was on Engine).

## Milestones

### 1. Define AgentEvent types and create agent.py skeleton

Approach:
- Create `src/archie/agent.py` with all event dataclasses (frozen) and the `AgentLoop` class skeleton (constructor + stub methods).
- Events: `TextDeltaEvent`, `ToolStarted`, `ToolFinished`, `UsageUpdated`, `TurnComplete`, `TurnInterrupted`, `TurnError`.
- `ToolFinished` carries a `summary` string (what was in `ToolCallResult.content` before) and `is_error`.
- `UsageUpdated` carries `input_tokens` and `output_tokens` (same as current `TurnComplete` did per-request). Keep it simple for now; cache breakdown comes in a later plan.
- Follow the frozen dataclass pattern from fable's agent.py but with our simpler event set.
- ⚠️ Don't import Textual anything in this module — it must stay UI-free for testability.

Tasks:
- Create `src/archie/agent.py` with event dataclasses and empty `AgentLoop` class with constructor signature
- Define `_Interrupted` exception (module-private)
- Add `interrupt()` method (sets `threading.Event`)
- Add `_check_interrupt()` method (raises `_Interrupted` if set)

Deliverable: `agent.py` exists with typed events and the interrupt mechanism, importable and lint-clean.

Verify: `uv run ruff check src/archie/agent.py && uv run python -c "from archie.agent import AgentLoop, TurnComplete, TurnInterrupted, TurnError"`

### 2. Implement run_turn core loop

Approach:
- Port the iteration logic from `Engine.run()` into `AgentLoop.run_turn()`. Same structure: loop until `stop_reason != "tool_use"` or iteration cap hit.
- Call `_check_interrupt()` before each LLM request, between stream events (inside the stream iteration), and before/after each tool execution.
- Accumulate text/tool_use from the stream the same way Engine does (text chunks + ToolUseEvent parsing).
- Record assistant turns and tool result turns via `session.add_turn()`.
- Emit events via `self._emit(event)` at each step.
- The stream iteration: iterate `self.llm.stream(...)`, check interrupt after each event, build up content.
- ⚠️ The interrupt check between stream events is the key improvement — in a 30-second stream, this lets Esc respond in <100ms instead of waiting for the full response.

Tasks:
- Implement `run_turn(text)` with the main loop, interrupt checks, and event emission
- Implement `_do_request()` helper that streams one LLM call and returns stop_reason + accumulated content
- Implement `_execute_tools()` helper that runs a batch of tool calls with interrupt checks between them
- Port `_build_context()` from Engine (eviction logic) unchanged
- Port `_execute_tool()` from Engine (single tool: lookup, consecutive-call detection, handler invocation)
- Wire up `ArtifactStore` and `TurnLog` accumulation

Deliverable: `AgentLoop.run_turn()` drives a multi-tool turn to completion, emitting the correct event sequence through the callback.

Verify: `uv run pytest tests/test_agent.py` — tests mock the LLM stream and tool handlers, assert the emitted event sequence and session state for: text-only response, multi-tool loop, and iteration cap.

### 3. Implement history repair on interrupt

Approach:
- `_finalise_interrupted_turn()` method on `AgentLoop`. Walks `session.turns`, finds any `ToolUseBlock` that lacks a matching `ToolResultBlock` in a subsequent turn, appends a synthetic result turn.
- If the turn has only the user's text message (interrupted before any response), remove it from session.turns.
- The `except _Interrupted` block in `run_turn`: call `_finalise_interrupted_turn()`, flush the turn log with `interrupted=True`, emit `TurnInterrupted`.
- ⚠️ The repair must happen AFTER any partial assistant content is committed to session.turns (so streamed text is preserved). The stream loop should commit the assistant turn before re-raising `_Interrupted`.

Tasks:
- Implement `_finalise_interrupted_turn()` — find unpaired toolUse blocks, synthesise results, handle orphan-turn case
- Wire `except _Interrupted` handler in `run_turn` — repair, flush log, emit event
- Ensure partial text from an interrupted stream is committed before raising

Deliverable: Interrupting mid-turn leaves session history API-legal with all completed work preserved.

Verify: `uv run pytest tests/test_agent.py -k interrupt` — tests cover: interrupt mid-stream (text preserved), interrupt between tools (completed tools have results, pending ones get synthetic), interrupt before any response (user message removed).

### 4. Implement error handling

Approach:
- Wrap the entire `run_turn` body in `try/except Exception`. Catch everything, emit `TurnError(message=str(e))`.
- Do NOT wrap in a custom exception type — show the raw error string. This includes botocore `ClientError` messages which are useful as-is.
- Also call `_finalise_interrupted_turn()` on error (a failed mid-turn leaves the same dangling-toolUse problem).
- ⚠️ Log the full traceback via `logger.exception()` before emitting the event — the error message shown in the UI may be truncated but the log has everything.

Tasks:
- Add top-level `try/except` in `run_turn` with history repair and `TurnError` emission
- Add `logger.exception()` call for full traceback in logs

Deliverable: Any exception during a turn results in a `TurnError` event with the raw message; the app does not crash and input is re-enabled.

Verify: `uv run pytest tests/test_agent.py -k error` — tests mock LLM to raise `ClientError` and an unexpected `RuntimeError`, assert `TurnError` is emitted with the message and session remains usable.

### 5. Rewire UI to use AgentLoop

Approach:
- Replace all Engine references in `ui/app.py` with AgentLoop.
- The worker becomes `run_worker(lambda: self._agent.run_turn(text), thread=True)`.
- The callback (`_on_agent_event`) uses `call_from_thread` to marshal events to the main thread, then `_handle_event` dispatches on event type to update widgets.
- `action_cancel` becomes: `self._agent.interrupt(); self.sandbox.cancel()`. It does NOT cancel the worker or touch history. The worker finishes on its own (quickly, because interrupt is set) and emits a terminal event.
- Remove: `_run_engine`, `StreamChunk`/`ToolStart`/`ToolResult`/`StreamComplete` messages, the `on_stream_complete` interrupt-fixup logic, `engine.current_turn_log` access.
- Status bar update: emit a `UsageUpdated` event from the loop after each LLM call (like fable does), and on the terminal events. The UI handler calls `_update_status()` when it receives one.
- ⚠️ Don't remove `self._stream_worker` tracking entirely — keep a boolean `_turn_active` flag to gate input and show/hide the Esc binding. Set it True on submit, False in `_end_turn()` (called by all three terminal event handlers).

Tasks:
- Construct `AgentLoop` in `ArchieApp.__init__` (replacing Engine)
- Implement `_on_agent_event` callback with `call_from_thread`
- Implement `_handle_event` dispatcher (pattern-match on event types, update widgets)
- Rework `on_message_input_submitted` to call `run_worker(lambda: self._agent.run_turn(text))`
- Rework `action_cancel` to signal interrupt only
- Rework `action_new_session` to recreate `AgentLoop`
- Remove old Engine-based message classes and handlers
- Update `_update_status()` to read from `UsageUpdated` event fields (input_tokens, output_tokens, stop_reason) rather than session attributes directly

Deliverable: The app runs using `AgentLoop` — text streaming, tool execution, interruption, and error display all work through the callback-based loop.

Verify: `uv run archie chat` — send a message, observe streaming; trigger a tool call; press Esc mid-tool and confirm input re-enables cleanly; Ctrl+Q quits.

### 6. Delete Engine, update tests, clean up

Approach:
- Remove `src/archie/engine.py`.
- Remove `EngineEvent` types from `types.py` (keep `ContentBlock` types).
- Remove `tests/test_engine.py` (replaced by `tests/test_agent.py`).
- Remove the now-unused Textual message classes from `ui/app.py` (`StreamChunk`, `ToolStart`, `ToolResult`, `StreamComplete`, `ShellResult`).
- Update any imports elsewhere that referenced Engine or the removed event types.
- ⚠️ Keep `ShellResult` and the `!` prefix shell command logic as-is — it's independent of the agent loop (runs in its own worker, already uses Textual Messages correctly).

Tasks:
- Delete `src/archie/engine.py`
- Remove `EngineEvent` union and event dataclasses from `types.py`
- Delete `tests/test_engine.py`
- Clean up imports across the codebase
- Verify lint and tests pass

Deliverable: No references to the old Engine remain; the codebase is clean and all tests pass.

Verify: `uv run pytest && uv run ruff check src tests`
