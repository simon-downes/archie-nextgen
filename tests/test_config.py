"""Tests for config loading."""

import pytest
import yaml

from archie.config import load_config


def test_load_config_creates_default(tmp_path, monkeypatch):
    """First run creates default config."""
    monkeypatch.setattr("archie.config.ARCHIE_DIR", tmp_path)
    monkeypatch.setattr("archie.config.CONFIG_PATH", tmp_path / "nextgen.yaml")

    config = load_config()
    assert config.model == "eu.anthropic.claude-fable-5"
    assert config.region == "eu-west-1"
    assert (tmp_path / "nextgen.yaml").exists()


def test_load_config_reads_existing(tmp_path, monkeypatch):
    """Reads existing config file."""
    monkeypatch.setattr("archie.config.ARCHIE_DIR", tmp_path)
    monkeypatch.setattr("archie.config.CONFIG_PATH", tmp_path / "nextgen.yaml")

    (tmp_path / "nextgen.yaml").write_text(
        yaml.dump(
            {
                "model": "eu.anthropic.claude-sonnet-4-6",
                "region": "eu-west-1",
            }
        )
    )

    config = load_config()
    assert config.model == "eu.anthropic.claude-sonnet-4-6"
    assert config.region == "eu-west-1"


def test_load_config_unknown_model(tmp_path, monkeypatch):
    """Unknown model ID raises."""
    monkeypatch.setattr("archie.config.ARCHIE_DIR", tmp_path)
    monkeypatch.setattr("archie.config.CONFIG_PATH", tmp_path / "nextgen.yaml")

    (tmp_path / "nextgen.yaml").write_text(
        yaml.dump(
            {
                "model": "unknown-model-id",
                "region": "us-east-1",
            }
        )
    )

    with pytest.raises(KeyError, match="Unknown model"):
        load_config()


def test_load_config_missing_model(tmp_path, monkeypatch):
    """Missing model field raises."""
    monkeypatch.setattr("archie.config.ARCHIE_DIR", tmp_path)
    monkeypatch.setattr("archie.config.CONFIG_PATH", tmp_path / "nextgen.yaml")

    (tmp_path / "nextgen.yaml").write_text(yaml.dump({"region": "us-east-1"}))

    with pytest.raises(ValueError, match="model.*required"):
        load_config()


def test_load_config_invalid_yaml(tmp_path, monkeypatch):
    """Malformed YAML raises."""
    monkeypatch.setattr("archie.config.ARCHIE_DIR", tmp_path)
    monkeypatch.setattr("archie.config.CONFIG_PATH", tmp_path / "nextgen.yaml")

    (tmp_path / "nextgen.yaml").write_text(": invalid: yaml: [")

    with pytest.raises((ValueError, yaml.YAMLError)):
        load_config()


def test_load_config_tools_allowed_directories(tmp_path, monkeypatch):
    """Tools allowed_directories are parsed into Path objects."""
    monkeypatch.setattr("archie.config.ARCHIE_DIR", tmp_path)
    monkeypatch.setattr("archie.config.CONFIG_PATH", tmp_path / "nextgen.yaml")

    (tmp_path / "nextgen.yaml").write_text(
        yaml.dump(
            {
                "model": "eu.anthropic.claude-sonnet-4-6",
                "region": "eu-west-1",
                "tools": {"allowed_directories": ["/tmp/allowed", "/home/user/projects"]},
            }
        )
    )

    config = load_config()
    from pathlib import Path

    assert len(config.tools.allowed_directories) == 2
    assert Path("/tmp/allowed") in config.tools.allowed_directories
    assert Path("/home/user/projects") in config.tools.allowed_directories


def test_load_config_tools_default_empty(tmp_path, monkeypatch):
    """Tools config defaults to empty allowed_directories."""
    monkeypatch.setattr("archie.config.ARCHIE_DIR", tmp_path)
    monkeypatch.setattr("archie.config.CONFIG_PATH", tmp_path / "nextgen.yaml")

    config = load_config()
    assert config.tools.allowed_directories == ()
