"""Tests for the shell escape-hatch logic.

Real subprocesses via ``bash -c`` — no soffice/poppler needed, this exercises
the command/timeout/truncation plumbing itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.office_shell import MAX_OUTPUT_CHARS, OfficeShellError, run_shell


def test_empty_command_raises() -> None:
    with pytest.raises(OfficeShellError, match="empty command"):
        run_shell("   ")


def test_missing_cwd_raises(tmp_path: Path) -> None:
    with pytest.raises(OfficeShellError, match="cwd does not exist"):
        run_shell("echo hi", cwd=tmp_path / "nope")


def test_successful_command_returns_stdout(tmp_path: Path) -> None:
    result = run_shell("echo hello", cwd=tmp_path)
    assert result.exit_code == 0
    assert result.stdout.strip() == "hello"
    assert result.stderr == ""
    assert result.timed_out is False


def test_nonzero_exit_is_a_normal_result_not_an_error(tmp_path: Path) -> None:
    result = run_shell("echo oops >&2; exit 3", cwd=tmp_path)
    assert result.exit_code == 3
    assert "oops" in result.stderr
    assert result.timed_out is False


def test_runs_in_the_given_cwd(tmp_path: Path) -> None:
    (tmp_path / "marker.txt").write_text("here")
    result = run_shell("ls", cwd=tmp_path)
    assert "marker.txt" in result.stdout


def test_timeout_kills_the_process_and_reports_it(tmp_path: Path) -> None:
    result = run_shell("sleep 5", cwd=tmp_path, timeout_s=0.2)
    assert result.timed_out is True
    assert result.exit_code == -1
    assert "timeout" in result.stderr.lower() or "killed" in result.stderr.lower()


def test_large_output_is_truncated(tmp_path: Path) -> None:
    result = run_shell(
        f"python3 -c \"print('x' * {MAX_OUTPUT_CHARS + 1000})\"",
        cwd=tmp_path,
    )
    assert len(result.stdout) <= MAX_OUTPUT_CHARS + 200
    assert "truncated" in result.stdout
