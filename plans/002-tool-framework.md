# Tool Framework + Engine

## Objective

Add a tool framework to archie-nextgen that enables the model to call tools (starting
with file read and file search), including an Engine layer that separates orchestration
logic from the UI to support the tool-use loop and future extensibility.

## Context

- Phase 1 (core chat loop) is complete — Textual UI, Bedrock streaming, session persistence
- Architecture review identified the need for an orchestration layer before adding tools
  (the tool-use loop doesn't fit in the UI code)
- Provider portability: Bedrock-specific format must not leak beyond the LLM client module
- This is a learning project — prefer clear, well-documented code over maximum abstraction
- The roadmap shows sandboxing (Phase 3) and shell tool (Phase 4) come next, so the tool
  framework must be extensible without modification

## Requirements

### Engine / Orchestration Layer

- MUST introduce an Engine class that owns the conversation loop
  - AC: send user message → stream response → detect tool_use → execute tools → loop → yield events
  - AC: The Textual app only handles display, not orchestration
- MUST define provider-agnostic internal message types
  - AC: Engine works with ContentBlock types (TextBlock, ToolUseBlock, ToolResultBlock)
  - AC: LLM client translates to/from provider wire format at the boundary
- MUST emit typed events from the Engine for the UI to consume
  - AC: TextDelta(text), ToolCallStart(tool_use_id, name, input), ToolCallResult(tool_use_id, name, content, is_error), TurnComplete(input_tokens, output_tokens, stop_reason)

### Tool Framework

- MUST support a tool registry where tools are registered by name with schema and handler
  - AC: ToolSpec contains name, description, schema (JSON Schema dict), handler (callable)
  - AC: Adding a new tool = one new file + one line in the registry setup function
- MUST send tool definitions to Bedrock as `toolConfig` in each request
  - AC: Model can see available tools and choose to call them
- MUST handle the tool-use loop
  - AC: When stopReason is "tool_use", all tool calls in the response are executed, results sent back, and the model continues generating
  - AC: Multi-tool responses work (model calls N tools, all execute before next LLM call)
- MUST truncate tool results to 4000 characters before adding to context
  - AC: Results exceeding the limit are truncated with a "[...truncated, N chars total]" indicator
- MUST handle tool execution errors gracefully
  - AC: Exception in a tool handler becomes an error ToolResultBlock sent to the model
  - AC: The model can see the error and attempt recovery (different approach, different args)
  - AC: Engine does not crash on tool errors

### File Read Tool

- MUST read files with optional line offset and limit
  - AC: `read_file(path, offset=0, limit=500)` returns file content
- MUST enforce path allowlist (cwd + subdirs + configured allowed_directories)
  - AC: Attempting to read outside allowed paths returns an error message, not the file
- MUST include line numbers in output (formatted as `  42|content`)
  - AC: Every line of output has its line number for easy reference
- MUST include file metadata: total line count, whether output was truncated
- MUST include pagination hint when truncated
  - AC: Response includes "Use offset=N to continue reading (showing lines M-N of T)"
- MUST detect and reject binary files
  - AC: Files with null bytes in the first 8KB return an error, not garbage
- SHOULD cap individual line length at 500 chars
  - AC: Lines exceeding 500 chars are truncated with "...[truncated]"

### File Search Tool

- MUST search file contents using regex pattern matching via ripgrep (`rg`)
  - AC: Returns matching lines with file path, line number, and content
- MUST include 2 context lines around matches by default
  - AC: Model gets enough surrounding code to understand each match
- MUST be case-insensitive by default
- MUST cap results at 50 matches
  - AC: If more than 50 matches exist, response includes pagination hint with offset
- MUST enforce the same path allowlist as file read
- MUST respect .gitignore (ripgrep does this by default)
- SHOULD support a file glob filter (e.g. `*.py`)
- SHOULD support pagination via offset parameter

### Configuration

- MUST add `tools.allowed_directories` to config (list of absolute paths, default empty)
  - AC: Files in these directories (and subdirs) can be read/searched in addition to cwd

### Loop Prevention + Efficiency

- MUST detect consecutive identical tool calls and warn/block
  - AC: Same tool with same args called 3 times → warning appended to result
  - AC: Same tool with same args called 4 times → hard block (error result returned)
  - AC: Counter resets when a different tool+args combination is called
- MUST deduplicate file reads by mtime
  - AC: If read_file is called with same (path, offset, limit) and file mtime hasn't
    changed since last read, return a stub ("file unchanged since last read") instead
    of re-sending content
  - AC: Cache invalidates when a different mtime is detected
  
### UI Integration

- MUST display tool calls in the conversation (tool name and arguments)
  - AC: User can see which tools the model is using and with what inputs
- MUST display tool results in the conversation
  - AC: Tool output is visible in the conversation flow
- MUST visually distinguish tool activity from text responses
  - AC: Tool calls have different styling (muted colours, monospace for args/results)

## Design

### Internal Message Types (`types.py`)

```python
@dataclass
class TextBlock:
    text: str

@dataclass
class ToolUseBlock:
    tool_use_id: str   # Generated by Bedrock, propagated through the system
    name: str          # Tool name (e.g. "read_file")
    input: dict        # Parsed arguments

@dataclass  
class ToolResultBlock:
    tool_use_id: str   # References the ToolUseBlock it's responding to
    content: str       # Tool output (text)
    is_error: bool     # True if the tool failed

type ContentBlock = TextBlock | ToolUseBlock | ToolResultBlock
```

Engine events (yielded to UI):
```python
@dataclass
class TextDelta:
    text: str

@dataclass
class ToolCallStart:
    tool_use_id: str
    name: str
    input: dict

@dataclass
class ToolCallResult:
    tool_use_id: str
    name: str
    content: str
    is_error: bool

@dataclass
class TurnComplete:
    input_tokens: int
    output_tokens: int
    stop_reason: str
```

### Engine Flow

```
engine.run(user_message):
    add user turn to session
    loop:
        call llm.stream(session.messages, system, tools)
        accumulate response content blocks (text + tool_use)
        yield TextDelta events as text chunks arrive
        
        if stop_reason == "end_turn":
            record assistant turn in session
            yield TurnComplete
            break
            
        if stop_reason == "tool_use":
            record assistant turn (with tool_use blocks) in session
            for each tool_use block:
                yield ToolCallStart
                execute tool handler (catch exceptions → error result)
                truncate result
                yield ToolCallResult
            record tool_result turn in session
            continue loop (call LLM again with tool results)
```

The Engine is a synchronous generator running in a Worker thread. Tools execute
synchronously inside the generator (blocking the thread, which is fine — it's a
background thread). Events are yielded between each step so the UI can update.

### LLM Client (`llm/bedrock.py`)

Responsibilities:
- Accept internal types (list of turns with ContentBlocks)
- Translate to Bedrock wire format for the request
- Parse EventStream back into internal event types
- Handle streaming tool-use (JSON argument accumulation from deltas)

The `Session.messages` property is REMOVED. Translation lives in the LLM client.

⚠️ Bedrock streams tool call arguments as JSON string deltas across multiple
`contentBlockDelta` events. The client must accumulate these and parse the complete
JSON when the content block ends (`contentBlockStop`). This requires tracking
"current block" state during stream iteration.

### Tool Registry (`tools/__init__.py`)

```python
@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict        # JSON Schema for the tool's input parameters
    handler: Callable[[dict], str]  # Takes parsed input, returns string result

class ToolRegistry:
    def register(self, spec: ToolSpec): ...
    def get(self, name: str) -> ToolSpec: ...
    def to_tool_config(self) -> list[dict]: ...  # Builds Bedrock toolConfig
```

Explicit registration in a `create_default_registry()` function — no auto-discovery.

### Path Access Control

Shared utility in `tools/__init__.py`:
```python
def validate_path(path: str, cwd: Path, allowed: list[Path]) -> Path:
    """Resolve path and verify it's under an allowed directory."""
```

### Persistence

ContentBlocks serialize to JSON with a type discriminator:
```json
[
    {"type": "text", "text": "Let me read that file."},
    {"type": "tool_use", "id": "tu_123", "name": "read_file", "input": {"path": "src/main.py"}}
]
```

The `turns.jsonl` summary uses the first text block (or tool name if no text).
Raw files store the full content block list.

### Config Change

```yaml
tools:
  allowed_directories: []
```

## Milestones

1. Internal types + Engine events + refactor Session
   Approach:
   - Create `src/archie/types.py` with ContentBlock types (TextBlock, ToolUseBlock,
     ToolResultBlock) and EngineEvent types (TextDelta, ToolCallStart, ToolCallResult,
     TurnComplete)
   - Change `Turn.content` from `str` to `list[ContentBlock]`
   - `add_turn()` still accepts a plain string for convenience (wraps in `[TextBlock(text)]`)
   - Update persistence: content blocks serialize with type discriminator
   - Update `turns.jsonl` summary: use first TextBlock's text (or tool name if no text)
   - Remove `Session.messages` property (translation moves to LLM client in milestone 2)
     but temporarily keep a compat version that the current app.py can use until milestone 5
   - ⚠️ This touches persistence format — existing sessions won't be loadable (acceptable,
     session resume isn't implemented yet)
   Tasks:
   - Create `src/archie/types.py`
   - Modify `Turn` dataclass and `Session.add_turn()`
   - Update `_persist_turn` and `_save_meta` for new content format
   - Keep backward-compat `Session.messages` (returns Bedrock format, will be removed in M5)
   - Update all tests for new Turn structure
   Deliverable: Session handles multi-block turns with proper serialization.
   Verify: `uv run pytest` passes. New tests cover: add text turn, add tool_use turn,
   add tool_result turn, persistence round-trips correctly.

2. LLM client refactor + tool-use parsing
   Approach:
   - Restructure: `llm.py` → `src/archie/llm/__init__.py` (re-exports) + `src/archie/llm/bedrock.py`
   - `BedrockClient.stream()` signature changes:
     - Input: `messages: list[Turn]` (internal types, client translates)
     - Input: `system: str`
     - Input: `tool_config: list[dict] | None` (pre-built Bedrock toolConfig)
     - Output: still `Generator[StreamEvent]` but with new event type `ToolUseEvent`
   - New stream events: `ToolUseEvent(tool_use_id, name, input)` — emitted when a complete
     tool call has been parsed from the stream
   - ⚠️ Tool call arguments arrive as JSON string fragments across multiple
     `contentBlockDelta` events. Must track current content block index, accumulate
     the `input` field as a string, then `json.loads()` it on `contentBlockStop`.
   - Handle mixed responses: text blocks followed by tool_use blocks in one assistant message
   - Keep the existing `TextDelta`, `Usage`, `Done` events unchanged
   Tasks:
   - Create `src/archie/llm/__init__.py` and `src/archie/llm/bedrock.py`
   - Move and refactor `BedrockClient` — add internal→Bedrock message translation
   - Add content block state tracking for streaming (current block type, accumulated data)
   - Add `ToolUseEvent` dataclass
   - Parse `contentBlockStart` (detect tool_use vs text), `contentBlockDelta` (accumulate
     args), `contentBlockStop` (emit ToolUseEvent with parsed JSON)
   - Update `Done` to correctly report stop_reason (critical: "tool_use" vs "end_turn")
   - Update all imports across the project
   - Tests: mock a multi-block response (text + tool_use), verify events
   Deliverable: LLM client parses tool-use responses and emits ToolUseEvent.
   Verify: Tests mock Bedrock EventStream with tool_use content blocks, verify TextDelta
   events for the text portion and ToolUseEvent with parsed name+input for the tool portion.

3. Tool registry + file tools
   Approach:
   - `src/archie/tools/__init__.py`: ToolSpec dataclass, ToolRegistry class,
     `validate_path()` utility, `truncate_result()` utility, `create_default_registry()`,
     `tool_result()` and `tool_error()` helper functions for consistent return formatting
   - `src/archie/tools/read_file.py`: handler reads file natively in Python (no subprocess),
     returns line-numbered content with metadata. Uses `Path.read_text()` + splitlines + slice.
     Includes binary detection (null bytes in first 8KB), line-length cap (500 chars),
     pagination hint when truncated ("Use offset=N to continue reading").
   - `src/archie/tools/search_files.py`: handler runs `rg` (ripgrep) via subprocess with
     `--json` output mode for structured parsing. Includes 2 context lines (`-C 2`),
     case-insensitive by default (`-i`), optional file glob filter (`-g`),
     pagination via offset/limit params. Respects .gitignore automatically.
   - Both tools use `validate_path()` to enforce the allowlist
   - Config update: add `tools: {allowed_directories: []}` to Config dataclass
   - `truncate_result(content, max_chars=4000)` with truncation indicator
   - ⚠️ rg `--json` outputs one JSON object per line (matches, context, begin/end) —
     parse each line, extract match data, format into readable output
   Tasks:
   - Create `tools/__init__.py` (ToolSpec, ToolRegistry, validate_path, truncate_result,
     tool_result, tool_error)
   - Create `tools/read_file.py` (spec + handler with line numbers, pagination hints,
     binary detection, line-length cap)
   - Create `tools/search_files.py` (spec + handler with rg subprocess, JSON parsing,
     context lines, pagination hints)
   - Update `config.py` and `Config` dataclass for tools section
   - Tests: read valid file (check line numbers in output), read with offset/limit,
     reject path outside allowlist, reject binary file, search finds matches,
     search respects cap and includes hint, truncation works
   Deliverable: Two working tools with path access control, truncation, and smart hints.
   Verify: `uv run pytest tests/test_tools.py` passes covering all cases above.

4. Engine — orchestration loop
   Approach:
   - `src/archie/engine.py`: Engine class with `run(user_message) → Generator[EngineEvent]`
   - Constructor takes: llm_client, session, tool_registry, system_prompt
   - Flow: add user turn → loop (call LLM → accumulate blocks → dispatch tools → loop)
   - Tool dispatch: for each ToolUseBlock, look up handler in registry, call it, catch
     exceptions (wrap in error ToolResultBlock), truncate result, yield events
   - Token tracking: accumulate Usage events across LLM calls in one engine turn
     (if tools cause multiple LLM calls, sum the tokens)
   - Engine yields EngineEvent types (from types.py), NOT raw LLM stream events
   - **Loop prevention**: track `(tool_name, args_hash)` → consecutive count. Reset counter
     when a different tool+args combination is called. At 3: append warning to tool result.
     At 4: return hard error, don't execute the tool.
   - **Mtime dedup**: maintain a dict of `(path, offset, limit) → mtime`. On read_file
     calls, check mtime before executing. If unchanged, return stub result without re-reading.
     Invalidate cache entry when mtime differs.
   - ⚠️ The Engine calls the LLM multiple times per user message when tools are involved.
     Each call adds to the session's message list. The session tracks the "real" turns
     (user → assistant → tool_result → assistant) while the Engine manages the loop.
   Tasks:
   - Create `engine.py` with Engine class
   - Implement message building (internal types → passed to LLM client)
   - Implement stream consumption (TextDelta pass-through, ToolUseEvent accumulation)
   - Implement tool dispatch loop
   - Implement error handling (tool exceptions → error result → send to model)
   - Implement token accumulation across LLM calls
   - Implement consecutive-call detection (warn at 3, block at 4, reset on different call)
   - Implement mtime dedup for read_file (cache + check + stub response)
   - Tests: mock LLM client, test plain response, single tool call, multi-tool,
     tool error recovery, token accumulation, consecutive-call warning/block, mtime dedup
   Deliverable: Engine handles the full conversation loop with tool use and loop prevention.
   Verify: `uv run pytest tests/test_engine.py` passes all cases.

5. Wire UI to Engine + tool display
   Approach:
   - `app.py`: replace direct LLM call with Engine. Worker runs `engine.run(message)`.
   - Map EngineEvents to Textual Messages (StreamChunk stays for text; new messages for
     tool events)
   - New widget `ToolCallMessage` in conversation.py: shows tool name, args (monospace),
     and result (monospace, muted). Always visible (no collapsing).
   - Remove the now-unused `Session.messages` compat property
   - Update status bar: token counts now come from TurnComplete event (summed across
     multiple LLM calls if tools were used)
   - ⚠️ The Engine may yield many events for one user message (text + tool + text + tool + 
     text...). Input stays disabled until TurnComplete arrives.
   Tasks:
   - Modify `app.py` to create Engine, run it in worker, handle new event types
   - Create ToolCallMessage widget (name, args, result with distinct styling)
   - Add new Textual Message classes for tool events
   - Remove Session.messages compat property
   - Update status bar handling for TurnComplete
   - End-to-end manual testing
   Deliverable: Full working tool-calling chat visible in the UI.
   Verify: Run `archie chat` in the project directory. Ask "read the pyproject.toml" —
   model calls read_file, file content appears in a tool result block, model summarises it.
   Ask "find all test files" — model calls search_files, matches appear, model lists them.
