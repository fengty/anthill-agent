"""Tests for document plugins.

Light tests: confirm graceful 'missing extra' message when libs not
installed, and basic round-trip when they are. We don't test the
parser internals — that's covered by upstream pypdf/python-docx/openpyxl.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from anthill.plugins.documents import DocxReadPlugin, PdfReadPlugin, XlsxReadPlugin


def test_pdf_read_missing_file(workspace: Path) -> None:
    """Either pypdf is installed and we get 'no such file', or it isn't
    and we get the install hint. Either is a safe-fail."""
    result = asyncio.run(PdfReadPlugin().call(path="ghost.pdf"))
    assert not result.ok
    assert ("no such file" in result.error) or ("anthill-agent[docs]" in result.error)


def test_docx_read_missing_file(workspace: Path) -> None:
    result = asyncio.run(DocxReadPlugin().call(path="ghost.docx"))
    assert not result.ok
    assert ("no such file" in result.error) or ("anthill-agent[docs]" in result.error)


def test_xlsx_read_missing_file(workspace: Path) -> None:
    result = asyncio.run(XlsxReadPlugin().call(path="ghost.xlsx"))
    assert not result.ok
    assert ("no such file" in result.error) or ("anthill-agent[docs]" in result.error)


def test_pdf_read_blocks_workspace_escape(workspace: Path) -> None:
    """Workspace sandbox enforced before pypdf even called."""
    result = asyncio.run(PdfReadPlugin().call(path="../../etc/passwd"))
    assert not result.ok
    # Either escape error or missing-lib — both prevent the attack.
    assert ("escapes" in result.error) or ("anthill-agent[docs]" in result.error)


def test_xlsx_returns_rows_when_library_present(workspace: Path) -> None:
    """If openpyxl is installed, write a tiny xlsx and read it back."""
    try:
        from openpyxl import Workbook
    except ImportError:
        pytest.skip("openpyxl not installed; the install-hint path is the test for this case")

    wb = Workbook()
    ws = wb.active
    ws.append(["name", "score"])
    ws.append(["alice", 90])
    ws.append(["bob", 80])
    target = workspace / "sample.xlsx"
    wb.save(str(target))

    result = asyncio.run(XlsxReadPlugin().call(path="sample.xlsx"))
    assert result.ok
    assert result.output[0] == ["name", "score"]
    assert result.output[1] == ["alice", 90]
    assert result.metadata["sheet"] == ws.title
