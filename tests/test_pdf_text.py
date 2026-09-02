"""Tests for the PDF text-extraction logic.

These run without pdftotext installed (CI pre_build has no poppler-utils), so
they cover the input-validation and error-mapping paths that don't need the
real binary — the actual extraction is exercised by the post-deploy
integration suite / manual QA against the running sidecar.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.pdf_text import (
    MAX_TEXT_CHARS,
    PdfTextExtractError,
    extract_text,
)


def test_missing_source_raises(tmp_path: Path) -> None:
    with pytest.raises(PdfTextExtractError, match="source does not exist"):
        extract_text(tmp_path / "nope.pdf")


def test_directory_source_raises(tmp_path: Path) -> None:
    d = tmp_path / "adir"
    d.mkdir()
    with pytest.raises(PdfTextExtractError, match="source is not a file"):
        extract_text(d)


def test_missing_pdftotext_raises_with_install_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.pdf_text as pt

    monkeypatch.setattr(pt.shutil, "which", lambda _name: None)
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"%PDF-1.4 fake")
    with pytest.raises(PdfTextExtractError, match="not on PATH"):
        extract_text(src)


def test_truncates_oversized_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tool_result this function returns goes straight into the agent's
    context — cap it rather than risk a repeat of the 2026-08-04 SDK
    message-buffer incident."""
    import src.pdf_text as pt

    monkeypatch.setattr(pt.shutil, "which", lambda _name: "/usr/bin/pdftotext")

    huge = "x" * (MAX_TEXT_CHARS + 5000)

    def _fake_run(argv: list[str], *, timeout_s: float) -> tuple[int, bytes, str]:
        return 0, huge.encode(), ""

    monkeypatch.setattr(pt, "_run_with_timeout", _fake_run)
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"%PDF-1.4 fake")

    result = extract_text(src)
    assert len(result) < len(huge)
    assert "truncated" in result


def test_empty_extraction_is_not_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A PDF whose text isn't machine-recoverable (scanned, or per-glyph
    vector outlines) returns "" — not an exception."""
    import src.pdf_text as pt

    monkeypatch.setattr(pt.shutil, "which", lambda _name: "/usr/bin/pdftotext")

    def _fake_run(argv: list[str], *, timeout_s: float) -> tuple[int, bytes, str]:
        return 0, b"", ""

    monkeypatch.setattr(pt, "_run_with_timeout", _fake_run)
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"%PDF-1.4 fake")

    assert extract_text(src) == ""
