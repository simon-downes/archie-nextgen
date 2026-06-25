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
   - Catalog is a plain `dict[str, SkillEntry]`, built once at session start
   - User skills path `~/.agents/skills/` is hardcoded as `Path.home() / ".agents" / "skills"` inside `discover_skills()`
   - `build_system_prompt()` accepts `catalog` and `loaded_skills` from this milestone onwards (empty list initially) so the "Loaded:" line renders correctly when skills are loaded in M3
   - Discovery goes in `_build_stack()` in `src/archie/ui/app.py` so it re-runs on new_session (Ctrl+N). Stored as `self._skill_catalog`
   - `<skills>` section renders after `<tools>`, before `<agents.md>`. Omitted entirely when catalog is empty.
   - ⚠️ Frontmatter parsing must handle missing/malformed frontmatter gracefully (log warning, skip file)
   Edge cases:
   - SKILL.md missing frontmatter delimiters: skip, log warning
   - Frontmatter YAML parse error: skip, log warning
   - Frontmatter missing `name` or `description`: skip, log warning
   - No skills found in any directory: `<skills>` section omitted from prompt, no tool registered
   - Duplicate name across scopes: project wins, user skill silently shadowed
   - Skill directory exists but is empty (no SKILL.md): ignored
   Tasks:
   - Add `src/archie/skills.py` with `SkillEntry` dataclass and `discover_skills(project_dir: Path) -> dict[str, SkillEntry]` (internally scans project `.archie/skills/` + `Path.home() / ".agents" / "skills"`)
   - Parse SKILL.md files: split on `---`, yaml.safe_load frontmatter, validate name+description present
   - Call `discover_skills()` in `_build_stack()` in `src/archie/ui/app.py`, store as `self._skill_catalog`
   - Update `build_system_prompt()` signature: `build_system_prompt(project_dir, git_branch, agents_md, catalog=None, loaded_skills=None)` — render `<skills>` section when catalog is non-empty
   - After M1, the call in app.py becomes: `build_system_prompt(project_dir=..., git_branch=..., agents_md=..., catalog=self._skill_catalog, loaded_skills=loaded_skills)`
   - Add `tests/test_skills.py` with fixtures for valid/invalid/duplicate skills
   Deliverable: System prompt includes a `<skills>` section listing discovered skills
   Verify: `uv run pytest tests/test_skills.py` passes; call `build_system_prompt()` with a test catalog, assert output contains `<skills>` with the skill name and description

2. Skill tool — load and read
   Approach:
   - Follows the closure pattern: `make_skill_spec(catalog, loaded_skills)` captures both
   - Skill body = everything after the second `---` line, stripped of leading/trailing whitespace. If no second `---`, body is empty string.
   - Reference files returned as raw content (no line numbers, no header) — same approach as brain tool read mode
   - Path validation for references: resolve `file` relative to `SkillEntry.path.parent`, check resolved path starts with that directory. Do not use `validate_path()` (that's for project paths) — implement a simple `resolved.resolve().is_relative_to(skill_dir)` check.
   - File listing in load response: list all files in skill dir excluding SKILL.md itself, as plain relative paths, one per line
   - `create_default_registry()` gains optional `catalog` and `loaded_skills` params
   Wiring:
   - State: `loaded_skills: list[tuple[str, str]]`, instantiated as `[]` in `app.py._build_stack()`
   - Producers: skill tool handler appends `(name, body)` during tool execution in `_execute_tools()`
   - Consumers: prompt-building closure reads it on each `_do_request()` call (wired in M3)
   - Call site: `create_default_registry(..., catalog=self._skill_catalog, loaded_skills=loaded_skills)`
   Edge cases:
   - Skill name not in catalog: return `tool_error("Unknown skill 'x'. Available: a, b, c")`
   - Duplicate load (skill already in loaded_skills): no-op, return "Skill 'x' already loaded"
   - SKILL.md has no body (only frontmatter): load succeeds, body is empty string
   - Reference file path traversal (`../`): return `tool_error("Path outside skill directory")`
   - Reference file not found: return `tool_error("File not found: references/x.md")`
   - Non-text reference file (binary): return `tool_error("Binary file")`
   Tasks:
   - Add `src/archie/tools/skill.py` with `make_skill_spec(catalog, loaded_skills)`
   - Implement load mode: parse body, check not already loaded, append to loaded_skills, list files in skill dir (excluding SKILL.md)
   - Implement read mode: resolve path relative to `SkillEntry.path.parent`, validate containment via `is_relative_to()`, return raw content
   - Add optional `catalog` and `loaded_skills` params to `create_default_registry()`, register skill tool when catalog is non-empty
   - Wire in `app.py._build_stack()`: instantiate `loaded_skills = []`, pass to `create_default_registry()`
   - Add `tests/test_tool_skill.py` covering each edge case above
   Deliverable: Skill tool loads skills into shared state and reads references safely
   Verify: `uv run pytest tests/test_tool_skill.py` — all cases pass

3. Prompt injection — loaded skills in system prompt
   Approach:
   - Add optional `build_prompt: Callable[[], str] | None = None` to `AgentLoop.__init__` alongside existing `system_prompt: str`
   - Internally: `self._build_prompt = build_prompt or (lambda: system_prompt)` — backwards-compatible, zero existing test changes
   - `_do_request()` calls `self._build_prompt()` instead of using `self.system_prompt` directly (line 423 in agent.py, the `system=self.system_prompt` arg to `self.llm.stream()`)
   - In `app.py._build_stack()`, construct the closure: `build_prompt=lambda: build_system_prompt(project_dir=..., git_branch=..., agents_md=..., catalog=catalog, loaded_skills=loaded_skills)`
   - All static params (project_dir, git_branch, agents_md, catalog) are captured at closure creation. Only `loaded_skills` content changes between calls.
   - Loaded skills render in insertion order (order loaded = order in prompt)
   - Existing tests continue passing `system_prompt="..."` with no `build_prompt` — the internal fallback wraps it in a lambda
   Wiring:
   - State: `loaded_skills` list (created in M2) is now consumed by the prompt closure
   - Producers: skill tool (M2) appends during tool execution
   - Consumers: `self._build_prompt()` called at start of each `_do_request()`
   - Call site: `AgentLoop(..., system_prompt="unused", build_prompt=lambda: build_system_prompt(...))`
   Edge cases:
   - `build_prompt` callable raises exception: should not happen (pure string formatting), but if it does, let it propagate (agent loop catches and emits TurnError)
   - `loaded_skills` modified during `build_prompt()` execution: impossible (single-threaded, tool execution completes before next request)
   Tasks:
   - Add `build_prompt` param to `AgentLoop.__init__`, wire `self._build_prompt` with fallback
   - Update `_do_request()`: change `system=self.system_prompt` to `system=self._build_prompt()`
   - Update `app.py._build_stack()` to pass `build_prompt` closure that calls `build_system_prompt()` with current loaded_skills
   - Integration test in `tests/test_agent.py`: construct AgentLoop with `build_prompt` returning a string containing "SKILL_MARKER", mock `llm.stream`, run a turn, assert `llm.stream` was called with `system=` containing "SKILL_MARKER"
   Deliverable: Loading a skill via the tool causes it to appear in the system prompt on the next request
   Verify: `uv run pytest tests/test_agent.py` passes (existing tests unchanged + new integration test)

4. Polish and debug integration
   Approach:
   - Add exercises to `debug.py` EXERCISES dict — `_build_registry()` in debug.py needs to discover skills and create a catalog (call `discover_skills(Path.cwd())`) and pass an empty `loaded_skills=[]` to `create_default_registry()`
   - Create `.archie/skills/example/SKILL.md` in the repo as a demo/test skill
   - UI summary: format_tool_pending and format_tool_complete both need a `"skill"` case
   - Load mode summary: `skill example (loaded)`
   - Read mode summary: `skill example/references/foo.md`
   - Detect mode from params: `file` param present = read mode, absent = load mode
   - Update `<tools>` section in prompt.py: add to the Patterns subsection: "Load domain expertise with the skill tool when the `<skills>` catalog lists something relevant"
   - This line is unconditional (even without skills, mentioning it is harmless since the tool won't be registered)
   Tasks:
   - Create `.archie/skills/example/SKILL.md` with a minimal demo skill + `references/example.md`
   - Update `_build_registry()` in `debug.py` to call `discover_skills()` and pass catalog/loaded_skills to `create_default_registry()`
   - Add `skill` exercises to `debug.py` EXERCISES: load example skill, read reference file
   - Add `"skill"` case to `format_tool_pending` and `format_tool_complete` in `tool_formatters.py`
   - Add skill tool mention to `_TOOLS_SECTION` in `prompt.py`
   Deliverable: Skill tool is fully integrated with debug command and UI
   Verify: `uv run archie debug skill` runs both exercises successfully showing loaded confirmation and reference content

## Risks

- **Prompt bloat from large skills:** Loading 3+ verbose skills could add 5-10K tokens to
  every request. Mitigation: document that skills should be concise (500-2000 tokens).
  Log a warning if total loaded skill content exceeds 4000 tokens.
- **Stale catalog:** Discovery runs at session start only. New skills added mid-session
  won't appear. Acceptable — restart the session.
