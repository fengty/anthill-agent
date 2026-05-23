"""0.2.22 — detect external tools on $PATH so anthill knows what it can call.

The gap: when the user asks "你能做什么" / "你如何和我的飞书对接的？",
anthill answers honestly that it has no built-in feishu integration —
but the user has `lark-cli` installed and ready to drive. The
citizens don't know that.

Fix: at startup (or first ask), scan $PATH for a curated list of
common CLIs. The detected tools get folded into self_context_block
so citizens can say:

  "anthill 本身没接飞书协议, 但你装了 lark-cli (/usr/local/bin/lark-cli),
   我可以 [[bash:lark-cli im send ...]] 帮你发消息."

Design notes:
  - One-shot scan, cached per process (re-scanning every ask
    burns ~100ms for nothing in steady state)
  - Curated list. We don't scan the whole PATH — too noisy
  - Result is just (name, full_path) pairs; we don't try to
    fingerprint versions or capabilities
  - Best-effort: missing 'which' or weird PATH still returns []
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from functools import lru_cache


# Curated list of tools whose presence is worth telling the model
# about. Each is something the model would plausibly want to shell
# out to via [[bash:]] to actually accomplish work.
#
# Grouped by category in source for readability; the runtime
# scan flattens this.
_INTERESTING_TOOLS: tuple[tuple[str, str], ...] = (
    # IM / feishu / lark ecosystem (the user's stated pain)
    ("lark-cli", "Lark/Feishu CLI (send messages, manage docs, drive calendar)"),
    ("lark", "Lark/Feishu CLI alias"),
    # Source control
    ("git", "Git VCS"),
    ("gh", "GitHub CLI (PRs, issues, releases)"),
    ("glab", "GitLab CLI"),
    # Containers / orchestration
    ("docker", "Docker container runtime"),
    ("podman", "Podman container runtime"),
    ("kubectl", "Kubernetes CLI"),
    ("helm", "Helm Kubernetes package manager"),
    # Infra
    ("terraform", "Terraform infrastructure-as-code"),
    ("ansible", "Ansible config management"),
    # Cloud
    ("aws", "AWS CLI"),
    ("gcloud", "Google Cloud CLI"),
    ("az", "Azure CLI"),
    # Package managers / build
    ("npm", "Node package manager"),
    ("yarn", "Yarn package manager"),
    ("pnpm", "pnpm package manager"),
    ("pip", "Python package installer"),
    ("uv", "Fast Python package manager"),
    ("poetry", "Python poetry"),
    ("cargo", "Rust cargo"),
    ("go", "Go toolchain"),
    ("brew", "Homebrew package manager"),
    # Network / debugging
    ("curl", "HTTP client"),
    ("wget", "HTTP downloader"),
    ("jq", "JSON processor"),
    ("yq", "YAML processor"),
    ("dig", "DNS lookup"),
    # Editors that work in scripts
    ("code", "VS Code CLI (open files, diff)"),
)


@dataclass(frozen=True)
class DetectedTool:
    """One external CLI we found on $PATH."""

    name: str          # what we searched for (e.g. 'lark-cli')
    path: str          # full resolved path
    description: str   # one-liner — what it does


@lru_cache(maxsize=1)
def detect_tools() -> tuple[DetectedTool, ...]:
    """Scan $PATH once, return the tuple of tools that resolved.

    Cached: first call does the work, every subsequent call returns
    the same tuple. The 'lru_cache' size of 1 acts as a permanent
    process-level cache — if the user installs a new tool mid-
    session we won't see it until restart, which is fine.
    """
    found: list[DetectedTool] = []
    for name, desc in _INTERESTING_TOOLS:
        path = shutil.which(name)
        if path:
            found.append(DetectedTool(name=name, path=path, description=desc))
    return tuple(found)


def format_tools_block(tools: tuple[DetectedTool, ...] | None = None) -> str:
    """Render a compact block of detected tools for the self-context.

    `tools=None` → call detect_tools() (the normal case).
    `tools=()`   → render empty (the "I explicitly want nothing" case
                    used by tests and noexec).

    Empty when nothing was detected — don't waste prompt budget on
    a header with no body.

    Format:
      External tools available on the king's machine:
        - lark-cli (/usr/local/bin/lark-cli) — Lark/Feishu CLI ...
        - gh       (/opt/homebrew/bin/gh) — GitHub CLI ...
        ...
      Invoke via [[bash:TOOL ...]] when relevant.
    """
    if tools is None:
        tools = detect_tools()
    if not tools:
        return ""
    # Align names for readability.
    max_name = max(len(t.name) for t in tools)
    lines = ["External tools available on the king's machine:"]
    for t in tools:
        padded = t.name.ljust(max_name)
        lines.append(f"  - {padded} ({t.path}) — {t.description}")
    lines.append("")
    lines.append(
        "Invoke any of these via [[bash:TOOL ...]] when the king "
        "asks for something they handle. Don't claim 'I can't do X' "
        "if the tool that does X is on this list."
    )
    return "\n".join(lines)
