"""CLI entry point.

Click is used to define the command-line interface. The `main` group allows
subcommands (e.g. `archie chat`, `archie build`). The entry point is
registered in pyproject.toml under [project.scripts] so `uv run archie`
or just `archie` (when installed) invokes `main()`.
"""

import os
import subprocess
import sys
from pathlib import Path

import click


def check_docker_available() -> None:
    """Verify Docker daemon is running. Exits with helpful error if not.

    Checks by running `docker info` — this confirms both that docker is
    installed and that the daemon is reachable (user is in docker group).
    """
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        raise click.ClickException(
            "Docker is not installed or not in PATH.\n"
            "Install Docker: https://docs.docker.com/get-docker/"
        ) from None
    if result.returncode != 0:
        raise click.ClickException(
            "Docker is not available. Ensure:\n"
            "  1. Docker daemon is running (sudo systemctl start docker)\n"
            "  2. Your user is in the docker group (sudo usermod -aG docker $USER)\n"
            f"\nDocker error: {result.stderr.strip()}"
        )


def check_sandbox_image(image: str) -> None:
    """Verify the sandbox image exists locally. Exits with helpful error if not.

    Args:
        image: Docker image name/tag to check for.
    """
    result = subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise click.ClickException(
            f"Sandbox image '{image}' not found.\nBuild it with: archie build"
        )


@click.group()
def main():
    """Archie — Personal AI agent harness."""


@main.command()
def chat():
    """Start an interactive chat session.

    Pre-flight checks verify Docker is available and the sandbox image
    exists before launching the TUI. This gives clear error messages
    instead of failing mid-session.
    """
    from archie.config import load_config

    try:
        config = load_config()
    except (KeyError, ValueError) as e:
        raise click.ClickException(str(e)) from None

    # Pre-flight checks: warn if Docker isn't available (shell tool won't work)
    try:
        check_docker_available()
        check_sandbox_image(config.sandbox.image)
    except click.ClickException as e:
        click.echo(f"⚠ {e.message}", err=True)
        click.echo("  Shell tool will not be available this session.\n", err=True)

    # Brain presence check — non-fatal warning if brain_dir doesn't exist.
    # Tells user to run `archie init` to create the brain structure.
    if not config.brain_dir.exists():
        click.echo(
            f"⚠ Brain directory not found: {config.brain_dir}\n"
            "  Run 'archie init' to create the brain structure.\n",
            err=True,
        )

    from archie.ui.app import ArchieApp

    try:
        app = ArchieApp()
    except (KeyError, ValueError) as e:
        raise click.ClickException(str(e)) from None
    app.run()


@main.command()
def build():
    """Build the sandbox Docker image.

    Builds sandbox/Dockerfile tagged as the configured image name.
    Passes the current user's UID and username as build args so that
    the container user matches the host user (avoids file permission issues).
    """
    from archie.config import load_config

    config = load_config()

    # Locate the sandbox/ directory relative to this source file.
    # This assumes we're running from the repo checkout (personal tool).
    sandbox_dir = Path(__file__).parents[2] / "sandbox"
    dockerfile = sandbox_dir / "Dockerfile"

    if not dockerfile.exists():
        raise click.ClickException(f"Dockerfile not found at {dockerfile}")

    username = os.environ.get("USER", "archie")
    uid = str(os.getuid())

    click.echo(f"Building sandbox image: {config.sandbox.image}")
    click.echo(f"  User: {username} (UID {uid})")
    click.echo(f"  Dockerfile: {dockerfile}")

    result = subprocess.run(
        [
            "docker",
            "build",
            "--tag",
            config.sandbox.image,
            "--build-arg",
            f"USERNAME={username}",
            "--build-arg",
            f"USER_UID={uid}",
            "-f",
            str(dockerfile),
            str(sandbox_dir),
        ],
        check=False,
    )

    if result.returncode != 0:
        sys.exit(result.returncode)

    click.echo(f"\n✓ Image '{config.sandbox.image}' built successfully.")


@main.command(name="init")
def init_brain():
    """Create the brain directory structure and initialise git.

    Idempotent: creates missing subdirectories and files without
    overwriting existing content. Safe to run multiple times.
    """
    from archie.config import load_config

    try:
        config = load_config()
    except (KeyError, ValueError) as e:
        raise click.ClickException(str(e)) from None

    brain_dir = config.brain_dir

    # Create subdirectories — exist_ok makes this idempotent
    for subdir in ("_memory", "projects", "knowledge", "people"):
        (brain_dir / subdir).mkdir(parents=True, exist_ok=True)

    # Create empty index.yaml if it doesn't exist
    index_path = brain_dir / "index.yaml"
    if not index_path.exists():
        index_path.write_text("{}\n")

    # Create .gitignore if it doesn't exist — keeps brain.db and
    # extraction watermarks out of version control
    gitignore_path = brain_dir / ".gitignore"
    if not gitignore_path.exists():
        gitignore_path.write_text("brain.db\n.last_extracted\n")

    # Initialise git repo if not already initialised
    git_dir = brain_dir / ".git"
    if not git_dir.exists():
        result = subprocess.run(
            ["git", "init"], cwd=str(brain_dir), capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            raise click.ClickException(f"git init failed: {result.stderr.strip()}")

    click.echo(f"✓ Brain initialised at {brain_dir}")


@main.group()
def brain():
    """Brain management commands."""


@brain.command()
def reindex():
    """Rebuild index.yaml by scanning brain .md files and extracting frontmatter.

    Walks all .md files in the brain directory (excluding _memory/),
    extracts YAML frontmatter, and builds the index keyed by type (parent
    directory) and slug (filename stem).
    """
    import yaml as _yaml

    from archie.config import load_config

    try:
        config = load_config()
    except (KeyError, ValueError) as e:
        raise click.ClickException(str(e)) from None

    brain_dir = config.brain_dir
    if not brain_dir.exists():
        raise click.ClickException(
            f"Brain directory not found: {brain_dir}\nRun 'archie init' first."
        )

    index: dict[str, dict] = {}
    count = 0

    # Walk all .md files, skipping _memory/ and .git/
    for md_file in brain_dir.rglob("*.md"):
        # Skip files in _memory/ and .git/
        rel = md_file.relative_to(brain_dir)
        parts = rel.parts
        if parts[0] in ("_memory", ".git"):
            continue

        # Parse frontmatter — only index files that have it
        text = md_file.read_text(encoding="utf-8", errors="replace")
        if not text.startswith("---"):
            continue

        # Find closing --- delimiter
        end = text.find("---", 3)
        if end == -1:
            continue

        try:
            fm = _yaml.safe_load(text[3:end])
        except _yaml.YAMLError:
            continue

        if not isinstance(fm, dict):
            continue

        # Type is the immediate parent directory name (projects, knowledge, people)
        item_type = parts[0] if len(parts) > 1 else "root"
        slug = md_file.stem

        if item_type not in index:
            index[item_type] = {}

        index[item_type][slug] = {
            "name": fm.get("name", slug),
            "path": str(rel),
            "summary": fm.get("summary", ""),
            "tags": fm.get("tags", []),
        }
        count += 1

    # Write the rebuilt index
    index_path = brain_dir / "index.yaml"
    index_path.write_text(_yaml.safe_dump(index, default_flow_style=False, sort_keys=True))

    click.echo(f"✓ Indexed {count} items → {index_path}")


# Register the brain group as a subcommand of main
main.add_command(brain)
