# Simple Video Share

A lightweight video-sharing web app designed for a **very weak server**: all
heavy processing (transcoding, clipping, cover extraction) happens
**client-side in the browser** (WebCodecs via mediabunny, with an ffmpeg.wasm
fallback). The server only caches files temporarily (LRU eviction, 500 MB
free-space reserve) and mirrors them to **Google Drive** through a background
worker.

## Features

- Multi-user accounts (admin-managed; no self-registration) with avatars and
  forced first-login password change.
- Upload queue with client-side transcode (AV1/H.264 caps: 1080p / 60 fps /
  admin-set bitrate), an optional QuickSplit-style clip editor, and segmented
  upload for files over 1 GB.
- Watch page with HTTP Range streaming, cache-on-demand download from Google
  Drive, comments (two-level replies, kaomoji welcome), tags, and a Download
  button.
- Folders (nested tree, unlimited depth) and a tag system.
- Live rooms (Node-Media-Server FLV streams behind Apache, on-air detection,
  cover/title management, presence + persisted chat).
- Watch Together rooms (synchronized playback, presence, ephemeral chat).
- Admin console: videos / users / live rooms / tags / settings / server &
  client logs, plus storage import/export with checksum verification.

## Requirements

- Python **3.12** (the code is also compatible with 3.11; the current dev venv
  is 3.11.3) — a virtual environment at `.venv/`.
- `pip install -r requirements.txt` (flask, pydrive2, waitress).
- `config.json` at the project root — copy `config.example.json`; the secret
  key is auto-seeded on first run.
- Google Drive OAuth credentials under `credentials/` (managed by the app at
  runtime; never edited by hand).
- Production: the bundled `000-default.conf` Apache vhost (HTTPS, reverse
  proxy to Waitress on `127.0.0.1:8080`, FLV playback guard).

## Run

- Local development: `python main.py`
- Production: `./start.sh` (self-updating `git pull --ff-only`, starts
  `waitress-serve wsgi:app` with a PID file)

## Documentation

The single source of truth is **`documents/PROJECT_PROGRESS.md`** (requirements,
decisions, dated change log, project rules) — it is git-ignored and travels
with the deployment, so it is not in the repository. Pending code changes are
registered in dated `documents/code_review_YYYY-MM-DD_HHMM.md` files (timestamp
includes the creation time so same-day sheets never collide) before
implementation. Test/probe scripts live under `testing/`.