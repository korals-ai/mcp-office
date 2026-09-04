"""Tests for the structured Excel cell-read logic.

Pure-python (openpyxl), so this runs a real round-trip: write a workbook
with openpyxl, read it back with ``extract_cells``, assert on the values —
no soffice/poppler needed.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from src.xlsx_extract import XlsxExtractError, extract_cells


def _write_workbook(dest: Path) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    ws1 = wb.active
    ws1.title = "Costsheet"
    ws1.append(["P/N", "Qty", "Unit Price"])
    ws1.append(["PNM-C32083RVQ", 4, 121.4])
    ws1.append([None, None, None])  # a blank row inside the used range
    ws1.append(["1LAN-SFP-4305BC-U", 5, 0])

    ws2 = wb.create_sheet("Notes")
    ws2.append(["Reviewed", datetime.date(2026, 9, 4)])

    wb.save(dest)


def test_missing_source_raises(tmp_path: Path) -> None:
    with pytest.raises(XlsxExtractError, match="source does not exist"):
        extract_cells(tmp_path / "nope.xlsx")


def test_directory_source_raises(tmp_path: Path) -> None:
    d = tmp_path / "adir"
    d.mkdir()
    with pytest.raises(XlsxExtractError, match="source is not a file"):
        extract_cells(d)


def test_corrupt_file_raises(tmp_path: Path) -> None:
    src = tmp_path / "bad.xlsx"
    src.write_bytes(b"not a real xlsx")
    with pytest.raises(XlsxExtractError, match="could not open"):
        extract_cells(src)


def test_reads_all_sheets_in_order_with_typed_values(tmp_path: Path) -> None:
    src = tmp_path / "book.xlsx"
    _write_workbook(src)

    result = extract_cells(src)

    assert list(result.keys()) == ["Costsheet", "Notes"]
    assert result["Costsheet"]["dimensions"] == "A1:C4"
    rows = result["Costsheet"]["rows"]
    assert rows[0] == ["P/N", "Qty", "Unit Price"]
    assert rows[1] == ["PNM-C32083RVQ", 4, 121.4]
    assert rows[2] == [None, None, None]  # blank row preserved, not dropped
    assert rows[3] == ["1LAN-SFP-4305BC-U", 5, 0]  # a real 0, not blank


def test_date_cells_are_jsonable(tmp_path: Path) -> None:
    src = tmp_path / "book.xlsx"
    _write_workbook(src)

    result = extract_cells(src)

    assert result["Notes"]["rows"][0] == ["Reviewed", "2026-09-04T00:00:00"]


def test_sheet_filter_restricts_to_one_sheet(tmp_path: Path) -> None:
    src = tmp_path / "book.xlsx"
    _write_workbook(src)

    result = extract_cells(src, sheet="Notes")

    assert list(result.keys()) == ["Notes"]


def test_unknown_sheet_name_raises_with_available_list(tmp_path: Path) -> None:
    src = tmp_path / "book.xlsx"
    _write_workbook(src)

    with pytest.raises(XlsxExtractError, match=r"not found.*Costsheet.*Notes"):
        extract_cells(src, sheet="Nope")


def test_oversized_workbook_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.xlsx_extract.MAX_CELLS", 2)
    src = tmp_path / "book.xlsx"
    _write_workbook(src)

    with pytest.raises(XlsxExtractError, match="exceeds"):
        extract_cells(src)
