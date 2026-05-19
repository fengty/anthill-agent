"""0.1.62 — head-tail conversation compression tests.

Hermes's context_compressor.py (1508 LOC) protects the first N and
last M turns while collapsing the middle. We borrow the strategy
into ConversationContext at a much smaller scale.

Verifies:
  - compressed_view is a no-op when len ≤ head+tail
  - middle turns are collapsed when len > head+tail
  - default placeholder when no summarize_fn
  - summarize_fn output replaces the placeholder
  - summarize_fn exception falls back gracefully (lossy placeholder)
  - compress_in_place mutates the deque + returns delta count
  - synthetic turn has empty request (no follow-up false-positive)
  - chronology: timestamps stay monotonic
"""

from __future__ import annotations

from anthill.core.conversation import ConversationContext


def _ctx_with_turns(n: int) -> ConversationContext:
    """Build a ConversationContext with n synthetic turns at ts=1..n."""
    c = ConversationContext(maxlen=50)
    for i in range(1, n + 1):
        c.record(
            request=f"q{i}",
            response=f"a{i}",
            timestamp=float(i),
        )
    return c


# --- compressed_view ------------------------------------------------------


def test_compressed_view_no_op_below_threshold() -> None:
    """6 turns + default head=2/tail=4 = exactly threshold; no compression."""
    c = _ctx_with_turns(6)
    out = c.compressed_view()
    assert len(out) == 6
    assert [t.request for t in out] == [f"q{i}" for i in range(1, 7)]


def test_compressed_view_collapses_middle_when_above_threshold() -> None:
    """10 turns + head=2/tail=4 → head(2) + synthetic(1) + tail(4) = 7."""
    c = _ctx_with_turns(10)
    out = c.compressed_view()
    assert len(out) == 7
    # head
    assert out[0].request == "q1"
    assert out[1].request == "q2"
    # synthetic middle (empty request → no follow-up false-positive)
    assert out[2].request == ""
    assert "omitted" in out[2].response.lower()
    # tail
    assert [t.request for t in out[3:]] == ["q7", "q8", "q9", "q10"]


def test_compressed_view_synthetic_request_is_empty() -> None:
    """Critical: the synthetic turn's request MUST be empty so
    is_follow_up doesn't accidentally treat it as a user message."""
    c = _ctx_with_turns(10)
    out = c.compressed_view()
    synthetic = out[2]
    assert synthetic.request == ""


def test_compressed_view_synthetic_timestamp_in_middle_range() -> None:
    """Chronology must stay monotonic — synthetic ts between head and
    tail extremes."""
    c = _ctx_with_turns(10)
    out = c.compressed_view()
    # Last head ts=2, first tail ts=7. Synthetic should be strictly
    # between them.
    last_head_ts = out[1].timestamp
    first_tail_ts = out[3].timestamp
    synthetic_ts = out[2].timestamp
    assert last_head_ts < synthetic_ts < first_tail_ts


def test_compressed_view_uses_summarize_fn_when_provided() -> None:
    """When the caller supplies a summarizer, its output replaces the
    lossy placeholder."""
    c = _ctx_with_turns(10)

    def fake_summarize(middle):
        return f"SUMMARY of {len(middle)} turns: q3-q6 happened"

    out = c.compressed_view(summarize_fn=fake_summarize)
    assert "SUMMARY of 4 turns" in out[2].response
    assert "omitted" not in out[2].response  # placeholder NOT used


def test_compressed_view_summarize_exception_falls_back() -> None:
    """A failing summarizer must not break the compression — fall
    back to the lossy placeholder."""
    c = _ctx_with_turns(10)

    def explode(_):
        raise RuntimeError("summarize boom")

    out = c.compressed_view(summarize_fn=explode)
    assert "omitted" in out[2].response.lower()


def test_compressed_view_does_not_mutate_deque() -> None:
    c = _ctx_with_turns(10)
    before = list(c.recent())
    _ = c.compressed_view()
    after = list(c.recent())
    assert [t.request for t in before] == [t.request for t in after]


def test_compressed_view_custom_keep_head_and_tail() -> None:
    """keep_head=3, keep_tail=2 → 3 + 1 + 2 = 6 for 10 turns."""
    c = _ctx_with_turns(10)
    out = c.compressed_view(keep_head=3, keep_tail=2)
    assert len(out) == 6
    assert [t.request for t in out[:3]] == ["q1", "q2", "q3"]
    assert [t.request for t in out[-2:]] == ["q9", "q10"]


# --- compress_in_place -----------------------------------------------------


def test_compress_in_place_returns_count_and_mutates() -> None:
    c = _ctx_with_turns(10)
    collapsed = c.compress_in_place(keep_head=2, keep_tail=4)
    # 10 - 7 = 3 turns net delta.
    assert collapsed == 3
    assert len(c) == 7


def test_compress_in_place_no_op_returns_zero() -> None:
    c = _ctx_with_turns(5)
    collapsed = c.compress_in_place(keep_head=2, keep_tail=4)
    assert collapsed == 0
    assert len(c) == 5


def test_compress_in_place_preserves_maxlen() -> None:
    """The deque's maxlen must survive compression — otherwise the
    next record() would unbound the buffer."""
    c = ConversationContext(maxlen=20)
    for i in range(1, 11):
        c.record(f"q{i}", f"a{i}", timestamp=float(i))
    c.compress_in_place(keep_head=2, keep_tail=4)
    assert c._turns.maxlen == 20  # noqa: SLF001 (we own this class)


# --- is_follow_up + synthetic turn interaction ----------------------------


def test_synthetic_turn_doesnt_trigger_follow_up() -> None:
    """Critical interaction: after compression, the most recent
    PRIOR turn passed to is_follow_up is still a real user turn
    (the last of the tail), NOT the synthetic one. Verify by
    walking the structure."""
    c = _ctx_with_turns(10)
    c.compress_in_place(keep_head=2, keep_tail=4)
    last = c.last_turn()
    assert last is not None
    assert last.request == "q10"  # actual most-recent user turn
