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
| id | string | ULID (encodes timestamp + random — provides time-ordering without a separate ts field) |
| session_id | string | Which session produced this fragment |
| type | string | decision, learning, preference, state, context |
| topic | string | Short topic label for retrieval (reuse across related fragments) |
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

The extraction call includes the last 3 memory entries for continuity — so the model knows what's already been remembered and can avoid duplication or fragmentation (e.g. updating an existing topic rather than creating a new one).

```
Here are the most recent memory entries for context:
{last_3_fragments}

Extract knowledge fragments from these conversation turns:
{turns_to_process}

## What to capture

Capture LIBERALLY. It is better to over-capture than to miss something. Include:
- Decisions made (explicit or implicit) and the reasoning behind them
- Discussion about tradeoffs, even if no conclusion was reached
- Things that were tried and didn't work (and why)
- User preferences or working style observations
- Technical insights, patterns, or approaches discovered
- Project progress and state changes
- Questions raised that remain open
- Context that would help a future session understand what happened

## What to skip

Only skip turns that are purely mechanical with zero informational value:
- Running a linter/formatter with no discussion
- Fixing a single typo with no context
- Tool calls that just read files without any resulting insight

When in doubt, INCLUDE IT. A fragment that turns out to be low-value costs nothing. A missing fragment that was needed is unrecoverable.

## Output format

For each fragment provide: type (decision/learning/preference/state/context), topic (short label — reuse existing topics where the subject is the same), content (1-3 sentences capturing the key information), and tags.

Return an empty array ONLY if the turns are entirely mechanical (e.g. only tool calls with no discussion).

Return as a JSON array.
```

### Extraction trigger

- **On startup**: before the session begins, check watermarks and extract any unextracted turns from recent sessions. Ensures memory is up to date before the model starts working.
- **During session**: every N turns (default 5), extract the latest turns so memory stays current for long sessions.
- **On session quit**: extract any remaining turns.

No manual CLI trigger — extraction is always automatic.

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
```

Enables: "what's frequently accessed?", "what's stale?"

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

## Observability

- **Ref counts** (SQLite `refs` table) — every brain read / recall query, enables "what's hot/stale?"
- Fragment count per session visible in watermark file
- Extraction runs logged at INFO level

## Review Resolutions

1. **BedrockClient non-streaming**: Add a simple `invoke()` method to BedrockClient that uses `converse` (not `converse_stream`). Returns the full response text. ~10 lines. Extraction doesn't need streaming.

2. **Startup extraction**: Process ALL unextracted turns before the TUI starts. Display progress on the console (before Textual takes over): "Updating memory... 3 sessions, 12 turns to process" → "Processing session 1/3..." → "Done." User sees what's happening and waits. If it becomes slow later, we optimise then.

3. **During-session extraction concurrency**: Fire-and-forget in a separate Worker thread. The extraction reads the session JSONL (already written) and writes to a separate memory JSONL — no shared state with the engine. `.last_extracted` is written only by the extraction thread (no contention). If two extractions overlap somehow, the watermark prevents re-processing.

4. **Recall search strategy**: Linear scan with date-range pre-filtering. The filename contains the date, so `since` filter skips entire files. Within a file, it's line-by-line scan. For 6 months of daily use (~180 files, ~1000 fragments per project), a full scan is <100ms (just JSON parsing + substring match). No index needed until we have 10K+ fragments.

5. **Watermark fragility**: Track by ULID of last extracted turn (from the session JSONL's turn `id` field), not by line number. If the file is externally modified, worst case we re-extract some turns (idempotent — duplicate fragments are low-cost noise, not corruption).

6. **Edge cases**:
   - First session: no `.last_extracted` → nothing to extract on startup, process first turns after N accumulate
   - Corrupt JSONL: `json.loads` per line, skip lines that fail with a warning log
   - Haiku malformed response: try `json.loads`, if it fails log warning and skip this extraction batch (next batch will include these turns)

7. **action_quit extraction**: Best-effort with 5s timeout. If Haiku doesn't respond in time, skip. The turns will be caught on next startup anyway.

8. **Config changes**: Add `brain_dir` and `memory` to Config dataclass with defaults. `load_config()` already handles missing fields gracefully (uses defaults for anything not in the YAML file).

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
- Extraction prompt with last 3 fragments as context (topic continuity)
- Response parsing (JSON array of fragments)
- Watermark tracking (.last_extracted)
- Write fragments to daily JSONL
- Run on startup: process all unextracted turns before session starts
- Trigger during session: every N turns + on session quit
- Tests: extraction from mock turns, watermark management, JSONL writing, continuity context

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
