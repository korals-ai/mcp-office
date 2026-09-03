"""Tests for the office conversion logic.

These run without LibreOffice installed (CI pre_build has no soffice), so
they cover the input-validation and error-mapping paths that don't need a
real ``soffice`` — the binary fork itself is exercised by the post-deploy
integration suite against the running sidecar.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.office_convert import (
    SUPPORTED_FORMATS,
    OfficeConvertError,
    convert,
)


def test_supported_formats_includes_core_roundtrips() -> None:
    for fmt in ("pdf", "docx", "xlsx", "pptx", "txt"):
        assert fmt in SUPPORTED_FORMATS


def test_unsupported_format_rejected_before_touching_disk(tmp_path: Path) -> None:
    src = tmp_path / "doc.rtf"
    src.write_text("hi")
    with pytest.raises(OfficeConvertError, match="unsupported output format"):
        convert(src, tmp_path, to="xyz")


def test_missing_source_raises(tmp_path: Path) -> None:
    with pytest.raises(OfficeConvertError, match="source does not exist"):
        convert(tmp_path / "nope.docx", tmp_path, to="pdf")


def test_directory_source_raises(tmp_path: Path) -> None:
    d = tmp_path / "adir"
    d.mkdir()
    with pytest.raises(OfficeConvertError, match="source is not a file"):
        convert(d, tmp_path, to="pdf")


def test_missing_soffice_raises_with_install_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With soffice absent from PATH, a valid request should fail at binary
    # resolution with an actionable message (not a cryptic FileNotFoundError).
    import src.office_convert as oc

    monkeypatch.setattr(oc.shutil, "which", lambda _name: None)
    src = tmp_path / "doc.rtf"
    src.write_text(r"{\rtf1\ansi hi}")
    with pytest.raises(OfficeConvertError, match="not on PATH"):
        convert(src, tmp_path, to="pdf")
