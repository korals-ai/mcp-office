"""Toolspace sidecar (office) — MCP server over Streamable HTTP.

Exposes LibreOffice document conversion as an MCP tool the workspace agent
calls over ``http://localhost:8090/mcp`` (the two containers share the pod
network namespace). The file itself never crosses the wire: the agent names
a path on the shared tenant PVC, this sidecar opens it in place, runs
``soffice``, and writes the result back to the PVC — the RPC carries only the
path + verdict (zero-copy data plane; see
docs/plan/20260619-200506-toolspace-sidecar.md §6b).

Transport is **Streamable HTTP** (one of MCP's two standard transports) rather
than stdio precisely because the server lives in a separate container from the
agent — stdio would require the SDK to spawn it as a child, defeating the
sidecar split.

The tool surface IS the agent-facing contract: the tool name, description, and
typed signature are what the agent reads to decide when/how to convert. No
separate "skill" doc is needed.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from src.office_author import (
    OfficeAuthorError,
)
from src.office_author import (
    author_docx as _author_docx,
)
from src.office_author import (
    author_pdf as _author_pdf,
)
from src.office_author import (
    author_pptx as _author_pptx,
)
from src.office_author import (
    author_xlsx as _author_xlsx,
)
from src.office_convert import (
    SUPPORTED_FORMATS,
    OfficeConvertError,
)
from src.office_convert import (
    convert as _convert,
)
from src.office_shell import OfficeShellError
from src.office_shell import run_shell as _run_shell
from src.pdf_rasterize import PdfRasterizeError
from src.pdf_rasterize import pdf_to_images as _pdf_to_images
from src.pdf_text import PdfTextExtractError
from src.pdf_text import extract_text as _extract_text
from src.xlsx_extract import XlsxExtractError
from src.xlsx_extract import extract_cells as _extract_cells

log = logging.getLogger("workspace-tool-office")

# Bind on all interfaces inside the pod; the workspace container reaches this
# over localhost. Distinct from the workspace server's :8080 so the two share
# a pod network namespace without colliding. Override via env for tests.
HOST = "0.0.0.0"  # noqa: S104 - pod-local bind; nothing injects a host, the pod netns is the fence
PORT = int(os.environ["WORKSPACE_TOOL_PORT"])

# FastMCP serves the Streamable HTTP endpoint at ``/mcp`` by default; the
# workspace SDK registers ``http://localhost:8090/mcp`` (phase 3b).
mcp = FastMCP("office", host=HOST, port=PORT)


@mcp.tool()
def convert(src: str, to: str = "pdf") -> str:
    """Convert an Office/Open document to another format with LibreOffice.

    Use this to round-trip tender artifacts — e.g. DOCX→PDF for delivery,
    PDF→DOCX for an editable copy, HTML→DOCX to author a proposal, DOCX→TXT
    for a plain-text dump.

    Args:
        src: Absolute path to the source document on the shared workspace
            volume (e.g. ``/home/agent/rfp.docx``). The file is read in place.
        to: Output format — one of docx, xlsx, pptx, odt, ods, odp, pdf, rtf,
            html, txt, doc, xls, ppt. Defaults to ``pdf``.

    Returns:
        The absolute path of the written output file (``<stem>.<ext>`` next to
        the source), as a string.

    Raises:
        Conversion failures (unsupported format, missing/corrupt source,
        soffice error or timeout) surface as an MCP tool error with a
        human-readable message.
    """
    source = Path(src)
    started = time.monotonic()
    try:
        out = _convert(source, source.parent, to=to)
    except OfficeConvertError as exc:
        # One structured line per call so Loki can chart error rate / spot a
        # broken input without per-pod scraping. Keys match the ocr sidecar.
        log.warning(
            "tool=office op=convert outcome=error dur_ms=%d to=%s src=%s err=%s",
            int((time.monotonic() - started) * 1000),
            to,
            source.name,
            exc,
        )
        # FastMCP turns a raised exception into an MCP tool error result.
        # Fold soffice's stderr into the message so the agent sees why.
        detail = f": {exc.stderr.strip()}" if exc.stderr else ""
        raise OfficeConvertError(f"{exc}{detail}") from exc
    log.info(
        "tool=office op=convert outcome=ok dur_ms=%d to=%s src=%s",
        int((time.monotonic() - started) * 1000),
        to,
        source.name,
    )
    return str(out)


@mcp.tool()
def pdf_to_images(src: str, dpi: int = 150) -> list[str]:
    """Rasterize every page of a PDF to a PNG image, so the agent can visually
    read a drawing that only exists as PDF (any technical drawing — floor
    plan, structural/framing plan, site plan, etc. — for PDF-to-CAD
    reconstruction; see the ``pdf-to-cad-reconstruction`` skill).

    This is one step in a free, best-effort reconstruction pipeline, NOT a
    commercial-grade vectorization product — quality depends heavily on the
    source PDF, and it can fail badly on some real-world drawings (see the
    skill for a confirmed real example). Set the user's expectations
    accordingly rather than presenting the result as a certified redraw.

    Prefer ``pdf_extract_text`` for any text on the page — it returns real
    embedded characters when the PDF has them, which is more reliable than
    reading a label off this rasterized image.

    Args:
        src: Absolute path to the source PDF on the shared workspace volume
            (e.g. ``/home/agent/floorplan.pdf``). The file is read in place.
        dpi: Rasterization resolution, 50-600. Defaults to 150 — enough to
            read text/dimensions on a typical drawing without producing an
            unreasonably large image.

    Returns:
        Absolute paths of the written page images (``<stem>-<n>.png`` next to
        the source, one per page, in document order), as a list of strings.

    Raises:
        Rasterization failures (out-of-range dpi, missing/corrupt source,
        pdftoppm error or timeout) surface as an MCP tool error with a
        human-readable message.
    """
    source = Path(src)
    started = time.monotonic()
    try:
        pages = _pdf_to_images(source, source.parent, dpi=dpi)
    except PdfRasterizeError as exc:
        log.warning(
            "tool=office op=pdf_to_images outcome=error dur_ms=%d dpi=%d src=%s err=%s",
            int((time.monotonic() - started) * 1000),
            dpi,
            source.name,
            exc,
        )
        detail = f": {exc.stderr.strip()}" if exc.stderr else ""
        raise PdfRasterizeError(f"{exc}{detail}") from exc
    log.info(
        "tool=office op=pdf_to_images outcome=ok dur_ms=%d dpi=%d pages=%d src=%s",
        int((time.monotonic() - started) * 1000),
        dpi,
        len(pages),
        source.name,
    )
    return [str(p) for p in pages]


@mcp.tool()
def pdf_extract_text(src: str) -> str:
    """Extract a PDF's real embedded text (all pages) — the deterministic
    alternative to reading a label off a rasterized image. Call this BEFORE
    visually reading a page for text: real embedded characters are exact and
    hallucination-free, unlike a vision-read label.

    An empty (or near-empty) result on a page that visually has lots of text
    is itself meaningful: it means this PDF's text isn't real character data
    (often a scanned page, or — confirmed on a real tender drawing,
    2026-08-04 — a CAD-to-PDF print driver that rendered annotation text as
    per-glyph vector outlines instead of real text). In that case, vision
    reading (or asking the user) is the only option left for that content —
    don't treat the empty result as a tool failure.

    Args:
        src: Absolute path to the source PDF on the shared workspace volume.
            The file is read in place.

    Returns:
        The extracted text (roughly layout-preserved), as a string. Empty
        string if the PDF has no machine-recoverable text — not an error.

    Raises:
        Extraction failures (missing/corrupt source, pdftotext error or
        timeout) surface as an MCP tool error with a human-readable message.
    """
    source = Path(src)
    started = time.monotonic()
    try:
        text = _extract_text(source)
    except PdfTextExtractError as exc:
        log.warning(
            "tool=office op=pdf_extract_text outcome=error dur_ms=%d src=%s err=%s",
            int((time.monotonic() - started) * 1000),
            source.name,
            exc,
        )
        detail = f": {exc.stderr.strip()}" if exc.stderr else ""
        raise PdfTextExtractError(f"{exc}{detail}") from exc
    log.info(
        "tool=office op=pdf_extract_text outcome=ok dur_ms=%d chars=%d src=%s",
        int((time.monotonic() - started) * 1000),
        len(text),
        source.name,
    )
    return text


@mcp.tool()
def xlsx_extract_cells(src: str, sheet: str = "") -> dict[str, dict[str, object]]:
    """Read an Excel workbook's real cell values — the deterministic
    alternative to converting it to text/CSV and reparsing, or to writing a
    one-off Bash + openpyxl script.

    Use this to reconcile a spreadsheet's numbers (e.g. a tender costsheet
    against a client-facing price letter) instead of a hand-rolled script:
    it returns typed values (numbers stay numbers), preserves blank
    rows/columns so row numbers still line up with the source file, and reads
    formulas as their last-computed result, not the formula text.

    Args:
        src: Absolute path to the source ``.xlsx`` on the shared workspace
            volume. The file is read in place.
        sheet: Restrict to one sheet by name (Excel's tab name), e.g.
            "Costsheet Corning". Defaults to "" — all sheets.

    Returns:
        ``{sheet_title: {"dimensions": "A1:D10", "rows": [[...], ...]}}``,
        one entry per sheet (or just the requested one). ``rows`` is every
        row in the sheet's used range, each a list of cell values in column
        order.

    Raises:
        An MCP tool error for a missing/corrupt source, an unknown sheet
        name, or a workbook too large for this tool (ask the user to split
        it or narrow ``sheet``).
    """
    source = Path(src)
    started = time.monotonic()
    try:
        sheets = _extract_cells(source, sheet=sheet or None)
    except XlsxExtractError as exc:
        log.warning(
            "tool=office op=xlsx_extract_cells outcome=error dur_ms=%d sheet=%s src=%s err=%s",
            int((time.monotonic() - started) * 1000),
            sheet or "*",
            source.name,
            exc,
        )
        raise
    log.info(
        "tool=office op=xlsx_extract_cells outcome=ok dur_ms=%d sheets=%d src=%s",
        int((time.monotonic() - started) * 1000),
        len(sheets),
        source.name,
    )
    return sheets


@mcp.tool()
def office_shell(cmd: str, cwd: str = "") -> dict[str, object]:
    """Run a shell command inside this office tool pod — the fallback for
    whatever the other tools here (``convert``, ``pdf_extract_text``,
    ``xlsx_extract_cells``, the ``author_*`` tools) don't cover.

    Prefer a curated tool above whenever one fits — it's faster, tested, and
    the sanctioned path (spreadsheet reads: ``xlsx_extract_cells``; PDF text:
    ``pdf_extract_text``). Reach for this only for genuinely uncovered work,
    typically something that needs LibreOffice (``soffice``) or another
    binary on this pod's PATH with no MCP equivalent. Every call here is
    logged and alerted on, so it should stay rare — a repeated pattern is a
    signal to ask for a proper tool instead of reaching for this again.

    Args:
        cmd: The shell command to run, exactly as you'd give it to Bash
            (e.g. ``"soffice --headless --convert-to pdf --outdir /home/agent /home/agent/x.docx"``).
        cwd: Absolute directory to run it in. Defaults to the shared
            workspace volume root.

    Returns:
        ``{"exit_code": int, "stdout": str, "stderr": str, "timed_out": bool}``.
        A non-zero exit code or ``timed_out: true`` is a normal result, not
        an MCP tool error — read stdout/stderr and decide what to do next,
        the same way you would after a failed Bash call.

    Raises:
        An MCP tool error only when the command never started at all (empty
        command, or ``cwd`` doesn't exist).
    """
    started = time.monotonic()
    try:
        result = _run_shell(cmd, cwd=Path(cwd) if cwd else None)
    except OfficeShellError as exc:
        log.warning(
            "tool=office op=office_shell outcome=error dur_ms=%d cmd=%s err=%s",
            int((time.monotonic() - started) * 1000),
            cmd[:500],
            exc,
        )
        raise
    log.info(
        "tool=office op=office_shell outcome=ok dur_ms=%d exit_code=%d timed_out=%s cmd=%s",
        int((time.monotonic() - started) * 1000),
        result.exit_code,
        result.timed_out,
        cmd[:500],
    )
    return {
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "timed_out": result.timed_out,
    }


def _log_author(op: str, dest: Path, started: float, ok: bool, err: object = "") -> None:
    line = "tool=office op=%s outcome=%s dur_ms=%d dst=%s"
    args = [op, "ok" if ok else "error", int((time.monotonic() - started) * 1000), dest.name]
    if not ok:
        line += " err=%s"
        args.append(str(err))
    (log.info if ok else log.warning)(line, *args)


@mcp.tool()
def author_xlsx(
    path: str,
    rows: list[list[object]],
    sheet_name: str = "Sheet1",
    bold_header: bool = True,
) -> str:
    """Create a real Excel ``.xlsx`` workbook from tabular data.

    This is the ONLY correct way to produce an ``.xlsx`` — do NOT hand-write a
    spreadsheet with the Write tool or a Bash script. A ``.xlsx`` is a binary
    ZIP package; text written under an ``.xlsx`` name is a corrupt file that
    won't open. This tool builds a valid workbook with openpyxl.

    Args:
        path: Absolute destination path on the shared workspace volume
            (e.g. ``/home/agent/cash_position.xlsx``). Parent dirs are created.
        rows: The sheet contents as a list of rows, each a list of cell values
            (strings/numbers/booleans/null), e.g.
            ``[["name","value"],["apple",1],["banana",2]]``.
        sheet_name: Worksheet name (Excel caps it at 31 chars). Defaults to
            ``Sheet1``.
        bold_header: Bold the first row. Defaults to true.

    Returns:
        The absolute path of the written ``.xlsx``.

    Raises:
        An MCP tool error with a human-readable message on an empty/invalid
        spec or an unwritable path.
    """
    dest = Path(path)
    started = time.monotonic()
    try:
        out = _author_xlsx(dest, rows, sheet_name=sheet_name, bold_header=bold_header)
    except OfficeAuthorError as exc:
        _log_author("author_xlsx", dest, started, ok=False, err=exc)
        raise
    _log_author("author_xlsx", out, started, ok=True)
    return str(out)


@mcp.tool()
def author_docx(path: str, title: str = "", paragraphs: list[str] | None = None) -> str:
    """Create a real Word ``.docx`` document.

    The ONLY correct way to produce a ``.docx`` — do NOT hand-write one with the
    Write tool (a ``.docx`` is a binary ZIP package; text under a ``.docx`` name
    is corrupt). Builds a valid document with python-docx.

    Args:
        path: Absolute destination path on the shared workspace volume. Parent
            dirs are created.
        title: Optional level-1 heading at the top of the document.
        paragraphs: Body paragraphs, one string each. Provide a title and/or at
            least one paragraph.

    Returns:
        The absolute path of the written ``.docx``.

    Raises:
        An MCP tool error on an empty/invalid spec or an unwritable path.
    """
    dest = Path(path)
    started = time.monotonic()
    try:
        out = _author_docx(dest, title or None, paragraphs or [])
    except OfficeAuthorError as exc:
        _log_author("author_docx", dest, started, ok=False, err=exc)
        raise
    _log_author("author_docx", out, started, ok=True)
    return str(out)


@mcp.tool()
def author_pptx(path: str, slides: list[dict[str, object]]) -> str:
    """Create a real PowerPoint ``.pptx`` presentation.

    The ONLY correct way to produce a ``.pptx`` — do NOT hand-write one with the
    Write tool. Builds a valid deck with python-pptx.

    Args:
        path: Absolute destination path on the shared workspace volume. Parent
            dirs are created.
        slides: One entry per slide, each ``{"title": str, "bullets": [str,
            ...]}`` (``bullets`` optional). Produces Title-and-Content slides.

    Returns:
        The absolute path of the written ``.pptx``.

    Raises:
        An MCP tool error on an empty/invalid spec or an unwritable path.
    """
    dest = Path(path)
    started = time.monotonic()
    try:
        out = _author_pptx(dest, slides)
    except OfficeAuthorError as exc:
        _log_author("author_pptx", dest, started, ok=False, err=exc)
        raise
    _log_author("author_pptx", out, started, ok=True)
    return str(out)


@mcp.tool()
def author_pdf(path: str, title: str = "", paragraphs: list[str] | None = None) -> str:
    """Create a real ``.pdf`` from text content (authored as a document, then
    rendered by LibreOffice).

    Use this for a simple text/report PDF. For a PDF *of a spreadsheet or an
    existing document*, author/obtain that file first, then call ``convert`` with
    ``to="pdf"``.

    Args:
        path: Absolute destination ``.pdf`` path on the shared workspace volume.
            Parent dirs are created.
        title: Optional heading at the top.
        paragraphs: Body paragraphs, one string each.

    Returns:
        The absolute path of the written ``.pdf``.

    Raises:
        An MCP tool error on an empty/invalid spec, a render failure, or an
        unwritable path.
    """
    dest = Path(path)
    started = time.monotonic()
    try:
        out = _author_pdf(dest, title or None, paragraphs or [])
    except OfficeAuthorError as exc:
        _log_author("author_pdf", dest, started, ok=False, err=exc)
        raise
    _log_author("author_pdf", out, started, ok=True)
    return str(out)


def main() -> None:
    """Run the MCP server forever over Streamable HTTP. Blocks; entrypoint."""
    logging.basicConfig(level=logging.INFO)
    log.info(
        "workspace-tool-office MCP server on %s:%d (/mcp) — formats: %s",
        HOST,
        PORT,
        ", ".join(SUPPORTED_FORMATS),
    )
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
