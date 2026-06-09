"""Tests for CLI startup checks (Docker availability and image existence)."""

import subprocess
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from archie.cli import check_docker_available, check_sandbox_image, main


@pytest.fixture
def runner():
    """Click test runner."""
    return CliRunner()


class TestCheckDockerAvailable:
    @patch("archie.cli.subprocess.run")
    def test_passes_when_docker_running(self, mock_run):
        """No exception when docker info succeeds."""
        mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")
        # Should not raise
        check_docker_available()

    @patch("archie.cli.subprocess.run")
    def test_raises_when_docker_unavailable(self, mock_run):
        """ClickException raised with helpful message when docker fails."""
        mock_run.return_value = subprocess.CompletedProcess([], 1, "", "permission denied")
        from click import ClickException

        with pytest.raises(ClickException) as exc_info:
            check_docker_available()

        # Should include both the hint and the actual error
        assert "docker group" in str(exc_info.value.message)
        assert "permission denied" in str(exc_info.value.message)


class TestCheckSandboxImage:
    @patch("archie.cli.subprocess.run")
    def test_passes_when_image_exists(self, mock_run):
        """No exception when image inspect succeeds."""
        mock_run.return_value = subprocess.CompletedProcess([], 0, "[{}]", "")
        check_sandbox_image("archie-sandbox:nextgen")

    @patch("archie.cli.subprocess.run")
    def test_raises_when_image_missing(self, mock_run):
        """ClickException raised suggesting 'archie build'."""
        mock_run.return_value = subprocess.CompletedProcess([], 1, "", "No such image")
        from click import ClickException

        with pytest.raises(ClickException) as exc_info:
            check_sandbox_image("archie-sandbox:nextgen")

        assert "archie build" in str(exc_info.value.message)


class TestChatCommandChecks:
    @patch("archie.ui.app.ArchieApp")
    @patch("archie.cli.check_sandbox_image")
    @patch("archie.cli.check_docker_available")
    def test_chat_warns_on_docker_failure(self, mock_docker, mock_image, mock_app, runner):
        """Chat command warns but continues when Docker checks fail."""
        from click import ClickException

        mock_docker.side_effect = ClickException("Docker not available")
        # Mock the app so we don't actually launch Textual
        mock_app.return_value.run.return_value = None

        result = runner.invoke(main, ["chat"])

        # Should warn but not crash
        assert "Docker not available" in result.output or result.exit_code == 0
        mock_docker.assert_called_once()
