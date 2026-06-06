"""Tests for the Docker sandbox module.

All docker commands are mocked — these tests verify the correct commands
are constructed without actually running containers.
"""

import subprocess
from unittest.mock import patch

import pytest

from archie.config import SandboxConfig
from archie.sandbox import Sandbox


@pytest.fixture
def sandbox(tmp_path):
    """Create a Sandbox instance with test values."""
    project_dir = tmp_path / "dev" / "myproject"
    project_dir.mkdir(parents=True)
    return Sandbox(
        config=SandboxConfig(),
        project_dir=project_dir,
        session_id="abc123",
        username="testuser",
        uid=1000,
    )


class TestContainerName:
    def test_uses_session_id(self, sandbox):
        assert sandbox.container_name == "archie-abc123"


class TestEnsureRunning:
    @patch("archie.sandbox.subprocess.run")
    def test_starts_container_with_correct_args(self, mock_run, sandbox):
        """Verify docker run is called with the right flags."""
        mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")

        sandbox.ensure_running()

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]

        # Check key parts of the command
        assert cmd[0:3] == ["docker", "run", "-d"]
        assert "--name" in cmd
        assert "archie-abc123" in cmd
        assert "--user" in cmd
        assert "1000:1000" in cmd
        assert "-w" in cmd
        assert str(sandbox.project_dir) in cmd
        assert cmd[-2:] == ["sleep", "infinity"]

    @patch("archie.sandbox.subprocess.run")
    def test_mounts_project_dir_rw(self, mock_run, sandbox):
        """Project directory is mounted read-write at the same path."""
        mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")

        sandbox.ensure_running()

        cmd = mock_run.call_args[0][0]
        expected_mount = f"{sandbox.project_dir}:{sandbox.project_dir}:rw"
        assert expected_mount in cmd

    @patch("archie.sandbox.Path.home")
    @patch("archie.sandbox.subprocess.run")
    def test_skips_nonexistent_dotfiles(self, mock_run, mock_home, sandbox, tmp_path):
        """Mounts that don't exist on host are skipped silently."""
        # Point Path.home() to a temp dir where no dotfiles exist
        mock_home.return_value = tmp_path / "fakehome"
        (tmp_path / "fakehome").mkdir()
        mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")

        sandbox.ensure_running()

        cmd = mock_run.call_args[0][0]
        cmd_str = " ".join(cmd)
        assert ".gitconfig" not in cmd_str
        assert ".ssh" not in cmd_str
        assert ".aws" not in cmd_str

    @patch("archie.sandbox.subprocess.run")
    def test_includes_extra_config_mounts(self, mock_run, tmp_path):
        """Extra mounts from config are included."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        config = SandboxConfig(mounts=("/data:/data:ro",))
        sb = Sandbox(config=config, project_dir=project_dir, session_id="x", username="u", uid=1)
        mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")

        sb.ensure_running()

        cmd = mock_run.call_args[0][0]
        assert "/data:/data:ro" in cmd

    @patch("archie.sandbox.subprocess.run")
    def test_no_op_if_already_running(self, mock_run, sandbox):
        """Second call to ensure_running is a no-op."""
        mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")

        sandbox.ensure_running()
        sandbox.ensure_running()

        assert mock_run.call_count == 1

    @patch("archie.sandbox.subprocess.run")
    def test_raises_on_docker_failure(self, mock_run, sandbox):
        """RuntimeError raised if docker run fails."""
        mock_run.return_value = subprocess.CompletedProcess([], 1, "", "container name in use")

        with pytest.raises(RuntimeError, match="Failed to start sandbox"):
            sandbox.ensure_running()


class TestExec:
    @patch("archie.sandbox.subprocess.run")
    def test_runs_command_in_container(self, mock_run, sandbox):
        """Verify docker exec command structure."""
        # First call: ensure_running (docker run), second: the exec
        mock_run.return_value = subprocess.CompletedProcess([], 0, "hello\n", "")

        output, code = sandbox.exec("echo hello")

        # Second call should be the exec
        exec_call = mock_run.call_args_list[-1]
        cmd = exec_call[0][0]
        assert cmd[0:2] == ["docker", "exec"]
        assert "archie-abc123" in cmd
        assert "echo hello 2>&1" in cmd
        assert output == "hello\n"
        assert code == 0

    @patch("archie.sandbox.subprocess.run")
    def test_returns_exit_code(self, mock_run, sandbox):
        """Non-zero exit codes are passed through."""
        mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")
        sandbox.ensure_running()

        mock_run.return_value = subprocess.CompletedProcess([], 127, "not found\n", "")
        output, code = sandbox.exec("nonexistent")

        assert code == 127
        assert "not found" in output

    @patch("archie.sandbox.subprocess.run")
    def test_timeout_returns_partial_output(self, mock_run, sandbox):
        """Timeout returns partial output and exit code -1."""
        # First call succeeds (ensure_running)
        mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")
        sandbox.ensure_running()

        # Second call times out
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd=["docker", "exec"], timeout=5, output=b"partial"
        )

        output, code = sandbox.exec("sleep 100", timeout=5)

        assert code == -1
        assert "partial" in output
        assert "Timed out" in output


class TestDestroy:
    @patch("archie.sandbox.subprocess.run")
    def test_removes_container(self, mock_run, sandbox):
        """Destroy calls docker rm -f."""
        mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")

        sandbox.destroy()

        cmd = mock_run.call_args[0][0]
        assert cmd == ["docker", "rm", "-f", "archie-abc123"]

    @patch("archie.sandbox.subprocess.run")
    def test_idempotent(self, mock_run, sandbox):
        """Calling destroy twice doesn't raise."""
        mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")

        sandbox.destroy()
        sandbox.destroy()

        assert mock_run.call_count == 2

    @patch("archie.sandbox.subprocess.run")
    def test_resets_running_flag(self, mock_run, sandbox):
        """After destroy, ensure_running will start a new container."""
        mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")

        sandbox.ensure_running()
        sandbox.destroy()
        sandbox.ensure_running()

        # 3 calls: run, rm -f, run
        assert mock_run.call_count == 3
