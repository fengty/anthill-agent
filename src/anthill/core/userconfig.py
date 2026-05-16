"""User-level configuration: models, channels, secrets.

The split between config.toml and secrets.toml is deliberate.

  ~/.anthill/config.toml
    Human-readable, safe to commit to a dotfiles repo. Lists every
    configured model and channel by name. References secrets by key
    name, never by value.

  ~/.anthill/secrets.toml
    Mode 0600. Holds API tokens, app secrets, anything that should
    never appear in a shell log. Loaded by name into providers.

This module is the bottom layer — pure data plus read/write. The CLI
'anthill setup', 'anthill model', and 'anthill channel' commands sit
on top of it (v0.2.2+).
"""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]


# A real TOML writer is annoying to install; we hand-roll a tiny one
# tuned to this schema. Generic enough for nested string/int/bool/list,
# nothing fancier.
def _emit_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        rendered = ", ".join(_emit_value(v) for v in value)
        return f"[{rendered}]"
    s = str(value)
    # escape backslashes and quotes
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _emit_section(name: str, data: dict[str, Any]) -> list[str]:
    lines: list[str] = [f"[{name}]"]
    for k, v in data.items():
        lines.append(f"{k} = {_emit_value(v)}")
    return lines


def _emit_table_array(name: str, rows: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for row in rows:
        out.append(f"[[{name}]]")
        for k, v in row.items():
            if v is None:
                continue
            out.append(f"{k} = {_emit_value(v)}")
        out.append("")
    return out


@dataclass
class ModelEntry:
    """One model configuration.

    `name` is the user-facing alias ('work-deepseek', 'cheap'). `provider`
    selects the adapter ('deepseek', 'minimax', 'openai', 'anthropic',
    'custom'). `secret_ref` names a key in secrets.toml; we never inline
    the secret value here.
    """

    name: str
    provider: str
    model: str
    secret_ref: str
    base_url: str | None = None      # only set for 'custom' / non-default endpoints
    extra: dict[str, Any] = field(default_factory=dict)  # provider-specific bits

    def to_dict(self) -> dict[str, Any]:
        out = {
            "name": self.name,
            "provider": self.provider,
            "model": self.model,
            "secret_ref": self.secret_ref,
        }
        if self.base_url:
            out["base_url"] = self.base_url
        for k, v in self.extra.items():
            out[k] = v
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelEntry":
        known = {"name", "provider", "model", "secret_ref", "base_url"}
        return cls(
            name=data["name"],
            provider=data["provider"],
            model=data["model"],
            secret_ref=data["secret_ref"],
            base_url=data.get("base_url"),
            extra={k: v for k, v in data.items() if k not in known},
        )


@dataclass
class ChannelEntry:
    """One IM channel configuration."""

    name: str
    kind: str            # "lark" / "telegram" / "slack" / "wecom"
    secret_ref: str
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out = {"name": self.name, "kind": self.kind, "secret_ref": self.secret_ref}
        for k, v in self.extra.items():
            out[k] = v
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChannelEntry":
        known = {"name", "kind", "secret_ref"}
        return cls(
            name=data["name"],
            kind=data["kind"],
            secret_ref=data["secret_ref"],
            extra={k: v for k, v in data.items() if k not in known},
        )


@dataclass
class UserConfig:
    """Top-level user config (everything except secret values)."""

    default_model: str | None = None
    models: list[ModelEntry] = field(default_factory=list)
    channels: list[ChannelEntry] = field(default_factory=list)

    def find_model(self, name: str) -> ModelEntry | None:
        for m in self.models:
            if m.name == name:
                return m
        return None

    def find_channel(self, name: str) -> ChannelEntry | None:
        for c in self.channels:
            if c.name == name:
                return c
        return None


def config_dir() -> Path:
    """Where config files live. Honors ANTHILL_HOME (used in tests + Docker)."""
    raw = os.getenv("ANTHILL_HOME")
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.home() / ".anthill").resolve()


def config_path() -> Path:
    return config_dir() / "config.toml"


def secrets_path() -> Path:
    return config_dir() / "secrets.toml"


def load_config() -> UserConfig:
    """Load config.toml if present. Missing file = empty config."""
    path = config_path()
    if not path.exists():
        return UserConfig()
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    cfg = UserConfig(default_model=data.get("default_model"))
    for entry in data.get("models", []) or []:
        cfg.models.append(ModelEntry.from_dict(entry))
    for entry in data.get("channels", []) or []:
        cfg.channels.append(ChannelEntry.from_dict(entry))
    return cfg


def save_config(cfg: UserConfig) -> Path:
    """Atomically write config.toml. Returns the path written."""
    config_dir().mkdir(parents=True, exist_ok=True)
    path = config_path()
    lines: list[str] = [
        "# Anthill user configuration.",
        "# Edit by hand or via the `anthill model` / `anthill channel` commands.",
        "# Secrets live in secrets.toml (this file is safe to share / dotfile).",
        "",
    ]
    if cfg.default_model:
        lines.append(f'default_model = {_emit_value(cfg.default_model)}')
        lines.append("")
    if cfg.models:
        lines.extend(_emit_table_array("models", [m.to_dict() for m in cfg.models]))
    if cfg.channels:
        lines.extend(_emit_table_array("channels", [c.to_dict() for c in cfg.channels]))
    text = "\n".join(lines).rstrip() + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)
    return path


def load_secrets() -> dict[str, str]:
    """Read secrets.toml. Returns {} when the file is missing or empty."""
    path = secrets_path()
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    # The file is a flat string-keyed table — but dotted keys like
    # 'model.foo' get parsed by TOML as nested tables. Flatten back to
    # the original dotted form on read.
    return dict(_flatten_strings(data))


def _flatten_strings(data: dict, prefix: str = "") -> "list[tuple[str, str]]":
    out: list[tuple[str, str]] = []
    for k, v in data.items():
        full = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.extend(_flatten_strings(v, full))
        elif isinstance(v, (str, int, float)):
            out.append((full, str(v)))
    return out


def _quote_key(key: str) -> str:
    """Quote a TOML key so dots inside don't become nested-table separators."""
    if key and all(c.isalnum() or c == "_" or c == "-" for c in key):
        return key
    escaped = key.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def save_secrets(secrets: dict[str, str]) -> Path:
    """Write secrets.toml atomically and chmod 600."""
    config_dir().mkdir(parents=True, exist_ok=True)
    path = secrets_path()
    lines = [
        "# Anthill secrets. NEVER commit this file.",
        "# Generated by `anthill model add` etc; safe to edit by hand.",
        "",
    ]
    for k, v in sorted(secrets.items()):
        lines.append(f"{_quote_key(k)} = {_emit_value(v)}")
    text = "\n".join(lines).rstrip() + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    # chmod BEFORE replace so the secret content is never world-readable.
    tmp.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0o600
    tmp.replace(path)
    # Defensive: re-chmod after replace because replace inherits from old file.
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except FileNotFoundError:
        pass
    return path


def secret_for(ref: str) -> str | None:
    """Look up a secret value by ref. Returns None if not set."""
    return load_secrets().get(ref)


def upsert_secret(ref: str, value: str) -> None:
    """Set or replace a single secret by ref. Other secrets preserved."""
    current = load_secrets()
    current[ref] = value
    save_secrets(current)


def remove_secret(ref: str) -> bool:
    """Drop a secret by ref. Returns True if it existed."""
    current = load_secrets()
    if ref not in current:
        return False
    del current[ref]
    save_secrets(current)
    return True


def mask(value: str, *, keep_prefix: int = 4, keep_suffix: int = 2) -> str:
    """Show a secret as 'sk-1...ab' for safe display."""
    if not value:
        return ""
    if len(value) <= keep_prefix + keep_suffix:
        return "*" * len(value)
    return f"{value[:keep_prefix]}…{value[-keep_suffix:]}"
