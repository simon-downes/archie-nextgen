# Plan 020: /workspace Mount and Environment Improvements

## Objective

Mount the project directory at `/workspace` inside the sandbox container (instead of the
host's absolute path), add path translation for file tools, inject an environment block
into the system prompt, and improve tool feedback for edit confirmations and code outline
errors.

## Context

- The current approach mounts the project at its host path (e.g.
  `/Users/simon.downes/dev/archie-nextgen`) inside the container. Docker creates ancestor
  directories as root, causing git "dubious ownership" errors.
- The model frequently gets confused about paths — using host paths, wrong prefixes, or
  `cd` to non-existent directories. An environment block in the system prompt and
  consistent `/workspace` path eliminates this.
- `edit_file` returns minimal feedback ("3 edit(s) applied") causing models to re-read
  files to verify changes.
- `code outline` returns a misleading "no symbols" message when a file doesn't exist
  instead of suggesting the correct path.

## Requirements

### /workspace mount

- MUST mount project directory at `/workspace` in the container (not the host path)
  - AC: `docker run -v {project_dir}:/workspace:rw -w /workspace`
  - AC: git operations work without ownership errors
  - AC: pre-create `/workspace` and set WORKDIR in Dockerfile

- MUST translate `/workspace/...` paths in file tool inputs to host paths
  - AC: `read_file(path="/workspace/src/foo.py")` reads `{project_dir}/src/foo.py`
  - AC: translation happens in `validate_path()` before security check
  - AC: relative paths (e.g. `src/foo.py`) still work unchanged
  - AC: non-project absolute paths (e.g. `~/.archie/brain/...`) pass through unchanged

- MUST NOT translate paths for non-project mounts
  - AC: `~/.archie/brain`, `~/.gitconfig`, `~/.ssh`, `~/.aws` mount at same path as host
  - AC: accessing `~/.archie/brain/foo.md` works without translation

- MUST remove the old `chown` workaround if it exists
  - AC: no post-start `docker exec` for ownership fixing

### System prompt environment block

- MUST inject a dynamic environment section into the system prompt
  - AC: includes project name, working directory (`/workspace`), OS (Linux/sandbox)
  - AC: includes Python interpreter path (`uv run python`)
  - AC: includes current git branch
  - AC: states that file tools accept relative paths from project root
  - AC: states that shell commands execute in `/workspace`

- SHOULD survive context compaction (placed early in system prompt)
  - AC: environment block is part of the system prompt, not a user message

### Edit confirmation improvement

- MUST include line numbers in edit_file success response
  - AC: response shows which lines were modified, e.g.
    `Edited: src/foo.py (3 edits at lines 45, 163, 310)`
  - AC: for multi-line replacements, show the range: `lines 45-52`

### Code outline path suggestions

- MUST return a helpful error when the file path doesn't exist
  - AC: searches for files with matching basename in the project
  - AC: suggests up to 3 candidate paths, e.g.
    `File not found: archie/app.py. Did you mean: src/archie/ui/app.py?`
  - AC: falls back to generic "file not found" if no candidates

## Design

### Path translation

Module constant in `tools/__init__.py` (not threaded through factories):

```python
CONTAINER_PROJECT_ROOT = "/workspace"

def validate_path(path: str, cwd: Path, allowed: list[Path]):
    # Translate container paths to host paths
    if path.startswith(CONTAINER_PROJECT_ROOT + "/"):
        path = str(cwd / path[len(CONTAINER_PROJECT_ROOT) + 1:])
    elif path == CONTAINER_PROJECT_ROOT:
        path = str(cwd)

    # ... existing resolution and security checks unchanged
```

No changes to tool factory signatures — `validate_path()` handles it internally using
the constant. All tools that call `validate_path()` get translation for free: read_file,
write_file, edit_file, search_files, list_files, and code.

### Sandbox changes

```python
# sandbox.py - _build_mounts()
mounts.append(f"{self.project_dir}:/workspace:rw")  # was host:host:rw

# sandbox.py - ensure_running()
"-w", "/workspace",  # was str(self.project_dir)
```

Dockerfile:
```dockerfile
# Install Python globally via uv
RUN UV_PYTHON_INSTALL_DIR=/usr/local uv python install && \
    PYTHON_PATH=$(find /usr/local -name "cpython-*" -type d | head -1) && \
    ln -s "$PYTHON_PATH/bin/python3" /usr/local/bin/python3 && \
    ln -s /usr/local/bin/python3 /usr/local/bin/python

RUN mkdir /workspace
WORKDIR /workspace
USER ${USERNAME}
```

### Environment block

Built dynamically in app.py before passing to AgentLoop:

```python
env_block = f"""
## Environment

- Project: {self.project_dir.name}
- Working directory: /workspace (Docker sandbox, Debian Linux)
- Python: python3 (available globally)
- Git branch: {self._git_branch}
- File tools: use relative paths from project root (e.g. src/archie/ui/app.py)
- Shell: executes in /workspace — do not cd elsewhere
- Prefer provided tools (read_file, edit_file, search_files, code) over shell equivalents
  (cat, sed, grep, find). Use shell only for running commands, tests, and git.
"""

system_prompt = SYSTEM_PROMPT + env_block
```

### Edit confirmation

Track line numbers during edit application:

```python
# After each successful replacement, record the line number
line_num = content[:match_start].count("\n") + 1
edit_lines.append(line_num)
```

Return: `Edited: src/foo.py (3 edits at lines 45, 163, 310)`

### Code outline suggestions

When `resolved.is_file()` is False in `_handle_outline`:

```python
# Search for files with matching basename
basename = Path(path_str).name
candidates = [f for f in index._discover_files() if f.name == basename]
if candidates:
    suggestions = [str(c.relative_to(cwd)) for c in candidates[:3]]
    return tool_error(
        f"File not found: {path_str}. Did you mean: {', '.join(suggestions)}?"
    )
return tool_error(f"File not found: {path_str}")
```

## Milestones

> ⚠️ Milestones 1–3 are atomically coupled — deploy together. Shipping any one without
> the others causes breakage (model sees /workspace paths but file tools reject them,
> or vice versa).

### 1. /workspace mount and Dockerfile

Approach:
- Change `_build_mounts()` in sandbox.py: project mount becomes `{project_dir}:/workspace:rw`
- Change `-w` in `ensure_running()` to `/workspace`
- Add `RUN mkdir /workspace` and `WORKDIR /workspace` to Dockerfile (before USER line)
- Remove any chown/ownership workaround if present
- ⚠️ Container must be rebuilt after Dockerfile change (`uv run archie build`)

Tasks:
- Update `_build_mounts()` to mount project at `/workspace`
- Update `-w` argument in `ensure_running()` docker run command to `/workspace`
- Update `-w` argument in `exec()` docker exec command to `/workspace`
- Add `mkdir /workspace` and `WORKDIR` to Dockerfile
- Add global Python install to Dockerfile via `uv python install` to `/usr/local`
- Update tests that assert mount paths

Deliverable: Sandbox starts with project at `/workspace`, git works without ownership errors.

Verify: `uv run archie build && uv run archie chat` — run `pwd` and `git status` in shell.

### 2. Path translation in validate_path

Approach:
- Add `CONTAINER_PROJECT_ROOT = "/workspace"` constant in `tools/__init__.py`
- Add prefix-stripping logic at the top of `validate_path()` — before any resolution
- No changes to tool factory signatures or call sites — translation is automatic
- All tools that call `validate_path()` benefit: read_file, write_file, edit_file,
  search_files, list_files, code
- ⚠️ Brain tool paths (e.g. `~/.archie/brain/...`) don't start with `/workspace` so
  they pass through unchanged — no special handling needed
- ⚠️ Test path traversal: `/workspace/../etc/passwd` must still be blocked by the
  existing security check after translation

Tasks:
- Add `CONTAINER_PROJECT_ROOT` constant to `tools/__init__.py`
- Add prefix translation at top of `validate_path()`
- Add tests: `/workspace/src/foo.py` resolves correctly, relative paths unchanged,
  `/workspace/../etc/passwd` blocked, bare `/workspace` resolves to cwd

Deliverable: File tools accept both `/workspace/...` and relative paths transparently.

Verify: `uv run pytest` — new path translation tests pass. Manual test: `read_file`
with `/workspace/src/archie/agent.py` returns file contents.

### 3. System prompt environment block

Approach:
- Build the environment string dynamically in app.py (project name, branch, OS info)
- Append to SYSTEM_PROMPT before passing to AgentLoop
- Place after the main instructions but before any project-specific context
- Include: project name, `/workspace` as cwd, Python available as `python3`, git branch,
  "use relative paths", "don't cd elsewhere", "prefer provided tools over shell"
- Do NOT include an "available CLI tools" list — that's handled separately via guidance docs

Tasks:
- Build environment block string in `_build_stack()` or `__init__`
- Concatenate with SYSTEM_PROMPT when creating AgentLoop
- Update on model switch / new session if branch changes
- Note: branch may go stale mid-session if user does `git checkout` via shell — acceptable,
  cosmetic only (doesn't affect tool correctness)

Deliverable: System prompt includes environment context; model knows it's in /workspace.

Verify: Use `self_debug` tool in a session to inspect the system prompt — environment
block is visible.

### 4. Edit confirmation with line numbers

Approach:
- In edit_file handler, after each successful match/replace, record the 1-based line number
  of the match start position (count newlines before match)
- For multi-line old text, record a range (start_line–end_line)
- Format: `Edited: path (N edits at lines 45, 163-170, 310)`
- ⚠️ Line numbers refer to positions AFTER prior edits in the batch (sequential application)

Tasks:
- Track line numbers during edit loop
- Format line references (single line vs range)
- Update success return string
- Update tests to check new format

Deliverable: Edit confirmations show which lines were modified.

Verify: `uv run pytest tests/test_write_tools.py` — updated assertions pass.

### 5. Code outline file-not-found suggestions

Approach:
- In `_handle_outline`, when `resolved.is_file()` is False, extract the basename
- Use `index._discover_files()` to find candidate files with matching name
- Suggest up to 3 matches with their relative paths
- ⚠️ `_discover_files()` uses ripgrep (fast) — acceptable for an error path
- If the path was a directory, the existing error (from our earlier fix) takes precedence

Tasks:
- Add file search logic after the `is_dir()` check and before the `is_file()` return
- Format suggestions as part of the error message
- Add test: wrong prefix path → correct suggestion shown

Deliverable: Outline on non-existent file suggests the correct path.

Verify: Manual test — `code outline archie/ui/app.py` returns suggestion for
`src/archie/ui/app.py`.
