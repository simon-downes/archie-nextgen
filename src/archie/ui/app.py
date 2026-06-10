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

import atexit
import logging
import os
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.widgets import Footer, TabbedContent, TabPane
from textual.worker import Worker, get_current_worker

from archie.artifact_store import ArtifactStore
from archie.config import Config, load_config
from archie.engine import Engine
from archie.llm import BedrockClient
from archie.models import get_model_info
from archie.project import detect_project_dir
from archie.prompt import SYSTEM_PROMPT
from archie.sandbox import Sandbox
from archie.session import Session
from archie.tools import create_default_registry
from archie.types import TextDelta, ToolCallResult, ToolCallStart, TurnComplete
from archie.ui.commands import ArchieCommands
from archie.ui.conversation import Conversation, StreamingMessage
from archie.ui.input import MessageInput
from archie.ui.status import StatusBar, _detect_git_branch
from archie.ui.throbber import Throbber

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


class ShellResult(Message):
    """Result of a user-initiated ! shell command. Posted from the worker thread."""

    def __init__(self, command: str, output: str, exit_code: int) -> None:
        super().__init__()
        self.command = command
        self.output = output
        self.exit_code = exit_code


class ArchieApp(App):
    """The main Textual application. Manages the UI and drives the Engine."""

    TITLE = "Archie"

    # External stylesheet for app-level theme overrides and composition.
    # Widget base styling stays in DEFAULT_CSS; this adds theme consistency,
    # tighter spacing, focus indicators, and layout overrides.
    CSS_PATH = "archie.tcss"

    # Command palette provider — Ctrl+P opens the palette with these commands.
    # Textual's built-in CommandPalette discovers providers from this set.
    COMMANDS = {ArchieCommands}

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+n", "new_session", "New Session"),
        Binding("ctrl+c", "copy_block", "Copy Block"),
    ]

    def __init__(self) -> None:
        super().__init__()

        # Load config and set up dependencies
        self.config = load_config()
        self.model_info = get_model_info(self.config.model)
        self.llm = BedrockClient(model_id=self.config.model, region=self.config.region)

        # detect_project_dir finds the project root (e.g. ~/dev/myproject)
        # even if archie was launched from a subdirectory within it.
        self.project_dir = detect_project_dir(Path.cwd(), self.config.project_root)

        self.session = Session(
            model_id=self.config.model,
            model_info=self.model_info,
            project_name=self.project_dir.name,
        )

        # Create tool registry with configured path access and sandbox.
        allowed = [Path(p) for p in self.config.tools.allowed_directories]

        # Create sandbox (lazy — container not started until first shell exec).
        # The sandbox is tied to the session and destroyed on quit/new session.
        self.sandbox = Sandbox(
            config=self.config.sandbox,
            project_dir=self.project_dir,
            session_id=self.session.session_id,
            username=os.environ.get("USER", "archie"),
            uid=os.getuid(),
        )

        # Registry includes the shell tool (bound to sandbox) so the model
        # can execute commands inside the container.
        self.artifact_store = ArtifactStore()
        self.tool_registry = create_default_registry(
            self.project_dir,
            allowed,
            self.sandbox,
            brain_dir=self.config.brain_dir,
            artifact_store=self.artifact_store,
        )

        # Create the engine — knows about sandbox so it can cancel running
        # commands when the user presses Esc.
        self.engine = Engine(
            llm_client=self.llm,
            session=self.session,
            tool_registry=self.tool_registry,
            system_prompt=SYSTEM_PROMPT,
            sandbox=self.sandbox,
            artifact_store=self.artifact_store,
        )

        # Safety net: destroy container on unexpected exit (e.g. crash, SIGTERM).
        # atexit handlers run when the Python interpreter exits normally.
        atexit.register(self.sandbox.destroy)

        # Streaming state
        self._streaming: StreamingMessage | None = None
        self._stream_worker: Worker | None = None
        self._stream_text: str = ""
        self._turn_count: int = 0  # Counts turns since last memory extraction

        # Thinking indicator — mounted in conversation while waiting for engine
        self._throbber: Throbber | None = None

        # Git branch detection — run once at startup on the host (not in container)
        self._git_branch = _detect_git_branch(self.project_dir)

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
        """Handle user pressing Enter in the input box.

        If the message starts with '!', it's a direct shell command — run it
        in the sandbox without involving the engine. This works even while
        model generation is in progress (independent of _stream_worker).
        """
        content = event.content

        # --- ! prefix: user shell command (independent of engine) ---
        if content.startswith("!"):
            command = content[1:].strip()
            if not command:
                return  # Bare "!" or "! " — ignore silently
            # Run in a worker thread so the UI stays responsive.
            # This is independent of _stream_worker — works during generation.
            self.run_worker(lambda: self._run_user_shell(command), thread=True)
            return

        if self._stream_worker is not None:
            return  # Already processing — ignore

        conv = self.query_one("#conversation", Conversation)
        conv.add_user_message(event.content)

        # Mount the thinking indicator — shows animated gradient bar while
        # waiting for the engine to produce its first event
        self._throbber = Throbber()
        conv.mount(self._throbber)
        conv.scroll_end(animate=False)

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

    def _run_user_shell(self, command: str) -> None:
        """Background thread: run a user ! command in the sandbox.

        Posts a ShellResult message on success or shows an error on failure.
        This is independent of the engine — runs in its own worker so it
        works even while model generation is active.
        """
        try:
            # exec() calls ensure_running() internally (lazy container start)
            output, exit_code = self.sandbox.exec(command)
            self.post_message(ShellResult(command, output, exit_code))
        except Exception as e:
            log.exception("User shell command failed")
            self.call_from_thread(self._show_error, f"Shell error: {e}")

    def on_shell_result(self, event: ShellResult) -> None:
        """UI thread: a user ! command finished. Display the result."""
        conv = self.query_one("#conversation", Conversation)
        conv.add_shell_output(event.command, event.output, event.exit_code)

    def on_stream_chunk(self, event: StreamChunk) -> None:
        """UI thread: a text chunk arrived. Append it to the streaming widget."""
        self._remove_throbber()
        conv = self.query_one("#conversation", Conversation)
        # Create streaming widget on first text chunk
        if self._streaming is None:
            self._streaming = conv.begin_streaming()
        self._stream_text += event.text
        self._streaming.append(event.text)
        # Keep conversation scrolled to bottom as content grows —
        # without this, the streaming widget grows below the visible area.
        conv.scroll_end(animate=False)

    def on_tool_start(self, event: ToolStart) -> None:
        """UI thread: a tool is about to be called.

        Finalise any in-progress text streaming first. Mount a ToolCallMessage
        in pending state (⌛) — it will be updated when the result arrives.

        We also update the status bar here because the session has already
        recorded the LLM call's token usage (add_turn happens in the engine
        before yielding ToolCallStart). This gives progressive cost feedback
        during multi-tool turns rather than waiting until the entire turn ends.
        """
        self._remove_throbber()
        self._finalise_streaming()
        # Mount the tool block immediately in pending state
        conv = self.query_one("#conversation", Conversation)
        conv.mount_tool_pending(event.tool_use_id, event.name, event.input)
        # Update metrics progressively (session totals are already updated)
        self._update_status()

    def on_tool_result(self, event: ToolResult) -> None:
        """UI thread: a tool finished. Update the pending block with result."""
        conv = self.query_one("#conversation", Conversation)
        conv.update_tool_result(event.tool_use_id, event.content, event.is_error)

    def on_stream_complete(self, event: StreamComplete) -> None:
        """UI thread: engine finished processing. Finalise everything."""
        self._remove_throbber()
        self._finalise_streaming()

        # Warn the user if the engine hit its iteration cap
        if event.stop_reason == "max_iterations":
            self._show_error(
                "Reached tool call limit (50 iterations). "
                "The response may be incomplete. Try breaking the task into smaller steps."
            )

        # Flush interrupted turns — the engine was abandoned mid-loop so it
        # couldn't flush itself. We grab whatever it accumulated and write it.
        if event.interrupted and self.engine.current_turn_log is not None:
            self.engine.current_turn_log.interrupted = True
            self.engine.current_turn_log.assistant_text = (
                self.engine.current_turn_log.assistant_text
                or "Response was interrupted by the user"
            )
            self.session.flush_turn(self.engine.current_turn_log)
            self.engine.current_turn_log = None

        # Update status bar with token counts from the full engine turn
        self._update_status(event.input_tokens, event.output_tokens)

        # Re-enable input and give it focus
        self._stream_worker = None
        inp = self.query_one("#input", MessageInput)
        inp.disabled = False
        inp.focus()

        # Trigger memory extraction every N turns (fire-and-forget in background)
        self._turn_count += 1
        if self._turn_count >= self.config.memory.extraction_interval:
            self._turn_count = 0
            self.run_worker(self._run_memory_extraction, thread=True)

    def _run_memory_extraction(self) -> None:
        """Background thread: extract memory fragments from recent turns."""
        try:
            from archie.memory import MemoryExtractor

            memory_dir = self.config.brain_dir / "_memory"
            if not memory_dir.exists():
                return
            extractor = MemoryExtractor(
                brain_dir=self.config.brain_dir,
                extraction_model=self.config.memory.extraction_model,
                region=self.config.region,
            )
            extractor.extract_all()
        except Exception:  # noqa: BLE001 — background extraction failure is non-fatal
            log.debug("Background memory extraction failed", exc_info=True)

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

    def _remove_throbber(self) -> None:
        """Remove the thinking indicator if it's still mounted.

        Called on the first event from the engine — any event means work
        has started and the throbber should disappear.
        """
        if self._throbber is not None:
            self._throbber.remove()
            self._throbber = None

    def _show_error(self, message: str) -> None:
        """Display an error as a styled block in the conversation."""
        conv = self.query_one("#conversation", Conversation)
        conv.add_error(message)

    def _update_status(self, turn_input: int = 0, turn_output: int = 0) -> None:
        """Push current stats to the status bar widget."""
        status = self.query_one("#status", StatusBar)
        status.project_name = self.project_dir.name
        status.git_branch = self._git_branch
        status.model_name = self.model_info.name
        status.turn_input = turn_input
        status.turn_output = turn_output
        status.total_input = self.session.total_input_tokens
        status.total_output = self.session.total_output_tokens
        status.context_pct = self.session.context_pct
        status.cost = self.session.total_cost
        status.warning = self.session.context_warning

    # --- Actions (bound to key presses via BINDINGS) ---

    def action_copy_block(self) -> None:
        """Ctrl+C pressed — copy the focused block's text to clipboard.

        Checks if the currently focused widget has a get_copy_text() method.
        If so, copies its text content to the system clipboard and shows
        a toast notification. If no block is focused, does nothing.
        """
        focused = self.focused
        if focused is not None and hasattr(focused, "get_copy_text"):
            text = focused.get_copy_text()
            if text:
                self.copy_to_clipboard(text)
                self.notify("Copied to clipboard")

    def action_cancel(self) -> None:
        """Esc pressed — cancel in-progress generation.

        Cancels the worker thread AND kills any running shell command in the
        sandbox. Both are needed: the worker cancellation stops the engine loop,
        and sandbox.cancel() kills the docker exec process so exec() returns
        immediately instead of blocking until the command finishes.
        """
        if self._stream_worker is not None:
            self._stream_worker.cancel()
            self.sandbox.cancel()

    async def action_quit(self) -> None:
        """Ctrl+Q pressed — extract memory, destroy sandbox, then exit.

        Extracts any remaining unprocessed turns before quitting so memory
        is up to date for the next session. Best-effort with 5s timeout.
        """
        # Best-effort memory extraction on quit
        try:
            from archie.memory import MemoryExtractor

            memory_dir = self.config.brain_dir / "_memory"
            if memory_dir.exists():
                extractor = MemoryExtractor(
                    brain_dir=self.config.brain_dir,
                    extraction_model=self.config.memory.extraction_model,
                    region=self.config.region,
                )
                extractor.extract_all()
        except Exception:  # noqa: BLE001 — don't block quit on extraction failure
            pass

        self.sandbox.destroy()
        await super().action_quit()

    def action_new_session(self) -> None:
        """Ctrl+N pressed — start a fresh conversation.

        Destroys the old sandbox container and creates a new lazy Sandbox
        instance tied to the new session (won't start until first shell exec).
        Recreates the registry and engine with the new sandbox reference.
        """
        # Cancel any in-progress generation — prevents the old worker from
        # posting messages after the conversation has been cleared.
        if self._stream_worker is not None:
            self._stream_worker.cancel()
            self.sandbox.cancel()
            self._stream_worker = None
            self._streaming = None
            self._stream_text = ""

        # Destroy the old session's container
        self.sandbox.destroy()

        # Create new session
        self.session = Session(
            model_id=self.config.model,
            model_info=self.model_info,
            project_name=self.project_dir.name,
        )

        # Create new sandbox tied to the new session
        self.sandbox = Sandbox(
            config=self.config.sandbox,
            project_dir=self.project_dir,
            session_id=self.session.session_id,
            username=os.environ.get("USER", "archie"),
            uid=os.getuid(),
        )
        # Re-register atexit for the new sandbox (backup cleanup)
        atexit.register(self.sandbox.destroy)

        # Recreate registry with the new sandbox (shell tool binds to it)
        allowed = [Path(p) for p in self.config.tools.allowed_directories]
        self.artifact_store = ArtifactStore()
        self.tool_registry = create_default_registry(
            self.project_dir,
            allowed,
            self.sandbox,
            brain_dir=self.config.brain_dir,
            artifact_store=self.artifact_store,
        )

        # Recreate engine with new session, registry, and sandbox
        self.engine = Engine(
            llm_client=self.llm,
            session=self.session,
            tool_registry=self.tool_registry,
            system_prompt=SYSTEM_PROMPT,
            sandbox=self.sandbox,
            artifact_store=self.artifact_store,
        )

        conv = self.query_one("#conversation", Conversation)
        conv.remove_children()
        self._update_status()

    def switch_model(self, model_id: str) -> None:
        """Switch to a different model and start a new session.

        Called from the command palette's "Change Model" command.
        Updates the LLM client with the new model ID, then delegates
        to action_new_session to reset everything cleanly.
        """
        from archie.models import get_model_info

        self.model_info = get_model_info(model_id)
        self.config = Config(
            model=model_id,
            region=self.config.region,
            project_root=self.config.project_root,
            tools=self.config.tools,
            sandbox=self.config.sandbox,
        )
        self.llm = BedrockClient(model_id=model_id, region=self.config.region)
        self.action_new_session()
        self.notify(f"Switched to {self.model_info.name}")
