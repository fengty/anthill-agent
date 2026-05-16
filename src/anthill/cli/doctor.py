"""anthill doctor — full self-check.

A read-only pass over the install. Each check returns one of:

  ok      everything's fine
  warn    works but probably not what the user wanted
  miss    a thing the user might want is not installed/configured
  fail    something is broken

Doctor reports counts and exits with code 0 when there's no `fail`,
1 otherwise. `warn` and `miss` do not fail the exit code (CI-friendly).
"""

from __future__ import annotations

import os
import platform
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from rich.console import Console


console = Console()


@dataclass
class CheckResult:
    name: str
    status: str           # 'ok' | 'warn' | 'miss' | 'fail'
    message: str
    fix_hint: str | None = None


_GLYPH = {
    "ok":   "[green]✓[/green]",
    "warn": "[yellow]⚠[/yellow]",
    "miss": "[yellow]·[/yellow]",
    "fail": "[red]✗[/red]",
}


def run_doctor() -> int:
    """Execute every check and pretty-print results. Returns exit code."""
    results: list[CheckResult] = []
    for check in _CHECKS:
        try:
            results.append(check())
        except Exception as e:  # noqa: BLE001
            results.append(CheckResult(
                name=check.__name__.replace("_check_", ""),
                status="fail",
                message=f"check raised: {e}",
            ))

    name_width = max(len(r.name) for r in results) + 1
    for r in results:
        glyph = _GLYPH.get(r.status, " ")
        console.print(f"  {glyph}  {r.name.ljust(name_width)}  {r.message}")
        if r.fix_hint and r.status in ("miss", "fail", "warn"):
            console.print(f"      [dim]→ {r.fix_hint}[/dim]")

    counts = {k: 0 for k in _GLYPH}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1

    console.print()
    summary = " • ".join(
        f"{counts[k]} {k}" for k in ("ok", "warn", "miss", "fail") if counts.get(k)
    )
    console.print(f"  [dim]{summary}[/dim]")

    return 1 if counts.get("fail") else 0


# --- Individual checks ----------------------------------------------------


def _check_python() -> CheckResult:
    impl = platform.python_implementation()
    version = "%d.%d.%d" % sys.version_info[:3]
    if sys.version_info < (3, 9):
        return CheckResult("python", "fail", f"{impl} {version} (need 3.9+)",
                           "upgrade Python to 3.9 or newer")
    return CheckResult("python", "ok", f"{impl} {version}")


def _check_git() -> CheckResult:
    path = shutil.which("git")
    if not path:
        return CheckResult("git", "miss", "not on PATH",
                           "needed only for the curl|bash installer; pip-installed users can ignore")
    return CheckResult("git", "ok", path)


def _check_anthill_home() -> CheckResult:
    raw = os.getenv("ANTHILL_HOME")
    home = Path(raw).expanduser().resolve() if raw else (Path.home() / ".anthill")
    if home.exists():
        return CheckResult("home", "ok", str(home))
    return CheckResult("home", "miss", f"{home} does not exist yet",
                       "anthill setup  (or first ask will create it)")


def _check_config_file() -> CheckResult:
    from anthill.core.userconfig import config_path
    path = config_path()
    if not path.exists():
        return CheckResult("config", "miss", f"{path} not present",
                           "anthill setup  or  anthill model add")
    return CheckResult("config", "ok", str(path))


def _check_secrets_perms() -> CheckResult:
    from anthill.core.userconfig import secrets_path
    path = secrets_path()
    if not path.exists():
        return CheckResult("secrets", "miss", "no secrets.toml yet",
                           "anthill model add will create it")
    if os.name != "posix":
        return CheckResult("secrets", "ok", str(path))
    mode = path.stat().st_mode & 0o777
    if mode != 0o600:
        return CheckResult("secrets", "warn", f"chmod is {oct(mode)} (want 0o600)",
                           f"chmod 600 {path}")
    return CheckResult("secrets", "ok", f"{path}  (mode 0600)")


def _check_default_model() -> CheckResult:
    from anthill.core.userconfig import load_config, secret_for
    cfg = load_config()
    if not cfg.models:
        return CheckResult("default_model", "miss", "no models configured",
                           "anthill model add")
    if cfg.default_model is None:
        return CheckResult(
            "default_model",
            "warn",
            f"{len(cfg.models)} model(s) configured but none marked default",
            f"anthill model use {cfg.models[0].name}",
        )
    entry = cfg.find_model(cfg.default_model)
    if entry is None:
        return CheckResult(
            "default_model",
            "fail",
            f"default '{cfg.default_model}' is not in models list",
            "anthill model list  then  anthill model use NAME",
        )
    api_key = secret_for(entry.secret_ref)
    if not api_key:
        return CheckResult(
            "default_model",
            "fail",
            f"'{cfg.default_model}' has no API key at {entry.secret_ref}",
            f"anthill model add {entry.name}  to re-set the key",
        )
    return CheckResult("default_model", "ok", f"{entry.name} ({entry.provider}/{entry.model})")


def _check_extras(label: str, module: str, install: str) -> Callable[[], CheckResult]:
    def go() -> CheckResult:
        try:
            __import__(module)
            return CheckResult(label, "ok", "installed")
        except ImportError:
            return CheckResult(label, "miss", "not installed",
                               f"pip install '{install}'")
    go.__name__ = f"_check_{label}"
    return go


_CHECKS: list[Callable[[], CheckResult]] = [
    _check_python,
    _check_git,
    _check_anthill_home,
    _check_config_file,
    _check_secrets_perms,
    _check_default_model,
    _check_extras("daemon-deps", "fastapi", "anthill-agent[daemon]"),
    _check_extras("docs-deps", "pypdf", "anthill-agent[docs]"),
    _check_extras("browser-deps", "playwright", "anthill-agent[browser]"),
]


_ = stat  # silence unused import on non-posix
