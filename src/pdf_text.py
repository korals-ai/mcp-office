"""Extract a PDF's real embedded text via ``pdftotext`` (poppler-utils).

Companion to ``pdf_rasterize.py``: where that module lets the agent *see* a
PDF page, this module gives it a **deterministic** alternative for text —
real embedded characters straight out of the PDF's content stream, not the
agent's visual reading of a raster image. Where the source PDF actually
carries real text, this is strictly more reliable than vision-reading a
label off a picture: zero hallucination risk, exact characters, real
positions available in the byte layout.

**This does not always work — and that's a meaningful signal, not a
failure.** Verified 2026-08-04 against a real tender PDF (an AutoCAD
drawing printed to PDF via an old PostScript driver,
`PScript5.dll`): ``pdftotext`` recovered almost nothing (one stray name)
because the drawing's annotation text was rendered as per-glyph vector
outlines, not real character data — the same drawings that, run through
``pdftoppm -svg``-style raw vector extraction, explode into 100k+ tiny
paths (see ``docs/plan/`` PDF-to-CAD notes / the 2026-08-04 PDF-to-DXF OOM
incident). So: call this FIRST. A non-empty, substantial result means the
source has real text — prefer it over vision-reading labels. An empty or
near-empty result on a page that visually has lots of text is itself the
signal that this PDF's text is not machine-recoverable this way, and vision
reading (or asking the user) is the only option left — the
``pdf-to-cad-reconstruction`` skill uses this distinction explicitly.

``pdftotext`` is free (GPL, part of poppler-utils — already vendored for
``pdftoppm``, no new licensing cost, no new Python dependency, no new
Dockerfile change since it ships in the same package).

Importable contract:

    from pathlib import Path
    from src.pdf_text import extract_text, PdfTextExtractError

    text = extract_text(Path("/home/agent/drawing.pdf"))
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_PDFTOTEXT_CANDIDATES = ("pdftotext",)

# Text extraction is CPU-light (no rasterization); a multi-page tender PDF
# finishes in well under a second normally. 30s leaves headroom for a
# pathologically large document without letting a stuck call wedge a turn.
_DEFAULT_TIMEOUT_S = 30.0

# Bound the returned string. This tool exists specifically to feed an MCP
# tool_result the agent reads directly (no file/Read round-trip) — and this
# repo just shipped a fix for a real incident where an oversized tool_result
# killed the whole SDK session (docs/incidents/2026-08-04-workspace-sdk-
# 1mib-message-buffer.md). A PDF technical drawing's real text (labels,
# dimensions, notes) is normally tiny; anything approaching this cap is an
# unusual document this tool isn't meant for, not something to silently
# truncate-and-hope.
MAX_TEXT_CHARS = 200_000


class PdfTextExtractError(RuntimeError):
    """Text extraction failed: missing binary, bad source, pdftotext
    non-zero exit, or timeout. ``stderr`` carries pdftotext's last words
    when relevant."""

    def __init__(self, msg: str, *, stderr: str | None = None) -> None:
        super().__init__(msg)
        self.stderr = stderr


def _find_pdftotext() -> str:
    for name in _PDFTOTEXT_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    raise PdfTextExtractError(
        "pdftotext not on PATH — install poppler-utils or run inside the "
        "workspace-tool-office image"
    )


def _run_with_timeout(argv: list[str], *, timeout_s: float) -> tuple[int, bytes, str]:
    """Run ``argv`` with a hard timeout; SIGKILL the whole process group on
    timeout. Returns ``(exit_code, stdout_bytes, stderr_text)``."""
    started = time.monotonic()
    proc = subprocess.Popen(  # noqa: S603 - argv source is trusted, see module docstring
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout_b, stderr_b = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        try:
            stdout_b, stderr_b = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stdout_b, stderr_b = b"", b""
        elapsed = time.monotonic() - started
        raise PdfTextExtractError(
            f"pdftotext timed out after {elapsed:.1f}s (limit {timeout_s:.1f}s)",
            stderr=stderr_b.decode("utf-8", errors="replace"),
        ) from None
    return proc.returncode, stdout_b, stderr_b.decode("utf-8", errors="replace")


def extract_text(
    src: Path,
    *,
    preserve_layout: bool = True,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> str:
    """Extract a PDF's real embedded text (all pages), or ``""`` if it has
    none machine-recoverable.

    ``src`` must exist and be a file pdftotext can open. ``preserve_layout``
    (default) keeps roughly the original spatial layout (columns, table-like
    alignment) rather than reflowing into plain paragraphs — usually better
    for reading labels/dimensions off a technical drawing.

    An **empty string is a normal, meaningful result** (a scanned page, or a
    drawing whose annotation text is vector-outline glyphs rather than real
    characters — see the module docstring) — it is NOT raised as an error.

    Raises ``PdfTextExtractError`` for any failure (binary missing, source
    missing/not a file, pdftotext non-zero exit, timeout).
    """
    if not src.exists():
        raise PdfTextExtractError(f"source does not exist: {src}")
    if not src.is_file():
        raise PdfTextExtractError(f"source is not a file: {src}")

    binary = _find_pdftotext()
    argv = [binary]
    if preserve_layout:
        argv.append("-layout")
    argv.extend([str(src), "-"])  # "-" = write to stdout

    logger.info("extract_text: %s (layout=%s)", src, preserve_layout)
    rc, stdout_b, stderr = _run_with_timeout(argv, timeout_s=timeout_s)
    if rc != 0:
        raise PdfTextExtractError(f"pdftotext exited {rc} on {src.name}", stderr=stderr)

    text = stdout_b.decode("utf-8", errors="replace")
    if len(text) > MAX_TEXT_CHARS:
        text = (
            text[:MAX_TEXT_CHARS]
            + f"\n\n[... truncated at {MAX_TEXT_CHARS} chars — unusually large "
            "for a technical drawing; this tool isn't meant for long documents]"
        )
    return text
