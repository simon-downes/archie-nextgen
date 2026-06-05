"""Configuration loading from ~/.archie/nextgen.yaml.

Config is intentionally minimal — just the things a user might want to change:
- model: which Bedrock inference profile to use
- region: AWS region for API calls
- system_prompt: personality/instructions for the model

Model properties (pricing, context limits) are NOT config — they're constants
in models.py because users can't change them. They're properties of the model.
"""

from dataclasses import dataclass
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
model: "eu.anthropic.claude-sonnet-4-6"
region: "eu-west-1"

system_prompt: |
  You are a helpful assistant. Be direct and concise.
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
class Config:
    """Immutable application configuration.

    frozen=True makes it hashable and prevents accidental mutation.
    """

    model: str
    region: str
    system_prompt: str
    tools: ToolsConfig = ToolsConfig()


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
    system_prompt = raw.get("system_prompt", "You are a helpful assistant.")

    # Parse tools config
    tools_raw = raw.get("tools", {}) or {}
    allowed_dirs = tuple(Path(p) for p in tools_raw.get("allowed_directories", []))
    tools_config = ToolsConfig(allowed_directories=allowed_dirs)

    return Config(model=model, region=region, system_prompt=system_prompt, tools=tools_config)
