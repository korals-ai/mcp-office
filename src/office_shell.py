"""Run an arbitrary shell command inside the office tool pod — the escape
hatch for whatever the curated tools (``convert``, ``pdf_extract_text``,
``xlsx_extract_cells``, the ``author_*`` tools) don't cover.

Deliberately shell-shaped rather than a bespoke Python-eval API: Claude is
heavily trained on Bash idioms, so a tool that mirrors that (a command
string in, stdout/stderr/exit-code back) minimizes the "unfamiliar surface
→ more turns/worse code" risk a bespoke API would carry. Runs inside this
pod's existing container — already has LibreOffice/poppler-utils/openpyxl/
python-docx/python-pptx on its PATH — as the same non-root user, on the same
PVC mount, under the same resource limits as every other tool in this
server. No new privilege, no new trust boundary: a workspace agent already
runs arbitrary shell commands against its own tenant's files elsewhere in
this system.

Not a replacement for the curated tools above, nor for whatever read path
already exists for common formats — this exists only for operations those
don't cover. Calls here are meant to be rare and monitored, so usage stays a
visible trigger to build a proper tool, not a silent second path.

Importable contract:

    from src.office_shell import run_shell, OfficeShellError

    result = run_shell("pdftotext -layout /home/agent/rfp.pdf -")
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Mirrors office_convert.py / pdf_text.py: generous enough for a real
# LibreOffice-class operation, bounded so a stuck command can't wedge a
# turn indefinitely.
_DEFAULT_TIMEOUT_S = 60.0

# Same rationale as pdf_text.MAX_TEXT_CHARS: this feeds an MCP tool_result
# the agent reads directly, and an oversized tool_result can overrun the
# session's message buffer. Truncate rather than let one command's output
# blow past that limit.
MAX_OUTPUT_CHARS = 200_000


class OfficeShellError(RuntimeError):
    """The command could not be run at all — empty command or cwd missing.
    A non-zero exit or a timeout is NOT this: those come back as a normal
    ``ShellResult`` so the agent can see stdout/stderr/exit code and react,
    the same way it already reacts to a failed Bash call."""


@dataclass(frozen=True)
class ShellResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n\n[... truncated at {MAX_OUTPUT_CHARS} chars]"


def run_shell(
    cmd: str,
    *,
    cwd: Path | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> ShellResult:
    """Run ``cmd`` via ``bash -c`` inside this pod and capture the result.

    ``cwd`` defaults to the shared workspace volume root; pass a specific
    tenant path when the command needs to run alongside a particular file.
    A non-zero exit and a timeout both come back as a ``ShellResult`` (with
    ``timed_out=True`` in the latter case) rather than raising — the agent
    gets to see exactly what a Bash tool call would show it and decide what
    to do next. ``OfficeShellError`` is reserved for cases the command never
    even started (empty command, missing ``cwd``).
    """
    if not cmd.strip():
        raise OfficeShellError("empty command")
    work_dir = cwd or Path(os.environ.get("HOME", "/home/agent"))
    if not work_dir.is_dir():
        raise OfficeShellError(f"cwd does not exist: {work_dir}")

    argv = ["bash", "-c", cmd]
    logger.info("office_shell: %s (cwd=%s)", cmd, work_dir)
    started = time.monotonic()
    proc = subprocess.Popen(  # noqa: S603 - argv is a literal bash -c invocation; cmd is the agent's own tool input, same trust boundary as the workspace pod's Bash tool
        argv,
        cwd=work_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout_b, stderr_b = proc.communicate(timeout=timeout_s)
        timed_out = False
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        try:
            stdout_b, stderr_b = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stdout_b, stderr_b = b"", b""
        elapsed = time.monotonic() - started
        stderr_b += (
            f"\n[office_shell: killed after timeout ({elapsed:.1f}s, limit {timeout_s:.1f}s)]"
        ).encode()
        timed_out = True
        exit_code = -1

    return ShellResult(
        exit_code=exit_code,
        stdout=_truncate(stdout_b.decode("utf-8", errors="replace")),
        stderr=_truncate(stderr_b.decode("utf-8", errors="replace")),
        timed_out=timed_out,
    )
