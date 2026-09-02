"""Office-document conversion via headless LibreOffice.

Ported from the workspace image's ``office_convert`` (apps/workspace) into
the workspace-tool-office sidecar. This is where the real ``soffice`` fork now
runs: the workspace image will shed LibreOffice (phase 3c) and reach this
sidecar over MCP instead. The conversion semantics are identical — only the
location moved.

Round-trips tender artifacts between PDF, DOCX, XLSX, PPTX, ODT, ODS, ODP,
RTF, HTML, and TXT. ``convert`` owns the fiddly ``soffice`` flag set, the
per-call profile dir under ``/tmp`` (so soffice cache junk never lands on the
tenant volume), and the exit-0-but-no-output verification.

Importable contract:

    from pathlib import Path
    from src.office_convert import convert, OfficeConvertError

    pdf = convert(Path("/home/agent/rfp.docx"), Path("/home/agent"), to="pdf")
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# soffice ships under different binary names on different distros. On
# bookworm-slim with libreoffice-* installed, ``soffice`` is on PATH;
# fall back to ``libreoffice`` so this module works on hosts where the
# packaging differs (and so a developer's macOS install with
# ``brew install --cask libreoffice`` still works for ad-hoc runs).
_SOFFICE_CANDIDATES = ("soffice", "libreoffice")

# Conservative default. A 50-page DOCX with embedded images takes ~3s
# on a warm soffice; cold-start of the JVM/uno bridge can add ~5s.
# 60s leaves headroom for a worst-case 200-page tender doc without
# letting a stuck conversion wedge a chat indefinitely.
_DEFAULT_TIMEOUT_S = 60.0

# Supported output formats. The key is what the caller passes; the value
# is the ``--convert-to`` filter string soffice expects and the file
# extension it actually writes (often the same as the key but not always
# — keep this table as the single source of truth).
_FORMATS: dict[str, tuple[str, str]] = {
    # Office Open XML (modern MS Office).
    "docx": ("docx:MS Word 2007 XML", "docx"),
    "xlsx": ("xlsx:Calc Office Open XML", "xlsx"),
    "pptx": ("pptx:Impress Office Open XML", "pptx"),
    # OpenDocument (LibreOffice native).
    "odt": ("odt", "odt"),
    "ods": ("ods", "ods"),
    "odp": ("odp", "odp"),
    # Portable / interchange.
    "pdf": ("pdf", "pdf"),
    "rtf": ("rtf", "rtf"),
    "html": ("html", "html"),
    "txt": ("txt:Text", "txt"),
    # Legacy MS Office (rare; included for inbound conversion of
    # ancient tender attachments).
    "doc": ("doc", "doc"),
    "xls": ("xls", "xls"),
    "ppt": ("ppt", "ppt"),
}


SUPPORTED_FORMATS: tuple[str, ...] = tuple(_FORMATS.keys())


class OfficeConvertError(RuntimeError):
    """Conversion failed.

    Raised for any reason the caller can't produce the requested
    output: missing binary, unsupported format, soffice non-zero
    exit, soffice exit-0 with no output, timeout. The message is
    human-readable; ``stderr`` carries soffice's last words when
    relevant.
    """

    def __init__(self, msg: str, *, stderr: str | None = None) -> None:
        super().__init__(msg)
        self.stderr = stderr


def _find_soffice() -> str:
    for name in _SOFFICE_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    raise OfficeConvertError(
        "soffice/libreoffice not on PATH — install libreoffice-{writer,"
        "calc,impress} or run inside the workspace-tool-office image"
    )


def _run_with_timeout(
    argv: list[str],
    *,
    timeout_s: float,
) -> tuple[int, str]:
    """Run ``argv`` with a hard timeout.

    Returns ``(exit_code, stderr_text)``. On timeout the process group
    is SIGKILLed and ``OfficeConvertError`` is raised — soffice spawns
    a uno helper, and a plain ``proc.kill()`` can leave the helper
    behind. ``start_new_session=True`` makes the soffice process its
    own group leader so we can kill the whole tree.
    """
    started = time.monotonic()
    # argv is built inside this module from a resolved soffice path +
    # static flags + a Path the caller controls. shell=False (default)
    # so no string-interpolation escape; no untrusted-shell risk.
    proc = subprocess.Popen(  # noqa: S603 - argv source is trusted, see comment above
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        _, stderr_b = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        # Kill the whole process group, then drain so we don't leak
        # the file descriptors. ``communicate()`` after kill is
        # bounded — the OS has already torn the procs down.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        try:
            _, stderr_b = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stderr_b = b""
        elapsed = time.monotonic() - started
        raise OfficeConvertError(
            f"soffice timed out after {elapsed:.1f}s (limit {timeout_s:.1f}s)",
            stderr=stderr_b.decode("utf-8", errors="replace"),
        ) from None
    return proc.returncode, stderr_b.decode("utf-8", errors="replace")


def convert(
    src: Path,
    dst_dir: Path,
    *,
    to: str = "pdf",
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> Path:
    """Convert an Office document to ``to`` format via headless soffice.

    ``src`` must exist and be a file LibreOffice can open. ``to`` is one
    of the keys in ``SUPPORTED_FORMATS`` (case-insensitive). ``dst_dir``
    is created if missing. The output is written as ``<src.stem>.<ext>``
    inside ``dst_dir`` and the path returned. Existing files at that path
    are overwritten by soffice.

    Raises ``OfficeConvertError`` for any failure mode (binary missing,
    unsupported ``to``, source missing/unreadable, soffice non-zero exit,
    soffice exit-0 with no output file, timeout).
    """
    fmt_key = to.lower()
    if fmt_key not in _FORMATS:
        raise OfficeConvertError(
            f"unsupported output format {to!r}; supported: {', '.join(sorted(SUPPORTED_FORMATS))}"
        )
    filter_str, ext = _FORMATS[fmt_key]

    if not src.exists():
        raise OfficeConvertError(f"source does not exist: {src}")
    if not src.is_file():
        raise OfficeConvertError(f"source is not a file: {src}")

    soffice = _find_soffice()
    dst_dir.mkdir(parents=True, exist_ok=True)
    expected = dst_dir / f"{src.stem}.{ext}"

    # Per-call user-profile dir under /tmp. Two reasons:
    #   * ``$HOME`` is the tenant PVC; soffice must not pollute it.
    #   * Two concurrent ``soffice`` invocations sharing one profile
    #     fight over the lockfile and one fails with "another instance
    #     is using this user profile". A per-call profile sidesteps
    #     that without forcing the caller to serialise.
    profile_dir = Path(tempfile.mkdtemp(prefix="lo-profile-"))
    try:
        argv = [
            soffice,
            "--headless",
            "--norestore",
            "--nologo",
            "--nofirststartwizard",
            f"-env:UserInstallation=file://{profile_dir}",
            "--convert-to",
            filter_str,
            "--outdir",
            str(dst_dir),
            str(src),
        ]
        logger.info("office_convert: %s -> %s (to=%s)", src, expected, fmt_key)
        rc, stderr = _run_with_timeout(argv, timeout_s=timeout_s)
        if rc != 0:
            raise OfficeConvertError(
                f"soffice exited {rc} converting {src.name} to {fmt_key}",
                stderr=stderr,
            )
        # soffice sometimes exits 0 without producing output — corrupt
        # source, password-protected doc, unsupported macro. Verify
        # the file actually landed before declaring success.
        if not expected.exists():
            raise OfficeConvertError(
                f"soffice produced no output for {src.name} -> {fmt_key} "
                f"(likely corrupt, password-protected, or wrong source format)",
                stderr=stderr,
            )
        return expected
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)
