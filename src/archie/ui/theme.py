"""Unified colour theme for the Archie TUI.

All colour values are defined once as module constants. The Textual Theme
references these constants, making them available as $variables in TCSS.
Rich markup imports the constants via `from archie.ui import theme` and
accesses them as `theme.PRIMARY`, `theme.MUTED`, etc.
"""

from textual.theme import Theme

# --- Semantic colours (Textual builtin slots) ---

PRIMARY = "#6871ff"  # accent — focus, model name, assistant header
SECONDARY = "#ff76ff"  # user messages, git branch
WARNING = "#fefb67"  # context 60-85%
ERROR = "#ff6d67"  # context >85%, errors
SUCCESS = "#00c200"  # success indicators
SURFACE = "#1e1e1e"  # chrome background

# --- Custom colours (available as $variables in TCSS) ---

MUTED = "#676767"  # de-emphasised text (captions, separators)
BRIGHT = "#feffff"  # emphasised values (context %)
POSITIVE = "#00c200"  # positive values (input tokens)
POSITIVE_BRIGHT = "#5ff967"  # bright positive (output tokens)
COST = "#c7c400"  # monetary values

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
