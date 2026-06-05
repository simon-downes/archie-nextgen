"""Main Textual application.

This is the UI layer — it handles display and user interaction only.
All orchestration logic (LLM calls, tool dispatch, the tool-use loop)
lives in the Engine. The app runs the engine in a background thread
and reacts to the events it yields.

Threading model:
- Textual runs an asyncio event loop on the main thread
- The Engine is a synchronous generator running in a Worker (background thread)
- Events are posted from the worker to the UI via Textual's message system
- This keeps the UI responsive while waiting for LLM responses and tool execution
"""

import logging
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.widgets import Footer, TabbedContent, TabPane
from textual.worker import Worker, get_current_worker

from archie.config import load_config
from archie.engine import Engine
from archie.llm import BedrockClient
from archie.models import get_model_info
from archie.session import Session
from archie.tools import create_default_registry
from archie.types import TextDelta, ToolCallResult, ToolCallStart, TurnComplete
from archie.ui.conversation import Conversation, StreamingMessage
from archie.ui.input import MessageInput
from archie.ui.status import StatusBar

log = logging.getLogger(__name__)


# --- Custom Textual Messages ---
# Posted from the worker thread to the UI thread. Textual's message system
# is thread-safe — post_message() from any thread and the handler runs on
# the main thread where it's safe to update widgets.


class StreamChunk(Message):
    """A chunk of text arrived from the model. Append it to the display."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class ToolStart(Message):
    """The model is about to call a tool."""

    def __init__(self, tool_use_id: str, name: str, input: dict) -> None:
        super().__init__()
        self.tool_use_id = tool_use_id
        self.name = name
        self.input = input


class ToolResult(Message):
    """A tool finished executing."""

    def __init__(self, tool_use_id: str, name: str, content: str, is_error: bool) -> None:
        super().__init__()
        self.tool_use_id = tool_use_id
        self.name = name
        self.content = content
        self.is_error = is_error


class StreamComplete(Message):
    """Engine finished processing this user message."""

    def __init__(
        self, input_tokens: int, output_tokens: int, stop_reason: str, interrupted: bool = False
    ) -> None:
        super().__init__()
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.stop_reason = stop_reason
        self.interrupted = interrupted


class ArchieApp(App):
    """The main Textual application. Manages the UI and drives the Engine."""

    TITLE = "Archie"

    CSS = """
    Conversation {
        height: 1fr;
    }
    StatusBar {
        height: 1;
        padding: 0 2;
        background: $surface;
        color: $text-muted;
    }
    MessageInput {
        height: auto;
        max-height: 8;
        min-height: 1;
        margin: 1 2;
        padding: 0 1;
        background: $surface;
        border: round $surface-lighten-2;
    }
    MessageInput:focus {
        border: round $primary;
    }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+n", "new_session", "New Session"),
    ]

    def __init__(self) -> None:
        super().__init__()

        # Load config and set up dependencies
        self.config = load_config()
        self.model_info = get_model_info(self.config.model)
        self.llm = BedrockClient(model_id=self.config.model, region=self.config.region)
        self.session = Session(model_id=self.config.model, model_info=self.model_info)

        # Create tool registry with configured path access
        cwd = Path.cwd()
        allowed = [Path(p) for p in self.config.tools.allowed_directories]
        self.tool_registry = create_default_registry(cwd, allowed)

        # Create the engine
        self.engine = Engine(
            llm_client=self.llm,
            session=self.session,
            tool_registry=self.tool_registry,
            system_prompt=self.config.system_prompt,
        )

        # Streaming state
        self._streaming: StreamingMessage | None = None
        self._stream_worker: Worker | None = None
        self._stream_text: str = ""
        self._pending_tool_args: dict[str, dict] = {}  # tool_use_id → args for UI display

    def compose(self) -> ComposeResult:
        """Build the widget tree."""
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

    # --- Message flow: User submits → Engine processes → Display ---

    def on_message_input_submitted(self, event: MessageInput.Submitted) -> None:
        """Handle user pressing Enter in the input box."""
        if self._stream_worker is not None:
            return  # Already processing — ignore

        conv = self.query_one("#conversation", Conversation)
        conv.add_user_message(event.content)

        # Disable input until TurnComplete arrives
        self.query_one("#input", MessageInput).disabled = True

        # Reset streaming state and start the engine worker
        self._stream_text = ""
        self._streaming = None
        self._stream_worker = self.run_worker(lambda: self._run_engine(event.content), thread=True)

    def _run_engine(self, message: str) -> None:
        """Background thread: run the engine and post events to the UI.

        The Engine yields events synchronously. We translate them into
        Textual Messages and post them to the UI thread.
        """
        worker = get_current_worker()

        try:
            for event in self.engine.run(message):
                if worker.is_cancelled:
                    self.post_message(StreamComplete(0, 0, "interrupted", interrupted=True))
                    return

                match event:
                    case TextDelta(text=text):
                        self.post_message(StreamChunk(text))
                    case ToolCallStart(tool_use_id=tid, name=name, input=inp):
                        self.post_message(ToolStart(tid, name, inp))
                    case ToolCallResult(tool_use_id=tid, name=name, content=content, is_error=err):
                        self.post_message(ToolResult(tid, name, content, err))
                    case TurnComplete(input_tokens=it, output_tokens=ot, stop_reason=sr):
                        self.post_message(StreamComplete(it, ot, sr))
        except Exception as e:
            log.exception("Engine error")
            self.call_from_thread(self._show_error, str(e))
            self.post_message(StreamComplete(0, 0, "error", interrupted=True))

    def on_stream_chunk(self, event: StreamChunk) -> None:
        """UI thread: a text chunk arrived. Append it to the streaming widget."""
        conv = self.query_one("#conversation", Conversation)
        # Create streaming widget on first text chunk
        if self._streaming is None:
            self._streaming = conv.begin_streaming()
        self._stream_text += event.text
        self._streaming.append(event.text)

    def on_tool_start(self, event: ToolStart) -> None:
        """UI thread: a tool is about to be called.

        Finalise any in-progress text streaming first. Store the args
        so we can display them when the result arrives.
        """
        self._finalise_streaming()
        # Store args keyed by tool_use_id for display when result arrives
        self._pending_tool_args[event.tool_use_id] = event.input

    def on_tool_result(self, event: ToolResult) -> None:
        """UI thread: a tool finished. Show it in the conversation."""
        conv = self.query_one("#conversation", Conversation)
        args = self._pending_tool_args.pop(event.tool_use_id, {})
        conv.add_tool_call(event.name, args, event.content, event.is_error)

    def on_stream_complete(self, event: StreamComplete) -> None:
        """UI thread: engine finished processing. Finalise everything."""
        self._finalise_streaming()

        # Update status bar with token counts from the full engine turn
        self._update_status(event.input_tokens, event.output_tokens)

        # Re-enable input and give it focus
        self._stream_worker = None
        inp = self.query_one("#input", MessageInput)
        inp.disabled = False
        inp.focus()

    # --- UI helpers ---

    def _finalise_streaming(self) -> None:
        """Finalise the current streaming widget if one exists."""
        if self._streaming is None:
            return
        conv = self.query_one("#conversation", Conversation)
        if self._stream_text:
            conv.finalise_streaming(self._streaming)
        else:
            self._streaming.remove()
        self._streaming = None
        self._stream_text = ""

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
        # Recreate engine with new session
        self.engine = Engine(
            llm_client=self.llm,
            session=self.session,
            tool_registry=self.tool_registry,
            system_prompt=self.config.system_prompt,
        )
        conv = self.query_one("#conversation", Conversation)
        conv.remove_children()
        self._update_status()
