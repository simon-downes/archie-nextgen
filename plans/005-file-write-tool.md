# Plan 005: File Write Tool

## Objective

Add file creation and editing capabilities so the model can write code, config, docs, etc. without resorting to shell commands (echo, sed, tee).

## Context

- Tool framework, file read/search/list, shell all working
- File writes are local to the project directory (no Docker needed — project is mounted rw anyway)
- Same path validation as read_file (allowlist enforcement)
- This is a learning project — clear, simple code over clever abstractions

## Design Decisions

### Two operations, one tool vs two tools

**Decision: Two tools** — `write_file` (create/overwrite) and `edit_file` (search-and-replace).

Rationale: They have fundamentally different schemas and failure modes. A single tool with a mode param adds schema complexity and makes the description harder for the model to parse. Two focused tools are easier to understand and produce better tool-calling behaviour.

### Edit approach: literal search-and-replace

**Decision: Exact string matching, not regex.**

Rationale:
- The model already knows the exact text (it just read the file with read_file)
- Regex introduces escaping bugs, greedy matching issues, and unexpected capture group behaviour
- For complex bulk edits, the model can use the shell tool (sed, rg --replace)
- Literal matching is deterministic and debuggable — you can see exactly what it looked for

### Ambiguity handling

**Decision: Require unique match by default, opt-in replace_all.**

When the `old` text appears multiple times in a file:
- Default: **fail with an error** telling the model how many matches were found. This forces the model to include more surrounding context to disambiguate.
- Opt-in: `replace_all: true` replaces all occurrences (for intentional bulk renames).

This is self-correcting: the error message teaches the model to be more specific on retry.

### No regex

**Decision: Literal only.**

- Model doesn't need pattern matching to locate text it already read
- Regex failures are silent/corrupt (wrong match, bad escaping) vs literal failures are obvious ("not found")
- Shell tool with sed/rg covers the rare case where regex is genuinely needed

## Requirements

### write_file tool

- MUST create a new file with the given content
  - AC: `write_file(path="new.py", content="...")` creates the file
- MUST overwrite an existing file when called with a path that exists
  - AC: Full content replacement
- MUST create parent directories if they don't exist
  - AC: `write_file(path="src/new_pkg/__init__.py", ...)` creates `src/new_pkg/`
- MUST validate path against allowlist (same as read_file)
  - AC: Paths outside project dir are rejected
- MUST return confirmation with line count
  - AC: "Written: src/new.py (42 lines)"
- SHOULD refuse to overwrite binary files
  - AC: Error if existing file contains null bytes in first 8KB

### edit_file tool

- MUST accept a list of edits, each with `old` (text to find) and `new` (replacement text)
  - AC: Multiple edits applied in sequence to the same file
- MUST require `old` to match exactly one location in the file (by default)
  - AC: Error "Found N matches for the provided text, include more context to disambiguate" when N > 1
- MUST support `replace_all: true` flag per edit to replace all occurrences
  - AC: `edit_file(path, edits=[{old: "foo", new: "bar", replace_all: true}])` replaces all
- MUST apply edits sequentially (each edit operates on the result of the previous)
  - AC: Edit 1 output is input to edit 2
- MUST validate path against allowlist
- MUST verify file exists before editing
  - AC: Error if file doesn't exist (use write_file to create new files)
- MUST return confirmation with edit count
  - AC: "Edited: src/app.py (3 edits applied)" or "Edited: src/app.py (replaced 5 occurrences in 2 edits)"
- MUST fail atomically — if any edit in the list fails, no changes are written
  - AC: File unchanged on disk if edit 3 of 5 fails to match

### Shared

- MUST use the same `validate_path()` helper as read_file
- MUST invalidate the read_file mtime cache for the written path (so subsequent reads return fresh content, not "file unchanged")
- SHOULD log writes at INFO level

## Technical Design

### write_file (`src/archie/tools/write_file.py`)

```python
def make_write_file_spec(cwd: Path, allowed_directories: list[Path], mtime_cache: dict) -> ToolSpec:
    def handler(params: dict) -> str:
        path_str = params["path"]
        content = params["content"]

        resolved = validate_path(path_str, cwd, allowed_directories)

        # Refuse to overwrite binary files
        if resolved.is_file():
            with resolved.open("rb") as f:
                if b"\x00" in f.read(8192):
                    return tool_error("Refusing to overwrite binary file")

        # Create parent dirs
        resolved.parent.mkdir(parents=True, exist_ok=True)

        # Write
        resolved.write_text(content, encoding="utf-8")

        # Invalidate read cache
        cache_keys = [k for k in mtime_cache if k[0] == str(resolved)]
        for k in cache_keys:
            del mtime_cache[k]

        line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return tool_result(f"Written: {path_str} ({line_count} lines)")

    return ToolSpec(name="write_file", ...)
```

### edit_file (`src/archie/tools/edit_file.py`)

```python
def make_edit_file_spec(cwd: Path, allowed_directories: list[Path], mtime_cache: dict) -> ToolSpec:
    def handler(params: dict) -> str:
        path_str = params["path"]
        edits = params["edits"]

        resolved = validate_path(path_str, cwd, allowed_directories)

        if not resolved.is_file():
            return tool_error(f"File not found: {path_str} (use write_file to create new files)")

        content = resolved.read_text(encoding="utf-8", errors="replace")
        original = content  # Keep for atomicity check

        applied = 0
        for i, edit in enumerate(edits):
            old = edit["old"]
            new = edit["new"]
            replace_all = edit.get("replace_all", False)

            if not old:
                return tool_error(f"Edit {i+1}: 'old' text cannot be empty.")

            count = content.count(old)
            if count == 0:
                return tool_error(
                    f"Edit {i+1}: text not found in file. Ensure the 'old' text matches exactly "
                    f"(including whitespace and indentation)."
                )
            if count > 1 and not replace_all:
                return tool_error(
                    f"Edit {i+1}: found {count} matches. Include more surrounding context to "
                    f"disambiguate, or set replace_all=true to replace all occurrences."
                )

            content = content.replace(old, new) if replace_all else content.replace(old, new, 1)
            applied += 1

        # Atomic write — only if all edits succeeded
        resolved.write_text(content, encoding="utf-8")

        # Invalidate read cache
        cache_keys = [k for k in mtime_cache if k[0] == str(resolved)]
        for k in cache_keys:
            del mtime_cache[k]

        return tool_result(f"Edited: {path_str} ({applied} edit(s) applied)")

    return ToolSpec(name="edit_file", ...)
```

### Mtime cache sharing

The read_file tool's mtime cache needs to be accessible to write tools so they can invalidate it. Options:
- Pass the cache dict as a parameter to all file tool factories (simple, explicit)
- Module-level dict (simpler but implicit coupling)

**Decision: Pass as parameter.** Explicit is better — `create_default_registry()` creates the cache dict and passes it to read_file, write_file, and edit_file.

### Changes to existing code

- `tools/__init__.py`: import and register both new tools in `create_default_registry()`
- `tools/read_file.py`: accept `mtime_cache` as a parameter instead of creating internally (small refactor)

## Edge Cases

- **Empty `old` string**: Reject with error. `"".count("")` has unintuitive behaviour in Python (matches everywhere). Validate that `old` is non-empty before processing.
- **Empty content in write_file**: Valid operation (creates empty file). Report "0 lines".
- **Encoding**: Both tools use UTF-8. `edit_file` reads with `errors="replace"` meaning non-UTF-8 bytes become `�`. If the model includes those replacement chars in `old`, it won't match the on-disk bytes. This is acceptable — the model would need to use shell (sed) for non-UTF-8 files anyway.
- **Concurrent modification**: Not handled. Single-agent, single-session — race conditions aren't a practical concern.
- **replace_all reporting**: When `replace_all=true`, report how many replacements were made: "replaced N occurrences".

## Milestones

### Milestone 1: Refactor mtime cache + write_file tool

Tasks:
- Refactor `make_read_file_spec` to accept an external `mtime_cache` dict parameter
  - Type: `dict[tuple[str, int, int], float]`
  - Update all test call sites that create read_file specs directly (pass `{}`)
- Update `create_default_registry()` to create the shared cache and pass to file tools
- Create `src/archie/tools/write_file.py`
- Register in `create_default_registry()`
- Tests: create new file, overwrite existing, empty content, parent dir creation, path validation, binary refusal, mtime cache invalidation
- Verify: ALL existing tests pass (read_file, engine, etc.)

### Milestone 2: edit_file tool

Tasks:
- Create `src/archie/tools/edit_file.py`
- Register in `create_default_registry()`
- Tests: single edit, multiple edits, unique match enforcement, replace_all (with count reported), empty old string rejection, atomicity (partial failure doesn't write), file not found, path validation, mtime cache invalidation
- Verify: all tests pass, lint clean

### Milestone 3: Review + comments

Tasks:
- Run review workflow (qa-runner + code-reviewer)
- Add detailed explanatory comments (learning project standard)
- Fix any findings
- Commit
