# Core Chat Loop

## Objective

Build a terminal-based AI chat application using Python, Textual, and AWS Bedrock that
serves as the foundation for a personal AI agent harness — a clean streaming chat loop with
session persistence, token tracking, and cost visibility.

## Context

- Next iteration of archie, currently running on kiro-cli
- Motivated by desire to own the runtime, control token economics, and learn the mechanics
- Existing archie code (SaaS integrations, persona, skills) will be ported later
- AWS Bedrock `converse_stream` API is the LLM backend (stateless, full context every turn)
- Sandboxing (Docker) will handle safety in future — no tool approval flows

## Requirements

### Core Chat Loop

- MUST send messages to AWS Bedrock via `converse_stream` and stream responses to the terminal
  - AC: User types a message, sees tokens appear incrementally as they're generated
- MUST maintain conversation history in-process and send full context on each API call
  - AC: Model can reference earlier messages in the conversation
- MUST support configurable model selection (model ID via config file)
  - AC: Changing model ID in config changes which Bedrock model is used
- MUST track and display actual token usage (input/output) from the Bedrock API response
  - AC: Status bar shows per-turn and cumulative input/output token counts
- MUST display estimated cost based on token counts and model pricing constants
  - AC: Running cost displayed in status bar, updates after each turn
- MUST support interrupt/cancel of in-progress generation
  - AC: User can press a key binding to abort streaming; partial response is kept in history
- MUST warn when approaching context window limit (threshold defined per model)
  - AC: Visual indicator changes when cumulative tokens exceed 80% of model's max context

### Session Persistence

- MUST persist sessions to disk in a structured format
  - AC: Session data survives process exit and can be inspected offline
- MUST split session storage into summary log (`turns.jsonl`) and raw payloads (`raw/`)
  - AC: `turns.jsonl` contains one entry per turn: role, content summary, token counts, timestamp
  - AC: `raw/{turn-id}.json` contains full content for that turn when it exceeds a size threshold
  - AC: Turns reference their raw file by ID
- MUST store session metadata (`meta.json`) including model, timestamps, cumulative tokens
  - AC: `meta.json` is human-readable and updated after each turn

### Terminal UI

- MUST use Textual for the terminal interface
  - AC: Application runs as a Textual app with structured layout
- MUST display messages in a scrollable conversation area with role distinction
  - AC: User and assistant messages are visually differentiated
- MUST render markdown in assistant responses
  - AC: Code blocks, bold, lists render correctly in the terminal
- MUST have an input area for composing messages (multiline support)
  - AC: User can type multiline messages before sending
- MUST display a status bar showing: model name, token counts (turn/cumulative), cost, context %
  - AC: Status bar is always visible and updates in real-time during streaming
- MUST display a help bar with keyboard shortcut hints
  - AC: Help bar is always visible outside the tabbed content area

### Configuration

- MUST load configuration from `~/.archie/nextgen.yaml`
  - AC: Application reads model ID and AWS region from config
  - AC: Missing config file produces a helpful error message
- SHOULD support environment variable overrides for AWS credentials
  - AC: Standard AWS credential chain (env vars, ~/.aws/credentials, IAM role) works

### Project Structure

- MUST use `uv` for project management with `pyproject.toml`
- MUST follow existing archie code conventions (ruff, pytest)
- SHOULD be installable as a CLI command (`archie` entry point via click)

## Design

### Project Layout

```
archie-nextgen/                  # repo root
├── pyproject.toml               # uv, hatchling
├── src/archie/
│   ├── __init__.py
│   ├── cli.py                   # click entry point
│   ├── config.py                # load ~/.archie/nextgen.yaml
│   ├── models.py                # model constants (max tokens, pricing)
│   ├── llm.py                   # Bedrock converse_stream wrapper
│   ├── session.py               # conversation state, persistence
│   └── ui/
│       ├── __init__.py
│       ├── app.py               # Textual App class
│       ├── conversation.py      # message display widget
│       ├── input.py             # message input widget
│       └── status.py            # status bar widget
└── tests/
    ├── __init__.py
    ├── test_config.py
    ├── test_session.py
    └── test_llm.py
```

### Config (`~/.archie/nextgen.yaml`)

```yaml
model: "anthropic.claude-sonnet-4-20250514-v1:0"
region: "us-east-1"

system_prompt: |
  You are a helpful assistant. Be direct and concise.
```

Model properties (max tokens, pricing) are code constants, not user config.

### Model Constants (`src/archie/models.py`)

```python
MODELS = {
    "anthropic.claude-sonnet-4-20250514-v1:0": {
        "name": "Claude Sonnet",
        "max_context_tokens": 200_000,
        "input_price_per_m": 3.0,
        "output_price_per_m": 15.0,
        "context_warning_threshold": 0.8,
    },
    # add more as needed
}
```

### Session Storage (`~/.archie/sessions/`)

```
~/.archie/sessions/
└── {YYYYMMDD-HHMM-xxxx}/       # date + 4 random hex
    ├── meta.json
    ├── turns.jsonl
    └── raw/
        └── {turn-id}.json
```

### UI Layout

```
┌─ TabbedContent ────────────────────────────────────────┐
│ [Session 1]                                            │
├─ TabPane ──────────────────────────────────────────────┤
│                                                        │
│  Scrollable conversation (RichLog)                     │
│                                                        │
├────────────────────────────────────────────────────────┤
│ sonnet │ turn: 1.2K/340 │ total: 4.8K/1.2K │ ctx: 3% │ $0.02 │
├────────────────────────────────────────────────────────┤
│ > input area (TextArea)                                │
│                                                        │
╞════════════════════════════════════════════════════════════╡
│ Enter: send │ Esc: cancel │ Ctrl+Q: quit │ Ctrl+N: new │
└────────────────────────────────────────────────────────┘
```

- TabbedContent + TabPane: one tab per session (single for now)
- RichLog: conversation display with markdown rendering
- Status bar: Static widget inside TabPane
- Input: TextArea inside TabPane
- Footer: Textual Footer widget (outside tabs) for key bindings

### Bedrock Integration

- `boto3` bedrock-runtime client, `converse_stream` operation
- Synchronous EventStream iterator run in thread via Textual Worker
- Stream events: `contentBlockDelta` → text, `metadata` → usage, `messageStop` → done
- Internal event types: `TextDelta(text)` | `Usage(input_tokens, output_tokens)` | `Done(stop_reason)`
- Errors: throttle → retry with backoff, validation → context too large error, timeout → retry once

### Interrupt

- Key binding (Esc) cancels the Textual Worker
- Partial response saved in session with `interrupted: true`
- Worker uses a threading Event to signal cancellation to the stream reader

## Milestones

1. Project scaffold + Textual app shell
   Approach:
   - Use `uv init` then `uv add` for dependencies (textual, boto3, click, pyyaml)
   - hatchling build backend, src layout, ruff + pytest config matching archie conventions
   - Textual app: subclass `App`, compose with `TabbedContent` > `TabPane` containing
     `RichLog` + status `Static` + `TextArea`, then `Footer` outside tabs
   - Populate with placeholder messages to validate layout renders correctly
   - `Footer` handles key binding display automatically from app `BINDINGS`
   - ⚠️ Textual's `TextArea` submit behaviour needs custom key binding (Enter to send)
     vs Shift+Enter or similar for newline — decide: Enter sends, Shift+Enter for newline
   Tasks:
   - `uv init`, configure pyproject.toml (hatchling, ruff, pytest, src layout)
   - `uv add textual boto3 click pyyaml` and `uv add --dev ruff pytest`
   - Create `src/archie/ui/app.py` — App class with composed layout
   - Create `src/archie/ui/conversation.py` — RichLog-based message display
   - Create `src/archie/ui/status.py` — status bar with placeholder values
   - Create `src/archie/ui/input.py` — TextArea with Enter-to-send binding
   - Create `src/archie/cli.py` — click group with `chat` command launching the app
   - Add app BINDINGS for Ctrl+Q (quit), Esc (placeholder cancel), Ctrl+N (placeholder)
   - Add placeholder messages (user + assistant with markdown) to verify rendering
   Deliverable: Textual app launches with full layout, placeholder content renders correctly, key bindings work.
   Verify: `uv run archie chat` opens TUI with placeholder messages, markdown renders, status bar visible, help footer shows bindings, Ctrl+Q quits.

2. Config + session persistence
   Approach:
   - Config: plain dataclass loaded from PyYAML, validate required fields (model, region)
   - Write a default config template on first run if `~/.archie/nextgen.yaml` doesn't exist
   - Model constants: dict keyed by model ID containing pricing, max tokens, threshold
   - Fail clearly if configured model ID isn't in the constants dict
   - Session ID format: `{YYYYMMDD}-{HHMM}-{4 random hex}` (sortable, no collisions)
   - Session dir created on first message, not on app start (no empty sessions)
   - `turns.jsonl`: append-only, one JSON line per turn
   - `raw/`: only populated when turn content > 500 chars
   - `meta.json`: rewritten after each turn with cumulative stats
   Tasks:
   - Create `src/archie/config.py` — load yaml, validate, return typed config dataclass
   - Create `src/archie/models.py` — model constants dict (sonnet to start)
   - Implement first-run: create `~/.archie/` dir and write default config if missing
   - Create `src/archie/session.py` — Session class with messages list, token counters
   - Implement `session.add_turn()` — appends to JSONL, writes raw if over threshold
   - Implement `session.save_meta()` — writes meta.json with cumulative stats
   - Tests: config loading (valid, missing file, bad yaml, unknown model), session persistence
   Deliverable: Config loads from `~/.archie/nextgen.yaml`, model constants resolve, session turns persist to `~/.archie/sessions/{id}/`.
   Verify: `pytest tests/test_config.py tests/test_session.py` passes. Manually: run app, check `~/.archie/nextgen.yaml` created on first run.

3. Bedrock streaming integration
   Approach:
   - `boto3` client created once per session using configured region
   - `converse_stream` is synchronous — wrap in Textual Worker (runs in thread)
   - EventStream iteration: read events in a loop, yield typed dataclass events
   - Must handle `contentBlockStart`, `contentBlockDelta`, `metadata`, `messageStop`
   - Token usage comes in the `metadata` event after streaming completes
   - ⚠️ EventStream must be iterated in the thread that created it — no cross-thread passing
   - Retry: on `ThrottlingException` retry up to 3 times with exponential backoff
   - On `ValidationException` (context too large): surface error to user, don't retry
   - Cost calculation: `(input_tokens * input_price / 1_000_000) + (output_tokens * output_price / 1_000_000)`
   Tasks:
   - Create `src/archie/llm.py` — `BedrockClient` class
   - Implement message format conversion: session messages → Bedrock messages format
   - Implement `stream_response()` — calls converse_stream, iterates events, yields typed events
   - Implement retry logic for throttling
   - Implement cost calculation utility
   - Tests: mock boto3 client, verify event parsing, verify retry behaviour, verify cost calc
   Deliverable: `BedrockClient` streams responses from Bedrock, yields text deltas and usage metadata with cost.
   Verify: `pytest tests/test_llm.py` passes. Manual: small script sends a message to Bedrock and prints streaming output + token counts (requires AWS credentials).

4. Wire it together — live chat
   Approach:
   - On input submit: add user message to session, spawn Worker that calls BedrockClient
   - Worker posts custom Textual messages (TextDelta, StreamDone) back to app
   - App handles TextDelta by appending to RichLog incrementally
   - On StreamDone: add assistant turn to session, persist, update status bar
   - Status bar: reads from session object (cumulative tokens, cost, context %)
   - Interrupt: Esc binding sets a cancel flag checked by the worker's stream loop,
     partial content saved with `interrupted: true`
   - Context warning: status bar turns red/yellow when context % > threshold
   - ⚠️ Must disable input while streaming (prevent sending during generation)
   Tasks:
   - Wire input submit → session.add_turn(user) → spawn stream worker
   - Implement worker: build messages from session, call llm.stream_response(), post events
   - Handle TextDelta in app: append to conversation RichLog
   - Handle StreamDone: finalise assistant turn, update status, persist session
   - Implement interrupt: Esc cancels worker, saves partial response
   - Implement input disable during streaming (re-enable on done/interrupt)
   - Update status bar with live data: model short name, turn tokens, cumulative, ctx %, cost
   - Implement context warning visual (style change when over threshold)
   - End-to-end manual test
   Deliverable: Full interactive chat — send messages, see streaming responses, token/cost tracking live, interrupt works, sessions persist.
   Verify: `uv run archie chat` — type a message, see streaming response, status bar shows real token counts and cost. Esc interrupts mid-stream. After Ctrl+Q, `~/.archie/sessions/` contains the conversation with correct meta.json and turns.jsonl.
