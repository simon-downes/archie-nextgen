"""Status bar widget with project info and metrics.

Reworked from a single Static to a Horizontal container with two sections:
- Left: project name + git branch (contextual info)
- Right: model name + token counts + context % + cost (metrics)

The git branch is detected once at startup via subprocess on the host
(not inside the sandbox container). This is intentionally simple — we don't
re-detect on branch change because it's rare during a session.

Uses Textual's reactive system: when any property changes, the relevant
Static widget updates automatically.
"""

import logging
import subprocess
from pathlib import Path

from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

log = logging.getLogger(__name__)


def _detect_git_branch(project_dir: Path) -> str:
    """Detect the current git branch by running git on the host.

    Returns the branch name (e.g. "main") or "—" if detection fails
    (not a git repo, git not installed, detached HEAD, etc.).

    Runs with a short timeout to avoid hanging the UI on startup.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        # git not found, timeout, or other OS error — all non-fatal
        log.debug("Git branch detection failed", exc_info=True)
    return "—"


class StatusBar(Widget):
    """Two-section status bar: left (project info) and right (metrics).

    Layout is handled by a Horizontal container with two Static children.
    The TCSS in archie.tcss handles the visual styling (background, padding,
    text alignment). This widget manages the content via reactives.
    """

    # --- Reactive properties ---
    # Left section
    project_name: reactive[str] = reactive("—")
    git_branch: reactive[str] = reactive("—")

    # Right section
    model_name: reactive[str] = reactive("—")
    turn_input: reactive[int] = reactive(0)
    turn_output: reactive[int] = reactive(0)
    total_input: reactive[int] = reactive(0)
    total_output: reactive[int] = reactive(0)
    context_pct: reactive[float] = reactive(0.0)
    cost: reactive[float] = reactive(0.0)
    warning: reactive[bool] = reactive(False)

    def compose(self):
        """Build the two-section layout inside a Horizontal container."""
        with Horizontal():
            yield Static("", id="status-left")
            yield Static("", id="status-right")

    def _watch_project_name(self) -> None:
        self._update_right()

    def _watch_git_branch(self) -> None:
        self._update_right()

    def _watch_model_name(self) -> None:
        self._update_left()

    def _watch_turn_input(self) -> None:
        self._update_left()

    def _watch_turn_output(self) -> None:
        self._update_left()

    def _watch_total_input(self) -> None:
        self._update_left()

    def _watch_total_output(self) -> None:
        self._update_left()

    def _watch_context_pct(self) -> None:
        self._update_left()

    def _watch_cost(self) -> None:
        self._update_left()

    def _watch_warning(self) -> None:
        self._update_left()

    def _update_left(self) -> None:
        """Update the left section: model + token metrics."""
        try:
            left = self.query_one("#status-left", Static)
        except Exception:  # noqa: BLE001 — widget may not be mounted yet
            return

        # Highlight context percentage in red when approaching the limit
        ctx_style = "bold red" if self.warning else ""
        ctx = (
            f"[{ctx_style}]ctx: {self.context_pct:.0f}%[/]"
            if ctx_style
            else f"ctx: {self.context_pct:.0f}%"
        )
        left.update(
            f" {self.model_name}"
            f" │ {_fmt(self.turn_input)}↑ {_fmt(self.turn_output)}↓"
            f" │ {ctx}"
            f" │ ${self.cost:.3f}"
        )

    def _update_right(self) -> None:
        """Update the right section: project name (branch)."""
        try:
            right = self.query_one("#status-right", Static)
            right.update(f"{self.project_name} ({self.git_branch}) ")
        except Exception:  # noqa: BLE001 — widget may not be mounted yet
            pass


def _fmt(n: int) -> str:
    """Format token count with K suffix for readability.

    e.g. 1500 → "1.5K", 800 → "800"
    """
    if n >= 1000:
        return f"{n / 1000:.1f}K"
    return str(n)
