# AGENTS.md

Archie — AI coding assistant: Textual TUI, tool-calling, Docker sandbox. Python 3.13+, managed with `uv`.

## Architecture

```
UI (ui/app.py)  →  Agent (agent.py)  →  Runtime (llm/ / sandbox / tools)
```

- UI builds AgentLoop, passes callback, runs turns on worker thread. Never calls LLMs or tools directly.
- Agent owns turn loop, history, event emission. Communicates with UI only via frozen `AgentEvent` dataclasses through `emit` callback.
- Runtime (LLM clients, sandbox, tools) handles I/O synchronously.
- Interruption is cooperative via `threading.Event`.

## Rules

- Tools use the closure pattern: handlers capture deps at registration via `make_<tool>_spec()`. Never pass config through dispatch.
- Adding a tool = new file in `src/archie/tools/` + one line in `create_default_registry()`.
- `types.py` is provider-agnostic. Bedrock wire format stays in `llm/bedrock.py`.
- Agent→UI communication only via `AgentEvent` types in `agent.py`.
- All file-access tools must use `validate_path()`.
- Config dataclasses are frozen — never mutate after load.

## Conventions

- Python 3.13+ syntax: `list[str]`, `str | None` (not `List`, `Optional`).
- Annotate public signatures; private helpers inferred.
- Tool factories: `make_<tool_name>_spec()` → `ToolSpec`
- Agent events: frozen dataclasses, noun phrases (`TextDeltaEvent`, `TurnComplete`)
- Module logger: `log = logging.getLogger(__name__)`
- Test classes: `Test<ThingUnderTest>`
- Every module starts with a docstring explaining purpose and key design decisions.

## Verify

```bash
uv run pytest && uv run ruff check src tests && uv run ruff format --check src tests
```

## Further Reference

- `CONTRIBUTING.md` — development setup, testing philosophy, PR process
- `README.md` — configuration, available models, usage
- `plans/` — feature plans and roadmap
