# Plan 007: Session Logging Refactor

## Objective

Replace the current multi-file session persistence (meta.json + turns.jsonl + raw/*.json) with a single JSONL file per session. One line per user turn. Format matches the existing brain digest schema (field names, structure) but in JSON instead of YAML.

## Context

- Current approach: directory per session with 3+ files — brittle, lots of small files
- Brain's existing digest format captures: user text, assistant text, tool summaries (name + target + success)
- We match that schema for brain compatibility but use JSONL for speed and simplicity
- Full tool output is not needed — summarise to exit codes, file paths, match counts etc.
- Keep full user prompts and full assistant responses (no truncation on either)
- A debug/raw log (capturing full LLM request/response) can be added later as a separate concern

## Design Decisions

### Single JSONL file per session

```
~/.archie/sessions/{session-id}.jsonl
```

- Every line is a turn entry — no header, no preamble
- One line per user turn, append-only
- Session ID format: `YYYY-MM-DD-{project}-{short-hash}` (e.g. `2026-06-08-archie-nextgen-d8c3b`)

### One entry per user turn

A "turn" is everything triggered by a single user input: the user's text, all tool calls and their summarised results, and the complete final assistant response. Multiple LLM calls (from tool loops) are collapsed into one turn. Each turn gets a ULID as its identifier (time-sortable, globally unique — requires `python-ulid` dependency).

### Tool output summarisation

We capture the full tool input (command, path, pattern, glob — what was *requested*) but only summarise the output (what came *back*). This covers debugging without bloating the log:

| Tool | Input captured (full) | Output summarised as |
|------|----------------------|---------------------|
| read_file | path, offset, limit | lines read, or error |
| write_file | path | lines written, or error |
| edit_file | path, edit count | edits applied, or error |
| list_files | glob, path | file count |
| search_files | pattern, glob, path | match count |
| shell | command (full) | exit code, output lines count |

### No truncation on user/assistant text

Unlike the existing digest (which caps assistant at 2000 chars), we keep both user and assistant text in full. This is a single-purpose log file, not a multi-session summary — disk is cheap and full context helps memory formation.

## Schema

### File structure

```
~/.archie/sessions/2026-06-08-archie-nextgen-d8c3b.jsonl
```

Pure turn entries, one per line, no header. Session metadata is derivable:
- Session ID → filename
- Project → in the filename
- Model → in each turn's `metadata.model`
- Started → `when` of the first turn
- Ended → `when` of the last turn

### Turn lines (one per user turn)

```json
{
  "id": "01J5KXQV9AMRN4T1JGPZ8K3QFH",
  "when": "2026-06-08 08:30:15",
  "user": "find test files and run them",
  "assistant": "Found 8 test files. Running pytest...\nAll 129 tests pass.",
  "tools": [
    {"id": "tooluse_abc", "name": "list_files", "input": {"glob": "**/test_*.py"}, "success": true, "summary": "8 files"},
    {"id": "tooluse_def", "name": "shell", "input": {"command": "uv run pytest tests/ -q"}, "success": true, "summary": "exit 0, 3 lines"}
  ],
  "metadata": {
    "model": "eu.anthropic.claude-sonnet-4-6",
    "input_tokens": 7100,
    "output_tokens": 250,
    "cost": 0.013,
    "interrupted": false
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| id | string | yes | ULID for this turn (time-sortable, globally unique) |
| when | string | yes | Timestamp of user submission |
| user | string | yes | Full user input text |
| assistant | string | yes | Full assistant text (all LLM calls concatenated). "Response was interrupted by the user" if cancelled before any output. |
| tools | list | no | Tool calls during this turn. Omitted if no tools used. |
| tools[].id | string | yes | tool_use_id from the model |
| tools[].name | string | yes | Tool name |
| tools[].input | object | yes | Full tool input parameters |
| tools[].success | bool | yes | Whether the tool succeeded |
| tools[].summary | string | yes | Short summary of the output |
| tools[].error | string | no | Error message (only when success=false) |
| metadata | object | yes | Turn accounting/execution info |
| metadata.model | string | yes | Model used for this turn |
| metadata.input_tokens | int | yes | Total input tokens across all LLM calls |
| metadata.output_tokens | int | yes | Total output tokens across all LLM calls |
| metadata.cost | float | yes | Total cost for this turn in USD |
| metadata.interrupted | bool | yes | Whether the user cancelled before completion |

## Tool output summarisation rules

Each tool type has a specific summarisation function that produces a short string:

```python
def _summarise_tool_output(name: str, input: dict, output: str, is_error: bool) -> str:
    if is_error:
        # First line of error, capped at 100 chars
        return output.split("\n")[0][:100]

    match name:
        case "read_file":
            # Count lines in output (after the header)
            lines = output.count("\n")
            return f"{lines} lines"
        case "write_file":
            # Already returns "Written: path (N lines)"
            return output
        case "edit_file":
            # Already returns "Edited: path (N edits applied)"
            return output
        case "list_files":
            files = output.strip().count("\n") + 1 if output.strip() else 0
            return f"{files} files"
        case "search_files":
            matches = output.count("\n")
            return f"{matches} matches"
        case "shell":
            # Parse exit code from our format "$ cmd\n[exit: N]\n..."
            lines = output.strip().split("\n")
            exit_line = next((l for l in lines if l.startswith("[exit:")), "")
            output_lines = max(0, len(lines) - 2)  # minus command + exit line
            return f"{exit_line}, {output_lines} lines"
        case _:
            return f"{len(output)} chars"
```

## Review Resolutions

1. **Session ID construction**: `Session` takes `project_name` as a constructor parameter (passed from app.py which already has `self.project_dir.name`). Short-hash is 5 random hex chars (`secrets.token_hex(3)[:5]`). Collision risk is negligible (1M values, max a few sessions per day).

2. **Tool summarisation timing**: Summarise tool output *before* truncation. The engine calls `_summarise_tool_output(name, input, raw_output)` immediately after tool execution, stores the summary string, then passes the full output to `truncate_result()` for context. The summary is a separate field, not derived from the truncated version.

3. **In-memory state + accumulation**: `add_turn` continues to record all intermediate turns in `session.turns` (user, assistant+tool_use, tool_results, assistant, etc.) — this is what builds LLM context. The engine *separately* accumulates a `TurnData` dict during its run loop for `flush_turn()`. No conflict — they serve different purposes.

4. **Interrupted turn flushing**: Happens in `app.py`'s `on_stream_complete` handler when `event.interrupted=True`. The app calls `session.flush_turn()` with whatever the engine accumulated before cancellation. The engine stores its in-progress accumulation on the `Engine` instance (not just local vars) so it's accessible after the generator is abandoned.

5. **ULID justification**: ULIDs provide a single opaque ID useful for cross-referencing turns from external systems (brain memory entries can reference specific turns). Simpler than composite keys. If the dependency feels heavy, `ulid` can be replaced with `f"{timestamp_ms:012x}{random_hex:10s}"` — same format, no library. Decision: use `python-ulid` for now, it's small and well-maintained.

6. **Cost calculation**: Use existing `calculate_cost(model_info, input_tokens, output_tokens)` with per-turn token totals. The function already accepts arbitrary token counts.

## Changes to existing code

### Session module (`src/archie/session.py`)

- Remove: `_persist_turn`, `_save_meta`, `_ensure_dir`, `_summarise_content`, raw/ directory logic
- Remove: per-role persistence (no more writing on every `add_turn`)
- Keep: `add_turn` for in-memory state (building context for next LLM call)
- Add: `flush_turn(turn_data)` — appends one JSONL turn line
- Add: `_summarise_tool_output()` helper
- Add: ULID generation for turn IDs
- Session ID: `YYYY-MM-DD-{project}-{short_hash}` (e.g. `2026-06-08-archie-nextgen-d8c3b`)
- Sessions dir: `~/.archie/sessions/` (flat, no subdirectories)

### Engine module (`src/archie/engine.py`)

- Accumulate turn data during the run loop: user text, tool calls with results, total tokens
- At end of `run()`, build the turn dict and call `session.flush_turn()`
- For interrupted turns: flush whatever was accumulated with `interrupted: true`

### App module (`src/archie/ui/app.py`)

- Remove any references to old session dir structure
- Session path is now `SESSIONS_DIR / f"{session_id}.jsonl"`

## Milestones

### Milestone 1: New session persistence

- Rewrite `Session` with `flush_turn()` method
- Generate session ID as `YYYY-MM-DD-{project}-{hash}`
- Implement `_summarise_tool_output()` for each tool type
- `add_turn` becomes memory-only (no disk writes)
- Write `{id}.jsonl` to `~/.archie/sessions/`
- Add `python-ulid` dependency
- Tests: turn appended, tool summarisation, interrupted turn handling, session ID format

### Milestone 2: Engine integration

- Modify engine `run()` to accumulate turn data (user text, tools, tokens)
- Call `session.flush_turn()` at end of run
- Handle interrupted turns (flush partial data with flag)
- Update engine tests

### Milestone 3: Cleanup + review

- Remove old persistence code (raw/, meta.json, turns.jsonl, directory-per-session)
- Update SESSIONS_DIR usage
- Run review workflow
- Verify: session files readable by `jq` (e.g. `jq -s '.[1:]' session.jsonl` for turns)
