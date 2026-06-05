"""CLI entry point.

Click is used to define the command-line interface. The `main` group allows
subcommands (e.g. `archie chat`, `archie config` in future). The entry point
is registered in pyproject.toml under [project.scripts] so `uv run archie`
or just `archie` (when installed) invokes `main()`.
"""

import click


@click.group()
def main():
    """Archie — Personal AI agent harness."""


@main.command()
def chat():
    """Start an interactive chat session.

    Loads config, creates the Textual app, and runs it.
    Config/model errors are caught and displayed as clean CLI errors
    rather than raw tracebacks.
    """
    from archie.ui.app import ArchieApp

    try:
        app = ArchieApp()
    except (KeyError, ValueError) as e:
        # KeyError: unknown model ID in config
        # ValueError: missing/malformed config file
        raise click.ClickException(str(e)) from None
    app.run()
