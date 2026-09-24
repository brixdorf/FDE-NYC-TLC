"""Load pipeline settings from config/pipeline.toml.

All paths are resolved against the project root so the pipeline behaves the
same no matter which directory it is launched from.
"""
from __future__ import annotations

import hashlib
import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "pipeline.toml"


def _apply_env_overrides(settings: dict) -> dict:
    """TLC_<SECTION>__<KEY>=value overrides one setting, parsed as TOML."""
    for name, value in os.environ.items():
        if not name.startswith("TLC_") or "__" not in name:
            continue
        section, key = name[4:].lower().split("__", 1)
        if section in settings and key in settings[section]:
            try:
                settings[section][key] = tomllib.loads(f"v = {value}")["v"]
            except tomllib.TOMLDecodeError:
                settings[section][key] = value  # plain strings need no TOML quoting
    return settings


@dataclass(frozen=True)
class Config:
    settings: dict
    root: Path = PROJECT_ROOT

    def __getitem__(self, section: str) -> dict:
        return self.settings[section]

    def path(self, name: str) -> Path:
        return self.root / self.settings["paths"][name]

    @property
    def fingerprint(self) -> str:
        """Short hash of the effective settings, stored in every run manifest."""
        blob = json.dumps(self.settings, sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:12]


def load_config(path: Path | None = None) -> Config:
    with open(path or DEFAULT_CONFIG, "rb") as f:
        settings = tomllib.load(f)
    return Config(settings=_apply_env_overrides(settings))
