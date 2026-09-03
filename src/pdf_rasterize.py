"""Rasterize a PDF's pages to PNG images via ``pdftoppm`` (poppler-utils).

Neither existing PDF-adjacent tool in this sidecar family produces an image
from a PDF: ``office_convert.py`` round-trips Office/PDF documents but never
rasterizes, and ``ocr_convert.py`` (the ``ocr`` sidecar) OCRs an *existing*
image/PDF, it doesn't create one. This module fills that gap so the agent can
*see* a PDF page — the first step of AI-vision PDF-to-CAD reconstruction (see
``apps/dispatcher/chart/files/skills/pdf-to-cad-reconstruction.md``).

``pdftoppm`` is free (GPL, part of poppler-utils, already the standard tool
most Linux PDF-to-image pipelines use) — no new licensing cost, no new Python
dependency. Same subprocess shape as ``office_convert``/``ocr_convert``: a
resolved binary, a hard timeout with process-group SIGKILL, and exit-0-but-
no-output verification.

Importable contract:

    from pathlib import Path
    from src.pdf_rasterize import pdf_to_images, PdfRasterizeError

    pages = pdf_to_images(Path("/home/agent/floorplan.pdf"), Path("/home/agent/job"))
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_PDFTOPPM_CANDIDATES = ("pdftoppm",)

# A dense multi-page tender PDF at moderate DPI still finishes in a few
# seconds per page; 120s covers a worst-case large plan set without letting a
# stuck conversion wedge a chat indefinitely (shorter than office_convert's
# 60s per-call budget would be tight here since this can render many pages
# in one call, not one document in one shot).
_DEFAULT_TIMEOUT_S = 120.0

# Sane bounds on requested resolution. Below 50 the raster is too coarse to
# read a floor plan's text/dimensions; above 600 a single page can balloon to
# tens of megapixels — a caller mistake (or an agent guessing a huge number
# "for quality"), not a real use case, and the fix belongs at the input, not
# a bigger container (same reasoning as the cad sidecar's byte/entity caps —
# see docs/incidents/2026-08-04-pdf-to-dxf-oom-and-viewer-freeze.md).
MIN_DPI = 50
MAX_DPI = 600


class PdfRasterizeError(RuntimeError):
    """Rasterization failed: missing binary, bad source, out-of-range DPI,
    pdftoppm non-zero exit, no pages produced, or timeout. ``stderr`` carries
    pdftoppm's last words when relevant."""

    def __init__(self, msg: str, *, stderr: str | None = None) -> None:
        super().__init__(msg)
        self.stderr = stderr


def _find_pdftoppm() -> str:
    for name in _PDFTOPPM_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    raise PdfRasterizeError(
        "pdftoppm not on PATH — install poppler-utils or run inside the workspace-tool-office image"
    )


def _run_with_timeout(argv: list[str], *, timeout_s: float) -> tuple[int, str]:
    """Run ``argv`` with a hard timeout; SIGKILL the whole process group on
    timeout. Returns ``(exit_code, stderr_text)``."""
    started = time.monotonic()
    # argv is built here from a resolved binary + static flags + caller Paths;
    # shell=False so no interpolation escape.
    proc = subprocess.Popen(  # noqa: S603 - argv source is trusted, see comment above
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        _, stderr_b = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        try:
            _, stderr_b = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stderr_b = b""
        elapsed = time.monotonic() - started
        raise PdfRasterizeError(
            f"pdftoppm timed out after {elapsed:.1f}s (limit {timeout_s:.1f}s)",
            stderr=stderr_b.decode("utf-8", errors="replace"),
        ) from None
    return proc.returncode, stderr_b.decode("utf-8", errors="replace")


def _page_number(path: Path, *, prefix: str) -> int:
    """Extract the trailing page number pdftoppm embeds in each output
    filename (``<prefix>-<n>.png``, zero-padded). Used to sort pages in
    document order regardless of padding width."""
    m = re.match(rf"^{re.escape(prefix)}-(\d+)\.png$", path.name)
    return int(m.group(1)) if m else 0


def pdf_to_images(
    src: Path,
    dst_dir: Path,
    *,
    dpi: int = 150,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> list[Path]:
    """Rasterize every page of a PDF to one PNG per page.

    ``src`` must exist and be a file pdftoppm can open. ``dst_dir`` is
    created if missing; output files are named ``<src.stem>-<n>.png``
    (pdftoppm's own numbering) inside it. Returns the page images in
    document order.

    Raises ``PdfRasterizeError`` for any failure (binary missing, source
    missing/not a file, ``dpi`` outside [50, 600], pdftoppm non-zero exit,
    no pages produced, timeout).
    """
    if not MIN_DPI <= dpi <= MAX_DPI:
        raise PdfRasterizeError(f"dpi {dpi} outside the supported range {MIN_DPI}-{MAX_DPI}")
    if not src.exists():
        raise PdfRasterizeError(f"source does not exist: {src}")
    if not src.is_file():
        raise PdfRasterizeError(f"source is not a file: {src}")

    binary = _find_pdftoppm()
    dst_dir.mkdir(parents=True, exist_ok=True)
    prefix = src.stem
    out_prefix = dst_dir / prefix

    argv = [binary, "-png", "-r", str(dpi), str(src), str(out_prefix)]
    logger.info("pdf_to_images: %s -> %s-*.png (dpi=%d)", src, out_prefix, dpi)
    rc, stderr = _run_with_timeout(argv, timeout_s=timeout_s)
    if rc != 0:
        raise PdfRasterizeError(f"pdftoppm exited {rc} on {src.name}", stderr=stderr)

    pages = sorted(dst_dir.glob(f"{prefix}-*.png"), key=lambda p: _page_number(p, prefix=prefix))
    if not pages:
        raise PdfRasterizeError(
            f"pdftoppm produced no pages for {src.name} (corrupt, encrypted, or not a PDF?)",
            stderr=stderr,
        )
    return pages
