# Plan 022: Unified Theming

## Objective

Replace the ad-hoc `colours.py` module and split styling (DEFAULT_CSS + archie.tcss)
with a single `theme.py` module that defines all colour values as constants, plus a
Textual `Theme` instance that references them. Both TCSS variables and Rich markup
use the same values from the same file, eliminating duplication.

## Context

- Currently colours live in `src/archie/ui/colours.py` as hex constants (iTerm2 Regular
  preset). Rich markup uses these directly via `Text.from_markup(f"[{BRIGHT_BLUE}]...")`.
- TCSS uses Textual's built-in theme variables (`$primary`, `$surface`) which are defined
  by Textual's default dark theme — not by us.
- There's no connection between the two — `$primary` is `#0178D4` (Textual default) while
  our `BRIGHT_BLUE` is `#6871ff` (iTerm2 palette). They don't match.
- `DEFAULT_CSS` on widgets duplicates/conflicts with `archie.tcss`. We want to slim
  `archie.tcss` to layout-only and derive all colours from the theme.

## Requirements

- MUST define all colour values once as module-level constants in `src/archie/ui/theme.py`
  - AC: single source of truth — no hex literals anywhere else in the codebase
  - AC: constants are UPPER_SNAKE_CASE
  - AC: accessed as `theme.PRIMARY`, `theme.MUTED`, etc. via `from archie.ui import theme`

- MUST create a Textual `Theme` instance that references the constants
  - AC: instance named `THEME` in `src/archie/ui/theme.py`
  - AC: uses `textual.theme.Theme` directly (no subclass)
  - AC: custom colours via `variables={}` dict (makes them available as `$name` in TCSS)
  - AC: registered on the app via `self.register_theme(theme.THEME)` + `self.theme = "archie"`

- MUST use Textual's semantic colour slots for standard meanings
  - AC: `primary` — accent colour (focus borders, model name, assistant header)
  - AC: `secondary` — secondary accent (user messages, git branch)
  - AC: `error` — error/danger states (context >85%, error messages)
  - AC: `warning` — warning states (context 60-85%)
  - AC: `success` — success states (green indicators)
  - AC: `surface` — elevated chrome (status bar, input, header, footer)

- MUST add custom variables only for values that don't map to Textual builtins
  - AC: custom variables use generic semantic names, not element-specific names
  - AC: use Textual's `lighten`/`darken` modifiers where possible instead of new slots

- MUST update all Rich markup to use `theme.<CONSTANT>`
  - AC: no hex literals or `colours.py` imports remain in widget code
  - AC: `colours.py` is deleted

- MUST reorganise `archie.tcss` and widget `DEFAULT_CSS`
  - AC: `DEFAULT_CSS` owns structural layout (height, margin, padding, overflow, width)
  - AC: `archie.tcss` owns visual styling (backgrounds, border colours, `:focus`, text colour/style)
  - AC: all colour values in TCSS use theme variables (`$primary`, `$surface`, etc.)
  - AC: removing `archie.tcss` would leave layout intact (just unstyled)

## Design

### Theme module

```python
# src/archie/ui/theme.py
"""Unified colour theme for the Archie TUI.

All colour values are defined once as module constants. The Textual Theme
references these constants, making them available as $variables in TCSS.
Rich markup imports the constants via `from archie.ui import theme` and
accesses them as `theme.PRIMARY`, `theme.MUTED`, etc.
"""

from textual.theme import Theme

# --- Semantic colours (Textual builtin slots) ---

PRIMARY = "#6871ff"          # accent — focus, model name, assistant header
SECONDARY = "#ff76ff"        # user messages, git branch
WARNING = "#fefb67"          # context 60-85%
ERROR = "#ff6d67"            # context >85%, errors
SUCCESS = "#00c200"          # success indicators
SURFACE = "#1e1e1e"          # chrome background

# --- Custom colours (available as $variables in TCSS) ---

MUTED = "#676767"            # de-emphasised text (captions, separators)
BRIGHT = "#feffff"           # emphasised values (context %)
POSITIVE = "#00c200"         # positive values (input tokens)
POSITIVE_BRIGHT = "#5ff967"  # bright positive (output tokens)
COST = "#c7c400"             # monetary values

# --- Textual Theme instance ---

THEME = Theme(
    name="archie",
    primary=PRIMARY,
    secondary=SECONDARY,
    warning=WARNING,
    error=ERROR,
    success=SUCCESS,
    surface=SURFACE,
    dark=True,
    variables={
        "muted": MUTED,
        "bright": BRIGHT,
        "positive": POSITIVE,
        "positive-bright": POSITIVE_BRIGHT,
        "cost": COST,
    },
)
```

### Widget markup pattern

```python
from archie.ui import theme

# Rich markup references constants directly
Text.from_markup(
    f" [{theme.PRIMARY}]{self.model_name}[/]"
    f" ⎇  [{theme.SECONDARY}]{self.git_branch}[/]"
    f" │ In: [{theme.POSITIVE}]{in_val}[/]"
    f"  Out: [{theme.POSITIVE_BRIGHT}]{out_display}[/]"
    f" │ Ctx: [{ctx_colour}]{self.context_pct:.0f}%[/]"
    f" │ [{theme.COST}]{self.pricing_label}[/]"
)
```

### App registration

```python
from archie.ui import theme

class ArchieApp(App):
    def __init__(self, ...):
        super().__init__()
        self.register_theme(theme.THEME)
        self.theme = "archie"
```

### Colour mapping

| Current usage | Access method | Notes |
|---------------|--------------|-------|
| Model name | `theme.PRIMARY` | |
| ▶ You header | `theme.SECONDARY` | |
| ● Archie header | `theme.PRIMARY` | |
| Git branch | `theme.SECONDARY` | |
| Input tokens | `theme.POSITIVE` | |
| Output tokens | `theme.POSITIVE_BRIGHT` | |
| Cost/pricing | `theme.COST` | |
| Context % (normal) | `theme.BRIGHT` | |
| Context % (warning) | `theme.WARNING` | |
| Context % (danger) | `theme.ERROR` | |
| Muted text/captions | `theme.MUTED` | |
| Input box border focus | `$primary` in TCSS | |
| UserMessage left border | `$primary` in TCSS | |
| Screen background | `$surface` in TCSS | |

### TCSS slimming

After this change, `archie.tcss` contains only:
- Layout: margin, padding, height, overflow, grid/flex
- Border styles referencing `$primary`, `$surface-lighten-2`, etc.
- No hex colour values anywhere

## Milestones

### 1. Create theme module and register on app

Tasks:
- Create `src/archie/ui/theme.py` with constants and `THEME` instance
- Register theme in `ArchieApp.__init__`
- Verify: input box border on focus matches `#6871ff` not Textual's default `#0178D4`

Deliverable: App uses custom theme; `$primary` in TCSS resolves to our blue.

### 2. Replace colours.py references with theme module

Tasks:
- Update `src/archie/ui/status.py` — `from archie.ui import theme`, use `theme.PRIMARY` etc.
- Update `src/archie/ui/conversation.py` — same pattern
- Search for any other `from archie.ui.colours import` and update
- Delete `src/archie/ui/colours.py`

Deliverable: All Rich markup uses `theme.<CONSTANT>`; `colours.py` deleted.

Verify: `uv run ruff check src/` clean. App renders with correct colours.

### 3. Reorganise DEFAULT_CSS and archie.tcss

Principle: **DEFAULT_CSS owns structure, archie.tcss owns style.**

- `DEFAULT_CSS` on each widget — properties that make it function correctly:
  height, min-height, max-height, width, margin, padding, display, overflow, text-align
- `archie.tcss` — visual properties that can be tweaked without breaking layout:
  background, color, border colours, text-style, `:focus` states, theme variable refs

#### Migration map

| Current location → target | Rules |
|---------------------------|-------|
| tcss → DEFAULT_CSS (Screen) | `overflow: hidden` |
| tcss → DEFAULT_CSS (ProjectHeader) | `height: 3; padding: 1 2` |
| tcss → DEFAULT_CSS (Conversation) | `height: 1fr; padding: 0` |
| tcss → DEFAULT_CSS (UserMessage) | `margin: 0 0 1 0; padding: 1 2` |
| tcss → DEFAULT_CSS (AssistantMessage) | `margin: 0 0 1 0; padding: 0 2` |
| tcss → DEFAULT_CSS (IterationBlock) | `margin: 0 0 1 0; padding: 0 2` |
| tcss → DEFAULT_CSS (IterationBlock .tool-body) | `display: none; height: auto; margin: 1 0 0 2` |
| tcss → DEFAULT_CSS (ShellOutput) | `margin: 0 0 1 0; padding: 0 2` |
| tcss → DEFAULT_CSS (StreamingMessage) | `margin: 0 0 1 0; padding: 0 2` |
| tcss → DEFAULT_CSS (ErrorMessage) | `margin: 0 0 1 0; padding: 1 2` |
| tcss → DEFAULT_CSS (StatusBar) | `height: 3; padding: 1` |
| tcss → DEFAULT_CSS (StatusBar #status-left) | `width: 1fr; padding: 0` |
| tcss → DEFAULT_CSS (StatusBar #status-right) | `width: auto; padding: 0; text-align: right` |
| tcss → DEFAULT_CSS (MessageInput) | `height: auto; max-height: 8; min-height: 1; margin: 0; padding: 0 1` |
| tcss → DEFAULT_CSS (Footer) | `height: 2; padding: 1 0 0 0` |
| tcss → DEFAULT_CSS (Throbber) | `height: 1; margin: 0; padding: 0` |
| stays in tcss | `Screen { background }` |
| stays in tcss | `ProjectHeader .project-name { color; text-style }` |
| stays in tcss | `Conversation { background }` |
| stays in tcss | `UserMessage { border-left: thick $primary }` |
| stays in tcss | `*Message/Block { border-left: thick transparent }` |
| stays in tcss | All `:focus { border-left: thick $primary }` rules |
| stays in tcss | `StatusBar { color }`, child `color: auto` |
| stays in tcss | `MessageInput { border }`, `:focus { border }` |
| stays in tcss | `MessageInput .text-area--cursor-line { background }` |
| stays in tcss | `IterationBlock .tool-body.expanded { display: block }` |
| stays in tcss | `ErrorMessage { border-left: thick $error-darken-3 }` |

Tasks:
- Move structural rules into each widget's `DEFAULT_CSS`
- Strip moved rules from `archie.tcss`, leaving only visual/theme rules
- Replace any remaining hex values in tcss with `$primary`, `$surface`, etc.

Deliverable: `archie.tcss` contains only visual styling; structural layout lives in widgets.

Verify: Visual inspection — identical appearance. Lint and tests pass.
