"""Conversation display widget with styled message blocks.

The conversation is a vertical scroll container that holds individual
message widgets. Each message type has its own styling:
- UserMessage: highlighted background so your messages stand out
- AssistantMessage: rendered Markdown for rich formatting
- StreamingMessage: plain text that updates live during generation
- ErrorMessage: red styling for errors

The streaming → finalised flow:
1. When the model starts generating, we mount a StreamingMessage
2. Text chunks are appended to it as they arrive (plain text, fast updates)
3. When generation completes, we REPLACE it with an AssistantMessage
   which renders the full response as proper Markdown (slower but prettier)

This two-phase approach avoids re-rendering Markdown on every chunk
(which would be expensive and cause visual flicker).
"""

from textual.containers import VerticalScroll
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Markdown, Static


class UserMessage(Static):
    """A user message block with highlighted background.

    Uses Textual's Rich markup for the header (▶ You in bold cyan).
    The content is rendered as plain text — users don't write markdown.
    can_focus enables keyboard/click navigation between message blocks.
    """

    # Allow this block to receive focus for keyboard navigation and block copy
    can_focus = True

    DEFAULT_CSS = """
    UserMessage {
        background: $primary-background;
        padding: 1 2;
        margin: 1 0;
    }
    """

    def __init__(self, content: str) -> None:
        super().__init__(f"[bold cyan]▶ You[/]\n{content}")
        self._content = content

    def get_copy_text(self) -> str:
        """Return the plain text content for clipboard copy."""
        return self._content


class AssistantMessage(Widget):
    """A finalised assistant message with full Markdown rendering.

    This is a compound widget: a styled header + a Markdown widget.
    The Markdown widget handles code blocks, lists, bold, etc.
    can_focus enables keyboard/click navigation between message blocks.
    """

    # Allow this block to receive focus for keyboard navigation and block copy
    can_focus = True

    DEFAULT_CSS = """
    AssistantMessage {
        padding: 1 2;
        margin: 1 0;
        height: auto;   /* Shrink to fit content, don't expand */
    }
    AssistantMessage > .header {
        color: $success;
        text-style: bold;
        height: auto;
    }
    AssistantMessage > Markdown {
        margin: 0;
        padding: 0;
        height: auto;   /* Critical — without this, Markdown expands to fill parent */
    }
    """

    def __init__(self, content: str = "") -> None:
        super().__init__()
        self._content = content

    def compose(self):
        yield Static("● Archie", classes="header")
        yield Markdown(self._content)

    def get_copy_text(self) -> str:
        """Return the markdown source text for clipboard copy."""
        return self._content

    def update_content(self, content: str) -> None:
        """Update the markdown content after initial render."""
        self._content = content
        try:
            md = self.query_one(Markdown)
            md.update(content)
        except Exception:  # noqa: BLE001 — widget may not be mounted yet during compose
            pass


class StreamingMessage(Widget):
    """A message that's actively being streamed from the model.

    Uses a reactive `text` property — when text changes, the content
    Static widget automatically updates via watch_text().

    This is plain text (not Markdown) because:
    1. Re-rendering Markdown on every chunk would be expensive
    2. Incomplete Markdown mid-stream looks broken (unclosed code blocks etc)
    3. Plain text updates are instant and smooth

    The ⟳ in the header indicates generation is in progress.
    """

    DEFAULT_CSS = """
    StreamingMessage {
        padding: 1 2;
        margin: 1 0;
        height: auto;
    }
    StreamingMessage > .header {
        color: $success;
        text-style: bold;
        height: auto;
    }
    StreamingMessage > .content {
        margin: 0;
        padding: 0;
        height: auto;
    }
    """

    # Textual reactive: changing this value triggers watch_text() automatically.
    text: reactive[str] = reactive("")

    def compose(self):
        yield Static("● Archie ⟳", classes="header")
        yield Static("", classes="content")

    def watch_text(self, value: str) -> None:
        """Called automatically when self.text changes. Updates the display."""
        try:
            self.query_one(".content", Static).update(value)
        except Exception:  # noqa: BLE001 — widget may not be mounted yet
            pass

    def append(self, chunk: str) -> None:
        """Append a text chunk. Triggers reactive update."""
        self.text += chunk


class ErrorMessage(Static):
    """An error message block with red styling.

    Displayed inline in the conversation so errors are visible in context
    (rather than disappearing toast notifications).
    """

    DEFAULT_CSS = """
    ErrorMessage {
        background: $error-darken-3;
        color: $error;
        padding: 1 2;
        margin: 1 0;
    }
    """

    can_focus = True

    def __init__(self, content: str) -> None:
        super().__init__(f"[bold red]✗ Error[/]\n{content}")
        self._content = content

    def get_copy_text(self) -> str:
        """Return the error text for clipboard copy."""
        return self._content


class IterationBlock(Widget):
    """A static block showing tool call summaries for one iteration.

    One iteration = one LLM response that triggered N tool calls. All N tools
    are displayed as lines within this single block. Lines stream in as tools
    start (pending ○) and complete (● green/red).

    This is a static display block like UserMessage/AssistantMessage — no
    collapse, no expand, no interactivity beyond focus for copy.
    """

    can_focus = True

    DEFAULT_CSS = """
    IterationBlock {
        padding: 0 2;
        margin: 0 0;
        height: auto;
    }
    IterationBlock > Static {
        height: auto;
        margin: 0;
        padding: 0;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._tool_widgets: dict[str, Static] = {}
        self._summaries: list[str] = []

    def add_pending(self, tool_use_id: str, ui_summary: str) -> None:
        """Add a pending tool line (○ indicator in primary colour).

        ui_summary is pre-formatted Rich markup from format_tool_pending.
        """
        text = f"[bold #0178d4]○[/] {ui_summary}"
        widget = Static(text, markup=True)
        self._tool_widgets[tool_use_id] = widget
        self.mount(widget)

    def complete_tool(
        self,
        tool_use_id: str,
        ui_summary: str,
        is_error: bool,
        ui_detail: list[str] | None = None,
    ) -> None:
        """Replace a pending line with the completed summary.

        ui_summary is pre-formatted Rich markup from format_tool_complete.
        ui_detail lines are pre-formatted Rich markup from format_tool_detail.
        """
        colour = "red" if is_error else "green"
        text = f"[bold {colour}]●[/] {ui_summary}"
        if ui_detail:
            text += "\n" + "\n".join(ui_detail)

        self._summaries.append(ui_summary)
        widget = self._tool_widgets.get(tool_use_id)
        if widget:
            widget.update(text)
        else:
            w = Static(text, markup=True)
            self._tool_widgets[tool_use_id] = w
            self.mount(w)

    def get_copy_text(self) -> str:
        """Return all tool summaries as plain text for clipboard."""
        return "\n".join(f"● {s}" for s in self._summaries)


def _escape(text: str) -> str:
    """Escape Rich markup characters in text."""
    return text.replace("[", r"\[").replace("]", r"\]")


class ShellOutput(Widget):
    """Display widget for user-initiated ! shell commands.

    Visually distinct from IterationBlock — this is for direct user commands
    (not model tool calls). Shows a $ prefix, exit code, and monospace output.
    Output is capped at 2000 chars to prevent the conversation from being
    overwhelmed by verbose command output.
    can_focus enables keyboard/click navigation between message blocks.
    """

    # Allow this block to receive focus for keyboard navigation and block copy
    can_focus = True

    # Max characters to display from command output.
    _OUTPUT_CAP = 2000

    DEFAULT_CSS = """
    ShellOutput {
        padding: 1 2;
        margin: 1 0;
        background: $surface;
        height: auto;
    }
    ShellOutput > .shell-header {
        color: $text-muted;
        text-style: bold;
        height: auto;
    }
    ShellOutput > .shell-exit {
        color: $text-muted;
        height: auto;
        margin: 0 0 0 2;
    }
    ShellOutput > .shell-output {
        color: $text-muted;
        height: auto;
        margin: 0 0 0 2;
        max-height: 20;
        overflow-y: auto;
    }
    """

    def __init__(self, command: str, output: str, exit_code: int) -> None:
        super().__init__()
        self._command = command
        self._output = output
        self._exit_code = exit_code

    def compose(self):
        yield Static(f"$ {self._command}", classes="shell-header")
        yield Static(f"[exit: {self._exit_code}]", classes="shell-exit")
        if self._output:
            # Cap output to prevent conversation bloat from verbose commands.
            display = self._output[: self._OUTPUT_CAP]
            if len(self._output) > self._OUTPUT_CAP:
                display += "\n..."
            # markup=False — output can contain [] and other Rich-interpreted chars.
            yield Static(display, classes="shell-output", markup=False)

    def get_copy_text(self) -> str:
        """Return command + output for clipboard copy."""
        parts = [f"$ {self._command}", f"[exit: {self._exit_code}]"]
        if self._output:
            parts.append(self._output)
        return "\n".join(parts)


class Conversation(VerticalScroll):
    """Scrollable container for message blocks.

    VerticalScroll is a Textual container that auto-scrolls and provides
    a scrollbar. Messages are mounted as children and rendered top-to-bottom.
    """

    DEFAULT_CSS = """
    Conversation {
        height: 1fr;    /* Fill available space */
        padding: 1 0;
    }
    """

    def add_user_message(self, content: str) -> None:
        """Add a user message and scroll to show it."""
        self.mount(UserMessage(content))
        self.scroll_end(animate=False)

    def add_error(self, content: str) -> None:
        """Add an error message and scroll to show it."""
        self.mount(ErrorMessage(content))
        self.scroll_end(animate=False)

    def add_assistant_message(self, content: str) -> None:
        """Add a complete assistant message (used for session replay, not streaming)."""
        self.mount(AssistantMessage(content))
        self.scroll_end(animate=False)

    def add_tool_call(self, name: str, args: dict, result: str, is_error: bool) -> None:
        """Add a complete tool call to the current iteration block (used for session replay)."""
        pass  # No session replay in this app

    def begin_iteration(self) -> None:
        """Start a new iteration block for tool calls."""
        self._current_iteration = IterationBlock()
        self.mount(self._current_iteration)
        self.scroll_end(animate=False)

    def add_tool_pending(self, tool_use_id: str, ui_summary: str) -> None:
        """Add a pending tool line to the current iteration block."""
        if not hasattr(self, "_current_iteration") or self._current_iteration is None:
            self.begin_iteration()
        self._current_iteration.add_pending(tool_use_id, ui_summary)
        self.scroll_end(animate=False)

    def complete_tool(
        self,
        tool_use_id: str,
        ui_summary: str,
        is_error: bool,
        ui_detail: list[str] | None = None,
    ) -> None:
        """Complete a tool in the current iteration block."""
        if hasattr(self, "_current_iteration") and self._current_iteration is not None:
            self._current_iteration.complete_tool(tool_use_id, ui_summary, is_error, ui_detail)
            self.scroll_end(animate=False)

    def end_iteration(self) -> None:
        """Mark the current iteration as done."""
        self._current_iteration = None

    def add_shell_output(self, command: str, output: str, exit_code: int) -> None:
        """Add a user shell command result (from ! prefix).

        This is NOT recorded in the session — it's purely for display.
        Distinct from add_tool_call which shows model-initiated tool use.
        """
        self.mount(ShellOutput(command, output, exit_code))
        self.scroll_end(animate=False)

    def begin_streaming(self) -> StreamingMessage:
        """Start a streaming response. Returns the widget to append chunks to."""
        msg = StreamingMessage()
        self.mount(msg)
        self.scroll_end(animate=False)
        return msg

    def finalise_streaming(self, streaming: StreamingMessage) -> None:
        """Replace a streaming widget with a finalised Markdown version.

        Uses mount(before=) + remove() to avoid a flash from the layout gap
        that would occur if we removed first then mounted.
        """
        final = AssistantMessage(streaming.text)
        self.mount(final, before=streaming)
        streaming.remove()
        self.scroll_end(animate=False)
