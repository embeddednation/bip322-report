#!/usr/bin/env bash
# One-shot setup from a fresh clone: virtualenv, hash-pinned third-party
# dependencies, bip322-core and bip322-audit at their pinned tags, editable
# install of bip322-report, tests.
#
#   ./setup.sh                                          # core and audit from git at CORE_REF / AUDIT_REF
#   ./setup.sh --core ../bip322-core --audit ../bip322-audit   # local checkouts (editable): development
#   ./setup.sh --pdf                                    # also WeasyPrint (hash-pinned, requirements-pdf.lock) for report.pdf
#   ./setup.sh --no-tests
#
# --pdf needs WeasyPrint's system libraries first (Debian/Ubuntu):
#   sudo apt install libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b
#
# Needs Python >= 3.10, git, and curl (only when python3 has no ensurepip).
set -euo pipefail
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
CORE_REF=${CORE_REF:-v0.11.1}
AUDIT_REF=${AUDIT_REF:-v0.12.1}
CORE_URL=${CORE_URL:-git+https://github.com/embeddednation/bip322-core.git}
AUDIT_URL=${AUDIT_URL:-git+https://github.com/embeddednation/bip322-audit.git}
CORE_PATH=""
AUDIT_PATH=""
RUN_TESTS=1
WITH_PDF=0
while [ $# -gt 0 ]; do
  case $1 in
    --core) CORE_PATH=$2; shift ;;
    --audit) AUDIT_PATH=$2; shift ;;
    --pdf) WITH_PDF=1 ;;
    --no-tests) RUN_TESTS=0 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || { echo "error: Python >= 3.10 required (found $("$PY" --version))" >&2; exit 1; }

# --- virtualenv (Debian/Ubuntu often ship python3 without ensurepip) ---------
if [ ! -x .venv/bin/python ]; then
  if "$PY" -c 'import ensurepip' 2>/dev/null; then
    "$PY" -m venv .venv
  else
    echo "python3 has no ensurepip; bootstrapping pip from bootstrap.pypa.io"
    "$PY" -m venv --without-pip .venv
    curl -sSfL https://bootstrap.pypa.io/get-pip.py | .venv/bin/python - -q
  fi
fi
PIP=.venv/bin/pip

# --- third-party dependencies, exact versions with sha256 hashes -------------
if .venv/bin/python -c 'import platform, sys; sys.exit(0 if sys.version_info[:2] == (3, 12) and platform.system() == "Linux" and platform.machine() == "x86_64" else 1)'; then
  "$PIP" install -q --require-hashes -r requirements.lock
else
  echo "note: the py-bitcoinkernel wheel in requirements.lock is for CPython 3.12 / Linux x86_64;"
  echo "      installing without the kernel engine (btclib remains; see 'bip322 engines')"
  awk '/^py-bitcoinkernel/ {skip = 2} skip > 0 {skip--; next} {print}' requirements.lock > .venv/requirements.nokernel.lock
  "$PIP" install -q --require-hashes -r .venv/requirements.nokernel.lock
fi

# --- bip322-core and bip322-audit: pinned tags from git, or local checkouts --
if [ -n "$CORE_PATH" ]; then "$PIP" install -q --no-deps -e "$CORE_PATH"; else "$PIP" install -q --no-deps "bip322-core @ $CORE_URL@$CORE_REF"; fi
if [ -n "$AUDIT_PATH" ]; then "$PIP" install -q --no-deps -e "$AUDIT_PATH"; else "$PIP" install -q --no-deps "bip322-audit @ $AUDIT_URL@$AUDIT_REF"; fi
"$PIP" install -q --no-deps -e .
"$PIP" install -q "pytest==9.1.1" "ruff==0.16.7"   # pinned: CI and local lint must agree

# --- optional: the PDF renderer -------------------------------------------
if [ "$WITH_PDF" = 1 ]; then
  "$PIP" install -q --require-hashes -r requirements-pdf.lock
  .venv/bin/python -c 'import weasyprint' || { echo "error: WeasyPrint cannot load its system libraries; see the top of this script" >&2; exit 1; }
fi

# --- tests (the regtest test skips without a Bitcoin Core binary) ------------
if [ "$RUN_TESTS" = 1 ]; then
  .venv/bin/python -m pytest -q
fi

echo
.venv/bin/bip322 --version
.venv/bin/python -c "import bip322audit, bip322report; print(bip322audit.TOOL); print(bip322report.TOOL)"
echo
echo "ready. put the commands on your PATH with:"
echo "  export PATH=\"$PWD/.venv/bin:\$PATH\""
echo "then: bip322-report help, examples/report_walkthrough.sh (needs a Bitcoin Core binary)"
