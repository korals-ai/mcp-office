"""Read an Excel workbook's real cell values via openpyxl — in two steps.

Step one, ``list_sheets``, is an INDEX (title, used range, row count,
non-empty cell count per sheet) and never cell data. Step two,
``extract_sheet``, is ONE sheet's rows keyed by Excel row number, with
trailing blank cells trimmed and wholly-empty rows omitted.

Why two steps and why the trimming: a whole-workbook dump of a real six-sheet
tender costsheet was 54k chars, 43% of it the literal ``null`` (takeoff sheets
are ~80% empty). That crossed the agent's MCP tool-result ceiling, was parked
on disk, and the agent re-read one sheet blind. Trimming alone measured 63% of
the original — not enough on its own — so the default answer is the index and
the agent fetches per sheet, paging a big one with ``min_row``/``max_row``.

Values stay typed (numbers, strings, booleans; dates as ISO-8601 strings) and
formulas read as their last-computed result (``data_only=True``), not the
formula text — this is for reconciling values, not auditing formulas.

Importable contract:

    from pathlib import Path
    from src.xlsx_extract import XlsxExtractError, extract_sheet, list_sheets

    index = list_sheets(Path("/home/agent/costsheet.xlsx"))
    cells = extract_sheet(Path("/home/agent/costsheet.xlsx"), "Costsheet")
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, TypedDict

logger = logging.getLogger(__name__)

# Same rationale as pdf_text.MAX_TEXT_CHARS: this return value goes straight
# into the agent's tool_result / context, and an oversized message can crash
# the whole agent session (a multi-megabyte tool_result has been observed to
# do this). Bounds the rectangle ONE call walks (the requested rows, column A
# to the sheet's last column), never the workbook — the index is unbounded by
# construction. It is a cell-count bound, not a payload bound: a dense sheet
# under it can still produce a result the agent's CLI parks on disk, which is
# what min_row/max_row paging is for. A tender costsheet is normally tens to
# low hundreds of rows; this is a generous ceiling, not a target.
MAX_CELLS = 50_000


class XlsxExtractError(RuntimeError):
    """Reading the workbook failed: missing/corrupt source, unknown sheet
    name, or one sheet's cell count exceeds ``MAX_CELLS``."""


class SheetSummary(TypedDict):
    title: str
    dimensions: str
    rows: int
    non_empty_cells: int


class SheetIndex(TypedDict):
    sheets: list[SheetSummary]


class SheetCells(TypedDict):
    title: str
    dimensions: str
    rows: dict[str, list[Any]]


def _jsonable(value: object) -> Any:
    """Coerce an openpyxl cell value to something JSON-serializable over the
    MCP wire. openpyxl already returns plain ``int``/``float``/``str``/``bool``/
    ``None`` for ordinary cells; only date/time types need converting."""
    import datetime

    if isinstance(value, datetime.datetime | datetime.date | datetime.time):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        # A duration-formatted cell ([h]:mm:ss) — openpyxl hands back a
        # timedelta; seconds keep it a number the agent can sum.
        return value.total_seconds()
    return value


def _open_workbook(src: Path) -> Any:
    """Load ``src`` for value reads, or raise ``XlsxExtractError``. Returns an
    openpyxl ``Workbook`` (untyped upstream, hence ``Any``); the caller closes it."""
    if not src.exists():
        raise XlsxExtractError(f"source does not exist: {src}")
    if not src.is_file():
        raise XlsxExtractError(f"source is not a file: {src}")
    if src.suffix.lower() in {".xls", ".xlsb"}:
        # openpyxl's own message for these says "use xlrd" — a library the
        # agent's pod does not have; point at the tool that does exist.
        raise XlsxExtractError(
            f"{src.name} is {src.suffix.lower()}, not .xlsx — convert it first with "
            "mcp__workspace-tool-office__convert(src=..., to='xlsx') and read the .xlsx it writes"
        )

    import zipfile

    from openpyxl import load_workbook
    from openpyxl.utils.exceptions import InvalidFileException

    try:
        # Not read_only: the read-only worksheet drops `.dimensions`, and
        # these workbooks (tender costsheets, not million-row datasets) are
        # small enough that the fully-loaded model's extra memory is a
        # non-issue.
        return load_workbook(src, data_only=True)
    except (InvalidFileException, KeyError, zipfile.BadZipFile, OSError) as exc:
        # openpyxl raises a bare KeyError for some malformed OOXML zips.
        raise XlsxExtractError(f"could not open {src.name} as an xlsx: {exc}") from exc


def _count_non_empty_cells(ws: Any) -> int:
    # openpyxl's sparse cell store: only cells present in the sheet XML exist
    # here, so this is O(present cells). iter_rows() would materialise a Cell
    # for every coordinate of the A1..max rectangle — a single stray value at
    # Z200000 costs ~1 GB that way, over the office container's memory limit.
    return sum(1 for cell in ws._cells.values() if cell.value is not None)


def _worksheets_by_title(wb: Any) -> dict[str, Any]:
    # wb.sheetnames includes chart sheets, which have no cells/dimensions and
    # would raise a raw AttributeError; only real worksheets are readable.
    return {ws.title: ws for ws in wb.worksheets}


def list_sheets(src: Path) -> SheetIndex:
    """Index every sheet in the ``.xlsx`` at ``src``, in workbook order —
    titles and sizes only, never cell data, so it is safe on any workbook.

    Raises ``XlsxExtractError`` for a missing/non-file/corrupt source.
    """
    wb = _open_workbook(src)
    try:
        sheets: list[SheetSummary] = []
        for title, ws in _worksheets_by_title(wb).items():
            sheets.append(
                {
                    "title": title,
                    "dimensions": ws.dimensions,
                    "rows": ws.max_row - ws.min_row + 1,
                    "non_empty_cells": _count_non_empty_cells(ws),
                }
            )
        return {"sheets": sheets}
    finally:
        wb.close()


def _trim_trailing_blanks(row: list[Any]) -> list[Any]:
    end = len(row)
    while end and row[end - 1] is None:
        end -= 1
    return row[:end]


def _rows_by_excel_number(ws: Any, *, min_row: int, max_row: int) -> dict[str, list[Any]]:
    """Every non-empty row of ``ws`` in ``[min_row, max_row]`` (0 = open end),
    keyed by its 1-based Excel row number.

    Columns stay positional from A (index 0) even when the used range starts
    further right, so the agent can still address F3 as ``rows["3"][5]``;
    only TRAILING blanks are dropped, blanks between values are kept.
    """
    rows: dict[str, list[Any]] = {}
    first = min_row or 1
    walk = ws.iter_rows(min_row=first, max_row=max_row or None, values_only=True)
    for number, row in enumerate(walk, start=first):
        trimmed = _trim_trailing_blanks([_jsonable(cell) for cell in row])
        if trimmed:
            rows[str(number)] = trimmed
    return rows


def extract_sheet(src: Path, sheet: str, *, min_row: int = 0, max_row: int = 0) -> SheetCells:
    """Read one sheet's cell values from the ``.xlsx`` at ``src``.

    Returns ``{"title", "dimensions", "rows"}`` where ``rows`` maps the
    Excel row number (as a string) to that row's cells from column A, trailing
    blanks trimmed, wholly-empty rows omitted — see ``_rows_by_excel_number``.
    ``min_row``/``max_row`` (1-based, inclusive, 0 = open) page a big sheet;
    the keys stay the sheet's real row numbers.

    Raises ``XlsxExtractError`` for a missing/non-file/corrupt source, an
    unknown ``sheet`` title (the message lists the real worksheets), a bad
    window, or a window whose column-A-to-last-column rectangle exceeds
    ``MAX_CELLS``.
    """
    if min_row < 0 or max_row < 0 or (max_row and min_row and max_row < min_row):
        raise XlsxExtractError(f"bad row window min_row={min_row} max_row={max_row}")
    wb = _open_workbook(src)
    try:
        by_title = _worksheets_by_title(wb)
        ws = by_title.get(sheet)
        if ws is None:
            raise XlsxExtractError(
                f"sheet {sheet!r} not found in {src.name} — available: {list(by_title)}"
            )
        # iter_rows walks column A to max_column for every row in the window
        # whatever the used range's top-left is, so that rectangle is what the
        # ceiling must bound. It counts formatted-but-empty cells too: a
        # border applied far below the data widens the range.
        first = min_row or 1
        last = min(max_row, ws.max_row) if max_row else ws.max_row
        cells = max(0, last - first + 1) * ws.max_column
        if cells > MAX_CELLS:
            raise XlsxExtractError(
                f"sheet {sheet!r} in {src.name} spans {cells} cells (rows {first}-{last}, "
                f"column A to {ws.dimensions.split(':')[-1]}; formatted empty cells count), "
                f"over this tool's {MAX_CELLS}-cell ceiling for ONE sheet call — page it "
                "with min_row/max_row"
            )
        return {
            "title": sheet,
            "dimensions": ws.dimensions,
            "rows": _rows_by_excel_number(ws, min_row=min_row, max_row=max_row),
        }
    finally:
        wb.close()
