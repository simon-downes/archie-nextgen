"""Configuration loading from ~/.archie/nextgen.yaml.

Config is intentionally minimal — just the things a user might want to change:
- model: which Bedrock inference profile to use
- region: AWS region for API calls
- system_prompt: personality/instructions for the model
- project_root: base directory for project detection
- sandbox: Docker sandbox settings

Model properties (pricing, context limits) are NOT config — they're constants
in models.py because users can't change them. They're properties of the model.
"""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from archie.models import get_model_info

# Filesystem paths for archie's data.
# Using ~/.archie/ as the root keeps everything in one place.
# "nextgen.yaml" avoids conflicts with any existing archie config.
ARCHIE_DIR = Path.home() / ".archie"
CONFIG_PATH = ARCHIE_DIR / "nextgen.yaml"
SESSIONS_DIR = ARCHIE_DIR / "sessions"

# Written to disk on first run if no config exists.
# Gives the user a working starting point they can edit.
DEFAULT_CONFIG = """\
model: "eu.anthropic.claude-fable-5"
region: "eu-west-1"
project_root: "~/dev"
"""


@dataclass(frozen=True)
class ToolsConfig:
    """Configuration for the tool framework.

    Attributes:
        allowed_directories: Additional absolute paths the model can read/search.
            The current working directory is always allowed implicitly.
    """

    allowed_directories: tuple[Path, ...] = ()


@dataclass(frozen=True)
class SandboxConfig:
    """Configuration for the Docker sandbox.

    Attributes:
        image: Docker image name/tag for the sandbox container.
        mounts: Additional mount specs in "host:container:mode" format.
            The project dir and standard dotfiles are mounted automatically.
    """

    image: str = "archie-sandbox:nextgen"
    mounts: tuple[str, ...] = ()


@dataclass(frozen=True)
class OllamaConfig:
    """Configuration for the Ollama local model provider.

    Attributes:
        host: Ollama server URL.
        timeout: Request timeout in seconds (generation can be slow on large models).
    """

    host: str = "http://localhost:11434"
    timeout: int = 120


@dataclass(frozen=True)
class MemoryConfig:
    """Configuration for the memory extraction system.

    Attributes:
        extraction_model: Bedrock model ID for memory extraction (cheap model).
        extraction_interval: Number of turns between extraction runs.
    """

    extraction_model: str = "eu.anthropic.claude-haiku-3-20250305-v1:0"
    extraction_interval: int = 5


@dataclass(frozen=True)
class Config:
    """Immutable application configuration.

    frozen=True makes it hashable and prevents accidental mutation.
    """

    model: str
    region: str
    project_root: Path = field(default_factory=lambda: Path.home() / "dev")
    brain_dir: Path = field(default_factory=lambda: Path.home() / ".archie" / "new-brain")
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)


def load_config() -> Config:
    """Load config from ~/.archie/nextgen.yaml, creating defaults on first run.

    Raises:
        ValueError: If config file is malformed or missing required fields.
        KeyError: If the configured model ID isn't in the known models registry.
    """
    # Ensure the directory exists (first run creates it)
    ARCHIE_DIR.mkdir(parents=True, exist_ok=True)

    # Auto-create a default config so users have something to edit
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(DEFAULT_CONFIG)

    # yaml.safe_load is critical — yaml.load() can execute arbitrary Python!
    raw = yaml.safe_load(CONFIG_PATH.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid config format in {CONFIG_PATH}")

    model = raw.get("model")
    if not model:
        raise ValueError(f"'model' is required in {CONFIG_PATH}")

    # Validate that the model ID exists in our registry.
    # This catches typos early rather than failing at the Bedrock API.
    get_model_info(model)

    region = raw.get("region", "eu-west-1")

    # Parse project_root — expand ~ to the user's home directory
    project_root = Path(raw.get("project_root", "~/dev")).expanduser()

    # Parse tools config
    tools_raw = raw.get("tools", {}) or {}
    allowed_dirs = tuple(Path(p) for p in tools_raw.get("allowed_directories", []))
    tools_config = ToolsConfig(allowed_directories=allowed_dirs)

    # Parse sandbox config
    sandbox_raw = raw.get("sandbox", {}) or {}
    sandbox_config = SandboxConfig(
        image=sandbox_raw.get("image", "archie-sandbox:nextgen"),
        mounts=tuple(sandbox_raw.get("mounts", [])),
    )

    # Parse brain_dir — expand ~ to user's home directory
    brain_dir = Path(raw.get("brain_dir", "~/.archie/new-brain")).expanduser()

    # Parse memory config
    memory_raw = raw.get("memory", {}) or {}
    memory_config = MemoryConfig(
        extraction_model=memory_raw.get(
            "extraction_model", "eu.anthropic.claude-haiku-3-20250305-v1:0"
        ),
        extraction_interval=memory_raw.get("extraction_interval", 5),
    )

    # Parse ollama config
    ollama_raw = raw.get("ollama", {}) or {}
    ollama_config = OllamaConfig(
        host=ollama_raw.get("host", "http://localhost:11434"),
        timeout=ollama_raw.get("timeout", 120),
    )

    return Config(
        model=model,
        region=region,
        project_root=project_root,
        brain_dir=brain_dir,
        tools=tools_config,
        sandbox=sandbox_config,
        ollama=ollama_config,
        memory=memory_config,
    )
