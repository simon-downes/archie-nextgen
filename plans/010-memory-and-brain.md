# Plan 010: Memory System + Brain Tool

## Objective

Two distinct systems for persistent knowledge:

1. **Memory** — automatic fragment-based memory extracted from session conversations by a cheap model. Queryable via a `recall` tool. No manual "remember" step needed.

2. **Brain** — curated knowledge base (projects, knowledge, people). Read/write/search/commit via a `brain` tool. Not memory — reference material.

## Context

- Session logs are already written (JSONL, one line per user turn, tool output summarised)
- The existing archie brain uses session-level summaries as memory (one big file per session) — we're replacing this with fragment-based atomic memories
- Claude Haiku ($0.25/$1.25 per M tokens) is cheap enough to run extraction every few turns
- Existing brain has ref counting via SQLite (brain.db) — worth preserving for observability
- Brain location configurable, defaults to `~/.archie/new-brain` during development

## System 1: Memory

### How it works

Every N turns (default 5), a background extraction process:
1. Reads the turns since the last extraction from the session log
2. Sends them to Haiku with an extraction prompt
3. Haiku returns structured fragments (decisions, learnings, preferences, state, context)
4. Fragments are appended to a daily per-project JSONL file

### Memory file structure

```
~/.archie/new-brain/_memory/
├── 2026-06-08-archie-nextgen.jsonl
├── 2026-06-09-archie-nextgen.jsonl
├── 2026-06-09-tillo-platform.jsonl
└── .last_extracted    # tracks extraction watermark per session
```

### Fragment schema

```json
{
  "id": "01J5KXQV9AMRN4T1JGPZ8K3QFH",
  "session_id": "2026-06-09-archie-nextgen-d8c3b",
  "type": "decision",
  "topic": "session logging format",
  "content": "Single JSONL per session, one line per user turn, tool output summarised.",
  "tags": ["archie-nextgen", "logging", "persistence"]
}
```

| Field | Type | Description |
|-------|------|-------------|
| id | string | ULID (time-sortable, globally unique) |
| session_id | string | Which session produced this fragment |
| type | string | decision, learning, preference, state, context |
| topic | string | Short topic label for retrieval |
| content | string | The actual knowledge fragment |
| tags | list[str] | Tags for filtering/retrieval |

### Fragment types

| Type | What it captures | Example |
|------|-----------------|---------|
| `decision` | A choice that was made and why | "Use JSONL not YAML for session logs — faster parsing, append-only" |
| `learning` | Something discovered/corrected | "Progressive file reading wastes more tokens than a single read" |
| `preference` | User working style/preferences | "User prefers bullet points over paragraphs" |
| `state` | Current status of something | "archie-nextgen: phases 1-5 complete, working on brain tool" |
| `context` | Background info worth retaining | "The project uses uv for packaging, ruff for linting" |

### Extraction prompt (sent to Haiku)

```
Extract durable knowledge fragments from these conversation turns.
For each fragment, provide: type (decision/learning/preference/state/context), topic (short label), content (1-2 sentences), and tags.
Only extract information worth remembering across sessions. Skip ephemeral details.
Return as a JSON array.
```

### Extraction trigger

- **Automatic**: every 5 turns during a session (configurable)
- **Manual**: `archie memory extract` CLI command (processes all unextracted turns)
- **End of session**: on `action_quit` (extract any remaining turns)

### Watermark tracking

`.last_extracted` tracks per-session extraction progress:
```json
{"2026-06-09-archie-nextgen-d8c3b": {"turn_index": 15, "extracted_at": "2026-06-09T11:00:00"}}
```

Ensures we never re-extract the same turns, handles crashes/restarts cleanly.

### `recall` tool

The query interface to memory. Separate from the brain tool.

```json
{
  "name": "recall",
  "description": "Search memory fragments from past conversations. Find decisions, learnings, preferences, and context by topic, type, project, or date range.",
  "schema": {
    "type": "object",
    "properties": {
      "query": {"type": "string", "description": "Search terms (matched against topic + content + tags)"},
      "type": {"type": "string", "enum": ["decision", "learning", "preference", "state", "context"], "description": "Filter by fragment type"},
      "project": {"type": "string", "description": "Filter by project name. Defaults to current project."},
      "since": {"type": "string", "description": "Only fragments after this date (YYYY-MM-DD)"},
      "limit": {"type": "integer", "description": "Max results (default 20)"}
    },
    "required": []
  }
}
```

Scoring: topic match (+3), tag match (+2), content match (+1), recency bonus (<7d +2, <30d +1).

## System 2: Brain

### What it is

A curated knowledge base for reference material that doesn't come from conversations. Projects, domain knowledge, people, architectural docs, etc.

### Brain directory structure

```
~/.archie/new-brain/
├── _memory/          # memory fragments (System 1, above)
├── projects/         # per-project context (architecture, conventions, decisions)
├── knowledge/        # domain knowledge (patterns, reference material)
├── people/           # people archie interacts with
├── index.yaml        # pre-built index of all items (name, path, tags, summary)
└── brain.db          # SQLite: ref counting + metrics
```

### `brain` tool

4 operations (no recall/remember — those are the memory system):

```json
{
  "name": "brain",
  "description": "Knowledge base for reference material. Use 'read' to get a specific item, 'write' to create/update, 'search' to find items, 'commit' to save to git.",
  "schema": {
    "type": "object",
    "properties": {
      "operation": {"type": "string", "enum": ["read", "write", "search", "commit"]},
      "path": {"type": "string", "description": "Path within brain (for read/write)"},
      "query": {"type": "string", "description": "Search terms (for search)"},
      "scope": {"type": "string", "description": "Limit search to subdirectory"},
      "content": {"type": "string", "description": "Markdown body (for write)"},
      "name": {"type": "string", "description": "Item title (for write)"},
      "summary": {"type": "string", "description": "Brief summary (for write)"},
      "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags (for write)"},
      "message": {"type": "string", "description": "Commit message (for commit)"},
      "paths": {"type": "array", "items": {"type": "string"}, "description": "Paths to stage (for commit, omit for all)"}
    },
    "required": ["operation"]
  }
}
```

### Index management

- `index.yaml` structure: `{type: {slug: {name, path, summary, tags}}}`
- Built on first search if empty/missing
- Updated automatically on every `write`
- Full rebuild via `archie brain reindex` CLI

### Ref counting (brain.db)

Every `brain read` and `recall` query records an access:
```sql
CREATE TABLE refs (path TEXT NOT NULL, ts INTEGER NOT NULL);
CREATE TABLE metrics (event TEXT, data TEXT, ts INTEGER);
```

Enables: "what's frequently accessed?", "what's stale?", future analytics.

### Search scoring (two-phase)

1. **Index phase**: search `index.yaml` — name (+3), tags (+2), summary (+1)
2. **Content phase**: ripgrep fallback for body matches (+1)
3. Sort by (-matches, -score), return top 20

### Write behaviour

- On create: generate frontmatter from name/summary/tags, write file, update index
- On update: merge provided fields into existing frontmatter (preserve fields not in request), write, update index
- Path validation: must be under brain_dir, reject `..`, block `.git/`, `brain.db`, `_memory/` (read-only via brain tool, written by memory system)

## CLI Commands

### `archie init`

Creates brain directory structure + git init:
```
~/.archie/new-brain/
├── _memory/
├── projects/
├── knowledge/
├── people/
├── index.yaml  (empty: {})
└── .gitignore  (brain.db, .last_extracted)
```

Idempotent — creates missing subdirs, doesn't touch existing files.

### `archie brain reindex`

Rebuilds `index.yaml` by scanning all brain .md files and extracting frontmatter.

### `archie memory extract`

Manually trigger memory extraction for all sessions with unextracted turns.

## Observability

### Tracked automatically:
- **Ref counts** (SQLite) — every brain read / recall query
- **Extraction log** (appended to metrics table) — when, fragments extracted, cost

### Available for future analysis:
- Which brain items are hot/stale?
- How many fragments per session?
- Extraction cost over time
- Recall hit rate (returned results vs empty)

## Config

```yaml
# ~/.archie/nextgen.yaml
brain_dir: ~/.archie/new-brain
memory:
  extraction_model: eu.anthropic.claude-haiku-3-20250305-v1:0
  extraction_interval: 5  # turns between extractions
```

## Milestones

### Milestone 1: Config + CLI + brain structure

- Add `brain_dir` and `memory` config section
- `archie init` — create brain directories, git init, empty index.yaml
- `archie brain reindex` — scan + build index from frontmatter
- Brain presence check at startup (fatal error if missing, tells user to run `archie init`)
- Tests: init structure, reindex builds correct index

### Milestone 2: Brain tool (read/write/search/commit)

- Create `src/archie/brain.py` — BrainIndex class (frontmatter parsing, index management, scoring, ref counting)
- Create `src/archie/tools/brain_tool.py` — tool spec + handler
- Implement: read (with ref tracking), write (with index update + frontmatter merge), search (two-phase), commit (git add + commit)
- Register in `create_default_registry()`
- SQLite brain.db for refs
- Tests: all operations, index updates, ref recording

### Milestone 3: Memory extraction process

- Create `src/archie/memory.py` — MemoryExtractor class
- Haiku client for extraction (reuse BedrockClient with different model)
- Extraction prompt + response parsing (JSON array of fragments)
- Watermark tracking (.last_extracted)
- Write fragments to daily JSONL
- Trigger: called from engine at turn intervals + on session quit
- `archie memory extract` CLI
- Tests: extraction from mock turns, watermark management, JSONL writing

### Milestone 4: Recall tool

- Create `src/archie/tools/recall.py` — tool spec + handler
- Search memory JSONL files (scan + match on topic/content/tags)
- Scoring + recency bonus
- Filter by: query, type, project, since, limit
- Ref tracking on each recall query
- Register in `create_default_registry()`
- Tests: search, filtering, scoring, recency

### Milestone 5: Integration + review

- Wire extraction trigger into the engine (every N turns)
- Wire extraction on session quit (app.py action_quit)
- Load relevant memories into system prompt at session start (recent + project-scoped)
- Run review workflow
- End-to-end test: session → extraction → recall
