# Plan 019 Implementation Review

## Status: Complete with issues

All 5 milestones delivered. Functional requirements met. Lint errors and minor
deviations from plan need fixing.

## Must Fix

| # | File | Line | Issue |
|---|------|------|-------|
| 1 | conversation.py | 53 | `ComposeResult` used as type annotation but not imported. Add `from textual.app import ComposeResult` to imports. |
| 2 | app.py | 62 | `BRIGHT_BLUE` and `BRIGHT_MAGENTA` imported from `archie.ui.colours` but unused. Remove this import line. |

## Should Fix

| # | File | Line | Issue |
|---|------|------|-------|
| 3 | conversation.py | 99, 152 | Assistant/Streaming headers use `[bright_blue]` (Rich named colour = ANSI 12, resolves to `#0000ff`) instead of `BRIGHT_BLUE` constant (`#6871ff` from colours.py). Plan requires using the palette constant. Fix: use `Text.assemble(("● Archie", Style(color=BRIGHT_BLUE, bold=True)))` pattern, same as UserMessage does. |
| 4 | app.py | 59 | `import time` is misplaced between archie.* imports. Move to stdlib block at top (after `from pathlib import Path`). Causes isort violation. |
| 5 | conversation.py | 55, 99, 152 | Unnecessary `f` prefix on strings with no interpolation (F541). Remove `f`. |
| 6 | conversation.py | 37 | Stale docstring says "▶ You in bold cyan" — should say "bright magenta". |
| 7 | conversation.py | 27 | `BRIGHT_BLUE` imported but unused because headers use the string literal instead of the constant. Will be resolved when fixing #3. |

## Notes

- UserMessage's DEFAULT_CSS still declares `padding: 1 2; margin: 1 0;` but is correctly
  overridden by TCSS (higher specificity). Plan says not to edit DEFAULT_CSS — acceptable.
- ErrorMessage (conversation.py:185) interpolates content into Rich markup
  (`f"[bold red]✗ Error[/]\n{content}"`) — pre-existing markup injection bug, not introduced
  by this plan. Should be fixed separately using Text.assemble.
- Total ruff errors: ~26 (mostly E402 from ProjectHeader class defined mid-imports in app.py,
  plus the F401/F541/F821 issues above).

## Milestone Checklist

| Milestone | Status | Notes |
|-----------|--------|-------|
| 1. Remove TabbedContent, add ProjectHeader | ✅ | Flat layout, no Tab imports remain |
| 2. Background inversion | ✅ | Screen=$surface, Conversation=black |
| 3. Conversation block styling | ✅ | No border-bottom, margin-bottom 1, UserMessage permanent left border |
| 4. Message header colours | ⚠️ | UserMessage correct (Text.assemble + BRIGHT_MAGENTA). AssistantMessage/StreamingMessage use wrong colour source (Rich named string vs palette constant) |
| 5. Double-esc clear input | ✅ | 500ms window, proper turn-active guard |
