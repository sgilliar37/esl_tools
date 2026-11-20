#!/usr/bin/env bash
# Update the tag over Bluetooth. No wires, no catching: the tag just needs power.
#   ./ota.sh            send the ported firmware (ATC_Paper_picksmart.bin)
#   ./ota.sh restore    put the original PickSmart firmware back
#   ./ota.sh --scan     list the tags in range
#   ./ota.sh test       run the offline tests (no Bluetooth needed)
set -euo pipefail
cd "$(dirname "$0")"
PY=../stock_backup/.venv/bin/python
[ -x "$PY" ] || { echo "Run ../stock_backup/setup.sh first."; exit 1; }
if [ "${1:-}" = "test" ]; then
  exec "$PY" tests/test_ota.py
fi
if ! "$PY" -c "import bleak" 2>/dev/null; then
  echo "Installing the Bluetooth library (one time, about a minute)..."
  "$PY" -m pip install --quiet "bleak==3.1.0"
fi
exec "$PY" ota_update.py "$@"
