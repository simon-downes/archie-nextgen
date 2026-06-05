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
    """

    DEFAULT_CSS = """
    UserMessage {
        background: $primary-background;
        padding: 1 2;
        margin: 1 0;
    }
    """

    def __init__(self, content: str) -> None:
        super().__init__(f"[bold cyan]▶ You[/]\n{content}")


class AssistantMessage(Widget):
    """A finalised assistant message with full Markdown rendering.

    This is a compound widget: a styled header + a Markdown widget.
    The Markdown widget handles code blocks, lists, bold, etc.
    """

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

    def __init__(self, content: str) -> None:
        super().__init__(f"[bold red]✗ Error[/]\n{content}")


class ToolCallMessage(Widget):
    """A tool call block showing tool name, arguments, and result.

    Visually distinct from text messages — uses muted colours and monospace
    formatting so the user can see tool activity in the conversation flow
    without it being confused for model-generated text.

    Structure:
        🔧 tool_name(args)
        ─────────────────
        result content
    """

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
    ToolCallMessage > .tool-args {
        color: $text-muted;
        height: auto;
        margin: 0 0 0 2;
    }
    ToolCallMessage > .tool-result {
        color: $text-muted;
        height: auto;
        margin: 1 0 0 2;
        max-height: 20;
        overflow-y: auto;
    }
    ToolCallMessage > .tool-error {
        color: $error;
        height: auto;
        margin: 1 0 0 2;
    }
    """

    def __init__(self, name: str, args: dict, result: str = "", is_error: bool = False) -> None:
        super().__init__()
        self._name = name
        self._args = args
        self._result = result
        self._is_error = is_error

    def compose(self):
        import json

        yield Static(f"🔧 {self._name}", classes="tool-header")
        # Show args in compact JSON format — markup=False because args contain
        # arbitrary text with [] characters that Rich would misinterpret as tags.
        args_str = json.dumps(self._args, indent=None)
        if len(args_str) > 200:
            args_str = args_str[:200] + "..."
        yield Static(args_str, classes="tool-args", markup=False)
        # Show result — also markup=False for the same reason (file content,
        # error messages, etc. can all contain Rich markup characters).
        if self._result:
            css_class = "tool-error" if self._is_error else "tool-result"
            display_result = self._result[:2000]
            if len(self._result) > 2000:
                display_result += "\n..."
            yield Static(display_result, classes=css_class, markup=False)


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
        """Add a complete tool call block showing name, args, and result."""
        self.mount(ToolCallMessage(name, args, result, is_error))
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
