# Plan 029: Agent Loop Improvements

## Objective

Make the agent loop faster and smarter: execute independent tool calls in parallel, and improve the existing doom loop detection to cover multi-tool patterns (not just single-call repetition).

## Context

- `src/archie/agent.py` — `AgentLoop` class with `_execute_tools()` running tool calls sequentially in a `for block in tool_blocks` loop.
- Bedrock requires all `ToolResultBlock`s for a given assistant turn in one user message — parallel execution must still produce one combined result list.
- Cooperative interruption via `threading.Event` must continue to work.
- Existing repetition detection: `_last_call_key` + `_consecutive_count` with `CONSECUTIVE_WARN=3` / `CONSECUTIVE_BLOCK=4` — detects the *same single call* repeated across turns. Does NOT detect multi-call doom loops (e.g. alternating between two calls in a cycle).
- System prompt (`src/archie/prompt.py`) already contains tool usage hints including batching guidance. No changes needed there.
- Tests in `tests/test_agent.py` with `_mock_llm()` helper for agent loop tests.

## Requirements

### Parallel tool execution

- The loop MUST execute multiple tool calls from a single assistant response concurrently using `concurrent.futures.ThreadPoolExecutor`.
  - AC: wall-clock time for N independent tool calls is approximately max(durations) not sum(durations).
  - AC: all ToolResultBlocks are committed as one user message (Bedrock protocol preserved).
- The loop MUST still check the interrupt event between tool completions and abort cleanly.
  - AC: pressing Esc during a parallel batch cancels pending futures, commits completed results, and emits `TurnInterrupted`.
- The loop MUST emit `ToolStarted` events for all tools before execution begins, and `ToolFinished` events as each completes.
  - AC: UI receives events in a coherent order — all ToolStarted first, then ToolFinished as they resolve.
- The loop SHOULD limit concurrency to a reasonable cap (e.g. 4 workers) to avoid overwhelming the sandbox or filesystem.
  - AC: a `max_parallel_tools` constant exists and is respected.
- The loop MUST handle the case where a single tool in the batch raises an exception without crashing the entire batch.
  - AC: failed tools produce error ToolResultBlocks; other tools in the batch complete normally.
- Truncated tool blocks (in `truncated_ids`) MUST still be handled synchronously before parallel dispatch — they don't need execution.
  - AC: truncated blocks emit their ToolFinished event and produce error results as before.

### Doom loop detection improvements

- The loop MUST detect multi-call doom loops — repeated *sets* of calls, not just individual repeated calls.
  - AC: if the same set of (name, input_hash) tuples appears 3 consecutive times across turns, inject an error result on the third occurrence.
- The existing single-call repetition detection (`CONSECUTIVE_BLOCK=4`) MUST be preserved for same-call-within-batch cases. Drop `CONSECUTIVE_WARN` — in parallel context, warn-then-continue adds no value. Only block (return error) at the threshold.
  - AC: existing test cases for consecutive call blocking continue to pass unchanged.
- The doom loop tracker MUST reset when the model produces a different set of tool calls.
  - AC: inserting a different call between repeated sets resets the counter.

## Design

### Parallel execution

Replace the sequential `for block in tool_blocks` with:

1. Emit all `ToolStarted` events upfront for non-truncated blocks.
2. Submit all non-truncated blocks to a `ThreadPoolExecutor(max_workers=MAX_PARALLEL_TOOLS)`.
3. Use `as_completed()` to collect results, emitting `ToolFinished` as each future resolves.
4. Check interrupt after each future completes — if set, cancel remaining futures, commit collected results, raise `_InterruptedError`.
5. Order final `results` list by original block position (Bedrock requires tool results to match the order of tool_use blocks in the assistant message).

Key constant: `MAX_PARALLEL_TOOLS = 4`.

The existing `_run_one_tool()` method is already stateless with respect to other tools (it only touches `_last_call_key`/`_consecutive_count` for repetition detection). Repetition detection state must be protected with a lock or moved to post-collection since tools now run concurrently.

Decision: move per-call repetition checking to *before* dispatch (in the main thread) rather than inside `_run_one_tool`. This avoids threading concerns — check all blocks for repetition, skip/warn as needed, then dispatch the survivors in parallel.

### Multi-call doom loop detection

Add to `AgentLoop.__init__`:
```python
self._last_batch_key: str | None = None
self._batch_repeat_count: int = 0
```

Before executing a batch, compute a batch fingerprint:
```python
batch_key = hashlib.md5(
    json.dumps(sorted((b.name, _hash_args(b.input)) for b in tool_blocks)).encode()
).hexdigest()
```

If `batch_key == self._last_batch_key`, increment `_batch_repeat_count`. If it hits 3, inject error results for all blocks: "Stuck in a loop — the same set of tool calls has been made 3 times. Try a different approach." Reset on any different batch.

### Thread safety

- `_emit` callback is already thread-safe (Textual's `call_from_thread` / test's `list.append`).
- `_run_one_tool` accesses `self.tools` (read-only after init) and `self.artifact_store` (thread-safe dict operations).
- `_pre_content_stash` is read via `.pop()` — use a lock or move pop to the main thread post-collection.
- Session is NOT touched during parallel execution — `add_turn` only after all results collected.

### What stays the same

- System prompt — already has comprehensive tool usage hints in `prompt.py`.
- Single-call repetition detection — logic preserved, just relocated to pre-dispatch.
- `_finalise_interrupted_turn()` — unchanged.
- Event types — no new AgentEvent types needed.

## Milestones

### 1. Parallel tool execution

Approach:
- Refactor `_execute_tools` to separate truncated-block handling, pre-dispatch repetition checking, parallel dispatch via `ThreadPoolExecutor`, and ordered result collection.
- Extract `_run_and_wrap_tool(block) -> tuple[ToolResultBlock, ToolFinished, dict]` as the unit of work submitted to the executor — returns the result block, the finished event, and the turn_log entry.
- Main thread emits all `ToolStarted`, submits futures, iterates `as_completed`, emits `ToolFinished`, checks interrupt.

Tasks:
- Add `MAX_PARALLEL_TOOLS = 4` constant.
- Extract `_run_and_wrap_tool()` from the body of the current for-loop (everything between ToolStarted emission and ToolFinished emission).
- Rewrite `_execute_tools` to: handle truncated blocks first, emit ToolStarted for remaining blocks, submit to executor, collect via `as_completed`, emit ToolFinished, check interrupt, commit results in original order.
- Move `_pre_content_stash.pop()` to result collection phase (main thread).
- Move per-call repetition check to pre-dispatch (main thread iterates blocks, calls `_check_repetition(block)` which returns an error result or None).
- Update `tests/test_agent.py`: add test for parallel execution (mock two tools with `time.sleep`, assert wall time < sum), test interrupt mid-batch, test one-tool-failure-doesn't-crash-batch.
- Verify existing tests pass unchanged.

Deliverable: `_execute_tools` runs tools concurrently, interrupt works, events emit correctly.

Verify: `uv run pytest && uv run ruff check src tests && uv run ruff format --check src tests`

### 2. Multi-call doom loop detection

Approach:
- Add batch fingerprinting and repeat counter to `AgentLoop`.
- Check at the top of `_execute_tools` before any dispatch.
- If triggered, return error results for all blocks without executing any.

Tasks:
- Add `_last_batch_key` and `_batch_repeat_count` to `__init__`.
- Compute batch fingerprint from sorted (name, input_hash) tuples.
- If repeat count hits 3, produce error ToolResultBlocks for all blocks, emit ToolFinished events with errors, commit results, and return early (don't raise — the model gets the error and can change course).
- Add tests: 3 identical batches triggers block, 2 identical then different resets, single-tool batches still caught by existing per-call detection.

Deliverable: Multi-call doom loops are detected and broken with an actionable error message.

Verify: `uv run pytest && uv run ruff check src tests && uv run ruff format --check src tests`
