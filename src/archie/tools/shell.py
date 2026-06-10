"""shell tool — executes commands in the Docker sandbox.

This is the "god tool" — it gives the model the ability to run arbitrary
commands in the sandboxed container. No approval prompts needed because
the container is disposable and isolated from the host.

Use cases: running tests, installing packages, git operations, build
commands, file manipulation via shell, anything the model needs a
terminal for.

Safety: The sandbox container is destroyed on session end. The project
directory is mounted read-write (same as a real terminal), but the host
system is otherwise untouched.
"""

from archie.sandbox import Sandbox
from archie.tools import ToolSpec


def make_shell_spec(sandbox: Sandbox) -> ToolSpec:
    """Create a shell ToolSpec bound to the given sandbox.

    Uses a closure pattern: the handler captures the sandbox instance at
    registration time. The sandbox handles lazy container start (via
    ensure_running) so the tool doesn't need to worry about lifecycle.

    Args:
        sandbox: The Sandbox instance for this session.
    """

    def handler(params: dict) -> str:
        """Execute a shell command in the sandbox and return formatted output.

        If RTK is available in the sandbox, the command is rewritten to its
        rtk equivalent (e.g. "git status" → "rtk git status") for compact
        output that saves tokens. Falls back to the original command if
        rtk isn't installed or doesn't have a filter for this command.

        Format: "$ {command}\\n[exit: {code}]\\n{output}"
        """
        command = params.get("command", "")
        if not command:
            return "Error: No command provided"

        # Try to rewrite through rtk for compact output.
        # rtk rewrite returns exit 0 or 3 with the rewritten command on stdout.
        # Exit 1/2 means no rewrite available — use original command.
        rewrite_output, rewrite_exit = sandbox.exec(f'rtk rewrite "{command}" 2>/dev/null')
        if rewrite_exit in (0, 3) and rewrite_output.strip():
            command = rewrite_output.strip()

        output, exit_code = sandbox.exec(command)
        return f"$ {command}\n[exit: {exit_code}]\n{output}"

    return ToolSpec(
        name="shell",
        description=(
            "Execute a shell command in the sandboxed container. Use for: running tests, "
            "installing packages, git operations, build commands, or any system command."
        ),
        schema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute",
                },
            },
            "required": ["command"],
        },
        handler=handler,
    )
