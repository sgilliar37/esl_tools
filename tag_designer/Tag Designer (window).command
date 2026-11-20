#!/bin/bash
# Started by Tag Designer.app. Runs the designer in its own window. Terminal stays
# open next to it because macOS grants Bluetooth to Terminal, not to the app itself.
# Closing the designer window ends it.
cd "$(dirname "$0")" || exit 1
printf '\033]0;Tag Designer (keep open while the app runs)\007'
echo "Tag Designer is running in its own window. This Terminal window must stay open; it closes the app if you close it."
exec ../stock_backup/.venv/bin/python server.py --window
