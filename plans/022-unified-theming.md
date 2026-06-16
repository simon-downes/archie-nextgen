# Plan 022: Unified Theming

## Objective

Replace the ad-hoc `colours.py` module and split styling (DEFAULT_CSS + archie.tcss)
with a single `ArchieTheme` subclass that defines all colour values. Both TCSS variables
and Rich markup reference the same theme instance, eliminating duplication.

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

- MUST define a single theme instance with all colour values
  - AC: theme instance named `theme` in `src/archie/ui/theme.py`
  - AC: uses `textual.theme.Theme` directly (no subclass needed)
  - AC: custom colours via `variables={}` dict (makes them available as `$name` in TCSS)
  - AC: module-level constants for custom colours (for Rich markup access)
  - AC: registered on the app via `self.register_theme(theme)` + `self.theme = "archie"`

- MUST use Textual's semantic colour slots for standard meanings
  - AC: `primary` — accent colour (focus borders, model name, assistant header)
  - AC: `secondary` — secondary accent (user messages, git branch)
  - AC: `error` — error/danger states (context >85%, error messages)
  - AC: `warning` — warning states (context 60-85%)
  - AC: `success` — success states (green indicators)
  - AC: `surface` — elevated chrome (status bar, input, header, footer)

- MUST add custom slots only for values that don't map to Textual builtins
  - AC: custom slots use generic semantic names, not element-specific names
  - AC: use Textual's `lighten`/`darken` modifiers where possible instead of new slots

- MUST update all Rich markup to reference `theme.primary`, `theme.secondary`, etc.
  - AC: no hex literals or `colours.py` imports remain in widget code
  - AC: `colours.py` is deleted

- MUST update `archie.tcss` to only contain layout rules
  - AC: no colour values in TCSS (all via `$primary`, `$surface`, etc. from theme)
  - AC: `DEFAULT_CSS` on widgets contains only structural styles (height, overflow)
  - AC: visual styles (colours, borders) reference theme variables via TCSS

- SHOULD keep widget `DEFAULT_CSS` for structural requirements only
  - AC: padding, margin, height, overflow — things that make the widget function
  - AC: no colour values in DEFAULT_CSS

## Design

### Theme definition

```python
# src/archie/ui/theme.py

from textual.theme import Theme

# Custom CSS variables are injected via the `variables` dict — these become
# available as $muted, $bright, etc. in TCSS. Subclass fields do NOT become
# CSS variables, so we use the variables dict for custom colours.

theme = Theme(
    name="archie",
    primary="#6871ff",        # accent — focus, model name, assistant header
    secondary="#ff76ff",      # secondary — user messages, git branch
    warning="#fefb67",        # context 60-85%
    error="#ff6d67",          # context >85%, errors
    success="#00c200",        # success indicators
    surface="#1e1e1e",        # chrome background
    dark=True,
    variables={
        "muted": "#676767",          # de-emphasised text (captions, separators)
        "bright": "#feffff",         # emphasised values (context %)
        "positive": "#00c200",       # positive values (input tokens)
        "positive-bright": "#5ff967",  # bright positive (output tokens)
        "cost": "#c7c400",           # monetary values
    },
)
```

Note: `theme.primary` etc. are accessible directly as attributes for Rich markup.
Custom variables from the `variables` dict are NOT accessible as attributes — access
them via a helper or store them as module-level constants alongside the theme:

```python
# For Rich markup access to custom vars
MUTED = "#676767"
BRIGHT = "#feffff"
POSITIVE = "#00c200"
POSITIVE_BRIGHT = "#5ff967"
COST = "#c7c400"
```

These duplicate the values in `variables` but give clean attribute access for
`Text.from_markup()`. The TCSS `$muted` and the Python `MUTED` constant are the
same value, defined in the same file.

### Colour mapping

| Current usage | Theme slot | Notes |
|---------------|-----------|-------|
| Model name | `theme.primary` | |
| ▶ You header | `theme.secondary` | |
| ● Archie header | `theme.primary` | |
| Git branch | `theme.secondary` | |
| Input tokens | `theme.positive` | Custom slot |
| Output tokens | `theme.positive_bright` | Custom slot |
| Cost/pricing | `theme.cost` | Custom slot |
| Context % (normal) | `theme.bright` | Custom slot |
| Context % (warning) | `theme.warning` | Textual builtin |
| Context % (danger) | `theme.error` | Textual builtin |
| Muted text/captions | `theme.muted` | Custom slot (or `$text-muted` in TCSS) |
| Input box border focus | `$primary` | Via TCSS |
| UserMessage left border | `$primary` | Via TCSS |
| Focus left border | `$primary` | Via TCSS |
| Screen background | `$surface` | Via TCSS |
| Conversation background | `black` | Hardcoded (not a theme variable — it's structural) |

### Widget markup pattern

```python
from archie.ui.theme import theme, MUTED, POSITIVE, POSITIVE_BRIGHT, BRIGHT, COST

# Status bar — builtins via theme.*, custom via constants
Text.from_markup(
    f" [{theme.primary}]{self.model_name}[/]"
    f" ⎇  [{theme.secondary}]{self.git_branch}[/]"
    f" │ In: [{POSITIVE}]{in_val}[/]"
    f"  Out: [{POSITIVE_BRIGHT}]{out_display}[/]"
    f" │ Ctx: [{ctx_colour}]{self.context_pct:.0f}%[/]"
    f" │ [{COST}]{self.pricing_label}[/]"
)
```

### TCSS slimming

After this change, `archie.tcss` contains only:
- Screen/Conversation backgrounds
- Widget heights, padding, margin
- Border styles (referencing `$primary`, `$surface-lighten-2`)
- No hex colour values

## Milestones

### 1. Create theme module and register on app

Approach:
- Create `src/archie/ui/theme.py` with `ArchieTheme` subclass and `theme` instance
- Register theme in `ArchieApp` via `self.register_theme(theme)` and set `self.theme = "archie"`
- ⚠️ `register_theme` must be called before `compose()` — do it in `__init__` or `on_mount`
- Verify TCSS variables (`$primary`, `$surface`) resolve to our values after registration

Tasks:
- Create `src/archie/ui/theme.py`
- Register theme in `ArchieApp.__init__` or class-level `THEME = "archie"`
- Verify: input box border on focus matches `theme.primary` colour visually

Deliverable: App uses custom theme; `$primary` in TCSS resolves to our blue.

Verify: Run app — focus the input box, confirm border is `#6871ff` not Textual's default `#0178D4`.

### 2. Replace colours.py references with theme

Approach:
- Update `status.py` to import `theme` from `archie.ui.theme` instead of constants from `colours.py`
- Update `conversation.py` similarly — `theme.primary` for Archie, `theme.secondary` for You
- Update any other files referencing `colours.py`
- Delete `colours.py`
- ⚠️ Verify `Text.from_markup(f"[{theme.primary}]...")` works — theme attributes are strings

Tasks:
- Update `src/archie/ui/status.py` — replace all colour constant references
- Update `src/archie/ui/conversation.py` — replace BRIGHT_BLUE/BRIGHT_MAGENTA
- Search for any other `from archie.ui.colours import` and update
- Delete `src/archie/ui/colours.py`
- Run lint to confirm no dead imports

Deliverable: All Rich markup uses `theme.*` attributes; `colours.py` deleted.

Verify: `uv run ruff check src/` clean. App renders with correct colours visually.

### 3. Slim archie.tcss and DEFAULT_CSS

Approach:
- Remove all hex colour values from `archie.tcss` — replace with `$primary`, `$surface`, etc.
- Remove colour-related rules from widget `DEFAULT_CSS` (move to TCSS if needed)
- Keep structural styles in `DEFAULT_CSS` (height, overflow, can_focus)
- Keep layout in `archie.tcss` (margin, padding, border styles, grid/flex)
- ⚠️ Test that `$surface-lighten-2` still works for subtle borders (Textual derives it
  from our `surface` value)

Tasks:
- Audit `archie.tcss` for any hardcoded colours — replace with variables
- Audit widget `DEFAULT_CSS` — move colour rules to TCSS, keep structural only
- Verify border colours, backgrounds all render correctly

Deliverable: No colour values in TCSS or DEFAULT_CSS; all from theme variables.

Verify: Visual inspection — all colours match previous appearance. Lint clean.
