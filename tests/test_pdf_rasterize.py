"""Tests for the PDF rasterization logic.

These run without pdftoppm installed (CI pre_build has no poppler-utils), so
they cover the input-validation and error-mapping paths that don't need the
real binary — the actual rasterization is exercised by the post-deploy
integration suite / manual QA against the running sidecar.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.pdf_rasterize import (
    MAX_DPI,
    MIN_DPI,
    PdfRasterizeError,
    pdf_to_images,
)


def test_dpi_below_minimum_rejected_before_touching_disk(tmp_path: Path) -> None:
    src = tmp_path / "plan.pdf"
    src.write_bytes(b"%PDF-1.4 fake")
    with pytest.raises(PdfRasterizeError, match="outside the supported range"):
        pdf_to_images(src, tmp_path, dpi=MIN_DPI - 1)


def test_dpi_above_maximum_rejected_before_touching_disk(tmp_path: Path) -> None:
    src = tmp_path / "plan.pdf"
    src.write_bytes(b"%PDF-1.4 fake")
    with pytest.raises(PdfRasterizeError, match="outside the supported range"):
        pdf_to_images(src, tmp_path, dpi=MAX_DPI + 1)


def test_missing_source_raises(tmp_path: Path) -> None:
    with pytest.raises(PdfRasterizeError, match="source does not exist"):
        pdf_to_images(tmp_path / "nope.pdf", tmp_path)


def test_directory_source_raises(tmp_path: Path) -> None:
    d = tmp_path / "adir"
    d.mkdir()
    with pytest.raises(PdfRasterizeError, match="source is not a file"):
        pdf_to_images(d, tmp_path)


def test_missing_pdftoppm_raises_with_install_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.pdf_rasterize as pr

    monkeypatch.setattr(pr.shutil, "which", lambda _name: None)
    src = tmp_path / "plan.pdf"
    src.write_bytes(b"%PDF-1.4 fake")
    with pytest.raises(PdfRasterizeError, match="not on PATH"):
        pdf_to_images(src, tmp_path)


def test_page_number_sorts_by_numeric_value_not_lexicographic(tmp_path: Path) -> None:
    from src.pdf_rasterize import _page_number

    # pdftoppm zero-pads (e.g. plan-01.png..plan-10.png), but a robust sort
    # must not depend on that — extract the integer and compare numerically.
    p9 = tmp_path / "plan-9.png"
    p10 = tmp_path / "plan-10.png"
    assert _page_number(p9, prefix="plan") < _page_number(p10, prefix="plan")


def test_page_number_unmatched_name_defaults_to_zero(tmp_path: Path) -> None:
    from src.pdf_rasterize import _page_number

    assert _page_number(tmp_path / "unrelated.png", prefix="plan") == 0
