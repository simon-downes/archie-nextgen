"""System prompt builder — assembles the prompt from discrete sections.

Sections:
- SOUL.md: identity, personality, core rules (loaded from file)
- <env>: dynamic environment context (cwd, OS, sandbox, git branch)
- <tools>: tool usage patterns and strategy
- <agents.md>: project's AGENTS.md if present (raw include)

The prompt is rebuilt at session start. Some sections (env) contain
values that are static for the session; others (agents.md) are read
from disk each time to pick up changes.
"""

import platform
from pathlib import Path

_SOUL_PATH = Path(__file__).parent / "soul.md"

_TOOLS_SECTION = """\
## Strategy

Minimise round-trips — every request re-sends the full context:
- Batch independent tool calls into one response (multiple tool_use blocks).
- Multiple edits to one file: single edit_file call with multiple edits entries.
- Use code (outline/search) to locate structure; read only for content you will
  edit or quote, targeting the region with offset/limit.
- Run verification (lint + tests) as one combined shell command once changes are
  complete — not piecemeal after each change.

## Patterns

- **Discovery**: code (structure) → grep (content) → read (targeted lines)
- **Modification**: edit_file (surgical) or write_file (full replace)
- **Verification**: shell (single combined command for format + lint + test)
- **Exploration**: glob (find files) → code (outline) → read (details)

## File Tools

- All paths relative to project root (e.g. src/archie/agent.py)
- read returns line numbers — use them for offset on subsequent reads
- read dedup: "unchanged" means the content is still in your context
- glob results sorted by most recently modified first

## Shell

The sandbox is an isolated Docker container (Debian). Destroyed after session.
Cannot affect the host. Cannot run docker commands inside it.

Available commands: git, rg (ripgrep), fd, jq, yq, aws, gh (GitHub CLI),
tofu (OpenTofu), python3, node, shellcheck, shfmt, pandoc, sqlite3, curl.

Use shell for: running tests, builds, git operations, installing packages,
any command-line workflow. Prefer file tools over shell equivalents (cat, sed, grep).
"""


def build_system_prompt(
    project_dir: Path,
    git_branch: str = "unknown",
    agents_md: str | None = None,
) -> str:
    """Assemble the full system prompt from sections.

    Args:
        project_dir: Project working directory.
        git_branch: Current git branch name.
        agents_md: Contents of the project's AGENTS.md (None if absent).
    """
    sections: list[str] = []

    # --- Soul ---
    soul = _SOUL_PATH.read_text(encoding="utf-8") if _SOUL_PATH.exists() else ""
    if soul:
        sections.append(soul.strip())

    # --- Environment ---
    env = _build_env(project_dir, git_branch)
    sections.append(f"<env>\n{env}\n</env>")

    # --- Tools ---
    sections.append(f"<tools>\n{_TOOLS_SECTION.strip()}\n</tools>")

    # --- AGENTS.md ---
    if agents_md:
        sections.append(f"<agents.md>\n{agents_md.strip()}\n</agents.md>")

    return "\n\n".join(sections)


def _build_env(project_dir: Path, git_branch: str) -> str:
    """Build the dynamic environment section."""
    return f"""\
- Project: {project_dir.name}
- Working directory: /workspace (maps to {project_dir})
- OS: {platform.system()} (sandbox is Debian Linux)
- Git branch: {git_branch}
- Python: 3.13"""
