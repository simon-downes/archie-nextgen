# Plan 023: Docstring Sweep

## Objective

Add docstrings to all functions/methods across the codebase. This is a learning
project — docstrings should explain the *why* as well as the *what*, especially
for longer functions with non-obvious logic.

## Context

- 82 functions currently lack docstrings
- Module-level docstrings already exist on most files (good)
- Key files already have thorough documentation (agent.py, bedrock.py, sandbox.py)
- The project conventions (CONTRIBUTING.md) state: "Every module starts with a
  docstring explaining what it does and the key design decisions."
- Closure-pattern tool handlers (`def handler(params)`) are the most common gap

## Requirements

- MUST add a docstring to every function and method that lacks one
  - AC: `uv run ruff check --select D102,D103` reports no missing docstrings
  - AC: docstrings explain *why* not just *what* for non-trivial functions
  - AC: single-line docstrings for trivial/obvious functions (getters, watchers)
  - AC: multi-line docstrings for handlers, parsers, and functions >10 lines

- MUST NOT change any code behaviour
  - AC: all existing tests pass unchanged
  - AC: no functional modifications — docstrings only

- SHOULD follow Google-style docstring format (Args/Returns/Raises sections)
  for public functions with parameters
  - AC: tool handlers document what params are expected and what's returned
  - AC: `__init__` methods document the key instance attributes created

## Milestones

Split by area to keep each session manageable (~20 functions per batch):

### 1. Core modules (agent, session, config, models, memory, sandbox, artifact_store)

Files: agent.py, artifact_store.py, brain.py, memory.py, sandbox.py
Functions: ~8

Approach:
- These are the most important to document well — they're the architecture
- `__init__` methods should explain what the class manages and key attributes
- Focus on *why* decisions were made (e.g. why sandbox is lazy-started)

Tasks:
- Add docstrings to all undocumented functions in the listed files
- Pay special attention to `AgentLoop.__init__` (complex, many params)

Deliverable: Core module functions all have docstrings.
Verify: `uv run pytest --ignore=tests/test_recall_tool.py -q` passes.

### 2. LLM clients and tools/__init__

Files: llm/__init__.py, llm/bedrock.py, llm/ollama.py, tools/__init__.py, logs.py
Functions: ~8

Approach:
- Protocol methods need docstrings explaining the contract
- `__init__` on clients should document connection details
- `validate_path` is a critical security function — document the threat model
- Log filter/formatter are non-obvious — explain the structured logging design

Tasks:
- Add docstrings to protocol methods, client __init__, validate_path, log helpers

Deliverable: LLM and infrastructure functions documented.
Verify: Tests pass, lint clean.

### 3. Tool handlers

Files: tools/code.py, tools/edit_file.py, tools/recall.py, tools/retrieve_artifact.py,
       tools/self_debug.py, tools/web_fetch.py, tools/web_search.py, tools/write_file.py
Functions: ~12

Approach:
- Every `handler(params)` closure should document what params it expects,
  what it returns, and error conditions
- Helper functions (_handle_outline, _handle_search, _handle_overview) need
  docstrings explaining their specific operation
- Keep them concise — the tool description (in the schema) already explains
  user-facing behaviour; the docstring explains implementation

Tasks:
- Add docstrings to all handler closures and helper functions

Deliverable: All tool modules fully documented.
Verify: Tests pass, lint clean.

### 4. Code intelligence (code_intel.py)

Files: code_intel.py
Functions: ~15

Approach:
- The `_load_*` functions are all identical in purpose (one per language) —
  use the same single-line docstring pattern for all: "Load the {lang} parser."
- The `_python_function`, `_python_class`, etc. extract AST nodes — document
  what tree-sitter node types they handle
- `CodeIndex.__init__` should explain the caching strategy

Tasks:
- Add docstrings to all _load_* functions (single-line, same pattern)
- Add docstrings to all _extract_* functions explaining node types
- Document CodeIndex.__init__

Deliverable: code_intel.py fully documented.
Verify: Tests pass, lint clean.

### 5. UI layer

Files: ui/app.py, ui/status.py, ui/conversation.py, ui/throbber.py, ui/input.py,
       ui/commands.py
Functions: ~42

Approach:
- Textual widget methods (compose, on_mount, watch_*, action_*) need brief
  docstrings explaining what triggers them and what they do
- `_refresh_display` is the key method in StatusBar — document the format
- Event handlers in app.py should explain the event flow
- `_build_env_block` and `_get_client` are important — document decisions
- Watchers can be single-line: "Refresh display when X changes."

Tasks:
- Add docstrings to all UI functions in the listed files
- Focus on app.py (most complex) first

Deliverable: UI layer fully documented.
Verify: Tests pass, app launches correctly.
