# Simple Video Share

[English](./README.md) | [日本語](./README.ja.md) | [简体中文](./README.zh-CN.md)

> English is the source of truth. Other language files are translations of this
> document.

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
  admin-set bitrate), an optional QuickSplit-style clip editor, and resilient
  segmented upload with resume/retry support for large or unstable transfers.
- Watch page with HTTP Range streaming, cache-on-demand download from Google
  Drive, comments (two-level replies, kaomoji welcome), tags, and a Download
  button.
- Folders (nested tree, unlimited depth) and a tag system.
- Live rooms (Node-Media-Server FLV streams behind Apache, on-air detection,
  cover/title management, presence + persisted chat; H.264 and Enhanced-FLV AV1
  playback in compatible browsers).
- Watch Together rooms (synchronized playback, presence, ephemeral chat).
- Admin console: videos / users / live rooms / tags / settings / server &
  client logs, plus storage import/export with checksum verification.

## Requirements

- Python **3.12** or above — a virtual environment at `.venv/`.
- `pip install -r requirements.txt` (flask, pydrive2, waitress).
- `config.json` at the project root — copy `config.example.json`; the secret
  key is auto-seeded on first run.
- Google Drive OAuth credentials under `credentials/` (managed by the app at
  runtime; never edited by hand).
- Production: the bundled `000-default.conf` Apache vhost (HTTPS, reverse
  proxy to Waitress on `127.0.0.1:8080`, FLV playback guard).


## Run

- Local development: `python main.py`

- Production (Linux VPS): `./start.sh` - pull, create `.venv`, install
  requirements, then run Waitress. It must stay a single process with many
  threads: room presence, upload progress and Watch Together state live in
  process memory, and `app/runtime_guard.py` logs a CRITICAL warning when a
  multi-worker launcher or a second instance sharing `storage/` shows up.
- Regression probes: `python testing/run_probes.py` (options `--list`,
  `--only <name>`, `--timeout <s>`, `--with-node`). Each probe also runs on its
  own, e.g. `python testing/probe_home_cards.py`.

## Security and dependency notes

- Every POST form carries a CSRF token through `{{ csrf_field() }}`, and
  `app/static/js/csrf.js` attaches `X-CSRF-Token` to every unsafe same-origin
  `fetch` / `XMLHttpRequest`. The guard itself is `app/csrf.py`.
- `cookie_secure` in `config.json` (default `true`) sets the `Secure` flag on the
  session cookie; `HttpOnly` and `SameSite=Lax` are always set. Keep it `true`
  unless you test over plain HTTP from another machine on your LAN (browsers
  treat `http://127.0.0.1` as a secure context, so local development is fine).
- `requirements.txt` is pinned, because `start.sh` reinstalls it on every deploy
  - bump a pin deliberately instead of letting an upstream release land by
  itself. Development-only tools (pyright) are pinned in `requirements-dev.txt`,
  which `start.sh` never installs.

## The optional 32 MB asset: ffmpeg.wasm

`app/static/js/ffmpeg/ffmpeg-core.wasm` is deliberately **not** in git. Uploads
transcode in the browser through mediabunny (WebCodecs); ffmpeg.wasm is only the
Stage-2 fallback for browsers that cannot encode the file. A 32 MB blob in the
repo would be carried by every clone and every boot-time `git pull`, so a fresh
clone starts with one warning in the log and the fallback unavailable until the
file is placed by hand:

    cp /somewhere/ffmpeg-core.wasm app/static/js/ffmpeg/ffmpeg-core.wasm
