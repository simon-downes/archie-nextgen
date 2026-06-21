# Plan 024: Model Picker Screen

## Objective

Remove model switching from `ArchieCommands` and replace it with a dedicated searchable model
picker. Opening the picker (Ctrl+M) shows **only** a searchable list of models — no intermediate
menu, no "New Session"/"Quit" entries. New Session and Quit remain accessible via their existing
bindings (`Ctrl+N`, `Ctrl+Q`).

## Context

- Current `ArchieCommands.discover()` yields ~10 Hits: one per model (`"Change Model → Claude Fable 5"`) plus "New Session" and "Quit".
- Textual's `Hit` API does not support hierarchical/nested choices — confirmed.
- The command palette (Ctrl+P) exists today but ArchieCommands is only used for model switching + two actions that already have dedicated keybindings.
- Stripping ArchieCommands to just these three items leaves a pointless palette — better to remove it entirely and use a single-purpose Screen.

## Verified API Surface

All signatures confirmed against installed Textual v8.1.0:

```python
# Option dataclass (frozen) from textual.widgets._option_list import Option
#   prompt:  VisualType    — Rich-markup text shown in the list
#   id:      str | None    — unique identifier (used to store model_id)
#   disabled: bool          — greyed-out, not selectable

Option(prompt="[bold]Model Name[/]", id="model.id", disabled=False)

# OptionList methods (use clear_options + add_options for the fundamental API):
opt_list.clear_options()                                    # empty the list
opt_list.add_options([Option(prompt, id=model_id) for ...])  # repopulate
opt_list.highlighted = 0                                    # highlight first item
evt.option.id                                               # access from event
```

## Requirements

### Must

- MUST present a searchable list of all MODELS when the picker opens (Ctrl+M)
  - AC: an `Input` widget at the top for typing search terms
  - AC: `OptionList` below showing all models, filtered by model name in real time
  - AC: pressing Escape dismisses the screen entirely (goes back to app)
- MUST show the model's display name and context window size on each entry
  - AC: each option prompt renders as two lines: `[bold]{info.name}[/]` on line one,
    `{info.max_context_tokens:,} ctx` on line two (dim)
- MUST highlight the currently active model with a `✓` prefix in bold green
- MUST preserve existing `switch_model()` behaviour — no logic changes to model switching
  - AC: selecting a model still calls `app.switch_model(model_id)` which updates LLM client, session pricing, and status bar
- MUST remove `ArchieCommands` entirely (no COMMANDS class variable on ArchieApp)

### Should

- SHOULD use arrow keys / mouse to navigate between models in the list
- SHOULD keep the screen small and centred (~24 rows × 50 cols) with a dark overlay scrim
- SHOULD filter by model name only (case-insensitive substring match on `info.name`)
- SHOULD clear search input when the screen opens so all models are shown by default

### Nice to Have

- Show output token limit alongside context window for quick comparison
- Keyboard shortcut within picker: `C` to jump focus to the Input

## Technical Design

### Widget hierarchy

```
ModelPickerScreen(ModalScreen)
└── Container(id="picker-box", classes="overlay")
    ├── Label(id="title", text="Archie — Select Model")
    ├── Input(id="search", placeholder="Search models…")
    └── OptionList(id="models")     ← single list, repopulated on search
```

One Container (centred overlay), one Input, one OptionList. No widget toggling or CSS visibility tricks — just clear + add to update the list contents.

### Screen implementation outline

```python
# src/archie/ui/model_picker.py

from textual.screen import ModalScreen
from textual.binding import Binding
from textual.containers import Container
from textual.widgets import Label, OptionList, Input

from archie.models import MODELS


class ModelPickerScreen(ModalScreen[bool]):
    """Searchable model picker — opens with Ctrl+M."""

    BINDINGS = [
        Binding("escape", "dismiss(False)", "Back"),
    ]

    def compose(self) -> ComposeResult:
        yield Container(
            Label("[bold]Archie — Select Model[/]", id="title"),
            Input(placeholder="Search models…", id="search"),
            OptionList(id="models"),
            id="picker-box",
        )

    def on_mount(self) -> None:
        self._populate_all()
        self.query_one("#search", Input).focus()

    def _all_options(self) -> list[Option]:
        current = self.app.config.model
        opts = []
        for model_id, info in MODELS.items():
            prefix = "[bold green]✓ [/green]" if model_id == current else ""
            prompt = (
                f"[bold]{prefix}{info.name}[/]\n"
                f"[dim]{info.max_context_tokens:,} ctx[/]"
            )
            opts.append(Option(prompt, id=model_id))
        return opts

    def _populate_all(self) -> None:
        models_list = self.query_one("#models", OptionList)
        models_list.clear_options()
        models_list.add_options(self._all_options())

    # --- Event handlers ──────────────────────────────────────────────

    def on_input_changed(self, event: Input.Changed) -> None:
        query = event.value.strip().lower()
        models_list = self.query_one("#models", OptionList)
        if not query:
            models_list.clear_options()
            models_list.add_options(self._all_options())
            return

        filtered = []
        current = self.app.config.model
        for model_id, info in MODELS.items():
            if query not in info.name.lower():
                continue
            prefix = "[bold green]✓ [/green]" if model_id == current else ""
            prompt = (
                f"[bold]{prefix}{info.name}[/]\n"
                f"[dim]{info.max_context_tokens:,} ctx[/]"
            )
            filtered.append(Option(prompt, id=model_id))
        models_list.clear_options()
        models_list.add_options(filtered)

    def _on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        model_id = event.option.id
        if model_id and model_id in MODELS:
            self.app.switch_model(model_id)
            self.notify(f"Switched to {MODELS[model_id].name}")
            self.dismiss(True)
```

### CSS (in archie.tcss)

```css
ModelPickerScreen {
    align: center middle;
}

#picker-box {
    width: 50%;
    height: 24;
    background: $surface;
    border: solid $accent;
    border-radius: 1;
    padding: 1 2;
    margin: 1;
}

#title {
    text-align: center;
    width: 100%;
    text-style: bold;
}

#search {
    width: 100%;
    margin-top: 1;
    margin-bottom: 1;
}

#models {
    width: 100%;
    height: 1fr;
}
```

### Integration with ArchieApp

```python
# In app.py — replace COMMANDS class variable:

from archie.ui.model_picker import ModelPickerScreen

class ArchieApp(App):
    BINDINGS = [
        Binding("ctrl+q", "action_quit", "Quit"),
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+n", "action_new_session", "New Session"),
        Binding("ctrl+m", "action_show_model_picker", "Model Picker"),
    ]

    # COMMANDS = {ArchieCommands}  — REMOVED entirely

    def action_show_model_picker(self) -> None:
        self.push_screen(ModelPickerScreen())
```

## Milestones

### 1. Scaffold: create ModelPickerScreen with unfiltered model list

Tasks:
- Create `src/archie/ui/model_picker.py`
- Implement `ModalScreen[bool]` subclass composing Container → Label + Input + OptionList
- Implement `_all_options()` to build Option entries (name + context window, ✓ for active)
- Add `Ctrl+M` binding + `action_show_model_picker()` in app.py
- Remove `COMMANDS = {ArchieCommands}` from app.py (don't set empty dict — just remove it; Textual defaults to no commands)
- Wire selection → `app.switch_model(model_id)` + dismiss

Deliverable: Opens on Ctrl+M, shows all models with ✓ and context size, selectable.

### 2. Search filtering

Tasks:
- Bind `on_input_changed` to filter `_all_options()` by model name substring (case-insensitive)
- Clear the list (show all) when input is empty
- Verify focus lands in Input on screen open (`on_mount`)

Deliverable: Real-time search filtering works correctly.

### 3. CSS, Polish, and cleanup

Tasks:
- Style via TCSS: centred overlay container (~24 rows × 50 cols), solid bg, border, padding
- Verify Escape dismisses the screen (via Binding, no extra handler needed)
- Verify arrow-key + mouse navigation works
- Remove `ArchieCommands` class entirely from `src/archie/ui/commands.py` (no longer used)
- Confirm model switching, New Session (`Ctrl+N`), Quit (`Ctrl+Q`) all work end-to-end

Deliverable: Refactor complete — single-purpose searchable model picker via Ctrl+M. No CommandPalette needed.
