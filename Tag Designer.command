#!/usr/bin/env bash
# Double-click to start Tag Designer. It opens in your web browser.
# Keep this Terminal window open while you use it; close it to stop.
#   Demo with simulated tags (no Bluetooth):  ./"Tag Designer.command" --demo
cd "$(dirname "$0")/tag_designer" 2>/dev/null || cd "$(dirname "$0")"
PY=../stock_backup/.venv/bin/python
if [ ! -x "$PY" ]; then
  echo "First-time setup (about a minute)..."
  (cd ../stock_backup && bash ./setup.sh) || { echo "Setup failed. See the messages above."; read -r -p "Press Enter to close."; exit 1; }
fi
if ! "$PY" -c "import bleak" 2>/dev/null; then
  echo "Installing the Bluetooth library (one time, about a minute)..."
  "$PY" -m pip install --quiet "bleak==3.1.0"
fi
exec "$PY" server.py "$@"
