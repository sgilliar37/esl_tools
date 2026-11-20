#!/usr/bin/env bash
# Take a lost password off a tag over the USB-UART adapter (SWire), the same
# wiring as ./flash.sh. Erases only the password sectors (0x7A000, 0x7B000):
# firmware, pictures and settings stay as they are.
#   ./unlock_usb.sh
#   ./unlock_usb.sh --port /dev/cu.usbserial-XXXX
set -euo pipefail
cd "$(dirname "$0")"
PY=../stock_backup/.venv/bin/python
[ -x "$PY" ] || { echo "Run ../stock_backup/setup.sh first."; exit 1; }
exec "$PY" flash_atc.py unlock "$@"
