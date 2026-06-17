# Archie

Personal AI coding assistant with a terminal UI, tool-calling, and a Docker sandbox for safe shell execution. Supports Anthropic Claude via AWS Bedrock and local models via Ollama.

## Key Rules

- Build the sandbox image with `archie build` before running `archie chat` — the chat command will fail pre-flight if the image is missing.
- For Bedrock: AWS credentials must be available in the environment (e.g. via `~/.aws/credentials` or environment variables).
- For Ollama: set `OLLAMA_HOST` or configure in `nextgen.yaml` — no AWS needed.
- Config lives at `~/.archie/nextgen.yaml` and is auto-created with defaults on first run — edit it to change the model or region.
- Model IDs must use the Bedrock cross-region inference profile format (e.g. `eu.anthropic.claude-sonnet-4-6`) — bare model IDs are not accepted.
- The sandbox container is per-session and disposable — it is started lazily on first tool use and destroyed when the session ends.
- Debug logging is always on at `~/.archie/nextgen.log` (rotating, 10MB × 3 backups) — check it when something goes wrong.
- Linting uses Ruff with line length 100 — run `uv run ruff check` and `uv run ruff format` before committing.

## Installation

**Prerequisites:** Python 3.13+, [uv](https://docs.astral.sh/uv/), Docker (optional, for shell tool), and either AWS credentials with Bedrock access OR [Ollama](https://ollama.com/) running locally.

```bash
# Clone and install
git clone <repo>
cd archie-nextgen
uv sync

# Build the sandbox image (required before first chat)
uv run archie build

# (Optional) Initialise the brain for memory/personal knowledge
uv run archie init
```

## Usage

```bash
# Start an interactive chat session
uv run archie chat

# Rebuild the sandbox image (e.g. after Dockerfile changes)
uv run archie build

# Initialise brain directory structure
uv run archie init

# Rebuild brain index from markdown files
uv run archie brain reindex
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
| `ollama.host` | `http://localhost:11434` | Ollama server URL |
| `ollama.timeout` | `120` | Request timeout in seconds |
| `brain_dir` | `~/.archie/new-brain` | Directory for memory/personal knowledge |
| `memory.extraction_model` | `eu.anthropic.claude-haiku-3-20250305-v1:0` | Model for memory extraction |
| `memory.extraction_interval` | `5` | Turns between extraction runs |

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

## Memory & Brain

Archie can extract and store memories from conversations:

- Memories are saved to `brain_dir/_memory/` as markdown files
- Automatic extraction runs every N turns (configurable via `memory.extraction_interval`)
- Use the **recall** tool to search past memories
- Use the **brain** tool to read/write structured knowledge (projects, people, knowledge base)
- Brain markdown files support YAML frontmatter for indexing

## Tools

| Tool | Description |
|------|-------------|
| `read_file` | Read file contents with line numbers and pagination |
| `write_file` | Create or overwrite files |
| `edit_file` | Search-and-replace edits to existing files |
| `list_files` | List directory contents with optional glob filtering |
| `search_files` | Regex search across files (ripgrep-based) |
| `shell` | Execute commands in the Docker sandbox |
| `web_search` | DuckDuckGo search |
| `web_fetch` | Fetch and convert web content to markdown |
| `code` | Code intelligence: outline, search symbols, project overview (tree-sitter based) |
| `self_debug` | Retrieve Archie's own debug logs |
| `retrieve_artifact` | Retrieve evicted tool results by ID |
| `brain` | Read/write structured knowledge (projects, people, knowledge base) |
| `recall` | Semantic search of extracted memories |
| `ui_summary` | Internal UI state inspection |

## Code Intelligence

Archie uses tree-sitter for parsing multiple languages:
- Python, JavaScript, TypeScript, Go, Rust, PHP, CSS, HCL

Use the **code** tool to: get project overviews, outline specific files, search for symbols by name.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

```bash
# Run tests
uv run pytest

# Lint + format
uv run ruff check src tests
uv run ruff format src tests
```
