# Plan 018: Ollama Provider Support

## Objective

Introduce an LLM client protocol and implement Ollama as a second backend provider. Models
from both Bedrock and Ollama should be switchable mid-session transparently — the agent loop,
session, and tools don't know or care which provider is serving responses.

## Context

- Current architecture has a single `BedrockClient` hardcoded as the LLM provider
- The `ollama` Python library (≥0.6.1, MIT, 10k stars) handles HTTP transport, streaming,
  tool-call parsing, and typed responses
- Agent loop uses exactly one method: `self.llm.stream(messages, system, tool_config)`
- Memory system uses `self._client.invoke(messages, system)` with raw dict messages
- Model switching currently mutates `self.llm.model_id` on the same client
- Two initial Ollama models: `qwen3.6:35b` and `gemma4:31b` (static registry, curated)

## Requirements

### Provider abstraction

- MUST define an `LLMClient` protocol with `stream()` and `invoke()` methods
  - AC: `stream()` signature: `(messages: list[Turn], system: str, tool_config: list[dict] | None) -> Generator[StreamEvent]`
  - AC: `invoke()` signature: `(messages: list[Turn], system: str) -> str`
  - AC: `BedrockClient` satisfies the protocol (minor change: `invoke()` takes `list[Turn]` instead of `list[dict]`)
  - AC: `AgentLoop` type-hints `llm_client` as `LLMClient`, not `BedrockClient`

- MUST support switching between providers mid-session (e.g. Bedrock → Ollama → Bedrock)
  - AC: switching preserves conversation history and sandbox state
  - AC: the next turn after switching uses the new provider

### Ollama client

- MUST implement `OllamaClient` satisfying the `LLMClient` protocol
  - AC: `stream()` yields the same `StreamEvent` types (`TextDelta`, `ToolUseEvent`, `Usage`, `Done`)
  - AC: `invoke()` accepts `list[Turn]` and returns a string response (used by memory extraction)

- MUST translate tool schemas from Bedrock format to Ollama/OpenAI format
  - AC: Bedrock `toolSpec` → OpenAI `{"type": "function", "function": {"name", "description", "parameters"}}` format
  - AC: all existing tools are callable by Ollama models

- MUST handle Ollama tool call responses correctly
  - AC: `tool_calls` from Ollama responses are mapped to `ToolUseEvent` with ULID-generated `tool_use_id`
  - AC: tool results are sent back as `role: "tool"` messages with `tool_name` field in subsequent requests

- MUST normalise stop_reason to match agent loop expectations
  - AC: Ollama `done_reason: "stop"` → `Done(stop_reason="end_turn")`
  - AC: presence of tool_calls in response → `Done(stop_reason="tool_use")`
  - AC: Ollama length limit → `Done(stop_reason="max_tokens")`
  - Why: agent loop branches on `stop_reason not in ("tool_use", "max_tokens")` to decide whether to continue

- MUST handle streaming responses
  - AC: text content streams incrementally (yields `TextDelta` as chunks arrive)
  - AC: tool calls are emitted as complete `ToolUseEvent` when the stream finishes them

- MUST connect to a configurable Ollama host
  - AC: default host is `http://localhost:11434`
  - AC: host is configurable via `ollama.host` in `nextgen.yaml`

- MUST handle connection failures gracefully
  - AC: if Ollama is unreachable, error message says "Ollama is not reachable at <host>"
  - AC: timeout on requests (default 120s for generation)

### Model registry

- MUST add Ollama models to the static `MODELS` registry in `models.py`
  - AC: entries for `qwen3.6:35b` and `gemma4:31b`
  - AC: pricing fields are all 0.0 (free local inference)
  - AC: `ModelInfo` gains a `provider` field (`"bedrock"` or `"ollama"`)
  - AC: existing entries default to `provider="bedrock"` — no changes needed

- MUST use the `provider` field to route model switches to the correct client
  - AC: switching to an Ollama model creates/reuses an `OllamaClient`
  - AC: switching to a Bedrock model creates/reuses a `BedrockClient`
  - AC: switching between two models on the same provider mutates `model_id` (no new client)

### Cost and usage

- SHOULD report token usage from Ollama responses when available
  - AC: `Usage` events emitted with `input_tokens` from `prompt_eval_count` and `output_tokens` from `eval_count`
  - AC: `cache_read_input_tokens` and `cache_write_input_tokens` are always 0 for Ollama

- MUST show $0.00 cost for Ollama models in the status bar
  - AC: cost calculation uses the 0.0 pricing from model info — no special-casing needed

### Configuration

- SHOULD allow setting an Ollama model as the default in `nextgen.yaml`
  - AC: `model: "qwen3.6:35b"` in config starts with the Ollama provider
  - AC: config validation still catches unknown model IDs

- MAY support `ollama.host` and `ollama.timeout` config fields
  - AC: if absent, defaults to `http://localhost:11434` and 120s respectively

### Scope exclusions

- MemoryExtractor keeps its own `BedrockClient` instance (stays on Haiku). Only change is passing `list[Turn]` instead of `list[dict]` to `invoke()`. Wiring it to use the protocol for provider selection is out of scope.
- No dynamic model discovery from Ollama — registry is static and curated.
- No prompt caching for Ollama (no equivalent mechanism).

## Design

### Overview

Introduce a `LLMClient` protocol in `src/archie/llm/__init__.py`, add `OllamaClient` as a new
module alongside `BedrockClient`, and add provider-aware model routing in the UI layer. The
`ollama` Python library handles all HTTP/streaming/parsing. The agent loop and tool framework
remain unchanged.

### Technical Stack

- `ollama>=0.6.1` — HTTP transport, streaming, tool-call parsing, typed responses. Pinned minimum in pyproject.
- No other new dependencies. `ulid` already available for ID generation.

### Code Structure

- `src/archie/llm/__init__.py` — add `LLMClient` protocol, re-export `OllamaClient`
- `src/archie/llm/ollama.py` — new module: `OllamaClient` implementing the protocol
- `src/archie/models.py` — add `provider` field to `ModelInfo`, add Ollama model entries
- `src/archie/config.py` — add `OllamaConfig` dataclass, parse `ollama` section from YAML
- `src/archie/ui/app.py` — provider-aware client instantiation and model switching
- `tests/test_ollama.py` — unit tests for the Ollama client

### Key Decisions

- **Protocol, not ABC** — `typing.Protocol`. Both clients already implement the right methods;
  no inheritance needed. `BedrockClient` satisfies it without changes.

- **`invoke()` uses `list[Turn]`** — both protocol methods speak the same internal types.
  `BedrockClient.invoke()` changes from `list[dict]` to `list[Turn]` (reuses the existing
  `_turns_to_bedrock_messages()` translator). `MemoryExtractor` passes
  `[Turn(id="", role="user", content=[TextBlock(text=...)])]` instead of raw dicts.
  One-line change, zero risk, fully provider-agnostic protocol.

- **Tool schema translation lives in `OllamaClient`** — accepts Bedrock-format `tool_config`
  from `ToolRegistry.to_tool_config()` and translates to OpenAI format internally. Agent loop
  and registry are unchanged.

- **Message translation lives in `OllamaClient`** — `_turns_to_ollama_messages()` maps internal
  `Turn` objects to Ollama's format. Same translator used for both `stream()` and `invoke()`.

- **Generated tool_use_ids** — `str(ULID())` from the `ulid` package (already a dependency).
  Sortable by time, consistent with existing ID patterns.

- **Stop reason normalisation** — Ollama's `done_reason` values mapped to Bedrock equivalents:
  `"stop"` → `"end_turn"`, tool_calls present → `"tool_use"`, `"length"` → `"max_tokens"`.

- **Provider routing** — UI holds a `dict[str, LLMClient]` of provider instances (lazily
  created). `switch_model()` checks `ModelInfo.provider`, creates client if needed, updates
  both `self.llm` and `self.agent.llm`. Within-provider switches just mutate `model_id`.

- **System prompt** — Ollama accepts system as a first message with `role: "system"` in the
  messages list. Client prepends it transparently.

- **Timeout** — 120s default for generation (local models can be slow on large contexts).
  Connection timeout stays at httpx default (5s). Configurable via `ollama.timeout`.

- **No retry logic** — local server doesn't throttle. Just timeout and connection error handling.

### Risks

- **Tool-calling quality** — local models are weaker at structured tool-calling than Claude.
  Malformed tool calls get `input_truncated=True` (same as Bedrock's max_tokens truncation)
  and the agent sends an error result asking the model to retry.
- **Context length** — no `max_context_tokens` enforcement from Ollama's side. If a session
  exceeds the model's context, Ollama silently truncates. Noted but not blocked.

## Milestones

### 1. LLMClient protocol and ModelInfo provider field

Approach:
- Define `LLMClient` as `typing.Protocol` in `src/archie/llm/__init__.py` with exactly two
  methods: `stream(messages, system, tool_config)` and `invoke(messages, system)`
- Add `provider: str = "bedrock"` to `ModelInfo` — default preserves all existing entries
- Ollama models use the Ollama model tag as their registry key (e.g. `qwen3.6:35b`)
- Context window sizes: use the model's declared context from Ollama docs (qwen3.6:35b = 128K,
  gemma4:31b = 128K). `max_output_tokens` set conservatively (16K for both)
- ⚠️ Don't change any runtime behaviour — this is a pure type/registry change

Tasks:
- Add `LLMClient` protocol to `src/archie/llm/__init__.py` with `stream()` and `invoke()`
- Add `provider: str = "bedrock"` field to `ModelInfo` dataclass
- Add `qwen3.6:35b` and `gemma4:31b` entries to `MODELS` with `provider="ollama"`, prices 0.0
- Update `AgentLoop.__init__` type hint for `llm_client` to `LLMClient`
- Change `BedrockClient.invoke()` to accept `list[Turn]` (use `_turns_to_bedrock_messages()`)
- Update `MemoryExtractor` to pass `[Turn(id="", role="user", content=[TextBlock(text=...)])]`
- Re-export `LLMClient` from `src/archie/llm/__init__.py`

Deliverable: Protocol defined, Ollama models in registry, all existing tests pass unchanged.

Verify: `uv run pytest && uv run ruff check src tests`

### 2. OllamaClient with streaming text generation

Approach:
- New file `src/archie/llm/ollama.py` following `bedrock.py`'s structure (module docstring,
  `log = logging.getLogger(__name__)`, `log_event()` for timing)
- Use `ollama.Client(host=..., timeout=...)` — synchronous, matches the threading model
  (agent loop runs in a worker thread)
- `stream()` translates `list[Turn]` → Ollama messages: system prompt as first `role: "system"`
  message, `TextBlock` → `{"role": ..., "content": text}`, tool blocks handled in milestone 3
- Stream via `client.chat(model=..., messages=..., stream=True)` — each chunk has
  `.message.content` for text deltas
- Token counts arrive only on final chunk (where `.done == True`): `prompt_eval_count` →
  `input_tokens`, `eval_count` → `output_tokens`
- `invoke()` calls `client.chat(model=..., messages=..., stream=False)` — translates the raw
  dict messages from Bedrock format to Ollama format first
- Tool support NOT wired this milestone — `tool_config` parameter accepted but ignored
- Hardcode defaults (host `localhost:11434`, timeout 120s) — config wiring in milestone 4
- ⚠️ Connection errors: catch `httpx.ConnectError` and `ollama.ResponseError`, raise with
  clear message

Tasks:
- Add `ollama>=0.6.1` to pyproject.toml dependencies
- Create `src/archie/llm/ollama.py` with `OllamaClient` class
- Implement `_turns_to_ollama_messages(turns, system)` handling `TextBlock`, `ToolUseBlock`,
  `ToolResultBlock`
- Implement `stream()` yielding `TextDelta`, `Usage`, `Done` (no tool calls yet)
- Implement `invoke()` using the same message translation (non-streaming call)
- Wrap connection/timeout errors with descriptive messages
- Add `log_event()` call on request completion (model, duration, tokens)
- Re-export `OllamaClient` from `src/archie/llm/__init__.py`
- Add `tests/test_ollama.py` with mocked `ollama.Client`: test message translation, streaming
  event emission, error handling

Deliverable: `OllamaClient` streams text responses and handles errors cleanly.

Verify: `uv run pytest tests/test_ollama.py` passes. Manual test against running Ollama with a
simple prompt confirms `TextDelta` chunks arrive.

### 3. Tool-calling support in OllamaClient

Approach:
- Translate Bedrock `tool_config` to Ollama format: each `{"toolSpec": {"name", "description",
  "inputSchema": {"json": schema}}}` → `{"type": "function", "function": {"name",
  "description", "parameters": schema}}`
- Pass translated tools to `client.chat(..., tools=...)` in `stream()`
- Tool calls arrive on chunks via `.message.tool_calls` — list of objects with
  `.function.name` (str) and `.function.arguments` (dict, already parsed by ollama library)
- Generate `tool_use_id` with `str(ULID())` for each tool call
- Stop reason logic: if any tool_calls present in the final response, emit
  `Done(stop_reason="tool_use")`. Otherwise map `done_reason`: `"stop"` → `"end_turn"`,
  `"length"` → `"max_tokens"`
- Tool results in subsequent turns: `ToolResultBlock` → `{"role": "tool", "content": text,
  "tool_name": name}` (Ollama uses `tool_name` field on the message)
- ⚠️ Local models may return malformed arguments (not a dict, or missing expected fields).
  If `.function.arguments` is not a dict or is empty when the schema requires params, set
  `input_truncated=True` on the `ToolUseEvent`

Tasks:
- Add `_tool_config_to_ollama(tool_config)` translation function
- Wire `tools` parameter into `client.chat()` call in `stream()` when `tool_config` is provided
- Emit `ToolUseEvent` for each tool call with ULID-generated ID
- Implement stop_reason normalisation logic
- Handle malformed arguments: empty/non-dict → `input_truncated=True`
- Update `_turns_to_ollama_messages()` to handle `ToolUseBlock` (assistant message with
  tool_calls) and `ToolResultBlock` (tool role message)
- Add tests: tool schema translation, tool call event emission, stop_reason mapping,
  malformed args handling

Deliverable: Ollama models can invoke tools and receive results through the full agent loop.

Verify: `uv run pytest tests/test_ollama.py` — all tool-calling tests pass.

### 4. Configuration and provider routing

Approach:
- Add `OllamaConfig` frozen dataclass to `config.py`: `host: str = "http://localhost:11434"`,
  `timeout: int = 120`
- Parse optional `ollama:` section from `nextgen.yaml`
- In `app.py`, hold `self._clients: dict[str, LLMClient]` — keyed by provider name, lazily
  populated
- On startup: look up configured model's `ModelInfo.provider`, create the appropriate client
- `switch_model()` becomes provider-aware:
  - Same provider, different model → mutate `client.model_id`
  - Different provider → create/reuse client from `self._clients`, assign to both `self.llm`
    and rebuild `self.agent.llm`
- ⚠️ `self.agent.llm` is set in `AgentLoop.__init__` as `self.llm = llm_client`. On provider
  switch, `app.py` must assign `self.agent.llm = new_client` directly (same pattern as the
  existing `self.llm.model_id = model_id` mutation, just at the reference level)
- Command palette already iterates `MODELS.keys()` — Ollama models appear automatically

Tasks:
- Add `OllamaConfig` dataclass to `config.py`
- Parse `ollama:` section in `load_config()`, add `ollama` field to `Config`
- Refactor `app.py` startup: create initial client based on `model_info.provider`
- Add `self._clients` dict to `ArchieApp`, populate lazily on first use per provider
- Update `switch_model()` to handle provider transitions
- Pass `config.ollama.host` and `config.ollama.timeout` to `OllamaClient` constructor
- Add/update tests for config parsing with `ollama` section

Deliverable: Can start with an Ollama model as default and switch between Bedrock/Ollama
mid-session via Ctrl+P.

Verify: Start with `model: "qwen3.6:35b"` in config → chat produces responses. Switch to
Sonnet via Ctrl+P → Bedrock responds. Switch back → Ollama responds. All without restarting.

### 5. Error handling and observability

Approach:
- Primary failure mode: Ollama offline. Detect on first `stream()` call, surface clearly
- Wrap all Ollama calls in try/except catching `httpx.ConnectError`, `httpx.ReadTimeout`,
  and `ollama.ResponseError`
- Format errors as the agent loop expects: raise or return an error string that gets emitted
  as `TurnError` event (check how Bedrock errors propagate — likely caught in agent.py's
  `run_turn()`)
- Request logging via `log_event()` matching Bedrock's pattern: model, duration_s, stop_reason,
  input/output tokens
- ⚠️ The agent loop catches exceptions from `self.llm.stream()` in a try/except and emits
  `TurnError` — verify this path works for Ollama errors too

Tasks:
- Audit agent.py error handling path — confirm exceptions from `stream()` become `TurnError`
- Add specific exception handling in `OllamaClient.stream()` and `invoke()` for connection,
  timeout, and model-not-found errors
- Ensure error messages include the host URL (helps debugging wrong port/host)
- Add `log_event()` call with full metrics on successful completion
- Add test: simulate connection refused → clean error message
- Add test: simulate timeout → clean error message
- Verify status bar shows $0.00 for Ollama turns (should work from 0.0 pricing, just confirm)

Deliverable: Ollama failures produce actionable error messages; requests are logged at the
same level of detail as Bedrock.

Verify: Stop Ollama, attempt a turn → error says "Ollama is not reachable at
http://localhost:11434". Start Ollama, chat, check `~/.archie/nextgen.log` for `request_end`
events with Ollama model name and token counts.
