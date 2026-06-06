# Sandboxing

## Objective

Add Docker-based sandboxing so the shell tool (Phase 4) can execute commands safely
without touching the host system. The container is a disposable execution environment —
created per session on first use, destroyed after.

## Context

- Phase 2 (tool framework) is complete — Engine, tool registry, file tools working
- Shell tool requires sandboxing first (no approval prompts, contain blast radius)
- Current archie already uses Docker with same-path mounts — proven pattern
- Engine runs on host; only shell execution goes through the container
- File read/search/list stay native on host (fast, no docker overhead)
- File write to project dir is local

## Requirements

### Container Lifecycle

- MUST create a Docker container per session on first shell tool call (lazy)
  - AC: Container not created until shell tool is actually used
- MUST destroy container when session ends (app quit or Ctrl+N)
  - AC: `docker rm -f` on session teardown; no orphaned containers
- MUST match host user UID inside container (avoids permission issues on mounted files)
  - AC: Files created by shell commands in the project dir are owned by the host user

### Mounts

- MUST mount the project directory at the same absolute path (rw)
  - AC: `cd /home/user/dev/myproject && ls` works identically inside the container
- MUST mount `~/.archie/brain` read-only (for future memory/brain access)
- MUST mount `~/.gitconfig` read-only
- MUST mount `~/.ssh` read-only
- SHOULD mount `~/.aws` read-only (for AWS CLI inside container)
- MUST be configurable via `sandbox.mounts` in config (list of additional host:container:mode entries)

### Project Directory Detection

- MUST detect project directory from cwd on startup
  - AC: Walk up from cwd to find first directory that's a direct child of `project_root`
- MUST have `project_root` config setting (default `~/dev`)
  - AC: `~/dev/myproject/src/lib` → project = `~/dev/myproject`
- MUST use detected project directory as the working directory for the container
- MUST use detected project directory as `cwd` for file tools (replacing current `Path.cwd()`)
- MUST fall back to cwd if not under project_root

### Networking

- MUST allow outbound network access
  - AC: `curl https://example.com` works inside container

### Startup Checks

- MUST check Docker is installed and daemon is running on startup
  - AC: Clear error message if `docker info` fails
- MUST check sandbox image exists (`archie-sandbox:nextgen`)
  - AC: Clear error message with instructions to run `archie build` if image missing

### Build Command

- MUST provide `archie build` CLI command that builds the sandbox image
  - AC: Builds `sandbox/Dockerfile` tagged as `archie-sandbox:nextgen`
  - AC: Passes host UID and username as build args

### Configuration

- MUST add `sandbox` section to config: image name, additional mounts
- MUST add `project_root` to config (default `~/dev`)

### Dockerfile

- MUST include: git, curl, bash, ca-certificates, unzip, zip, ripgrep, fd, tree,
  jq, yq, Python (via uv), uv, AWS CLI, OpenTofu, terraform-docs, GitHub CLI,
  difftastic, xh, just, shfmt, shellcheck, pandoc, sqlite3
- MUST pre-seed GitHub SSH host keys
- MUST create user with configurable UID matching host user

## Design

### Key Decisions (from review)

- **Docker failure mid-session**: `ensure_running()` raises, Engine catches it as a tool
  execution error, sends error result to model. No crash.
- **Mount paths that don't exist** (e.g. `~/.archie/brain`): skip silently. Only mount
  paths that actually exist on the host.
- **Quit hook**: override Textual's `action_quit` — call `sandbox.destroy()` then
  `super().action_quit()`.
- **Dockerfile location for build**: use `Path(archie.__file__).parents[2] / "sandbox"`.
  Always run from the repo checkout (personal tool).
- **Container naming**: uses `session.session_id` which is generated immediately on Session
  creation (before any persistence).
- **Timeout**: `subprocess.run(timeout=N)` kills the docker exec process. Returns captured
  output + timeout indicator. Command inside container may linger briefly.
- **New session**: destroy old sandbox, create new lazy Sandbox instance (not started).
- **stderr/stdout**: combined via `2>&1` in the bash command passed to docker exec.
- **Docker permissions error**: error message includes hint about docker group.
- **Project dir as cwd**: intentional behaviour change. File tools operate from project root,
  not the subdirectory you happened to launch from. This matches how IDEs work (project-level).

### Project Layout (new/changed)

```
archie-nextgen/
├── sandbox/
│   └── Dockerfile
├── src/archie/
│   ├── cli.py              # MODIFIED — add `build` command, startup checks
│   ├── config.py           # MODIFIED — add sandbox + project_root config
│   ├── project.py          # NEW — project directory detection
│   ├── sandbox.py          # NEW — container lifecycle management
│   └── ui/app.py           # MODIFIED — pass project_dir, teardown container
```

### Sandbox Class

```python
class Sandbox:
    """Manages a Docker container for sandboxed shell execution.
    
    Lifecycle: lazy start on first exec(), destroyed on session end.
    Uses subprocess to call docker directly (no docker-py dependency).
    """
    
    def __init__(self, image, project_dir, session_id, mounts, username, uid): ...
    def ensure_running(self) -> None: ...
    def exec(self, command: str, timeout: int = 60) -> tuple[str, int]: ...
    def destroy(self) -> None: ...
```

- Container name: `archie-{session_id}` (unique per session)
- Started with `docker run -d --name ... sleep infinity` (stays alive for exec calls)
- Working directory inside container: project_dir path
- `exec()` uses `docker exec -w {project_dir} {container} bash -c {command}`

### Mounts (built by Sandbox.ensure_running)

```
project_dir     → same path (rw)
~/.archie/brain → same path (ro)
~/.gitconfig    → /home/{user}/.gitconfig (ro)
~/.ssh          → /home/{user}/.ssh (ro)
~/.aws          → /home/{user}/.aws (ro)
+ config sandbox.mounts entries
```

### Project Detection

```python
def detect_project_dir(cwd: Path, project_root: Path) -> Path:
    """Walk up from cwd to find first child of project_root.
    
    ~/dev/myproject/src/lib → ~/dev/myproject
    ~/random/place → ~/random/place (fallback: cwd itself)
    """
```

### Config Additions

```yaml
project_root: "~/dev"

sandbox:
  image: "archie-sandbox:nextgen"
  mounts: []
```

### Startup Flow

```
archie chat:
  1. load_config()
  2. detect_project_dir(cwd, config.project_root)
  3. check Docker available (docker info)
  4. check image exists (docker image inspect)
  5. create ArchieApp with project_dir → Engine → tools use project_dir as cwd
  6. on quit/new session → sandbox.destroy()
```

## Milestones

1. Project directory detection + config
   Approach:
   - Add `project_root` to config (default `~/dev`)
   - Add `sandbox` section: `image` (default `archie-sandbox:nextgen`), `mounts` (default empty)
   - Create `src/archie/project.py` with detection utility — walk up from cwd, find first
     child of project_root. Fallback to cwd if not under project_root.
   - Wire into app startup: detected project_dir replaces `Path.cwd()` as the tools' `cwd`
   - ⚠️ Fallback when cwd isn't under project_root: use cwd itself
   Tasks:
   - Add `project_root` and `sandbox` to Config + DEFAULT_CONFIG
   - Create `src/archie/project.py` with `detect_project_dir()`
   - Update `app.py` to detect project dir and pass to tool registry
   - Tests: detection from nested dir, at project root child, fallback
   Deliverable: Project dir auto-detected and used as file tools base.
   Verify: Tests pass. Run from subdirectory — file tools see full project.

2. Dockerfile + build command
   Approach:
   - Create `sandbox/Dockerfile` with stripped-down toolset (no kiro, no PHP, no chromium)
   - Add `archie build` command to cli.py — runs `docker build` with USERNAME, USER_UID,
     TARGETARCH build-args. Tags as `archie-sandbox:nextgen`.
   - Locate Dockerfile via package path (`Path(__file__).parent.parent.parent / "sandbox"`)
   - ⚠️ For installed packages, the sandbox/ dir may not be at a predictable path relative
     to the source. Include it as package data and use importlib.resources, OR require
     building from the repo checkout (simpler for now — this is a personal tool).
   Tasks:
   - Create `sandbox/Dockerfile`
   - Add `build` command to cli.py
   - Document in README that `archie build` must be run from the repo
   Deliverable: `archie build` produces the sandbox image.
   Verify: `archie build` completes, `docker images archie-sandbox:nextgen` shows result.

3. Sandbox module — container lifecycle
   Approach:
   - `src/archie/sandbox.py` with Sandbox class
   - Uses subprocess for all docker commands (no docker-py dependency)
   - `ensure_running()`: `docker run -d --name archie-{session_id}` with all mounts,
     `--user {uid}:{uid}`, working dir set, network enabled, sleep infinity
   - `exec(command, timeout)`: `docker exec -w {project_dir} archie-{session_id} bash -c {command}`
     Returns (combined stdout+stderr, exit_code). Timeout via subprocess timeout.
   - `destroy()`: `docker rm -f archie-{session_id}`, idempotent (ignore errors if not exists)
   - Container reuses host networking (simplest, full outbound access)
   Tasks:
   - Create `src/archie/sandbox.py`
   - Implement ensure_running with mount building
   - Implement exec with timeout + output capture
   - Implement destroy (idempotent)
   - Tests: mock subprocess, verify correct docker commands built
   Deliverable: Sandbox can start, exec, and destroy containers.
   Verify: Tests pass. Manual: instantiate Sandbox, exec "echo hello", destroy.

4. Startup checks + teardown wiring
   Approach:
   - Pre-flight checks in cli.py before launching TUI (via click.echo):
     `docker info` → check daemon; `docker image inspect` → check image
   - Fail with clear error and exit code if checks fail
   - Wire Sandbox into ArchieApp: create instance in __init__, destroy on quit
   - Override `action_quit` to call sandbox.destroy() before exit
   - On `action_new_session`: destroy old sandbox, create new one
   - ⚠️ Ensure destroy runs even on unexpected exit (atexit handler as backup)
   Tasks:
   - Add startup checks to cli.py chat command
   - Create Sandbox in ArchieApp.__init__ (lazy — doesn't start container)
   - Wire destroy into quit and new_session
   - Add atexit handler for cleanup
   - Tests: mock docker checks, verify error messages
   Deliverable: App refuses to start without Docker/image; containers cleaned up on exit.
   Verify: Remove image → helpful error. Run and quit → `docker ps -a` shows no orphan.
