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

        Format: "$ {command}\\n[exit: {code}]\\n{output}"
        This gives the model clear structure — it can see what ran, whether
        it succeeded, and what the output was.
        """
        command = params.get("command", "")
        if not command:
            return "Error: No command provided"

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
