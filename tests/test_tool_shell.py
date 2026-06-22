"""Tests for the shell tool handler.

The shell tool delegates to sandbox.exec() — these tests mock the sandbox
to verify the tool's formatting and error handling.
"""

from unittest.mock import MagicMock

from archie.tools.shell import make_shell_spec


class TestShellTool:
    def setup_method(self):
        """Create a shell tool spec with a mock sandbox."""
        self.sandbox = MagicMock()
        self.spec = make_shell_spec(self.sandbox)

    def test_spec_metadata(self):
        """Verify tool name, description, and schema are correct."""
        assert self.spec.name == "shell"
        assert "shell command" in self.spec.description
        assert self.spec.schema["properties"]["command"]["type"] == "string"
        assert "command" in self.spec.schema["required"]

    def test_formats_output_correctly(self):
        """Output format: $ command\\n[exit: code]\\noutput."""
        # First call is rtk rewrite (no rewrite available), second is the actual command
        self.sandbox.exec.side_effect = [("", 1), ("hello world\n", 0)]

        result = self.spec.handler({"command": "echo hello world"})

        assert result == "$ echo hello world\n[exit: 0]\nhello world\n"

    def test_shows_nonzero_exit_code(self):
        """Non-zero exit codes are clearly shown."""
        self.sandbox.exec.side_effect = [("", 1), ("bash: nope: command not found\n", 127)]

        result = self.spec.handler({"command": "nope"})

        assert "[exit: 127]" in result
        assert "$ nope" in result

    def test_empty_command_returns_error(self):
        """Empty command string returns an error without calling sandbox."""
        result = self.spec.handler({"command": ""})

        assert "Error" in result
        self.sandbox.exec.assert_not_called()

    def test_missing_command_returns_error(self):
        """Missing command param returns an error."""
        result = self.spec.handler({})

        assert "Error" in result
        self.sandbox.exec.assert_not_called()

    def test_calls_exec_with_rtk_rewrite(self):
        """Shell tool calls rtk rewrite first, then executes."""
        self.sandbox.exec.side_effect = [("", 1), ("", 0)]

        self.spec.handler({"command": "ls"})

        assert self.sandbox.exec.call_count == 2

    def test_uses_rtk_rewrite_when_available(self):
        """If rtk rewrite succeeds (exit 0/3), use the rewritten command."""
        self.sandbox.exec.side_effect = [("rtk git status\n", 0), ("M file.py\n", 0)]

        result = self.spec.handler({"command": "git status"})

        assert "$ rtk git status" in result
        self.sandbox.exec.assert_any_call("rtk git status")
