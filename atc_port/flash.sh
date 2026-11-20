#!/usr/bin/env bash
# Write the ported firmware to the tag (or put the stock firmware back).
#   ./flash.sh            write ATC_Paper_picksmart.bin, then verify every byte
#   ./flash.sh restore    write the stock backup back, then verify
#   ./flash.sh --port /dev/cu.usbserial-XXXX
set -euo pipefail
cd "$(dirname "$0")"
PY=../stock_backup/.venv/bin/python
[ -x "$PY" ] || { echo "Run ../stock_backup/setup.sh first."; exit 1; }
exec "$PY" flash_atc.py "$@"
