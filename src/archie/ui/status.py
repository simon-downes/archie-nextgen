"""Status bar widget showing model info, token counts, and cost.

Displays a single line at the bottom of the session tab with:
- Model name (so you know which model you're talking to)
- Last turn's tokens (↑ input sent, ↓ output received)
- Cumulative tokens across the session
- Context window usage as a percentage
- Running cost in USD

Uses Textual's reactive system: when any property changes, render()
is called automatically to update the display. The app pushes new
values after each completed turn.
"""

from textual.reactive import reactive
from textual.widgets import Static


class StatusBar(Static):
    """Single-line status display with reactive properties.

    Textual reactives: declaring a class variable with reactive() makes it
    a watched property. When any reactive changes, Textual calls render()
    to refresh the widget's content. No manual refresh needed.
    """

    model_name: reactive[str] = reactive("—")
    turn_input: reactive[int] = reactive(0)
    turn_output: reactive[int] = reactive(0)
    total_input: reactive[int] = reactive(0)
    total_output: reactive[int] = reactive(0)
    context_pct: reactive[float] = reactive(0.0)
    cost: reactive[float] = reactive(0.0)
    warning: reactive[bool] = reactive(False)

    def render(self) -> str:
        """Build the status bar text. Called automatically when reactives change.

        Format: model │ last: Xk↑ Yk↓ │ total: Xk↑ Yk↓ │ ctx: N% │ $0.XXX
        ↑ = tokens sent to the model (input)
        ↓ = tokens received from the model (output)
        """
        # Highlight context percentage in red when approaching the limit
        ctx_style = "bold red" if self.warning else ""
        ctx = (
            f"[{ctx_style}]ctx: {self.context_pct:.0f}%[/]"
            if ctx_style
            else f"ctx: {self.context_pct:.0f}%"
        )
        return (
            f" {self.model_name}"
            f" │ last: {_fmt(self.turn_input)}↑ {_fmt(self.turn_output)}↓"
            f" │ total: {_fmt(self.total_input)}↑ {_fmt(self.total_output)}↓"
            f" │ {ctx}"
            f" │ ${self.cost:.3f}"
        )


def _fmt(n: int) -> str:
    """Format token count with K suffix for readability.

    e.g. 1500 → "1.5K", 800 → "800"
    """
    if n >= 1000:
        return f"{n / 1000:.1f}K"
    return str(n)
