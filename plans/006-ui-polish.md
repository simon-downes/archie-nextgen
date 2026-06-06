# Plan 006: UI Polish

## Objective

Bring the Textual TUI to daily-driver quality. A small set of features done well, consistent theming, and a polished feel. Inspired by Toad's UI approach.

## Context

- Phases 1-4 complete (core chat, tools, sandbox, shell)
- write_file/edit_file just added
- Current UI is functional but rough: inconsistent styling, full-screen streaming, no visual feedback during "thinking" phase, no command palette
- Toad reference implementation available at `_research/toad/`
- Textual has a built-in `CommandPalette` triggered by `ctrl+p` with `Provider` classes — we can leverage this directly
- Textual has `notify()` for toast notifications (built-in)

## Requirements

### Colour Theme + Visual Consistency

- MUST have a consistent colour palette across all UI elements
  - AC: Status bar, messages, tool calls, input all use a cohesive dark theme
- MUST improve visual distinction between status bar elements
  - AC: Model name, tokens, context, cost are visually separated (not just `│`)
- MUST remove visual "blank line" appearance between message blocks
  - AC: Tighter spacing, messages flow naturally without excessive margin

### Status Bar Improvements

- MUST show project name in status bar
  - AC: Derived from project directory name (e.g. "archie-nextgen")
- MUST show current git branch in status bar
  - AC: Run `git rev-parse --abbrev-ref HEAD` on startup and after shell commands
- MUST retain: model name, token counts, context %, cost

### Command Palette (Ctrl+P)

- MUST use Textual's built-in CommandPalette with custom Provider
  - AC: Ctrl+P opens searchable command list
- MUST include commands: "Change Model", "Quit", "New Session"
  - AC: "Change Model" presents available models and switches on selection
- MUST support fuzzy search of commands
  - AC: Typing "mod" finds "Change Model"

### Thinking Indicator

- MUST show a visual indicator while waiting for LLM response
  - AC: Animated throbber/spinner appears between user message and response
  - AC: Disappears when first text chunk arrives or tool call starts
- SHOULD be subtle (not distracting), similar to Toad's rainbow gradient bar

### Streaming Improvements

- MUST NOT clear the screen or take full width during streaming
  - AC: Streaming text appears inline in the conversation as a regular block
  - AC: Previous messages remain visible above
  - AC: Conversation auto-scrolls to follow streaming text

### Clipboard + Notifications

- MUST allow Ctrl+C to copy the full text content of the currently focused block
  - AC: Focus a message block (arrow keys / click), press Ctrl+C, full text is in clipboard
  - AC: Toast notification "Copied to clipboard" appears briefly
  - AC: Works for user messages, assistant messages, tool results, shell output
- NOTE: Textual intercepts right-click for selection which conflicts with terminal context menus.
  Block-level copy via Ctrl+C sidesteps this entirely.

### Collapsible Tool Blocks

- SHOULD show tool calls in collapsed state by default (header only)
  - AC: Shows "🔧 tool_name(args)" with ▶ expand indicator
  - AC: Click or keyboard toggle expands to show full result
  - AC: Failed tools auto-expand (error is always relevant)

## Design

### Theme (TCSS)

Use Textual's theme variables for consistency. Dark theme with:
- `$background` — base
- `$surface` — cards/blocks (messages)
- `$primary` — accent (user message highlights, active borders)
- `$text-muted` — secondary info (tool output, status bar sections)
- `$success` — model responses
- `$error` — errors

Key changes:
- Reduce `margin` on message blocks from `1 0` to `0 0` with a thin border-bottom separator
- Status bar: use background sections/pills for distinct elements
- Input: subtle border only when focused

### Status Bar Layout

```
 archie-nextgen (main) │ Claude Sonnet 4.6 │ 1.5K↑ 214↓ │ ctx: 3% │ $0.013
```

Project name + branch on the left, model + metrics on the right.

### Command Palette

Textual's built-in `CommandPalette` is triggered by `ctrl+p`. We provide a custom `Provider`:

```python
from textual.command import Provider, Hits, Hit

class ArchieCommands(Provider):
    async def search(self, query: str) -> Hits:
        commands = [
            ("Change Model", "Switch to a different LLM model"),
            ("New Session", "Start a fresh conversation"),
            ("Quit", "Exit Archie"),
        ]
        for name, help_text in commands:
            if self.matcher(query).match(name) > 0 or not query:
                yield Hit(score, match_display, callable, help=help_text)
```

Register on the App: `COMMANDS = {ArchieCommands}`.

### Thinking Indicator

A single-line widget that shows an animated gradient bar (like Toad's `Throbber`). Mounted in the conversation when the engine starts, removed when first content arrives.

### Block Focus + Navigation

All conversation blocks (`UserMessage`, `AssistantMessage`, `ToolCallMessage`, `ShellOutput`) get `can_focus = True`. Textual handles:
- Click to focus
- Up/Down arrows to navigate between blocks
- Escape returns focus to input

Visual: subtle left border on `:focus` (Toad's "cursor bar" effect without the custom overlay):
```css
.block:focus {
    border-left: thick $primary;
}
```

This enables Ctrl+C block copy — the focused block's content is what gets copied.

### Collapsible Tool Blocks

Rewrite `ToolCallMessage` to have:
- A clickable header (always visible): `▶ 🔧 read_file(path="src/app.py") ✔`
- A collapsible body (hidden by default): full args + result content
- Toggle via click or `x` key when focused
- Auto-expand on error (failed tools should always show the error)

### File Structure Changes

```
src/archie/ui/
├── app.py              # MODIFIED — theme, command palette, thinking indicator
├── conversation.py     # MODIFIED — collapsible tools, tighter spacing, inline streaming
├── input.py            # MODIFIED — subtle styling tweaks
├── status.py           # MODIFIED — project name, git branch, layout
├── commands.py         # NEW — CommandPalette provider
├── throbber.py         # NEW — thinking indicator widget
└── archie.tcss         # NEW — external stylesheet (cleaner than inline CSS)
```

## Review Resolutions

1. **Change Model mid-session**: Keep it simple — model switch starts a new session implicitly. The palette command calls `action_new_session()` then swaps the model. No mixed-model session state.

2. **Clipboard selection**: Textual 8.x doesn't have broad text selection on Static widgets. **Defer this to later** — mark as "future" not "this phase". Not worth building a custom selectable widget right now.

3. **Command palette `discover()` method**: Use both `discover()` (shows all commands when palette opens) and `search()` (filters as user types). The plan example was simplified.

4. **Git branch — host detection**: Run `git rev-parse` once at startup on the host (not in container). Don't update after shell commands — branch changes are rare and the user can just restart the session. Simple.

5. **Throbber removal on error**: Remove throbber on ANY event from the engine — `StreamChunk`, `ToolStart`, or `StreamComplete` (including interrupted/error). Covers all cases.

6. **Tool call display during execution**: Show collapsed header immediately on `ToolStart` with ⏳ indicator. Update to ✔/✗ when `ToolResult` arrives.

7. **Streaming is already inline**: The plan's context section was wrong — streaming IS already inline. The actual issue is probably auto-scroll behaviour or the reactive text replacement being jarring. Remove "fix streaming" as a task; focus on the thinking indicator and smooth transitions instead.

8. **CSS migration approach**: Keep `DEFAULT_CSS` on widgets for base styling (Textual's intended pattern). Use `archie.tcss` only for app-level overrides and composition. Don't migrate everything — just add a sheet for theme consistency.

9. **Status bar layout**: Use a `Horizontal` container with two `Static` children (left/right). Simple and works with Textual's layout system.

## Milestones

### Milestone 1: Theme + Layout Foundation

- Create `archie.tcss` external stylesheet
- Move all inline CSS from widget `DEFAULT_CSS` into the external sheet
- Establish colour theme with consistent variables
- Tighten message spacing (remove excess margin/blank lines)
- Verify: app looks cohesive, no visual regressions

### Milestone 2: Status Bar + Git Branch

- Add project name (from `self.project_dir.name`)
- Add git branch detection (subprocess: `git rev-parse --abbrev-ref HEAD`)
- Rework layout: left-aligned project info, right-aligned metrics
- Visual pills/sections for distinct elements

### Milestone 3: Thinking Indicator

- Create `throbber.py` — animated gradient bar widget (inspired by Toad)
- Mount throbber in conversation when engine worker starts
- Remove throbber on first StreamChunk, ToolStart, or StreamComplete (any event)
- Verify: user sees smooth flow from input → thinking bar → response appearing

### Milestone 4: Collapsible Tool Blocks

- Rewrite `ToolCallMessage` with header/body structure
- Show header immediately on ToolStart (with ⏳ pending indicator)
- Update status on ToolResult (✔ success, ✗ failed)
- Collapsed by default (header only), expand on click/key
- Auto-expand on error
- Verify: tool-heavy sessions are much less noisy

### Milestone 5: Command Palette + Model Switching

- Create `commands.py` with `ArchieCommands` Provider
- Implement `discover()` (shows all commands) and `search()` (filters)
- Register on ArchieApp via `COMMANDS` class var
- Implement "Change Model" (new session with different model)
- Implement "New Session" and "Quit" (delegate to existing actions)
- Verify: Ctrl+P opens palette, search works, commands execute

### Milestone 6: Block Copy (Ctrl+C)

- Override Ctrl+C to copy the focused block's text content to clipboard
- Each message widget exposes a `get_copy_text()` method returning its plain text
- Show toast notification via `self.notify("Copied to clipboard")`
- Verify: navigate to block → Ctrl+C → paste elsewhere confirms content

### Milestone 7: Review + Polish Pass

- Run review workflow
- Fix any findings
- Ensure all tests pass
- Commit
