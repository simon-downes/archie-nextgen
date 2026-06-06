"""Command palette provider for Archie.

Textual's built-in CommandPalette is triggered by Ctrl+P. We provide a
custom Provider that surfaces Archie-specific commands: model switching,
new session, and quit.

The Provider class implements two methods:
- discover(): yields all commands (shown when palette opens with no query)
- search(): filters commands as the user types (fuzzy matching via matcher)

"Change Model" is implemented as a two-step flow:
1. User selects "Change Model" from the palette
2. We iterate available models from MODELS dict and switch to the selected one
   by updating the config, creating a new LLM client, and starting a new session
"""

from textual.command import Hit, Hits, Provider

from archie.models import MODELS


class ArchieCommands(Provider):
    """Command palette provider exposing Archie actions.

    Registered on ArchieApp via COMMANDS = {ArchieCommands}. Textual
    automatically discovers this and includes it in the Ctrl+P palette.
    """

    async def discover(self) -> Hits:
        """Yield all commands — shown when the palette first opens."""
        # Model switching commands — one per available model
        for model_id, info in MODELS.items():
            yield Hit(
                1.0,
                f"Change Model → {info.name}",
                self._make_change_model(model_id),
                help=f"Switch to {info.name} (starts new session)",
            )
        # Session management
        yield Hit(
            0.5,
            "New Session",
            self._action_new_session,
            help="Start a fresh conversation",
        )
        yield Hit(
            0.5,
            "Quit",
            self._action_quit,
            help="Exit Archie",
        )

    async def search(self, query: str) -> Hits:
        """Filter commands as the user types — uses Textual's fuzzy matcher."""
        matcher = self.matcher(query)

        # Model switching commands
        for model_id, info in MODELS.items():
            label = f"Change Model → {info.name}"
            score = matcher.match(label)
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(label),
                    self._make_change_model(model_id),
                    help=f"Switch to {info.name} (starts new session)",
                )

        # Session management
        for label, callback, help_text in [
            ("New Session", self._action_new_session, "Start a fresh conversation"),
            ("Quit", self._action_quit, "Exit Archie"),
        ]:
            score = matcher.match(label)
            if score > 0:
                yield Hit(score, matcher.highlight(label), callback, help=help_text)

    def _make_change_model(self, model_id: str):
        """Return a callback that switches to the given model.

        Uses a closure to capture model_id — each model gets its own callback.
        """

        async def _change() -> None:
            from archie.ui.app import ArchieApp

            app = self.app
            assert isinstance(app, ArchieApp)
            app.switch_model(model_id)

        return _change

    async def _action_new_session(self) -> None:
        """Delegate to the app's existing new session action."""
        self.app.action_new_session()

    async def _action_quit(self) -> None:
        """Delegate to the app's existing quit action."""
        await self.app.action_quit()
