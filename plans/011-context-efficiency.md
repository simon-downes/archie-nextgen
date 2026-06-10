# Plan 011: Context Efficiency — Tool Result Eviction + Prompt Caching

## Objective

Stop re-sending stale tool output on every LLM call. Keep the full conversation (user messages + assistant responses) but replace tool result content with one-line stubs after 2 turns. Add prompt caching for the static prefix. Add RTK to the sandbox for pre-compressed shell output.

## Context

- A 10-turn session currently costs $2+ because every prior tool result is re-sent on every call
- Turn 9 of the last session was 212K input tokens — $0.64 for a single LLM call
- The model's own assistant response already carries forward the understanding from tool results
- If the model needs old content again, it can re-read the file (mtime cache makes this fast on our side)
- RTK (Rust Token Killer) is a CLI proxy that compresses command output by 60-90% for 100+ commands

## Design

### What gets evicted

Only `ToolResultBlock.content` in turns older than N iterations (default 2) of the tool loop. Specifically:

- The `role: "user"` message that contains `toolResult` blocks → its content gets replaced with a stub
- The stub format: `"[evicted: {tool_name} — {summary} | id: {tool_use_id}]"` using our existing `summarise_tool_output()`
- The `toolUse` block in the assistant message stays (so the model can see what was called)

### What stays verbatim

- All user text messages (short, provide intent)
- All assistant text responses (carry forward reasoning)
- All `toolUse` blocks (show what was called + args)
- Tool results from the current and previous tool loop iteration (model needs them for continuity)
- The system prompt + tool definitions (cached)

### The eviction is only in the messages sent to the LLM

`session.turns` in memory keeps everything (for the session log, for context if needed). The eviction happens in a new step between "build messages" and "call LLM" — a view/projection, not a mutation.

### Artifact store for recovery

Full tool results are kept in an in-memory dict keyed by `tool_use_id`. A new `retrieve_artifact` tool lets the model re-fetch evicted content if the stub isn't enough. This is the safety valve — makes eviction non-lossy.

### Prompt caching

Add `cachePoint` block after the system prompt. Bedrock caches everything before it — system text + tool definitions are identical across calls, perfect cache candidates. Cached tokens cost 90% less.

### RTK in sandbox

Add RTK binary to the Dockerfile. Shell commands automatically get compact output (test results → failures only, git → one-liners, etc.). This reduces what enters context in the first place, compounding with eviction.

## Schema

### retrieve_artifact tool

```json
{
  "name": "retrieve_artifact",
  "description": "Retrieve the full content of a previously evicted tool result. Use when the summary stub is insufficient. Provide the tool_use_id from the original tool call.",
  "schema": {
    "type": "object",
    "properties": {
      "tool_use_id": {
        "type": "string",
        "description": "The tool_use_id of the original tool call whose result you want to retrieve."
      }
    },
    "required": ["tool_use_id"]
  }
}
```

## Implementation

### Changes to engine.py

The engine currently does:
```python
self.llm.stream(messages=self.session.turns, ...)
```

It becomes:
```python
messages = self._build_context(self.session.turns)
self.llm.stream(messages=messages, ...)
```

Where `_build_context()`:
1. Iterates through turns
2. For toolResult turns older than 2 loop iterations, replaces content with the stub
3. Returns the modified messages list (without mutating session.turns)

### Artifact store

```python
class ArtifactStore:
    """Keeps full tool results for retrieval after eviction."""

    def __init__(self):
        self._store: dict[str, str] = {}  # tool_use_id → full content

    def put(self, tool_use_id: str, content: str) -> None:
        self._store[tool_use_id] = content

    def get(self, tool_use_id: str) -> str | None:
        return self._store.get(tool_use_id)
```

Lives on the Engine instance, populated during tool execution.

### Eviction logic

```python
def _build_context(self, turns: list[Turn]) -> list[Turn]:
    """Build LLM-facing message list with old tool results evicted."""
    # Find the boundary: current loop iteration starts after the last user text message
    # Everything before that boundary minus 1 iteration is eligible for eviction
    ...
```

The "2 iterations" means: the current tool-use loop keeps full results, and the previous completed user turn keeps full results. Everything older gets evicted.

### Prompt caching (bedrock.py)

```python
system_blocks = [
    {"text": system_text},
    {"cachePoint": {"type": "default"}},  # Cache everything above
]
```

One-line change. If the SDK/model rejects it, fall back gracefully (try/except on the first call).

### RTK in Dockerfile

```dockerfile
# RTK — compresses shell output for LLM consumption
RUN curl -fsSL https://raw.githubusercontent.com/rtk-ai/rtk/refs/heads/master/install.sh | sh
```

The shell tool routes commands through `rtk` automatically when available.

### Summary generation for stubs

Reuse existing `summarise_tool_output()` from session.py. It's already computed and stored with each tool call. We just need to keep it accessible for the eviction step.

## Cost impact estimate

For the $2.18 session (10 turns):
- Without eviction: 212K input tokens on turn 9 ($0.64 per call)
- With eviction (2-turn window): ~30K input tokens on turn 9 (~$0.09 per call)
- With eviction + prompt caching: system prefix cached, ~$0.07 per call
- **Session total: ~$0.30 instead of $2.18 (85% reduction)**

## Milestones

### Review Resolutions

1. **Eviction boundary**: Per user-message, not per-iteration. The current `run()` invocation keeps all tool results full. The *previous* `run()` invocation also keeps full results. Everything older gets evicted. Simple rule: keep the last 2 complete user turns' tool results, evict the rest. Tracked by counting `flush_turn()` calls (each corresponds to one user message).

2. **Summary storage**: Store the summary on the ArtifactStore alongside the full content. When a tool result is stored: `store.put(tool_use_id, content=full_result, summary=summary_string)`. The `_build_context()` function retrieves the summary from the store when building stubs.

3. **_build_context return type**: Build a new `list[dict]` (Bedrock message format) directly, not a modified list of Turn objects. The `llm.stream()` already accepts this format internally (it translates turns to dicts). We skip the Turn→dict translation for evicted results and emit the stub directly. No copying of Turn objects needed.

4. **retrieve_artifact — how the model knows IDs**: Include the `tool_use_id` in the eviction stub format: `"[evicted: read_file — 245 lines | id: tooluse_abc123]"`. The model can see the ID and use it to retrieve if needed. The `toolUse` block above it also has the ID, so there's redundancy.

5. **RTK integration detail**: Call `rtk rewrite "<command>"` inside the sandbox (rtk installed there). If exit code 0 or 3, use stdout as the command. If exit code 1 or 2, run original command unchanged. If rtk binary not found, run original command. One extra exec call per shell command (~10ms overhead — negligible vs the command itself).

6. **Prompt caching placement**: Bedrock's `cachePoint` in the system block caches the system prompt text. Tool definitions in `toolConfig` are a separate top-level param and are cached separately by Bedrock automatically (they're stable across calls). The `cachePoint` in system is sufficient — no need to move tools into system.

7. **ArtifactStore access for retrieve_artifact tool**: Create the ArtifactStore *before* the registry. Pass it to `create_default_registry()` (similar to how we pass sandbox). The Engine also receives it. Both the tool and the engine reference the same store instance.

## Milestones (updated)

### Milestone 1: Artifact store + eviction logic

- Create artifact store (simple dict on Engine)
- Store full tool result content on each tool execution
- Implement `_build_context()` that replaces old tool results with stubs
- Store the summary from `summarise_tool_output()` alongside each tool result for stub generation
- Tests: eviction after N turns, stubs contain tool name + summary, recent results kept full

### Milestone 2: retrieve_artifact tool

- Create `src/archie/tools/retrieve_artifact.py`
- Handler looks up content from Engine's artifact store
- Register in tool registry
- Tests: retrieval of stored artifact, missing artifact error

### Milestone 3: Prompt caching

- Add `cachePoint` to system blocks in bedrock.py
- Graceful fallback if unsupported (catch specific error, retry without)
- No tests needed (API behaviour, verified by running)

### Milestone 4: RTK in sandbox

- Add RTK install to sandbox/Dockerfile
- Modify shell tool to route through `rtk` when available (detect with `which rtk`)
- Fallback: run command directly if rtk not found
- Tests: shell tool still works without rtk (mock subprocess)

### Milestone 5: Review

- Run review workflow
- End-to-end cost comparison (before/after on the benchmark prompt)
- Verify no behavioural regressions
