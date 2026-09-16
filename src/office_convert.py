"""Office-document conversion via the shared Collabora Online service.

``convert`` POSTs the source file to the office service's headless
``/cool/convert-to/<fmt>`` endpoint (multipart field ``data``) and writes the
bytes it answers with next to the source. One LibreOffice on the platform
renders every office document — the browser editor's saves and these
conversions alike — so this image carries no ``soffice`` of its own (the
~380 MB it used to fork; measured 2.6 s for a 4.8 MB workbook over the wire).

Round-trips PDF, DOCX, XLSX, PPTX, ODT, ODS, ODP, RTF, HTML, TXT and the
legacy DOC/XLS/PPT.

Importable contract:

    from pathlib import Path
    from src.office_convert import convert, OfficeConvertError

    pdf = convert(
        Path("/home/agent/rfp.docx"), Path("/home/agent"), to="pdf",
        convert_url="http://collabora:9980",
    )

``convert_url`` is the service base URL the caller resolved from its own
required config (the server reads ``OFFICE_CONVERT_URL`` at startup); this
module never reads the environment.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# A 50-page DOCX with embedded images renders in ~3 s on a warm service;
# 60 s leaves headroom for a worst-case 200-page tender doc without letting a
# stuck conversion wedge a chat indefinitely.
_DEFAULT_TIMEOUT_S = 60.0

# Supported output formats: the value is both the path segment convert-to
# takes and the extension of the file written. Keep this the single source
# of truth for what the tool advertises.
_FORMATS: dict[str, str] = {
    # Office Open XML (modern MS Office).
    "docx": "docx",
    "xlsx": "xlsx",
    "pptx": "pptx",
    # OpenDocument (LibreOffice native).
    "odt": "odt",
    "ods": "ods",
    "odp": "odp",
    # Portable / interchange.
    "pdf": "pdf",
    "rtf": "rtf",
    "html": "html",
    "txt": "txt",
    # Legacy MS Office (rare; inbound conversion of ancient tender
    # attachments).
    "doc": "doc",
    "xls": "xls",
    "ppt": "ppt",
}


SUPPORTED_FORMATS: tuple[str, ...] = tuple(_FORMATS.keys())

# How much of a rejected response to fold into the error the agent reads.
_BODY_SNIPPET = 300


class OfficeConvertError(RuntimeError):
    """Conversion failed.

    Raised for any reason the caller can't produce the requested output:
    unsupported format, missing source, the service unreachable, a non-2xx
    answer, an empty answer, timeout. The message is human-readable;
    ``stderr`` carries the service's own words when it gave any.
    """

    def __init__(self, msg: str, *, stderr: str | None = None) -> None:
        super().__init__(msg)
        self.stderr = stderr


def convert_endpoint(convert_url: str, fmt: str) -> str:
    """The convert-to URL for ``fmt`` on the service at ``convert_url``."""
    return f"{convert_url.rstrip('/')}/cool/convert-to/{fmt}"


def _post_document(src: Path, endpoint: str, *, timeout_s: float) -> bytes:
    """POST ``src`` to ``endpoint`` and return the converted bytes.

    Every transport outcome that is not "2xx with a body" becomes an
    ``OfficeConvertError`` naming the cause — unreachable, timed out, or the
    service's own rejection with its status and first words.
    """
    try:
        with src.open("rb") as fh, httpx.Client(timeout=timeout_s) as client:
            response = client.post(endpoint, files={"data": (src.name, fh)})
    except httpx.TimeoutException as exc:
        raise OfficeConvertError(
            f"office conversion service timed out after {timeout_s:.0f}s converting {src.name}"
        ) from exc
    except httpx.HTTPError as exc:
        raise OfficeConvertError(
            f"office conversion service unreachable at {endpoint}: {exc}"
        ) from exc
    if response.status_code < 200 or response.status_code >= 300:
        raise OfficeConvertError(
            f"office conversion service answered {response.status_code} converting {src.name}",
            stderr=response.text[:_BODY_SNIPPET],
        )
    return response.content


def convert(
    src: Path,
    dst_dir: Path,
    *,
    to: str = "pdf",
    convert_url: str,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> Path:
    """Convert an Office document to ``to`` format through the office service.

    ``src`` must exist and be a file the service can open. ``to`` is one of
    the keys in ``SUPPORTED_FORMATS`` (case-insensitive). ``dst_dir`` is
    created if missing. The output is written as ``<src.stem>.<ext>`` inside
    ``dst_dir`` and the path returned; an existing file there is overwritten.

    Raises ``OfficeConvertError`` for any failure mode (unsupported ``to``,
    source missing/unreadable, service unreachable, non-2xx answer, empty
    answer, timeout).
    """
    fmt_key = to.lower()
    if fmt_key not in _FORMATS:
        raise OfficeConvertError(
            f"unsupported output format {to!r}; supported: {', '.join(sorted(SUPPORTED_FORMATS))}"
        )
    ext = _FORMATS[fmt_key]

    if not src.exists():
        raise OfficeConvertError(f"source does not exist: {src}")
    if not src.is_file():
        raise OfficeConvertError(f"source is not a file: {src}")

    dst_dir.mkdir(parents=True, exist_ok=True)
    expected = dst_dir / f"{src.stem}.{ext}"
    endpoint = convert_endpoint(convert_url, fmt_key)

    logger.info("office_convert: %s -> %s (to=%s)", src, expected, fmt_key)
    body = _post_document(src, endpoint, timeout_s=timeout_s)
    # The service answers 200 with an empty body for a document it opened
    # but could not render (password-protected, corrupt) — verify the bytes
    # landed before declaring success.
    if not body:
        raise OfficeConvertError(
            f"office conversion service produced no output for {src.name} -> {fmt_key} "
            f"(likely corrupt, password-protected, or wrong source format)"
        )
    expected.write_bytes(body)
    return expected
