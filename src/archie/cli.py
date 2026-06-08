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
