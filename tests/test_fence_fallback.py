"""0.2.23 — recover from the model writing ```bash``` instead of [[bash:]].

When the citizen produces a markdown bash fence (the failure mode
in the user's screenshot), the REPL queues the candidate command
and the user can press Enter on the next prompt to execute.

Tests cover the extraction primitive and the contract that a
proper [[bash:]] marker anywhere disables the fence-fallback path
(model was deliberate, don't second-guess).
"""

from __future__ import annotations

from anthill.cli.repl import SessionStats
from anthill.core.shell import extract_fence_candidates


def test_single_line_bash_fence_extracted() -> None:
    """The canonical failure mode: model wrote `​```bash\ncmd\n```​`
    with a single command. We pull it out."""
    text = "Let me check:\n```bash\nping -c 5 192.168.1.149\n```\n"
    assert extract_fence_candidates(text) == ["ping -c 5 192.168.1.149"]


def test_sh_and_shell_lang_tags_also_extracted() -> None:
    """Different language tags the model might write."""
    assert extract_fence_candidates("```sh\nls -la\n```") == ["ls -la"]
    assert extract_fence_candidates("```shell\ndf -h\n```") == ["df -h"]


def test_multiline_fence_not_extracted() -> None:
    """Multi-line fences are usually scripts/tutorials, not
    'run this'. We don't auto-queue them."""
    multi = "```bash\nls\ngit status\ncat foo\n```"
    assert extract_fence_candidates(multi) == []


def test_proper_bash_marker_suppresses_fence_fallback() -> None:
    """When the model DID use [[bash:]] anywhere, we don't fall back.
    The fence in this output is probably explanation, not an action."""
    text = "Did it: [[bash:echo done]] — for reference:\n```bash\nls -la\n```"
    assert extract_fence_candidates(text) == []


def test_empty_fence_ignored() -> None:
    text = "```bash\n\n```"
    assert extract_fence_candidates(text) == []


def test_session_stats_queue_default_none() -> None:
    """Fresh SessionStats: no pending queued shell command."""
    s = SessionStats()
    assert s.queued_shell_command is None
