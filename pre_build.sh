#!/usr/bin/env bash
# Pre-build gate for the workspace-tool-office sidecar image. Mirrors the
# workspace gate: ruff format + ruff check + mypy + pip-audit + pytest.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log()  { echo "$(date '+%Y-%m-%d %H:%M:%S') [workspace-tool-office] $*"; }
fail() { log "FAIL: $*"; exit 1; }

log "Running pre-build checks..."

cd "$SCRIPT_DIR"

# src/server.py reads WORKSPACE_TOOL_PORT with NO default (the operator injects
# it in the tool pod), and tests/test_server.py imports that module — so the
# runner supplies it explicitly here. Never a conftest setdefault.
export WORKSPACE_TOOL_PORT=8090

VENV="$SCRIPT_DIR/.venv"

if [ ! -x "$VENV/bin/ruff" ]; then
  log "Bootstrapping .venv..."
  uv venv "$VENV" --python python3.12 >&2
  uv pip install --python "$VENV/bin/python" --index-url https://pypi.org/simple/ -e '.[dev]' >&2
fi

pick() {
  if [ -x "$VENV/bin/$1" ]; then
    echo "$VENV/bin/$1"
  elif command -v "$1" >/dev/null 2>&1; then
    command -v "$1"
  fi
}

RUFF="$(pick ruff)"
MYPY="$(pick mypy)"
PYTEST="$(pick pytest)"
PIP_AUDIT="$(pick pip-audit)"

HINT="  Install: $VENV/bin/pip install --index-url https://pypi.org/simple/ -e '.[dev]'"
[ -n "$RUFF" ]      || fail "ruff not found. $HINT"
[ -n "$MYPY" ]      || fail "mypy not found. $HINT"
[ -n "$PYTEST" ]    || fail "pytest not found. $HINT"
[ -n "$PIP_AUDIT" ] || fail "pip-audit not found. $HINT"

# The shared `toollog` package lives one level up; the image COPYs it next to
# src and imports it as `toollog`. Put its parent on the import path so the
# import resolves for mypy and pytest exactly as it does in the image (/app on
# sys.path under `python -m`), and gate the package itself — it has no manifest,
# so nothing else would.
TL_PARENT="$(cd "$SCRIPT_DIR/.." && pwd)"
export PYTHONPATH="$TL_PARENT${PYTHONPATH:+:$PYTHONPATH}"
export MYPYPATH="$TL_PARENT${MYPYPATH:+:$MYPYPATH}"

log "1/5 Format check (ruff format)..."
"$RUFF" format --check src tests || fail "ruff format (run: ruff format src tests)"
log "  ✓ ruff format passed"

log "2/5 Linting (ruff)..."
"$RUFF" check src tests || fail "ruff"
log "  ✓ ruff passed"

log "3/5 Static typing (mypy)..."
"$MYPY" src || fail "mypy"
log "  ✓ mypy passed"

log "4/5 Dependency CVE scan (pip-audit)..."
PIP_INDEX_URL=https://pypi.org/simple/ "$PIP_AUDIT" --no-deps -r requirements.txt || fail "pip-audit"
log "  ✓ pip-audit passed"

log "5/5 Running unit tests (with coverage)..."
if "$PYTEST" --help 2>&1 | grep "coverage reporting" > /dev/null; then
  "$PYTEST" -q --cov=src --cov-report=term --cov-fail-under=0 || fail "pytest"
else
  log "  (pytest-cov not installed; running without coverage)"
  "$PYTEST" -q || fail "pytest"
fi
log "  ✓ pytest passed"

log "Gating the shared toollog package..."
bash "$TL_PARENT/toollog/check.sh" "$RUFF" "$MYPY" "$PYTEST" || fail "toollog"
log "  ✓ toollog passed"
log "Gating the shared loopwatch package..."
bash "$TL_PARENT/loopwatch/check.sh" "$RUFF" "$MYPY" "$PYTEST" || fail "loopwatch"
log "  ✓ loopwatch passed"

log "Pre-build checks complete ✓"
