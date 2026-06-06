"""Docker sandbox for shell command execution.

The Sandbox manages a disposable Docker container per session. Commands
execute inside the container while the host remains untouched. The container
is lazy-started on first exec() and destroyed when the session ends.

All Docker interaction is via subprocess (no docker-py dependency) — keeps
things simple and avoids version conflicts with the Docker Engine API.
"""

import subprocess
from pathlib import Path

from archie.config import SandboxConfig


class Sandbox:
    """Manages a Docker container for sandboxed shell execution.

    Lifecycle:
        1. Created in ArchieApp.__init__ (stores config, doesn't start container)
        2. Container started lazily on first exec() call via ensure_running()
        3. Destroyed on session end (quit, new session, or atexit)

    Attributes:
        config: Sandbox configuration (image name, extra mounts).
        project_dir: Host path to the project directory (mounted rw).
        session_id: Unique session identifier (used in container name).
        username: Host username (for mount paths like ~/.gitconfig).
        uid: Host user UID (container runs as this user).
    """

    def __init__(
        self,
        config: SandboxConfig,
        project_dir: Path,
        session_id: str,
        username: str,
        uid: int,
    ) -> None:
        self.config = config
        self.project_dir = project_dir
        self.session_id = session_id
        self.username = username
        self.uid = uid
        self._running = False

    @property
    def container_name(self) -> str:
        """Docker container name, unique per session."""
        return f"archie-{self.session_id}"

    def ensure_running(self) -> None:
        """Start the container if not already running.

        Creates a detached container with sleep infinity, all configured
        mounts, and the host user's UID. Skips mounts for paths that
        don't exist on the host (e.g. ~/.archie/brain before first use).

        Raises:
            RuntimeError: If docker run fails.
        """
        if self._running:
            return

        # Build mount list: project dir (rw) + standard dotfiles (ro)
        mounts = self._build_mounts()

        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            self.container_name,
            "--user",
            f"{self.uid}:{self.uid}",
            "-w",
            str(self.project_dir),
        ]

        # Add volume mounts
        for mount in mounts:
            cmd.extend(["-v", mount])

        # Add any extra mounts from config
        for extra in self.config.mounts:
            cmd.extend(["-v", extra])

        cmd.extend([self.config.image, "sleep", "infinity"])

        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to start sandbox container: {result.stderr.strip()}")

        self._running = True

    def exec(self, command: str, timeout: int = 60) -> tuple[str, int]:
        """Execute a command inside the sandbox container.

        Starts the container if not already running (lazy start).
        Combines stdout and stderr via `2>&1` so the model sees all output.

        Args:
            command: Shell command to execute (passed to bash -c).
            timeout: Maximum seconds to wait (default 60).

        Returns:
            Tuple of (output_text, exit_code). On timeout, output includes
            a timeout message and exit_code is -1.
        """
        self.ensure_running()

        cmd = [
            "docker",
            "exec",
            "-w",
            str(self.project_dir),
            self.container_name,
            "bash",
            "-c",
            f"{command} 2>&1",
        ]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, check=False
            )
            return result.stdout, result.returncode
        except subprocess.TimeoutExpired as e:
            # Return whatever output was captured before timeout
            partial = e.stdout.decode() if e.stdout else ""
            return f"{partial}\n\n[Timed out after {timeout}s]", -1

    def destroy(self) -> None:
        """Remove the container. Idempotent — safe to call even if not running.

        Uses `docker rm -f` which handles both running and stopped containers.
        Errors are silently ignored (container may already be gone).
        """
        subprocess.run(
            ["docker", "rm", "-f", self.container_name],
            capture_output=True,
            check=False,
        )
        self._running = False

    def _build_mounts(self) -> list[str]:
        """Build the list of volume mount strings for docker run.

        Mounts:
        - Project directory at same path (rw) — so paths work identically
        - ~/.gitconfig (ro) — git commands use host config
        - ~/.ssh (ro) — git clone can authenticate
        - ~/.aws (ro) — AWS CLI uses host credentials
        - ~/.archie/brain (ro) — future memory/brain access

        Skips any path that doesn't exist on the host.
        """
        home = Path.home()
        mounts = []

        # Project directory — always mounted (it must exist, it's our cwd)
        mounts.append(f"{self.project_dir}:{self.project_dir}:rw")

        # Standard read-only mounts (skip if they don't exist on host)
        ro_paths = [
            home / ".gitconfig",
            home / ".ssh",
            home / ".aws",
            home / ".archie" / "brain",
        ]

        for path in ro_paths:
            if path.exists():
                mounts.append(f"{path}:{path}:ro")

        return mounts
