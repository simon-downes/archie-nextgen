# Plan 027: grep Tool

## Objective

Replace the `search_files` tool with a new `grep` tool that produces cleaner output,
sorts results by file mtime (most recently modified first), and uses match groups
(contiguous blocks) as the limiting unit instead of individual match lines.

## Context

- The current `search_files` tool (`src/archie/tools/search_files.py`) uses `rg --json`
  with hardcoded 2 context lines, offset/limit pagination, and pipe/colon formatting.
- Pagination (offset) adds complexity for the model and is rarely useful — a simple limit
  on match groups is sufficient. If results are truncated, the model can narrow the query.
- Sorting by mtime surfaces the most relevant files first (recent edits are likely what
  the model cares about).
- Match groups (contiguous blocks of matches within a file) are a more meaningful unit
  than individual lines — counting lines penalises dense matches unfairly.
- Context lines default to 0 (unlike the current 2) — the model can request context when
  needed, reducing output noise for simple grep operations.
- Line truncation at 500 bytes prevents minified files or long lines from bloating results.

## Requirements

- MUST execute regex search via ripgrep and return results sorted by file mtime descending
  - AC: given files A (mtime 10:00) and B (mtime 11:00) both matching, B appears first
  - AC: uses `rg --json` for structured output parsing

- MUST use match groups as the limiting unit
  - AC: adjacent match lines (e.g. lines 10, 11, 12) count as 1 group
  - AC: non-adjacent matches in the same file (e.g. lines 10 and 50) count as 2 groups
  - AC: default limit is 50 groups; `limit` parameter overrides this
  - AC: when context > 0, overlapping context merges into the same group

- MUST support configurable context lines (default 0)
  - AC: `context: 0` returns only matching lines
  - AC: `context: 2` returns 2 lines before and after each match
  - AC: context lines use `:` separator; match lines use `|` separator

- MUST truncate individual output lines at 500 bytes
  - AC: lines longer than 500 bytes are cut and suffixed with ` [...]`

- MUST be case-insensitive by default
  - AC: pattern "foo" matches "Foo", "FOO", "foo"

- MUST validate the search path against allowed directories
  - AC: path outside cwd and allowed_directories returns an error
  - AC: uses `validate_path()` from the tools module

- MUST support optional glob filtering
  - AC: `glob: "*.py"` restricts search to Python files only

- SHOULD format output with file headers and indented line numbers
  - AC: no-context format matches the spec (file header, `  NNN| line`)
  - AC: with-context format uses `  NNN: line` for context lines
  - AC: blank line separates file groups

- MUST replace `search_files` in the tool registry
  - AC: `create_default_registry()` registers `grep`, not `search_files`
  - AC: old `search_files.py` is deleted

## Design

### Schema

```json
{
  "name": "grep",
  "description": "Search file contents using regex. Case-insensitive. Results sorted by file modification time.\n\n- Respects .gitignore.\n- Do NOT wrap the pattern in quotes. Do NOT double-escape.\n- Use `context` param only when you need surrounding lines \u2014 default is match-only.\n- Prefer speculative parallel searches over sequential rounds of glob+grep.",
  "schema": {
    "type": "object",
    "properties": {
      "pattern": {
        "type": "string",
        "description": "Regex pattern to search for"
      },
      "path": {
        "type": "string",
        "description": "Directory to search in (default: working directory)"
      },
      "glob": {
        "type": "string",
        "description": "File glob filter (e.g. '*.py')"
      },
      "context": {
        "type": "integer",
        "description": "Lines of context around matches (default: 0)"
      },
      "limit": {
        "type": "integer",
        "description": "Max match groups to return (default: 50)"
      }
    },
    "required": ["pattern"]
  }
}
```

### Output format

No context (`context: 0`):
```
src/archie/agent.py:
  250| def run_turn(self, user_message) -> None:
  465| def _execute_tools(self, tool_blocks, turn_log):

src/archie/tools/__init__.py:
  45| def validate_path(path_str, cwd, allowed):
```

With context (`context: 1`):
```
src/archie/agent.py:
  249: 
  250| def run_turn(self, user_message) -> None:
  251:     """Run one turn of the agent loop."""
  ...
  464:     return results
  465| def _execute_tools(self, tool_blocks, turn_log):
  466:     """Execute tool calls and return results."""
```

### Key decisions

- **Mtime sort** — Run `rg --json` first, collect all results, then stat each matched file
  and sort file groups by mtime descending. This is two passes but keeps the rg invocation
  simple and avoids needing `--sort-files` (which sorts by path, not mtime).
- **Match group counting** — After parsing rg output, split each file's matches into groups
  by checking line number gaps. With context=N, lines within N of each other merge into one
  group. Groups are counted globally across files; stop emitting when limit is reached.
- **No pagination** — Simpler interface. If 50 groups isn't enough, the model should narrow
  the search. Removing offset eliminates a class of off-by-one bugs.
- **500 byte line cap** — Prevents minified JS/CSS or generated files from consuming the
  entire output budget. Applied per-line before formatting.
- **Factory signature** — `make_grep_spec(cwd, allowed_directories)` matching existing
  convention. Runs rg on host via subprocess, same as `search_files` does today.
- **Relative paths in output** — All paths displayed relative to cwd for readability.

### Implementation approach

1. Parse rg JSON output into per-file match data (line number, text, is_match)
2. For each file with matches, call `os.path.getmtime()` (or `Path.stat()`)
3. Sort files by mtime descending
4. Within each file, segment matches into groups (contiguous = gap ≤ context lines)
5. Emit groups until the global limit is reached
6. Truncate each line at 500 bytes before formatting

## Milestones

### 1. Create `grep` tool implementation

Approach:
- New file `src/archie/tools/grep.py` with `make_grep_spec(cwd, allowed_directories)`
- Handler validates params, resolves path via `validate_path()`, builds rg command
- rg flags: `--json`, `-i`, `-C <context>`, `--max-count 200`, optional `-g <glob>`
- Parse rg JSON stdout into per-file structures
- Stat each matched file for mtime, sort file groups descending
- Segment matches into groups (gap > 1 + 2*context = new group)
- Format output with `|` for matches, `:` for context, 500-byte line cap
- Count groups globally, stop at limit

Tasks:
- Create `src/archie/tools/grep.py`
- Implement `make_grep_spec()` factory with closure-captured deps
- Implement `_parse_rg_json()` helper (rg JSON → per-file match list)
- Implement `_sort_by_mtime()` helper (stat + sort)
- Implement `_format_groups()` helper (segmentation + formatting + limit + truncation)

Deliverable: `grep.py` with complete handler logic.

Verify: `uv run ruff check src/archie/tools/grep.py`

### 2. Register and remove old tool

Approach:
- Replace `make_search_files_spec` import/registration with `make_grep_spec` in
  `create_default_registry()`
- Delete `src/archie/tools/search_files.py`
- Update any imports referencing `search_files` in tests

Tasks:
- Update `src/archie/tools/__init__.py`: import `make_grep_spec`, register it, remove
  `search_files` import and registration
- Delete `src/archie/tools/search_files.py`
- Remove search_files test methods from `tests/test_tools.py`

Deliverable: `search_files` fully replaced by `grep` in the registry.

Verify: `uv run ruff check src/archie/tools/__init__.py` — no import errors

### 3. Write tests

Approach:
- `tests/test_grep_tool.py` mirroring the tool test pattern (setup_method creates spec
  with tmp_path as cwd)
- Mock `subprocess.run` to return controlled rg JSON output
- Test: basic match formatting, context formatting, mtime sort order, group counting
  and limit, line truncation at 500 bytes, glob filter passed to rg, error cases
  (empty pattern, path outside allowed, rg not found, timeout)

Tasks:
- Create `tests/test_grep_tool.py` with `TestGrepTool` class
- Test empty pattern error
- Test path validation rejection
- Test basic output formatting (no context)
- Test output with context lines
- Test match group segmentation (adjacent = 1 group, gap = 2 groups)
- Test limit enforcement (stops at N groups)
- Test mtime sort order (mock stat)
- Test 500-byte line truncation
- Test rg not installed / timeout errors

Deliverable: Comprehensive test file covering handler logic.

Verify: `uv run pytest tests/test_grep_tool.py && uv run ruff check src tests && uv run ruff format --check src tests`
