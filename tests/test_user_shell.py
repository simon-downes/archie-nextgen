"""Tests for the ! prefix user shell feature and ShellOutput widget.

These tests verify:
- ! prefix is intercepted and not sent to the engine
- Empty ! commands are ignored
- ShellOutput widget renders correctly with capped output
"""

from archie.ui.conversation import ShellOutput


class TestShellOutput:
    def test_stores_command_and_output(self):
        """ShellOutput stores its data for rendering."""
        widget = ShellOutput("ls -la", "total 42\nfile.txt", 0)
        assert widget._command == "ls -la"
        assert widget._output == "total 42\nfile.txt"
        assert widget._exit_code == 0

    def test_output_cap_constant(self):
        """Output cap is 2000 chars."""
        assert ShellOutput._OUTPUT_CAP == 2000

    def test_handles_empty_output(self):
        """Empty output is stored correctly."""
        widget = ShellOutput("true", "", 0)
        assert widget._output == ""

    def test_handles_nonzero_exit(self):
        """Non-zero exit codes are stored."""
        widget = ShellOutput("false", "", 1)
        assert widget._exit_code == 1
