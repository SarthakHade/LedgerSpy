import dataclasses
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from ledgerspy.config import ConfigError, load_config
from ledgerspy.paths import DEFAULT_CONFIG_PATH

VALID = """\
config_version: 1
server:
  host: 127.0.0.1
  port: 8765
workspace:
  path: workspace
  db_filename: ledgerspy.db
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "cfg.yaml"
    path.write_text(text, encoding="utf-8")
    return path


# ---------- loading and the single interface ----------


def test_default_config_loads_and_validates():
    cfg = load_config()
    assert cfg.source == DEFAULT_CONFIG_PATH
    assert cfg.settings.server.host == "127.0.0.1"
    assert re.fullmatch(r"[0-9a-f]{64}", cfg.sha256)


def test_config_is_immutable():
    cfg = load_config()
    with pytest.raises(ValidationError):
        cfg.settings.server.port = 1
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.sha256 = "x"


# ---------- hash determinism ----------


def test_hash_is_deterministic_across_loads():
    assert len({load_config().sha256 for _ in range(5)}) == 1


def test_hash_ignores_formatting_comments_and_key_order(tmp_path):
    reordered = """\
# comment
workspace: {db_filename: ledgerspy.db, path: workspace}
server:
    port: 8765      # trailing comment
    host: "127.0.0.1"
config_version: 1
"""
    assert load_config(write(tmp_path, VALID)).sha256 == load_config(write(tmp_path, reordered)).sha256


def test_hash_matches_the_shipped_default(tmp_path):
    # VALID mirrors config/default.yaml's values; both describe the same effective config.
    assert load_config(write(tmp_path, VALID)).sha256 == load_config().sha256


def test_hash_changes_when_a_value_changes(tmp_path):
    base = load_config(write(tmp_path, VALID)).sha256
    changed = load_config(write(tmp_path, VALID.replace("8765", "8766"))).sha256
    assert base != changed


def test_overrides_are_validated_and_included_in_hash():
    base = load_config()
    overridden = load_config(overrides={"server": {"port": 9000}})
    assert overridden.settings.server.port == 9000
    assert overridden.settings.workspace == base.settings.workspace  # deep merge keeps siblings
    assert overridden.sha256 != base.sha256
    assert load_config(overrides={"server": {"port": 9000}}).sha256 == overridden.sha256
    with pytest.raises(ConfigError):
        load_config(overrides={"server": {"port": 80}})
    with pytest.raises(ConfigError):
        load_config(overrides={"risk": {"tau_high": 0.7}})  # section not defined yet


# ---------- validation failures ----------


@pytest.mark.parametrize(
    ("text", "why"),
    [
        (VALID.replace("127.0.0.1", "0.0.0.0"), "non-loopback bind address"),
        (VALID.replace("127.0.0.1", "localhost"), "hostname instead of loopback IP"),
        (VALID.replace("8765", '"8765"'), "port given as string (strict types)"),
        (VALID.replace("8765", "80"), "privileged port"),
        (VALID.replace("8765", "70000"), "port out of range"),
        (VALID.replace("config_version: 1", "config_version: 2"), "unknown config version"),
        (VALID + "extra_section: {}\n", "unknown top-level key"),
        (VALID.replace("  port: 8765", "  port: 8765\n  debug: true"), "unknown nested key"),
        ("config_version: 1\nserver: {host: 127.0.0.1, port: 8765}\n", "missing section"),
        (VALID.replace("ledgerspy.db", "../escape.db"), "db filename with path"),
        (VALID.replace("  port: 8765", "  port: 8765\n  port: 8766"), "duplicate key"),
        ("server: [unclosed", "invalid YAML"),
        ("- just\n- a list\n", "top level not a mapping"),
        ("", "empty file"),
    ],
)
def test_invalid_config_is_rejected(tmp_path, text, why):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, text))


def test_missing_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "nope.yaml")
