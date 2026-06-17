# Contributing

## Key Rules

- Use the closure pattern for all tools — handlers capture their dependencies (cwd, sandbox, etc.) at registration time via `make_<tool>_spec()`; don't pass config through the dispatch system.
- New tools require two things only: a new file in `src/archie/tools/` and one line in `create_default_registry()` in `tools/__init__.py`.
- Keep provider-specific types out of `types.py` — `types.py` defines provider-agnostic `ContentBlock` types; Bedrock wire format stays in `llm/bedrock.py`.
- Agent events live in `agent.py` — the `AgentEvent` types are the sole communication channel from the agent loop to the UI.
- All file-access tools must use `validate_path()` — never resolve user-supplied paths without it.
- Test tool handlers by mocking at the boundary (`sandbox.exec`, subprocess calls) not at the tool spec level — verify formatting and error handling, not that mocks work.
- Run `uv run ruff check src tests` and `uv run ruff format src tests` before committing.

## Development Setup

### Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/)
- Docker (daemon running, user in `docker` group) — only needed for shell tool
- AWS credentials with Bedrock access (`eu-west-1` by default) — only needed for Bedrock models
- OR Ollama running locally — for local models without AWS

### Setup

1. Clone the repository
2. `uv sync` — installs the package + dev dependencies into a managed virtualenv
3. `uv run archie build` — builds the sandbox Docker image
4. Verify: `uv run archie --help` should show commands (`chat`, `build`, `init`, `brain`)

## Project Conventions

### Code Organisation

- `src/archie/` — main package (src layout — not importable without install/`uv run`)
- `src/archie/agent.py` — the AgentLoop: callback-based turn orchestrator with cooperative interruption
- `src/archie/tools/` — one file per tool; `__init__.py` holds the registry, `ToolSpec`, and shared utilities
- `src/archie/memory.py` — memory extraction from conversation history
- `src/archie/code_intel.py` — tree-sitter based code intelligence
- `src/archie/models.py` — model metadata and constants (pricing, context limits)
- `src/archie/llm/` — LLM provider clients; `bedrock.py` and `ollama.py`
- `src/archie/ui/` — Textual TUI components; `app.py` is the entry point
- `src/archie/types.py` — provider-agnostic `ContentBlock` types (TextBlock, ToolUseBlock, ToolResultBlock)
- `sandbox/` — `Dockerfile` for the per-session execution container
- `tests/` — mirrors `src/archie/` structure; one test file per module
- `plans/` — feature plan documents and roadmap; not production code

### Architecture

The app has three layers with strict dependency flow:

```
UI (app.py)  →  Agent (agent.py)  →  Runtime (llm/ / sandbox / tools)
```

- The **UI** constructs the AgentLoop, passes a callback, and runs `run_turn()` on a worker thread. It never calls Bedrock or runs tools directly.
- The **Agent** owns the turn loop, history mutations, and event emission. It communicates with the UI exclusively via frozen `AgentEvent` dataclasses pushed through the `emit` callback.
- The **Runtime** (LLM clients: Bedrock + Ollama, sandbox, tools) handles I/O. The agent calls these synchronously.

Interruption is cooperative: the UI calls `agent.interrupt()` (sets a `threading.Event`); the agent checks it between stream events and around tool calls, raises internally, repairs history, and emits `TurnInterrupted`.

### Code Style

Ruff is used for linting and formatting, configured in `pyproject.toml`:

```bash
uv run ruff check src tests   # lint
uv run ruff format src tests  # format
uv run ruff check --fix src tests  # auto-fix safe issues
```

Line length is 100. Rules: `E`, `W`, `F`, `I` (isort), `B` (bugbear), `C4`, `N` (naming), `UP` (pyupgrade). `E501` (line-too-long) is ignored — Ruff formats but doesn't error on long lines.

### Type Hints

- Use Python 3.13+ syntax: `list[str]`, `dict[str, int]` instead of `List`, `Dict`
- Annotate public function signatures; private helpers can be inferred
- Use `|` for union types: `str | None` instead of `Optional[str]`

### Naming Conventions

- Tool factory functions: `make_<tool_name>_spec()` — returns a `ToolSpec`
- Agent events: frozen dataclasses as noun phrases (`TextDeltaEvent`, `ToolStarted`, `TurnComplete`)
- Config dataclasses: frozen (`frozen=True`) — mutating config after load is never valid
- Content blocks: frozen dataclasses (`TextBlock`, `ToolUseBlock`, `ToolResultBlock`)
- Module-level logger: `log = logging.getLogger(__name__)` — don't use `logger` or the root logger
- Test classes: `Test<ThingUnderTest>` — e.g. `TestValidatePath`, `TestToolRegistry`

### Docstrings

Every module starts with a docstring explaining what it does and the key design decisions. Non-obvious choices are explained inline with comments, not just restated in prose. See `src/archie/tools/read_file.py` or `src/archie/agent.py` for the expected level of detail.

## Testing

### Running Tests

```bash
uv run pytest
```

### Test Organisation

- Tests live in `tests/`, mirroring `src/archie/` — `test_agent.py` tests `agent.py`, etc.
- Test classes group related cases: `class TestValidatePath`, `class TestShellTool`
- No coverage gate enforced, but new behaviour should have tests

### Writing Tests

- Use `tmp_path` (pytest built-in) for any test that touches the filesystem
- Use `monkeypatch` to redirect `ARCHIE_DIR` / `CONFIG_PATH` in config tests — don't touch `~/.archie`
- Mock at the I/O boundary: `MagicMock()` for `sandbox.exec`, `subprocess.run`, `BedrockClient.stream`, and `OllamaClient.stream` — not for internal logic
- LLM stream responses are mocked using `_mock_llm(*call_responses)` in `test_agent.py` — follow that pattern for agent loop tests
- `setup_method` is used in tool test classes to create a fresh spec + mock before each test

### Adding a New Tool

1. Create `src/archie/tools/<tool_name>.py` with a `make_<tool_name>_spec()` factory
2. Register it in `create_default_registry()` in `src/archie/tools/__init__.py`
3. Add `tests/test_<tool_name>_tool.py` — test spec metadata, output formatting, and error cases
4. If the tool accesses files, use `validate_path()` and test symlink escape and out-of-bounds paths

## Submitting Changes

1. Branch from `main`: use `<type>/<short-description>` (e.g. `feat/memory-read-tool`, `fix/sandbox-cancel`)
2. Make changes
3. Run checks: `uv run pytest && uv run ruff check src tests && uv run ruff format --check src tests`
4. Push and open a PR

### Commit Messages

Uses [Conventional Commits](https://www.conventionalcommits.org/). Types in use: `feat`, `fix`, `chore`, `docs`, `refactor`, `test`. Example: `feat(tools): add write_file tool with mtime cache invalidation`.

### Pull Requests

- Reference the relevant plan doc in `plans/` if applicable (e.g. "implements plans/005-file-write-tool.md")
- PR description should explain *why*, not just *what* — the diff covers the what
