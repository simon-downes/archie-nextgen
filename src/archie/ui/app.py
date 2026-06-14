"""Main Textual application.

This is the UI layer — it handles display and user interaction only.
All orchestration logic (LLM calls, tool dispatch, the tool-use loop)
lives in the AgentLoop. The app runs the agent in a background thread
and reacts to events it emits via a callback.

Threading model:
- Textual runs an asyncio event loop on the main thread
- Each turn runs the synchronous AgentLoop.run_turn() on a worker thread
- Events are marshalled from the worker to the UI via call_from_thread
- This keeps the UI responsive while waiting for LLM responses and tool execution
"""

import atexit
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.widgets import Footer, TabbedContent, TabPane

from archie.agent import (
    AgentEvent,
    AgentLoop,
    TextDeltaEvent,
    ToolFinished,
    ToolStarted,
    TurnComplete,
    TurnError,
    TurnInterrupted,
    UsageUpdated,
)
from archie.artifact_store import ArtifactStore
from archie.config import load_config
from archie.llm import BedrockClient
from archie.logs import log_event
from archie.models import get_model_info
from archie.project import detect_project_dir
from archie.prompt import SYSTEM_PROMPT
from archie.sandbox import Sandbox
from archie.session import Session
from archie.tools import create_default_registry
from archie.ui.commands import ArchieCommands
from archie.ui.conversation import Conversation, StreamingMessage
from archie.ui.input import MessageInput
from archie.ui.status import StatusBar, detect_git_branch
from archie.ui.throbber import Throbber

log = logging.getLogger(__name__)


# --- Custom Textual Messages ---
# Only ShellResult remains — it's independent of the agent loop.


class ShellResult(Message):
    """Result of a user-initiated ! shell command. Posted from the worker thread."""

    def __init__(self, command: str, output: str, exit_code: int) -> None:
        super().__init__()
        self.command = command
        self.output = output
        self.exit_code = exit_code


class ArchieApp(App):
    """The main Textual application. Manages the UI and drives the AgentLoop."""

    TITLE = "Archie"
    CSS_PATH = "archie.tcss"
    COMMANDS = {ArchieCommands}

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+n", "new_session", "New Session"),
        Binding("ctrl+g", "editor", "Editor"),
        Binding("ctrl+c", "copy_block", "Copy Block"),
    ]

    def __init__(self) -> None:
        super().__init__()

        self.config = load_config()
        self.model_info = get_model_info(self.config.model)
        self.llm = BedrockClient(
            model_id=self.config.model,
            # Models with an in-region-only endpoint pin their own region;
            # geo-inference models fall back to the configured session region.
            region=self.model_info.region or self.config.region,
            max_output_tokens=self.model_info.max_output_tokens,
        )
        self.project_dir = detect_project_dir(Path.cwd(), self.config.project_root)

        self._build_stack()

        # Single atexit handler — always destroys the current sandbox
        atexit.register(lambda: self.sandbox.destroy())

        # UI state
        self._streaming: StreamingMessage | None = None
        self._stream_text: str = ""
        self._turn_active: bool = False
        self._turn_count: int = 0
        self._throbber: Throbber | None = None
        self._git_branch = detect_git_branch(self.project_dir)
        # Incremented on new_session — stale events from old agents are dropped
        self._agent_generation: int = 0

    def _build_stack(self) -> None:
        """Create the session, sandbox, tools, and agent. Called from __init__ and new_session."""
        self.session = Session(
            model_id=self.config.model,
            model_info=self.model_info,
            project_name=self.project_dir.name,
        )
        log_event(
            log,
            logging.INFO,
            "session_start",
            session=self.session.session_id,
            model=self.config.model,
            project=self.project_dir.name,
        )
        self.sandbox = Sandbox(
            config=self.config.sandbox,
            project_dir=self.project_dir,
            session_id=self.session.session_id,
            username=os.environ.get("USER", "archie"),
            uid=os.getuid(),
        )
        allowed = [Path(p) for p in self.config.tools.allowed_directories]
        allowed.append(Path.home() / ".archie")
        self.artifact_store = ArtifactStore()
        # Shared between read_file (populates), write/edit tools (invalidate on
        # modification) and AgentLoop (invalidates on context eviction).
        self.mtime_cache: dict[tuple[str, int, int], tuple[float, str]] = {}
        # Shared between edit/write tools (populate) and AgentLoop (consume for UI diffs).
        self.pre_content_stash: dict[str, str] = {}
        self.tool_registry = create_default_registry(
            self.project_dir,
            allowed,
            self.sandbox,
            brain_dir=self.config.brain_dir,
            artifact_store=self.artifact_store,
            session_id_fn=lambda: self.session.session_id,
            mtime_cache=self.mtime_cache,
            pre_content_stash=self.pre_content_stash,
        )
        self._agent = AgentLoop(
            llm_client=self.llm,
            session=self.session,
            tool_registry=self.tool_registry,
            system_prompt=SYSTEM_PROMPT,
            emit=self._on_agent_event,
            sandbox=self.sandbox,
            artifact_store=self.artifact_store,
            mtime_cache=self.mtime_cache,
            cwd=self.project_dir,
            pre_content_stash=self.pre_content_stash,
        )

    def compose(self) -> ComposeResult:
        with TabbedContent():
            with TabPane("Session 1"):
                yield Conversation(id="conversation")
                yield StatusBar(id="status")
                yield MessageInput(id="input")
        yield Footer()

    def on_mount(self) -> None:
        self._update_status()
        self.query_one("#input", MessageInput).focus()

    # --- Agent event callback (runs on worker thread) ---

    def _on_agent_event(self, event: AgentEvent) -> None:
        """Marshal agent events to the main thread for widget updates.

        Checks that the event came from the current agent — stale events from a
        previous agent (after new_session) are silently dropped.
        """
        gen = self._agent_generation
        self.call_from_thread(self._handle_event, event, gen)

    def _handle_event(self, event: AgentEvent, generation: int) -> None:
        """Dispatch one agent event to the appropriate widget update."""
        if generation != self._agent_generation:
            return  # Stale event from a previous session's agent
        conv = self.query_one("#conversation", Conversation)

        if isinstance(event, TextDeltaEvent):
            self._remove_throbber()
            conv.end_iteration()
            if self._streaming is None:
                self._streaming = conv.begin_streaming()
            self._stream_text += event.text
            self._streaming.append(event.text)
            conv.scroll_end(animate=False)
            # Update streaming output estimate in status bar
            self.query_one("#status", StatusBar).update_output_estimate(len(self._stream_text))

        elif isinstance(event, ToolStarted):
            self._remove_throbber()
            self._finalise_streaming()
            conv.add_tool_pending(event.tool_use_id, event.ui_summary)

        elif isinstance(event, ToolFinished):
            conv.complete_tool(event.tool_use_id, event.ui_summary, event.is_error, event.ui_detail)
            self._throbber = Throbber()
            conv.mount(self._throbber)
            conv.scroll_end(animate=False)

        elif isinstance(event, UsageUpdated):
            self._update_status_from_event(event)

        elif isinstance(event, TurnComplete):
            self._end_turn()

        elif isinstance(event, TurnInterrupted):
            conv.add_error("[interrupted]")
            self._end_turn()

        elif isinstance(event, TurnError):
            self._show_error(event.message)
            self._end_turn()

    # --- Message flow: User submits → Agent processes ---

    def on_message_input_submitted(self, event: MessageInput.Submitted) -> None:
        content = event.content

        # ! prefix: user shell command (independent of agent)
        if content.startswith("!"):
            command = content[1:].strip()
            if not command:
                return
            self.run_worker(lambda: self._run_user_shell(command), thread=True)
            return

        if self._turn_active:
            return

        self._turn_active = True
        conv = self.query_one("#conversation", Conversation)
        conv.add_user_message(content)

        self._throbber = Throbber()
        conv.mount(self._throbber)
        conv.scroll_end(animate=False)

        self.query_one("#input", MessageInput).disabled = True
        self._stream_text = ""
        self._streaming = None

        # Run the agent loop on a worker thread. It emits events via the callback.
        self.run_worker(lambda: self._agent.run_turn(content), thread=True, exit_on_error=False)

    def _run_user_shell(self, command: str) -> None:
        """Background thread: run a user ! command in the sandbox."""
        try:
            output, exit_code = self.sandbox.exec(command)
            self.post_message(ShellResult(command, output, exit_code))
        except Exception as e:
            log.exception("User shell command failed")
            self.call_from_thread(self._show_error, f"Shell error: {e}")

    def on_shell_result(self, event: ShellResult) -> None:
        conv = self.query_one("#conversation", Conversation)
        conv.add_shell_output(event.command, event.output, event.exit_code)

    # --- UI helpers ---

    def _end_turn(self) -> None:
        """Single teardown path for every turn outcome."""
        self._remove_throbber()
        self._finalise_streaming()
        conv = self.query_one("#conversation", Conversation)
        conv.end_iteration()
        self._turn_active = False
        self._update_status()
        inp = self.query_one("#input", MessageInput)
        inp.disabled = False
        inp.focus()

        # Trigger memory extraction every N turns
        self._turn_count += 1
        if self._turn_count >= self.config.memory.extraction_interval:
            self._turn_count = 0
            self.run_worker(self._run_memory_extraction, thread=True)

    def _finalise_streaming(self) -> None:
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
        if self._throbber is not None:
            self._throbber.remove()
            self._throbber = None

    def _show_error(self, message: str) -> None:
        conv = self.query_one("#conversation", Conversation)
        conv.add_error(message)

    def _update_status(self) -> None:
        """Push current stats to the status bar widget."""
        status = self.query_one("#status", StatusBar)
        status.project_name = self.project_dir.name
        status.git_branch = self._git_branch
        status.model_name = self.model_info.name
        status.session_input = self.session.total_input_tokens
        status.session_output = self.session.total_output_tokens
        status.cache_read = self.session.total_cache_read_tokens
        status.cache_write = self.session.total_cache_write_tokens
        status.context_pct = self.session.context_pct
        status.cost = self.session.total_cost
        status.warning = self.session.context_warning

    def _update_status_from_event(self, event: UsageUpdated) -> None:
        """Update status bar from agent's UsageUpdated event."""
        status = self.query_one("#status", StatusBar)
        status.session_input = event.input_tokens
        status.session_output = event.output_tokens
        status.cache_read = event.cache_read_tokens
        status.cache_write = event.cache_write_tokens
        status.cost = event.cost
        status.context_pct = self.session.context_pct
        status.warning = self.session.context_warning
        status.clear_estimate()

    def _run_memory_extraction(self) -> None:
        self._extract_memory()

    def _extract_memory(self) -> None:
        """Best-effort memory extraction. Called on quit and periodically between turns."""
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
        except Exception:  # noqa: BLE001
            log.warning("Memory extraction failed", exc_info=True)

    # --- Actions ---

    def action_copy_block(self) -> None:
        focused = self.focused
        if focused is not None and hasattr(focused, "get_copy_text"):
            text = focused.get_copy_text()
            if text:
                self.copy_to_clipboard(text)
                self.notify("Copied to clipboard")

    def action_cancel(self) -> None:
        """Esc — signal the agent to interrupt and kill any running sandbox command."""
        if self._turn_active:
            self._agent.interrupt()
            self.sandbox.cancel()

    def action_editor(self) -> None:
        """Ctrl+G — compose the prompt in $EDITOR for long/multi-line input.

        Seeds a tempfile with current input. Detects save vs quit-without-save via mtime.
        Saving non-empty content auto-submits; saving empty clears input; quitting leaves it.
        """
        if self._turn_active:
            return

        prompt_input = self.query_one("#input", MessageInput)
        current_text = prompt_input.text
        editor = os.environ.get("EDITOR", "vi")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(current_text)
            tmpfile = f.name

        seeded_mtime = Path(tmpfile).stat().st_mtime

        with self.suspend():
            try:
                subprocess.run([editor, tmpfile])
            except FileNotFoundError:
                Path(tmpfile).unlink(missing_ok=True)
                self._show_error(f"Editor not found: {editor}")
                return

        path = Path(tmpfile)
        saved = path.stat().st_mtime != seeded_mtime
        content = path.read_text()
        path.unlink(missing_ok=True)

        if not saved:
            return

        text = content.strip()
        if text:
            prompt_input.clear()
            prompt_input.post_message(MessageInput.Submitted(text))
        else:
            prompt_input.clear()

    async def action_quit(self) -> None:
        self._extract_memory()
        self.sandbox.destroy()
        await super().action_quit()

    def action_new_session(self) -> None:
        """Ctrl+N — start a fresh conversation."""
        if self._turn_active:
            self._agent.interrupt()
            self.sandbox.cancel()
            self._turn_active = False
            self._streaming = None
            self._stream_text = ""

        self._agent_generation += 1
        self.sandbox.destroy()
        self._build_stack()
        self._turn_count = 0

        conv = self.query_one("#conversation", Conversation)
        conv.remove_children()
        self._update_status()

    def on_exception(self, error: Exception) -> None:
        """Log unhandled exceptions before Textual crashes.

        Without this, exceptions in widgets (e.g. Markdown rendering failures)
        vanish silently or only appear briefly on stderr after the TUI exits.
        """
        log_event(log, logging.ERROR, "unhandled_exception", exc_info=True, error=str(error)[:500])

    def switch_model(self, model_id: str) -> None:
        """Switch to a different model mid-session. Takes effect on the next turn.

        Updates the LLM client, session pricing, and status bar. History and sandbox
        are preserved — no restart needed.
        """
        from archie.models import get_model_info

        self.model_info = get_model_info(model_id)
        self.llm.model_id = model_id
        self.session.model_info = self.model_info
        self.query_one("#status", StatusBar).model_name = self.model_info.name
        self.notify(f"Switched to {self.model_info.name}")
