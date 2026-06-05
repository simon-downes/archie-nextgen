"""Tests for config loading."""

import pytest
import yaml

from archie.config import load_config


def test_load_config_creates_default(tmp_path, monkeypatch):
    """First run creates default config."""
    monkeypatch.setattr("archie.config.ARCHIE_DIR", tmp_path)
    monkeypatch.setattr("archie.config.CONFIG_PATH", tmp_path / "nextgen.yaml")

    config = load_config()
    assert config.model == "eu.anthropic.claude-sonnet-4-6"
    assert config.region == "eu-west-1"
    assert "helpful" in config.system_prompt
    assert (tmp_path / "nextgen.yaml").exists()


def test_load_config_reads_existing(tmp_path, monkeypatch):
    """Reads existing config file."""
    monkeypatch.setattr("archie.config.ARCHIE_DIR", tmp_path)
    monkeypatch.setattr("archie.config.CONFIG_PATH", tmp_path / "nextgen.yaml")

    (tmp_path / "nextgen.yaml").write_text(
        yaml.dump(
            {
                "model": "eu.anthropic.claude-haiku-3-20250305-v1:0",
                "region": "eu-west-1",
                "system_prompt": "Be terse.",
            }
        )
    )

    config = load_config()
    assert config.model == "eu.anthropic.claude-haiku-3-20250305-v1:0"
    assert config.region == "eu-west-1"
    assert config.system_prompt == "Be terse."


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
