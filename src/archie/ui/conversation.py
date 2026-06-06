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
    }
    StreamingMessage > .header {
        color: $success;
        text-style: bold;
    }
    StreamingMessage > .content {
        margin: 0;
        padding: 0;
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


class ToolCallMessage(Widget):
    """A collapsible tool call block with a clickable header and hidden body.

    The header is always visible and shows the tool name, short args summary,
    and a status indicator (⌛ pending, ✔ success, ✘ error). The body contains
    the full arguments and result content, hidden by default to reduce noise
    in tool-heavy sessions.

    Lifecycle:
    1. Mounted at ToolStart with pending state (⌛) — only header visible
    2. Updated at ToolResult with success/error state and result content
    3. Auto-expands on error so failures are always visible

    Toggle expand/collapse:
    - Click the header
    - Press Enter or x when the block has focus

    The widget ID is set to the tool_use_id so it can be found and updated
    when the result arrives (Conversation.update_tool_result uses query_one).
    """

    # Allow this block to receive focus for keyboard navigation and block copy
    can_focus = True

    # Keybindings for toggling expand/collapse when focused
    BINDINGS = [
        ("enter", "toggle_expand", "Toggle"),
        ("x", "toggle_expand", "Toggle"),
    ]

    DEFAULT_CSS = """
    ToolCallMessage {
        padding: 1 2;
        margin: 1 0;
        background: $surface;
        height: auto;
    }
    ToolCallMessage > .tool-header {
        color: $text-muted;
        text-style: bold;
        height: auto;
    }
    ToolCallMessage > .tool-body {
        height: auto;
        margin: 0 0 0 2;
        display: none;
    }
    ToolCallMessage > .tool-body.expanded {
        display: block;
    }
    ToolCallMessage > .tool-body > .tool-args {
        color: $text-muted;
        height: auto;
    }
    ToolCallMessage > .tool-body > .tool-result {
        color: $text-muted;
        height: auto;
        margin: 1 0 0 0;
        max-height: 20;
        overflow-y: auto;
    }
    ToolCallMessage > .tool-body > .tool-error {
        color: $error;
        height: auto;
        margin: 1 0 0 0;
    }
    """

    def __init__(
        self,
        name: str,
        args: dict,
        result: str = "",
        is_error: bool = False,
        pending: bool = False,
        widget_id: str | None = None,
    ) -> None:
        # Use tool_use_id as widget ID so we can find this widget later
        super().__init__(id=widget_id)
        self._name = name
        self._args = args
        self._result = result
        self._is_error = is_error
        self._pending = pending
        self._expanded = False

    def compose(self):
        yield Static(self._build_header_text(), classes="tool-header")
        # Body container — hidden by default via CSS (display: none)
        body = Static("", classes="tool-body", markup=False)
        body.update(self._build_body_text())
        yield body

    def _build_header_text(self) -> str:
        """Build the header line: ▶/▼ 🔧 tool_name(short_args) status_icon."""
        import json

        # Collapse/expand indicator
        arrow = "▼" if self._expanded else "▶"
        # Status indicator
        if self._pending:
            status = "⌛"
        elif self._is_error:
            status = "✘"
        else:
            status = "✔"
        # Short args summary — just the first ~80 chars of compact JSON
        args_str = json.dumps(self._args, indent=None)
        if len(args_str) > 80:
            args_str = args_str[:80] + "…"
        return f"{arrow} 🔧 {self._name}({args_str}) {status}"

    def _build_body_text(self) -> str:
        """Build the body content: full args + result (if available)."""
        import json

        parts = []
        # Full arguments
        args_str = json.dumps(self._args, indent=2)
        parts.append(args_str)
        # Result content (only present after ToolResult arrives)
        if self._result:
            parts.append("─" * 40)
            display_result = self._result[:2000]
            if len(self._result) > 2000:
                display_result += "\n..."
            parts.append(display_result)
        return "\n".join(parts)

    def update_result(self, result: str, is_error: bool) -> None:
        """Called when ToolResult arrives — update status and content.

        Auto-expands the block on error so failures are always visible.
        """
        self._result = result
        self._is_error = is_error
        self._pending = False
        # Auto-expand on error — failed tools should always show their output
        if is_error:
            self._expanded = True
        self._refresh_display()

    def action_toggle_expand(self) -> None:
        """Toggle the body visibility (bound to Enter and x keys)."""
        self._expanded = not self._expanded
        self._refresh_display()

    def on_click(self, event) -> None:
        """Click on the header toggles expand/collapse.

        We check if the click target is the header widget — clicking in the
        body area (to read/select result text) should not collapse the block.
        """
        # Only toggle if clicking the header, not the body content
        try:
            header = self.query_one(".tool-header", Static)
            if event.widget is header:
                self._expanded = not self._expanded
                self._refresh_display()
        except Exception:  # noqa: BLE001
            pass

    def _refresh_display(self) -> None:
        """Re-render header text and toggle body visibility."""
        try:
            header = self.query_one(".tool-header", Static)
            header.update(self._build_header_text())
            body = self.query_one(".tool-body", Static)
            body.update(self._build_body_text())
            # Toggle the 'expanded' CSS class to show/hide body
            body.set_class(self._expanded, "expanded")
        except Exception:  # noqa: BLE001 — widget may not be mounted yet
            pass

    def get_copy_text(self) -> str:
        """Return formatted tool name + args + result for clipboard copy."""
        import json

        parts = [f"🔧 {self._name}"]
        parts.append(json.dumps(self._args, indent=2))
        if self._result:
            parts.append("─" * 40)
            parts.append(self._result)
        return "\n".join(parts)


class ShellOutput(Widget):
    """Display widget for user-initiated ! shell commands.

    Visually distinct from ToolCallMessage — this is for direct user commands
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
        """Add a complete tool call block showing name, args, and result.

        Used for session replay where we have the full tool call already.
        """
        self.mount(ToolCallMessage(name, args, result, is_error))
        self.scroll_end(animate=False)

    def mount_tool_pending(self, tool_use_id: str, name: str, args: dict) -> None:
        """Mount a tool call block in pending state (⌛) when ToolStart arrives.

        Uses tool_use_id as the widget ID so we can find it later at ToolResult.
        """
        self.mount(ToolCallMessage(name, args, pending=True, widget_id=tool_use_id))
        self.scroll_end(animate=False)

    def update_tool_result(self, tool_use_id: str, result: str, is_error: bool) -> None:
        """Update a pending tool call block with its result (✔ or ✘).

        Finds the ToolCallMessage by its widget ID (the tool_use_id).
        """
        try:
            widget = self.query_one(f"#{tool_use_id}", ToolCallMessage)
            widget.update_result(result, is_error)
        except Exception:  # noqa: BLE001 — widget may have been removed
            pass

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

        This is the plain-text → Markdown transition. The streaming widget
        is removed and a new AssistantMessage (with full Markdown rendering)
        takes its place.
        """
        final = AssistantMessage(streaming.text)
        streaming.remove()
        self.mount(final)
        self.scroll_end(animate=False)
