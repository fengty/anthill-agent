"""Anthill configuration.

Resolution order, highest to lowest priority:
    1. Explicit arguments at construction
    2. Environment variables (ANTHILL_*)
    3. ~/.anthill/config.toml
    4. Built-in defaults

This keeps secrets out of files when users prefer env vars, while still
letting them have a single config file if they want one.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]


DEFAULT_HOME = Path.home() / ".anthill"
DEFAULT_CONFIG_PATH = DEFAULT_HOME / "config.toml"


@dataclass
class AnthillConfig:
    """Top-level configuration."""

    home: Path = field(default_factory=lambda: DEFAULT_HOME)
    default_model: str = "deepseek-chat"
    exploration_rate: float = 0.10
    decay_rate: float = 0.05

    @classmethod
    def load(cls, path: Path | None = None) -> "AnthillConfig":
        config_path = path or DEFAULT_CONFIG_PATH
        data: dict = {}
        if config_path.exists():
            with config_path.open("rb") as f:
                data = tomllib.load(f)

        # Env var overrides
        if env_home := os.getenv("ANTHILL_HOME"):
            data["home"] = env_home
        if env_model := os.getenv("ANTHILL_DEFAULT_MODEL"):
            data["default_model"] = env_model

        kwargs: dict = {}
        if "home" in data:
            kwargs["home"] = Path(data["home"]).expanduser()
        if "default_model" in data:
            kwargs["default_model"] = data["default_model"]
        if "exploration_rate" in data:
            kwargs["exploration_rate"] = float(data["exploration_rate"])
        if "decay_rate" in data:
            kwargs["decay_rate"] = float(data["decay_rate"])
        return cls(**kwargs)

    def ensure_home(self) -> Path:
        self.home.mkdir(parents=True, exist_ok=True)
        return self.home
