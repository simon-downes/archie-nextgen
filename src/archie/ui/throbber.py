"""Animated gradient throbber widget — thinking indicator.

Shows a single-line rainbow gradient bar that scrolls horizontally,
indicating the engine is working (waiting for LLM response or processing).

Inspired by Toad's throbber widget. Uses Textual's Visual protocol for
efficient rendering — bypasses Rich entirely and builds Strip objects
directly from colour gradients.

Lifecycle:
- Mounted in the Conversation when the engine worker starts
- Removed when the first event arrives from the engine (StreamChunk,
  ToolStart, or StreamComplete) — any event means work has begun
- The animation runs at ~15fps via auto_refresh
"""

from functools import lru_cache
from time import monotonic

from rich.segment import Segment
from rich.style import Style as RichStyle
from textual.color import Color, Gradient
from textual.css.styles import RulesMap
from textual.strip import Strip
from textual.style import Style
from textual.visual import RenderOptions, Visual
from textual.widget import Widget

# Rainbow gradient colours — wraps around (first == last) for seamless scrolling
_COLORS = [
    "#881177",
    "#aa3355",
    "#cc6666",
    "#ee9944",
    "#eedd00",
    "#99dd55",
    "#44dd88",
    "#22ccbb",
    "#00bbcc",
    "#0099cc",
    "#3366bb",
    "#663399",
    "#881177",
]


class _ThrobberVisual(Visual):
    """Textual Visual that renders a scrolling gradient bar.

    A Visual is Textual's equivalent of a Rich renderable but with
    transparency support and direct Strip output. We use it here because
    it gives us per-cell colour control without Rich markup overhead.
    """

    # Pre-computed gradient from the colour list — shared across all instances
    _gradient = Gradient.from_colors(*[Color.parse(c) for c in _COLORS])

    def __init__(self, character: str = "━") -> None:
        self._character = character

    def render_strips(
        self, width: int, height: int | None, style: Style, options: RenderOptions
    ) -> list[Strip]:
        """Render one strip (single line) with time-based offset for animation."""
        segments = _make_segments(self._gradient, self._character, style, width)
        # Scroll offset based on wall-clock time — monotonic() gives smooth animation
        offset = width - int((monotonic() % 1.0) * width)
        return [Strip(segments[offset : offset + width], cell_length=width)]

    def get_optimal_width(self, rules: RulesMap, container_width: int) -> int:
        return container_width

    def get_height(self, rules: RulesMap, width: int) -> int:
        return 1


@lru_cache(maxsize=8)
def _make_segments(gradient: Gradient, character: str, style: Style, width: int) -> list[Segment]:
    """Build double-width segment list for smooth wrapping.

    Module-level function (not a method) to avoid B019 lru_cache memory leak.
    We generate 2× the width so we can slice a window from any offset
    without needing modulo per-character.
    """
    background = style.rich_style.bgcolor
    return [
        Segment(
            character,
            RichStyle.from_color(gradient.get_rich_color((i / width) % 1), background),
        )
        for i in range(width * 2)
    ]


class Throbber(Widget):
    """Single-line animated gradient bar — the "thinking" indicator.

    Mount this in the conversation when waiting for the engine.
    Remove it when the first response event arrives.
    """

    def on_mount(self) -> None:
        # ~15fps refresh rate — enough for smooth animation without burning CPU
        self.auto_refresh = 1 / 15

    def render(self) -> _ThrobberVisual:
        return _ThrobberVisual()
