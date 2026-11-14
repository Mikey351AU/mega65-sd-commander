#!/bin/sh
# macOS: double-click to start MEGA65 SD Commander.
# Prefers the python.org Python (its tkinter is newer than the one Apple ships).
cd "$(dirname "$0")" || exit 1
for py in /Library/Frameworks/Python.framework/Versions/Current/bin/python3 /usr/local/bin/python3 python3; do
  if command -v "$py" >/dev/null 2>&1; then
    exec "$py" m65_sd_commander.py
  fi
done
echo "Python 3 was not found. Install it from https://www.python.org/downloads/"
echo "then run this again. See INSTALL.md for details."
read -r _
