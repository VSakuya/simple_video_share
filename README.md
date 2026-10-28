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
  admin-set bitrate), an optional QuickSplit-style clip editor, and segmented
  upload for files over 1 GB.
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
