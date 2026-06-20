"""Message input widget.

A TextArea configured for chat input:
- Enter sends the message
- Shift+Enter inserts a newline (for multiline messages)
- Tab moves focus (doesn't insert a tab character)

The widget posts a Submitted message (Textual's event system) when the
user presses Enter with non-empty content. The parent app handles this
message via on_message_input_submitted().
"""

from textual.message import Message
from textual.widgets import TextArea


class MessageInput(TextArea):
    """Multiline input area with chat-style key bindings."""

    DEFAULT_CSS = """
    MessageInput {
        height: auto;
        max-height: 8;
        min-height: 1;
        margin: 0;
        padding: 0 1;
    }
    """

    class Submitted(Message):
        """Posted when user presses Enter with non-empty content.

        The parent widget (ArchieApp) handles this via the
        on_message_input_submitted() convention — Textual auto-routes
        messages to handlers named on_<widget>_<message>().
        """

        def __init__(self, content: str) -> None:
            super().__init__()
            self.content = content

    def __init__(self, **kwargs) -> None:
        # language=None disables syntax highlighting (this is chat, not code)
        # show_line_numbers=False keeps it clean
        super().__init__(language=None, show_line_numbers=False, **kwargs)
        # Tab should move focus to the next widget, not insert a tab character
        self.tab_behavior = "focus"

    async def _on_key(self, event) -> None:
        """Override key handling for chat-style Enter behaviour.

        Textual's TextArea normally treats Enter as "insert newline".
        We intercept it to mean "send message" instead.

        - Enter: send the message (if non-empty), clear the input
        - Shift+Enter: insert a newline (the "escape hatch" for multiline)

        event.prevent_default() stops the TextArea from processing the key.
        event.stop() prevents the event from bubbling up to parent widgets.
        """
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            content = self.text.strip()
            if content:
                self.post_message(self.Submitted(content))
                self.clear()
        elif event.key == "shift+enter":
            event.prevent_default()
            event.stop()
            self.insert("\n")
