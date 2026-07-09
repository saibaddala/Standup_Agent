#!/usr/bin/env sh
# Create scripts/.venv on fresh cloud VMs (no python3-venv package required).
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$ROOT/scripts/.venv"

if [ -x "$VENV/bin/python3" ]; then
  echo "venv already exists: $VENV"
else
  if python3 -m venv "$VENV" 2>/dev/null; then
    echo "created venv with python3 -m venv"
  else
    echo "python3-venv unavailable — installing virtualenv via pip"
    python3 -m pip install --user virtualenv
    python3 -m virtualenv "$VENV"
  fi
fi

"$VENV/bin/pip" install -r "$ROOT/scripts/requirements.txt"
echo "ready: $VENV/bin/python3"
