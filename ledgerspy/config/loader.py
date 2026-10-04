"""Load, merge, validate and hash the LedgerSpy configuration."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from ledgerspy.config.schema import LedgerSpyConfig
from ledgerspy.hashing import canonical_json, sha256_hex
from ledgerspy.paths import DEFAULT_CONFIG_PATH


class ConfigError(ValueError):
    """The configuration file is unreadable, malformed or fails schema validation."""


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys (PyYAML silently keeps the last one)."""


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    mapping: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ConfigError(f"duplicate key {key!r} (line {key_node.start_mark.line + 1})")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def _deep_merge(base: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overrides.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


@dataclass(frozen=True)
class EffectiveConfig:
    """Validated configuration plus the SHA-256 that identifies it."""

    settings: LedgerSpyConfig
    sha256: str
    source: Path


def config_hash(settings: LedgerSpyConfig) -> str:
    """SHA-256 of the canonical JSON of the validated config (independent of YAML formatting/key order)."""
    return sha256_hex(canonical_json(settings.model_dump(mode="json")))


def load_config(
    path: Path | str | None = None, overrides: Mapping[str, Any] | None = None
) -> EffectiveConfig:
    """The one entry point for configuration.

    `overrides` (e.g. case-level settings or CLI flags) are deep-merged over the file before validation,
    so they are validated and included in the hash.
    """
    source = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file {source}: {exc}") from exc
    try:
        raw = yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506 — _UniqueKeyLoader is a SafeLoader
    except yaml.YAMLError as exc:
        raise ConfigError(f"{source}: invalid YAML: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{source}: top level must be a mapping")
    if overrides:
        raw = _deep_merge(raw, overrides)
    try:
        settings = LedgerSpyConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"{source}: {exc}") from exc
    return EffectiveConfig(settings=settings, sha256=config_hash(settings), source=source)
