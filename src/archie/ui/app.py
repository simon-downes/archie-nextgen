"""Main Textual application.

This is the orchestrator that wires everything together:
- UI widgets (conversation, input, status bar)
- LLM client (sends messages to Bedrock)
- Session (tracks state and persists to disk)

Architecture note: Currently the orchestration logic (send → stream →
accumulate → record) lives here in the UI layer. When we add tools (Phase 2),
we'll extract an Engine class that handles the LLM loop and tool dispatch,
and the app will just drive the engine and display its events.

Threading model:
- Textual runs an asyncio event loop on the main thread
- Bedrock streaming is synchronous (blocks waiting for chunks)
- We run the stream in a Worker (background thread) and communicate
  back to the UI via Textual's message system (post_message)
- This keeps the UI responsive while waiting for the model
"""

import logging

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.widgets import Footer, TabbedContent, TabPane
from textual.worker import Worker, get_current_worker

from archie.config import load_config
from archie.llm import BedrockClient, Done, TextDelta, Usage
from archie.models import get_model_info
from archie.session import Session
from archie.ui.conversation import Conversation, StreamingMessage
from archie.ui.input import MessageInput
from archie.ui.status import StatusBar

log = logging.getLogger(__name__)


# --- Custom Textual Messages ---
# These are posted from the worker thread to the UI thread.
# Textual's message system is thread-safe — post_message() from any thread
# and the handler runs on the main thread where it's safe to update widgets.


class StreamChunk(Message):
    """A chunk of text arrived from the model. Append it to the display."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class StreamComplete(Message):
    """Streaming finished (either naturally, interrupted, or on error).

    input_tokens/output_tokens may be 0 if an error occurred before
    the API returned usage metadata.
    """

    def __init__(self, input_tokens: int, output_tokens: int, interrupted: bool = False) -> None:
        super().__init__()
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.interrupted = interrupted


class ArchieApp(App):
    """The main Textual application. Manages the UI and orchestrates LLM calls."""

    TITLE = "Archie"

    # Textual CSS — defines layout and styling for widgets.
    # Uses Textual's CSS dialect (similar to web CSS but with some differences).
    CSS = """
    Conversation {
        height: 1fr;       /* Fill available vertical space */
    }
    StatusBar {
        height: 1;         /* Exactly one line tall */
        padding: 0 2;
        background: $surface;
        color: $text-muted;
    }
    MessageInput {
        height: auto;      /* Grow with content */
        max-height: 8;     /* But cap at 8 lines */
        min-height: 1;
        margin: 1 2;
        padding: 0 1;
        background: $surface;
        border: round $surface-lighten-2;
    }
    MessageInput:focus {
        border: round $primary;  /* Highlight border when focused */
    }
    """

    # Key bindings — Textual maps these to action_* methods.
    # They also auto-populate the Footer widget with hints.
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+n", "new_session", "New Session"),
    ]

    def __init__(self) -> None:
        super().__init__()

        # Load config and set up dependencies.
        # If config is invalid, this raises and cli.py shows a clean error.
        self.config = load_config()
        self.model_info = get_model_info(self.config.model)
        self.llm = BedrockClient(model_id=self.config.model, region=self.config.region)
        self.session = Session(model_id=self.config.model, model_info=self.model_info)

        # Streaming state — tracks the in-progress response
        self._streaming: StreamingMessage | None = None  # The UI widget being streamed into
        self._stream_worker: Worker | None = None  # The background thread handle
        self._stream_text: str = ""  # Accumulated text from all chunks

    def compose(self) -> ComposeResult:
        """Build the widget tree. Called once when the app starts.

        Layout:
        ┌─ TabbedContent ─────────────────────────┐
        │ [Session 1]                              │  ← tab (single for now)
        │ ┌─ Conversation ───────────────────────┐ │
        │ │ messages scroll here                 │ │
        │ └─────────────────────────────────────-┘ │
        │ ┌─ StatusBar ─────────────────────────-┐ │
        │ │ model │ tokens │ cost                │ │
        │ └──────────────────────────────────────┘ │
        │ ┌─ MessageInput ──────────────────────-┐ │
        │ │ type here...                         │ │
        │ └──────────────────────────────────────┘ │
        └──────────────────────────────────────────┘
        ┌─ Footer ────────────────────────────────-┐
        │ Ctrl+Q: Quit │ Esc: Cancel │ ...         │  ← auto-generated from BINDINGS
        └──────────────────────────────────────────┘
        """
        with TabbedContent():
            with TabPane("Session 1"):
                yield Conversation(id="conversation")
                yield StatusBar(id="status")
                yield MessageInput(id="input")
        yield Footer()

    def on_mount(self) -> None:
        """Called after all widgets are mounted. Focus the input."""
        self._update_status()
        self.query_one("#input", MessageInput).focus()

    # --- Message flow: User submits → Stream → Display ---

    def on_message_input_submitted(self, event: MessageInput.Submitted) -> None:
        """Handle user pressing Enter in the input box.

        This kicks off the full flow:
        1. Display the user's message in the conversation
        2. Record it in the session
        3. Disable input (prevent double-sends)
        4. Start streaming the model's response in a background thread
        """
        if self._stream_worker is not None:
            return  # Already streaming — ignore

        conv = self.query_one("#conversation", Conversation)
        conv.add_user_message(event.content)

        self.session.add_turn("user", event.content)

        self.query_one("#input", MessageInput).disabled = True

        # Reset streaming state and start the worker
        self._stream_text = ""
        self._streaming = conv.begin_streaming()
        self._stream_worker = self.run_worker(self._run_stream, thread=True)

    def _run_stream(self) -> None:
        """Background thread: call Bedrock and post events to the UI.

        Runs in a Worker thread (not the main UI thread). Communicates
        with the UI exclusively via post_message() which is thread-safe.

        The worker checks is_cancelled on each chunk — this is how Esc
        interrupts generation without killing the thread.
        """
        worker = get_current_worker()
        input_tokens = 0
        output_tokens = 0

        try:
            for event in self.llm.stream(
                messages=self.session.messages,
                system=self.config.system_prompt,
            ):
                # Check cancellation between chunks — cooperative cancellation
                if worker.is_cancelled:
                    self.post_message(StreamComplete(input_tokens, output_tokens, interrupted=True))
                    return

                if isinstance(event, TextDelta):
                    self.post_message(StreamChunk(event.text))
                elif isinstance(event, Usage):
                    input_tokens = event.input_tokens
                    output_tokens = event.output_tokens
                elif isinstance(event, Done):
                    pass  # We detect completion by the stream ending

            self.post_message(StreamComplete(input_tokens, output_tokens))
        except Exception as e:
            # Log the full traceback for debugging, show clean message to user
            log.exception("Stream error")
            self.call_from_thread(self._show_error, str(e))
            # Signal completion with zero tokens — on_stream_complete won't
            # record a turn because _stream_text will be empty (or partial)
            self.post_message(StreamComplete(0, 0, interrupted=True))

    def on_stream_chunk(self, event: StreamChunk) -> None:
        """UI thread: a text chunk arrived. Append it to the streaming widget."""
        if self._streaming:
            self._stream_text += event.text
            self._streaming.append(event.text)

    def on_stream_complete(self, event: StreamComplete) -> None:
        """UI thread: streaming finished. Finalise the response.

        Steps:
        1. Replace the streaming widget with a proper Markdown widget
        2. Record the assistant turn in the session (if there's content)
        3. Update the status bar with token counts
        4. Re-enable the input box
        """
        conv = self.query_one("#conversation", Conversation)

        # Finalise or remove the streaming widget
        if self._streaming:
            if self._stream_text:
                # Replace plain-text streaming widget with rendered Markdown
                conv.finalise_streaming(self._streaming)
            else:
                # Error before any text arrived — just remove the empty widget
                self._streaming.remove()
            self._streaming = None

        # Only record a turn if the model actually produced content.
        # On errors, _stream_text is empty and we don't want ghost turns.
        if self._stream_text:
            self.session.add_turn(
                "assistant",
                self._stream_text,
                input_tokens=event.input_tokens,
                output_tokens=event.output_tokens,
                interrupted=event.interrupted,
            )

        self._update_status(event.input_tokens, event.output_tokens)

        # Re-enable input and give it focus
        self._stream_worker = None
        inp = self.query_one("#input", MessageInput)
        inp.disabled = False
        inp.focus()

    # --- UI helpers ---

    def _show_error(self, message: str) -> None:
        """Display an error as a styled block in the conversation."""
        conv = self.query_one("#conversation", Conversation)
        conv.add_error(message)

    def _update_status(self, turn_input: int = 0, turn_output: int = 0) -> None:
        """Push current stats to the status bar widget."""
        status = self.query_one("#status", StatusBar)
        status.model_name = self.model_info.name
        status.turn_input = turn_input
        status.turn_output = turn_output
        status.total_input = self.session.total_input_tokens
        status.total_output = self.session.total_output_tokens
        status.context_pct = self.session.context_pct
        status.cost = self.session.total_cost
        status.warning = self.session.context_warning

    # --- Actions (bound to key presses via BINDINGS) ---

    def action_cancel(self) -> None:
        """Esc pressed — cancel in-progress generation."""
        if self._stream_worker is not None:
            self._stream_worker.cancel()

    def action_new_session(self) -> None:
        """Ctrl+N pressed — start a fresh conversation."""
        self.session = Session(model_id=self.config.model, model_info=self.model_info)
        conv = self.query_one("#conversation", Conversation)
        conv.remove_children()
        self._update_status()
