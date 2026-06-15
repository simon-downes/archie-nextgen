# Plan 019: UI Layout Refresh

## Objective

Simplify the app layout by removing the unused TabbedContent/TabPane layer, add a
project header, invert the background colours (surface default, black conversation),
and tighten up conversation block styling. Also add double-esc to clear input.

## Context

- Current layout uses `TabbedContent` → `TabPane("Session 1")` wrapping the conversation,
  status bar, and input. Multi-session tabs were never implemented.
- The background is currently black everywhere with `$surface` on individual elements.
  We want the inverse: `$surface` as the app default, black for the conversation scroll area.
- Message blocks have `border-bottom` separators and `border-left: thick transparent` which
  causes visual oddity on unfocused user messages (no visible left border until focus).
- "▶ You" is currently `[bold cyan]`, "● Archie" is unstyled.
- Esc currently interrupts a running turn. Double-esc when idle should clear the input.
- Project switching (new session in same window for different project) is out of scope
  but the header should accommodate it conceptually.

## Requirements

### Layout

- MUST remove `TabbedContent` and `TabPane` from the compose tree
  - AC: compose yields `Header`, `Conversation`, `StatusBar`, `MessageInput`, `Footer` directly
  - AC: no `TabbedContent` or `TabPane` imports remain in app.py

- MUST add a project header widget at the top
  - AC: displays the current project directory name (e.g. "archie-nextgen")
  - AC: height of 2-3 rows (visually "tall" — 1 row padding + 1 row text + optional padding)
  - AC: background is `$surface`, text colour uses the palette

### Backgrounds

- MUST set app/screen default background to `$surface`
  - AC: status bar, input box, header, and footer all appear on `$surface` without
    needing their own explicit `background: $surface`

- MUST set conversation area background to black (or `$panel`)
  - AC: the scrollable chat area is visually distinct (darker) from surrounding chrome

### Conversation blocks

- MUST remove `border-bottom` from all message block types
  - AC: no horizontal separator lines between messages

- MUST remove `padding` from message blocks (or reduce significantly)
  - AC: blocks are tighter — rely on margin for spacing

- MUST add `margin: 0 0 1 0` (bottom margin of 1) between conversation blocks
  - AC: 1 row of black space between messages (uses conversation's black background)

- MUST give `UserMessage` a permanent visible left border (same colour as focus)
  - AC: `border-left: thick $primary` always (not just on focus)
  - AC: removes the "jump" when user messages gain/lose focus

### Message colours

- MUST colour "▶ You" with bright magenta (`BRIGHT_MAGENTA` from `ui/colours.py`)
  - AC: user message header uses `[{BRIGHT_MAGENTA}]▶ You[/]` via `Text.from_markup()`

- MUST colour "● Archie" with the primary/accent colour (same as border highlights)
  - AC: assistant header uses the primary colour (dodger blue / `BRIGHT_BLUE` from palette)

### Input behaviour

- MUST clear input on double-esc when no turn is active
  - AC: first esc does nothing (or interrupts if turn active); second esc within 500ms
    clears the input text
  - AC: if a turn is active, esc still interrupts (existing behaviour unchanged)
  - AC: if input is already empty, double-esc does nothing extra

## Design

### Layout structure

Replace:
```python
with TabbedContent():
    with TabPane("Session 1"):
        yield Conversation(...)
        yield StatusBar(...)
        yield MessageInput(...)
yield Footer()
```

With:
```python
yield ProjectHeader(id="header")
yield Conversation(id="conversation")
yield StatusBar(id="status")
yield MessageInput(id="input")
yield Footer()
```

`ProjectHeader` is a simple `Static` subclass (or just a `Static` with an id) showing the
project name. Could be in a new file or inline in app.py — keep it minimal.

### Background approach

Set `Screen` background to `$surface` in TCSS. Remove explicit `background: $surface` from
individual widgets (they inherit). Set `Conversation { background: black; }` to make the
chat area dark.

### Double-esc implementation

Add a timestamp tracker in `MessageInput._on_key()` or in the app's `action_cancel()`.
On esc when no turn is active: if last esc was <500ms ago and input has text → clear it.
Otherwise record the timestamp. This avoids conflicting with the interrupt behaviour.

Best location: `action_cancel()` in app.py — it already handles esc. Add the double-tap
logic there (check `self._turn_active` first for interrupt, then check timing for clear).

### Message colours

`UserMessage` in `conversation.py` currently uses `f"[bold cyan]▶ You[/]\n{content}"`.
Change to `Text.from_markup(f"[{BRIGHT_MAGENTA}]▶ You[/]\n{content}")`.

`AssistantMessage` header `Static("● Archie", classes="header")` — change to
`Static(Text.from_markup(f"[{BRIGHT_BLUE}]● Archie[/]"), classes="header")`.

Import colours from `archie.ui.colours`.

### TCSS changes summary

```css
Screen {
    background: $surface;
}

Conversation {
    background: black;
}

/* Remove background from StatusBar, MessageInput, Footer — they inherit $surface */

/* All message blocks: remove border-bottom, remove padding, add margin-bottom */
UserMessage {
    margin: 0 0 1 0;
    padding: 0 2;
    border-left: thick $primary;
}

AssistantMessage {
    margin: 0 0 1 0;
    padding: 0 2;
    border-left: thick transparent;
}

/* ... same pattern for IterationBlock, ShellOutput, StreamingMessage, ErrorMessage */

/* Focus just highlights left border (unchanged for non-User blocks) */
AssistantMessage:focus { border-left: thick $primary; }
/* UserMessage:focus not needed — already has permanent border */
```

## Review Findings (Resolved)

1. **`ToolCallMessage` was dead CSS** — already removed from TCSS and replaced with
   `IterationBlock` (the actual widget class). No action needed by implementor.

2. **User content must be escaped in `Text.from_markup()`** — user text containing `[` will
   be interpreted as Rich markup. Use `Text.assemble()` instead:
   ```python
   Text.assemble(
       (f"▶ You\n", Style(color=BRIGHT_MAGENTA, bold=True)),
       content,  # plain string — not parsed as markup
   )
   ```
   Or use `markup.escape(content)` from Rich before interpolating.

3. **`action_cancel` needs an else branch** — currently does nothing when `_turn_active` is
   False. The double-esc logic goes in the else branch. Needs `import time` (or
   `time.monotonic()`). Clear input via `self.query_one("#input", MessageInput).clear()`.

4. **Use `black` not `$panel`** for conversation background — concrete value, no ambiguity.

5. **`ProjectHeader`** — define inline in app.py as a `Static` subclass (3 lines).

6. **Milestone 1 will look visually broken until milestone 2** — note for implementor:
   complete milestones 1+2 together before verifying appearance.

7. **DEFAULT_CSS on UserMessage** — it defines its own `padding: 1 2; margin: 1 0;
   background: $primary-background`. The TCSS overrides will take precedence (higher
   specificity), but the implementor should NOT edit DEFAULT_CSS — only TCSS.

8. **`ShellOutput` exists** (confirmed) — keep its CSS rules, apply same treatment.

## Milestones

### 1. Remove TabbedContent, add ProjectHeader

Approach:
- Create a minimal `ProjectHeader` widget — either a `Static` subclass with one line of
  compose, or just use `Static` directly with an id
- Style it in TCSS: `height: 3; padding: 1 2; background: $surface;`
- Remove `TabbedContent`/`TabPane` from app.py compose and imports
- Pass project name from `self.project_dir.name`

Tasks:
- Remove `TabbedContent`, `TabPane` imports from app.py
- Replace compose tree with flat: Header, Conversation, StatusBar, MessageInput, Footer
- Create `ProjectHeader` (inline Static or simple widget in a new file)
- Add TCSS for ProjectHeader
- Verify app launches with new layout

Deliverable: App renders with project header at top, no tab bar, same conversation below.

Verify: `uv run archie chat` shows project name header, conversation, status bar, input, footer.

### 2. Background inversion

Approach:
- Add `Screen { background: $surface; }` to TCSS
- Add `Conversation { background: black; }` (or `$panel`)
- Remove explicit `background: $surface` from StatusBar, MessageInput, Footer CSS rules
  (they inherit from Screen)
- Keep `background: $surface` on MessageInput if needed for the cursor-line fix

Tasks:
- Add Screen background rule
- Set Conversation background to black
- Remove redundant background declarations
- Test that all elements look correct (no unexpected colour)

Deliverable: Grey chrome surrounds a black conversation area.

Verify: Visual inspection — header/status/input/footer are grey, conversation is black.

### 3. Conversation block styling

Approach:
- All block types: remove `border-bottom`, change `padding: 1 2` → `padding: 0 2`,
  add `margin: 0 0 1 0`
- `UserMessage`: change `border-left: thick transparent` → `border-left: thick $primary`
  (permanently visible, same as current focus colour)
- Keep focus styling for non-User blocks (they go from transparent → $primary on focus)
- Remove `UserMessage:focus` rule (already has the border)

Tasks:
- Update all message block CSS rules in archie.tcss
- Remove `UserMessage:focus` border rule (redundant now)
- Verify focus still works on other block types

Deliverable: Messages have 1-row black gaps between them, no bottom borders, user messages
have permanent left accent bar.

Verify: Visual inspection — blocks are tighter, user messages have blue left bar always.

### 4. Message header colours

Approach:
- In `conversation.py`, update `UserMessage` to use `Text.from_markup()` with
  `BRIGHT_MAGENTA` for "▶ You"
- Update `AssistantMessage` header Static to use `Text.from_markup()` with `BRIGHT_BLUE`
  for "● Archie"
- Import colours from `archie.ui.colours`
- Same pattern for the streaming "● Archie ⟳" header

Tasks:
- Import `BRIGHT_MAGENTA`, `BRIGHT_BLUE` from `archie.ui.colours`
- Import `Text` from `rich.text`
- Update UserMessage content construction
- Update AssistantMessage/StreamingMessage header Static
- Verify colours render (must use `Text.from_markup()` not raw strings due to CSS override)

Deliverable: User messages show magenta "▶ You", assistant messages show blue "● Archie".

Verify: Visual inspection in running app.

### 5. Double-esc to clear input

Approach:
- Add `_last_esc_time: float = 0` to `ArchieApp`
- In `action_cancel()`: if turn is active → interrupt (unchanged). Otherwise, check if
  time since `_last_esc_time` < 0.5s AND input has text → clear input. Otherwise record
  current time as `_last_esc_time`.
- This means: first esc = no visible effect (records timestamp), second esc within 500ms =
  clears input.

Tasks:
- Add `_last_esc_time` attribute to ArchieApp.__init__
- Update `action_cancel()` with double-tap logic
- Test: type text, esc once (nothing), esc again fast (cleared), esc once wait 1s esc (nothing)

Deliverable: Double-tapping esc clears the input box when no turn is running.

Verify: Manual test — type text, double-esc clears it. Single esc does not. Esc during a
turn still interrupts.
