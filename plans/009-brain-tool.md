# Plan 009: Brain Tool (DEPRECATED)

> **Superseded by Plan 010** — the brain and memory concerns have been separated into distinct systems. See `plans/010-memory-and-brain.md`.

## Original Objective

Add a `brain` tool that provides frontmatter-aware knowledge management over `~/.archie/brain/`. Five operations: recall (memory retrieval), remember (memory creation), read (any item), write (any item), search (scored across everything).

## Context

- The brain at `~/.archie/brain/` contains: projects, knowledge, people, _archie (memory + logs), _inbox
- All items have YAML frontmatter: `name`, `summary`, `tags`, `status`, `priority`, `sources`
- Memory files live in `_archie/memory/` with date-project naming convention
- Session logs (our new JSONL format) are already written to `~/.archie/sessions/`
- The brain directory already exists and is populated from the current archie system
- README.md is loaded as context at session start; last session log provides continuity
- `brain.db` exists with basic refs/watermark tables (for the existing memory workflow)

## Design Decisions

### Frontmatter as first-class data

Every read/write/search operation understands YAML frontmatter. Reading returns metadata structured separately from content. Writing generates proper frontmatter. Search scores matches differently based on which field matched.

### Search scoring

Matches are scored additively per term (matching the proven agent-kit approach):
- **name/slug match**: +3 per term
- **tag match**: +2 per term
- **summary match**: +1 per term
- **body content match** (via rg): +1 per term
- **Recency bonus** (memories only): <7d +2, <30d +1, >90d -1

Results sorted by (-matches, -score), top 20.

### Memory is special

`recall` and `remember` are memory-specific operations with extra semantics:
- `recall` auto-scopes to current project by default, includes recency weighting
- `remember` generates the filename, date, frontmatter automatically — model just provides the knowledge

### No model needed for retrieval

All search/scoring is deterministic (keyword matching, field weighting, recency). The *model* decides what to recall and what to remember — the tool just stores and retrieves efficiently.

## Schema

```json
{
  "name": "brain",
  "description": "Knowledge management. Use 'recall' to retrieve relevant memories. Use 'remember' to persist learnings. Use 'search' to find anything in the knowledge base. Use 'read'/'write' for specific items. Use 'commit' to save changes to git.",
  "schema": {
    "type": "object",
    "properties": {
      "operation": {
        "type": "string",
        "enum": ["recall", "remember", "read", "write", "search", "commit"],
        "description": "recall: find memories. remember: save a memory. read: get an item. write: create/update an item. search: find across all brain content. commit: git commit changes."
      },
      "query": {
        "type": "string",
        "description": "Search terms (for recall/search). Space-separated keywords."
      },
      "path": {
        "type": "string",
        "description": "Path within the brain (for read/write). e.g. 'projects/archie-nextgen/architecture.md'"
      },
      "content": {
        "type": "string",
        "description": "Content to write (for remember/write). Markdown body without frontmatter."
      },
      "name": {
        "type": "string",
        "description": "Item title (for remember/write). Used in frontmatter."
      },
      "summary": {
        "type": "string",
        "description": "Brief summary (for remember/write). Used in frontmatter and search."
      },
      "tags": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Tags (for remember/write/recall). Used for categorisation and search."
      },
      "scope": {
        "type": "string",
        "description": "Limit search to a subdirectory. e.g. 'projects', 'knowledge', '_archie/memory'"
      },
      "message": {
        "type": "string",
        "description": "Commit message (for commit operation)."
      },
      "paths": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Paths to stage for commit. If omitted, stages all changes."
      }
    },
    "required": ["operation"]
  }
}
```

## Operations

### recall

Retrieve relevant memories. Always searches `_archie/memory/` — no scope override.

**Input**: `{"operation": "recall", "query": "terraform module patterns", "tags": ["terraform"]}`

**Behaviour**:
1. Search `_archie/memory/` only (hard-scoped)
2. Match query against name, summary, tags, body
3. Score matches (field weights + recency bonus: files < 7 days get 2x, < 30 days get 1.5x)
4. Return top 10 results as summaries (name + summary + tags + path)

**Output**:
```
Found 3 relevant memories:

[1] Container-to-Container Networking for Agent Browser (score: 12)
    Summary: Added Caddy proxy sidecar for container access; resolved login issues
    Tags: core, docker, networking, caddy
    Path: _archie/memory/2026-05-13-core-965f.md

[2] ...
```

The model can then `read` the full item if needed.

### remember

Create a new memory file with generated frontmatter and filename.

**Input**:
```json
{
  "operation": "remember",
  "name": "Session logging uses single JSONL per session",
  "summary": "Replaced multi-file persistence with one JSONL file per session, one line per user turn",
  "tags": ["archie-nextgen", "session", "logging", "decisions"],
  "content": "## Decision\n\nSingle JSONL file at ~/.archie/sessions/{date}-{project}-{hash}.jsonl\n..."
}
```

**Behaviour**:
1. Generate filename: `YYYY-MM-DD-{project}-{short_hash}.md`
2. Generate frontmatter from name, summary, tags
3. Write to `_archie/memory/`
4. Return confirmation with path

### read

Read any brain item. Returns frontmatter metadata separately from body content.

**Input**: `{"operation": "read", "path": "projects/archie/improvements/session-handoff.md"}`

**Output**:
```
File: projects/archie/improvements/session-handoff.md
Name: Session Handoff
Summary: End-of-session artifacts that let the next session pick up without context loss
Tags: archie, improvements, skills, handoff, memory, sessions
Status: proposed
Priority: medium
---

# Session Handoff

## Why This Matters
...
```

### write

Create or update a brain item with proper frontmatter.

**Input**:
```json
{
  "operation": "write",
  "path": "projects/archie-nextgen/decisions/session-logging.md",
  "name": "Session Logging Format",
  "summary": "Single JSONL per session, one line per user turn, no header",
  "tags": ["archie-nextgen", "decisions", "logging"],
  "content": "# Session Logging Format\n\n## Decision\n..."
}
```

**Behaviour**:
1. Build frontmatter from provided name, summary, tags (+ any additional fields)
2. Create parent directories if needed
3. Write frontmatter + content
4. Return confirmation

### search

Scored search across the entire brain (or scoped to a subdirectory).

**Input**: `{"operation": "search", "query": "terraform module", "scope": "knowledge"}`

**Behaviour**:
1. Walk files in scope (or entire brain), parse frontmatter
2. Match query terms against: name (×10), tags (×8), summary (×5), body (×1)
3. Score = sum of field_weight × term_match_count
4. Return top 20 results sorted by score

**Output**:
```
Found 5 results for "terraform module":

[1] Terraform Module Patterns (score: 25)
    Summary: Common patterns for structuring Terraform modules
    Tags: terraform, infrastructure, patterns
    Path: knowledge/terraform/module-patterns.md

[2] ...
```

## Review Resolutions

1. **Substring vs whole-word matching**: Substring is acceptable. "form" matching "terraform" is a feature not a bug — better to over-match and let the scoring sort it out than miss relevant results. Query tokenization: `query.lower().split()`. Empty query with tags provided → match on tags only.

2. **recall vs search distinction**: Make it hard — `recall` ALWAYS searches `_archie/memory/` regardless of scope param. Remove `scope` from recall. If the model wants to search knowledge/projects, it uses `search` with a scope. Clear rule: recall = memories only, search = everything.

3. **Write preserving existing frontmatter**: On update, read existing file first. Merge provided fields into existing frontmatter (overwrite name/summary/tags if provided, preserve status/priority/sources/any other fields). On create, only write the provided fields. This way writes don't destroy metadata the model didn't explicitly set.

4. **Path validation**: Simple containment check — resolve the path, verify it's under `brain_dir`. Reject paths containing `..`. No access to `validate_path()` (different root). Block writes to: `.git/`, `brain.db`, `.brain.lock`, `_archie/logs/` (read-only).

5. **Edge cases**:
   - `brain_dir` doesn't exist → startup check in `archie chat` (like Docker check). Error tells user to run `archie init`. Tool never encounters a missing brain.
   - `.brain.lock` → ignore (it's for the external `ak` tool, not for us)
   - `brain.db` → don't use (legacy, we maintain our own index.yaml)
   - `.git` → don't auto-commit (the `commit` operation handles this explicitly)
   - `simon/` directory → the tool doesn't care about specific directory names, it searches everything
   - Non-.md files → skip in search/recall (only process `.md` files)

6. **Registry integration**: Add `brain_dir` and `project_name` as params to `create_default_registry()`. The app already has both values. Small signature change.

7. **Truncation**: Cap search/recall output with `truncate_result()` as with other tools. Each result shows frontmatter only (name + summary + tags + path), not body content. 20 results × ~100 chars = ~2000 chars max — within bounds.

8. **Index management**: Build `index.yaml` on first search if it doesn't exist. Update it on every write/remember. Provide `archie brain reindex` CLI command for manual rebuild. Index is fast to build (just frontmatter parsing, no content).

9. **Brain location**: Config option `brain_dir` (default `~/.archie/new-brain`). Separate from existing brain to avoid conflicts during development.

10. **Commit operation**: A 6th operation `commit` that stages specified paths (or all changes) and commits to the brain's git repo with a message. Model writes one or more files then commits as a batch.

11. **Init command**: `archie init` creates the brain directory structure (subdirectories only). Fails gracefully if already initialized.

### commit

Stage and commit changes to the brain's git repo.

**Input**: `{"operation": "commit", "message": "Add session logging decision", "paths": ["projects/archie-nextgen/decisions/session-logging.md"]}`

**Behaviour**:
1. If `paths` provided, `git add` those specific paths. Otherwise `git add -A`.
2. `git commit -m "{message}"`
3. Return confirmation or error (e.g. nothing to commit)

The model typically does: write → write → commit (batch writes then commit once).

## CLI Commands

### `archie init`

Creates the brain directory structure:
```
~/.archie/new-brain/
├── _archie/
│   └── memory/
├── projects/
├── knowledge/
├── people/
└── index.yaml (empty: {})
```

Also runs `git init` in the brain directory.

### `archie brain reindex`

Rebuilds `index.yaml` by scanning all brain files and extracting frontmatter. Useful after external edits or bulk imports.

## Implementation

### Architecture

```
src/archie/brain.py              — BrainIndex class (frontmatter parsing, scoring, memory management)
src/archie/tools/brain.py        — tool spec + handler (operation dispatch)
```

### BrainIndex class

The existing brain has an `index.yaml` at its root — a pre-built index of all items with name, path, tags, and summary. We leverage this for fast search (no filesystem scan needed when index exists). Falls back to filesystem scan + frontmatter parsing when index is missing.

```python
class BrainIndex:
    def __init__(self, brain_dir: Path, project_name: str):
        self._brain_dir = brain_dir
        self._project_name = project_name
        self._index: dict | None = None  # Loaded from index.yaml on first use

    def _load_index(self) -> dict:
        """Load index.yaml if it exists. Structured as {type: {slug: {name, path, summary, tags}}}."""

    def recall(self, query: str, tags: list[str]) -> list[SearchResult]
    def remember(self, name: str, summary: str, tags: list[str], content: str) -> Path
    def read(self, path: str) -> tuple[dict, str]  # (frontmatter, body)
    def write(self, path: str, name: str, summary: str, tags: list[str], content: str) -> Path
    def search(self, query: str, scope: str) -> list[SearchResult]
```

### Search strategy (two-phase, matching existing agent-kit approach)

1. **Phase 1: Index search** — if `index.yaml` exists, search it (name, tags, summary). Fast, no file I/O.
2. **Phase 2: Content search** — ripgrep across brain files for terms not found in the index. Returns excerpts.
3. **Merge and score** — combine results, apply field weights + age decay for memories.

### Scoring (refined from agent-kit)

```python
# Index match scoring (per term)
name/slug match: +3
tag match: +2
summary match: +1

# Content match scoring (per term, via rg)
body match: +1

# Recency bonus (memories only, from filename date)
< 7 days: +2
< 30 days: +1
> 90 days: -1
```

### Query tokenization

Multi-word queries are split into terms with stopwords removed. Each term is searched independently. This matches the existing agent-kit approach and prevents "the terraform module" from failing because "the" isn't in any file.

### Existing infrastructure we leverage (read-only)

- **`index.yaml`** — pre-built item index (maintained by existing `ak brain reindex`)
- **`brain.db`** — existing refs table (we can record_ref on read for continuity)
- **ripgrep** — content search fallback (already available)

### Brain directory config

Brain root defaults to `~/.archie/new-brain/` (avoids conflicting with existing brain during development). Configurable via `brain_dir` in `nextgen.yaml`.

## Milestones

### Milestone 1: Config + CLI commands

- Add `brain_dir` to config (default `~/.archie/new-brain`)
- Add `archie init` command — creates brain directory structure + `git init`
  - Creates: `_archie/memory/`, `projects/`, `knowledge/`, `people/`, empty `index.yaml`
  - No-op for existing subdirs (creates any that are missing, doesn't touch existing files)
- Add `archie brain reindex` command — rebuilds `index.yaml` from filesystem
- Add brain presence check at startup (non-fatal warning like Docker, or fatal if brain is required)
- Tests: init creates structure, init is idempotent, reindex builds correct index

### Milestone 2: BrainIndex + index management

- Create `src/archie/brain.py` with `BrainIndex` class
- Implement frontmatter parsing (YAML extraction)
- Implement `build_index()` — scan all brain .md files, extract frontmatter, write `index.yaml`
  - Index structure: `{type: {slug: {name, path, summary, tags}}}` where type = parent directory name (projects, knowledge, people, memory), slug = filename stem
- Build index on first search if `index.yaml` is empty/outdated
- Update index entry on write/remember
- Two-phase search: index first, then rg fallback for content matches (rg not found → skip phase 2, log warning)
- Stopword removal for multi-word queries
- Tests: parse frontmatter, build index, update index on write, search scoring

### Milestone 3: read + write + search operations

- Implement `read()` — returns structured frontmatter + body
- Implement `write()` — generates/merges frontmatter, creates dirs, writes file, updates index
- Implement `search()` — two-phase search (index + rg), scored, scoped
- Tests: read brain files, write with frontmatter merge, search with scoring

### Milestone 4: recall + remember operations

- Implement `recall()` — memory-scoped search with recency weighting (hard-scoped to `_archie/memory/`)
- Implement `remember()` — auto-generates filename, writes to `_archie/memory/`, updates index
- Memory filename convention: `YYYY-MM-DD-{project}-{hash}.md` (project defaults to "general" if empty)
- Tests: recall with recency bonus, remember file creation, index update

### Milestone 5: commit operation + tool registration

- Implement `commit()` — `git add` specified paths (or `git add -A` for all) + `git commit -m`
  - Note: `git add -A` stages everything — intentional for batch operations
- Create `src/archie/tools/brain.py` (tool spec, handler, operation dispatch)
- Register in `create_default_registry()` (add `brain_dir` and `project_name` params — 2 call sites in app.py)
- Tests: commit stages and commits, handles empty changeset, tool handler routing
- Run review workflow
