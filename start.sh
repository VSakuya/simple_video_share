#!/usr/bin/env bash
# start.sh — launch the Simple Video Share Flask app.
#
# Usage: ./start.sh
#   - Creates .venv and installs requirements if missing.
#   - Seeds config.json / secret key on first run (handled by config.py).
#   - Initialises the SQLite DB (handled by db.init_db() via create_app()).
#   - Runs the app on the host/port from config.json.

set -euo pipefail

# Resolve the project root (directory containing this script), so it works
# regardless of the caller's current working directory.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="$SCRIPT_DIR/.venv"
PYTHON="${PYTHON:-python3}"

# 1. Create a virtual environment if it does not exist yet.
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment at $VENV_DIR ..."
    "$PYTHON" -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# 2. Ensure dependencies are installed (pip is idempotent, so this is a
#    no-op once the environment is already set up).
if ! "$VENV_DIR/bin/pip" install -q -r requirements.txt; then
    echo "ERROR: failed to install requirements." >&2
    exit 1
fi

# 3. Launch the app (host/port/secret come from config.json, auto-seeded).
echo "Starting Simple Video Share ..."
exec "$VENV_DIR/bin/python" main.py
