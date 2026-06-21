# Plan 026: Glob Tool

## Objective

Add a dedicated `glob` tool for recursive file discovery by glob pattern, sorted by
modification time (most recent first), with relative path output. Replaces the common
use case of `list_files` with a glob filter when the model needs to find recently-changed
files matching a pattern.

## Context

- `list_files` already supports a `glob` parameter via `rg --files -g`, but it sorts
  alphabetically and returns absolute paths — unhelpful when the model needs to find
  recent activity or understand project structure at a glance.
- The model frequently needs "which files matching X changed recently?" — this requires
  mtime sorting which `rg --files` does not support.
- `rg --files` is still the best portable way to enumerate files (respects .gitignore,
  fast, available everywhere). Sorting by mtime is done post-collection via `os.stat()`.
- Tool follows the standard closure pattern: `make_glob_spec(cwd, allowed_directories)`.

## Requirements

- MUST discover files recursively matching a glob pattern, sorted by mtime descending
  - AC: `**/*.py` in a project returns Python files with most recently modified first
  - AC: `*.md` scoped to a subdirectory only searches that subtree
  - AC: results are sorted by `st_mtime` descending (most recent first)

- MUST return paths relative to cwd
  - AC: output shows `src/archie/agent.py` not `/home/user/dev/archie-nextgen/src/archie/agent.py`
  - AC: if search path is outside cwd, paths are shown as absolute

- MUST enforce a configurable result limit (default 100)
  - AC: default invocation returns at most 100 files
  - AC: model can pass `limit` parameter to adjust (e.g. 10 or 50)
  - AC: when results exceed limit, a summary line indicates total count

- MUST validate paths via `validate_path()`
  - AC: paths outside cwd and allowed_directories are rejected with an error
  - AC: symlink escape attempts are blocked

- MUST handle errors gracefully
  - AC: non-existent directory returns a clear error, not a stack trace
  - AC: ripgrep not installed returns a clear error
  - AC: timeout after 15 seconds returns an error

- SHOULD respect .gitignore (via ripgrep's default behaviour)
  - AC: files in .gitignore are excluded from results

- SHOULD NOT paginate — model narrows the pattern if over limit
  - AC: no offset/page parameter in the schema

## Design

### Code structure

- `src/archie/tools/glob.py` — `make_glob_spec()` factory returning a `ToolSpec`
- `src/archie/tools/__init__.py` — register in `create_default_registry()`
- `tests/test_glob_tool.py` — unit tests

### Key decisions

- **mtime sort via os.stat()** — `rg --files` doesn't support mtime sorting. Collect all
  paths from rg, `os.stat()` each, sort by `st_mtime` descending. This is simple, portable,
  and fast enough for typical project sizes (stat'ing 10k files takes ~50ms).
- **Relative paths** — output uses paths relative to cwd. If the search path is outside cwd,
  absolute paths are used instead (unambiguous, avoids `../../` reasoning).
- **Default limit 100** — balances context usage with discovery utility. Model can reduce
  with `limit` param. No pagination — if there are too many results, narrow the pattern.
- **Summary line when truncated** — tells the model the total count so it can decide whether
  to narrow the search.

### Schema

```json
{
  "name": "glob",
  "description": "Find files by glob pattern. Results sorted by most recently modified.\n\n- Respects .gitignore.\n- Narrow the pattern if too many results \u2014 no pagination.\n- Use for discovering which files exist, not for reading content.",
  "schema": {
    "type": "object",
    "properties": {
      "pattern": {
        "type": "string",
        "description": "Glob pattern (e.g. '**/*.py', 'src/**/*.ts', '*.md')"
      },
      "path": {
        "type": "string",
        "description": "Directory to search from (default: working directory)"
      },
      "limit": {
        "type": "integer",
        "description": "Maximum files to return (default: 100)"
      }
    },
    "required": ["pattern"]
  }
}
```

### Output format

```
src/archie/agent.py
src/archie/tools/read_file.py
src/archie/tools/code.py
...
100 files shown of 234. Narrow the pattern for more.
```

When results fit within limit, no summary line is appended.

### Implementation approach

```python
# 1. Validate path
search_path = validate_path(path_str, cwd, allowed_directories)

# 2. Run rg --files with glob filter
cmd = ["rg", "--files", "-g", pattern, "-g", "!.git/", str(search_path)]
result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)

# 3. Stat each file, sort by mtime descending
files_with_mtime = []
for f in result.stdout.strip().split("\n"):
    try:
        st = os.stat(f)
        files_with_mtime.append((f, st.st_mtime))
    except OSError:
        continue
files_with_mtime.sort(key=lambda x: x[1], reverse=True)

# 4. Truncate to limit, make relative, format output
```

## Milestones

### 1. Implement glob tool

Approach:
- New file `src/archie/tools/glob.py` with `make_glob_spec(cwd, allowed_directories)`
- Handler: validate path, run `rg --files -g <pattern>`, stat each file, sort by mtime
  descending, truncate to limit, format as relative paths
- Use `validate_path()` for the search directory
- Compute relative paths: `os.path.relpath(file_path, cwd)` when search_path is under cwd,
  otherwise `os.path.relpath(file_path, search_path)`
- Handle edge cases: empty results ("No files found."), missing pattern (error), non-existent
  path (error from validate_path or post-validation check)

Tasks:
- Create `src/archie/tools/glob.py`
- Register in `create_default_registry()` in `src/archie/tools/__init__.py`
- Add import and `registry.register(make_glob_spec(cwd, allowed_directories))` line

Deliverable: `glob` tool returns mtime-sorted relative paths for a given pattern.

Verify: `uv run ruff check src/archie/tools/glob.py` passes.

### 2. Add tests

Approach:
- Create `tests/test_glob_tool.py` with `TestGlobTool` class
- Mock `subprocess.run` to return controlled file lists
- Mock `os.stat` to return controlled mtime values
- Test: basic pattern match, mtime sort order, limit enforcement, summary line when
  truncated, relative path output, error cases (no files, invalid path, rg not found,
  timeout)

Tasks:
- Create `tests/test_glob_tool.py`
- Test cases: happy path, limit truncation with summary, empty results, path validation
  rejection, ripgrep error, timeout

Deliverable: Comprehensive test coverage for the glob tool handler.

Verify: `uv run pytest tests/test_glob_tool.py && uv run ruff check src tests && uv run ruff format --check src tests`
