"""Status bar widget with project info and metrics.

Shows: model │ in:fresh/cache_read/cache_write out:output │ ctx:N% │ $cost │ project ⎇ branch

Token counts are session totals (lifetime, only climb). While streaming, output
shows a ~N estimate (chars/4) that snaps to the real value when UsageUpdated arrives.
Git branch is read directly from .git/HEAD (no subprocess spawn).
"""

import logging
from pathlib import Path

from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

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

    # Left section — session lifetime totals
    model_name: reactive[str] = reactive("—")
    session_input: reactive[int] = reactive(0)
    session_output: reactive[int] = reactive(0)
    cache_read: reactive[int] = reactive(0)
    cache_write: reactive[int] = reactive(0)
    context_pct: reactive[float] = reactive(0.0)
    cost: reactive[float] = reactive(0.0)
    warning: reactive[bool] = reactive(False)

    # Right section
    project_name: reactive[str] = reactive("—")
    git_branch: reactive[str] = reactive("—")

    # Streaming output estimate
    _output_estimate: reactive[int] = reactive(0)
    _estimating: reactive[bool] = reactive(False)

    def compose(self):
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
        # git_branch is set by the app from _detect_git_branch; this triggers display update
        self._refresh_display()

    # --- Watchers: any reactive change triggers a display refresh ---

    def _watch_model_name(self) -> None:
        self._refresh_display()

    def _watch_session_input(self) -> None:
        self._refresh_display()

    def _watch_session_output(self) -> None:
        self._refresh_display()

    def _watch_cache_read(self) -> None:
        self._refresh_display()

    def _watch_cache_write(self) -> None:
        self._refresh_display()

    def _watch_context_pct(self) -> None:
        self._refresh_display()

    def _watch_cost(self) -> None:
        self._refresh_display()

    def _watch_project_name(self) -> None:
        self._refresh_display()

    def _watch_git_branch(self) -> None:
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

        # Context percentage with warning highlight
        ctx = (
            f"[bold red]ctx:{self.context_pct:.0f}%[/]"
            if self.warning
            else f"ctx:{self.context_pct:.0f}%"
        )

        left.update(
            f" {self.model_name}"
            f" │ in:{_fmt(self.session_input)}/{_fmt(self.cache_read)}/{_fmt(self.cache_write)}"
            f" out:{out_display}"
            f" │ {ctx}"
            f" │ ${self.cost:.4f}"
        )
        right.update(f"{self.project_name} ⎇ {self.git_branch} ")


def _fmt(n: int) -> str:
    """Format token count with K suffix. e.g. 1500 → "1.5K", 800 → "800"."""
    if n >= 1000:
        return f"{n / 1000:.1f}K"
    return str(n)
