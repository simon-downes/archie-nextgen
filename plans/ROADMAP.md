# Archie Nextgen — Roadmap

Progressive enhancement approach. Each phase builds on the last and is independently useful.
Learning project — slow and steady, understand each piece before moving on.

## Phase 1: Core Chat Loop ✅

- [x] Textual UI (tabs, conversation, status bar, input)
- [x] Bedrock converse_stream integration
- [x] Session persistence (meta.json + turns.jsonl + raw/)
- [x] Token tracking + cost display
- [x] Interrupt/cancel (Esc)
- [x] Config (~/.archie/nextgen.yaml)

---

## Phase 2: Tool Framework + File Tools

Get the model doing useful work. Establishes the tool-calling pattern everything else builds on.

- Tool dispatch framework (parse toolUse from Bedrock, execute, return toolResult)
- File read tool (with line limits, offset support)
- File search tool (grep/ripgrep wrapper, returns line numbers + context)
- Tool result truncation (hard cap on chars returned to context)
- Display tool calls in the UI (show what's being called, collapsible results)

## Phase 3: Sandboxing

Must come before shell. No approvals means the blast radius must be contained.

- Docker-based execution environment
- Mount strategy (project dirs read-write, system read-only)
- Network policy (allow/deny outbound)
- Agent runs inside container, UI runs outside
- File write tool (enabled now — writes go to container, sync back)

## Phase 4: Shell

The big unlock. Safe now because sandboxed.

- Shell tool (command execution, timeout, output capture)
- `!` prefix in input box to run shell commands directly (user convenience, not agent)
- Display shell output in conversation
- Web fetch/search tools (network policy already in place from sandboxing)

## Phase 5: UI Polish + UX

Minimal features done well > many features done poorly. Daily driver quality.

- Theming (proper colour scheme, light/dark, customisable)
- Command palette (Ctrl+P or similar — switch session, change model, load skill)
- `$EDITOR` support for large input (write to temp file, read on save)
- Keyboard shortcut refinements
- Markdown rendering improvements (code block copy, syntax highlighting accuracy)
- Responsive layout (handle terminal resize gracefully)

## Phase 6: System Prompt + Persona

Make it actually feel like Archie rather than a generic assistant.

- System prompt assembly (compose from parts: persona + context + active skills index)
- Configurable persona (port Archie's personality from existing system)
- Project context injection (working directory, key files)

## Phase 7: Memory + Brain

Persistent knowledge across sessions. Core to what makes Archie *Archie*.

- Memory/brain read tool (search across stored knowledge)
- Memory write tool (save insights, decisions, learnings)
- Session log ingestion (extract key info from past sessions)
- Project-scoped vs global memory
- Brain search (semantic or keyword across all stored knowledge)

## Phase 8: Context Efficiency

Essential for long sessions and keeping costs sane.

- Tool result eviction (replace old tool results with summaries after N turns)
- Smart file reading (search first → targeted read, not whole files)
- Conversation compression (summarise older blocks, keep recent verbatim)
- Code outline tool — returns just signatures + docstrings, not bodies (~90% token
  reduction for understanding a file's role vs reading the full source). Use tree-sitter
  or AST parsing to extract classes, functions, imports programmatically. The model
  uses this for overview tasks ("summarise this project") and read_file for editing tasks.
- Directory tree tool — structured project overview without reading individual files

## Phase 9: Skills

On-demand knowledge loading — the token-efficient alternative to bloating the tool list.

- Skill format (SKILL.md with frontmatter metadata)
- Skill index in system prompt (one line per skill, minimal tokens)
- Skill loading tool (model requests a skill, full content enters context)
- Skills as slash commands (e.g. `/plan`, `/review` — triggers skill load + specific workflow)
- Skill directory configuration

## Phase 10: SaaS Integration

Leverage existing archie CLIs via shell tool + skills.

- Port existing CLIs (linear, notion, slack, jira, google) into the sandbox
- Write skills documenting each CLI's interface
- Composable pipelines (pipe CLI output between tools)

## Phase 11: Session Management

Multiple sessions, resume, model switching. Nice-to-have — multiple terminal instances work fine until then.

- Session resume (list previous sessions, reload into UI)
- Multiple model support (switch model mid-session via command/palette)
- Session metadata display (age, turn count, which model)

## Phase 12: Subagents

Delegate work to specialised sub-conversations.

- Spawn a sub-session with a scoped system prompt
- Return summarised results to parent
- Use cases: research, code review, planning

## Phase 13: Daemon Architecture

Decouple the UI from the brain. Enables multi-client.

- Daemon process (manages sessions, runs LLM calls)
- Unix socket protocol (terminal clients)
- HTTP/WebSocket (web/mobile clients)
- Write-lock protocol (one writer, multiple viewers)
- Session multiplexing

---

## Not Yet Planned

- Hooks/event system (most hook use cases will be built-in features)
- Web UI
- Mobile client
- Multi-user / collaboration
- Autonomous background tasks (cron-style)
