# Plan 014: Address Code Review Findings

## Objective

Fix the protocol bug (consecutive user turns on interrupt), unify wire serialization, clean up drift/duplication, and improve code quality across the findings raised in REVIEW.md.

## Context

A codebase review identified 13 findings. We're addressing 10, documenting 1, and skipping 2:
- **Skip:** config.load_config() dict-plucking (fine at current size) and `_StreamParser` extraction (marginal benefit for 30 lines of state machine).
- **Document only:** ArtifactStore in-memory coupling (sessions aren't resumable; add a comment).

## Requirements

- MUST fix the interrupted-tool-results protocol bug (two consecutive user turns)
  - AC: interrupting mid-tool-batch produces a single user turn containing both completed and synthetic results
- MUST unify wire serialization so `_build_context` returns `list[Turn]` and bedrock.py owns all dict-building
  - AC: `agent.py` no longer imports or constructs Bedrock wire-format dicts
  - AC: the `list[Turn] | list[dict]` union in `BedrockClient.stream()` is removed
- MUST replace `_do_request`'s 7-tuple with a result dataclass
  - AC: call site uses named fields, not positional destructuring
- MUST fix stale "Engine" references in docstrings and the README/config model mismatch
  - AC: `grep -ri engine src/` returns zero hits outside of git history
  - AC: README default model matches `DEFAULT_CONFIG` in config.py
- MUST extract composition root from ArchieApp so `__init__` and `action_new_session` share one factory
  - AC: single `_build_stack()` method called from both
- MUST rename `_detect_git_branch` to remove the private prefix (it's imported cross-module)
  - AC: no leading underscore in the function name or its import
- MUST extract memory-extraction helper to eliminate duplication between `action_quit` and `_run_memory_extraction`
  - AC: single helper called from both
- MUST fix `atexit.register` accumulation on new_session
  - AC: only one atexit handler exists regardless of how many sessions are created
- MUST promote inline magic numbers (50, 3, 4) to module constants in agent.py
  - AC: no bare numeric literals for iteration cap or repetition thresholds
- MUST extract `_estimated_next_input` property in Session to deduplicate `context_pct` / `context_warning`
  - AC: computation appears once

## Design

### Protocol bug fix

In `_finalise_interrupted_turn`, instead of creating a new user turn, find the last user turn (which contains the partial tool results) and extend its content list with the synthetic results. If no user turn with tool results exists yet (interrupt before any tool completed), create one as today.

### Wire serialization unification

- `_build_context()` returns `list[Turn]` with `ToolResultBlock` content replaced by stubs for old turns (eviction produces modified `Turn` objects, not dicts).
- Remove the inline dict-building from `agent.py`.
- `BedrockClient.stream()` accepts only `list[Turn]` (remove the union type and the `isinstance` check).
- `_turns_to_bedrock_messages` handles all serialization including eviction stubs — it just serializes whatever `ToolResultBlock.content` contains.

### Composition root

Add `_build_stack(self)` method to `ArchieApp` that creates session, sandbox, registry, agent from `self.config`, `self.model_info`, `self.llm`, `self.project_dir`. Called from `__init__` and `action_new_session`.

### Atexit fix

Register a single closure in `__init__` that calls `self.sandbox.destroy()`. Since `self.sandbox` is reassigned on new_session, the closure always destroys the current one. Remove the per-session `atexit.register` calls.

## Milestones

### 1. Fix protocol bug (consecutive user turns on interrupt)

Approach:
- In `_finalise_interrupted_turn`, when adding synthetic results, find the last user turn in session.turns that contains `ToolResultBlock`s and extend it rather than creating a new turn.
- If no such turn exists (interrupt before any tool completed), create one as before.
- ⚠️ `Turn.content` is a `list` (mutable) — we can append to it directly. But `ToolResultBlock` is frozen, which is fine since we're adding new blocks, not mutating existing ones.
- Add a test that interrupts mid-batch and verifies only one user turn with tool results exists in history.

Tasks:
- Rework `_finalise_interrupted_turn` to extend existing user turn
- Add test for the two-consecutive-user-turns scenario
- Verify with existing interrupt tests

Deliverable: Interrupting mid-tool-batch never produces consecutive user turns.

Verify: `uv run pytest tests/test_agent.py -k interrupt` — all pass, new test specifically checks turn structure.

### 2. Replace 7-tuple with RequestResult dataclass + promote magic numbers

Approach:
- Add a `@dataclass` `RequestResult` at module level in `agent.py` with fields: `text_chunks`, `tool_use_blocks`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `stop_reason`.
- `_do_request` returns `RequestResult`.
- Add module constants: `MAX_TOOL_ITERATIONS = 50`, `CONSECUTIVE_WARN = 3`, `CONSECUTIVE_BLOCK = 4`.

Tasks:
- Define `RequestResult` dataclass
- Update `_do_request` return type and `run_turn` call site
- Extract magic numbers to module constants

Deliverable: No bare numeric literals for thresholds; `_do_request` returns a named dataclass.

Verify: `uv run pytest tests/test_agent.py` passes; `grep -n "range(50)\|>= 4\|== 3" src/archie/agent.py` returns zero.

### 3. Unify wire serialization

Approach:
- `_build_context()` returns `list[Turn]` — for evicted results, create new `Turn` objects with stub `ToolResultBlock`s (content replaced with the eviction stub string). No dicts.
- `BedrockClient.stream()` signature changes to `messages: list[Turn]`. Remove the `list[dict]` union and the `isinstance` check.
- `_turns_to_bedrock_messages` remains the sole serialization point. It just reads `block.content` — whether that's the original or a stub string, it serializes the same way.
- ⚠️ Since `ToolResultBlock` is frozen, we must create new instances for evicted results (can't mutate `.content` in place). Build new `Turn` objects for evicted turns.
- The cache point is appended in `stream()` after serialization (as it is today) — that stays as-is.

Tasks:
- Rework `_build_context` to return `list[Turn]`
- Remove dict-building from `agent.py` entirely
- Change `BedrockClient.stream()` to accept only `list[Turn]`
- Update the `invoke()` method signature if needed (it uses raw dicts for memory extraction — leave that path alone since it's called from outside the agent)

Deliverable: `agent.py` contains zero Bedrock wire-format dict construction.

Verify: `uv run pytest && grep -n "toolResult\|toolUse\|\"role\"" src/archie/agent.py` returns zero matches (only in bedrock.py).

### 4. Extract composition root + fix atexit + memory helper

Approach:
- Add `_build_stack(self)` to `ArchieApp` — creates and assigns `self.session`, `self.sandbox`, `self.artifact_store`, `self.tool_registry`, `self._agent`. Called from `__init__` (after config/llm setup) and from `action_new_session` (after destroying old sandbox).
- Register atexit once in `__init__` with a lambda that calls `self.sandbox.destroy()`. Remove the per-session register in `action_new_session`.
- Extract `_extract_memory(self)` helper that does the MemoryExtractor construction + guard. Call from `action_quit` and `_run_memory_extraction`.

Tasks:
- Implement `_build_stack()`
- Rework `__init__` and `action_new_session` to use it
- Fix atexit to register once
- Extract `_extract_memory()` helper

Deliverable: `action_new_session` is <10 lines; no duplicated object-graph construction.

Verify: `uv run pytest && uv run ruff check src`

### 5. Sweep stale references + minor cleanups

Approach:
- `grep -ri "engine" src/` and fix all stale docstring references.
- Fix README default model vs `DEFAULT_CONFIG` mismatch (make them agree on `eu.anthropic.claude-fable-5`).
- Rename `_detect_git_branch` → `detect_git_branch` and update the import in app.py.
- Extract `_estimated_next_input` property in Session (used by both `context_pct` and `context_warning`).
- Add a comment to `ArtifactStore` documenting the in-memory-only coupling.

Tasks:
- Fix all "Engine" references in docstrings
- Align README default model with config.py
- Rename `_detect_git_branch`
- Extract `Session._estimated_next_input` property
- Document ArtifactStore coupling

Deliverable: Zero stale Engine references; README and code agree on default model.

Verify: `grep -ri "engine" src/` returns nothing; `uv run pytest && uv run ruff check src tests`
