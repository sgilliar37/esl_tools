#!/usr/bin/env bash
# One-time setup: Python venv with pyserial, plus a self-test (no hardware needed).
set -euo pipefail
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
if ! command -v "$PY" >/dev/null; then
  echo "python3 not found. Install it with: xcode-select --install   (or brew install python)"; exit 1
fi
echo "Using $($PY --version) at $(command -v $PY)"

if [ ! -d .venv ]; then
  "$PY" -m venv .venv
fi
./.venv/bin/python -m pip install --quiet --upgrade pip
./.venv/bin/python -m pip install --quiet "pyserial>=3.5"

test -f tools/TlsrComSwireWriter/TLSR825xComFlasher-macos.py || { echo "Missing vendored flasher in tools/"; exit 1; }
echo "Flasher pinned at commit $(cat tools/TlsrComSwireWriter/PINNED_COMMIT)"

echo "Running self-test (simulated flash, no adapter needed)..."
./.venv/bin/python -m unittest discover -s tests
echo
echo "Setup OK. When the adapter is wired up, run:  ./backup.sh"
