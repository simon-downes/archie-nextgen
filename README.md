# Archie

Personal AI coding assistant with a terminal UI, tool-calling, and a Docker sandbox for safe shell execution. Powered by Anthropic Claude via AWS Bedrock.

## Key Rules

- Build the sandbox image with `archie build` before running `archie chat` — the chat command will fail pre-flight if the image is missing.
- AWS credentials must be available in the environment (e.g. via `~/.aws/credentials` or environment variables) — Archie talks to Bedrock directly, there is no API key.
- Config lives at `~/.archie/nextgen.yaml` and is auto-created with defaults on first run — edit it to change the model or region.
- Model IDs must use the Bedrock cross-region inference profile format (e.g. `eu.anthropic.claude-sonnet-4-6`) — bare model IDs are not accepted.
- The sandbox container is per-session and disposable — it is started lazily on first tool use and destroyed when the session ends.
- Debug logging is always on at `~/.archie/nextgen.log` (rotating, 10MB × 3 backups) — check it when something goes wrong.
- Linting uses Ruff with line length 100 — run `uv run ruff check` and `uv run ruff format` before committing.

## Installation

**Prerequisites:** Python 3.13+, [uv](https://docs.astral.sh/uv/), Docker, AWS credentials with Bedrock access.

```bash
# Clone and install
git clone <repo>
cd archie-nextgen
uv sync

# Build the sandbox image (required before first chat)
uv run archie build
```

## Usage

```bash
# Start an interactive chat session
uv run archie chat

# Rebuild the sandbox image (e.g. after Dockerfile changes)
uv run archie build
```

### Key Bindings

| Key | Action |
|-----|--------|
| Enter | Submit prompt |
| Shift+Enter | Insert newline |
| Esc | Interrupt in-flight turn (preserves completed work) |
| Ctrl+G | Open `$EDITOR` to compose prompt (save to submit, quit to cancel) |
| Ctrl+P | Command palette (switch model, new session, quit) |
| Ctrl+N | New session (destroys sandbox, clears conversation) |
| Ctrl+Q | Quit |
| `!command` | Run a shell command directly in the sandbox |

### Model Switching

Use Ctrl+P to switch models mid-session. The change takes effect on the next turn — history and sandbox are preserved, no restart needed.

## Configuration

Config file: `~/.archie/nextgen.yaml` (auto-created on first run).

| Key | Default | Description |
|-----|---------|-------------|
| `model` | `eu.anthropic.claude-fable-5` | Bedrock inference profile ID |
| `region` | `eu-west-1` | AWS region for Bedrock API calls |
| `project_root` | `~/dev` | Base directory for project detection |
| `sandbox.image` | `archie-sandbox:nextgen` | Docker image name built by `archie build` |
| `sandbox.mounts` | `[]` | Additional `host:container:mode` mount specs |
| `tools.allowed_directories` | `[]` | Extra absolute paths the model can read/search |

### Available Models

| ID | Name | Context |
|----|------|---------|
| `eu.anthropic.claude-sonnet-4-6` | Claude Sonnet 4.6 | 1M tokens |
| `eu.anthropic.claude-haiku-3-20250305-v1:0` | Claude Haiku | 200K tokens |
| `eu.anthropic.claude-opus-4-6-v1` | Claude Opus 4.6 | 1M tokens |
| `eu.anthropic.claude-opus-4-8-v1` | Claude Opus 4.8 | 1M tokens |
| `eu.anthropic.claude-fable-5` | Claude Fable 5 | 1M tokens |

Swap `eu.` for `us.` to use US cross-region inference profiles.

## Cost Management

Archie uses prompt caching to reduce costs. The status bar shows a four-way token breakdown:

```
in:fresh/cache_read/cache_write out:output │ ctx:N% │ $cost
```

- **Cache read** tokens are ~10x cheaper than fresh input — these climb as the session grows
- **Cache write** is a one-time premium when new content enters the cache prefix
- The second turn in a session is dramatically cheaper than the first (everything is cached)
- Use Sonnet for routine work, Opus/Fable for hard problems

## Sandbox

The sandbox is a Debian-based Docker container with a curated set of developer tools pre-installed: `git`, `ripgrep`, `fd`, `jq`, `yq`, AWS CLI, GitHub CLI, OpenTofu, `pandoc`, `shellcheck`, `sqlite3`, and more. See [`sandbox/Dockerfile`](sandbox/Dockerfile) for the full list.

The container user matches the host user (UID is passed as a build arg) to avoid file permission issues on mounted directories.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

```bash
# Run tests
uv run pytest

# Lint + format
uv run ruff check src tests
uv run ruff format src tests
```
