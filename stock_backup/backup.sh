#!/usr/bin/env bash
# Read-only backup of the stock firmware. Extra options go straight through, e.g.
#   ./backup.sh --port /dev/cu.usbserial-0001
#   ./backup.sh -t 15000        (longer window to plug 3V3 back in after "Activate")
#   ./backup.sh -b 460800       (slower link if reads keep mismatching)
set -euo pipefail
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { echo "Run ./setup.sh first."; exit 1; }
exec ./.venv/bin/python backup_stock_firmware.py backup "$@"
