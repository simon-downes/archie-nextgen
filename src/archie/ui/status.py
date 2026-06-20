"""Status bar widget with project info and metrics.

Shows: model │ in:fresh/cache_read/cache_write out:output │ ctx:N% │ $cost │ session_id ⎇ branch
       (cache columns hidden for non-cache models; "Local" shown instead of $0 for free models)

Token counts are session totals (lifetime, only climb). While streaming, output
shows a ~N estimate (chars/4) that snaps to the real value when UsageUpdated arrives.
Git branch is read directly from .git/HEAD (no subprocess spawn).
"""

import logging
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from archie.ui import theme

log = logging.getLogger(__name__)


def detect_git_branch(project_dir: Path) -> str:
    """Read the current branch from .git/HEAD directly — no subprocess.

    Returns the branch name or a short commit hash (detached HEAD), or "—" if
    not a git repo.
    """
    head = project_dir / ".git" / "HEAD"
    if not head.is_file():
        return "—"
    try:
        content = head.read_text().strip()
        if content.startswith("ref: refs/heads/"):
            return content.removeprefix("ref: refs/heads/")
        return content[:8]  # detached HEAD — show short hash
    except OSError:
        return "—"


class StatusBar(Widget):
    """Two-section status bar: left (metrics) and right (project info)."""

    DEFAULT_CSS = """
    StatusBar {
        height: 3;
        padding: 1;
    }
    StatusBar #status-left {
        width: 1fr;
        padding: 0;
    }
    StatusBar #status-right {
        width: auto;
        padding: 0;
        text-align: right;
    }
    """

    # Left section — session lifetime totals
    model_name: reactive[str] = reactive("—")
    session_input: reactive[int] = reactive(0)
    session_output: reactive[int] = reactive(0)
    cache_read: reactive[int] = reactive(0)
    cache_write: reactive[int] = reactive(0)
    context_pct: reactive[float] = reactive(0.0)
    warning: reactive[bool] = reactive(False)

    # Right section
    session_id: reactive[str] = reactive("—")
    git_branch: reactive[str] = reactive("—")

    # Model capability flags
    supports_cache: reactive[bool] = reactive(True)
    pricing_label: reactive[str] = reactive("$cost")

    # Streaming output estimate
    _output_estimate: reactive[int] = reactive(0)
    _estimating: reactive[bool] = reactive(False)

    def compose(self) -> ComposeResult:
        """Build the status bar with left and right sections."""
        with Horizontal():
            yield Static("", id="status-left")
            yield Static("", id="status-right")

    def update_output_estimate(self, chars: int) -> None:
        """Show a provisional output-token count while text is still streaming."""
        self._output_estimate = chars // 4
        self._estimating = True
        self._refresh_display()

    def clear_estimate(self) -> None:
        """Clear the streaming estimate when real usage arrives."""
        self._estimating = False
        self._refresh_display()

    def refresh_branch(self) -> None:
        """Re-read the git branch (e.g. after a checkout via shell tool)."""
        # Branch is read once up front and refreshed at each turn's end (a turn
        # may have committed/checked out). Read directly from .git/HEAD below.
        self._refresh_display()

    # --- Watchers: any reactive change triggers a display refresh ---

    def _watch_model_name(self) -> None:
        """Refresh display when model name changes."""
        self._refresh_display()

    def _watch_session_input(self) -> None:
        """Refresh display when input tokens change."""
        self._refresh_display()

    def _watch_session_output(self) -> None:
        """Refresh display when output tokens change."""
        self._refresh_display()

    def _watch_cache_read(self) -> None:
        """Refresh display when cache read tokens change."""
        self._refresh_display()

    def _watch_cache_write(self) -> None:
        """Refresh display when cache write tokens change."""
        self._refresh_display()

    def _watch_context_pct(self) -> None:
        """Refresh display when context percentage changes."""
        self._refresh_display()

    def _watch_session_id(self) -> None:
        """Refresh display when session ID changes."""
        self._refresh_display()

    def _watch_supports_cache(self) -> None:
        """Refresh display when cache support flag changes."""
        self._refresh_display()

    def _watch_pricing_label(self) -> None:
        """Refresh display when pricing label changes."""
        self._refresh_display()

    def _watch_git_branch(self) -> None:
        """Refresh display when git branch changes."""
        self._refresh_display()

    def _refresh_display(self) -> None:
        """Render the status bar content."""
        try:
            left = self.query_one("#status-left", Static)
            right = self.query_one("#status-right", Static)
        except Exception:  # noqa: BLE001 — widget may not be mounted yet
            return

        # Output: show ~estimate while streaming, real value otherwise
        out_display = f"~{self._output_estimate}" if self._estimating else str(self.session_output)

        # Input format: include cache columns only when caching is supported
        if self.supports_cache:
            in_val = (
                f"{_fmt(self.session_input)} / {_fmt(self.cache_read)} / {_fmt(self.cache_write)}"
            )
        else:
            in_val = f"{_fmt(self.session_input)}"

        # Context percentage with color progression
        if self.warning or self.context_pct > 85:
            ctx_val = f"[bold {theme.ERROR}]{self.context_pct:.0f}%[/]"
        elif self.context_pct >= 60:
            ctx_val = f"[bold {theme.WARNING}]{self.context_pct:.0f}%[/]"
        else:
            ctx_val = f"[{theme.BRIGHT}]{self.context_pct:.0f}%[/]"

        left.update(
            Text.from_markup(
                f" [{theme.PRIMARY}]{self.model_name}[/]"
                f" ⎇  [{theme.SECONDARY}]{self.git_branch}[/]"
                f" │ In: [{theme.POSITIVE}]{in_val}[/]"
                f"  Out: [{theme.POSITIVE_BRIGHT}]{out_display}[/]"
                f" │ Ctx: {ctx_val}"
                f" │ [{theme.COST}]{self.pricing_label}[/]"
            )
        )
        right.update(Text.from_markup(f"{self.session_id} "))


def _fmt(n: int) -> str:
    """Format token count with K suffix. e.g. 1500 → "1.5K", 800 → "800"."""
    if n >= 1000:
        return f"{n / 1000:.1f}K"
    return str(n)
