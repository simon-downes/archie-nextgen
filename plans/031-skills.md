# Plan 031: Skills System

## Objective

Add a skills system that lets Archie load domain-specific knowledge, patterns,
and workflows into the system prompt on demand. Skills are persistent for the
session (never evicted), discovered from conventional directories, and loaded
via a tool the model calls when it recognises relevance.

## Context

Session analysis showed models perform significantly better when given explicit
patterns and strategies. The system prompt already has a `<tools>` section with
usage patterns — skills extend this concept to arbitrary domains (testing
conventions, deployment workflows, language idioms, project-specific rules).

Research of 8 reference projects (Codex, Maki, OpenCode, CLI-Agent-Orchestrator,
Cline, Open-SWE, AMCP, Amazon Q) showed universal convergence on: markdown with
YAML frontmatter, two-stage loading (catalog + on-demand content), and system
prompt injection for persistence.

## Requirements

- MUST discover skills from `<project>/.archie/skills/` and `~/.agents/skills/`
  - AC: Skills in project dir and user dir are both found at session start
  - AC: Project skills take priority over user skills with the same name

- MUST display a catalog of available skill names and descriptions in the system prompt
  - AC: `<skills>` section lists all discovered skills

- MUST provide a `skill` tool that loads a skill's body into the system prompt
  - AC: After calling `skill {"name": "x"}`, the next LLM request includes the skill body
  - AC: Tool returns confirmation message and lists reference files if any exist

- MUST persist loaded skills in the system prompt for the remainder of the session
  - AC: Loaded skill content appears in every subsequent request's system prompt
  - AC: Loaded skill content is not subject to context eviction

- MUST support reading reference files from a skill's directory via the same tool
  - AC: `skill {"name": "x", "file": "references/foo.md"}` returns file content
  - AC: Paths outside the skill directory are rejected

- MUST use YAML frontmatter in SKILL.md with `name` and `description` fields
  - AC: Skills missing required frontmatter are skipped with a warning logged

- SHOULD indicate which skills are currently loaded in the `<skills>` catalog
  - AC: Loaded skill names are marked in the catalog section

## Design

### Skill Format

Standard markdown with YAML frontmatter, same format used by Codex, OpenCode,
Maki, and CAO:

```
<project>/.archie/skills/<skill-name>/
├── SKILL.md            # Required: frontmatter (name, description) + body
└── references/         # Optional: supporting docs loadable via skill tool
```

### Discovery Scopes (priority order)

1. `{project_dir}/.archie/skills/*/SKILL.md` — project-specific
2. `~/.agents/skills/*/SKILL.md` — user-level, cross-project

One level deep only (no recursive scan). Duplicate names: project wins.

### Prompt Integration

The system prompt gains two additions:

1. Catalog (always present):
```xml
<skills>
Available (load with the skill tool when relevant):
- python-testing: pytest patterns, fixtures, mocking strategies
- git-workflow: branching, commits, PR process

Loaded: python-testing
</skills>
```

2. Loaded skill bodies (after `<tools>`, one per loaded skill):
```xml
<skill python-testing>
[full SKILL.md body here]
</skill>
```

### Dynamic Prompt Mechanism

Today `self.system_prompt` in `AgentLoop` is a static string set in `__init__`.
The skill tool needs to modify the prompt mid-session.

Approach: `AgentLoop.__init__` keeps `system_prompt: str` but adds an optional
`build_prompt: Callable[[], str] | None` parameter. If provided, it's used;
otherwise the static string is wrapped internally:

```python
# In AgentLoop.__init__:
self._build_prompt = build_prompt or (lambda: system_prompt)

# In _do_request():
system = self._build_prompt()
```

This is backwards-compatible — all existing callers pass a string and work
unchanged. Only `app.py` passes the callable. Zero test breakage.

### State Ownership and Wiring

`loaded_skills: list[tuple[str, str]]` is instantiated in `app.py` alongside
other shared session state (`mtime_cache`, `pre_content_stash`). It's passed to:

1. `create_default_registry(..., catalog=catalog, loaded_skills=loaded_skills)`
   — the skill tool captures it via closure, mutates it on load
2. The prompt-building closure passed to `AgentLoop` — reads it on each request

This mirrors how `mtime_cache` is already shared between tools (mutate) and the
agent loop (read/invalidate). Same pattern, same wiring location in `app.py`.

### User Skills Path

`~/.agents/skills/` is hardcoded in `discover_skills()` as a default. It's a
conventional cross-tool path (matching Maki/OpenCode standards) that doesn't
vary per-project. No config key needed. If customisation is needed later, add
one then.

### Thread Safety

Not a concern. The agent loop is single-threaded: tool execution and LLM
requests happen sequentially on the same worker thread. The skill tool mutates
`loaded_skills`, then the next `_do_request()` reads it — always in order.

## Milestones

1. Skill discovery and catalog
   Approach:
   - New module `src/archie/skills.py` following existing module pattern (module docstring, dataclass, log)
   - Use `pyyaml` (already a dependency) for frontmatter parsing — split on `---` delimiters, `yaml.safe_load` the middle
   - Discovery returns a frozen dataclass `SkillEntry(name, description, path)` per skill
   - `SkillCatalog` is a dict mapping name → `SkillEntry`, built once at session start
   - User skills path `~/.agents/skills/` is hardcoded in `discover_skills()` as a constant
   - `build_system_prompt()` accepts `loaded_skills` from this milestone onwards (empty initially) so the "Loaded:" line renders correctly when skills are loaded in M3
   - ⚠️ Frontmatter parsing must handle missing/malformed frontmatter gracefully (log warning, skip file)
   Tasks:
   - Add `src/archie/skills.py` with `SkillEntry` dataclass and `discover_skills(project_dir: Path) -> dict[str, SkillEntry]` (scans project + user paths internally)
   - Parse SKILL.md files: split on `---`, yaml.safe_load frontmatter, validate name+description present
   - Call `discover_skills()` in `src/archie/ui/app.py` at session start
   - Update `build_system_prompt()` to accept `catalog: dict[str, SkillEntry]` and `loaded_skills: list[tuple[str, str]]`, render `<skills>` section with catalog and loaded state
   - Add `tests/test_skills.py` with fixtures for valid/invalid/duplicate skills
   Deliverable: System prompt includes a `<skills>` section listing discovered skills
   Verify: `uv run pytest tests/test_skills.py` passes; create a test skill in `.archie/skills/`, verify `build_system_prompt()` output includes the skill name in `<skills>`

2. Skill tool — load and read
   Approach:
   - Follows the closure pattern: `make_skill_spec(catalog, loaded_skills)` captures both
   - `loaded_skills` is a `list[tuple[str, str]]` (name, body) — instantiated in `app.py`, passed to both registry and prompt builder
   - `create_default_registry()` gains `catalog` and `loaded_skills` params (both optional, tool only registered when catalog is non-empty)
   - Mode 1 (load): read SKILL.md body (everything after second `---`), append to loaded_skills, return confirmation + file listing via `os.listdir` on skill dir
   - Mode 2 (read reference): resolve `file` param relative to skill's directory path, validate it stays within (no `..` escape), return content
   - ⚠️ The `file` param must be validated like `validate_path` — reject traversal outside skill dir
   Tasks:
   - Add `src/archie/tools/skill.py` with `make_skill_spec(catalog, loaded_skills)`
   - Implement load mode: read body, append to loaded_skills, format file listing
   - Implement read mode: resolve path, validate containment, return content
   - Add `catalog` and `loaded_skills` params to `create_default_registry()`, register skill tool when catalog is non-empty
   - Wire in `app.py`: instantiate `loaded_skills = []`, pass to both `create_default_registry()` and prompt closure
   - Add `tests/test_tool_skill.py` covering: load, load duplicate, read reference, path escape rejection
   Deliverable: Skill tool loads skills and reads references correctly
   Verify: `uv run pytest tests/test_tool_skill.py` passes

3. Prompt injection — loaded skills in system prompt
   Approach:
   - Add optional `build_prompt: Callable[[], str] | None = None` to `AgentLoop.__init__` alongside existing `system_prompt: str`
   - Internally: `self._build_prompt = build_prompt or (lambda: system_prompt)` — backwards-compatible, zero test changes
   - `_do_request()` calls `self._build_prompt()` instead of using `self.system_prompt` directly
   - In `app.py`, construct the callable as a closure over `loaded_skills`, catalog, and other prompt inputs
   - `build_system_prompt()` already accepts `loaded_skills` from M1 — it renders each loaded skill as `<skill name>...</skill>` after `<tools>`
   Tasks:
   - Add `build_prompt` param to `AgentLoop.__init__`, wire `self._build_prompt` with fallback
   - Update `_do_request()` to call `self._build_prompt()`
   - Update `app.py` to pass a closure: `build_prompt=lambda: build_system_prompt(..., loaded_skills=loaded_skills)`
   - Write integration test: mock LLM, call skill tool, verify next `stream()` call receives system prompt containing the skill body
   Deliverable: Loading a skill via the tool causes it to appear in the system prompt on the next request
   Verify: `uv run pytest tests/test_agent.py` and integration test pass; existing tests unchanged (still pass string via `system_prompt`)

4. Polish and debug integration
   Approach:
   - Add exercises to `debug.py` EXERCISES dict — needs a test skill to exist
   - Create `.archie/skills/example/SKILL.md` in the repo as a demo/test skill
   - UI summary format: `skill python-testing (loaded)` or `skill python-testing/references/foo.md`
   - Update `<tools>` section in prompt.py to mention skill usage pattern
   Tasks:
   - Create `.archie/skills/example/SKILL.md` with a minimal demo skill
   - Add `skill` exercises to `debug.py` (load, read reference)
   - Add UI summary case for skill tool in `tool_formatters.py`
   - Add one line to `<tools>` section: "Use the skill tool to load domain expertise when available"
   Deliverable: Skill tool is fully integrated with debug command and UI
   Verify: `uv run archie debug skill` runs both exercises successfully

## Risks

- **Prompt bloat from large skills:** Loading 3+ verbose skills could add 5-10K tokens to
  every request. Mitigation: document that skills should be concise (500-2000 tokens).
  Log a warning if total loaded skill content exceeds 4000 tokens.
- **Stale catalog:** Discovery runs at session start only. New skills added mid-session
  won't appear. Acceptable — restart the session.
