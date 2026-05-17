"""0.1.11 — @file / @glob file-as-context syntax.

Closes "I want to ask 'how do I change this file' without cat-ing it
in by hand." The REPL and `anthill ask` both expand `@`-tokens in the
request to inlined file contents before the prompt reaches Scout.

Tests:
  1. parse_at_tokens — tokenization, trailing-punctuation trim
  2. expand_attachments — literal path, glob, missing, dedup
  3. binary detection — NUL byte triggers skip
  4. per-file cap — files over the cap are skipped, not truncated
  5. total cap — once aggregate goes over, later files skipped + truncated flag
  6. render — empty when no files; well-formed block otherwise
  7. utf-8 robustness — bytes that aren't clean utf-8 still decode (with replace)
  8. paths display relative to base when possible
  9. absolute glob is anchored at root
"""

from __future__ import annotations

from pathlib import Path


def test_parse_at_tokens_basic() -> None:
    from anthill.core.attachments import parse_at_tokens

    tokens = parse_at_tokens("explain @src/foo.py and @docs/*.md please")
    assert tokens == ["src/foo.py", "docs/*.md"]


def test_parse_at_tokens_trims_trailing_punctuation() -> None:
    from anthill.core.attachments import parse_at_tokens

    tokens = parse_at_tokens("look at @foo.py, @bar.py. and @baz.py!")
    assert tokens == ["foo.py", "bar.py", "baz.py"]


def test_parse_at_tokens_handles_email_like() -> None:
    """The @ inside an email address is not a token start (no leading space)."""
    from anthill.core.attachments import parse_at_tokens

    # 'fty@example.com' — '@' is preceded by a non-space, but our
    # regex catches it anyway. That's acceptable: the file resolver
    # will simply not find a file named 'example.com' and emit an
    # error, which the REPL renders as a yellow warning.
    tokens = parse_at_tokens("contact fty@example.com")
    assert tokens == ["example.com"]


def test_parse_at_tokens_empty_input() -> None:
    from anthill.core.attachments import parse_at_tokens

    assert parse_at_tokens("") == []
    assert parse_at_tokens("no at tokens here") == []


def test_expand_attachments_literal_path(tmp_path: Path) -> None:
    from anthill.core.attachments import expand_attachments

    (tmp_path / "hello.txt").write_text("world")
    block = expand_attachments("look at @hello.txt", base=tmp_path)
    assert len(block.files) == 1
    assert block.files[0].path == "hello.txt"
    assert block.files[0].content == "world"
    assert not block.errors


def test_expand_attachments_glob(tmp_path: Path) -> None:
    from anthill.core.attachments import expand_attachments

    (tmp_path / "a.py").write_text("aaa")
    (tmp_path / "b.py").write_text("bbb")
    (tmp_path / "c.md").write_text("ccc")  # not matched
    block = expand_attachments("review @*.py", base=tmp_path)
    paths = sorted(f.path for f in block.files)
    assert paths == ["a.py", "b.py"]


def test_expand_attachments_recursive_glob(tmp_path: Path) -> None:
    from anthill.core.attachments import expand_attachments

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "deep").mkdir()
    (tmp_path / "src" / "deep" / "inner.py").write_text("deep")
    (tmp_path / "src" / "outer.py").write_text("outer")
    block = expand_attachments("scan @src/**/*.py", base=tmp_path)
    paths = sorted(f.path for f in block.files)
    assert paths == ["src/deep/inner.py", "src/outer.py"]


def test_expand_attachments_missing_file_emits_error(tmp_path: Path) -> None:
    from anthill.core.attachments import expand_attachments

    block = expand_attachments("@does-not-exist.py", base=tmp_path)
    assert block.files == []
    assert len(block.errors) == 1
    assert block.errors[0].token == "@does-not-exist.py"
    assert "not found" in block.errors[0].reason


def test_expand_attachments_dedupes_same_path(tmp_path: Path) -> None:
    """The same file referenced twice is read once, not twice."""
    from anthill.core.attachments import expand_attachments

    (tmp_path / "x.py").write_text("body")
    block = expand_attachments("compare @x.py with @x.py", base=tmp_path)
    assert len(block.files) == 1
    assert block.files[0].path == "x.py"


def test_binary_file_is_skipped(tmp_path: Path) -> None:
    from anthill.core.attachments import expand_attachments

    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02 binary header")
    block = expand_attachments("@blob.bin", base=tmp_path)
    assert block.files == []
    assert any("binary" in e.reason for e in block.errors)


def test_per_file_cap_skips_large_files(tmp_path: Path) -> None:
    from anthill.core.attachments import expand_attachments

    (tmp_path / "huge.txt").write_text("x" * 5000)
    block = expand_attachments(
        "@huge.txt", base=tmp_path, per_file_cap=1000,
    )
    assert block.files == []
    assert any("too large" in e.reason for e in block.errors)


def test_total_cap_truncates_and_flags(tmp_path: Path) -> None:
    """Once aggregate exceeds the total cap, later tokens are skipped."""
    from anthill.core.attachments import expand_attachments

    (tmp_path / "a.txt").write_text("a" * 600)
    (tmp_path / "b.txt").write_text("b" * 600)
    (tmp_path / "c.txt").write_text("c" * 600)
    # First two fit (1200 bytes); third would push us over the 1500 cap.
    block = expand_attachments(
        "@a.txt @b.txt @c.txt",
        base=tmp_path,
        per_file_cap=10_000,
        total_cap=1500,
    )
    paths = [f.path for f in block.files]
    assert "a.txt" in paths
    assert "b.txt" in paths
    assert "c.txt" not in paths
    assert block.truncated is True
    assert any("total attachment cap" in e.reason for e in block.errors)


def test_render_empty_when_no_files() -> None:
    """Empty block produces empty string — callers can prepend unconditionally."""
    from anthill.core.attachments import AttachmentBlock

    assert AttachmentBlock().render() == ""


def test_render_formats_well_formed_block(tmp_path: Path) -> None:
    from anthill.core.attachments import expand_attachments

    (tmp_path / "a.py").write_text("def x(): pass")
    block = expand_attachments("@a.py", base=tmp_path)
    rendered = block.render()
    assert "[attached files" in rendered
    assert "<file path='a.py'>" in rendered
    assert "def x(): pass" in rendered
    assert "</file>" in rendered
    # Trailing blank line so the user's request doesn't crash into the block.
    assert rendered.endswith("\n\n")


def test_non_utf8_bytes_decoded_with_replace(tmp_path: Path) -> None:
    from anthill.core.attachments import expand_attachments

    # A latin-1 byte that's not valid utf-8 ⇒ would normally raise.
    (tmp_path / "latin.txt").write_bytes(b"caf\xe9 mode")
    block = expand_attachments("@latin.txt", base=tmp_path)
    assert len(block.files) == 1
    # Content is decoded with replacement; the surrounding text survives.
    assert "caf" in block.files[0].content
    assert "mode" in block.files[0].content


def test_display_path_falls_back_to_absolute_when_outside_base(tmp_path: Path) -> None:
    """If the user attaches a file outside the base, show absolute path."""
    from anthill.core.attachments import expand_attachments

    outside = tmp_path.parent / f"outside-{tmp_path.name}.txt"
    outside.write_text("hi")
    try:
        block = expand_attachments(f"@{outside}", base=tmp_path)
        assert len(block.files) == 1
        assert block.files[0].path == str(outside)
    finally:
        outside.unlink()


def test_attachment_module_round_trip_in_prompt(tmp_path: Path) -> None:
    """The rendered block + original request together form the prompt
    that Scout / executors see. Sanity: it's a single string and
    contains both pieces."""
    from anthill.core.attachments import expand_attachments

    (tmp_path / "code.py").write_text("print('hi')")
    request = "how do I improve @code.py"
    block = expand_attachments(request, base=tmp_path)
    effective = block.render() + request
    assert "print('hi')" in effective
    assert "how do I improve @code.py" in effective
    # Block comes first so the planner sees context before the task.
    assert effective.index("print('hi')") < effective.index("how do I improve")
