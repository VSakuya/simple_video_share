#!/usr/bin/env bash
# start.sh — launch the Simple Video Share Flask app.
#
# Usage: ./start.sh
#   - Pulls the latest code (git pull --ff-only); aborts on failure so a
#     half-updated tree is never started.
#   - Creates .venv and installs requirements if missing.
#   - Seeds config.json / secret key on first run (handled by config.py).
#   - Initialises the SQLite DB (handled by db.init_db() via create_app()).
#   - Runs the app under Waitress (a production WSGI server) on the
#     host/port from config.json.

set -euo pipefail

# Resolve the project root (directory containing this script), so it works
# regardless of the caller's current working directory.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="$SCRIPT_DIR/.venv"
PYTHON="${PYTHON:-python3}"

# 1. Pull the latest code so ./start.sh is a self-updating deploy: the VPS
#    always runs what is on the remote. --ff-only fast-forwards without merge
#    commits and aborts (see set -e) if the tree has uncommitted edits to
#    tracked files or the branch has diverged — a half-updated app never starts.
#    config.json and documents/PROJECT_PROGRESS.md are git-ignored, so the pull
#    never touches the VPS's live secret_key / drive_folder_id.
echo "Pulling latest code (git pull --ff-only) ..."
if ! git pull --ff-only; then
    echo "ERROR: git pull failed; aborting so a half-updated tree never starts." >&2
    echo "  - Uncommitted local edits? Commit or discard them, then retry." >&2
    echo "  - GitHub unreachable? Check network / credentials." >&2
    exit 1
fi

# 2. Create a virtual environment if it does not exist yet.
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment at $VENV_DIR ..."
    "$PYTHON" -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# 3. Ensure dependencies are installed (pip is idempotent, so this is a
#    no-op once the environment is already set up).
if ! "$VENV_DIR/bin/pip" install -q -r requirements.txt; then
    echo "ERROR: failed to install requirements." >&2
    exit 1
fi

# 4. Launch the app under Waitress, a production WSGI server (host/port come
#    from config.json, auto-seeded). Waitress is single-process, multi-threaded:
#    every request — including the long-lived SSE streams — gets its own thread,
#    so the in-memory room state in app/presence.py stays in one process while
#    many connections are served concurrently. (A multi-process server such as
#    `gunicorn -w N` would split that state across workers and break presence.)
#    THREADS (env, default 50) must cover the expected number of simultaneous
#    SSE connections plus a burst of normal requests.
HOST="$("$VENV_DIR/bin/python" -c "import json; print(json.load(open('config.json')).get('host', '127.0.0.1'))")"
PORT="$("$VENV_DIR/bin/python" -c "import json; print(json.load(open('config.json')).get('port', 8080))")"
THREADS="${THREADS:-50}"
echo "Starting Simple Video Share (Waitress) on ${HOST}:${PORT} (threads=${THREADS}) ..."
exec "$VENV_DIR/bin/waitress-serve" \
    --listen="${HOST}:${PORT}" \
    --threads="${THREADS}" \
    --ident="simple-video-share" \
    wsgi:app
