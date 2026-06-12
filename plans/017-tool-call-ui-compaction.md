# Plan 017: Tool Call UI Compaction

## Objective

Replace per-tool-call blocks with per-iteration blocks. One iteration = one LLM request that may include multiple tool calls. The block streams tool summaries as they complete, matching Kiro's compact visual style. Also generate UI-only diffs for edit_file and write_file (not sent back to the model).

## Context

- Currently each tool call is its own `ToolCallMessage` widget — a 46-turn session produces dozens of blocks dominating the conversation
- Kiro shows all tool calls from one iteration in a single compact block: `● Tool summary` lines streamed as they complete, interleaved with model thinking text
- The current `ToolFinished` event already carries a `summary` string — we need a richer, tool-specific UI summary that includes key params
- edit_file currently returns only "Edited: path (N edits)" to the model — we want a rich Kiro-style diff shown in the UI only (not inflating context)

## Design Decisions

### Block-per-iteration vs block-per-tool

The model streams text, then emits N tool_use blocks in a single response. Those N tools are one "iteration". We group them into a single UI widget.

**Iteration block lifecycle:**
1. Created when the first `ToolStarted` event fires after text streaming
2. Each `ToolStarted` appends a pending line (`○` in primary colour + summary text)
3. Each `ToolFinished` replaces that pending line with a completed summary (`●` green/red + summary text, possibly multi-line for shell/edit/write)
4. Block is "done" when the next text stream begins or the turn ends

**Important:** Only the `●`/`○` indicator is coloured — the summary text itself is default text colour.

### UI-only diffs for edit_file and write_file

The agent only sees concise results like "Edited: path (N edits)" or "Written: path (N lines)". The UI shows a compact Kiro-style diff with ±1 line context below the summary line. This is display-only — never sent back to the model.

**Approach:** The edit_file and write_file handlers read the file content before modifying
it and store it in a module-level dict keyed by `current_tool_use_id`. After the handler
returns, the agent loop reads the pre-content from that dict and passes it to
`format_tool_detail()` to generate the diff. This avoids changing the handler return type
contract — handlers still return `str`. The pre-content dict is cleared after each use.

### No expansion needed

All essential information is in the summary itself (including multi-line diffs for edits).
The iteration block is a static display block — same as user messages or assistant messages.
No collapse, no expand, no interactivity.

### Summary format per tool

Each tool gets a summary designed for quick scanning. Most are single-line. Shell commands
may be multi-line if the command itself spans lines. Edit/write tools include a diff block.

**Indicators (only the dot is coloured):**
- `○` (primary theme colour) — tool in progress
- `●` (green) — tool completed successfully
- `●` (red) — tool failed

#### read_file

The format always communicates which lines were read. `offset` defaults to 0 (line 1),
`limit` defaults to None (read until EOF or byte budget). Either way, we know the actual
ending line number from the result.

| Scenario | Format | Example |
|----------|--------|---------|
| Full file (no offset/limit) | `Read <path> (<N> lines)` | `● Read src/archie/agent.py (254 lines)` |
| With offset only | `Read <path> (L<start>–<end>)` | `● Read src/archie/agent.py (L200–254)` |
| With limit only | `Read <path> (L1–<end>)` | `● Read src/archie/agent.py (L1–50)` |
| With offset + limit | `Read <path> (L<start>–<end>)` | `● Read src/archie/agent.py (L200–250)` |
| Cache hit (full file) | `Read <path> (<N> lines - cached)` | `● Read src/archie/agent.py (254 lines - cached)` |
| Cache hit (with range) | `Read <path> (L<start>–<end> - cached)` | `● Read src/archie/agent.py (L200–250 - cached)` |
| Error | `Read <path> — <error>` | `● Read /etc/shadow — Denied: outside allowed directories` |

#### write_file

Summary line followed by a Kiro-style diff showing what was written. For new files,
show the first/last few lines. For overwrites, show the unified diff if we have the
previous content (from mtime cache).

| Scenario | Format |
|----------|--------|
| New file | `Write <path> (<N> lines)` |
| Overwrite | `Write <path> (<N> lines)` |
| Error | `Write <path> — <error>` |

Example (new file):
```
● Write src/archie/tools/foo.py (42 lines)
```

Example (overwrite with diff available):
```
● Write src/archie/tools/foo.py (42 lines)
  added 2 lines, removed 1 line at L15 in foo.py
   14   def handler(params):
   15-      return "old"
   15+      result = compute(params)
   16+      return result
   17       pass
```

#### edit_file

Summary line followed by a Kiro-style diff. The diff shows ±1 line of context,
with `+` lines in green and `-` lines in red. Line numbers are shown. Limited to
a reasonable number of diff lines to prevent bloat.

| Scenario | Format |
|----------|--------|
| Single edit | `Edit <path>` |
| Multiple edits | `Edit <path> (<N> edits)` |
| With replace_all | `Edit <path> (<N> replacements)` |
| Error | `Edit <path> — <error>` |

Example:
```
● Edit src/archie/ui/app.py (3 edits)
  added 5 lines, removed 3 lines at L265 in app.py
   265   ### Milestone 5: Styling and polish
   266
   267-  - Green `●` / red `✗` colouring
   268-  - Diff syntax highlighting (green/red for +/-)
   269-  - Consistent indentation for expanded detail
   267+  - `●` uses `$success` (green) for completed tools
   268+  - `●` uses `$error` (red) for failed tools
   269+  - `○` uses `$primary` (theme accent) for in-progress tools
   270+  - Diff syntax highlighting: `+` lines green, `-` lines red
   271+  - Consistent indentation (2-space indent under tool line)
   270   - Smooth scroll behaviour during streaming
   271   - Update TCSS for new widget classes
```

Diff output is capped — if the diff exceeds ~30 lines, show a summary like
`  … 12 more changed lines` at the end.

#### list_files

Path and glob are combined into a single path expression.

| Scenario | Format | Example |
|----------|--------|---------|
| Directory listing | `List <path> (<N> files)` | `● List src/archie/tools/ (12 files)` |
| With glob filter | `List <path/glob> (<N> files)` | `● List src/*.py (34 files)` |
| Truncated | `List <path> (<N> of <total> files)` | `● List . (200 of 1024 files)` |
| Empty directory | `List <path> (empty)` | `● List src/missing/ (empty)` |
| Glob no match | `List <path/glob> (no matches)` | `● List src/*.rs (no matches)` |
| Error | `List <path> — <error>` | `● List /root — Denied: outside allowed directories` |

#### search_files

The search pattern is displayed in a distinct colour (not quoted) to avoid conflicts
with searches that contain quote characters.

| Scenario | Format | Example |
|----------|--------|---------|
| Basic search | `Search <pattern> in <path> (<N> matches)` | `● Search def handler in src/ (5 matches)` |
| With glob | `Search <pattern> in <path/glob> (<N> matches)` | `● Search TODO in src/*.py (12 matches)` |
| No matches | `Search <pattern> in <path> (no matches)` | `● Search foobar in src/ (no matches)` |
| Error | `Search <pattern> — <error>` | `● Search [invalid — regex error` |

Note: `<pattern>` is rendered in a highlight/accent colour in the UI to distinguish
it from surrounding text without needing quotes.

#### shell

Commands are shown in full — no truncation. Multi-line commands render across
multiple lines. Exit 0 is omitted (implied by green `●`). Non-zero exit shows
the code in parentheses. On error (non-zero exit), also show the last 3 lines
of output indented below.

| Scenario | Format |
|----------|--------|
| Success (exit 0) | `Shell <command>` |
| Non-zero exit | `Shell <command> (exit <N>)` + last 3 lines of output |
| Multi-line command | Lines preserved as-is |
| Error (no command) | `Shell — <error>` |

Example (success):
```
● Shell uv run pytest
```

Example (failure with output):
```
● Shell uv run pytest (exit 1)
    FAILED tests/test_agent.py::test_interrupt - AssertionError
    FAILED tests/test_tools.py::test_validate - ValueError
    2 failed, 14 passed
```

Example (multi-line command):
```
● Shell cd /opt/archie/agent-kit && \
  uv run ak digest
```

#### code

| Scenario | Format | Example |
|----------|--------|---------|
| Outline | `Code outline <path> (<N> symbols)` | `● Code outline src/archie/agent.py (24 symbols)` |
| Search | `Code search <name> (<N> results)` | `● Code search validate_path (3 results)` |
| Search with lang | `Code search <name> [<lang>] (<N> results)` | `● Code search handler [python] (8 results)` |
| Overview | `Code overview <path>` | `● Code overview src/archie/` |
| Error | `Code <op> — <error>` | `● Code outline missing.py — not a file` |

Note: `<name>` is rendered in the same highlight colour as search_files patterns.

#### brain

| Scenario | Format | Example |
|----------|--------|---------|
| Read | `Brain read <path>` | `● Brain read projects/archie/README.md` |
| Write (create) | `Brain write <path>` | `● Brain write knowledge/terraform/tips.md` |
| Write (update) | `Brain update <path>` | `● Brain update people/simon.md` |
| Search | `Brain search <query> (<N> results)` | `● Brain search memory pipeline (4 results)` |
| Search with scope | `Brain search <query> in <scope> (<N> results)` | `● Brain search archie in projects/ (2 results)` |
| Commit | `Brain commit <message>` | `● Brain commit feat: add terraform tips` |
| Error | `Brain <op> — <error>` | `● Brain read — path not found` |

Note: `<query>` and `<message>` rendered in highlight colour.

#### recall

| Scenario | Format | Example |
|----------|--------|---------|
| Basic search | `Recall <query> (<N> results)` | `● Recall prompt caching (3 results)` |
| With project filter | `Recall <query> [<project>] (<N> results)` | `● Recall docker [archie] (5 results)` |
| No results | `Recall <query> (no results)` | `● Recall nonexistent thing (no results)` |
| Error | `Recall — <error>` | `● Recall — query is required` |

Note: `<query>` rendered in highlight colour.

#### retrieve_artifact

| Scenario | Format | Example |
|----------|--------|---------|
| Success | `Retrieve artifact <id_short>` | `● Retrieve artifact tooluse_ab12…` |
| Not found | `Retrieve artifact <id_short> — not found` | `● Retrieve artifact tooluse_zz99… — not found` |

#### self_debug

| Scenario | Format | Example |
|----------|--------|---------|
| Basic tail | `Debug log (<N> records)` | `● Debug log (50 records)` |
| Filtered | `Debug log [<level>] (<N> records)` | `● Debug log [ERROR] (3 records)` |
| With event filter | `Debug log [<event>] (<N> records)` | `● Debug log [tool_end] (12 records)` |
| Empty | `Debug log (empty)` | `● Debug log (empty)` |
| Error | `Debug log — <error>` | `● Debug log — file not found` |

## Requirements

### Iteration block widget

- MUST replace individual ToolCallMessage widgets with a single IterationBlock per LLM response
  - AC: If the model calls 5 tools, one block appears containing the summaries of all 5 (may be more than 5 lines due to multi-line shell/edit output)
- MUST stream tool summaries as they complete (pending → done transition)
  - AC: A pending tool shows `○ Shell uv run pytest` (only the dot in primary colour) then becomes `● Shell uv run pytest` (only the dot in green) when finished
- MUST colour only the `●`/`○` indicator, not the summary text
  - AC: Text remains default colour; only the dot changes colour to indicate state
- MUST show `●` in green for success, `●` in red for errors
  - AC: Errors also show additional context (last 3 lines for shell, error message for others)
- MUST show `○` in primary theme colour for in-progress tools
  - AC: Visually distinct from completed tools
- MUST be a static display block (like user/assistant messages)
  - AC: No collapse, no expand, no interactivity beyond focus for copy

### Tool-specific UI summaries

- MUST implement the summary format tables above for all tools
  - AC: Each tool produces the exact format specified including multi-line output for shell/edit/write
- MUST truncate paths to show only relative-to-cwd portion
  - AC: `/home/user/dev/archie-nextgen/src/archie/agent.py` → `src/archie/agent.py`
- MUST NOT truncate shell commands — render them in full including multi-line
  - AC: Long or multi-line commands display naturally
- MUST show last 3 lines of output for failed shell commands (non-zero exit)
  - AC: Indented below the summary line
- MUST render search patterns, queries, and commit messages in a highlight colour
  - AC: Visually distinct from surrounding text without needing quotes

### UI-only diffs for edit_file and write_file

- MUST generate a Kiro-style diff for edit_file calls
  - AC: Shows `added N lines, removed M lines at L<start> in <filename>` header
  - AC: Shows ±1 line context with line numbers, `+` lines green, `-` lines red
  - AC: Capped at ~30 diff lines; excess shows `… N more changed lines`
- MUST generate a diff for write_file when previous content is available (overwrite)
  - AC: Same Kiro-style format as edit_file
- MUST NOT send diffs back to the model (context efficiency)
  - AC: The ToolResultBlock content remains concise ("Edited: path", "Written: path")

### Agent event changes

- MUST add `ui_summary: str` field to `ToolStarted` event (the pending one-liner)
  - AC: Used by the iteration block to show what's running before it finishes
- MUST add `ui_summary: str` field to `ToolFinished` event (the completed summary)
  - AC: Used by the iteration block for display; distinct from the existing `summary` which is for logs
- MUST add `ui_detail: list[str] | None` field to `ToolFinished` event
  - AC: Populated for edit_file/write_file with diff lines, None for other tools
  - AC: For shell errors, populated with last 3 lines of output

### Conversation API changes

- MUST replace `mount_tool_pending` / `update_tool_result` with iteration-aware methods
  - AC: `begin_iteration()`, `add_tool_pending(ui_summary)`, `complete_tool(tool_use_id, ui_summary, is_error, ui_detail)`

### Backward compatibility

- MUST preserve keyboard navigation (focus on iteration blocks)
- MUST preserve copy-to-clipboard (get_copy_text returns all tool summaries as plain text)

## Implementation

### Milestone 1: Tool UI summary functions

Create `src/archie/tools/ui_summary.py` with two functions:

`format_tool_pending(name, params, cwd)` — produces the summary shown while the tool
is running. Uses only the input params. E.g. `Shell uv run pytest`, `Read src/agent.py`

`format_tool_complete(name, params, result, is_error, cwd)` — produces the completed
summary with result metadata. E.g. `Shell uv run pytest (exit 1)`, `Read src/agent.py (254 lines)`

Also: `format_tool_detail(name, params, result, is_error, cwd, pre_content)` — produces
multi-line detail (diff lines for edit/write, error output for shell). Returns `list[str] | None`.

Add `ui_summary` field to both `ToolStarted` and `ToolFinished` events. Add `ui_detail: list[str] | None` to `ToolFinished`.

**Files:** `src/archie/tools/ui_summary.py`, `src/archie/agent.py`
**Tests:** `tests/test_ui_summary.py` — verify every permutation from the tables above

### Milestone 2: Edit/write file diff generation

Before edit_file applies changes, snapshot the original content into a module-level dict
keyed by `current_tool_use_id`. Same for write_file when overwriting an existing file.
After the handler returns, the agent loop reads from this dict and passes it to
`format_tool_detail()` to generate the Kiro-style diff.

The diff format:
```
  added N lines, removed M lines at L<start> in <filename>
   <line_num>   <context line>
   <line_num>-  <removed line>
   <line_num>+  <added line>
   <line_num>   <context line>
```

**Key detail:** The handler return type does NOT change — handlers still return `str`.
The pre-content is communicated via a shared dict (same closure pattern as mtime_cache).
The agent loop clears the entry after reading it.

**Files:** `src/archie/tools/edit_file.py`, `src/archie/tools/write_file.py`, `src/archie/agent.py`
**Tests:** `tests/test_edit_file_tool.py`, `tests/test_write_file_tool.py` — verify diff output

### Milestone 3: IterationBlock widget

Replace `ToolCallMessage` with `IterationBlock` in conversation.py. The widget:
- Is a static container (like UserMessage/AssistantMessage) — no interactivity
- Contains tool summary lines as Static widgets with Rich markup for coloured dots
- Each tool summary is one or more lines (single for most tools, multi-line for shell/edit/write)
- Pending tools show `○` (primary), completed show `●` (green/red)
- `can_focus = True` for keyboard navigation and copy

**Files:** `src/archie/ui/conversation.py`
**Tests:** `tests/test_conversation.py` — verify pending→complete transitions

### Milestone 4: Wire up app.py

Update the event handler in app.py:
- On first `ToolStarted` after text: `begin_iteration()` + `add_tool_pending()`
- On subsequent `ToolStarted`: `add_tool_pending()`
- On `ToolFinished`: `complete_tool()` (replaces pending with completed + detail lines)
- On `TextDeltaEvent` after tools or `TurnComplete`: iteration implicitly ends

**Files:** `src/archie/ui/app.py`
**Tests:** `tests/test_app.py` — verify iteration lifecycle from event sequences

### Milestone 5: Styling and polish

- `●` uses `$success` (green) for completed tools — only the dot character
- `●` uses `$error` (red) for failed tools — only the dot character
- `○` uses `$primary` (theme accent) for in-progress tools — only the dot character
- Summary text remains default `$text` colour
- Search patterns/queries/messages rendered in `$accent` or `$secondary` colour
- Diff lines: `+` lines green, `-` lines red, context lines `$text-muted`
- Diff header (`added N, removed M`) in `$text-muted`
- Line numbers in `$text-muted`
- Shell error output indented 4 spaces, in `$text-muted`
- Update TCSS for new widget classes

**Files:** `src/archie/ui/conversation.py`, `src/archie/ui/archie.tcss`
