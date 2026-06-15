# Plan 021: Web Search Tool

## Objective

Add a `web_search` tool that searches the web via the `ddgs` library (DuckDuckGo
metasearch). The tool provides the model with access to current web information for
researching documentation, checking library versions, looking up error messages, and
verifying facts.

## Context

- The roadmap lists "Web fetch/search tools" — `web_fetch` exists (plan 015), this is
  the search half.
- Currently the model can only access the web via `curl` in the sandbox shell, which
  returns raw HTML (massive token waste) and requires the model to know exact URLs.
- The `ddgs` library (v7+, MIT, 2.7k stars) provides metasearch across multiple backends
  (Google, Bing, DuckDuckGo, Brave, etc.) with clean structured results.
- The tool runs on the host (Python library, no side effects) — not in the sandbox.
- This is a simple single-file tool following the existing closure pattern.

## Requirements

- MUST search the web and return structured results (title, URL, snippet)
  - AC: query "pydantic python 3.14" returns relevant results with titles and URLs
  - AC: returns up to 8 results per query
  - AC: results are formatted compactly (one result ≈ 3 lines)

- MUST handle errors gracefully
  - AC: network failures return a clear error message, not a stack trace
  - AC: empty results return "No results found"
  - AC: empty/missing query returns a helpful error

- MUST use opinionated defaults (not expose engine internals to the model)
  - AC: schema has only `query` parameter — no region, backend, page, safesearch params
  - AC: safesearch is off (hardcoded)
  - AC: backend is "auto" (ddgs picks the best engine)

- MUST include guidance in the tool description for effective usage
  - AC: description mentions including the year for recency
  - AC: description lists when to use it (documentation, versions, errors, APIs)

- SHOULD be registered in `create_default_registry()` like all other tools
  - AC: available immediately on app startup, no config needed

## Design

### Code structure

- `src/archie/tools/web_search.py` — `make_web_search_spec()` factory returning a `ToolSpec`
- `src/archie/tools/__init__.py` — register in `create_default_registry()`
- `pyproject.toml` — add `ddgs>=9,<10` dependency

### Key decisions

- **Host-side execution** — `ddgs` is a Python library. No sandbox, no Docker, no subprocess.
  Same pattern as `web_fetch`.
- **Lazy import** — `from ddgs import DDGS` inside the handler to avoid import-time cost
  (ddgs pulls in httpx, lxml, etc). Same pattern as ollama lazy import.
- **8 results** — DuckDuckGo quality is lower than Google. 5 is too tight for noisy queries,
  10 is token-wasteful. 8 is the sweet spot.
- **No parameters beyond query** — the model doesn't need to choose backends, regions, or
  pages. If results are bad, refine the query. Simpler schema = fewer tool-call formatting
  errors from weaker models.
- **`backend="auto"`** — ddgs rotates across Google, Bing, DuckDuckGo, Brave, etc.
  automatically. No need to expose this.
- **Description includes year hint** — "Include the current year in your query for recent
  results" helps the model (especially Qwen) get relevant results without us hardcoding
  `timelimit`.

### Schema

```json
{
  "name": "web_search",
  "description": "Search the web. Use for: finding documentation, checking library versions, looking up error messages, researching APIs and tools. Returns top 8 results with title, URL, and snippet. Include the current year in your query for recent results (e.g. 'pydantic python 3.14 2025').",
  "schema": {
    "type": "object",
    "properties": {
      "query": {
        "type": "string",
        "description": "Search query. Be specific — include library names, versions, error messages."
      }
    },
    "required": ["query"]
  }
}
```

### Result format

```
1. Pydantic v2.12 Release — pydantic.dev
   https://pydantic.dev/articles/pydantic-v2-12-release
   Pydantic V1 core functionality will not work properly with Python 3.14 or greater.

2. Add initial support for Python 3.14 · Issue #11613 — github.com
   https://github.com/pydantic/pydantic/issues/11613
   Reload to refresh your session...

...
```

## Milestones

### 1. Implement web_search tool

Approach:
- New file `src/archie/tools/web_search.py` with `make_web_search_spec()` factory
- Factory takes **no arguments** (no filesystem access, no cwd needed)
- Handler: validate query, lazy-import `DDGS`, call `.text()`, format results
- Exact call: `DDGS(timeout=10).text(query, safesearch="off", backend="auto", max_results=8)`
- Create a fresh `DDGS()` instance per handler invocation (no singleton)
- Catch broad `Exception` around the `.text()` call — return `tool_error(f"Search failed: {e}")`
- No `validate_path` needed — no filesystem access
- Add `ddgs>=9,<10` to pyproject.toml dependencies
- Top-level imports in the file: `from archie.tools import ToolSpec, tool_error, tool_result`
- Registration in `create_default_registry()`: `registry.register(make_web_search_spec())`

Tasks:
- Add `ddgs>=9,<10` to pyproject.toml
- Create `src/archie/tools/web_search.py`
- Register in `create_default_registry()` in `src/archie/tools/__init__.py`
- Run `uv sync` to install
- Add basic tests: empty query error, result formatting, error handling (mock DDGS)

Deliverable: `web_search` tool returns formatted results for a query.

Verify: `uv run pytest tests/test_web_search.py` passes. Manual test in a session —
ask the model to search for something and confirm results appear.
