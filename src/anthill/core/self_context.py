"""0.2.6 — anthill's self-knowledge for the Scout/citizen prompt.

When a user asks "你能做什么" / "你如何接入飞书" / "how do I use this tool",
the citizens have no idea what "你/this" refers to. They see a prompt
that contains episodic memory, the user's request, and not a single
hint that they're inside an agent called "anthill" with specific
channels and capabilities. So they answer abstractly — "AI 助手通常可
以..." — or worse, ask clarification about WHICH AI to talk about.

This module surfaces anthill's identity as part of the Scout prompt
whenever the request looks self-referential. Compact (~400 tokens),
opt-in by heuristic (zero cost on requests that don't ask about
anthill itself).

The self-context is generated from LIVE config, not a hardcoded blob:
configured channels / installed extras / nation name / version all
get reflected. So "anthill 怎么接飞书" gets an accurate answer for
THIS user's setup, not a generic doc dump.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from anthill.core.userconfig import UserConfig


# 0.2.6 — self-reference detector. Permissive: false positives just
# inject 400 extra tokens, which is fine; false negatives mean the
# user gets a generic "AI 助手" answer to "anthill 怎么用", which is
# the exact UX bug we're fixing. So bias toward triggering.
_SELF_REF_MARKERS_CN = (
    "anthill", "蚁巢",
    "你能", "你可以", "你会", "你支持", "你如何", "你怎么",
    "你是", "你有", "你的", "你做", "你跑",
    "怎么用你", "如何用你", "这个工具", "这个系统", "这个 agent",
    "你自己",
)
_SELF_REF_MARKERS_EN = (
    "anthill",
    "this tool", "this system", "this agent", "this bot",
    "yourself", "your capabilities", "your features",
    "what can you do", "what do you do",
    "how do i use", "how to use",
    "how do you", "what are you",
)


def looks_self_referential(request: str) -> bool:
    """Heuristic: does this request ask about anthill itself?

    Returns True when the request contains any 2nd-person reference
    paired with verbs about capability / configuration. Liberal —
    we'd rather inject self-context on a borderline question and
    answer it well than miss "你能..." and answer abstractly.
    """
    if not request:
        return False
    text = request.lower()
    for marker in _SELF_REF_MARKERS_EN:
        if marker in text:
            return True
    # Chinese markers — preserve original case, do plain substring.
    for marker in _SELF_REF_MARKERS_CN:
        if marker in request:
            return True
    return False


def _configured_channels(user_cfg: "UserConfig | None") -> list[tuple[str, str]]:
    """Return [(name, kind), ...] for channels actually configured."""
    if user_cfg is None or not getattr(user_cfg, "channels", None):
        return []
    out: list[tuple[str, str]] = []
    for ch in user_cfg.channels:
        # Be lenient about the shape — userconfig has evolved.
        name = getattr(ch, "name", None) or "?"
        kind = getattr(ch, "kind", None) or "?"
        out.append((name, kind))
    return out


def _model_count(user_cfg: "UserConfig | None") -> int:
    if user_cfg is None or not getattr(user_cfg, "models", None):
        return 0
    return len(user_cfg.models)


# Channel kind → user-readable feature line. Used when the user asks
# about specific integrations.
_CHANNEL_DOCS = {
    "lark": "Lark/Feishu — webhook bot, supports group threads + replies",
    "feishu": "Lark/Feishu — webhook bot, supports group threads + replies",
    "slack": "Slack — Web API bot, threads via thread_ts",
    "telegram": "Telegram — bot API, forum topics + quoted replies",
    "wecom": "WeCom (企业微信) — corp bot, no thread concept",
    "discord": "Discord — bot API, threads as channels + quote-reply",
    "email": "Email — SMTP send + optional IMAP poll for receive",
}


def self_context_block(
    user_cfg: "UserConfig | None" = None,
    *,
    nation_name: str = "default",
) -> str:
    """Build the self-introduction block for Scout to see.

    Format mirrors how MEMORY.md / USER.md get injected — a fenced
    block with a clear `<anthill_self>` marker so models can find /
    cite it without it leaking into output.
    """
    try:
        from anthill import __version__ as anthill_version
    except Exception:  # noqa: BLE001
        anthill_version = "unknown"

    channels = _configured_channels(user_cfg)
    if channels:
        channel_lines = []
        for name, kind in channels:
            doc = _CHANNEL_DOCS.get(kind.lower(), kind)
            channel_lines.append(f"  - {name} (kind={kind}): {doc}")
        channels_section = "\n".join(channel_lines)
    else:
        channels_section = (
            "  (none configured yet — `anthill channel add NAME --kind "
            "<lark|slack|telegram|wecom|discord|email>` to add one)"
        )

    n_models = _model_count(user_cfg)

    return f"""<anthill_self>
You are running INSIDE anthill (v{anthill_version}), a multi-model AI
agent system. When the user says "你" / "this tool" / "this agent",
they mean ME — anthill — not "an AI assistant in general".

# CRITICAL: be BRIEF.
Default to a 2-3 line answer + the exact command. Users hate
tutorials they didn't ask for. Don't generate tables, multi-section
guides, "前期准备 / 步骤一 / 步骤二 / 常见报错" walls of text
unless the user explicitly asks for "详细教程" / "step by step".
Show what to type, in one code block, then STOP. End with
"想展开任何一步告诉我" if there's more to say.

# Key facts (use when relevant, don't dump verbatim)
- Architecture: a "nation" of citizens (one per configured model)
  with pheromone-routed task assignment. {n_models} model(s),
  nation "{nation_name}".
- Each ask: Scout plans → 1-N subtasks on best-fit citizens →
  optional synthesis. Multiple models can collaborate.
- Learning: pheromone trails reinforce model×task_type fit.
  Skills auto-distill from successful complex asks.

# Configured channels on THIS install
{channels_section}

# Built-in plugins
web_fetch, web_search, file_read/write/list, shell, code_exec,
pdf_read, docx_read, xlsx_read, browser_render, browser_screenshot

# Common commands
- CLI REPL: `anthill`
- Add channel: `anthill channel add <name> --kind <kind> ...`
- Serve as bot: `anthill serve` (port 8765, webhook at /<kind>/webhook)
- Cron: `anthill cron add '@daily HH:MM' '<request>' --channel <n> --target <id>`

# Output discipline
- "如何对接飞书" → 3-line answer + the 2 commands. Stop.
- "怎么用" → name 3 starter actions. Stop.
- "你能做什么" → 1-line per category, max 5 categories. Stop.
- Only expand into headers/tables/sections when user uses
  "详细" / "完整" / "step by step" / "tell me everything".
</anthill_self>"""
