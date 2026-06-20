# Plan 025: Unified Read Tool

## Objective

Merge `read_file` and `list_files` into a single `read` tool that auto-detects whether the
path is a file or directory and returns appropriate output. Reduces tool count, simplifies
the model's decision-making, and provides a more consistent interface.

## Context

- Currently two separate tools: `read_file` (Python file read with line numbers, mtime cache,
  binary detection) and `list_files` (ripgrep `--files` based directory listing with glob filter).
- The model frequently needs to decide between them — a unified tool removes that friction.
- `read_file` already handles offset/limit pagination, binary detection, char budget, and
  mtime dedup. These behaviours are preserved.
- `list_files` uses `rg --files` which gives flat recursive listings — the new tool keeps
  `rg --files` but formats output as a tree-style listing (depth 3, capped at 200 entries).
- The glob parameter from `list_files` is dropped — the `glob` tool covers pattern matching.
- The mtime cache type key uses `(path, offset, limit)` — directory reads don't populate it.
- `write_file` and `edit_file` invalidate mtime cache entries by resolved path string — this
  contract is unchanged.

## Requirements

- MUST detect path type and return file content or directory listing accordingly
  - AC: existing file path returns numbered content identical to current `read_file` output
  - AC: existing directory path returns sorted listing (dirs first, then files)
  - AC: non-existent path returns a clear error message

- MUST support offset/limit for file reads (1-indexed offset)
  - AC: `offset=10, limit=5` returns lines 10-14 of a file
  - AC: offset/limit params are ignored when path is a directory

- MUST truncate individual lines at 500 bytes
  - AC: a file with a 2000-char minified line shows first 500 chars + truncation marker

- MUST format directory listings as a tree-style output respecting .gitignore
  - AC: uses `rg --files` for gitignore-aware file discovery
  - AC: output shows entries up to depth 3 relative to the listed directory
  - AC: directories are suffixed with `/` and listed before files at each level
  - AC: entries indented by level (2 spaces per depth)
  - AC: total entries capped at 200; if exceeded, shows "N entries shown of M. Narrow the path for more."

- MUST use `validate_path()` for all path access
  - AC: paths outside cwd and allowed_directories are rejected
  - AC: symlinks escaping allowed directories are rejected

- MUST preserve mtime dedup cache for file reads
  - AC: reading the same unchanged file twice returns "file unchanged" stub
  - AC: cache key type remains `(str, int, int)` — compatible with write/edit invalidation

- SHOULD use the tool name `read` with factory `make_read_spec`
  - AC: registered as `read` in `create_default_registry()`
  - AC: `read_file` and `list_files` registrations removed

- SHOULD shorten paths in directory output relative to cwd
  - AC: if cwd is `/home/user/project` and listing `/home/user/project/src`, header shows `src/`

- SHOULD respect .gitignore for directory listings
  - AC: gitignored files/directories are excluded from listing
  - AC: `.git/` directory is always excluded

## Design

### Schema

```json
{
  "name": "read",
  "description": "Read a file or list a directory. Files return content with line numbers (1-indexed).\n\n- Use `code` first to locate relevant line ranges, then read with offset/limit.\n- **Always include offset and limit** for files >200 lines.\n- Do not reread the same file range twice.\n- Use truncation hints to continue reading with the correct offset.\n- Prefer `grep` to locate content instead of scanning full files.",
  "schema": {
    "type": "object",
    "properties": {
      "path": {
        "type": "string",
        "description": "File or directory path (relative to working directory or absolute)"
      },
      "offset": {
        "type": "integer",
        "description": "Start line, 1-indexed (files only, default: 1)"
      },
      "limit": {
        "type": "integer",
        "description": "Maximum lines to return (files only, default: entire file)"
      }
    },
    "required": ["path"]
  }
}
```

### File output format

```
42: line content here
43: another line
44: ...

Truncated 150 lines. Use offset=85 to continue.
```

Line numbers are right-aligned, colon-separated. Pagination hint appended when truncated.

### Directory output format

```
agent.py
config.py
llm/
  bedrock.py
  ollama.py
tools/
  __init__.py
  code.py
  read.py
  grep.py
ui/
  app.py
  conversation.py
```

Tree-style output up to depth 3. Directories listed before files at each level, both
sorted alphabetically. Hidden entries (dotfiles) excluded. Respects .gitignore via
`rg --files`. Capped at 200 entries total.

### Key decisions

- **1-indexed offset** — matches line numbers shown in output. Internally converted to 0-based
  for slicing. Existing `read_file` uses 0-based; the new tool switches to 1-based for
  consistency with the displayed numbers.
- **`rg --files` for directory listing** — respects `.gitignore`, nested ignore files, and
  binary detection without reimplementing any of that in Python. Get flat file list, filter
  to depth 3, build tree-style output. Subprocess overhead is negligible for this use case.
- **Depth 3 cap** — shows enough structure to navigate without drilling into subdirectories
  one at a time. For most projects this shows everything; for large ones the 200-entry cap
  kicks in and the model narrows the path.
- **Drop glob parameter** — the `glob` tool handles pattern-based file discovery.
  Keeping the schema minimal reduces tool-call errors from weaker models.
- **500-byte line cap** — prevents minified JS/CSS from consuming the entire char budget.
  Same as existing `read_file` behaviour.
- **Char budget preserved** — the 32KB internal budget from `read_file` carries forward for
  file reads. Directory listings are naturally short (single level).
- **`self_truncating=True`** — same as current `read_file`; the tool manages its own output
  size via char budget and pagination.

### Factory signature

```python
def make_read_spec(
    cwd: Path,
    allowed_directories: list[Path],
    mtime_cache: dict | None = None,
) -> ToolSpec:
```

Same closure pattern — captures cwd, allowed_directories, and mtime_cache. No sandbox
dependency (native Python I/O).

## Milestones

### 1. Implement unified read tool

Approach:
- Create `src/archie/tools/read.py` with `make_read_spec()`. The handler checks
  `resolved.is_dir()` vs `resolved.is_file()` after path validation and dispatches to
  the appropriate formatting logic.
- File-read logic is carried over from `read_file.py` with the offset change (1-indexed).
- Directory logic uses `rg --files` (subprocess) to get gitignore-respecting file list,
  filters to max depth 3 relative to the target path, builds a tree-style output with
  indentation. Dirs before files at each level, alphabetical within groups.
- Tree building: parse the flat `rg --files` output into path components, group by directory
  level, format with 2-space indentation per depth.
- Path shortening: directory listing header and entries use paths relative to cwd.

Tasks:
- Create `src/archie/tools/read.py` with `make_read_spec()` factory
- File path: validate → mtime check → binary detect → read → paginate → format with line numbers
- Directory path: validate → `rg --files` → parse paths → filter depth 3 → build tree → cap at 200 entries
- Handle edge cases: empty directory, path doesn't exist, path is neither file nor dir

Deliverable: `src/archie/tools/read.py` with complete handler logic.

Verify: `uv run python -c "from archie.tools.read import make_read_spec; print('ok')"`

### 2. Write tests

Approach:
- Create `tests/test_read_tool.py` mirroring the test pattern from other tool tests.
- Test file reads: basic content, offset/limit, line truncation, binary detection, mtime dedup.
- Test directory reads: tree-style output, depth 3 cap, dotfile exclusion, trailing slash on
  dirs, 200-entry cap with truncation message, correct indentation.
- Test errors: non-existent path, outside allowed dirs, symlink escape.

Tasks:
- Create `tests/test_read_tool.py` with `TestReadFile` and `TestReadDirectory` classes
- Verify mtime cache interaction with write/edit invalidation still works

Deliverable: Comprehensive test coverage for both file and directory modes.

Verify: `uv run pytest tests/test_read_tool.py -v`

### 3. Register and remove old tools

Approach:
- Update `create_default_registry()` in `src/archie/tools/__init__.py`: remove `read_file`
  and `list_files` imports/registrations, add `read` import/registration.
- Pass existing `mtime_cache` to `make_read_spec()`.
- Delete `read_file.py` and `list_files.py`.
- Update any imports in `test_tools.py` that reference the old specs.

Tasks:
- Import `make_read_spec` in `tools/__init__.py`
- Replace `make_read_file_spec` and `make_list_files_spec` registrations with `make_read_spec`
- Pass `cwd`, `allowed_directories`, `mtime_cache` to factory
- Delete `src/archie/tools/read_file.py` and `src/archie/tools/list_files.py`
- Update any remaining imports/references across the codebase

Deliverable: Clean codebase — `read` registered, old tools removed, all tests pass.

Verify: `uv run pytest && uv run ruff check src tests`


