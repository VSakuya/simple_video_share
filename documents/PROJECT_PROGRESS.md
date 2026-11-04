# Project Requirements — Simple Video Share

> All code, identifiers, comments, and UI strings MUST be written in **English only**.
> This document is the single source of truth for the design and is meant to be re-checked
> during later implementation passes.

---

## Project Rules (standing)

- **Test scripts:** every test / probe / verification script MUST be generated under the
  **`testing/`** folder — never at the project root or inside `app/`.
- **Bug backlog:** `testing/bugs.md` is the running bug list; new bugs are appended there.
  Each line is one bug/issue; a resolved line is prefixed with `#` and can be skipped.

---

## 0. Project First Principle

**Documentation first — the requirements are the single source of truth.**

Any **new requirement or new instruction** MUST first be written into this document
(`documents/PROJECT_PROGRESS.md`) and confirmed **before** any code is written for it.
Code that has no corresponding entry here is incomplete and must be documented
retroactively in a dated §13.x session entry. This principle takes precedence over
all other project rules.

---

## 1. Overview

A lightweight video sharing web application. Users upload videos from their own machines,
where all heavy processing (transcoding, cover extraction) happens **client-side in the
browser**. The server only stores files temporarily and moves them to **Google Drive** for
long-term storage. The server is **very weak**, so **no heavy CPU work (especially
transcoding) is allowed on the server**.

- **Runtime platform:** Ubuntu
- **Language / runtime:** Python 3.12 (virtual environment at `.venv/`). The
  current dev venv is CPython 3.11.3 and the code is compatible with both;
  the spec stays 3.12 (rebuild the venv on 3.12 at the next deploy if desired).
- **Database:** SQLite (`sqlite3` from the standard library)
- **Server-side dependencies already available:** `pydrive2`, `googleapiclient`,
  `google_auth_httplib2`, `httplib2`, `oauth2client`, `requests`
- **Web framework:** **Flask** (chosen — simple, low memory footprint, suitable for a
  weak server, easy static serving + HTTP Range streaming). Added to `requirements.txt`.

### Core constraints
- **Keep 500 MB of free disk space on the server at all times.**
- **All transcoding / heavy computation happens on the user's machine (browser), never on the server.**
- **Codec priority:** AV1 (default) → H.264 (auto-fallback). HEVC is NOT offered. Admin-configurable.
- **Resolution cap:** 1080p (height). Source > 1080p → downscale to 1080p (keep aspect ratio). Source ≤ 1080p → keep as-is.
- **Frame rate cap:** 60 fps. Source > 60 fps → drop to 60. Source ≤ 60 fps → keep original.
- **Bitrate cap:** admin-configurable (the "Default bitrate" setting, default
5000 kb/s). The upload page is rendered with the current value exposed as
`window.SVS_MAX_BITRATE_KBPS`, and the client uses it as the transcode
bitrate **target** (a WebCodecs CBR target, not a hard ceiling — see §15.2).
  WebCodecs CBR can overshoot the target on complex content, so the encoded
  output may exceed this value; that is accepted. When direct-passing, the
  original bitrate is preserved (no forced increase).
- **Transcode decision table:**

| Condition | Action |
|-----------|--------|
| No clip AND H ≤ 1080 AND FPS ≤ 60 AND BR ≤ 5000 | **Direct pass** (original codec, no re-encode) |
| No clip AND any metric exceeds cap | **Transcode** to AV1 (1080p cap, 60fps cap, 5000kbps cap) |
| Clip selected (regardless of metrics) | **Transcode** the clipped segment to AV1 |

- **Container:** Transcoded output is always **MP4** (AV1 or H.264 in MP4).

### Project layout

All source lives in the `app` package; all runtime data lives under `storage/`:

```
simple_video_share/
├── app/                  # all Python source
│   ├── __init__.py       # create_app() — application factory
│   ├── config.py         # reads config.json (project-root-anchored)
│   ├── db.py             # SQLite (storage/data/app.db) + sql/ schema
│   ├── drive.py          # Google Drive client (credentials/)
│   ├── storage.py        # free-space checks + LRU eviction
│   ├── auth.py           # login/register + current_user guards
│   ├── routes/           # blueprints: home, upload, watch, user, admin
│   ├── sql/schema.sql    # DB schema source of truth
│   ├── templates/        # Jinja2 templates
│   └── static/           # css / js / img (+ ffmpeg.wasm assets)
├── storage/              # runtime data (created at startup)
│   ├── data/app.db       # SQLite database
│   ├── videos/           # cached video files
│   ├── covers/           # cover images
│   └── avatars/          # user avatars
├── credentials/          # Google Drive OAuth client (read-only, runtime-managed)
├── test/                 # test asset (test_original.mp4)
├── main.py               # thin entry point -> create_app()
├── config.json           # deployment config (gitignored)
└── start.sh
```

All paths resolve relative to the **project root** (the parent of `app/`), so the app
runs correctly from any working directory.

---

## 2. User Scenarios

### Scenario 1 — Upload (user machine → server → Google Drive)
1. User selects a video file in the web page.
2. **(Optional) Client-side clip:** the user may cut a single continuous segment
   (start `mm:ss` → end `mm:ss`) using a simple GUI (draggable preview + time inputs).
   Frame-accurate. Embedded in the upload flow (not a separate feature).
3. **Transcode decision (client-side, per the decision table in Section 1):**
   - Direct pass (no re-encode), **or**
   - **Client-side transcode** (in the browser) to 1080p cap / 60 fps cap / AV1 (fallback H.264),
     5000 kb/s cap, MP4 container. Progress is shown to the user.
4. **Client-side cover extraction:** if the user does not upload a custom cover, the browser
   captures the frame at **the 60-second mark** and uses it as the cover image.
5. Browser uploads the resulting video (and cover) to the server.
   - **If the final MP4 is > 1 GB**, the browser splits it into **segments** (e.g. ≤ 512 MB
     each) and uploads them in order (with an index + total). The server reassembles the
     segments into one file before the space check / Drive upload. Segmentation keeps each
     HTTP body small (avoids browser / `werkzeug` single-upload limits for very large files).
6. **Server space check:** before/while storing, ensure free space stays ≥ 500 MB + incoming size.
   If space is insufficient, **evict cached videos** (already stored on Google Drive) using
   an LRU policy until enough space is available.
7. Server saves the video to local disk (`storage/videos/`).
8. **Server uploads the file to Google Drive** for long-term storage (see Section 6).
   The Drive file ID and path are recorded in the database. The upload runs on a
   **background worker**: the HTTP response returns as soon as the local copy is
   stored, and the video row flips to `status='ready'` when the Drive upload
   completes (§13.17).
9. **After the Drive upload succeeds**, the server deletes the local file **if space is
   insufficient** (to keep ≥ 500 MB free). Otherwise the file may remain as a local cache.
10. Database is updated with the video record (metadata, cover, Drive reference) —
    the row is created **immediately** after the local copy is stored, with
    `status='uploading'` (§13.17).

### Scenario 2 — Watch (user → server → optional Google Drive)
1. User opens a video to watch.
2. Server checks whether the local file exists (already cached).
3. **If it exists** → stream it to the browser (HTTP Range / partial-content supported) and play.
4. **If it does not exist** → server downloads the **full file** from Google Drive to local disk
   (subject to the 500 MB free-space rule + LRU eviction), and the UI shows the video as
   **"Caching…"**. Once the download completes, the video is streamed and played.
   - **Chosen: "download fully before playing".** Progressive/edge streaming is NOT in v1
     (it is more complex and fragile on a weak server) but is listed as a possible later feature.
   - The played file supports **Range seeking** regardless (seeking in the player still works).

### Scenario 3 — Admin
- Admin logs in (user flagged `is_admin = 1`).
- Admin can manage **all** videos and **all** users, and edit global settings
  (default bitrate, codec, minimum free-space threshold, etc.).

### Scenario 4 — Regular User
- User logs in.
- User manages **only their own** videos: rename, move between folders, set/replace cover,
  delete, and create/rename/delete **folders**.

### Common access rules
- **Every user can watch every video** (regardless of owner).
- The **homepage** lists **all** watchable videos from **all** users in a
  **waterfall / masonry layout** with **lazy loading** (e.g. `IntersectionObserver`).

---

## 3. Client-Side (Browser) Processing

All heavy work runs on the user's machine, implemented in the web frontend:

- **Transcoding / clipping:** in-browser **ffmpeg.wasm** (WebAssembly build of FFmpeg),
  running in a **Web Worker** (so the UI stays responsive). The community build
  (`@ffmpeg/ffmpeg-core`) includes `libsvtav1` (AV1) and `libx264` (H.264).
  - **Codec priority:** AV1 (default) → H.264 (auto-fallback if AV1 encoder unavailable).
    HEVC is not offered.
  - **Clipping:** a single continuous segment (start → end), frame-accurate, implemented as
    `-ss <start> -to <end>` in the same ffmpeg call as the transcode.
  - **Caps applied on transcode:** height ≤ 1080, fps ≤ 60, bitrate ≤ 5000 kb/s. MP4 out.
  - **Direct pass** (no ffmpeg) when no clip and all caps already met.
  - Progress reporting is surfaced to the user.
- **Cover extraction:** use an HTML5 `<video>` element (or ffmpeg.wasm) to seek to the
  **60-second** mark, draw the frame to a `<canvas>`, and export it as a JPEG/PNG.
- **GPU acceleration (best effort):** when transcoding in the browser, prefer a **GPU-
  accelerated** encode path (e.g. a WebGPU-backed AV1/H.264 encoder, or a GPU ffmpeg
  backend) if the browser/OS exposes one; otherwise fall back to CPU ffmpeg.wasm. The goal
  is "use the GPU when available, else CPU" — the exact library is an implementation choice.
- **Segmented upload (client side):** when the final video exceeds **1 GB**, slice the file
  into fixed-size parts (e.g. 512 MB) with `Blob.slice()` and POST each part with an index;
  the server concatenates the parts into the single video file.
- **Memory limitation (must be respected):**
  - `ffmpeg.wasm` reads the **entire source file** into browser memory (MEMFS).
  - ~1–2 GB source is usually fine; several-GB 4K files may exhaust memory and crash
    the tab.
  - **Mitigation:** show file size before processing; warn if large; fail gracefully with a
    clear message (e.g. "File too large — consider clipping first, or choose direct pass").
  - **Direct pass** (no clip, no transcode) uses a plain file upload and has **no** memory limit.

---

## 4. Server Responsibilities (Weak Server)

- Serve the web frontend (static files) and API.
- Accept uploaded videos and covers.
- Enforce the **500 MB minimum free space** rule; evict **LRU** cached videos when needed.
- Upload files to / download files from **Google Drive** (network I/O only — acceptable).
- Stream cached videos with HTTP Range support for playback.
- Manage users, folders, videos, and settings in SQLite.
- **Never** transcode or otherwise perform heavy CPU work.

---

## 5. Data Model (SQLite)

- **users**: `id`, `username` (unique), `password_hash`, `is_admin` (0/1), `avatar_filename` (nullable),
  `must_change_password` (0/1, default 0), `created_at`
- **login_attempts**: `id`, `username`, `ip`, `attempted_at` — failed-login records used for the
  brute-force / credential-stuffing lockout (see Section 7)
- **folders**: `id`, `name`, `owner_id` (FK → users), `created_at`
- **videos**: `id`, `title`, `owner_id` (FK → users), `folder_id` (FK → folders, nullable),
  `google_drive_file_id`, `drive_path`, `drive_filename` (hashed, unique Drive name),
  `local_filename` (nullable = not currently cached),
  `cover_filename` (nullable), `duration`, `resolution`, `codec`, `bitrate`, `size_bytes`,
  `status`, `view_count`, `created_at`, `uploaded_at`
- **comments**: `id`, `video_id` (FK → videos, ON DELETE CASCADE), `author_id` (FK → users,
  ON DELETE CASCADE), `body` (TEXT, ≤ 2000 chars, plain text — kaomoji allowed, no HTML),
  `created_at` (UTC). Comments are newest-first in the UI.
- **settings**: `key`, `value`
  - e.g. `default_bitrate` (5000), `default_codec` (`av1`), `fallback_codec` (`h264`),
    `max_resolution` (1080), `max_fps` (60), `min_free_space_bytes` (500 MB),
    `max_cache_mb` (0 = unlimited), `cache_root`, `covers_root`

### Schema source of truth
- All table-creation SQL lives in **`app/sql/schema.sql`** (source of truth), NOT
  hardcoded in Python. `db.init_db()` reads and executes that file. Add new
  tables/columns as new `.sql` files under `app/sql/` (applied in order) rather than
  inline DDL in code.

### Storage layout (server)
All runtime data lives under `storage/`:
- `storage/data/` → SQLite database file
- `storage/videos/` → cached video files
- `storage/covers/` → cover images
- `storage/avatars/` → user avatars

### Space management policy (local disk AND Google Drive)
- **Local reserve:** always keep ≥ `min_free_space_bytes` (500 MB) free on local disk.
- **Drive reserve:** also check the Google Drive account quota
  (`quotaBytesTotal - quotaBytesUsed` = remaining). The incoming file must fit in Drive.
- **On upload:**
  1. Check **Drive** remaining ≥ incoming size. Drive is the permanent store, so there is no
     "eviction" that frees Drive space — a video stays on Drive until explicitly deleted.
     If Drive has no room, **reject** the upload with a clear message.
  2. Check local disk: if `local_free < reserve + incoming_size`, evict **LRU** videos that
     are already stored on Google Drive (this frees *local* space only) until enough exists.
     If even after evicting everything local free < reserve, **reject** with a clear message.
- **After Drive upload:** if local free space drops below the threshold, delete the
  just-uploaded local file (it remains available on Google Drive).

---

## 6. Google Drive Integration

- Use **`pydrive2`** exactly like `test.py`:
  - `client_config_file` → `credentials/client_secret.json`
  - Load saved creds from `credentials/mycreds.txt`; if missing → `LocalWebserverAuth()`,
    if expired → `Refresh()`, else → `Authorize()`; always `SaveCredentialsFile(...)`.
- **Target folder:** `drive_folder_id` is read from `config.json` (see Section 11).
- Upload videos to the configured Drive folder and store the returned **file ID** and path.
- Download by file ID when a cached copy is missing (Scenario 2).
- **Quota check:** before an upload, call the Drive API `about` method to read
  `quotaBytesTotal` / `quotaBytesUsed` and compute remaining bytes. This is how the server
  enforces the Drive-side half of the space-management policy (Section 5).
- **`credentials/` is read-only and managed by the app at runtime** — never edit those
  files directly from source code.

---

## 7. Security & Auth

- Passwords hashed with `werkzeug.security` (`generate_password_hash` / `check_password_hash`).
- Session-based login (Flask session); distinguish **admin** vs **regular user**.
- Route guards: admin routes require `is_admin`; user routes require ownership (or admin).
- Google Drive credentials live in `credentials/` and are never committed or hand-edited
  (`.gitignore` already excludes `credentials/*` and `*.sqlite3`).

### Admin bootstrap (auto-created on first boot)
- On **every first boot** (i.e. whenever the `users` table is empty), the app creates a default
  admin account: username `admin`, password `admin`, `is_admin = 1`, `must_change_password = 1`.
  Once any user exists, this is a no-op (idempotent, safe to run on every startup).
- **Forced password change:** because the default credentials are public, the first login with the
  bootstrap account is redirected to a **mandatory** change-password page. The user must set a new
  password (min 6 chars, confirmed) before doing anything else. While `must_change_password` is set,
  a `before_request` guard bounces every non-auth / non-static request to that page.
- There is **no self-registration** at all. After bootstrap, new users are created **only by an
  admin** (admin area). The "Register" page and its links were removed.

### Login hardening (anti credential-stuffing)
- **Unauthenticated** users see only the login page (and static assets). Home, watch, covers,
  avatars, and all app pages require login.
- Failed logins are recorded in `login_attempts` (username + IP + timestamp). A **sliding-window**
  lockout applies:
  - **Per username:** ≥ 5 failed attempts within 10 minutes → locked until the window clears.
  - **Per IP:** ≥ 20 failed attempts within 10 minutes → locked (stops distributed stuffing).
- A successful login clears that username's recorded failures.
- Each failed attempt also incurs a short fixed delay (default 0.5 s) to slow brute force.
- `login_attempts` rows older than 1 day are pruned automatically.

### User lifecycle (admin-controlled)
- **Self-service:** a logged-in user may change **their own** username, password, and avatar.
  A user cannot create/delete other users or change anyone's admin flag.

---

## 8. UI Requirements

- **Homepage:** waterfall/masonry grid of **all** videos from **all** users, lazy-loaded
  (IntersectionObserver). Each card shows the cover image, title, and the uploader's
  avatar (small circle) beside the username.
- **Upload page:** file picker, **(optional) clip GUI** — a QuickSplit-style timeline:
  a video preview + a thumbnail strip with draggable start/end handles (the clip defaults
  to the full video; a full-range clip is treated as "no clip"), transcode progress, cover
  preview (auto from 60s frame or user-uploaded), folder selection, submit.
- **Watch page:** video player with Range support; shows **"Caching…"** state while the
  server downloads from Google Drive. Above the description, the uploader's avatar
  (circle) and username are shown. Below the metadata, a **comment section** (§13.16):
  a textarea (≤ 2000 chars, plain text — kaomoji welcome) with a **kaomoji quick-insert
  bar**, and a list of the video's comments (author avatar, username, UTC timestamp,
  body). The author of a comment (or an admin) sees a Delete button.
- **User area:** manage own videos + create/rename/delete folders, set cover.
- **Admin area:** manage all videos/users + global settings (bitrate, codec, min free space,
  max resolution, max fps).
- **My Videos list:** each video row's title is a link to its watch page (same target as home
  cards), so a video can be opened directly from My Videos.
- **Video edit dialog:** visually consistent with the Upload page — it reuses the Upload form's
  components (`.upload-section`, `.upload-label`, `.upload-input`, and a `.upload-cover`
  preview box for the cover) instead of a bare form, and is a properly-sized modal card.
- **Confirm dialog** (logout, video delete, user delete): a small, centered modal card (≈420px),
  not a full-viewport overlay. Confirm **and** Cancel (and backdrop click) all close it.
- **Avatar:** uploaded from the Account page with client-side **cropping** (§9), and shown both on
  the Account page and in the sidebar. When unset, a **default avatar** image is shown.
- **Sidebar footer:** the account avatar (replacing the Account icon) links to the Account page
  and sits directly above the Log out link; both are pinned to the bottom-left of the sidebar.
- **Language:** all UI text in **English**.

---

## 9. Decisions & Assumptions

| Item | Decision |
| --- | --- |
| Web framework | Flask (added to `requirements.txt`) |
| Database | SQLite via stdlib `sqlite3` |
| Client transcoding | ffmpeg.wasm (in-browser, Web Worker) |
| Codec | AV1 default → H.264 auto-fallback. HEVC excluded. |
| Container | MP4 (always) |
| Resolution cap | 1080p (height), keep aspect ratio |
| Frame rate cap | 60 fps |
| Bitrate cap | 5000 kb/s (admin-configurable) |
| Direct pass | No clip + all caps met → upload as-is (original codec preserved) |
| Clipping | Browser-side, single segment, frame-accurate, embedded in upload flow |
| Cover extraction | Browser `<video>` + `<canvas>` at 60 s |
| Watch mode | Download fully before playing (progressive listed as future option) |
| Drive auth | OAuth user creds via pydrive2 (see `test.py`) |
| Drive folder | Read from `config.json` (`drive_folder_id`) |
| Eviction policy | LRU over videos already present on Google Drive |
| Free-space reserve | 500 MB |
| Cache capacity | Optional admin cap `max_cache_mb` (0 = unlimited) on total locally cached bytes; LRU-evicts Drive-backed videos, never touches in-flight uploads (§13.17) |
| Drive upload | Background worker (in-process thread) after the HTTP response; `videos.status` `uploading` → `ready` / `failed`; progress polled via `GET /user/progress` (§13.17) |
| Admin bootstrap | Auto-created `admin`/`admin` on first boot (empty `users`); forced to change password |
| ffmpeg.wasm hosting | Self-hosted in `app/static/ffmpeg/` (no external CDN) |
| Server binding | `config.json` → `host` + `port` |
| Language | All code, UI, and docs in English |
| SQL location | `app/sql/schema.sql` (+ new `.sql` migration files), never hardcoded in Python |
| User creation | No self-registration; admin-only. Bootstrap admin auto-created on first boot |
| Login hardening | Sliding-window lockout (5/user, 20/IP per 10 min) + 0.5 s fail delay; guests see only login |
| Free-space check | Local disk **and** Google Drive quota (`about`) both enforced |
| Large upload | Final MP4 > 1 GB → split into segments (≤ 512 MB) and reassemble server-side |
| Browser encode | Prefer GPU-accelerated encode; fall back to CPU ffmpeg.wasm |
| Test asset | `test/test_original.mp4` (large 4K source, > 2 GB) for end-to-end tests |
| Modal dialog | `<dialog>` content wrapped in an `<article>` (Pico's modal-card pattern) so it is a sized card — Pico's `dialog { min-width:100% }` otherwise forces a full-viewport overlay |
| Avatar cropping | Client-side **Cropper.js**, vendored under `app/static/` (no external CDN, per §13.2/§13.4) |
| Default avatar | Bundled asset `app/static/img/avatar-default.svg` shown when `users.avatar_filename` is NULL |
| Comments | Plain text only (kaomoji welcome, HTML is escaped, never rendered); ≤ 2000 chars; author-or-admin can delete; stored in SQLite `comments` (no external service) |

---

## 10. Verification Workflow (per project rules)
- `python -m py_compile <files>` — syntax check.
- `python -m pyright <files>` — goal: `0 errors, 0 warnings`.
- `python -c "from app import create_app; app = create_app(); print('OK', app.name)"` — app builds
  (write diagnostics to `sys.stderr` if the value is dropped by the logger).
- Exercise handler / space-management / eviction logic in isolation with `python -c`
  one-liners before launching the server.
- For auth changes, exercise the test client: guest → redirect to login; `admin`/`admin`
  login → redirect to forced change-password; wrong password N× → lockout; successful
  change clears `must_change_password`; registration route no longer exists.

---

## 11. Deployment Config (`config.json`)

A JSON file at the project root. **Added to `.gitignore`** (contains secrets).
A `config.example.json` template is committed.

```json
{
  "host": "0.0.0.0",
  "port": 8080,
  "drive_folder_id": "your google drive folder id",
  "secret_key": "generate-on-first-run"
}
```

| Field | Description |
|-------|-------------|
| `host` | Bind IP. `0.0.0.0` = all interfaces (default). Can be a specific IP. |
| `port` | TCP port (default 8080). |
| `drive_folder_id` | Google Drive target folder ID for video uploads. |
| `secret_key` | Flask session signing key. Auto-generated on first run if placeholder. |

**Separation of concerns:**
- `config.json` → **deployment-level** parameters (IP, port, Drive folder, secret key).
- SQLite `settings` table → **runtime/admin** parameters (bitrate, codec, caps, thresholds).

**Behavior:**
- On startup, `create_app()` (invoked by `main.py`) reads `config.json`. If missing, print an error and exit
  (or generate a template from `config.example.json` with a generated `secret_key`).
- `credentials/` remains read-only, managed by the app at runtime (unchanged).

---

## 12. Development Process (rules of the road)

- **Summarize before executing:** whenever a **new instruction or requirement** is given, it
  MUST be summarized into this file (`documents/PROJECT_PROGRESS.md`) *first*, and only then
  implemented. Do not start coding a new requirement until it is reflected here.
- **No direct `git commit`:** the agent MUST NOT run `git commit` (or `git push`) directly.
  After making changes it MAY stage them (`git add`) and report exactly what is staged, but
  the user performs the actual commit (and any subsequent push).
- This rule is also persisted in `.clinerules/project_rules.md` so it survives across sessions.

---

## 13. Bug Fixes & UI Improvements (Session: 2026-09-22)

### 13.1 `/user/` 500 — broken import
- **Root cause:** `app/routes/user.py` had `from auth import current_user` (absolute import) inside the
  function body. The module lives at `app.auth`, so the correct form is the relative `from ..auth import ...`.
- **Fix:** Moved `current_user` into the top-level import alongside `login_required`.

### 13.2 Upload CORS — `ffmpeg-core.js` from unpkg.com blocked
- **Root cause:** `upload.js` called `toBlobURL("https://unpkg.com/@ffmpeg/core@0.12.6/dist/ffmpeg-core.js")`,
  but unpkg does not send `Access-Control-Allow-Origin` for `http://127.0.0.1:8080`, so the browser
  blocks the fetch and ffmpeg.wasm fails to load.
- **Fix:** Downloaded `ffmpeg-core.js` (112 KB) and `ffmpeg-core.wasm` (31 MB) into
  `app/static/js/ffmpeg/`. `upload.js` now passes same-origin URLs
  (`/static/js/ffmpeg/ffmpeg-core.js`, `/static/js/ffmpeg/ffmpeg-core.wasm`) to `ffmpeg.load()`.
  `toBlobURL` import removed (no longer needed).

### 13.4 Upload CORS — `@ffmpeg/ffmpeg` worker from esm.sh blocked
- **Root cause:** `upload.js` imported `FFmpeg` from `https://esm.sh/@ffmpeg/ffmpeg@0.12.10`, whose
  `load()` spawns `new Worker(<esm.sh URL>/worker.js)`; the browser refuses to construct a
  cross-origin Worker (`SecurityError: Failed to construct 'Worker': Script at
  'https://esm.sh/@ffmpeg/ffmpeg@0.12.10/es2022/worker.js' cannot be accessed from origin
  'http://127.0.0.1:8080'`). Also `ffmpeg.load({core, wasm})` used the wrong config keys —
  the 0.12 API expects `coreURL`/`wasmURL`, so the core would have fallen back to the
  unpkg default `CORE_URL`.
- **Fix:** Vendored the ESM builds locally (all imports inside them are relative, so they work
  as-is):
  - `@ffmpeg/ffmpeg@0.12.10` `dist/esm/` → `app/static/js/ffmpeg/lib/`
    (`index.js`, `classes.js`, `worker.js`, `const.js`, `utils.js`, `errors.js`, `types.js`, `empty.mjs`)
  - `@ffmpeg/util@0.12.1` `dist/esm/` → `app/static/js/ffmpeg-util/`
    (`index.js`, `const.js`, `errors.js`, `types.js`)
  - `upload.js` now imports `./ffmpeg/lib/index.js` and `./ffmpeg-util/index.js` (relative to
    `/static/js/upload.js`); `classes.js` resolves its own `worker.js` via
    `new URL("./worker.js", import.meta.url)` → same origin.
  - `ffmpeg.load()` now uses the correct keys `coreURL` / `wasmURL`.

### 13.5 Transcode fallback masked by `readFile` Code=-1
- **Root cause:** `@ffmpeg/ffmpeg@0.12` `exec()` does NOT throw when the ffmpeg process exits
  non-zero — it returns the exit code. A failed transcode (e.g. unsupported encoder/codec for
  the input) therefore left no `output.mp4` in the virtual FS, and `ffmpeg.readFile("output.mp4")`
  threw the misleading `Error: File could not be read! Code=-1` for every codec, hiding the real
  ffmpeg error.
- **Fix:** `runFfmpeg()` now captures `const code = await ffmpeg.exec(args)` and throws
  `"ffmpeg exec exited with code <n> for args: ..."` when non-zero. The real cause is also
  visible in the console via the existing `ffmpeg.on("log", ...)` handler. The codec loop
  (`libsvtav1` → `libx264`) now falls back on the true error, and each attempt uses a fresh
  `FFmpeg` instance (clean virtual FS, input re-uploaded).

### 13.6 Tab OOM during transcode ("Paused before potential out-of-memory crash")
- **Root cause:** the screenshot showed the failure at `fetchFile` → `FileReader.onerror`
  (the `Code=-1` is just the fallback when a memory-pressure abort has no error code).
  Each codec attempt held in memory: the original File + a full-file ArrayBuffer
  (`readAsArrayBuffer`) + a copy in the WASM virtual FS + the transcoded output + the
  final read-back. Worse, the old code never called `ffmpeg.terminate()`, so a failed
  `libsvtav1` attempt pinned its entire WASM heap while `libx264` allocated the same
  amount again → renderer OOM.
- **Fix in `runFfmpeg()`:**
  - `ffmpeg.terminate()` in a `finally` block — the whole WASM heap is released after
    every codec attempt (success or failure).
  - `await ffmpeg.deleteFile("input")` after a successful `exec`, so the input is freed
    from the WASM heap before the output is read back (peak = max(input, output), not sum).
  - `new Blob([data.data.slice(0)])` — explicit copy out of the WASM heap so the Blob
    survives `terminate()`.
  - `fetchFile`/`writeFile` failure is now re-thrown with a clear message
    ("Failed to read the selected file into browser memory (N MB) …") instead of the
    generic `File could not be read! Code=-1`.
  - `processVideo()` returns the Blob directly; the direct-pass path still returns the
    original `File` (both satisfy `.size` / `.slice()` / `FormData.append`).

### 13.7 Large-file transcode OOM — stream the input via WORKERFS
- **Goal:** clip/transcode a large source **without loading the whole file into RAM**.
  The previous fix (§13.6) freed the input *after* it had already been fully copied into
  the WASM heap via `fetchFile` (a whole-file `ArrayBuffer`) + `writeFile`. For a multi-GB
  source that whole-file copy alone can OOM the tab before the transcode even starts.
- **Capability:** the already-vendored `@ffmpeg/ffmpeg@0.12.10` exposes
  `FFmpeg.mount(fsType, options, mountPoint)` / `unmount(mountPoint)` / `createDir(path)`,
  and `@ffmpeg/core@0.12.6` bundles the Emscripten `WORKERFS` driver (`FS.filesystems.WORKERFS`).
  No new dependency / npm install is required.
- **How WORKERFS works:** `mount("WORKERFS", { files: [file] }, mountPoint)` creates a
  read-only node for each `File` at `mountPoint/<file.name>`. `WORKERFS` reads those nodes
  in chunks through `FileReaderSync` (available in the Web Worker that runs ffmpeg-core),
  so the input is **streamed** and never held as a single `ArrayBuffer`.
- **Fix in `upload.js` (`runFfmpeg()`):**
  - Create a fresh `FFmpeg`, `load()`, then `createDir("/in")` and
    `mount("WORKERFS", { files: [inputFile] }, "/in")`. The input is referenced by its
    mount path `WORKERFS_INPUT_PATH` (`/in/input.mp4`) in the `-i` arg; the transcoded
    output is written to the FS root (MEMFS).
  - The input `File` is wrapped as `new File([sourceFile], "input.mp4", { type })` so the
    node name is fixed and safe (avoids unusual characters in the browser file name).
  - After `exec`, `readFile("output.mp4")` is **awaited** (it returns a Promise resolving to
    a `Uint8Array` whose buffer is transferred to the main thread), and the Blob is built
    from that buffer.
  - `finally` still calls `terminate()`, which releases the whole WASM heap *and* the
    WORKERFS mount. (The browser `File` itself is untouched.)
- **Scope / limits:** WORKERFS removes the input-side whole-file copy (peak WASM-heap
  memory is now ≈ output size, not input + output). The transcoded **output** still lives in
  the WASM heap until read, so a very large *output* can still OOM — that is handled by
  downscaling / shortening the clip (or future server-side transcode).
- **Verified API (vendored, byte-for-byte `@ffmpeg/ffmpeg@0.12.10` / `@ffmpeg/core@0.12.6`):**
  `mount`/`unmount`/`createDir`/`readFile` all go through the worker message bus and resolve
  to a Promise; the worker's `mount` looks the driver up in `ffmpeg.FS.filesystems[fsType]`.

### 13.3 Upload page redesign
- Replaced the bare form with a YouTube-Studio / Bilibili-style layout:
  - Large **drag-and-drop zone** with icon, "Drag and drop video files to upload" text,
    "Select files" pill button, and file name + size display once a file is chosen.
  - **Title** input (visible text field, not hidden).
  - **Cover** section with image preview and "Choose cover image" button.
  - **Clip** panel (shown only after file selection) with Start / End time inputs.
  - **Progress** bar with status text.
  - **Upload** pill button, centered.
- Added drag-and-drop event handlers in `upload.js` (`dragover`, `dragleave`, `drop`).
- Added corresponding CSS classes (`.upload-*`) to `style.css`.

### 13.8 FFmpeg scale filter — unescaped comma broke the filtergraph
- **Root cause:** `processVideo()` built `-vf scale=-2:min(1080,ih)`. In FFmpeg's
  filtergraph syntax a comma separates filters in a chain, so the unescaped comma in
  `min(1080,ih)` splits the graph — ffmpeg tries to resolve `ih)` as a standalone filter
  and fails with `No such filter: 'ih)'` (the scale expression is mis-parsed).
- **Fix:** escape the comma so the filtergraph parser treats it as a literal argument to
  `min()`: `scale=-2:min(1080\,ih)`. In the JS source that is
  `"scale=-2:min(" + MAX_HEIGHT + "\\,ih)"` (the `\\,` becomes `\,` in the string handed to
  ffmpeg). Args are passed as a discrete array to `ffmpeg.exec(args)` — never a
  shell-style joined string — so nothing re-splits on the comma.
- **Also:** `runFfmpeg()` now logs the final `args` array as discrete elements
  (`console.log("[ffmpeg] exec args:", args)`) right before `ffmpeg.exec(args)`, so the
  browser console can confirm the `-vf` value reaches FFmpeg intact.
- **Verified:** with the escaped expression a 2560x1440 source transcodes to a valid
  1920x1080 output (via the `libx264` fallback).

### 13.9 QuickSplit-style clip timeline
- **Goal:** replace the Start/End text inputs + Apply button with a visual timeline — a
  video preview, a thumbnail strip, and draggable start/end handles; the selected clip
  drives the FFmpeg `-ss`/`-to`.
- **Implementation:**
  - `upload.html`: `#clip-panel` now holds `<video id="preview-video" controls muted
    playsinline>`, a `#timeline-strip` (thumbnail row + `#clip-dim-left`/`#clip-dim-right`
    overlays + `#clip-start-handle`/`#clip-end-handle`), and a label row
    (`#clip-start-label`/`#clip-end-label`/`#clip-duration-label`). `#clip-start` /
    `#clip-end` are now `type="hidden"` (still read by `clipRange()`).
  - `upload.js`:
    - `setupTimeline(file)` points the preview `<video>` at a fresh object URL for the
      file (old URL revoked) and re-renders the timeline.
    - `generateThumbnails()` seeks a hidden `<video>` to ~1 frame / 15 s (capped at 36),
      draws each frame to a 160×90 canvas, and appends JPEG thumbnails to the strip. A
      generation-id guard aborts a stale run if the user picks a new file mid-generation.
    - `updateTimeline()` positions the two handles + the dim overlays + the time labels
      from the clip start/end (percentages of `timelineDuration`).
    - `setupHandleDrag()` wires each handle so dragging updates the matching hidden input
      (clamped so the handles stay ≥ 1 s apart and within the duration); clicking the
      strip seeks the preview; `timeupdate` pauses the preview at the clip end.
    - Persistent listeners attach once at module scope (the elements live for the page),
      so re-selecting a file only re-points the preview `src`.
  - `style.css`: `.upload-timeline*` classes (video, strip, thumbnail row, dim, handles,
    info row).
- **No-transcode path preserved:** `clipRange()` returns `null` for a clip spanning
  (essentially) the whole video (`start < 1 && end >= duration - 1`; the 1 s tolerance
  absorbs whole-second rounding). A full-range clip therefore does NOT force a transcode —
  the direct-pass path (§9) still uploads the original `File` when all caps are met. A
  proper sub-range clip still forces a re-encode with `-ss`/`-to`.
- **Wiring unchanged:** the timeline writes the same `#clip-start`/`#clip-end` values that
  `clipRange()`/`processVideo()` already consume, so no FFmpeg-arg changes were needed
  beyond §13.8.

### 13.10 Browser testing — progress stuck on "Preparing…" + thumbnail TypeError
- **Progress never advanced (§13.5's `on("progress")` shape was wrong):** the vendored
  `@ffmpeg/core@0.12` posts progress as `{progress, time}` (both in **ms**), NOT the
  `{ratio}` the `@ffmpeg/ffmpeg@0.12` README documents. Verified in the vendored files:
  `ffmpeg-core.js` → `receiveProgress(progress,time){Module["progress"]({progress:progress,
  time:time})}` → `worker.js` forwards `data` untouched → `classes.js`
  `on("progress")` callback receives it as-is. The old handler destructured `{ ratio }`
  (undefined), so `setProgress` never fired and the bar stayed on "Preparing…" while
  `libx264` encoded.
  - **Fix:** the handler now accepts both shapes — a real `ratio` if present, otherwise
    `progress / time` (clamped to `[0,1]`) — and calls
    `setProgress(r, "Transcoding…")`.
- **Thumbnail `TypeError: Cannot set properties of null` at `generateThumbnails`:** the
  code did `$(".upload-timeline__thumbs")`, but `$` is `document.getElementById` — the
  strip's thumbnail `<div>` had a *class*, not an *id*, so it returned `null`.
  - **Fix:** added `id="timeline-thumbs"` to the div in `upload.html` and changed the
    lookup to `$("timeline-thumbs")`. The strip already has `height: 80px` and
    `img { flex: 1 0 0; object-fit: cover }`, so thumbnails now render.
- **Not a bug:** a Flask `304` (Not Modified) in the server log during upload is a
  same-origin static asset being re-requested from cache — harmless, not an error.
- **Perf note (expected, not a code bug):** the vendored ffmpeg.wasm is built
  `--disable-asm --disable-pthreads`, so a 4K 60 fps → 1080p re-encode is
  single-threaded and slow (minutes). The progress bar now reflects this so the user
  knows it is working rather than hung.




### 13.11 Follow-up fixes (GPU question, upload UX, preview audio, account/admin UI, forced password change)

**1. Transcode speed — input seeking (the "long Preparing…" bug).**
- Confirmed: the browser transcode is **CPU-only**. `ffmpeg.wasm` is pure WebAssembly with no GPU
  encoder path, and the vendored build is single-threaded (`--disable-asm --disable-pthreads`), so
  4K sources take minutes. Server-side FFmpeg (with optional NVENC) remains a future option; no
  GPU is used today.
- Root cause of the very long "Preparing…" before "Transcoding…": `-ss`/`-to` were passed **after**
  `-i` (output options), so FFmpeg decoded and dropped everything from `0` to `clip.start`. For a
  4K HEVC 60 fps source that is minutes of single-threaded WASM decoding before the first output
  frame (progress stays at 0% because it is derived from output timestamps).
- Fix: `-ss` is now an **input option** (before `-i`, fast seek + default `-accurate_seek` keeps it
  frame-accurate) and `-to` was replaced with `-t <end-start>` (output timestamps are reset after an
  input seek, so `-to` would be wrong). Progress now starts at 0 and runs 0→100% over the clip only.

**2. Upload button / progress UX.**
- After the Upload button is pressed: the button is **disabled** and relabelled ("Processing…"), the
  drop zone is frozen (no file re-selection), and the progress panel is visible for the whole
  pipeline (Preparing → Transcoding → Extracting cover → Uploading).
- While no measurable ratio exists (Preparing/Extracting/Uploading) the bar shows an **indeterminate
  animated** state so the user knows it is alive; during transcoding it shows the real 0→100%.
- On error the button and drop zone are restored; on success the page navigates away.

**3. Preview audio.**
- The timeline preview `<video>` was hard-muted (`muted` attribute + JS). Both removed, so the user
  can play the preview with sound. (The hidden probe/thumbnail/cover `<video>` elements stay muted.)

**4. Account & Admin page layout.**
- Both pages were bare `<section>`/`<ul>`/inline-form markup — redesigned with the existing dark
  design system: `.panel` cards, two-column `.panel-grid` (responsive), `.field` label/input groups,
  `.btn` / `.btn--danger` / `.badge` components, proper tables, larger avatar. No behaviour change:
  same forms, same routes, same hidden inputs and data attributes.

**5. Forced password change kept the old password.**
- Diagnosis: the DB proved the forced change **did commit** (`must_change_password` cleared, new
  scrypt hash stored) — the stored hash verified against "admin", i.e. the "new" password submitted
  was "admin" itself (browser autofill). The code accepted **new password == current password**.
- Fix: when the change is **forced** (`must_change_password` set), a new password equal to the
  current one is rejected ("The new password must be different…"). The voluntary Account page
  rejects the same-password case too.
- The bootstrap `admin` account was re-flagged `must_change_password = 1` so its next login forces a
  real change (admin→admin is now blocked by the new guard).

### 13.12 Follow-up: mediabunny as the primary browser-side transcode engine

**1. Transcode chain (client-side).** `processVideo()` now runs a three-stage chain, in order:
1. **Direct pass** — when `needsTranscode()` is false (no clip and all caps met), the original
   file is returned unchanged (§req. 7).
2. **mediabunny** (WebCodecs, hardware-accelerated) — the preferred engine. It tries each codec
   in `WEBCODECS_CODEC_PRIORITY` (`av1` → `avc`), transcodes to **MP4** with
   `forceTranscode: true`, applies the caps (1080p / 60fps / 5000kbps) **only when needed**,
   and **copies** audio (no re-encode). Height-only capping preserves the aspect ratio
   (matching the old ffmpeg `scale=-2:min(1080,ih)`).
3. **ffmpeg.wasm (ST)** — CPU fallback, logic unchanged (AV1 → H.264).
4. **Original-file passthrough** — final fallback so the upload is never lost.

**2. Secure-context requirement.** WebCodecs (and therefore mediabunny) only runs in a secure
context — `HTTPS` or `localhost`. `mediabunnyAvailable()` checks `window.isSecureContext &&
"VideoEncoder" in window` plus a successful dynamic import of the same-origin
`vendor/mediabunny.min.mjs`; on any failure it silently falls back to ffmpeg.wasm (no error shown).

**3. Deferred AAC re-encode.** The mediabunny path copies audio (the `audio` option is omitted)
for now, matching the ffmpeg.wasm path's "copy audio when possible" behavior. AAC re-encoding
is a later task.

**4. Cancellation.** `beforeunload` and a fresh codec attempt call `cancelActiveTranscode()`,
which `abort()`s the `AbortController` (passed to `Conversion.execute()` as `pauseSignal`)
**and** calls `Conversion.cancel()`, so `execute()` throws `ConversionCanceledError`.
`processVideo` treats that as "cancelled" (not a codec failure), so the loop stops and the
upload is aborted.

### 13.13 Logging system + no-silent-passthrough (Session: 2026-09-24)

**1. End-to-end logging.** A central logging system was added so any upload failure can be
diagnosed from the server without reopening DevTools:
- **Server:** new `app/log.py` configures the `simple_video_share` logger with two rotating
  files — `storage/logs/app.log` (server activity, also mirrored to the console) and
  `storage/logs/client.log` (browser pipeline logs). `app/routes/upload.py` adds
  `POST /upload/log` to receive the client lines; `drive.py` and the upload / seg /
  finalize handlers now log their key steps.
- **Client:** `upload.js` adds `clientLog(level, msg)`, which timestamps each line, writes it
  to the console, renders it in a visible on-page `#log-console` panel (with a Clear button),
  and batches the lines to `POST /upload/log`. Every pipeline stage is logged: file selected,
  probe (which engine + result), the transcode decision + caps, each engine attempt
  (mediabunny codec / ffmpeg.wasm encoder+audio) with its success/failure, cover extraction,
  and upload start / accept / done.

**2. No silent passthrough (hard block).** Previously `processVideo()` silently passed the
original file through whenever mediabunny was unavailable or a transcode failed — so a file
that needed transcoding (a clip, or height > 1080p) could be uploaded **untranscoded**. The
chain is now:
1. **Direct pass** — only when `needsTranscode()` is false (no clip AND height ≤ 1080 AND
   fps ≤ 60 AND bitrate ≤ 5000). This is the *only* case the original file is uploaded.
2. **mediabunny** (WebCodecs) — AV1 then H.264.
3. **ffmpeg.wasm** (CPU) — restored as a real fallback (it had been dropped in a prior
   session). AV1 (`libsvtav1`) then H.264 (`libx264`); audio is copied first and re-encoded
   to AAC if the copy is not MP4-compatible. Runs in **any** context (no secure-context /
   WebCodecs requirement), so it also covers LAN-IP access where WebCodecs is unavailable.
4. **Hard stop** — if every engine fails, `processVideo()` throws (via `failTranscode`)
   instead of passing the original through; the upload is aborted and a red
   `#transcode-warning` banner shows the exact reason. A non-compliant original is never
   uploaded.

**3. Root cause of the reported error.** `mediabunny.min.mjs failed to load (Failed to fetch
dynamically imported module: …)` was caused by the bundle being served with a `text/plain`
MIME type. The `.mjs` → `application/javascript` fix in `app/__init__.py` made the server
correct, but browsers cache the *response* of a dynamic `import()` in the HTTP cache — so a
client that had already fetched the bundle (with the old `text/plain` type) kept reusing the
stale cached response even after a server restart (a `304` revalidation preserves the
original cached headers). The import URL therefore carries a cache-bust query
(`MEDIABUNNY_BUNDLE = "./vendor/mediabunny.min.mjs?v=2"`); bump `?v=N` whenever the vendored
bundle is replaced, so clients always fetch the current file with the correct MIME type.

### 13.14 UI polish, confirm dialogs, video details & description (Session: 2026-09-24)

UI work completed in a prior pass that had not yet been recorded in this document:

**1. Smaller, consistent elements.** Global type/spacing scale shrunk in `style.css` (`html` at
14px, smaller `h1`/`h2`/`h3`, and smaller `input`/`textarea`/`button`/`table`), so the UI reads as
a compact app rather than Pico's default large forms.

**2. Collapsible desktop sidebar.** The desktop sidebar collapses to an icon-only rail
(YouTube-style); the choice persists in `localStorage`. On mobile it is an off-canvas drawer opened
from the top bar.

**3. Inline SVG icons.** All navigation / action icons are inline SVG (no icon font), so they
inherit `currentColor` and need no external request.

**4. Table action buttons stay inline.** Pico forces `width:100%` on `button[type=submit]`;
`.row-actions .btn { width: auto }` keeps the per-row Edit/Delete buttons from overflowing.

**5. Generic confirm dialog.** A single reusable `<dialog id="confirm-dialog">` (driven by any
element with a `data-confirm` attribute) replaced native `confirm()`. Logout, My-Videos delete, and
Admin user-delete all use it. *(Its two defects — full-viewport rendering and a non-closing Cancel
button — are fixed in §13.15.)*

**6. Per-video edit + human-readable sizes.** The My Videos table has a per-row Edit dialog (title,
description, new cover). Byte counts render through the `human_size` Jinja filter.

**7. Watch page technical details + description.** Below the player, a `.video-meta` grid shows
duration / resolution / codec / bitrate / frame rate / file size (only the fields that exist), via
new `human_duration` and `human_bitrate` filters. An optional nullable `videos.description` column
(added to `schema.sql` + a migration in `db.py`) is captured at upload (single + segmented) and at
edit, and shown (newlines preserved) only when present.

**8. Centered page content.** `.container` is nested inside `<main class="main">` so the sidebar
offset (`.main { margin-left }`) and the container's auto-centering no longer fight each other.

### 13.15 Confirm/edit dialogs, clickable My Videos, avatar crop + sidebar avatar (Session: 2026-09-24)

> Written per the documentation-first rule (§12) before implementation; all items below are now complete.

**Shared root cause (items 1 and 2):** Pico's `dialog` rule is `position:fixed; inset:0;
min-width:100%; min-height:100%`, and it sizes the *card* through a child `<article>`
(`dialog article { max-width:510px; margin:…; overflow:auto }`). Our `#confirm-dialog` and
`#edit-dialog` put their content directly in the `<dialog>` (no `<article>`), so Pico's
`min-width:100%` forces them to span the whole viewport — and `min-width` beats our `max-width`.
That is why the logout confirm looks "fullscreen" and the edit dialog looks stretched / ugly.

1. **Confirm dialog: small + working Cancel.** Wrap the message + buttons in an `<article>` so the
   dialog is a ≈420px centered card. Fix the JS `settle()` to call `dialog.close()` so **Cancel**
   (and backdrop-click) actually closes the dialog. Applies to logout, video delete, and user delete
   (all share the one dialog).
2. **Edit dialog: match the Upload page.** Restyle `#edit-dialog` using the Upload page's
   components (`.upload-section` / `.upload-label` / `.upload-input`, and a `.upload-cover`
   preview box for the cover) so it looks like the upload form. Wrap its content in `<article>` so
   it is a proper modal card, not full-viewport.
3. **My Videos rows are clickable.** In `user.html`, the video title cell links to `watch.page`
   (same as home cards), so a video can be opened from My Videos.
4. **Avatar: crop + default + display.** Upload from the Account page with client-side cropping via
   **Cropper.js** (vendored locally, no CDN). Show the avatar on the Account page **and** in the
   sidebar. When unset, show a **default avatar** (`static/img/avatar-default.svg`).
5. **Sidebar: avatar at the bottom-left.** Replace the Account nav icon with the avatar image and
   move that item to the bottom of the sidebar, directly **above** Log out; pin both to the
   bottom-left.

### 13.16 Comments with kaomoji support (Session: 2026-09-24)

> Written per the documentation-first rule (§12) before implementation.

1. **Every video gets a comment section** on its watch page, below the metadata: a form
   (textarea, `maxlength=2000`) plus the video's comments, newest first. Each comment row
   shows the author's avatar (default SVG when unset), username, and UTC timestamp.
2. **Kaomoji support.** Comments are **plain text** (Jinja autoescape, `white-space:
   pre-wrap`), so kaomoji like `(≧▽≦)` display exactly as typed with no markup language.
   A small **kaomoji quick-insert bar** above the textarea inserts the chosen kaomoji at
   the cursor position (the bar is client-side only, no extra HTTP request).
3. **Routes** (in `app/routes/watch.py`): `POST /watch/<video_id>/comments` (login
   required; body must be non-empty after trimming and ≤ 2000 chars — otherwise a flash
   error and redirect back) and `POST /watch/<video_id>/comments/<comment_id>/delete`
   (author or admin; anyone else gets a flash error).
4. **Data:** new `comments` table in `app/sql/schema.sql` — `video_id`/`author_id` are
   `ON DELETE CASCADE` FKs, so deleting a video or a user removes their comments
   automatically (consistent with the existing FK policy). CRUD lives in `app/db.py`
   (`add_comment`, `list_comments`, `delete_comment`).

### 13.17 Cover fix, background Drive upload, admin cache cap (Session: 2026-09-24)

> Written retroactively per the documentation-first rule (§0): these changes were
> implemented before being documented; this entry brings the document back in sync.

**1. Cover mix-up — every upload overwrote one file.**
- **Root cause:** the browser always sends the cover as `cover.jpg`, and the server
  saved it under the client-supplied filename — so every video overwrote the same
  `storage/covers/cover.jpg` and videos displayed each other's covers.
- **Fix:** `storage.save_cover()` stores covers under a guaranteed-unique name
  (`cover_<uuid>.<ext>`, original image extension preserved) and the DB records that
  filename. Used by both the upload flow and the edit-cover flow.
- **Legacy data:** videos uploaded before the fix share the one overwritten cover
  file; their covers must be re-set manually (My Videos → Edit → choose cover).

**2. Background Google Drive upload (no longer blocking the HTTP request).**
- **`videos.status` column** (`app/sql/schema.sql`): `'uploading'` → `'ready'` (Drive
  upload succeeded) / `'failed'`; default `'ready'`. `db._migrate` back-fills the
  column on older DBs with `'ready'` (pre-existing rows were uploaded synchronously,
  so they are ready).
- **New `app/drive_worker.py`:** a process-wide singleton `DriveUploadWorker` that
  - `enqueue()`s a daemon thread which pushes the local file to Drive
    (`drive.upload_file` now takes a `progress_cb` fed by the resumable-upload loop),
  - on success flips the row to `'ready'` (Drive id/path recorded), drops the local
    copy when disk is tight, then enforces the cache cap; on failure flips the row to
    `'failed'` and keeps the error in an in-memory progress store,
  - tracks in-flight video ids so the cache enforcer never evicts a video whose Drive
    upload is still running,
  - `reap_orphans()` on startup marks every `'uploading'` row left over from a
    previous (crashed / restarted) run as `'failed'` — the resumable transfer is gone —
    so the owner can retry.
- **Upload flow (`app/routes/upload.py`):** save the video (unique name) and the cover
  locally → create the DB row with `status='uploading'` → return to the browser
  immediately; the worker finishes the Drive upload in the background.
- **Visibility:** the shared home gallery lists only `status='ready'` videos
  (`db.list_public_videos`); the owner sees all of their own videos in My Videos.
- **Progress + retry (My Videos page):** `GET /user/progress` returns the in-flight
  progress store as JSON and the page polls it while any row is `'uploading'`, showing
  a per-video progress bar. A `'failed'` video gets a Retry button
  (`POST /user/videos/<id>/retry`) that re-enqueues the upload from the local copy —
  a local copy must still exist, otherwise the video cannot be retried.
- **Assumption:** single-process deployment — the progress store and in-flight set are
  in-memory. A multi-worker deployment would need a shared store (not a v1 requirement).

**3. Admin cache-capacity cap.**
- New admin setting `max_cache_mb` (0 = unlimited, the default) on the settings form,
  persisted by `POST /admin/settings` — that route was missing (the form had no submit
  destination; a latent bug fixed together).
- `storage.enforce_cache_limit()` evicts LRU cached videos until the total recorded
  cache size is under the cap. The total is `SUM(size_bytes)` over rows with a local
  copy (a cheap DB query — no filesystem stat). A video is evicted only if it has a
  local copy AND a Drive copy AND is neither in-flight nor `'uploading'` — an
  in-progress upload is never dropped.
- Enforced at: startup (`create_app`), after each new upload is stored locally, and
  after each background Drive upload completes.

**4. Known legacy data (manual follow-up, not code):**
- Video id=14's cover was overwritten by the pre-fix bug → re-set it in My Videos.
- A ~797 MB local file (HELLDIVERS2) has no DB row (orphan from a crashed pre-fix run)
  → delete it or re-upload it.

### 13.18 Upload success screen (Session: 2026-09-24)

After the browser has finished sending the transcoded file to the server (i.e.
`/upload/submit` or `/upload/seg/finish` returned `ok`), the upload page no longer
navigates away immediately. Instead it shows a **success screen**:

- A full-page overlay card: "Upload successful" + "Your video is saved — it is now
  being uploaded to Google Drive in the background."
- The page **auto-navigates to My Videos** (`/user`) after **3 seconds**, with a live
  countdown. A **"Go to My Videos"** button navigates immediately (and cancels the
  timer).
- My Videos is the destination (not home) because it is the page that shows the
  live per-video Drive upload progress bar (polls `GET /user/progress`) and the
  Retry button for `failed` uploads (§13.17).
- The non-AJAX fallback of `POST /upload/submit` (plain form post, no JS) also
  redirects to My Videos with the same "now uploading to Google Drive" flash.

**Where:** overlay markup in `app/templates/upload.html` (`#success-overlay`),
`showSuccess()` in `app/static/js/upload.js`, overlay styles in
`app/static/css/style.css` (`.upload-success*`), redirect in
`app/routes/upload.py:submit()`.

### 13.20 Nested comments — two-level replies (Session: 2026-09-24)

Comments on a video can now be **two levels deep**:

- A **top-level comment** (parent_id IS NULL) can receive **replies**
  (parent_id = the top-level comment's id).
- Replies **cannot** receive further replies (no third level); the UI hides the
  Reply button on replies.
- Replies are displayed **indented** beneath their parent comment, ordered
  oldest-first within a parent. Top-level comments remain oldest-first.
- Deleting a top-level comment removes its replies too (application-layer
  cascade in `db.delete_comment`).
- A reply's delete button is still shown only to the reply's author or an
  admin (same rule as top-level comments).
- The reply form is an inline `<form>` that appears beneath each top-level
  comment's "Reply" button (toggled by a small JS handler).

**DB change:** `comments.parent_id INTEGER REFERENCES comments(id) ON DELETE
SET NULL` — a `NULL` parent means top-level. The FK uses `SET NULL` so that if
a reply's parent is removed directly (e.g. by an admin deleting the parent),
the reply is **not** orphaned into a broken state; the application layer
handles cascade deletion explicitly for the normal flow.

**Future work (deferred):** an @mention/notification system would let reply
authors be notified; that requires a messaging or notification subsystem and
is intentionally out of scope here.

### 13.19 Resumable uploads across restarts, faster chunked Drive upload, speed display, MB free-space setting (Session: 2026-09-24)

**1. Uploads survive a server restart (no more orphan → failed).**
- The Drive resumable-session URI is persisted per video
  (`videos.drive_resumable_uri`, written when the session is established and
  refreshed on each 308; cleared when the upload completes).
- On startup the worker no longer marks leftover `'uploading'` rows `failed`;
  `resume_incomplete()` re-enqueues them (resuming from the last committed chunk
  via the saved session URI). Videos whose local file is gone are marked `failed`.
- The Retry button also passes the saved session URI, so a manual retry resumes
  instead of restarting. If the server no longer knows the session (HTTP 404/410),
  `drive.upload_file` transparently starts a fresh session.

**2. Faster Drive upload (10 MB chunks).** pydrive2's `File.upload()` was evaluated
and rejected: it runs a single `execute()` and aborts on the first network drop,
which is unreliable for large files. The existing `request.next_chunk()` loop is
kept, but `_UPLOAD_CHUNK_SIZE` rises 2 MB → 10 MB (still commits within the 120 s
request timeout), cutting per-chunk round trips by 5×.

**3. Upload speed shown in My Videos.** The worker records timestamped progress
samples; `GET /user/progress` now includes `speed_bps` (bytes/s over the recent
sample window) and the polling script renders e.g. "Uploading… 42% · 3.2 MB/s".

**4. "Min free space" admin setting is edited in MB.** The admin settings form now
edits `min_free_space_bytes` as MB (label "Min free space (MB)"); the save route
converts MB → bytes before persisting, so the stored value is unchanged.

### 13.20 Mount under a `/video` subpath via Apache reverse proxy (Session: 2026-09-24)

The app must be servable behind an existing site (e.g. `https://vsakuya.xyz/`) at a
subpath (`/video`) while the rest of the site (notably the `/live` HLS routes) keeps
working. Apache strips the `/video` prefix and reverse-proxies the remainder to the
loopback-only Flask backend, so **Flask routes stay app-relative** and no blueprint
`url_prefix` changes.

**Requirements:**
- New `config.json` field `base_path` (e.g. `"/video"`, or `""` for a root deployment).
  It is a **deployment parameter** (like `host`/`port`), not a runtime setting.
  `config.example.json` documents it as `""`.
- When `base_path` is non-empty, the app must inject `SCRIPT_NAME` into the WSGI
  environ **before** the request context is created, so `url_for()` and the
  `/static` handler emit base-prefixed URLs. This is done by wrapping
  `app.wsgi_app` in the factory.
- The base path is exposed to:
  - Jinja templates (so `url_for` is not needed for the few hand-written URLs), and
  - JS as `window.SVS_BASE` (set in `base.html`).
- **Server-rendered links** keep using `url_for(...)` — they pick up `SCRIPT_NAME`
  automatically. The two hand-written cover URLs (`home.html`, `user.html`) are
  switched to `url_for("home.cover", filename=...)`.
- **Client-side JS** prefixes its hard-coded app-relative URLs with
  `window.SVS_BASE`:
  - `upload.js`: `/upload/log` (log endpoint), `/upload/submit`, `/upload/seg/start`,
    `/upload/seg/part`, `/upload/seg/finish`, the `/user` redirects after a successful
    upload, and the ffmpeg `coreURL`/`wasmURL`.
  - `user.html`: the `GET /user/progress` poll and the edit form `action`
    (`/user/videos/<id>/edit`).
- **Unchanged:** the loopback-only Flask backend, the `/live` proxy, and every
  blueprint route. With `base_path` empty the app behaves exactly as before (all
  prefixes reduce to no-ops), so local development needs no change.

**Where:** `base_path` default in `app/config.py`; normalization + `wsgi_app` wrap
+ template exposure in `app/__init__.py`; `window.SVS_BASE` in
`app/templates/base.html`; `url_for("home.cover", ...)` in `app/templates/home.html`
and `app/templates/user.html`; `SVS_BASE` prefixes in `app/static/js/upload.js` and
`app/templates/user.html`; `base_path` in `config.example.json` / `config.json`.
Apache adds a `/video` `RewriteRule` + `ProxyPass`/`ProxyPassReverse` pair to the
existing `:443` virtual host (prefix strip + trailing-slash pass-through).

### 13.21 Folder system completion + cover cropping (Session: 2026-09-24)

> Written per the documentation-first rule (§12) before implementation.

The folder schema (`folders`, `videos.folder_id` FK `ON DELETE SET NULL`) and the
DB helpers (`create_folder` / `list_folders` / `rename_folder` / `delete_folder`)
already existed, but the UI never used them. This completes the feature and adds
client-side cover cropping.

**1. Folders — Upload page (choose existing or create new).**
- The previously hidden `folder_id` input is replaced by a visible `<select>` with
  **Root** (empty), every existing folder, and a **New folder…** sentinel.
  Selecting the sentinel reveals an inline name input. A hidden `folder_id` field
  remains; the JS resolves the selection into it before the upload starts.
- "New folder…" is created server-side via `POST /user/folders` (JSON when sent
  with `X-Requested-With`, so the upload page can read the returned `folder_id`
  before uploading). The resolved id is what the upload sends.

**2. Folders — My Videos (move a video, manage folders).**
- Each video row's *Folder* cell is now a `<select name="folder_id">` (Root + all
  folders, pre-selected to the current folder) that auto-submits to
  `POST /user/videos/<id>/move`. Empty value = move to Root.
- The folder panel's create / rename / delete controls are real server-rendered
  forms (previously buttons with no handler and no endpoint). Delete uses
  `data-confirm`; deleting a folder sends its videos to Root via the FK's
  `ON DELETE SET NULL` (the videos are not deleted).
- New endpoints in `user.py`: `POST /user/folders`, `POST /user/folders/<id>/rename`,
  `POST /user/folders/<id>/delete`, `POST /user/videos/<id>/move`. All are scoped
  to the owner (an admin may act on any). `db.get_folder()` is added for the
  ownership check.
- Upload `folder_id` is now validated to belong to the owner (or admin) in both
  the single and segmented paths, so a client cannot assign a video to another
  user's folder.

**3. Cover cropping (client-side, Cropper.js — same pattern as avatars).**
- A user-provided **cover** is cropped in the browser with the vendored
  Cropper.js (no CDN). Aspect ratio is fixed to **16:9** and the export is capped
  at **1280×720**, exported as JPEG.
  - *Why 16:9 / 1280×720:* the home gallery is a masonry of video covers whose
    dominant ratio is 16:9, and cards render ~350–400 px wide, so 1280×720 is
    retina-quality without inflating the upload (mirrors the 256×256 avatar cap).
- Applied in two places:
  - **Upload page** (`onCoverSelected`): the chosen cover opens the crop dialog;
    applying sets the cropped blob as the cover. Cancelling leaves no user cover
    (the pipeline then auto-extracts the 60 s frame).
  - **My Videos edit dialog**: choosing a new cover opens the same crop dialog.
- The **auto-extracted** cover (the 60 s frame) is unchanged — it already carries
  the video's own aspect ratio and needs no interactive crop.

**Where:** `db.get_folder` in `app/db.py`; new endpoints + `get_folder` import in
`app/routes/user.py`; folder ownership check in `app/routes/upload.py`; the folder
select, cover crop dialog, and Cropper.js include in `app/templates/upload.html`;
the folder forms, per-row move select, and cover crop dialog in
`app/templates/user.html`; `resolveFolder()` + cover crop in
`app/static/js/upload.js`; folder/move control styles in `app/static/css/style.css`.

### 13.22 Watch page shows Drive file name + folder button sizing (Session: 2026-09-24)

Two small UI follow-ups to §13.21:

**1. Watch page: show the Google Drive file name.**
The video detail page now lists the `drive_filename` (the hashed, unique name the
file is stored under in Drive) as a meta item, in monospace (`.mono`) so the long
hash is easy to read and copy. Rendered only when present, and added to the meta
block's render condition so it shows even if the other media fields are absent.
**Where:** new `Drive file name` item in `app/templates/watch.html`; `.mono`
style in `app/static/css/style.css`.

**2. My Videos: folder action buttons were oversized.**
Pico CSS forces `width:100%` on `button[type=submit]`. Inside the flex `.folder-create`
/ `.folder-rename` rows that made the action button swallow the whole row (the
"Create folder" button rendered full-width). Fixed by keeping those buttons
content-sized: `.folder-create .btn, .folder-rename .btn { width:auto; flex:0 0 auto }`.
**Where:** `app/static/css/style.css` (folder section, §13.21 block).

### 13.23 Upload clip: Set Start / Set End buttons replace the timeline drag (Session: 2026-09-24)

The Preview & clip panel's drag timeline (the `#timeline-strip` with sampled
thumbnails, dim overlays and the draggable `#clip-start-handle` / `#clip-end-handle`)
was replaced by a single row with two button groups. There is no timeline to drag
anymore; the thumbnail strip, dim overlays and handles are gone.

- **Row layout:** one row, two parts.
  - **Set Start** button + an editable time field showing the start time.
  - **Set End** button + an editable time field showing the end time.
- **How it works:** the user plays/scrubs the preview with the native `<video>`
  controls, then presses **Set Start** or **Set End** to capture the current time
  (written to the field next to that button). Each time field is **also manually
  editable** (`<input type="text">`): the user can type `M:SS`, `H:MM:SS`, or a
  bare number of seconds. On commit (blur/Enter) a valid entry is normalized
  (e.g. `90` -> `1:30`) and written to the hidden input; an invalid entry is
  reverted to the last valid value. `parseTime()` was extended to accept a bare
  number of seconds so manual entry is forgiving.
- **Clipping unchanged:** `clipRange()` still reads the same `#clip-start` /
  `#clip-end` hidden values and drives FFmpeg `-ss` / `-to`, so the re-encode
  pipeline is untouched. A clip with `end <= start` (or spanning the whole video)
  still means "no clip".
- The preview plays continuously after Set End (it does NOT auto-pause at the set
  end point); the old `timeupdate` auto-pause was removed because it blocked the
  user from scrubbing through the rest of the video.
- On file selection, start resets to 0:00 and end to the full duration.

**Where:** `#clip-panel` in `app/templates/upload.html` (new `.upload-clip-controls`
row with `#set-start-btn` / `#set-end-btn` + the editable `#clip-start-input` /
`#clip-end-input` time fields); `app/static/js/upload.js` (`currentClipStart` /
`currentClipEnd` / `updateClipLabels`, `onTimeInputChanged` + the two field
`change` listeners, the two button listeners, and `setupTimeline` now calls
`updateClipLabels`; `parseTime` now accepts bare seconds); `app/static/css/style.css`
(`.upload-clip-controls*` replace the old `.upload-timeline__strip` / `__thumbs` /
`__dim` / `__handle` / `__info` rules, and `__time` is now an editable field).
This supersedes the timeline-strip portion of §13.11.

### 13.24 Show uploader avatar on home cards + watch page (Session: 2026-09-24)

The uploader's identity is now surfaced on two read-only surfaces:

- **Home page cards:** each video card shows the uploader's **avatar** (small circle,
  default SVG when unset) beside the username. The avatar is 20 px; the username keeps
  the muted styling and truncates with an ellipsis if long.
- **Watch/detail page:** an **author row** is shown above the description — the uploader's
  avatar (circle, 56 px) and a larger username (1.25 rem). This is the same circular-avatar
  treatment used by comments (`§13.16`) and the sidebar (`§13.15`), scaled up for a
  prominent header.

**Data:** `db.list_public_videos()` (home) now also selects
`u.avatar_filename AS owner_avatar_filename`. `db.get_video_by_id()` (watch page) now joins
`users` on `v.owner_id` to expose `owner_name` and `owner_avatar_filename` (previously it
returned only the raw `videos` row). Both are additive keys; existing callers are
unaffected. The avatar is served by the existing `home.avatar_file` endpoint
(`GET /avatars/<filename>`), falling back to `static/img/avatar-default.svg` when
`users.avatar_filename` is NULL.

**Where:** `app/templates/home.html` (card meta row); `app/templates/watch.html`
(author row above the description); `app/db.py` (`list_public_videos`, `get_video_by_id`);
`app/static/css/style.css` (`.card__avatar` / `.card__owner`, and the new `.video-author*`
rules).

### 13.25 MP4 faststart (moov atom up front) for instant progressive playback (Session: 2026-09-24)

**Decision — container stays MP4; NO FLV transcode.** This app is VOD (upload a finished
video, others watch it later), served with `send_file(conditional=True)` (HTTP Range
progressive download) and played by the native HTML5 `<video>` tag. MP4 (AV1/H.264 + AAC)
is the standard for web VOD. FLV is a dead Flash-era container — modern browsers cannot
play it in `<video>` at all, and its only web use (`flv.js` over RTMP) is a live-streaming
stack, which is not what this app is. HLS/DASH (adaptive bitrate) would be over-engineering
for a single-machine site. The one thing that actually affects MP4 streamability is the
position of the `moov` atom (the index): if it is at the END of the file, the browser
cannot start until it has downloaded the whole file; if it is at the FRONT ("faststart"),
playback starts as soon as the header arrives.

**Current state (verified on disk, `storage/videos/`):**
- The mediabunny primary path is ALREADY faststart: it writes to a `BufferTarget`, and
  mediabunny automatically defaults `fastStart` to `"in-memory"` for in-memory targets
  (moov written up front). Confirmed by reading the vendored bundle and by inspecting the
  stored files (both existing videos have layout `ftyp → moov → mdat`).
- The **ffmpeg.wasm fallback** path (`upload.js` `runFfmpeg()`) does not pass
  `-movflags +faststart` → its output has `moov` at the end.
- The **direct-pass** path (an already-compliant original uploaded as-is) may also have
  `moov` at the end.

**Fix — one server-side choke point, no re-encode.** New `app/mp4.py` with
`ensure_faststart(path) -> bool`:
- Parses the TOP-LEVEL MP4 boxes only (`ftyp` / `moov` / `mdat` / `free` / …), including
  64-bit extended sizes.
- If `moov` already precedes `mdat` (or the file is fragmented MP4 with an `mvex` box,
  or has multiple top-level `mdat` boxes, or has no `moov` at all) → no-op.
- Otherwise it reorders the top-level boxes to `ftyp → moov → mdat → rest` (all other
  boxes keep their original relative order) and patches every 32-bit `stco` / 64-bit
  `co64` chunk offset inside `moov` by the delta of the mdat payload's new absolute
  offset. Box sizes are unchanged, so no other box needs rewriting.
- When descending `moov → trak → mdia → minf → stbl` to locate `stco`/`co64`, each
  container's child boxes may start right after its header (plain container) or after
  a 4-byte version/flags field (FullBox). `moov` is spec'd as a FullBox, yet common
  encoders (and the files this app serves) write it as a plain container, so the child
  start offset is detected dynamically by matching the expected first child box type
  (`mvhd` / `tkhd` / `mdhd` / `vmhd`|`smhd`|`hmhd`|`sthd` / `stsd`).
- Writes to a temp file in the same directory, verifies the size is unchanged and the
  result still parses (moov before mdat), then `os.replace()` over the original — atomic,
  never re-encodes, and on ANY parse error the original file is left untouched (warning
  logged). File size is identical before/after.
- Hooked in `app/routes/upload.py::_register_and_enqueue()` — the single point both the
  normal upload (`submit`) and the segmented upload (`seg_finish`) converge on — called
  BEFORE `drive_worker.worker.enqueue()` so the Google Drive copy is faststart too.
  Both existing stored videos were verified to already be faststart, so no migration of
  already-stored files is needed.

**Verification:** `python -m py_compile app/mp4.py app/routes/upload.py`;
`python -m pyright app/mp4.py app/routes/upload.py` (0 errors); round-trip test — take a
real stored video, build a copy with `moov` moved to the end (patching its `stco`
offsets), run `ensure_faststart()` on it, and assert the result is byte-identical to the
original; plus the standard GUI build check.

### 13.26 Player, folders, async comments, theming, proportional sizing (Session: 2026-09-25)

Seven coordinated upgrades:

**1. video.js player + in-page (non-OS) fullscreen.** The watch page's native
`<video>` is now a video.js player (vendored `static/vendor/video.min.js` +
`video-js.min.css`; the icon font is inlined as base64, so no extra font file). A
custom "in-page fullscreen" button (shared `static/js/inpage.js`) expands the player
to a `position:fixed` full-viewport overlay **inside the page** — it does NOT use the
browser Fullscreen API, so the browser chrome (tabs, address bar) stays visible.
Esc exits. The upload clip panel reuses the same helper (its old `requestFullscreen`
button is replaced). The upload *preview* intentionally stays a native `<video>`: it
is a tooling surface whose Set Start/Set End controls read the raw element's
`currentTime`, and wrapping it in a full player would add weight with no benefit.

**2. Proportional UI sizing.** The fixed `html { font-size: 14px }` becomes
`html { font-size: clamp(13px, 0.65vw + 10px, 22px) }`, so the whole rem-based UI
(Pico + this app) scales with viewport width on high-resolution screens. The
container max-width and the sidebar width are converted from fixed px to rem so they
scale too.

**3. Centered Set Start / Set End.** `.upload-clip-controls` now uses
`justify-content: center` so the two button+time groups are centered as a unit.

**4. Folders on the homepage + multi-level + better management.** `folders` gains a
`parent_id` self-reference (migration in `db._migrate`), enabling nested folders.
- Home (`home.index`) now shows top-level folders as cards plus the Root videos
  (those with no folder). A new `home.folder/<id>` route shows a folder's subfolders
  + its videos with a breadcrumb trail.
- My Videos (`user.index`) shows a nested folder tree with per-node rename/delete and
  an "Add subfolder" form; the per-video "move to folder" select is the full
  indented tree.
- `db.delete_folder` reparents a folder's children to its grandparent (its videos
  still fall back to Root via the FK).
- Upload's folder selector is the indented tree.

**5. Async comments (no reload) + local-timezone timestamps.** Posting a comment, a
reply, and deleting a comment go through `fetch()` with `X-Requested-With:
XMLHttpRequest`; `watch.add_comment` / `watch.delete_comment` return JSON on AJAX and
the new comment is rendered by a shared recursive partial (`_comment.html`) and
inserted into the DOM. Each comment `<li>` carries `data-ts="<UTC>"`;
`svsFormatCommentTimes()` re-renders every timestamp in the viewer's local timezone
(replacing the old hard-coded "UTC" label). The confirm dialog in `base.html` was
refactored to event delegation so dynamically-added delete buttons work.

**6. Light/dark theme + sidebar toggle.** The app was hard-coded dark. It now uses
`<html data-theme="dark|light">` (Pico v2's own mechanism) plus app-level CSS tokens
(the previously hard-coded hexes are now `--*` variables) defined under
`[data-theme="dark"]` and `[data-theme="light"]`. A sidebar button toggles the theme,
persists to `localStorage`, and the initial theme is the saved choice or
`prefers-color-scheme`. `color-scheme` is set per theme so native scrollbars/form
controls match (the cause of the broken look on light systems). The theme
button's `Theme` label is hidden in the collapsed (icon-only) rail like the other
nav labels, so the rail stays icon-only.

**Where:** `app/sql/schema.sql` (+ `db._migrate`); `app/db.py` (`create_folder`,
`delete_folder`, `list_root_folders`, `list_child_folders`, `list_folder_videos`,
`folders_tree`, `folders_with_depth`); `app/routes/home.py` (+ `home.folder`),
`app/routes/user.py` (+ `create_subfolder`), `app/routes/watch.py` (AJAX responses);
`app/templates/home.html`, `user.html`, `upload.html`, `watch.html`, `_comment.html`
(new), `base.html`; `app/static/css/style.css`; `app/static/js/inpage.js` (new);
vendored `static/vendor/video.min.js` / `video-js.min.css`.

**Verification:** `python -m py_compile` + `python -m pyright` on the touched Python
files; the standard GUI build; a browser pass over in-page fullscreen, folder
navigation, async comments (post/reply/delete + local time), the theme toggle, and
the centered clip controls.

### 13.27 video.js player matches the video's aspect ratio + in-page fullscreen in the control bar (Session: 2026-09-25)

**1. No more letterbox bars.** With `data-setup='{}'`, video.js injected
`.player-dimensions { width:<nativeW>px; height:<nativeH>px }` into a `<style>` tag.
Our `#player { width: 100% }` (id beats that class) made the box `100%` wide ×
`<nativeH>`px tall, so the forced `aspect-ratio: 16/9` was ignored (both width and
height were set) and the tech's `object-fit: contain` letterboxed/pillarboxed
anything that wasn't exactly `100%-width : <nativeH>px`. Fix: set
`window.VIDEOJS_NO_DYNAMIC_STYLE = true` on the watch page so video.js defers sizing
to our CSS, then set the element's `aspect-ratio` from the media's real
`videoWidth`/`videoHeight` on `loadedmetadata`. The player is now initialized
explicitly (`videojs('player')`) instead of via `data-setup`, so the sizing JS has a
player object immediately. The in-page fullscreen path already overrides sizing with
`width/height:100%` + `object-fit:contain`, so it is unaffected. A latent bug also
fixed: the tag-reuse `<video>` IS the `.video-js` element, so the overlay's
`object-fit:contain` (which targeted a *child* `<video>`) never matched; it now also
targets `.inpage-overlay__fill.video-js` directly.

**2. In-page fullscreen lives in the bottom control bar.** The old top-right
`#player-fs-btn` is removed. A control-bar button (`.svs-inpage-fs`, a styled
`vjs-control`) is inserted just before the native fullscreen button on `ready` and
toggles the in-page overlay. The native `.vjs-fullscreen-control` is hidden via CSS
because the app uses in-page fullscreen rather than the browser Fullscreen API.

**Where:** `app/templates/watch.html` (head flag, player init + sizing + control-bar
button IIFE, top-right button removed); `app/static/css/style.css` (dead
`.player-fullscreen-btn` removed; `.svs-inpage-fs`, `.vjs-fullscreen-control`,
`.inpage-overlay__fill.video-js` added); `app/static/js/inpage.js` (+ `isActive`).

**Verification:** browser pass — a 16:9 source fills the box with no bars; wide and
tall sources also fill the box; in-page fullscreen (now from the control bar) still
letterboxes correctly to the viewport; Esc and the control-bar button both exit.

### 13.28 Async comment post failed with `querySelectorAll is not a function` (Session: 2026-09-25)

Posting a comment (or reply) via the §13.26 AJAX path threw
`TypeError: (scope || document).querySelectorAll is not a function`. Root cause:
`postComment` did `li.innerHTML = res.j.html; var node = li.firstChild;`. The rendered
`_comment.html` has leading whitespace (the header `{# ... #}` comment's trailing
newline), so `li.firstChild` was a text node, not the `<li>` element. `refreshTimes`
then ran `(node || document).querySelectorAll` on that truthy text node (no
`querySelectorAll`), and the append also dropped the `<li>` (only the `"\n"` was
inserted). Fix: `var node = li.firstElementChild;` (skips the whitespace text node)
with an `if (!node) return;` guard. Server-rendered full-page comments were unaffected
because the `{% include %}` places the `<li>` directly in the `<ul>`, where the
whitespace is inert.

**Where:** `app/templates/watch.html` (`postComment`).

### 13.29 Restore native fullscreen, fix comment `Failed to fetch` (protocol-relative URL), and harden player aspect-ratio (Session: 2026-09-25)

Three regressions from the §13.27 video.js migration were fixed.

**1. Comment post `TypeError: Failed to fetch` — root cause: protocol-relative fetch URL.**
`config.json` ships `base_path: "/"`. The `app/__init__.py` normalization turned a
lone `/` into the truthy string `"/"` (`"/".rstrip("/")` is `""`, then re-prefixed
back to `"/"`), so `BASE_PATH = "/"` and templates exposed `svs_base = "/"`. Client
code builds hand-written URLs as `SVS_BASE + "/…"`, so watch.html's comment fetch
became `"/" + "/watch/<id>/comments"` = `"//watch/<id>/comments"`. A leading `//`
is a **protocol-relative** URL, so the browser fetched from a host literally named
`watch` (cross-origin) and rejected it with `Failed to fetch`. `url_for` was
unaffected (Flask treats `SCRIPT_NAME = "/"` as the root), which is why the page and
the video still loaded. Fix: strip trailing slashes and only re-prefix a leading
slash when the path is non-empty, so `""` and `"/"` both normalize to `""` (serve
from the domain root). The client now fetches `"/watch/<id>/comments"` (single
slash). This also fixes every other `SVS_BASE`-prefixed hand-written URL (upload.js
log/submit endpoints, user.html progress polling) that was building `//…` the same
way.

**2. Native fullscreen button missing.** §13.27 added
`.vjs-fullscreen-control { display: none !important; }` to hide the native button
because the app used in-page fullscreen; that removed native fullscreen entirely.
The rule is removed, so the native `.vjs-fullscreen-control` is visible and
functional again, alongside the in-page `.svs-inpage-fs` control-bar button
(§13.27) — both fullscreen paths now work.

**3. Player aspect-ratio hardened.** §13.27 set the box's `aspect-ratio` from the
media's `videoWidth`/`videoHeight` on the native `loadedmetadata`. That trigger can
miss the event in edge cases, leaving the box on the CSS `16/9` fallback (which
pillarboxes a non-16:9 source). The ratio is now applied on the native element
event, the video.js player's own `loadedmetadata` (fired after the tech's metadata
is ready), and the already-loaded `readyState >= 1` guard; whichever fires first
wins, the rest are no-ops. The CSS `16/9` stays as the pre-metadata fallback, and
the inline native ratio overrides it once loaded, so wide/tall sources fill the box
with no left/right bars and the container height follows the player.

**Where:** `app/__init__.py` (`BASE_PATH` normalization); `app/static/css/style.css`
(`.vjs-fullscreen-control` hide removed); `app/templates/watch.html` (player sizing
IIFE).

**Verification:** `create_app()` with `base_path: "/"` now yields `BASE_PATH = ""`
and `SVS_BASE + "/watch/1/comments"` = `"/watch/1/comments"` (single slash), matching
`url_for("watch.add_comment")` = `"/watch/1/comments"`. A logged-in test-client POST
to `/<id>/comments` with `X-Requested-With: XMLHttpRequest` returns `200` JSON
(`ok: true` plus the rendered `html`). `py_compile` clean on the touched files.

### 13.30 Remove the hardcoded `16/9` `aspect-ratio` so the player box tracks the video's intrinsic ratio (Session: 2026-09-25)

§13.29 item 3 assumed the inline JS `aspect-ratio` (from `videoWidth`/`videoHeight`)
overrides the CSS `16/9` fallback once metadata loads, leaving `16/9` only as a
pre-metadata placeholder. In practice a non-16:9 source still pillarboxed (black
bars left/right), i.e. the box stayed `16/9`.

**Root cause.** `app/static/css/style.css` declared `aspect-ratio: 16 / 9` on both
`#player` and `.video-js`. A `<video>` is a *replaced* element: with `width: 100%`
and no explicit height it would otherwise size its height from the media's
**intrinsic** aspect ratio as soon as metadata loads. A CSS `aspect-ratio`
declaration overrides that intrinsic sizing, so the box was pinned to `16/9` for
every source. The inline JS ratio only wins when it is actually applied; for the
`watch.stream` source that trigger did not reliably fire, so the box stayed on the
CSS `16/9` and pillarboxed. `video-js.min.css` was checked and sets neither
`aspect-ratio` nor a forced height on `.video-js`, so the 16:9 came only from our
own rules.

**Fix.** Drop the `aspect-ratio: 16 / 9` declaration from both `#player` and
`.video-js`, leaving `width: 100%` with no height. The `<video>` then fills its
height from the media's intrinsic ratio once metadata is available, so any
aspect ratio (wide, tall, square) fills the box with no left/right bars and the
`.player-wrap` container height follows the player — with no dependency on the JS
trigger. The inline JS ratio (§13.29) is now a harmless reinforcement of the same
value. Pre-metadata the box has no defined height for a brief moment (mitigated by
`preload="auto"`); the in-page fullscreen fill (`aspect-ratio: auto !important`)
is unaffected because it lives on a separate overlay element.

**Where:** `app/static/css/style.css` (`#player`, `.video-js`).

**Verification:** No headless browser is available, so this requires a live check —
open a non-16:9 video on the watch page and confirm there are no left/right black
bars and the container height matches the player. Computed style for `#player`
should now report `aspect-ratio: auto` (intrinsic) rather than `16 / 9`.

### 13.31 Force intrinsic-ratio height and strip video.js dynamic sizing so the box can never be pinned (Session: 2026-09-25)

§13.30 removed the CSS `aspect-ratio: 16/9`, leaving the box to the `<video>`'s
intrinsic ratio. A non-16:9 source still pillarboxed (black bars left/right), i.e.
the box height was not tracking the media's intrinsic ratio.

**Root cause.** Tracing the shipped `video.min.js`: `updateStyleEl_()` re-runs on
the tech's `loadedmetadata`. With `VIDEOJS_NO_DYNAMIC_STYLE` truthy it writes
`width`/`height` *attributes* on the `<video>` (only when those are numbers); when
falsy it adds a `player-dimensions` class and injects a
`vjs-styles-dimensions` `<style>` tag of the form `.<id>-dimensions { width:Npx;
height:Mpx }`. Either path can pin a fixed pixel height on the `<video>`, which
overrides its intrinsic-ratio sizing and pillarboxes non-16:9 sources. Our CSS had
`width: 100%` but relied on the implicit `height: auto`, which loses to any
video.js-injected height.

**Fix.** Make natural sizing bulletproof:
1. `app/static/css/style.css` `.video-js` now sets `height: auto !important`. A
   `<video>` is a replaced element, so `height: auto` derives the box height from
   the media's intrinsic ratio; `!important` defeats any height video.js injects
   via the dynamic style tag or a `height` attribute. The in-page overlay still
   wins: `.inpage-overlay__fill.video-js` sets `height: 100%` with a
   higher-specificity selector.
2. `app/templates/watch.html` `applyRatio()` (fired on `loadedmetadata`) now also
   removes the `player-dimensions` class and the `vjs-styles-dimensions` `<style>`
   element, so the box is never pinned even if `VIDEOJS_NO_DYNAMIC_STYLE` is
   somehow missed. The existing `aspect-ratio: <videoWidth>/<videoHeight>` inline
   style is retained as a redundant reinforcement of the same ratio (§13.29).

With `width: 100%` and `height: auto`, any aspect ratio (wide, tall, square)
fills the box with no left/right bars and `.player-wrap`'s height follows the
player.

**Where:** `app/static/css/style.css` (`.video-js`), `app/templates/watch.html`
(`applyRatio`).

**Verification:** No headless browser is available, so this requires a live check
— hard-refresh a non-16:9 video on the watch page and confirm there are no
left/right black bars and the container height matches the player. Computed style
for `#player` should report `height: auto` and `aspect-ratio` equal to the video's
`videoWidth / videoHeight`; the `player-dimensions` class and the
`vjs-styles-dimensions` `<style>` tag should be absent.

### 13.33 base_path login redirect, watch title, sidebar avatar, non-cached caching, and cloud-drive evaluation (Session: 2026-09-25)

**1. base_path login redirect.** When mounted under a subpath (e.g. `/video` behind a
reverse proxy), a protected page's `login_required` stored only the app-relative
`request.path` as the `next` target, so after login the user bounced to the domain
root (`/`) instead of the mounted path (`/video/...`). **Fix:** `app/auth.py` adds
`_full_path(path)` (prepends `request.script_root`, the recorded `SCRIPT_NAME`) and
`login_required`/`admin_required` now store `next=_full_path(request.path)`.
`_safe_next` already passes the full mounted path through; no change at the root.
**Where:** `app/auth.py`. **Verify:** `python testing/probe_base_path.py`.

**2. Watch-page title size.** The video title used the generic `h1` size. **Fix:**
`watch.html` adds a `.watch-title` class; `style.css` sizes it `1.6rem`/700.
**Where:** `app/templates/watch.html`, `app/static/css/style.css`.

**3. Sidebar avatar size.** The sidebar Account avatar was 20px. **Fix:**
`.sidebar__avatar` is now 28px, and the Account item's icon slot widens via
`.sidebar__item:has(.sidebar__avatar) .icon { flex: 0 0 28px }` so it is not clipped.
**Where:** `app/static/css/style.css`.

**4. Non-cached video caching flow.** A video whose local copy was evicted
(`local_filename` NULL) previously hard-failed with a 503 on the watch page. Now the
page offers to download it from Drive first. `watch.page` passes `is_cached` and
`has_drive` to the template; when not cached it renders a confirm panel (Cancel →
home; "Download & play" → start). `POST /watch/<id>/cache` starts a background
`pydrive2` download (via `drive.download_file`'s new progress callback) into a unique
local file, keyed by video id in an in-process store; on success it sets
`local_filename` and enforces the cache cap (protecting the new file).
`GET /watch/<id>/cache/status` returns `{cached, active, transferred, total, progress,
error}` for polling; the page shows a progress bar and swaps in the player when
`cached`. Reloads mid-download auto-resume (`active`).
**Where:** `app/routes/watch.py`, `app/drive.py`, `app/templates/watch.html`,
`app/static/css/style.css`. **Verify:** `python testing/probe_cache_flow.py`
(fakes the Drive download; drives page→confirm→start→poll→cached→player plus the
missing-Drive and already-cached edge cases).

**5. Cloud-drive homepage / file manager — EVALUATION (pending approval, NOT started).**
Proposal: a Drive-native file/folder browser on the homepage (tree UI, right-click
menus, create/rename/move/delete folders, permissions) that replaces "My Videos".

**Difficulty: HIGH.**
- **New Drive API surface.** `drive.py` only uploads/downloads/deletes by file ID —
  there is no listing. This needs new methods: `files().list()` (paginate,
  `'<id>' in parents and trashed=false`, include folders), create folder, patch
  folder name (rename), move (change `parents`), and folder delete — all wrapped in the
  existing auth/quota handling.
- **Two conflicting data models.** Folders today are a *local* SQLite `folders` tree
  (per-user, `owner_id`) that is *not* synced to Drive; every video lives flat in one
  `DRIVE_FOLDER_ID` under a hashed `drive_filename`. A Drive-native browser would show
  the real Drive tree, whose files carry no title/cover/comments/owner — so it must
  reconcile Drive files against the `videos` table (show only recognized files, or
  render foreign files as "import this file").
- **Ownership / permissions.** The app is single-owner-per-video with no multi-user
  sharing; Drive permissions are per-file ACLs. A shared Drive view needs a policy for
  who can see/move/delete what that does not map cleanly onto the current model.
- **Replaces existing UI.** "My Videos" (`user.py`/`user.html`) already does local
  folder create/rename/delete, move, retry, and delete. The new view must absorb or
  deprecate all of that plus the upload-target selection.

**Rough effort:** Drive list + create/rename/move/delete (~1 day); folder-tree +
file-grid view with right-click menus and AJAX endpoints (~2–3 days); Drive↔`videos`
reconciliation + import of foreign files (~1–2 days); ownership/permissions policy +
UI (~1–2 days); testing (shared files, trashed, quota, partial auth) (~1 day).
**≈ 1.5–2 weeks** for a solid first version.

**Recommended next step:** Decide the data model first — (a) keep local folders and add
a "sync to Drive" concept, or (b) make Drive the source of truth and migrate local
folders into real Drive folders. That choice drives the whole design and should be
scoped in its own session before implementation.

### 13.34 Fix upload-progress stall, in-page fullscreen toolbar, and home folder cell size (Session: 2026-09-25)

Three items from `testing/bugs.md`:

**1. My Videos upload progress stalled at ~98–99%.** `DriveUploadWorker._run`
never dropped the in-memory progress entry on success (only `_fail` left one, and
`finally` discards `_in_flight`). The My Videos poller treats `p === undefined`
from `GET /user/progress` as "finished -> ready" and reloads to the `ready` badge,
so the bar sat at the last committed chunk forever. A `_drop()` helper now removes
the progress entry at the end of the success path (after the cache-cap enforce), so
the poller detects the terminal state and reloads. Failure still keeps its entry
(with `error`), so the `failed` badge + Retry still appear.
**Where:** `app/drive_worker.py` (`_run` success path, new `_drop`).

**2. In-page fullscreen had no playback toolbar.** When the video.js player is moved
into the `.inpage-overlay`, its control bar auto-hides (`vjs-user-inactive`) and
stays hidden, leaving only the top-right exit button. A rule forces
`.vjs-control-bar` visible while the player lives in the overlay, so the progress
bar / playback toolbar is always available (bilibili-style). It leaves on exit,
restoring normal auto-hide.
**Where:** `app/static/css/style.css` (`.inpage-overlay .vjs-control-bar`).

**3. Home folder cards were smaller than video cells.** Folders rendered in a
separate compact `.folders` strip. They now render inside the `.waterfall` as
full-size cells the same size as a video card: a 16:9 cover area holding a folder
glyph, with the folder name as the title. The old compact markup/CSS
(`folder-card__icon`/`__name`/`__count` and the `.folders` strip) is removed.
**Where:** `app/templates/home.html` (folders moved into `.waterfall`);
`app/static/css/style.css` (`.folder-card__cover`/`__title`).

**Verification:** `py_compile` + `pyright` clean on `drive_worker.py`; `probe_cache_flow.py`
still passes; a render probe confirmed home renders `class="card folder-card"` cells with
the folder name and no leftover compact markup. In-page fullscreen and the ~100%
upload completion need a live browser pass (no headless browser available).


### 13.35 Admin log viewer with level filtering (Session: 2026-09-25)

The admin page gained a **Logs** panel that surfaces the rotating log files
(§13.13) without leaving the browser, so an admin can read server activity and
the browser upload pipeline from the settings interface.

**1. Endpoint.** `GET /admin/logs?source=app|client&level=<LEVEL>` (admin-only)
returns JSON `{source, level, total, truncated, lines}` where `lines` is a list
of `{level, text}` for the newest matching lines (capped at 2000, newest last).
`source` selects `app.log` (server + client, the full picture) or `client.log`
(browser pipeline only). `level` is `all` or a logging level name; a line with no
level token (e.g. a traceback continuation line) inherits the level of the
record it belongs to, so tracebacks stay attached when filtering. Unknown
`source`/`level` fall back to `app`/`all`. The log directory is exposed from
`app/log.py` as `LOG_DIR`.

**2. UI.** A new `.admin-log` panel sits under Settings: a *Source* select
(server / browser pipeline), a *Level* select (All / INFO / WARNING / ERROR /
CRITICAL), a *Refresh* button, a status line, and a scrollable monospace box
that colors each line by level (reusing the `--log-*` theme vars). An inline
script fetches on load and on any control change and renders the result.

**Where:** `app/log.py` (`LOG_DIR`); `app/routes/admin.py` (`_LOG_FILES`,
`_LOG_LEVELS`, `_LOG_LINE_RE`, `_MAX_LOG_LINES`, `_read_filtered_log`, `logs`);
`app/templates/admin.html` (Logs panel + inline script); `app/static/css/style.css`
(`.admin-log-controls`, `.admin-log`).

**Verification:** `py_compile` + `pyright` clean; a test-client probe hit
`/admin/logs` for `all`, `ERROR`, `WARNING`, `client`/`INFO`, and bogus values,
confirming correct filtering, the 2000-line cap, and a 403 for non-admins.


### 13.36 Nine open `bugs.md` items: in-page hover toolbar, home card date/title, codec dropdowns, selectable max FPS, spacebar, full-width preview, hashed local names, sidebar collapse flash (Session: 2026-09-25)

Nine unstarted (`- `) items from `testing/bugs.md`, all resolved:

**1. In-page fullscreen: hover control bar.** Forcing the video.js control bar
visible left only the top-right exit button in in-page mode. `inpage.js` now
builds its own bilibili-style bottom bar — play/pause, a seekable progress
track, and a `current / total` time readout — that appears on mouse-move and
auto-hides after 3 s. It is shown only for players without native controls (a
`video-js` element); a plain `<video controls>` (the upload preview) keeps its
native controls, so the custom bar stays hidden there. The old
`.inpage-overlay .vjs-control-bar` rule is removed and the video.js bar is hidden
while the custom one is active (`.inpage-overlay--custom`) so they never overlap.

**2. Home card upload date.** Each video cell's meta row now shows the upload date
(`uploaded_at[:10]`) right-aligned on the same line as the uploader name
(`.card__date { flex: 0 0 auto }` after a shrinkable `.card__owner`).

**3. Hashed local filenames.** `_unique_video_name` (upload) and
`_cached_filename` (watch cache) now return a short SHA-256 digest
(`{16 hex}.mp4`) mixed with a per-file uuid — the same style as the Drive name —
so same-title videos never collide on disk; the human title stays in the DB.

**4. Admin codec dropdowns.** Default codec and Fallback codec are now `<select>`
(AV1 / H.264) instead of free-text inputs.

**5. Selectable max FPS.** The upload page has a *Max FPS* number field (≤ 60);
`getMaxFps()` feeds the transcode decision (`needsTranscode`, the direct-pass log,
and `capFps`) in place of the hard-coded constant, so a 60 fps source can be
capped lower per upload.

**6. Home card title on one line.** `.card__title` is now single-line with an ellipsis.

**7. Spacebar on the upload preview.** A `keydown` handler toggles play/pause on
the preview when Space is pressed (only when a file is loaded and the user isn't
typing in a field), instead of scrolling the page.

**8. Full-width upload preview.** `.upload-page` no longer caps at 720 px, and
`.upload-timeline__video` height cap went from 420 px to 75 vh, so the preview
spans the content width.

**9. Sidebar collapse flash.** The `sidebar-collapsed` class is now applied to
`<html>` in `<head>` (pre-paint) and toggled on `documentElement`, and the CSS
keyed off `.sidebar-collapsed` (not `body.sidebar-collapsed`), so a full page
load no longer flashes the expanded rail before collapsing it.

**Where:** `app/static/js/inpage.js` (control bar); `app/templates/home.html`,
`app/templates/admin.html`, `app/templates/upload.html`, `app/templates/base.html`;
`app/routes/upload.py` (`_unique_video_name`), `app/routes/watch.py`
(`_cached_filename`); `app/static/js/upload.js` (`getMaxFps`, Spacebar handler);
`app/static/css/style.css` (`.card__title` / `.card__date` / `.card__owner`,
`.upload-page` / `.upload-timeline__video` / `.upload-input--narrow`,
`.inpage-overlay__controls*`, `.sidebar-collapsed`).

**Verification:** `py_compile` + `pyright` clean on the touched Python; a
test-client render with a logged-in admin confirmed `/` (date), `/upload/`
(max-FPS field), and `/admin/` (codec selects) all return 200 with the new
markup; `_unique_video_name` and `_cached_filename` return distinct 16-hex `.mp4`
names; all four edited templates parse. Live browser checks (hover bar, spacebar,
### 13.37 In-page fullscreen: fix the missing custom control bar (Session: 2026-09-25)

Follow-up to §13.36 item 1, which was marked resolved but the bar still never
appeared for the watch-page player (only the top-right exit button showed).
Root cause: `useCustomBar` was computed from the *found* `<video>` element, but
the watch page runs video.js in **standard mode** — the expanded element is a
`<div class="video-js">` wrapper and the tech `<video>` (a child) carries no
`video-js` class. So `v.classList.contains('video-js')` was false, the custom bar
was never revealed, and the native (hidden) bar was the only candidate.

Fixes in `app/static/js/inpage.js`:
- Detect the player on the expanded element **or** the found `<video>`
  (`el.classList.contains('video-js') || v.classList.contains('video-js')`), so
  both tag-reuse and standard-mode video.js are covered.
- Reveal the bar immediately on entry (`expand()` now calls `showBar()`), instead
  of waiting for the first mouse-move.
- `showBar()` now schedules the 3 s idle hide (previously it only *cleared* the
  timer and the bar hid solely on `mouseleave`), so the bar follows mouse
  activity and auto-hides while idle — the bilibili behaviour §13.36 described.

Also added a self-maintaining cache-buster to rule out a stale cached `inpage.js`:
`app/__init__.py` computes a content hash of `inpage.js` + `style.css` once at
startup (`STATIC_VERSION`) and exposes it to templates as `static_version`;
`base.html` appends `?v={{ static_version }}` to those two static references.

**Where:** `app/static/js/inpage.js` (`showBar`, `expand`); `app/__init__.py`
(`import hashlib`, `_static_version`, `static_version` context var);
`app/templates/base.html` (cache-buster on `inpage.js` / `style.css`).

**Verification:** `py_compile` clean on `__init__.py`; `create_app()` computes
`STATIC_VERSION` and a test-client `GET /` (200) renders `base.html` with
`css/style.css?v=` and `js/inpage.js?v=` present. The live bar (appears on entry,
seeks, play/pause, auto-hides on idle, no overlap with the exit button) is the
remaining manual browser check.
### 13.38 Home card date+time, folder-card height, upload-preview Spacebar (Session: 2026-09-25)

Three remaining `$` items from `testing/bugs.md`:

**1. Home card upload date+time.** The video cell's meta row now shows the upload
date *and* time (`uploaded_at[:16]` → `YYYY-MM-DD HH:MM`) instead of date only
(`[:10]`). `uploaded_at` is `datetime('now')`, so `HH:MM` is the hour/minute of
upload.

**2. Folder card matches a video cell's height.** A video cell is cover + title
(`.card__title`, `padding: 8px 10px 0`) + a meta row (20px avatar + `.card__meta`
`padding: 0 10px 8px` ≈ 28px). A folder card was cover + title only
(`.folder-card__title`, `padding: 8px 10px`), ~28px shorter. `.folder-card__title`
now uses `padding: 8px 10px 28px` (absorbing the meta-row height) and a larger
`font-size: 1rem`, so a folder cell is the same height as a video cell with a
prominent folder name.

**3. Spacebar pause/resume on the upload preview.** The preview is
`<video controls>`, so while it has focus the *native* element also toggles
play/pause on Space, double-toggling with the JS handler (pause, then instantly
resume). The `keydown` handler now blurs the preview before toggling
(`document.activeElement === previewVideo` → `previewVideo.blur()`), so only the
JS handler toggles the state.

**Where:** `app/templates/home.html` (`card__date`); `app/static/css/style.css`
(`.folder-card__title`); `app/static/js/upload.js` (Spacebar handler).

**Verification:** `home.html` parses as valid Jinja; `uploaded_at[:16]` yields
`YYYY-MM-DD HH:MM`; `upload.js` braces balanced. The visual height match and the
spacebar toggle are the remaining manual browser checks.

## 13.39 — Watch page player: DPlayer replaces video.js; native fullscreen
- The watch page now uses [DPlayer](https://dplayer.js.org/) 1.27.1, vendored at
  `app/static/vendor/dplayer/DPlayer.min.js` (a single self-contained file: it
  injects its own CSS and inline SVG icons, so no separate stylesheet or font is
  required). `video.min.js` + `video-js.min.css` are no longer loaded on the
  watch page.
- `watch.stream` serves the MP4 via `send_file(conditional=True)` (§13.33), which
  supports HTTP Range requests, so DPlayer's plain HTML5 `<video>` can play it
  with working seeking (no custom in-page overlay or MSE is needed).
- DPlayer renders `<div id="dplayer">` (inside `.player-wrap`, which carries the
  16:9 aspect-ratio fallback and is updated to the exact ratio on
  `loadedmetadata`) with its own control bar and **native fullscreen**
  (`requestFullscreen` on the container), replacing the old custom in-page
  fullscreen overlay on the watch page. The `.svs-inpage-fs` button and its CSS
  are removed. DPlayer receives a poster (`video.pic`) from `home.cover`
  (`video['cover_filename']`).
- Cache-download flow (§13.33) is unchanged in behavior: when the video is not
  cached the wrapper is hidden and the player is built **once** from the polling
  IIFE's `done()` after the Drive download completes
  (`window.initWatchPlayer()`); when already cached it is built at page load.
  `initWatchPlayer` is idempotent (guarded by the `.dplayer` class).
  `switchVideo()` is **not** used — this is a one-time "becomes cached"
  transition, not a segment switch, so the player is never rebuilt during the
  page's life.
- `inpage.js` is retained globally because the **upload page** still uses it for
  its clip-timeline in-page expand (`data-inpage-expand="#clip-timeline"`); only
  the watch page's use of it was removed.
- With video.js gone, the video.js-specific dead code was stripped from the
  shared `inpage.js` / `style.css`. `inpage.js` no longer builds its custom bottom
  control bar (that `useCustomBar` detection only ever matched a `.video-js`
  element, and the only `inpage` target left is the upload page's native
  `<video controls>`); it is now a minimal move-in / move-out overlay
  (expand / exit / Esc / on-screen exit button). The matching CSS was removed:
  the `.inpage-overlay__controls` / `__play` / `__progress` / `__time` block,
  `.inpage-overlay--custom .vjs-control-bar`, and the `.video-js` selector rules
  (the `.inpage-overlay__fill:not(.upload-timeline) video` rule is kept for any
  native `<video>` expand target). A whole-project grep for `videojs` / `video-js`
  / `vjs-` returns nothing.

## 13.40 — Sidebar icons & avatar: proportional (rem) sizing (Session: 2026-09-25)
- The whole rem-based UI scales with the viewport via `html { font-size:
  clamp(13px, 0.2vw + 12px, 22px) }` (§13.26), but the sidebar nav icons and the
  account avatar were hard-coded in **pixels**, so they stayed small on
  high-resolution (e.g. 2K) screens while everything else grew.
- **Fix:** convert every fixed-pixel sidebar glyph/avatar size to `rem` (16 px
  base reference) so it rides the same proportional scale:
  `.sidebar__item .icon` / `.sidebar__theme-toggle .icon` slot `20px -> 1.25rem`
  and their `svg` `19px -> 1.1875rem`; `.sidebar__toggle svg` `18px -> 1.125rem`;
  `.sidebar__avatar` `28px -> 1.75rem`; and the account icon-slot override
  `.sidebar__item:has(.sidebar__avatar) .icon` `28px -> 1.75rem`.
- **Where:** `app/static/css/style.css` (sidebar block, §13.26 area).

## 13.41 — Live streaming page (list + view + admin) (Session: 2026-09-25)
- New **Live** section, reached from a sidebar item added under *Home*. Two
  layers:
  1. **List** — `GET /live` (blueprint `live`, `url_prefix="/live"`): a gallery
     of every live room. Each card shows the room cover (placeholder when none),
     the title, and an "ON AIR" badge. The badge is probed once, server-side,
     when the page renders (§14.7): the room's stream must deliver a real
     video/audio FLV tag within 6 s, and refreshing `/live` re-probes. (The
     original client-side `200/206` ranged-fetch probe was unreliable — see
     §14.7.)
  2. **View** — `GET /live/<code>`: the live player page, URL
     `[base_path]/live/<code>` (e.g. `[base_path]/live/VSakuya`). It renders
     `.player-wrap > #dplayer` and wires **DPlayer** (vendored, §13.39) to a
     **flv.js** player (`isLive: true`) via DPlayer's `customFlv` type — the
     same integration as the reference `index.html`, adapted to the app. DPlayer
     is created with `live: true` (live UI: no seek bar, "LIVE" label) and
     autoplay; the **progress chaser** from `index.html` is preserved (a 3 s
     `setInterval` that, while playing, jumps the `<video>` back to
     `buffered.end(0) - 0.5` whenever it falls more than 5 s behind the live
     edge), plus the mobile volume patch.
- **flv.js is vendored** at `app/static/vendor/flvjs/flv.min.js` (1.6.2, UMD,
  exposes the `flvjs` global) so the live page has no CDN dependency — matching
  the vendored-DPlayer approach (§13.39).
- **Data model.** A `live_rooms` table is added to `sql/schema.sql`
  (`id`, `code` UNIQUE, `title`, `cover_filename`, `created_at`). `db.init_db()`
  seeds a default room `code="VSakuya"` when the table is empty. New CRUD in
  `db.py`: `list_live_rooms`, `get_live_room`, `get_live_room_by_code`,
  `create_live_room`, `update_live_room`, `delete_live_room`.
- **Admin management.** A "Live rooms" panel in `admin.html`: an *add* form
  (code + title + cover) and one card per room with an editable title, a cover
  file picker, a Save button (`POST /admin/live-rooms/<id>`) and a Delete button
  (`POST /admin/live-rooms/<id>/delete`). Cover uploads are stored under
  `covers/` via `storage.save_cover` and served by the existing `home.cover`
  route; deleting a room removes its cover file.
- **Stream URL convention.** The flv stream is served by the media server behind
  the reverse proxy at the **domain root** `/live/<code>.flv` (Node-Media-Server),
  independent of the app's mount base. The live *page* lives under the base at
  `[base_path]/live/<code>`. Both the player and the list probe request
  `/live/<code>.flv` (root-absolute).

## 14 — Cross-cutting hardening: SQL files, UUID storage, 24h time, layout, live links (Session: 2026-09-25)

This session addresses six new requirements from `testing/bugs.md`.

### 14.1 No hard-coded SQL — every query lives in `app/sql/`
- `db.py` no longer contains SQL literals. Each named query is a `.sql` file under
  `app/sql/<feature>/<name>.sql` (features: `users`, `login_attempts`, `folders`,
  `videos`, `comments`, `settings`, `live_rooms`; conditional DDL for old databases
  lives in `app/sql/migrations/`). `db._sql(feature, name)` reads + caches the file.
- Dynamic queries keep a `{placeholder}` token that Python substitutes with a
  caller-controlled value: `users/update.sql` and `videos/update.sql` use
  `{set_clause}` (built from the caller's kwargs); `login_attempts/count_in_window.sql`
  uses `{column}` (whitelisted to `username`/`ip`).
- `PRAGMA` statements (`foreign_keys`, `table_info`) stay inline — they are
  connection setup / introspection, not CRUD.

### 14.2 Every stored file uses a UUID name
- Avatars were saved under the client's *original* filename; they now go through
  `storage.save_avatar` (UUID stem + image extension), mirroring `save_cover`. The
  previous avatar file is removed on replacement (best effort). Videos already use a
  `(title|uuid)` SHA-256 stem and covers use `cover_<uuid>`; the avatar was the only
  remaining original-name offender.

### 14.3 All displayed times use a 24-hour clock
- The only JS time render (`refreshTimes` in `watch.html`, comment timestamps) used
  `toLocaleString()`, which follows the OS locale and can be 12h. It now passes
  `{ hour12: false }`. All server-rendered times already slice `datetime('now')`
  to `YYYY-MM-DD HH:MM` (24h).

### 14.4 Content width aligned to Home / Upload
- Every page is wrapped by `base.html` in `.main > .container` (max-width 92rem).
  Home and Upload already fill that column. The `.page` wrapper (Admin, My Videos,
  Account) capped content at 900px, so it was narrower. `.page` no longer constrains
  width, so all pages share the 92rem content column. The deliberately narrow
  `.auth-card` (Login / Change password) is left unchanged.

### 14.5 Home content fills the page height
- `.main` is now a full-height flex column and `.container` grows to fill it, so a
  short listing (few / no videos) no longer leaves a visibly separate band at the
  bottom of the page.

### 14.6 Live rooms: stream link instead of code; cover like a video; no "VSakuya"
- `live_rooms.code` (UNIQUE) is replaced by `live_rooms.url` — a full stream link
  (an internal `/live/<code>.flv` or any external URL). The live *page* is now
  `GET /live/<id>` (room id) instead of `/live/<code>`. The ON AIR probe and the
  DPlayer / flv.js player both use `room.url` directly.
- Migration: `code` is copied into `url` as `/live/<code>.flv`, then the column is
  dropped (table rebuild). The seeded default room is a generic example (no
  "VSakuya", which is private info).
- Admin "Live rooms" now sets the **link** + title + cover; the cover picker mirrors
  the video cover flow — choosing a file opens a shared Cropper.js dialog
  (16:9, exported at 1280×720 JPEG) whose result is attached to the form's
  `cover` input, with a live preview (the add form shows a small preview; each
  room card previews into its own cover tile). The placeholder/example no longer
  mentions "VSakuya".

### 14.7 ON AIR badge: server-side FLV probe (was: client-side status check)
- The old client-side probe did a ranged `fetch()` on `room.url` and treated
  `200/206` as on air. Two failures: the stream path sat behind Apache Basic
  auth (the proxy now uses a Referer/User-Agent guard instead — see the last
  bullet), so the browser fetch always got `401` (the badge could never light up,
  and the fetch popped the native auth dialog); and the media server itself
  answers `200` + FLV header **immediately for a non-live room** and then
  stalls (recorded 2026-09-25 with the stream credentials: `GET
  https://vsakuya.xyz/live/VSakuya.flv` → `200 OK`, `Content-Type:
  video/x-flv`, then only the 9-byte FLV header plus the 4-byte
  `PreviousTagSize0` — 13 bytes total in 20 s, no FLV tags at all). HTTP
  status alone cannot distinguish live from non-live.
- The probe now runs **server-side, once per page load**: when `GET /live`
  renders, every room is probed in parallel and the result is baked straight
  into the badge HTML — there is no `/live/status` endpoint and no client-side
  polling. A room is ON AIR only when a video (FLV tag type 1) or audio (type 8)
  tag arrives within 6 s; 4xx/5xx, a missing `FLV` signature, a stalled/closed
  stream or a timeout are all off air. The badge reflects the state at the
  moment the page loads, so refreshing `/live` re-probes; it no longer carries
  the stream URL.
- Correct FLV framing matters (a bug found during live validation): after the
  9-byte header comes a 4-byte `PreviousTagSize0` *before* the first tag, and
  every non-media tag (e.g. the `onMetaData` script tag, type 18) is followed
  by its own 4-byte `PreviousTagSize`. The probe must consume both, or the tag
  walk desyncs and real media tags are misread. The whole walk is also bounded
  by a total 6 s deadline (not just a per-read socket timeout), so a live
  stream that keeps sending non-media data can never loop forever.
- The `.flv` guard on the reverse proxy (see the Apache vhost config) admits a
  stream request only when it carries a Referer from this site's player page
  or the probe's User-Agent (`simple-video-share/live-probe`, sent by
  `app/live_probe.py`) — the old Apache Basic-auth protection was replaced by
  that guard, so the probe no longer needs (or sends) stream credentials. The
  temporary `_STREAM_USER`/`_STREAM_PASSWORD` constants in
  `app/routes/live.py` are dead code and are removed by the 2026-10-06 code
  review (`documents/code_review_2026-10-06_155159.md`). A room's `url` may be a full
  URL or a root-relative path (resolved against the request host). Room 1's URL
  was set to the full address `https://vsakuya.xyz/live/VSakuya.flv`.

### 14.8 Per-user live rooms: self-service management + owner in list/view
- Live rooms are now **owned by a user** (`live_rooms.owner_id`, one room per
  user) and carry a `description` (the "直播详情"). A fresh DB adds both columns via
  `schema.sql`; an existing DB back-fills them with the additive migrations
  `migrations/live_rooms_add_description.sql` and `migrations/live_rooms_add_owner_id.sql`
  (invoked from `_migrate`). Pre-existing rooms keep `owner_id = NULL` (ownerless —
  the admin can delete them, but no user can edit them).
- **Self-service (the user-detail / account page).** A "Live room" panel is added to
  `account.html`. A user with no room sees a *create* form; a user who already has
  one sees an *edit* form. Both post to `POST /live/room`, a new login-required route
  `live.manage_room` that upserts by `owner_id` (create on first submission, update
  after). Fields: stream link, title, description, and an optional 16:9 cover — the
  same Cropper.js flow as video covers (the account page reuses the
  `liveroom-cover-input` / `liveroom-cover-btn` wiring from `base.html`).
  `_valid_live_url` (a full URL or a `/…` path) now lives in `app/routes/live.py`;
  the cover is stored under `covers/` via `storage.save_cover`, and replacing a cover
  deletes the old file.
- **Admin is delete-only.** The admin "Live Rooms" panel no longer creates or edits
  rooms — each card shows the room's cover, title and owner with only a Delete button
  (`POST /admin/live-rooms/<id>/delete`). The admin create/edit routes and its
  `_valid_live_url` helper were removed from `app/routes/admin.py`.
- **Owner surfaced in the list and the player.** `list_live_rooms` and
  `get_live_room` now `LEFT JOIN users` (SQL in `sql/live_rooms/list.sql` /
  `get_by_id.sql`) and return `owner_username` + `owner_avatar`. The live list
  (`live_list.html`) renders each room like a home-page video cell — 16:9 cover with
  the ON AIR badge, the title, then an avatar + username meta row. The live player
  page (`live_view.html`) shows the title on top, then the streamer's avatar +
  username, then the room description, above the DPlayer.
- New `db` helpers: `get_live_room_by_owner(owner_id)`; `create_live_room` and
  `update_live_room` accept `owner_id` / `description`.

### 14.9 ON AIR badge: background probe (the list page renders instantly)
- The §14.7 server-side probe used to run **during** `GET /live` render: every room
  was probed in parallel (up to 6 s each, §14.7 `PROBE_TIMEOUT`) before the HTML
  was returned, so the list page stayed blank for up to ~6 s on first load. The
  probe is now moved off the critical path.
- `GET /live` (`live.index`) renders **instantly** with every ON AIR badge off
  (`on_air={}`) and no longer probes.
- New endpoint `GET /live/onair` (`live.onair`, login-required) runs the same
  parallel server-side probe and returns JSON `{room_id: is_on_air}`. The probe
  stays **server-side** (§14.7) because the stream server sits behind Apache Basic
  auth — a browser `fetch(room.url)` gets `401` and pops the native auth dialog.
- `live_list.html` gives each card `data-room-id`; after the page loads, an inline
  script fetches `url_for("live.onair")` and adds `.is-onair` to the badges of the
  rooms that are live. A badge is off until the fetch resolves, so an on-air room
  lights up a few seconds after the page appears; a failed fetch leaves the badges
  off (the next page load re-probes). There is still no client-side polling of the
  stream.

**Where:** `app/routes/live.py` (`_on_air_map`, `index`, `onair`, `import jsonify`);
`app/templates/live_list.html` (`data-room-id` + the background fetch script).
`app/live_probe.py` is unchanged.

**Verification:** `py_compile` clean; `create_app()` + test client: `GET /live`
(200) returns instantly with every badge off and each card carrying `data-room-id`;
`GET /live/onair` (200) returns a JSON object of `room_id → bool` for every room.
Lighting the badge on a live stream needs a browser + pushing stream to confirm.

## 15 — Upload/edit page split, queued upload, CBR, home time, admin 404, remaining space (Session: 2026-09-26)

This session resolves several `testing/bugs.md` items.

### 15.1 Upload / edit page split + multi-file queued upload
- The old single-page upload is split into two pages under the same `upload` blueprint
  (`app/routes/upload.py`):
  - `GET /upload` (`upload.index`) — the **main upload** page (`upload.html` + `upload.js`):
    a **multi-file queue**. Files are processed sequentially (probe → transcode → cover →
    upload); the queue continues after a single-file failure. **No clip/trim UI.**
  - `GET /upload/edit` (`upload.edit`) — the **clip/edit** sub-page (`edit.html` + `edit.js`):
    a **single** video with preview + trim, then transcode. Links back to the main page.
- Both routes are `@login_required` and render with `folders` + `max_bitrate_kbps` (the admin
  "default bitrate" exposed as `window.SVS_MAX_BITRATE_KBPS`).
- **Shared pipeline.** The page-agnostic probe / process / cover / upload helpers were extracted
  into `app/static/js/pipeline.js`, driven by a page-supplied `ctx` object so the shared code
  never hard-codes page DOM. `upload.js` uses it for the queue. `edit.js` is kept as a
  self-contained copy (the clip flow) for now; refactoring it onto `pipeline.js` is a follow-up.

### 15.2 CBR target (post-encode re-encode guard removed)
- The MediaBunny transcode path pins the output to a constant-bitrate **target** instead of a
  two-pass ABR estimate: `Quality` uses `bitrate: opts.bitrateKbps * 1000` (bps) with
  `bitrateMode: "constant"`. WebCodecs CBR exposes only `bitrate` + `bitrateMode` (no hard
  `maxBitrate`), so "constant" is a **target**, not a guaranteed ceiling — hardware encoders
  (esp. AV1) routinely overshoot ~2× on complex content.
- A post-encode guard previously probed the real output bitrate and **re-encoded at a lower
  target** (geometric 0.6× back-off, up to 4 attempts) when the cap was exceeded. **That guard
  has been removed**: both `pipeline.js` and `edit.js` now call `runMediabunny` directly (the
  `runMediabunnyCapped` wrapper is gone). The encoded bitrate may therefore exceed the
  configured cap — the accepted trade-off (see §1). `probeVideo` still runs on the output but
  **only for stored metadata**; it no longer triggers a re-encode.

### 15.3 Home time mismatch (UTC → viewer-local)
- The home gallery renders `uploaded_at` as a UTC `data-ts` attribute and converts it to
  the viewer's **local** time on the client (`home.html`), so the date no longer depends on
  the server locale. This is a display fix, not a storage change (DB stays UTC).

### 15.4 Admin 404 log URL fix
- `admin.html` logged 404s with a doubled base-path prefix (`SVS_BASE` applied twice). The
  client now uses `url_for("admin.logs")` only, so the logged URL is correct both with and
  without `base_path` (verified for `/admin/logs` and `/video/admin/logs`).

### 15.5 Cache-based remaining space (home) vs real disk free (admin)
- **Home** shows **cache** semantics: bytes used / `storage.max_cache_bytes` cap / remaining.
  The "bytes used" figure is the **real on-disk size** (`storage.actual_cache_bytes`), which
  stats the actual cached files — a row whose `local_filename` is set but whose file is
  missing (e.g. a server migration that did not copy the media) counts as 0. This keeps the
  number honest after a migration instead of showing the phantom upload-time total. It is
  the local-cache budget, not total disk, and also shows the Google Drive remaining space
  (`Drive:` in the same strip).
- **Admin** shows **real disk free** space **and** the Google Drive account quota
  (`Drive free:`). The Google Drive remaining space is therefore shown on **both** pages;
  the two pages otherwise use different disk semantics (cache budget vs real disk free).
- The cache-capacity **enforcement** (`storage.enforce_cache_limit`) also runs off the real
  on-disk total, so a migration that left phantom DB rows never triggers needless evictions
  (and can never delete a real cached file to make room for a missing one).

### 15.6 Verification
- New `testing/smoke_upload_pages.py`: `/upload` (200, queue list, multi-file input, edit entry
  link, loads `upload.js`) and `/upload/edit` (200, clip panel, preview, back-link, loads
  `edit.js`, `SVS_MAX_BITRATE_KBPS` present). `testing/smoke_quick_fixes.py` re-verified the
  admin log URL (root + `base_path=/video`), home cache/`data-ts`, and admin disk-free text.
- `pyright` on the changed Python: 0 errors, 0 warnings.
- **Still pending (manual / browser):** queued-upload + transcode fallback + stored-metadata +
  output-bitrate verification; CBR overshoot check against an over-cap source; live-room checks.

### 15.7 Home page: data-first progressive covers (the gallery renders instantly)
- The home waterfall (`home.index`) and the live list (`live.index`) already return
  their HTML fast (the render cost is cached — `drive.get_drive_quota` 60 s,
  `drive.exists` 120 s), but the **perceived** load is slow: covers are large
  1280×720 JPEGs (500–800 KB each) and the home cells had **no fixed aspect ratio**,
  so each cover arrives at a different time and the waterfall **reflows** as they
  land. The page is made *perceived*-instant with a data-first cover strategy.
- **Fixed 16:9 box.** `.card__cover` now carries `aspect-ratio: 16/9` +
  `object-fit: cover` (previously only `.live-card .card__cover` had this; home
  video cells had none), so every cell has a definite box before its cover loads
  and the layout never shifts. The redundant `.live-card .card__cover` override is
  removed.
- **Generic placeholder on first paint.** `.card__cover` shows a neutral,
  theme-aware `var(--panel-2)` background until its cover is decoded — the
  "generic cover" the user sees the instant the data (title / owner / date) is
  visible, not when the image arrives.
- **Fade-in on load.** Covers start at `opacity: 0`; the card adds `.is-loaded`
  (`opacity: 1`, 0.3 s) via `onload` on the `<img>`, so each cover fades in as it
  downloads. A cover that fails to load simply stays on the placeholder (no broken
  icon).
- **Lazy + async decode.** The existing `loading="lazy"` is kept (off-screen covers
  don't download until scrolled near); `decoding="async"` is added so the large
  covers decode off the main thread without jank.
- **Scope:** purely client-side (CSS + the two cover `<img>` tags). No Python, no
  new asset, no new JS file. The same change applies to the live list for
  consistency (identical `.card__cover` pattern).

**Where:** `app/static/css/style.css` (`.card__cover` base rule + `.is-loaded`;
drop `.live-card .card__cover`); `app/templates/home.html` and
`app/templates/live_list.html` (cover `<img>` gains `decoding="async"` +
`onload="this.classList.add('is-loaded')"`).

**Verification:** rendered HTML (test client) shows every cover carrying
`decoding="async"` + the `onload` handler; `.card__cover` computes `aspect-ratio:
16/9` and a `--panel-2` background. Browser check: cells paint as placeholders
instantly and covers fade in with no layout shift.

### 15.8 Home page: do not re-time-out on every load when Drive is unreachable
- `home.index` drops videos that have no local copy by calling `drive.exists(id)`
  **serially** for each of them, and `_render_gallery` then calls
  `drive.get_drive_quota()`. All of these are synchronous Google Drive calls on the
  request path.
- When Drive is reachable these are cheap (cached 120 s / 60 s, §15.7). But when
  Drive is **unreachable** (VPN down, firewall, network cut), each `exists` call
  hits an OS-level connect timeout (`WinError 10060`, several seconds each) and
  returns `None` (unknown). `None` was previously **not** cached (only definitive
  `True`/`False` were), so **every** home load re-ran all the timeouts serially and
  the page hung for `(N no-local-copy videos + 1) × timeout` — the browser spinner
  never resolved. This is a render-path dependency on Drive reachability, not a 500.
- **Fix:** `drive.exists` now caches the `None`/unknown result too, for a short TTL
  (`_EXISTS_UNKNOWN_TTL_SECONDS = 30 s`), separate from the definitive
  `_EXISTS_TTL_SECONDS = 120 s`. While Drive is down the first load pays the
  timeouts once and subsequent loads within the window render from cache; the
  keep-on-unknown semantic (a `None` never hides a video) is unchanged, and
  `home.py` still only hides a video on a definitive `False`.
- **Scope / tradeoff:** the first load after Drive goes down — or after a server
  reload, which clears the in-memory cache — still pays one round of timeouts,
  because the answer is genuinely unknown until checked. §15.9 removes the
  *serial* part of that cost (the per-video `exists` calls now fan out in
  parallel), so the first-load latency is one round of timeouts, not N.

### 15.9 Home page: Drive calls run concurrently (no more serial per-video stalls)
- The home page needs one Drive `about` (quota) read plus one `files().get`
  existence check per **non-cached** video. Before this change those were issued
  **serially** on the request path (`home._drop_missing` looped over every
  non-cached video calling `drive.exists`, then `_render_gallery` called
  `drive.get_drive_quota`). After a server migration — where *nothing* is locally
  cached — the first home load fired `(N + 1)` sequential Google Drive round trips
  and the page stalled for minutes.
- **Fix:** `home._resolve_drive` now does all of that in a single
  `concurrent.futures.ThreadPoolExecutor` (capped at `_DRIVE_FANOUT_WORKERS = 8`):
  the quota read and every `drive.exists` are submitted together and the page
  waits for the slowest, so the whole Drive step costs ~one network round trip
  regardless of how many videos are non-cached. `drive.get_drive_quota` (60 s)
  and `drive.exists` (120 s / 30 s unknown) keep their per-file caching, so a
  warm load still issues zero API calls.
- **Semantics unchanged:** a video is hidden only on a definitive `False`
  (Drive file gone); `None` (API unreachable) keeps it, and a local file present
  on disk is kept as cached without any Drive call. `drive_free` (the `Drive:`
  figure) is returned alongside the kept list.
- **Why a thread pool (not `asyncio`):** the app is a synchronous Flask/Werkzeug
  app and the Drive client (`pydrive2`/`googleapiclient`) is blocking, so the
  only correct "async" here is running the blocking calls on worker threads.
- **Why the fan-out is safe (per-thread Drive client):** the `pydrive2`/
  `googleapiclient` client and its `httplib2`/OpenSSL transport are **not
  thread-safe** — sharing one client across the pool's threads corrupts the SSL
  state (`[SSL: RECORD_LAYER_FAILURE]`) and segfaults the process. `drive.get_drive()`
  therefore returns a **per-thread** client (stored in `threading.local`), so each
  worker runs its blocking call on its own client and no SSL state is shared. This
  also removes the same latent hazard on the serial paths (uploads, admin, quota)
  that already run on different Waitress threads. The shared caches
  (`_exists_cache`, `_quota_cache`) stay process-wide: they hold immutable tuples
  and a redundant cold-cache fetch is harmless (it only re-confirms the same value).

**Where:** `app/routes/home.py` (`index`, `folder`, `_render_gallery`, and the new
`_resolve_drive`, replacing `_drop_missing`).
**Verification:** `create_app()` builds (8 blueprints, 66 routes). Isolated check
with `drive.exists`/`get_drive_quota` mocked: a cached video short-circuits
(no `exists` call), a `False` is dropped, `True`/`None` are kept with
`is_cached` set, `drive_free` is propagated, and `exists` is called only for the
non-cached, Drive-referenced videos.

**Where:** `app/drive.py` (`get_drive()` now serves a per-thread client from
`threading.local` instead of a process-wide `_drive`; `_reset_for_tests` clears the
current thread's client).
**Verification:** with `drive._build_drive` mocked, eight threads each get a
distinct client and each thread reuses its own client on repeated `get_drive()`
calls (verified in isolation; this is what keeps the §15.9 fan-out from sharing an
`httplib2`/OpenSSL transport and segfaulting).

**Where:** `app/drive.py` (`_exists_cache` value type → `Optional[bool]`,
`_EXISTS_UNKNOWN_TTL_SECONDS`, and `exists()` caches `None` and applies the shorter
TTL to unknown entries).

**Verification:** `app/drive.py` compiles; a pre-seeded `_exists_cache` entry
(`None`/`True`/`False`) is served by `exists()` with no network call, and
`_EXISTS_UNKNOWN_TTL_SECONDS < _EXISTS_TTL_SECONDS`.

### 16.1 Generic async form updates (no-reload buttons)
- Many mutating buttons (admin save/create/delete, user folder + video ops) were plain
  `method="post"` forms that `redirect` back — every click reloaded the page. A single
  reusable helper now turns any opted-in form into an async, no-reload mutation.
- **Helper:** new `app/static/js/async.js` (loaded from `base.html` after the confirm
  script). For every `<form data-async>` it intercepts `submit`, shows the existing
  `data-confirm` dialog when present (via `window.svsConfirm`), POSTs the form's
  `action` with `X-Requested-With: XMLHttpRequest`, and on the JSON response
  (`{ok, message?, ...}`) shows a toast and applies the requested DOM update — no page
  load. The submit button is disabled while the request is in flight.
- **Declarative update modes** (one attribute each, composable):
  - `data-async-remove="<sel>"` → remove `form.closest(<sel>)` (a table row, a card).
  - `data-async-refetch="<sel>"` + `data-async-refetch-url="<url>"` → GET the url and
    replace `<sel>`'s innerHTML (e.g. the folder tree).
  - `data-async-add="<sel>"` → append the server's `html` field to `<sel>` (a new row).
  - `data-async-action="<name>"` → call `window.svsAsyncActions[name](form, data)`
    (page-registered; e.g. the pin toggle). Otherwise just toast `message`.
- **Toast:** `svsToast(message, type)` renders a self-dismissing toast in a `#svs-toasts`
  container added to `base.html` (no full-page flash needed).
- **Confirm integration:** the global `data-confirm` submit handler in `base.html` now
  skips `form[data-async]` so async forms run their own confirm+fetch (no double dialog).
- **Endpoints** gain an `_is_ajax()` JSON branch (the pattern `user.create_folder` already
  used): `admin.save_settings`, `admin.create_user` (returns the new row's `html`),
  `admin.delete_user`, `admin.delete_live_room`, `admin.pin_video` (new),
  `user.delete_video`, `user.retry_video`, `user.rename_folder`, `user.delete_folder`.
  `user.create_folder` was already async and now re-fetches the tree.
- **New read endpoint** for re-fetching without a reload: `user.folder_tree` returns the
  folder-tree HTML so folder ops can swap it. (Create-user returns the new row's HTML
  directly, so no separate users-list endpoint is needed.)
- **Intentionally left as full-reload** (file flows / settings where a flash is useful):
  account avatar / password / live-room forms, and `user.edit_video` / `user.move_video`.
  The helper is generic, so these can opt in later by adding `data-async`.

**Where:** `app/static/js/async.js` (new); `base.html` (load async.js, toast container,
confirm guard); `style.css` (`.svs-toast*`); `app/routes/admin.py` + `app/routes/user.py`
(AJAX branches + new endpoints); `admin*.html`, `user.html`, `_user_row.html`,
`_folder_tree.html` (data-async wiring + partials).

**Verification:** `async.js` parses; `base.html` loads it; each converted form carries
`data-async`; the new endpoints return JSON under `X-Requested-With` (test client) and
redirect otherwise; `py_compile` clean.

### 16.2 Avatar crop: live circular preview
- The account avatar crop dialog (`account.html`, Cropper.js) showed only the crop box.
  It now mirrors the reference layout: the cropper on the left and a **circular live
  preview** on the right ("Preview"), updated on every crop so the user sees the final
  avatar before applying.
- **Markup:** the dialog card becomes a two-column row — left `<img id="avatar-crop-image">`
  (the cropper), right a circular `<img id="avatar-crop-preview">` with a "Preview" caption.
- **JS:** after `new Cropper(...)`, bind `cropper.on('crop', ...)` (and run once at init)
  to `cropper.getCroppedCanvas({width:256,height:256})` → set the preview's `src` to the
  data URL. The apply/cancel flow is unchanged (the cropped JPEG still fills the hidden
  `avatar` input).
- **CSS:** `.crop-dialog .crop-row` (two-column, stacks on narrow screens) and
  `.crop-preview` (circular, fixed 128–160px).

**Where:** `account.html` (dialog markup + crop JS); `style.css` (`.crop-row`,
`.crop-preview`).

**Verification:** the rendered dialog has `#avatar-crop-image` plus a circular
`#avatar-crop-preview`; the crop handler sets the preview `src`. Browser check: dragging
the crop updates the circular preview live.

### 16.3 Admin page → subpages (hub + detail)
- `admin.index` was one long page (Settings, Live rooms, Logs, Users, All videos). It is
  split into a **hub** plus one subpage per feature — "pick a feature, see details".
- **Hub** (`admin.html`): a nine-grid (grid of entry cards) — Videos, Users, Live rooms,
  Settings, Logs, **Data & diagnostics** — each card linking to its subpage.
  `admin.index` renders the hub.
  - **Card sizing:** the hub is a prominent card grid — wide cards
    (`minmax(220px, 1fr)`, 18px gap, ~26px padding), a large count/value
    (2.2rem) over a label (1rem) — so each entry reads at a glance
    (reference: console-style "Quick access" tiles).
- **Subpages** (new templates + routes, all `/admin/...`, still `@admin_required`):
  - `admin.videos` → `admin_videos.html` (All videos table + pin/unpin, §16.6).
  - `admin.users` → `admin_users.html` (users table + create user).
  - `admin.rooms` → `admin_rooms.html` (live rooms list + delete).
  - `admin.settings` → `admin_settings.html` (settings form + disk/drive/cached stats).
  - `admin.logs` → `admin_logs.html` (log viewer).
  - `admin.data` → `admin_data.html` (Data & diagnostics: export/import + WS test).
- **Logs endpoint conflict:** the JSON log endpoint moved from `GET /admin/logs` to
  `GET /admin/logs/data` (`admin.logs_data`); `/admin/logs` is now the HTML subpage. The
  log viewer JS fetches `admin.logs_data`.
- **Admin nav:** each subpage renders a single **back-to-hub** button
  (`.admin-back__btn` → `admin.index`); the global sidebar's Admin item stays active on
  all `/admin/*` (unchanged). There is no horizontal tab bar — the hub is the only index.

**Where:** `app/templates/admin.html` (→ nine-grid hub); new `admin_videos.html`,
`admin_users.html`, `admin_rooms.html`, `admin_settings.html`, `admin_logs.html`,
`admin_data.html`, `_admin_nav.html` (back-to-hub button), `_user_row.html`;
`app/routes/admin.py` (subpage routes + `admin.data` + `admin.logs_data`).

**Verification:** each subpage returns 200 for an admin and renders its section; the hub
links to all six; every subpage shows the back-to-hub button;
`/admin/logs/data?source=app&level=all` returns JSON; `/admin/logs` returns HTML.

### 16.4 Home page: filter + sort
- The home gallery (and folder pages) gains a toolbar like common video sites: a title
  **search** box, an **owner** filter, and a **sort** selector — all client-side over the
  already-rendered cards (no new listing endpoint; the page already returns every video
  in the folder).
- **Sort:** Newest (default) and Oldest. ("Most viewed" was dropped once view
  counts stopped being tracked; title A→Z / Z→A were never shipped.) **Pinned**
  videos always sort first regardless of choice (§16.6).
- **Filter:** search by title (case-insensitive substring) and by uploader. Folders are
  never filtered/sorted — only `.card--video` cells are.
- **Data:** each video card carries `data-title`, `data-owner`,
  `data-uploaded-at` (raw UTC), `data-pinned`, and `data-tags` (pipe-joined
  tag names); the owner select is built from the page's videos (Jinja
  `unique | sort`), so no route change.
- **Reorder** is done by re-appending the visible cards in order (`.waterfall` is CSS
  multi-column, so reflow is automatic); filtered-out cards get the `hidden` attribute.
- **Server order** also becomes pinned-first: `list_folder.sql` / `list_folder_root.sql`
  now `ORDER BY v.is_pinned DESC, v.uploaded_at DESC`, so the initial HTML is correct
  before JS runs (and matches the default sort).

**Where:** `app/templates/home.html` (toolbar + card data attrs + sort/filter JS);
`app/static/css/style.css` (`.home-toolbar*`); `app/sql/videos/list_folder.sql` +
`list_folder_root.sql` (pinned-first order).

**Verification:** rendered home carries the toolbar and per-card data attributes; the
sort/filter script is present. Browser check: typing in search hides non-matching cards;
changing sort reorders (pinned first); the owner filter narrows to one uploader.

### 16.5 Watch page: download button
- The video detail page adds a **Download** button in the uploader row
  (`.video-author`), right-aligned, so a cached video can be saved without the player.
- **Route:** new `watch.download` (`GET /watch/<id>/download`) mirrors `watch.stream`'s
  checks (video exists, cached locally) but serves
  `send_file(..., as_attachment=True, download_name=<title>.<ext>)` — no Range, so the
  browser downloads instead of streaming.
- **Markup/CSS:** the button renders only when `video['local_filename']` is set and
  carries `margin-left:auto` so it sits at the right end of the row.

**Where:** `app/routes/watch.py` (`watch.download`); `app/templates/watch.html` (button in
the `.video-author` row); `app/static/css/style.css` (`.video-author__download`).

**Verification:** for a cached video the button links to `/watch/<id>/download` and that
route returns the file with `Content-Disposition: attachment`; for a non-cached video the
button is absent and the route 404/503s.

### 16.6 Admin: pin a video + pinned badge on home
- An admin can pin/unpin any video from the **Videos** admin subpage; pinned videos get a
  visible **pinned badge** on the home gallery and always sort first (§16.4).
- **Data:** `videos.is_pinned INTEGER NOT NULL DEFAULT 0` (added to `schema.sql` +
  additive migration `videos_add_pinned.sql`; the `INSERT` is unaffected because the
  column has a default and lists its columns explicitly). Pinned-first ordering added in
  `list_folder.sql` / `list_folder_root.sql` (§16.4).
- **Toggle:** new `admin.pin_video` (`POST /admin/videos/<id>/pin`) flips `is_pinned`
  (via the generic `db.update_video`) and returns `{ok, pinned, video_id}`; the Videos
  table shows a pin indicator and an async Pin/Unpin button.
- **Home badge:** a pinned card renders a small "Pinned" badge (pin icon) over its cover.

**Where:** `app/sql/schema.sql` + `app/sql/migrations/videos_add_pinned.sql`; `app/db.py`
(`_migrate`); `app/routes/admin.py` (`pin_video`); `app/templates/admin_videos.html`
(badge + button), `app/templates/home.html` (badge), `app/static/css/style.css`
(`.card__pin`, `.pin-badge`); `async.js` action `pin`.

**Verification:** the migration adds the column (fresh + existing DB); `pin_video`
toggles the flag and returns `pinned`; a pinned video's home card carries the badge and
`is_pinned` leads the listing order.

### 16.7 Batch of bugs 47–54 (Session: 2026-09-26)
A second batch of `testing/bugs.md` items, resolved together.

- **Bug 47 — comment fetch URL (no code change).** The watch-page comment POST used a
  relative URL (`base + '/watch/<id>/comments'`, no credentials), so the browser
  `TypeError ... URL that includes credentials` was a one-off, not a code defect. Verified
  in `app/templates/watch.html`; no change made.
- **Bug 48 — avatar border.** A 1px border is applied to every avatar context: sidebar,
  card/comment, avatar, video-author, and live-view avatars, via `.avatar`-scoped CSS in
  `style.css`.
- **Bug 49 — upload max-bitrate cap.** The upload page gains a max-bitrate input
  (`id="max-bitrate-input"`), persisted to `localStorage` like the custom fps. `upload.js`
  exposes `getMaxBitrate()` and passes `maxBitrateKbps` into `processVideo`; `pipeline.js`
  clamps the custom cap to the admin max before transcode.
- **Bug 50 — My Videos table.** Each row gains a cover-thumbnail column (async fade-in),
  an uploaded-time column rendered as local 24-hour time, a size column class, and a
  `colspan="7"` empty state; supporting CSS added in `style.css`.
- **Bug 51 — admin WebSocket test.** New stdlib-only `app/ws.py` implements the RFC 6455
  handshake + echo on Werkzeug's raw socket (no Flask-SocketIO). `admin.py` adds the
  `/admin/ws` route; the admin "Data & diagnostics" panel has a WebSocket test button with
  its script and CSS. **(Later removed — 2026-10-06 review: `app/ws.py`, the `/admin/ws`
  route and the test button are gone; SSE remains the realtime transport.)**
- **Bug 52 — admin export/import.** `GET /admin/export` zips the storage root **excluding**
  `videos/` and `logs/` (includes DB, covers, avatars). `POST /admin/import` validates the
  uploaded zip, then extracts with path-traversal protection via `Path.is_relative_to`.
  Admin UI + CSS for both.
- **Bug 53 — home remote badge.** `home.py` annotates each card with `is_cached`;
  `home.html` renders a **Remote** badge on non-cached / Drive-only cards; CSS added.
- **Bug 54 — larger sidebar icons + avatar.** Sidebar nav icons and the account avatar CSS
  were enlarged so they no longer sit in the corner.

**Where:** `app/routes/home.py`, `app/routes/admin.py`, `app/ws.py`; `app/templates/home.html`,
`app/templates/upload.html`, `app/templates/user.html`, `app/templates/admin.html`;
`app/static/js/upload.js`, `app/static/js/pipeline.js`, `app/static/css/style.css`;
`pyrightconfig.json` (local pyright config).

**Verification:** `py_compile` clean for all touched Python files; `pyright` clean for
`home.py`, `admin.py`, `ws.py`. A throwaway `_tmp_verify_s16.py` confirmed (via the Flask
test client with redirect-following, and `socket.socketpair()` for WS) the home Remote
badge, the upload max-bitrate input, the My-Videos cover/uploaded columns, export (zip,
excludes `videos/`+`logs/`, contains `data/app.db`), bad-import rejection, and the WS
handshake/accept/echo — all passing. Existing probes `probe_admin_pages`,
`probe_admin_logs`, `probe_no_reload`, and `probe_home_covers` all pass with no regressions.


### 16.7 My Videos table: full-width columns + truncated titles
- The My Videos table (`user.html`) carries a `table--user` modifier that sets
  `width: 100%`, so the table fills the full content width and the columns spread
  across the page (the `.table-wrap` still scrolls on narrow screens).
- Long video titles still truncate with an ellipsis instead of widening the table: the
  `.row-title` link inside the table is a block with `max-width`, `white-space: nowrap`,
  `overflow: hidden`, and `text-overflow: ellipsis`.
- The per-row Edit/Delete buttons are left-aligned in this table (`.table--user
  .row-actions { text-align: left }`) because the shared `.row-actions` rule
  right-aligns them, which left a gap above the Status badge. Admin tables keep the
  shared right-aligned actions.

**Where:** `app/templates/user.html` (adds `table--user` to the table);
`app/static/css/style.css` (`.table--user` + `.table--user .row-title` rules, My Videos section).

**Verification:** `user.html` parses as valid Jinja; CSS is scoped to `.table--user` so the
admin tables are untouched. Reload My Videos in the browser and confirm the table fills
the full content width and long titles show `…`.


### 16.8 Live room page: player + right panel with SSE presence & ephemeral chat (Session: 2026-09-26)
- **Layout (Bilibili-style).** `live_view.html` is a two-column grid (`.live-page`): a
  large left/center DPlayer (`.live-player`, `1fr`) and a fixed-width right panel
  (`.live-side`, `24rem`). The right panel holds, top to bottom: the room title, the
  streamer's avatar + username, the room description, an online counter, one scrolling
  **mixed feed** (`.live-feed`), and a bottom composer (`.live-composer`: input + send).
  Enter/leave notices and chat messages share the single feed — there is no separate
  notification box. The title/owner/description moved from above the player into the
  right panel header. Below 768px the grid stacks: player on top, panel below at
  `70vh`.
- **Why SSE.** Realtime is Server-Sent Events, not WebSockets: SSE needs no new
  dependency and fits the one-way broadcast (join/leave/chat) shape. (The temporary
  admin WebSocket test, §16.7 bug 51, showed the Werkzeug dev server rejects a WS
  upgrade before the route runs; it was later removed — 2026-10-06 review.)
- **`app/presence.py` (new, stdlib only).** In-memory per-room state keyed by
  `room_id`: `members` is `user_id → {id, username, avatar_url}` (dedupe by `user_id`,
  so a page refresh never creates a duplicate entry) and `subs` is
  `user_id → [(sub_id, queue)]` (one queue per open SSE connection; a user may hold
  several tabs). `stream()` registers the connection, broadcasts `join` only when the
  user is new to the room, yields the current `state` (online count), then loops
  `q.get(timeout=15)` — on timeout it yields an SSE comment heartbeat (`: hb`) that
  keeps idle proxies and the browser connection alive. On disconnect (Werkzeug closes
  the response generator, `finally` runs) the connection is dropped, and when it was
  the user's last connection the member entry is deleted and `leave` is broadcast.
  Rooms with no members and no connections are removed from the global map.
  `broadcast_message()` pushes a `message` event onto every open connection's queue.
  All events are a single `data: <json>\n\n` line (types: `state`, `join`, `leave`,
  `message`); the client uses the default `message` event type.
- **Routes (`live.py`, login-required, 404 on unknown rooms).**
  `GET /live/<room_id>/presence` (`live.presence_stream`) returns
  `text/event-stream` with `Cache-Control: no-cache, no-transform` and
  `X-Accel-Buffering: no` (disables Apache proxy buffering).
  `POST /live/<room_id>/chat` (`live.chat`) accepts JSON `{"text": "..."}`
  (1–500 chars after trim) and broadcasts it as a `message` event.
- **Chat persistence (live rooms only).** Chat is stored in the `live_messages`
  table (one row per message: `room_id`, `user_id`, `username`, `avatar_filename`,
  `body`, `created_at`), capped at 50 per room with **no time-based pruning** —
  `db.prune_live_messages` drops the oldest on every insert, so history survives a
  restart and a room being removed; the page displays the most recent 50 messages.
  `POST /live/<room_id>/chat` writes the row before broadcasting; `GET
  /live/<room_id>/presence` replays the stored history to a new connection (`username`/
  `avatar_filename` are snapshots, so history still renders after a user is
  deleted; deleting a room cascades away its messages). Watch-together rooms
  (§17) keep the in-memory ring buffer (50) — this persistence is live rooms only.
- **Client (inline script in `live_view.html`).** Opens an `EventSource` on the
  presence endpoint; `state` updates the online counter, `join`/`leave` append notice
  rows, `message` appends an avatar + username + text row (all via `textContent`,
  never `innerHTML`). Chat messages are capped at 50 (§16.12). The composer POSTs to the chat endpoint
  with `fetch` and clears the input only on success. `EventSource` reconnects
  automatically; server-side cleanup is disconnect-driven and dedupe is by `user_id`,
  so refreshes are safe.
- **CSS (`style.css`).** `.live-page` grid; `.live-side` is `position: relative` and its
  flex layout lives in an absolutely-positioned `.live-side__inner`, so the panel's
  content height never stretches the grid row (the row is sized by the player's 16:9
  aspect). `.live-feed` scrolls internally (`flex: 1; min-height: 0;
  overflow-y: auto`).

**Where:** `app/presence.py` (new); `app/routes/live.py` (`presence_stream`, `chat`,
`_me_info`, `_avatar_url`); `app/templates/live_view.html`; `app/static/css/style.css`
(live page block). Chat persistence: `app/db.py` (`add_live_message`,
`list_live_messages`, `prune_live_messages`), `app/sql/schema.sql` +
`app/sql/migrations/create_live_messages.sql` (the `live_messages` table), and
`app/sql/live_messages/*.sql` (`insert`/`list`/`prune`).

**Verification:** `py_compile` + `pyright` clean for `presence.py` and `live.py`.
`create_app()` + test client: both routes registered; 404 on unknown rooms; anonymous
redirect to login; SSE response carries `text/event-stream` + no-cache headers.
Presence logic exercised standalone (no server): join broadcast, `user_id` dedupe on
refresh, two tabs per user, `leave` only when the last connection closes, room cleanup,
and chat fan-out to every open connection.

### 16.9 Live page: full-screen layout, composer input, online count (Session: 2026-09-26)
- **Full-screen letterboxed player (desktop).** The live room page now fills the
  viewport instead of sitting in the capped, padded `.container`. `base.html` gains a
  `{% block body_class %}` on `<body>`; `live_view.html` overrides it with `page--live`.
  In `style.css`, `body.page--live` (inside a `min-width: 769px` media query) pins
  `.main` to `height: 100vh` and turns `.container` — the flex child of `.main` —
  into a full-bleed flex column (`max-width: none; padding: 0`), so `.live-page` fills
  the space below the breadcrumb. The player fills its grid column edge-to-edge
  (`.player-wrap` drops its `aspect-ratio: 16/9`, margin, radius and shadow in favor of
  `width/height: 100%`), and the DPlayer `<video>` — which DPlayer sizes to `100%/100%`
  but gives no `object-fit` — letterboxes via `object-fit: contain`. The right panel
  keeps its `24rem` width and fills the column height. Mobile keeps the §16.8 stacked
  layout.
- **Composer input fix.** Pico forces `width: 100%` on submit buttons (via a
  `:where()` rule, specificity 0), which locked the live composer's Send button at full
  width and squeezed the input. `.live-composer button[type="submit"]` now sets
  `flex: 0 0 auto; width: auto`, so the button is auto-sized and the input (`flex: 1`)
  expands to fill the row. Applies on all viewports.
- **Online count includes the viewer (§16.11).** The count is driven by the SSE
  events, not the template (the route passes only `room`; the counter renders `…`
  until the first event arrives). `presence.stream()` yields the total number of
  distinct users in the room (including the connecting user), and the `join`/
  `leave` events carry the same total, so the counter updates on every join and
  leave.

**Where:** `app/templates/base.html` (`body` + `body_class` block);
`app/templates/live_view.html` (`body_class` override); `app/static/css/style.css`
(live full-screen block + composer button rule); `app/presence.py` (`stream`).

**Verification:** `py_compile` + `pyright` clean. `build_gui()` still builds the full
block set. Standalone presence check: a lone user's `state` reports `online: 0`
(excluding self); with two distinct users the second connection's `state` reports
`online: 1`. Rendered live page carries `body class="page--live"`.

### 16.10 Admin users: avatar, last-login and password hash (Session: 2026-09-26)
- **Column (migration).** `users` gains `last_login_at TEXT` (nullable) — the UTC time
  of the user's most recent *activity*: an app-wide `before_request` hook
  refreshes it on every authenticated request (in practice "last visit", not
  just logins; the legacy column name `last_login_at` is kept). `app/sql/schema.sql` adds the column to
  the CREATE TABLE and `app/sql/migrations/users_add_last_login_at.sql` adds it to
  existing databases; `db._migrate()` runs that migration when the column is absent.
  `db.update_user()` is fully dynamic, so no new SQL file is needed to write the value.
- **Write on every request.** The `before_request` hook in `app/__init__.py`
  refreshes `last_login_at` via `db.update_user(user_id, last_login_at=...)` in
  UTC `%Y-%m-%d %H:%M:%S` form (matching the other timestamp columns) on every
  authenticated request, so the admin users page shows the user's last visit.
- **Admin users page.** The user table now shows, per row, the user's avatar (served
  by the existing `home.avatar_file` route, with a default SVG fallback when no file
  is set), the last-login time as stored (`YYYY-MM-DD HH:MM:SS`), or an em-dash when
  never recorded, and a truncated `scrypt:` hash (first 12 chars + `…`) that proves a
  hash is stored without leaking the full value. The `must change` badge (when set)
  shows in the Password cell. Table headers are `User` / `Avatar` / `Last login` /
  `Password` / `Actions`.

**Where:** `app/sql/schema.sql`; `app/sql/migrations/users_add_last_login_at.sql`
(new); `app/db.py` (`_migrate`); `app/auth.py` (login success);
`app/templates/admin_users.html`, `app/templates/_user_row.html`;
`app/static/css/style.css` (`.user-row__avatar`, `.user-row__hash`).

**Verification:** `py_compile` + `pyright` clean for `auth.py` and `db.py`. Migration
confirmed (the column exists in the live DB after `create_app()`). `/admin/users`
rendered as admin shows the avatar image, `—` for a never-logged-in user, and the
truncated `scrypt:` hash. The login write reuses `db.update_user` (function verified;
the exact `UPDATE users SET last_login_at=? WHERE id=?` statement exercised directly).

### 16.11 Live-room online count: includes the viewer, updates on join/leave (Session: 2026-09-26)
- **Bug (L61).** The online counter in the right panel was frozen at the value of
  the initial SSE `state` event and never changed when other users joined or
  left. `state` is sent once on entry and is not resent, so the displayed number
  had to come from somewhere else.
- **Fix.** `presence.stream()` now yields the total number of distinct users in
  the room (`len(members)`), *including* the connecting user (who is added to
  `members` before `state` is computed). This replaces the §16.9 behaviour of
  subtracting the viewer (`len(members) - 1`). The `join` and `leave` events also
  carry the updated total (`{"online": len(room.members)}`), so every client —
  the joiner/leaver included, whose own `state` was already stale — stays in sync.
- **Client.** `live_view.html` now sets `#live-online` from all three events
  (`state`, `join`, and `leave`), not just `state`. The initial value is still the
  placeholder `…` until the first event arrives.

**Where:** `app/presence.py` (`stream` — the `state` yield and the `join`/
`leave` payloads); `app/templates/live_view.html` (the `join`/`leave` handlers).

**Verification:** `py_compile` clean. Standalone presence check: with three
distinct users in a room, the third connection's `state` reports `online: 3`;
the `join` event the other two clients receive also carries `online: 3`.

### 16.12 Live chat: capped at 50 messages (in-memory ring buffer, superseded for live rooms) (Session: 2026-09-26)
- **Bug (L62).** The live chat feed grew unbounded — the original `trim()` capped
  it at 200 *rows* (messages and join/leave notices mixed), and the server kept
  no history, so a user who entered mid-stream saw an empty feed.
- **Server ring buffer.** `presence._Room` gains a `messages` list (oldest first),
  capped at `_MAX_MESSAGES` (50) — the oldest is dropped when the cap is
  exceeded. `broadcast_message()` appends each outgoing message to the buffer
  before broadcasting it. On entry, `stream()` snapshots the buffer and replays
  the recent history to the new connection (as `message` events) right after the
  `state` event, so an entering user sees the last 50 messages (auto-load, per
  L62).
- **Client cap.** `live_view.html` caps the chat *records* (`.live-feed__msg`
  rows) at `MAX_MESSAGES` (50); join/leave notices are not chat records and are
  not counted. The oldest message is removed first, so the feed shows at most 50
  messages plus any join/leave notices.
- **Superseded for live rooms.** This in-memory buffer served live rooms until
  §16.8 moved live chat history into the `live_messages` table (server-side cap
  of 50 per room, no time-based pruning; the page shows the latest 50). The ring
  buffer now matters only for watch-together rooms (§17.5), whose chat stays
  in-memory by design.

**Where:** `app/presence.py` (`_Room.messages`, `_MAX_MESSAGES`,
`broadcast_message`, `stream` — the history snapshot + replay);
`app/templates/live_view.html` (`MAX_MESSAGES`, `trim`).

**Verification:** `py_compile` clean. Standalone presence check: after 60
`broadcast_message` calls the ring buffer holds the last 50 (`msg 10` … `msg
59`); a newly-connected client receives `state` (`online: 3` with three users)
followed by the 50 history messages in order.

### 16.13 Clip precision + player keyboard/wheel controls + admin video columns (Session: 2026-09-26)
Three bugs from `testing/bugs.md` (L63–L65), addressed together.

- **(a) Clip precision (L63).** The clip editor stored both cut points via
  `formatTime()`, which floors to whole seconds — so the clip *end* landed a
  fraction of a second early and the exact frame the user aimed at could be
  dropped. `#set-start-btn` now stores `Math.floor(currentTime)` and
  `#set-end-btn` stores `Math.ceil(currentTime)`. This widens the clip by up to
  ~1 s at each end so the user's exact in/out frames are always fully kept.
  (Whole-second granularity is what the time fields already use, so
  `clipRange()` → `trimStart`/`trimEnd` needed no change.)
- **(b) Player keyboard/wheel controls (L64).** On both the clip-editor preview
  (`edit.js`) and the watch page (`watch.html`), ←/→ now seek ∓/±1 s and the
  scroll wheel adjusts volume — only while the pointer is over the video, so
  they never fight page scrolling or text entry. Space already toggled
  pause/play and is unchanged. The watch page detects hover on the `#dplayer`
  container (DPlayer builds the `<video>` lazily).
- **(c) Admin → Videos columns (L65).** The admin video list now shows a cover
  thumbnail, the upload time, and the owner's avatar (next to the owner name),
  matching My Videos. `list_all.sql` gains `u.avatar_filename AS
  owner_avatar_filename` (additive — existing callers are unaffected). The time
  cell reuses the My Videos `data-ts` → local 24-h formatter.

**Where:** `app/static/js/edit.js` (the two `#set-start-btn`/`#set-end-btn`
handlers; a new arrow/wheel block after the Space handler); `app/templates/
watch.html` (a new arrow/wheel block after the Space handler); `app/sql/
list_all.sql` (the `owner_avatar_filename` column); `app/templates/
admin_videos.html` (new Cover/Uploaded/owner-avatar cells + `data-ts` time
formatter); `app/static/css/style.css` (`.admin-owner`/`.admin-owner__avatar`).

**Verification:** `py_compile` clean; `build_gui` unchanged. Standalone check:
`formatTime(Math.ceil(12.345))` → `"00:13"` while `formatTime(12.345)` →
`"00:12"` (end ceils, start floors); the admin row renders the owner avatar and
a local-time string derived from `data-ts`.

**Follow-up (2026-09-27).** The cover `<img>` in `admin_videos.html` initially
used a non-existent endpoint `home.cover_file`, which raised a Werkzeug
`BuildError` on the live admin → Videos page. Corrected to `home.cover`
(`GET /covers/<filename>`), the same endpoint `watch.html`/`user.html` use.
Re-rendered in an app context: the row builds `/covers/<filename>` and
`/avatars/<filename>` URLs and renders cleanly (empty state keeps `colspan="8"`).

### 16.14 Video tag system (Session: 2026-09-26)
Bug L67 from `testing/bugs.md`: a video tag system where anyone can add tags
to the shared library and tag their own videos, the home search box matches
tags as well as titles, a video can carry at most 20 tags, and tag management
is open to every logged-in user (create) while deletion stays admin-only.

- **Tag library + junction (§bug L67).** Two new tables in `app/sql/schema.sql`
  (plus the idempotent migration `app/sql/migrations/create_tags.sql`, applied
  by `_ensure_schema`): `tags` (flat shared library, case-insensitive
  UNIQUE name) and `video_tags` (video↔tag junction, `UNIQUE(video_id,
  tag_id)`, both sides `ON DELETE CASCADE`). The tag SQL lives in
  `app/sql/tags/*.sql`.
- **Upload + edit tag selection.** `app/routes/upload.py` enforces the 20-tag
  cap (`MAX_TAGS_PER_VIDEO`) and associates the checked tags via
  `db.set_video_tags()` inside `_register_and_enqueue` (covers both the single
  and segmented flows). Both upload pages render the tag checkboxes: the
  clip/edit page (`edit.html`, per video, `edit.js` `addTags()`) and the main
  queue page (`upload.html`, global to the whole queue — `upload.js`
  `selectedTagIds()` → `pipeline.js` `uploadVideo` `appendTags()`, which covers
  both the single and segmented paths).
- **Home display + filtering.** `home.py` annotates each video with its tags
  (`_annotate_tags`) so the search box matches title *and* tags, and renders
  the tag badge (`card__tag` in `card__tags`) on each card plus the tag-filter
  select (`home-tag`, present only when a tag exists). The filter is driven by
  inline JS in `home.html`, matching the §16.4 filter+sort contract
  (client-side, re-rendered on every filter/sort/search change).
- **Tag management.** Subpage `/admin/tags` (`admin_tags.html` +
  `_tag_row.html`), linked from the hub (`admin.html` Tags card; `tag_count` in
  `admin()`). Permissions: `GET /admin/tags` and `POST /admin/tags/create` are
  `@login_required` (any logged-in user), `POST /admin/tags/<id>/delete` stays
  `@admin_required`. The page renders the delete forms and the admin nav only
  for admins (via `user_is_admin`, exposed by the context processor in
  `app/__init__.py`). The async create endpoint follows the §16.3 contract and
  additionally returns `tag_id`/`name` so inline clients can build the new
  checkbox; the page's tag forms are wired by the shared `data-async` form
  interceptor in `async.js` (no page-specific handlers).
- **Inline new-tag creation.** Both upload pages (`upload.html`, `edit.html`)
  show the tag section even when the library is empty and carry an inline
  "New tag" control in `.tag-picker` (`#tag-new-name` + `#tag-new-btn`,
  `type="button"` so it never submits the page form). `upload.js`/`edit.js`
  wire the button click and Enter key to `POST /admin/tags/create` (AJAX); on
  success they append a checked `.tag-checkbox` with the returned `tag_id` and
  clear the input, so the existing tag collection (`selectedTagIds()` /
  `addTags()`) picks it up automatically. The 20-tag cap and an obvious
  duplicate check are enforced client-side before the request; errors toast.

**Where:** `app/sql/schema.sql`, `app/sql/migrations/create_tags.sql`,
`app/sql/tags/*.sql`, `app/db.py` (`create_tag`/`get_tag`/`list_tags`/
`delete_tag`/`set_video_tags`/`get_video_tags`/`bulk_video_tags`),
`app/routes/upload.py`, `app/routes/home.py`, `app/routes/admin.py`,
`app/templates/edit.html`, `app/templates/upload.html`,
`app/templates/home.html`, `app/templates/admin.html`,
`app/templates/admin_tags.html`, `app/templates/_tag_row.html`,
`app/static/js/edit.js`, `app/static/js/upload.js`, `app/static/js/pipeline.js`,
`app/static/js/async.js`, `app/static/css/style.css`.

**Verification:** `py_compile` + `pyright` clean on the touched Python. The DB
layer was exercised in isolation (`create_tag`/`get_tag`/`list_tags`,
`set_video_tags` (FK enforced, originals restored), `bulk_video_tags`, and
`delete_tag` cascade all pass). The full HTTP route flow was then verified with
the Flask test client (`testing/probe_tag_routes.py`): a non-admin `GET
/admin/tags` renders (create form visible, no delete controls) and a non-admin
`POST /admin/tags/create` (AJAX) returns JSON with `tag_id`/`name` + the new
`_tag_row`; a non-admin `POST /admin/tags/<id>/delete` gets 403 and the tag is
kept; an admin `GET /admin/tags` renders the delete controls; `GET /` renders
the `home-tag` filter; `GET /upload/edit` and `GET /upload/` render the tag
checkboxes plus the inline new-tag control; and the admin delete (AJAX)
returns JSON with the tag gone. The pure client-side behaviours (search
matching a tag, the `data-async` DOM append/remove, a real upload carrying
tags, and the inline new-tag flow) are browser-only and were confirmed by code
inspection.

### 16.15 My Videos: edit a video's tags
The My Videos edit dialog (`user.html` → `POST /user/videos/<id>/edit`,
`user.edit_video`) can now change a video's tags, not just its title,
description, and cover. It reuses the exact tag picker + inline new-tag control
from the upload pages.

- **Route.** `user.index` passes `tags=db.list_tags()` and annotates each video
  with `tag_ids` (from `db.bulk_video_tags`) so the dialog can pre-check the
  video's current tags. `user.edit_video` reads the checked `tags` checkboxes
  via `_tag_ids_from_form()` (imported from `upload.py` — same 20-cap, drops
  stale/unknown ids) and calls `db.set_video_tags()`, so the selection is
  *replaced*: unchecking a tag removes it, and leaving the form as-is keeps the
  current set (the form pre-checks the current tags).
- **Dialog.** The edit form gains a `Tags` `.upload-section` with one
  `.tag-checkbox` (`name="tags"`) per library tag plus the inline
  `#tag-new-name` + `#tag-new-btn` control (same markup as `edit.html` /
  `upload.html`). Each Edit button carries `data-tag-ids` (comma-separated);
  the dialog JS pre-checks those boxes and clears the inline input on open.
  The inline new-tag wiring (AJAX `POST /admin/tags/create`, append a checked
  checkbox, 20-cap + duplicate check, error toast) is copied into
  `user.html`'s inline script, matching the upload pages.
- **Full-reload.** Like the rest of the edit form, tags submit with the normal
  form POST (redirect + flash) — not AJAX — matching the §16.3
  "intentionally full-reload" note for `user.edit_video`.

**Where:** `app/routes/user.py`, `app/templates/user.html`.

**Verification:** `py_compile` + `pyright` clean on `user.py`. `GET /user/`
renders the tag picker (checkboxes + inline control) in the edit dialog and
each Edit button carries `data-tag-ids`. `POST /user/videos/<id>/edit` with a
`tags` list replaces the video's tags (verified in isolation via the Flask test
client).

### 16.16 Shared tag chip input, clip cover source, clip bitrate cap
Three upload/editor refinements: one shared tag chip input replaces the per-page
checkbox pickers, the clip editor's automatic cover is extracted from the
clipped/transcoded output (not the source), and the clip editor gains a
user-adjustable max-bitrate cap (matching the upload page).

**Shared tag chip input.** The checkbox tag pickers on the upload queue page
(`upload.html`), the clip editor (`edit.html`), and the My Videos edit dialog
(`user.html`) are replaced by one reusable component: `app/static/js/taginput.js`
(exposes `window.SVSTagInput = { init, all, get, set, reset }`) rendered from
the `app/templates/_tag_input.html` partial. The partial carries the full tag
library in `data-tags` (JSON of `{id, name}`) plus a `data-max-tags` cap;
`taginput.js` auto-initialises every `.tag-input` element when it loads (it is
included from `base.html` after `async.js` so `window.svsToast` is available).
The selected tags show as removable chips (each chip carries a hidden
`input[name="tags"]` with the tag id) plus a trailing text field; a
`<datalist>` feeds the field's autocomplete from the library. Enter or comma
commits the field; Backspace on an empty field removes the last chip. Typing a
name that already exists (matched case-insensitively against the library)
selects it without a round-trip; a new name is created via
`POST /admin/tags/create` (AJAX) and then selected. The 20-tag cap and a
duplicate check are enforced client-side (with a toast) and again server-side.
Pages read the selection with `SVSTagInput.get(el)` (the tag ids as strings):
`upload.js` `selectedTagIds()` and `edit.js` `addTags()` both use it instead of
the old `.tag-checkbox:checked` scan. The My Videos dialog prefills with
`SVSTagInput.set(el, ids)` on open (from the Edit button's `data-tag-ids`) and
submits the chip hidden inputs natively with the form. The old per-page inline
"new tag" wiring (`createNewTag()` in `upload.js` / `edit.js` and `user.html`'s
inline script) and the `.tag-picker*` / `.tag-checkbox` CSS are removed;
`.tag-input*` styles are added (accent pills via `var(--accent)`).

**Clip cover source.** `edit.js` previously extracted the automatic cover from
the original `sourceFile`, so every clip of the same source reused the original
60-second frame. `extractCoverWithMediabunny()` now decodes `processedBlob`
(the clipped/transcoded output) and uses `outputInfo.duration` for the 60s mark,
and `extractCoverWithHtml5()` seeks within `processedBlob`. On the direct-pass
path `processedBlob` is the source itself, so this is safe for every case.

**Clip max-bitrate cap.** The clip editor (`edit.html` + `edit.js`) gains a
`max-bitrate-input` (matching the upload page). `edit.js` adds `getMaxBitrate()`,
which reads the input clamped to the admin max (`MAX_BITRATE_KBPS`, kept as the
upper bound / fallback) and persists it to `localStorage` under the shared
`svs-max-bitrate` key. `needsTranscode()` and `processVideo()` now use
`getMaxBitrate()` instead of the hard-coded `MAX_BITRATE_KBPS`, so the transcode
cap is user-adjustable per upload.

**Where:** `app/static/js/taginput.js` (new), `app/templates/_tag_input.html`
(new), `app/templates/base.html` (loads `taginput.js`), `app/templates/
upload.html`, `app/templates/edit.html`, `app/templates/user.html`,
`app/static/js/upload.js`, `app/static/js/edit.js`, `app/static/css/style.css`,
`testing/probe_tag_routes.py`.

**Verification:** `taginput.js` parses (`node --check`). `testing/
probe_tag_routes.py` now asserts the `.tag-input` / `data-tags` markup on both
upload pages (instead of the old `.tag-checkbox` / `#tag-new-*` control). The
pure client-side behaviours (chip add/remove, existing-name matching, new-tag
creation, the 20-cap + duplicate toasts, the clip cover coming from the output,
and the bitrate cap affecting the transcode) are browser-only.

### 16.17 Watch page shows a video's tags
The video watch/detail page (`/watch/<id>`, `watch.page`) now displays the
video's tags under the title, matching the tag-pill look of the home cards.

- **Route.** `watch.page` fetches the video's tags via `db.get_video_tags`
  (returns `[{id, name}]`, alphabetical) and passes them to the template as
  `video_tags`.
- **Template.** `watch.html` renders the tags between the `.watch-title` and
  the player, only when the video has at least one tag, reusing the home
  card's pill (`.card__tag`). The container is a new `.watch-tags` class (flex
  row, wraps) so the pills align with the title rather than the card padding.

**Where:** `app/routes/watch.py`, `app/templates/watch.html`,
`app/static/css/style.css` (`.watch-tags`), `testing/probe_tag_routes.py`
(asserts the watch page renders a tagged video's tag name).

**Verification:** `py_compile` clean on `watch.py`. `testing/probe_tag_routes.py`
finds any video that already has a tag and asserts `GET /watch/<id>` renders its
tag name plus the `.watch-tags` container.

### 16.18 Favicon (browser tab icon)
The site previously had no favicon, so browsers fell back to a generic globe
icon. A PNG favicon is now served.

- **Template.** `base.html` adds `<link rel="icon" type="image/png"
  href="{{ url_for('static', filename='img/favicon.png') }}">` right after the
  `<title>` element. It uses `url_for` (not a hard-coded `/static/...`) so it
  respects the `svs_base` path prefix, matching the other static references.
- **Asset.** The icon file is `app/static/img/favicon.png` (dropped in by the
  operator; square PNG, e.g. 64×64 or 128×128 — the browser scales it to
  16×16 / 32×32).

**Where:** `app/templates/base.html`, `app/static/img/favicon.png`.

**Verification:** the `<link rel="icon">` tag is present in rendered pages;
browsers cache favicons aggressively, so a hard refresh (Ctrl+Shift+R) or a
fresh private window is required to see a change take effect.





## 17. Watch Together (Session: 2026-09-27)

A "Watch Together" feature lets logged-in users watch a cached video in a
synchronized session with other viewers. Each video has an ephemeral
in-memory room; rooms are created on the first join and dropped when the last
viewer leaves. No new database tables or migrations are required.

### 17.2 Routes (`app/routes/together.py`, `url_prefix="/together"`)

| Route | Method | Description |
|-------|--------|-------------|
| `/together` | GET | Room list page — shows active (non-empty) watch rooms. |
| `/together/<video_id>` | GET | Room page — joins the video's room and renders the synchronized player + chat sidebar. Redirects to the watch page for uncached videos. |
| `/together/<video_id>/presence` | GET | SSE stream — presence (join/leave/state) + play-state events. |
| `/together/<video_id>/chat` | POST | Ephemeral chat — sends a message to all viewers in the room (≤ 500 chars). |
| `/together/<video_id>/play` | POST | Play-state control — `{"action": "play"|"pause"|"seek", "t": <seconds>}`. |
| `/together/<video_id>/position` | POST | Position report — `{"t": <seconds>}`; the client's playback position, used to name the "waiting for XX" overlay. |

All routes require login (`@login_required` decorator, redirect to
`/auth/login` if anonymous).

### 17.3 Presence key namespacing

`app/presence.py` keys rooms by a namespaced string (not the bare integer
`video_id`/`room_id`) to avoid collisions between live rooms and watch rooms
that share the same numeric id.

- `presence.live_room_key(room_id) -> "live:<room_id>"`
- `presence.watch_room_key(video_id) -> "watch:<video_id>"`

`_ROOMS` is re-keyed from `dict[int, _Room]` to `dict[str, _Room]`. Existing
callers in `live.py` are updated to use `live_room_key(...)`. The
`online_count` function is updated to accept the namespaced key.

### 17.4 Sync protocol

The **slowest viewer sets the pace**: the shared target is the minimum of all
members' reported playback positions, so no one is left behind. Every member
can pause/resume/seek. A seek **pauses the whole group** for a short settle
window so every client hard-seeks to the seek target and stays frozen while the
seeks land; the watch tick then resumes playback in sync (a seek made while the
group is paused stays paused). Drift within the 1 s tolerance is left alone
(viewers just keep
playing); a viewer more than 1 s ahead of the slowest one **pauses in place**
and shows a "Waiting for XX…" overlay naming the slowest member, and resumes once the
gap drops back under 1 s. When the shared playback is **paused** by a member,
every client shows a "{XX} paused" overlay naming that member (and hides it
once playback resumes). There is **no hard seek for drift** — the only seeks
are a fresh join and an explicit seek (a new `seek_seq`), which removes the
re-buffer stutter the old 0.3 s hard-seek caused.

**Server-side state (per room, in `_Room`):**

- `play_state` — `{"playing": bool, "target_t": float, "paused_by": str | None, "seek_seq": int, "seek_t": float, "seek_settle_until": float | None, "resume_after_seek": bool | None}`:
  whether the shared playback is in play (last-write-wins), the last seek target
  (the fallback pace while no member has a fresh position yet), the username
  of the member who paused (so clients show a "{XX} paused" overlay; `None`
  while playing), the last seek sequence number (bumped on every seek, so a
  client that missed the one-shot broadcast hard-seeks on the next tick), the
  last seek target in seconds (where clients hard-seek to when `seek_seq`
  changes), plus the seek **settle window**: `seek_settle_until` is the
  wall-clock time when the group resumes after a seek (`None` when not
  settling), and `resume_after_seek` records whether the group was playing
  before the seek (so the tick resumes playback, or leaves it paused).
- `positions` — `dict[int, {"t": float, "ts": float}]`: each member's reported
  playback position (`t`, seconds) and the wall-clock time it was reported
  (`ts`). A member whose `ts` is older than `_POSITION_STALE_SECONDS` (3 s) is
  treated as disconnected and excluded from the pace.

The pace (target position) is **derived on every tick**, not stored: it is the
minimum `t` across fresh, still-present members.

**SSE events:**

| Event name | Payload | When |
|------------|---------|------|
| `state` | `{"type":"state","online":N,"playing":bool,"target_t":float,"paused_by":str\|None,"seek_t":float,"seek_seq":int}` | On join (initial state, watch rooms only). `target_t` is the current pace so a late joiner seeks to it; the client adopts `seek_seq` and hard-seeks to `target_t`. |
| `time` | `{"type":"time","playing":bool,"target_t":float,"slowest":str,"paused_by":str\|None,"seek_t":float,"seek_seq":int}` | Broadcast every 0.5 s to every active watch room, and immediately after each play/pause/seek. `target_t` is the pace; `slowest` is the username defining it (drives the drift overlay); `paused_by` is the username of who paused (drives the "{XX} paused" overlay); `seek_seq`/`seek_t` let a client that missed a seek hard-seek to `seek_t` when `seek_seq` changes. |
| `join` / `leave` / `message` | same as live presence | — |

**Client-side reconciliation (every `time` tick):**

```
delta = player.currentTime - target_t
if seek_seq != lastSeekSeq:   hard-seek to seek_t, adopt seek_seq
if not playing:          pause (suppress) + show "{paused_by} paused"
elif delta > 1 s:        pause (suppress) + show "Waiting for {slowest}…"
else:                    play (suppress) + hide overlay
```

The pause is a **local** decision; the suppression flags keep it from POSTing a
shared `pause`, so the group's `playing` state is unchanged and the slowest
viewer keeps moving.

A seek is **self-healing**: the client hard-seeks only when `seek_seq` changes
(a fresh join or a real seek) and re-checks on every 0.5 s tick, so a client
that was in a background tab or reconnected and missed the one-shot seek
broadcast catches up on the next tick instead of drifting.

**Client → server actions:**

- `play` / `pause` — POST `/play` with `{"action":"play"}` or `{"action":"pause"}`.
- `seek` — POST `/play` with `{"action":"seek","t":<seconds>}`. The server pauses the whole group and records the seek target (`seek_t`, `seek_settle_until`). Every client hard-seeks to `t` and skips position reporting while `video.seeking` is set, so a client only reports its (new) position once it has actually landed. The watch tick resumes playback (if the group was playing) as soon as every connected member's latest report is within ~1 s of `t` — i.e. everyone has landed — or once `seek_settle_seconds` in `config.json` (default 60 s, the max wait; set it to your server's worst-case hard-seek) has passed, whichever first. A member that disconnects is removed from the room and is no longer required to arrive, so the group never waits on a gone client. On resume everyone's position is reset to `t`, so a member that was still catching up (it missed the cap) simply plays forward from `t` instead of dragging the pace back.
- position — POST `/position` with `{"t":<seconds>}` every ~0.5 s, reporting the viewer's own `currentTime`.
- **Pause then seek** — if the group is already paused when a seek lands (e.g. the user pauses and then drags the timeline), the group stays paused on the new point and only jumps in sync; it does not resume on its own. This is why the seek settle must wait on the members' *real* position reports rather than pinning them to `t` up front: pinning would make `_all_arrived` fire the instant the seek is broadcast, before any client has actually landed, so a paused group would resume at the wrong point instead of staying frozen on the new point. A `play`/`pause` action that arrives while a seek is still settling sets `resume_after_seek` accordingly (a pause keeps the group paused after the seek; a play resumes it), so the final state matches the last intent. A joiner that arrives mid-settle adopts the seek target `seek_t` (not the derived pace, which is unpinned during the window), so it hard-seeks to the same point as everyone else.

### 17.5 Ephemeral chat

Chat is in-memory (ring buffer, capped at 50 messages per room). Late joiners
receive the last 50 messages in the `state` event. Messages vanish when the
room is dropped (last viewer leaves). No new database table.
### 17.6 Watch-room scheduler

A single background thread (started via `presence.start_watch_scheduler()`,
idempotent) broadcasts a `time` event to every active watch room every 0.5 s.
This guarantees drift stays under 1 s regardless of client clock accuracy.

### 17.7 UI

- **List page** (`together_list.html`): gallery of active rooms — cover image,
  title, "N watching" badge. Links to the room page.
- **Room page** (`together_view.html`): two-column layout mirroring the live
  room page — DPlayer on the left (cached source `/watch/<id>/stream`), right
  sidebar with title, online count, ephemeral chat feed, and composer. Uses
  `body class="page--live"` to reuse the full-screen layout from §16.9.
- **Watch Together button** on `watch.html`: rendered only when `is_cached`
  is true; links to `/together/<video_id>`.
- **Sidebar nav item** in `base.html`: "Watch Together" links to `/together`,
  active on `/together…` paths.

### 17.8 CSS

`.together-*` rules mirror the `.live-*` patterns:
- `.together-card` / `.together-badge` — list page gallery.
- `.together-page` — two-column grid (identical to `.live-page`).
- `.together-player` / `.together-side` / `.together-feed` / `.together-composer` —
  mirror the live room sidebar.
- `body.page--live` rules apply to the watch room page via the same body class.

### 17.9 Scope limits

- Watch Together is only available for **cached** (locally playable) videos.
  The watch room page redirects to the watch page for uncached videos.
- Rooms are ephemeral: dropped when the last viewer leaves. No DB persistence.
- Login is required on all routes (no anonymous watching).
- No new DB tables, no migrations.

**Where:** `app/presence.py` (namespace keys, watch state, scheduler);
`app/routes/together.py` (new blueprint); `app/__init__.py` (blueprint
registration); `app/routes/live.py` (use namespaced keys);
`app/templates/together_list.html` (new); `app/templates/together_view.html`
(new); `app/templates/base.html` (nav item); `app/templates/watch.html`
(Watch Together button); `app/static/css/style.css` (`.together-*` rules).

**Verification:**
- `py_compile` + `pyright` clean for all Python files touched.
- `create_app()` builds successfully.
- `/together` redirects anonymous users to login.
- Room page renders for a cached video; redirects for an uncached one.
- SSE stream: join/leave presence; `time` ticks arrive every ~0.5 s.
- Play/pause/seek propagate to a second client within one tick.
- Drift stays under 1 s under normal conditions.
- Chat caps at 50 messages; messages vanish when room empties.
- No new DB tables or migrations.

### 17.10 Room member avatars

A row of member avatars is shown in the right sidebar, directly below the room
title, on **both** the live room page and the Watch Together room page.

- **Server:** `presence.stream` now includes a `members` list in the initial
  `state` event — every member's `{id, username, avatar_url}`, including the
  connecting user. `join`/`leave` already carry the single user.
- **Client:** each sidebar holds a `members` list and an avatar stack
  (`.room-members`). `state` replaces the list with the full `members` array;
  `join` appends (deduped by `id`, so the user's own join broadcast — which the
  new connection also receives — does not duplicate); `leave` removes by `id`.
- **Overflow:** at most 8 avatars render; any remaining users collapse into a
  `+N` chip (`.room-members__more`) whose tooltip names the count.
- Avatars overlap (negative margin) with a `var(--panel-2)` border so each sits
  on its own disc against the sidebar background.

**Where:** `app/presence.py` (member list in `state`); `app/templates/live_view.html`
+ `app/templates/together_view.html` (`.room-members` container + JS);
`app/static/css/style.css` (`.room-members*` rules).

**Verification:** `py_compile` + `pyright` clean. The `state` event carries a
`members` list of the correct length; a second logged-in client's avatar appears
on the first within one SSE tick; closing a tab removes its avatar; the row caps
at 8 avatars and shows `+N` beyond that.

## 18. Production WSGI Server & Apache Proxy (Session: 2026-09-28)

The app was being run with the Werkzeug **development** server (`main.py` ->
`app.run(...)`) behind the Apache reverse proxy. Two problems followed from that:

1. **Apache `mod_proxy` timeouts on the SSE feed.** The live room page's only
   long-lived connection is the Server-Sent-Events stream
   `GET /live/<room_id>/presence` (§16.8). It stays open for the life of the
   tab and heartbeats every 15 s, so it never "completes". Apache `mod_proxy`
   kills such a response once `ProxyTimeout` (default 60 s) elapses, logging
   `proxy_http:error ... (70007) The timeout specified has expired: AH01110:
   error reading response`. The `X-Accel-Buffering: no` header the route sets is
   an **nginx** directive — Apache `mod_proxy` ignores it, so mod_proxy buffering
   is still on. This fix lives in the Apache vhost, not the backend (§18.4).
2. **Backend flapping (connection refused).** `config.json` had `"debug": true`,
   so the Werkzeug **auto-reloader** (and interactive debugger) ran in
   production, and nothing supervised the process. A crash/OOM left the port
   down and Apache logged `AH01102 ... Connection refused` until it was started
   again. The Werkzeug dev server is not production-grade in the first place.

### 18.1 WSGI entry point

- **`wsgi.py` (new, project root).** Exposes the app as a module-level `app`
  (`app = create_app()`) so a production WSGI server can load it by `wsgi:app`.
  It never starts the dev server, reloader, or debugger. `main.py` remains the
  local-development entry (`python main.py`).

### 18.2 Waitress as the production server

- **`start.sh`.** The launch step now runs
  `waitress-serve --listen=<host>:<port> --threads=<N> wsgi:app` instead of
  `python main.py`. `<host>`/`<port>` are read from `config.json` (unchanged
  mechanism); `<N>` is the `THREADS` env var, defaulting to **50**.
- **Self-updating deploy (`git pull --ff-only`).** `start.sh` now pulls the
  latest code from the remote as its first step, so the VPS always runs what is
  on the remote. `--ff-only` fast-forwards without creating merge commits and
  aborts the start (fail-closed, `set -e`) if the tree has uncommitted edits to
  tracked files or the branch has diverged — a half-updated app never starts.
  `config.json` and `documents/PROJECT_PROGRESS.md` are git-ignored, so the pull never
  touches the VPS's live `secret_key`/`drive_folder_id`. Trade-off: a transient
  GitHub outage prevents a restart (the operator investigates rather than
  silently running possibly-stale code).
- **`requirements.txt`.** Adds `waitress`.
- **Why Waitress (and not multi-process Gunicorn):** `app/presence.py` keeps all
  room state (online counts, chat buffers, watch-together pace) in **one
  process's memory**. A multi-process WSGI server (e.g. `gunicorn -w 4`) would
  split that state across workers — a chat message sent to one worker would not
  reach another, and online counts would disagree. Waitress is a single process
  with a thread per connection, so all SSE connections and regular requests share
  the same in-memory rooms while still running concurrently.
- **Thread budget.** Every open SSE connection holds one thread for its whole
  lifetime (it blocks on `queue.get`), so `THREADS` must exceed the expected
  number of simultaneous live/watch-room viewers plus a burst of normal requests.
  50 is ample for this site; raise it if rooms get bigger.
- **No hop-by-hop response headers (PEP 3333).** Waitress enforces PEP 3333
  strictly, unlike the Werkzeug dev server. The SSE routes must **not** set
  `Connection` (a hop-by-hop header the WSGI server owns) — doing so raises
  `AssertionError: Connection is a "hop-by-hop" header`. Only end-to-end
  headers such as `Cache-Control` and `X-Accel-Buffering` may be set by the
  application. (Werkzeug silently ignored the header, which masked this.)
- **Gunicorn, if preferred, must run single-process (`-w 1`).** The memory
  argument for fewer workers is real, but `-w > 1` is a hard no here regardless
  of memory: `_ROOMS` in `app/presence.py` is a **per-process** dict, so two
  workers hold two independent copies that diverge on the first join — a chat
  message broadcast by one worker never reaches a client pinned to the other,
  and online counts disagree. `--preload`/fork does **not** fix this: it only
  copy-on-writes the read-only imported modules; the mutable room state diverges
  the moment any user connects. The only correct Gunicorn mode is
  `gunicorn -w 1 --threads 50 -b 127.0.0.1:8080 wsgi:app` — note `wsgi:app`,
  **not** `app:app` (there is no module-level `app` in `app/__init__.py`). That
  mode is functionally identical to Waitress (single process, one thread per
  connection), so Waitress is kept: same concurrency, less worker-manager
  overhead, and no `-w` foot-gun. With `-w 1` there is no per-worker memory
  multiplication — total RSS is roughly one app (~100–150 MB) plus a little per
  active thread, since idle threads blocked on `queue.get` use only a few
  hundred KB of their stack.

### 18.3 Debug off + process supervision

- **`config.json`.** `"debug": false` — no auto-reloader, no interactive
  debugger, generic error pages in production (also correct for the local-dev
  fallback in `main.py`).
- **systemd (on the VPS).** The service should run `start.sh` (or
  `waitress-serve wsgi:app` directly) with `Restart=always` so a crash or OOM
  self-restarts instead of leaving the port down. The unit file lives on the VPS,
  not in this repo.

### 18.4 Apache proxy tuning (on the VPS)

On the vhost that proxies to `127.0.0.1:8080`, exempt the SSE endpoints from
`mod_proxy`'s response timeout so the long-lived stream is not cut at 60 s:

    <LocationMatch "/video/(live|together)/\d+/presence$">
        ProxyTimeout 0
        ProxyIOBufferSize 0
        ProxyMaxForwards 1
    </LocationMatch>

(The exact path prefix depends on the site's mount point; match whatever the
`/…/presence` URL is. The 15 s heartbeat already bounds how long a dead SSE
connection can linger, so `ProxyTimeout 0` is safe.) The Apache vhost lives on the
VPS, not in this repo.

**Where:** `wsgi.py` (new); `start.sh` (launch step); `requirements.txt`
(`waitress`); `config.json` (`debug`); Apache vhost + systemd unit (VPS).

**Verification:**
- `wsgi.py` imports cleanly: `python -c "import wsgi; print(wsgi.app)"`.
- `start.sh` starts Waitress on `127.0.0.1:8080` and serves the app; Apache
  proxies to it with no `AH01110` timeout on `/live/<id>/presence` while the tab
  stays open past 60 s.
- No `AH01102` connection-refused bursts after a process restart (supervised).
- Concurrent viewers in a live room all receive each other's chat (single process).

## 19. Post-download watch page, live list viewer count, empty-comment placeholder (Session: 2026-09-28)

### 19.1 Watch page: show Watch Together + Download after on-demand Drive download completes
- **Problem.** For a non-cached video the watch page renders the `.video-author` row with the
  **Watch Together** button omitted (`{% if is_cached %}`) and the **Download** button omitted
  (`{% if video['local_filename'] %}`). After the user clicks *Download & play* and the background
  Drive→local download finishes, the cache-poll success callback `done()` reveals the player and
  builds DPlayer, but the two buttons stay absent until a manual reload.
- **Fix.** Render both buttons unconditionally and hide them with the HTML `hidden` attribute while
  the video is not cached. In `done()`, set `el.hidden = false` on both so the row matches the cached
  layout exactly — no reload. The existing `[hidden] { display: none !important }` rule (style.css)
  force-hides them over Pico's `display`.
  - Watch Together: gated on `is_cached` (unchanged; now expressed via `hidden` instead of `{% if %}`).
  - Download: gate changed from `video['local_filename']` to `is_cached`, so it only shows once the
    local file actually exists (matches the `watch.download` route's `os.path.exists` check).
- **Where:** `app/templates/watch.html` (`.video-author` row + `done()`).

**Verification:** for a non-cached video both buttons are in the DOM but `hidden`; when the poll
returns `cached: true` both become visible without a reload; for an already-cached video both are
visible on load.

### 19.2 Live list card: show the online count (like the Watch Together card)
- **Problem.** The Watch Together list card shows a `{{ room['online'] }} watching` badge
  (`.together-badge`), but the live list card only shows the ON AIR badge (`.live-badge`, top-right,
  hidden unless on air). A live card has no viewer count.
- **Fix.** `live.index` computes each room's current online count from in-memory presence
  (`presence.online_count(presence.live_room_key(room['id']))`) and passes an `online` map to the
  template. `live_list.html` renders `<span class="live-online">N watching</span>` per card.
  - The count is a snapshot baked at render time (same as the Watch Together list); it refreshes on
    the next page load, not via polling. The lookup is in-memory, so it does not block the instant
    first paint (the ON AIR badge is still fetched in the background from `/live/onair`).
  - Positioned **top-left** (`.live-online`) so it never overlaps the ON AIR badge (top-right).
  - Always visible; reads `0 watching` for an empty room.
- **Where:** `app/routes/live.py` (`index`); `app/templates/live_list.html`;
  `app/static/css/style.css` (`.live-online`).

**Verification:** `py_compile` + `pyright` clean. The rendered live list carries one `.live-online`
badge per card showing the current in-memory count (0 when the room is empty).

### 19.3 Watch page: hide "No comments yet" after the first comment is posted
- **Problem.** When a video has no comments the page renders
  `<li class="muted">No comments yet — be the first!</li>` inside `.comments__list`. Posting the first
  comment via AJAX appends the new comment but leaves the placeholder, so it reads "No comments yet"
  next to the new comment.
- **Fix.** Give the placeholder an `id` (`no-comments`). In the async `postComment` success handler,
  remove `#no-comments` if present. The server-rendered count in the `Comments (N)` heading is left
  as-is (out of scope here).
- **Where:** `app/templates/watch.html` (comment placeholder + `postComment`).

**Verification:** a video with no comments shows the placeholder; after posting the first comment via
the form the placeholder is removed and the new comment is shown.

## 20. Home card redesign: uploader avatar over the cover + comment-count badge (Session: 2026-10-06)

The home gallery cards (home page and folder pages) show an oversized uploader
avatar and a comment signal, decided and confirmed with the owner:

- **Avatar overlap.** The card's uploader avatar moves out of the bottom meta
  row and sits over the **bottom-left of the cover image**: the 48 px avatar
  is pulled up with a negative margin so its lower half covers part of the
  cover. The uploader name renders to the **right of the avatar**, on the row
  directly below the cover (the "uploader row" is now the first line of the
  card body).
- **Card body order:** `cover → [avatar + uploader] → title → tags → date`.
- **Comment-count badge.** A dark semi-transparent capsule in the cover's
  **bottom-right corner** (same look as the Remote badge, chat-bubble glyph +
  number). It is **hidden when the video has 0 comments**.
- **Avatar treatment:** stays circular, grows 20 px → **48 px**, border
  1.5 px → **3 px `var(--border-strong)`** plus a light drop shadow, so
  every uploader reads clearly on any cover.
- **Live & Watch Together list cards sync (owner-confirmed).** The live-room
  list (`/live`) and the Watch Together room list (`/together`) adopt the
  same author-row layout (48 px avatar overlapping the cover's bottom edge,
  name to its right, first body line): live cards show the **room owner**,
  together cards the **video uploader**. The comment-count badge stays
  **home-only** — neither list page shows one (owner decision).

**Data link.** The count is computed per page with one aggregate query
(`app/sql/comments/count_by_video.sql`, `COUNT(*) … WHERE video_id IN ($ids)
GROUP BY video_id`, reusing the project's `$ids` placeholder convention and
the existing `idx_comments_video` index) through a new
`db.comment_counts(video_ids)`; `home.py` annotates the Drive-filtered card
list (`video['comment_count']`, default 0, replies included) in
`_render_gallery()` before rendering. No schema change.

**Where:** `app/templates/home.html` (card body reorder: new `.card__author`
row after the cover; `.card__meta` keeps only the date; `.card__comments`
cover badge); `app/static/css/style.css` (`.card__avatar` 48 px / 3 px border
/ shadow, pulled out of the shared avatar-border group; new `.card__author`
with `margin-top: -24px; z-index: 2`; new `.card__comments`; `folder-card__title`
padding re-calibrated so folder cells match the new video-card height;
`.live-card`/`.together-card` title bottom padding); `app/templates/
live_list.html` (author row first, room owner, no date row); `app/routes/
together.py` (`index()` enriches each room with the uploader's username/
avatar); `app/templates/together_list.html` (author row, video uploader);
`app/sql/comments/count_by_video.sql` (new); `app/db.py` (`comment_counts`);
`app/routes/home.py` (`_annotate_comment_counts` in `_render_gallery`);
`testing/probe_home_cards.py` (new probe, incl. the live/together list
checks).

**Verification:** `testing/probe_home_cards.py` — author row + 48 px avatar on
every video card; badge appears with the exact count after a comment (and a
reply → 2) is inserted and disappears for 0-comment videos; folder pages and
the `data-*` contract unchanged; `GET /live` and `GET /together` cards render
the same author row (live: room owner, "Unknown" + default avatar for
owner-less rooms; together: video uploader) with **no** comment badge; the
ON AIR / "watching" badge markup is untouched. `probe_home_covers.py` and
`probe_no_reload.py` pass unmodified; `py_compile` + `pyright` clean. Browser
check in both themes: avatar straddles the cover edge, badge clear of the
Pinned/Remote badges, 2-column mobile layout intact, folder cards same height
as tag-less video cards.

**Implementation instruction sheet:** `documents/code_review_2026-10-06_163158.md`.

---

## 20.1 Home card refinements: 64 px backdropped avatar, name clear of the
cover, right-aligned smaller date, comment badge to the top-left
(Session: 2026-10-06 17:29)

Owner-confirmed refinements to the §20 card design. All five are pure CSS
(no template / JS / SQL / route / DB changes): the markup of
`home.html`, `live_list.html` and `together_list.html` stays byte-for-byte
identical.

- **Avatar 48 px → 64 px** (owner decision, 2026-10-06). The overlap
  convention is kept: `.card__author` `margin-top` becomes `-32px`, so the
  lower half of the 64 px avatar still straddles the cover's bottom edge.
- **Solid backdrop under semi-transparent avatars.** `background:
  var(--panel)` on `.card__avatar` (existing `border-radius: 50%` makes it
  a filled disc): transparent PNG avatar regions show the card background,
  never the cover. The 3 px `var(--border-strong)` border and drop shadow
  are kept.
- **Uploader name out of the cover.** `.card__author` switches from
  `align-items: center` to `align-items: flex-end`: the name aligns to the
  row's bottom, which with `-32px` sits fully below the cover's bottom
  edge; it can no longer overlap the cover image.
- **Date label smaller + bottom-right.** `.card__meta` gains
  `justify-content: flex-end` and its font size drops 0.8 rem →
  0.72 rem (the date is the row's only child, so it hugs the card's
  bottom-right corner).
- **Comment badge to the cover's top-left.** `.card__comments` moves from
  `bottom: 8px; right: 8px` to `top: 8px; left: 8px`. On pinned cards the
  Pinned badge occupies that corner, so a new rule
  `.card__pin ~ .card__comments` (DOM order in `home.html` is pin →
  remote → comments) drops the comment badge to the bottom-right when a
  Pinned badge is present. Pinned + Remote + comments therefore reads:
  Pinned top-left, Remote top-right, comments bottom-right.

**Ripple:** the live-list and together-list cards share
`.card__avatar` / `.card__author`, so the 64 px backdropped avatar and the
bottom-aligned name apply to them automatically (they have no date row and
no comment badge). `.folder-card__title` `padding-bottom` is re-calibrated
50 px → **58 px** (the video-card author row's effective contribution grows
24 px → 32 px with the larger avatar) to keep folder cells level with
tag-less video cells.

**Verification:** `testing/probe_home_cards.py` gains a CSS sanity block
asserting the new declarations (64 px + `var(--panel)` on `.card__avatar`,
`-32px` / `flex-end` on `.card__author`, `0.72rem` / `flex-end` on
`.card__meta`, top-left on `.card__comments`, the
`.card__pin ~ .card__comments` fallback rule, 58 px on
`.folder-card__title`); every existing markup check must keep passing
unchanged, and two consecutive probe runs must be clean. `probe_home_covers.py`
and `probe_no_reload.py` pass unmodified; `pyright app` stays clean. Owner's
manual browser pass (dark + light, 2-column mobile): semi-transparent avatar
shows the solid `var(--panel)` disc, name fully below the cover edge, date
small and bottom-right, comment badge top-left / bottom-right on pinned
cards, folder cells level with tag-less video cells.

**Implementation instruction sheet:** `documents/code_review_2026-10-06_172902.md`.

## Change Log (from code_review_2026-10-06_172902)

**Landed:** 2026-10-06 17:43 — both items 3.1–3.2 implemented and verified.

| # | File(s) | Change | Why | Blast radius |
|---|---------|--------|-----|--------------|
| 3.1 | `app/static/css/style.css` | `.card__avatar` 64 px + `background: var(--panel)`; `.card__author` `margin-top: -32px` + `align-items: flex-end`; `.card__meta` `0.72rem` + `justify-content: flex-end`; `.card__comments` moved to `top: 8px; left: 8px` with a new `.card__pin ~ .card__comments` bottom-right fallback; `.folder-card__title` padding-bottom 50 px → 58 px; rule comments updated | §20.1 owner refinements 1–5 | home + folder gallery, live list, together list (shared card classes) |
| 3.2 | `testing/probe_home_cards.py` (extended) | New CSS sanity step (docstring step 6 + `_css_block` / `_check_card_css` helpers) asserting the declarations above in `style.css`; all existing markup checks unchanged | regression for §20.1 | testing only |

**Verification (2026-10-06 17:43):**
- `python testing/probe_home_cards.py` — two consecutive runs pass with no DB residue: every existing markup check (home author row + `data-*` contract, pin badge, comment badge 0 → 1 → 2 → hidden, folder page, `/live` incl. owner-less fallback, `/together`) plus the new CSS sanity step (64 px + `var(--panel)` avatar, `-32px` / `flex-end` author row, `0.72rem` right-aligned meta, top-left comment badge with the `.card__pin ~ .card__comments` bottom-right fallback, 58 px `.folder-card__title` padding).
- `python testing/probe_home_covers.py` and `python testing/probe_no_reload.py` pass unmodified.
- `py_compile` on the probe: clean; `pyright app`: 0 errors / 0 warnings.
- Manual browser pass (dark + light, 2-column mobile): still to be done by the owner — semi-transparent avatar shows the solid `var(--panel)` disc, name fully below the cover edge, date small and bottom-right, comment badge top-left / bottom-right on pinned cards, folder cells level with tag-less video cells.

**Date:** 2026-10-06 (sheet) / landed 2026-10-06 17:43
**Source:** `documents/code_review_2026-10-06_172902.md`

---

## Pending Changes (from code_review_2026-10-06_155159)

| # | File(s) | Change | Why | Blast radius |
|---|---------|--------|-----|-------------|
| 1.1 | `app/static/js/pipeline.js` | Replace `libx264/libvpx` ffmpeg fallback with `libsvtav1/libx264` 4-attempt chain; add `opts.audio` to `runFfmpeg`; rewrite header comment | §13.13/§17.1: output must be AV1 or H.264; VP9 was undocumented | upload pipeline (main page) |
| 1.2 | `app/static/js/edit.js` | Reword stale header comment (L1-16) to match current contract | Comment accuracy; no behaviour change | edit page |
| 1.3 | `app/static/js/upload.js` | Count successes in `startQueue()`; only show success toast when ≥1 file succeeded; log failure count when all fail | A fully-failed batch should not announce success | upload page |
| 1.4 | `app/routes/live.py` | Remove `_STREAM_USER`/`_STREAM_PASSWORD` constants; call `probe_stream(url)` with empty credentials; update comments | Apache vhost now uses Referer/UA guard, not Basic auth | live room on-air probe |
| 1.5 | `app/routes/upload.py` | In `seg_part()` and `seg_finish()`: compare `sess["user_id"]` with `me["id"]`; return 403 on mismatch | Prevent cross-user token replay on segmented uploads | upload seg endpoints |
| 1.6 | `app/routes/watch.py` | In `start_cache()`: after loading video + me, require `owner_id == me["id"]` or `is_admin`; return 403 otherwise | Non-owners should not consume server disk + cache cap on someone else's video | watch cache endpoint |
| 1.7 | `app/templates/admin.html` | Change Data hub label from `Export · import · WS` to `Export · import` | WS test (Bug 51) was removed from code | admin hub UI |
| 1.8 | `app/sql/list_all.sql` | Delete orphan file (loader reads `app/sql/videos/list_all.sql`) | Avoids confusion; the top-level file is never loaded | none |
| 1.9 | `testing/progress.md` | Delete stale early progress log (superseded by `documents/PROJECT_PROGRESS.md`) | Two progress files invite divergence | none |
| 1.10 | `start.sh` | Fix comment: `project_requirements.md` → `documents/PROJECT_PROGRESS.md` | Correct filename reference in deployment script comment | none |
| 1.11 | `app/routes/together.py` + `app/templates/together_view.html` | Unify together-room chat payload key from `"message"` to `"text"` (matching the live-room convention) | One key means "chat message body" across the app | together room chat |

**Date:** 2026-10-06
**Source:** `documents/code_review_2026-10-06_155159.md`

## Change Log (from code_review_2026-10-06_163158)

**Landed:** 2026-10-06 17:12 — all items 2.1–2.9 implemented and verified.

| # | File(s) | Change | Why | Blast radius |
|---|---------|--------|-----|--------------|
| 2.1 | `app/templates/home.html` | Card body reorder: new `.card__author` row (avatar + uploader) first, below the cover; `.card__meta` keeps only the date; new `.card__comments` cover badge (bottom-right) rendered when `comment_count > 0` | §20: avatar overlaps the cover; uploader row tops the body | home + folder gallery |
| 2.2 | `app/static/css/style.css` | `.card__avatar` → 48 px, 3 px `border-strong` border + shadow (removed from the shared 1.5 px avatar-border group); new `.card__author` (`margin-top: -24px; z-index: 2`); new `.card__comments` (Remote-badge style at `bottom: 8px; right: 8px`); `.folder-card__title` padding re-calibrated to keep folder cells the same height; `.live-card`/`.together-card` title bottom padding | §20: the confirmed visual spec | home + folder gallery |
| 2.3 | `app/sql/comments/count_by_video.sql` (new) | `SELECT video_id, COUNT(*) AS n FROM comments WHERE video_id IN ($ids) GROUP BY video_id` | §20: per-video comment aggregate | comments reads only |
| 2.4 | `app/db.py` | New `comment_counts(video_ids) -> dict[int, int]` via `_sql("comments", "count_by_video")` (``$ids`` → N literal `?`); empty input → `{}` | §20 data link | db layer (additive) |
| 2.5 | `app/routes/home.py` | `_annotate_comment_counts()` on the Drive-filtered list in `_render_gallery` (mirrors `_annotate_tags`) | §20: every rendered card carries `comment_count` | home + folder gallery |
| 2.6 | `testing/probe_home_cards.py` (new) | Probe: author-row markup + data-* contract, badge appears with exact count after a comment/reply and is absent at 0, folder pages unchanged | regression for §20 | testing only |
| 2.7 | `app/templates/live_list.html` | Author row (room owner, "Unknown" fallback for owner-less rooms) first below the cover; `card__meta` row removed; ON AIR / watching badges unchanged; no comment badge | §20 sync: live list cards share the redesigned layout | live list page |
| 2.8 | `app/routes/together.py` (`index`) + `app/templates/together_list.html` | Enrich rooms with the uploader's username/avatar (`db.get_user_by_id` on `video["owner_id"]`, deleted user → "Unknown"); insert the same `.card__author` block in the card; no comment badge | §20 sync: together cards show the video uploader | together list page |
| 2.9 | `testing/probe_home_cards.py` (extended) | Extra check blocks: `/live` author row (incl. owner-less "Unknown" + default-avatar fallback) and `/together` uploader row (after priming an active room via the presence stream); neither page renders a `.card__comments` badge; ON AIR / watching badge markup unchanged | regression for the §20 list-card sync | testing only |

**Verification (2026-10-06 17:12):**
- `python testing/probe_home_cards.py` — all checks pass: author row + `data-*` contract on every home card; pin badge markup unchanged; comment badge hidden at 0 → 1 after a comment → 2 after a reply (replies included) → hidden after cleanup; folder pages render the new body order with folder cards intact; `/live` cards show the room owner ("Unknown" + `avatar-default.svg` for owner-less rooms) with no comment badge and the ON AIR / watching badges untouched; `/together` (room primed via the presence stream) shows the video uploader in the author row, no comment badge, "watching" badge intact. The probe is idempotent (two consecutive runs pass, no DB residue).
- `python testing/probe_home_covers.py` and `python testing/probe_no_reload.py` pass unmodified.
- `py_compile` on `home.py` / `db.py` / `together.py` / the probe: clean; `pyright app`: 0 errors / 0 warnings.
- `.folder-card__title` `padding-bottom` set to **50 px** by box-model computation (required X = 32 + root font-size; the root size is the viewport clamp `13–22 px`, so no single value is exact for every viewport — 50 px keeps folder cells within ~2 px of tag-less video cells on common desktop widths, 1366–2560). The sheet's rough 56–64 px estimate was superseded by this measurement; a manual browser check (dark + light themes, 2-column mobile) remains for the owner per §20.
- Manual browser pass (dark + light): still to be done by the owner (avatar straddling the cover edge, badge clear of Pinned/Remote, 2-column mobile layout).

**Date:** 2026-10-06 (sheet) / landed 2026-10-06 17:12
**Source:** `documents/code_review_2026-10-06_163158.md`

## Pending Changes (from code_review_2026-10-06_175116)

Review of the §20.1 follow-up (sheet `code_review_2026-10-06_172902.md`):
approved — all five CSS rules, the probe CSS sanity step, the unchanged
markup, and both bookkeeping entries match the sheet; all three probes pass
on independent re-run. Two non-blocking findings led to the owner's
§20.2 refinements below.

## 20.2 Home card refinements: bigger uploader name; comment badge to the
cover's bottom-right
(Session: 2026-10-06 17:51)

Owner feedback on the §20.1 result:

1. **Bigger uploader name.** `.card__owner` 0.85 rem is too small next to
   the 64 px avatar → **1 rem** (slightly above the 0.9 rem card title).
   The class is shared, so live/together list owner labels grow too.
2. **Comment badge to the cover's bottom-right, on every card** — not the
   §20.1 top-left default and not the card-relative bottom-right pinned
   fallback. The §20.1 fallback positioned the badge relative to `.card`
   (the containing block), so on pinned cards it landed in the card's
   bottom-right date-row area, not over the cover. Fix: a
   `position: relative` wrapper `<div class="card__cover-wrap">` around the
   cover `<img>` in `home.html` (badge span moved inside it); the badge's
   `right: 8px; bottom: 8px` then means the **cover's** bottom-right. The
   `.card__pin ~ .card__comments` fallback rule is deleted — Pinned
   (top-left) and Remote (top-right) never collide with a cover
   bottom-right badge, so the badge no longer moves on pinned cards.

### Pending Changes

| # | File(s) | Change | Why | Blast radius |
|---|---------|--------|-----|--------------|
| 4.1 | `app/templates/home.html` | Wrap the cover `<img class="card__cover">` in `<div class="card__cover-wrap">` (both comment-count branches); move the `<span class="card__comments">` block inside the wrapper, after the `<img>`; pin/remote spans stay outside the wrapper | badge containing block must be exactly the cover area | home + folder gallery markup |
| 4.2 | `app/static/css/style.css` | Add `.card__cover-wrap { position: relative; }`; `.card__owner` `font-size: 0.85rem;` → `1rem;`; `.card__comments` drop `top: 8px; left: 8px;`, set `right: 8px; bottom: 8px;` (cover-relative now), reword comment; delete the `.card__pin ~ .card__comments` rule | §20.2 owner refinements 1–2 | home + folder gallery, live list, together list (owner label) |
| 4.3 | `testing/probe_home_cards.py` | Update `_check_card_css`: `.card__comments` contains `right: 8px;` + `bottom: 8px;` and NOT `top: 8px;` / `left: 8px;`; drop the `~` fallback assertion; assert a `.card__cover-wrap` rule with `position: relative;`; assert `.card__owner` contains `font-size: 1rem;`; update docstring step 6. All existing markup checks untouched | regression for §20.2 | testing only |

**Verification:** `probe_home_cards.py` all existing steps pass unchanged
(wrapper does not disturb the cover < author < title order check) + updated
CSS sanity step; two consecutive runs clean. `probe_home_covers.py` and
`probe_no_reload.py` pass unmodified. `py_compile` on the probe; `pyright
app` clean. Owner's manual browser pass (dark + light, 2-column mobile):
bubble in the cover's bottom-right on normal, pinned and pinned+remote
cards; 1 rem username readable next to the 64 px avatar on all three lists.

**Implementation instruction sheet:** `documents/code_review_2026-10-06_175116.md`.

## Change Log (from code_review_2026-10-06_175116)

**Landed:** 2026-10-06 18:00 — items 4.1–4.3 implemented and verified.

| # | File(s) | Change | Why | Blast radius |
|---|---------|--------|-----|--------------|
| 4.1 | `app/templates/home.html` | Wrap the cover `<img class="card__cover">` in `<div class="card__cover-wrap">` (both comment-count branches); move the `<span class="card__comments">` badge inside the wrapper, after the `<img>`; pin/remote spans stay outside the wrapper | badge containing block must be exactly the cover area | home + folder gallery markup |
| 4.2 | `app/static/css/style.css` | Add `.card__cover-wrap { position: relative; }`; `.card__owner` `font-size: 0.85rem;` → `1rem;`; `.card__comments` drop `top: 8px; left: 8px;`, set `right: 8px; bottom: 8px;` (now cover-relative via the wrapper), rule comment reworded; delete the `.card__pin ~ .card__comments` fallback rule | §20.2 owner refinements 1–2 | home + folder gallery, live list, together list (owner label) |
| 4.3 | `testing/probe_home_cards.py` | `_check_card_css` updated: `.card__comments` must contain `right: 8px;` + `bottom: 8px;` and NOT `top: 8px;` / `left: 8px;`; drop the `.card__pin ~ .card__comments` fallback assertion; new assertions for the `position: relative` `.card__cover-wrap` rule and the 1 rem `.card__owner`; docstring step 6 + the CSS print line reworded; all existing markup checks untouched | regression for §20.2 | testing only |

**Verification (2026-10-06 18:00):**
- `python testing/probe_home_cards.py` — two consecutive runs pass with no DB residue: every existing markup check (author row + `data-*` contract on all 8 home cards, pin badge on the pinned card, comment badge 0 → 1 → 2 → hidden now exercising the wrapper markup, folder pages, `/live` incl. owner-less "Unknown" + default-avatar fallback, `/together` room card) plus the updated CSS sanity step (64 px + `var(--panel)` avatar, `-32px` / `flex-end` author row, 1 rem `.card__owner`, 0.72 rem right-aligned meta, `position: relative` `.card__cover-wrap`, `.card__comments` anchored to the cover's bottom-right with no top-left / pinned-fallback rules, 58 px `.folder-card__title` padding).
- `python testing/probe_home_covers.py` (8 cover `<img>` on home, 2 on live) and `python testing/probe_no_reload.py` pass unmodified; all three probes exit 0.
- `py_compile testing/probe_home_cards.py`: clean; `pyright app`: 0 errors / 0 warnings.
- Guardrails held: `live_list.html` / `together_list.html` byte-for-byte unchanged (their owner labels grow only via the shared `.card__owner` rule); `.card__pin`, `.card__remote`, `.live-badge`, `.live-online`, `.together-badge` and `.live-room-card__owner` (0.8 rem) untouched.
- Manual browser pass (dark + light, 2-column mobile): still to be done by the owner — comment bubble sits in the cover's bottom-right corner on a normal card, a pinned card and a pinned+remote card (no three-badge collision); 1 rem username reads clearly beside the 64 px avatar on home, live and together lists.

**Date:** 2026-10-06 (sheet) / landed 2026-10-06 18:00
**Source:** `documents/code_review_2026-10-06_175116.md`

## Change Log — server-side permission-bit fix (Session: 2026-10-06 18:14)

Owner request (this session, chat): on the VPS, `start.sh` is invoked as
`./start.sh` but is committed with mode `100644`, so the operator must `chmod +x`
it after every deploy. The local mode change then (a) makes
`git pull --ff-only` fail on the next startup ("local changes"), and
(b) when a content update does land, git rewrites the file at mode `644`,
stripping the `+x` — the second startup fails with `Permission denied`.

Fix: make the repository the source of truth for the exec bit, and make the
pull tolerant of mode-only local edits.

**Landed:** 2026-10-06 18:15 — committed and pushed as `d3a6eb2` (`start.sh`: mode
`100644` → `100755` + `core.fileMode false` guard in step 1). Verification:
`bash -n` on the script passes (git-bash); `git ls-files -s start.sh` shows
`100755`; `git push origin master` succeeded (`ae450a7..d3a6eb2`). On the VPS the
next `./start.sh` pulls, keeps the exec bit, and rewrites at `755` on any update
— no more manual `chmod`, no more mode-change blocking `git pull --ff-only`.

| # | File(s) | Change | Why | Blast radius |
|---|---------|--------|-----|--------------|
| 5.1 | `start.sh` | In step 1, run `git config core.fileMode false` before `git pull --ff-only` (local repo setting, idempotent); update the header comment | Mode-only local edits (e.g. a hand `chmod +x` on any file) no longer block `git pull --ff-only` or show up as uncommitted changes on the VPS | deployment only (script runs before anything else) |
| 5.2 | `start.sh` | File mode `100644` → `100755` (`git update-index --chmod=+x start.sh`), so the committed tree already carries the exec bit the VPS needs | `./start.sh` works right after any `git pull`/rewrite without a manual `chmod`; the mode now survives content rewrites | deployment only |

**Verification:** `git diff --cached --stat` shows the `start.sh` mode change
(`old mode 100644` / `new mode 100755`) plus the one-line content edit;
`bash -n start.sh` syntax check; on the VPS after push: `./start.sh` starts the
app with no `Permission denied`, and re-running it after a further commit still
succeeds (exec bit preserved, no mode-change noise in `git status`).

---

## 21. Message notification system (Session: 2026-10-06)

> Requirements: `documents/requirements_notification_system_2026-10-06_194029.md`
> (approved; decisions locked with the user on 2026-10-06). Users are notified
> when (a) someone comments on a video they uploaded, or (b) someone replies to
> their comment. A per-user SSE channel (reusing the `presence.py` fan-out
> pattern) pushes events in real time; a red dot on the bottom-left avatar chip
> + a dropdown panel (recent items) + a dedicated `/notifications` page surface
> them. Opening either view marks **all** notifications read (all tabs sync via
> the `unread-cleared` SSE event). The `type` column is the extensibility axis
> for future kinds (live chat, likes, follows, …); the data model, SSE channel,
> and UI render whatever `type` arrives. Self-events are never notified.

### Change Log (Session: 2026-10-06)

**Landed:** 2026-10-06 20:06 — all items N.1–N.14 implemented and verified.
Note on the reply trigger: a reply fires only `comment_reply` (to the parent's
author) and never also a `video_comment` for the video owner, so the owner is
not pinged twice for the same reply.

| # | File(s) | Change | Why | Blast radius |
|---|---------|--------|-----|--------------|
| N.1 | `app/sql/schema.sql` | `notifications` table (`user_id`, `type`, `actor_id` + denormalized `actor_username` / `actor_avatar_filename` snapshots, `video_id` + `video_title` snapshot, `comment_id`, `data` JSON, `read_at`) + `idx_notifications_user` + partial `idx_notifications_unread`; FKs: user/video `ON DELETE CASCADE`, actor `ON DELETE SET NULL` | §21.4 data model | fresh DB boot only |
| N.2 | `app/sql/migrations/create_notifications.sql` (new) | the same table + indexes as an idempotent DDL script | upgrades live DBs without data loss | db init only |
| N.3 | `app/sql/notifications/{insert,list_by_user,unread_count,mark_all_read}.sql` (new) | the four CRUD queries (`list_by_user` carries a `{limit}` placeholder, newest first) | §21.1 SQL location rule (no hard-coded SQL in code) | db layer only |
| N.4 | `app/db.py` | `add_notification(user_id, type, actor, video, comment_id=None, data=None)`, `list_notifications`, `unread_count`, `mark_all_read`; `_migrate` runs `create_notifications` via `executescript` (fresh DBs get it from schema.sql) | §21.2 DB layer | db layer (additive) |
| N.5 | `app/notify.py` (new) | per-user SSE fan-out mirroring `presence.py`: `user_id -> list[queue.Queue]` under a module lock, one queue per open connection, unbounded queues, `stream()` with a 15 s `: hb` heartbeat (removed on generator close), `broadcast()`, `online_count()` | §21.3 real-time delivery; no room/membership semantics | new module |
| N.6 | `app/routes/notify.py` (new) | `notify_bp` (prefix `/notify`): `GET /notify/stream` (SSE, `@login_required`), `GET /notify/api/list` (`?limit=`, default 20; `{ok, unread, items[]}`), `POST /notify/api/read` (mark-all-read + broadcast `unread-cleared`); `notify_page_bp`: `GET /notifications` (100 newest, mark-all-read on load, `unread-cleared` broadcast); shared `item_from_parts` / `item_from_row` shapes + `push_new` / `push_unread_cleared` helpers for the trigger code | §21.4 endpoints | new blueprints |
| N.7 | `app/__init__.py` | register `notify_bp` + `notify_page_bp`; bake `unread_notifications` into `inject_globals` (one cheap indexed query per render — dot state at page load); add `static/js/notify.js` to the `_static_version()` hash list | §21.4/§21.6.4 wiring | app factory |
| N.8 | `app/routes/watch.py` | `add_comment` notification trigger: top-level comment → `video_comment` for the video owner (skip when owner == author); reply → `comment_reply` for the parent's author (skip when replier == author); `notify` broadcast in both cases; whole block `try/except` so a failure never breaks comment posting (logged, comment still returned) | §21.5 trigger hook | comment posting only |
| N.9 | `app/templates/base.html` | the bottom-left Account link becomes a user **chip** (avatar + username label, red dot baked from `unread_notifications`); hover (desktop) / click (touch) reveals a dropdown with exactly two entries — **Notifications** (dot) + **My Account** (`auth.account`); dropdown + recent-items panel markup (`#userchip-dropdown`, `#notify-panel`) on `body` so the panel can anchor fully on-screen even in the collapsed-rail state; loads `notify.js` | §21.6.1–6.2 UI | every logged-in page |
| N.10 | `app/templates/notifications.html` (new) | dedicated page: the 100 newest notifications with the same row layout as the panel (actor avatar/name, per-type one-line text, local-time timestamp, unread marker), empty state “No notifications yet.”; each row deep-links to `/watch/<id>#c<comment_id>` | §21.6.3 page | new page |
| N.11 | `app/static/js/notify.js` (new) | logged-in pages only: `EventSource` on `/notify/stream` (`notification` → dot + prepend to the open panel + `svsToast`; `unread-cleared` → hide dot + clear open-panel markers); dropdown open/close (hover grace + click toggle + outside-click/Esc); panel fetch of `/notify/api/list?limit=20` + immediate `POST /notify/api/read` on open; row click → `/watch/<id>#c<id>`; local-time rendering for `[data-ts]` | §21.6.2/6.4 real-time badge | every logged-in page |
| N.12 | `app/static/css/style.css` | `--notify-dot` token (dark + light); chip avatar dot, dropdown + panel (fixed-positioned, anchored to the chip, theme-aware surfaces), row layout + unread marker + page list, empty state, and the watch-page `comment--flash` highlight keyframes | §21.6 styles | global CSS (additive) |
| N.13 | `app/templates/watch.html` | small script: a `#c<id>` hash (from a notification click) scrolls to `[data-comment-id="<id>"]` and flashes the `comment--flash` highlight; best effort (deleted comment → lands on the list) | §21.6.2 deep-link | watch page only |
| N.14 | `testing/probe_notifications.py` (new) | end-to-end probe against the dev DB: schema/index presence + idempotent DDL on a scratch DB; comment trigger → owner notified (DB + `/notify/api/list` + baked dot on a fresh render); self-comment → no notification; reply → parent's author notified (`comment_reply`); SSE push of `notification` + `unread-cleared` on an open stream; `/notify/api/read` + `/notifications` page mark-all-read; guests see no chip and `/notify/*` redirects to login; page empty state; cleanup leaves no residue | §21.9 acceptance criteria | testing only |

**Verification (2026-10-06 20:06):**
- `python testing/probe_notifications.py` — all checks pass: schema + both indexes present, idempotent DDL, `video_comment` trigger (DB row + list API + baked dot), self-events create nothing, `comment_reply` trigger (no duplicate `video_comment`), SSE `notification` + `unread-cleared` delivered on an open stream, page + `/notify/api/read` mark-all-read with the dot gone on the next render, empty state, guest redirects, clean DB afterwards.
- `python testing/probe_home_cards.py`, `probe_home_covers.py`, `probe_no_reload.py` — all pass unmodified (no regressions from the base.html chip / CSS / static-version changes).
- `py_compile` on all touched modules: clean; `pyright app`: 0 errors / 0 warnings.
- Manual browser pass (hover dropdown, panel, live dot, cross-tab sync, `/notifications` page, watch-page deep-link flash, collapsed rail + mobile drawer positioning): still to be done by the owner.

**Date:** 2026-10-06 (sheet) / landed 2026-10-06 20:06
**Source:** `documents/requirements_notification_system_2026-10-06_194029.md`

## 21.1 Change Log: user-chip dropdown never opened; chip rendered as a primary button (Session: 2026-10-06)

**Landed 2026-10-06 20:26** — both fixes below are implemented and verified.

| # | File | Change | Why | Blast radius |
| --- | --- | --- | --- | --- |
| B.1 | `app/templates/base.html` | moved the `notify.js` `<script>` tag from before the `#userchip-dropdown` / `#notify-panel` markup to immediately after it (still after `async.js`, so `svsToast` stays available) | The script ran while the dropdown/panel elements did not exist yet, so `getElementById('userchip-dropdown')` returned null and the whole IIFE bailed out early — no listeners bound, hence hover/click opened nothing (reproduced headless: chip present, no JS errors, dropdown stays hidden) | every logged-in page |
| B.2 | `app/templates/base.html` | the user chip keeps `tabindex="0"` + the Enter/Space handler but drops `role="button"` and gains `aria-haspopup="menu"` | Pico CSS styles `[role=button]` as a **primary button** (`--pico-background-color: var(--pico-primary-background)`), so the chip was permanently painted with the blue button fill ("always selected" look); confirmed headless: computed background `rgb(1, 114, 173)` = `#0172ad` | sidebar only |

**Verification (2026-10-06 20:26):**
- Root causes reproduced headless (puppeteer-core against the live dev server with a real signed session cookie): the chip computed background was Pico's primary blue and the dropdown stayed `hidden` after both a real pointer hover and a click — i.e. `notify.js` had bailed out on the missing dropdown node.
- After B.1 + B.2: the dev server was restarted (template cache, `debug=false`); headless re-probe shows the synthetic `mouseenter` and the chip click both reveal the dropdown anchored to the chip's right edge, clicking "Notifications" opens the panel with the live `/notify/api/list` rows (deep links `/watch/<id>#c<id>`), and the chip's computed background is no longer the Pico button fill (screenshot confirms the plain avatar + label).
- `python testing/probe_notifications.py` still passes (no behavioral change to the backend).
- Owner: hard-refresh the browser (the fix is server-rendered HTML); the red-dot SSE feed re-attaches on reload.

## 22. Offline red-dot guarantee + SSE reconnect resync (Session: 2026-10-08)

> Requirements: `documents/requirements_offline_red_dot_2026-10-08_213716.md`
> (created 2026-10-08 21:37:16, system time; decisions locked with the user on 2026-10-08).
>
> Locked interpretation of "收到 notifications even offline": a user who received
> notifications while offline (no open site tab / no SSE connection) must see the
> in-page **red dot** on the **next page they open** — no external delivery channels
> of any kind (no browser Notification API, no Web Push / service worker, no email).
> Audit result: the §21 system already guarantees the offline→online red dot via the
> `notifications` table + the render-time `unread_notifications` bake (`base.html`
> chip/dropdown dots fed by the `inject_globals` context processor). Exactly one real
> gap was found: a long-lived tab whose SSE feed dropped and auto-reconnected (server
> restart, network blip) silently loses `notification` events broadcast during the
> disconnect window (they are never replayed), so the in-memory dot state desyncs
> until a full page reload.
>
> Contract (documented + patched):
> 1. **Offline guarantee (locked requirement, existing behavior):** a notification
>    arriving while the user has zero open SSE connections is persisted and lights
>    the red dot on the user's next page render.
> 2. **Reconnect resync (new):** on **every** (re)establishment of the
>    `/notify/stream` EventSource — the initial `open` and each automatic
>    reconnect — the client fetches `/notify/api/list?limit=1` and aligns the dot
>    to the returned `unread` count. One tiny GET per reconnect, no polling loop;
>    the resync only updates the dot, never re-renders an open panel.

### Change Log (Session: 2026-10-08)

**Landed:** 2026-10-08 21:42 — all items O.1–O.3 implemented and verified (2026-10-08 21:42:56, system time).

| # | File(s) | Change | Why | Blast radius |
|---|---------|--------|-----|--------------|
| O.1 | `app/static/js/notify.js` | the feed's `EventSource` gains an `open` listener: on every (re)connect it fetches `/notify/api/list?limit=1` and calls `setDot(unread > 0)`; new `resyncUrl` constant; the resync only touches the dot, never the open panel | closes the stale-dot window left by `notification` events broadcast while the SSE connection was down (server restart / network blip) — the offline red-dot guarantee now also holds for long-lived tabs; the static-version hash over `notify.js` in `create_app` re-busts the cache automatically | client on every logged-in page; one extra tiny GET per reconnect |
| O.2 | `testing/probe_offline_red_dot.py` (new) | probe: with no open stream (offline) a comment is persisted and the dot is baked on A's next render; `/notify/api/list?limit=1` returns the exact resync payload shape; reconnect simulation (close stream → comment → reopen) proves the missed event is not replayed but the resync endpoint reports the unread; after mark-read no dot is baked | §22 acceptance criteria | testing only |
| O.3 | `documents/requirements_offline_red_dot_2026-10-08_213716.md` (new) | session deliverable: timestamped requirements + design document for the offline red-dot guarantee (locked decisions, audit summary, dot-state model, resync design, edge cases, acceptance criteria, assumptions) | documentation-first rule (§12) | docs only |

**Verification (2026-10-08 21:42):**
- `python testing/probe_offline_red_dot.py` — all four acceptance checks pass: offline comment (zero open streams) persists + dot baked on the next render; `/notify/api/list?limit=1` returns the exact `{ok, unread, items}` shape the `open` handler consumes; reconnect simulation shows the gap event is NOT replayed on the reopened stream while the resync endpoint reports `unread: 2` (the dot lights without a reload); after `POST /notify/api/read` a fresh render bakes no dot and the resync payload reports zero unread.
- `python testing/probe_notifications.py` — full §21 regression passes unmodified (triggers, self-event skip, SSE `notification` / `unread-cleared`, page + API mark-read, guest redirects).
- `node --check app/static/js/notify.js` — JS syntax clean; `python -m py_compile testing/probe_offline_red_dot.py` clean.
- App boot smoke: `create_app()` builds with all 10 blueprints (exercised by both probes).
- Owner (manual): keep a tab open, restart the dev server, comment from a second account — the dot should light on the first tab without a reload (scenario 3 of the real browser; the probe covers the same sequence server-side).



## 23. Live playback stability and diagnostics (Session: 2026-10-28)

**Confirmed scope:** the user approved the detailed player proposal in chat and
provided this local project for implementation. Changes are local working-tree
changes; deployment and Git commits are outside this instruction.

Requirements recorded before implementation:
- Keep DPlayer and the bundled flv.js, existing stream URLs, tokens, and codecs.
- Replace periodic hard seeks to 0.5 seconds behind the buffer end with a
  hysteretic catch-up controller: start 1.03–1.05x only after >3 seconds of
  continuous buffered headroom persists, return to 1x around 2 seconds, and
  stop chasing while paused, waiting, seeking, hidden, or nearly out of buffer.
- Hard seeks are exceptional: >10 seconds of sustained backlog, a cooldown,
  a destination inside the usable continuous range, and ~1.5 seconds headroom.
- Do not add a fixed startup wait or enlarge the default IO stash to force delay.
- Rebuild only after explicit network failures, with bounded exponential retry
  backoff. Respect pause, offline and hidden-tab states; avoid retry loops for
  unsupported codecs/media errors and clean up old players/listeners/timers.
- Record bounded, local-only diagnostics for buffering, seeks, reconnects,
  buffered headroom and decoded/dropped frames, without URLs or credentials.
- Put verification scripts under testing/, exercise the controller with a fake
  clock/media element, and check the rendered Flask/Jinja integration.
- All new code, comments, documentation and UI messages are English.

Status: implemented locally. Deterministic and local real-browser checks passed
(see below). Remote deployment and real-route live verification remain pending.

Implementation and verification:
- Added app/static/js/live-player.js and wired it into the existing live template.
- Added its bytes to the existing startup static-version hash in app/__init__.py.
- Implemented the policy, bounded network-error recovery and local diagnostics
  described above; credentials and runtime storage were not edited.
- 24 deterministic controller cases passed; isolated Flask/Jinja rendering,
  inline JavaScript syntax, py_compile and Pyright passed (0 errors/warnings).
- Real headless Chrome decoded a local H.264/AAC fixture and recovered from an
  intentionally broken stream socket; pause/resume and cleanup also passed.
- No server deployment or real Guangdong-route playback test was performed.
- Operational details and verification commands: documents/LIVE_PLAYER_STABILITY.md.

## 24. AV1 live playback via Enhanced FLV (Session: 2026-10-28)

**Confirmed scope:** the user explicitly requested local AV1 live-room support
following the read-only compatibility audit. This supersedes section 23's choice
of flv.js, while retaining its stability policy and local-only deployment scope.

Requirements recorded before implementation:
- Keep DPlayer, ordinary MP4/video playback, current stream URLs and tokens.
- Vendor a pinned official mpegts.js release with Enhanced FLV AV1 support and
  its license; serve it locally, without a runtime CDN dependency.
- Adapt the existing live controller to mpegts.js and explicitly disable its
  built-in latency chasing so only one catch-up controller runs.
- Retain H.264 compatibility, bounded network retries, cleanup and diagnostics.
- Expose safe codec metadata in diagnostics and a useful unsupported-codec
  message; do not silently promise AV1 support on every browser/device.
- Validate AV1 and H.264 using local software-generated fixtures and real
  browser MSE playback. Do not change or test-publish to the active HK stream.
- Keep all verification scripts under testing/. Do not edit credentials/,
  stage changes, create commits or deploy remotely.

Status: implemented locally; remote deployment and a real NVENC stream remain pending.

Compatibility finding before the dependency fix:
- The latest official mpegts.js 1.8.2 release still rejects valid Enhanced FLV
  AV1 video-metadata packets (packet type 4) emitted by the local FFmpeg fixture.
- Pin 1.8.2 and retain a documented, reproducible vendor patch that skips this
  optional metadata packet instead of rejecting the stream. Sequence headers
  and coded frames must remain unchanged. Validate the metadata-bearing AV1
  fixture, H.264 regression and both failure/recovery paths before completion.
- A second upstream AV1 issue surfaced after successful realtime decoding:
  MEDIA_INFO was not emitted once dimensions became available. The vendored
  compatibility fix will emit complete media information after AV1 frame-header
  parsing, and remove upstream per-keyframe/OBU console debug output.
- The local AV1 fixture uses realtime zero-lookahead encoding to represent the
  live workflow; this does not expand support to every offline AV1 prediction
  pattern or HDR profile.


Section 24 verification results:
- Pinned official mpegts.js 1.8.2 with npm integrity verification, original and
  vendored SHA-256 hashes, licenses and a reproducible compatibility script.
- 26 controller tests, template rendering, JavaScript/Python syntax and Pyright
  passed (0 errors/warnings).
- Actual Chrome decoded AV1 Enhanced FLV/AAC (av01.0.00M.08, 79 decoded frames
  after reconnect) and H.264/AAC (avc1.42c014, 84 frames), including optional AV1
  metadata, a broken local transport, recovery, pause/resume and cleanup.
- Ordinary MP4 playback and active production ingest were not changed.
- See documents/LIVE_AV1_SUPPORT.md and app/static/vendor/mpegts/README.md.


## 25. Faster live catch-up (2026-10-28)

Confirmed by the user before implementation: retain the >3 second backlog
sustained for 3 seconds trigger; use 1.1x up to 5 seconds, 1.2x above
5 seconds, and 1.25x above 8 seconds. Return to 1x at <=2 seconds.
Retain low-buffer/paused/hidden guards and the existing >10 second
guarded seek with a 30 second cooldown. Local changes only.

Verification: all 27 controller regression checks passed, including exact speed-tier boundaries, low-buffer guards and seek cooldown. JavaScript syntax and isolated template/cache invalidation checks passed. No remote deployment performed.

## 26. Resumable uploads + lower-failure transfer path (Session: 2026-11-02)

**Confirmed scope:** improve upload reliability for large files and unstable
links, with resumable transfers and fewer gateway failures, while keeping the
existing upload UX and metadata flow.

Requirements recorded before implementation:
- Keep the existing `/upload/seg/start`, `/upload/seg/part`, `/upload/seg/finish`
  flow and ownership checks.
- Add resumable segmented upload across transient failures and server restarts:
  persist session metadata on disk; allow clients to pass a resume token and
  skip already-uploaded parts.
- Add bounded retry for transient upload failures (network / 5xx / 502 class)
  on segmented start/part/finish requests.
- Reduce single-request failure exposure by preferring segmented upload earlier,
  and auto-fallback from single POST to segmented upload when retriable errors
  happen.
- Keep documentation aligned in README (EN/zh-CN/ja).

Status: implemented locally. Remote deployment/production verification pending.

Implementation:
- `app/routes/upload.py`
  - segmented session metadata now persists at
    `videos/.segments/<token>/session.json`.
  - `seg_start` accepts `resume_token` and returns `uploaded_parts` for resume.
  - `seg_part` validates part index range against `total_parts`.
  - `seg_finish` reports missing-part state without destroying resumable state;
    successful finalize cleans up the persisted session.
- `app/static/js/pipeline.js` (main upload pipeline)
  - segmented threshold lowered from 1 GB to 256 MB (prefer chunked path).
  - added retriable upload wrapper with bounded backoff.
  - added local resume-token persistence and resume handshake with server.
  - single-upload retriable failure now auto-falls back to segmented upload.
- `app/static/js/edit.js` (clip/edit page)
  - mirrored the same retry/resume/fallback behavior as `pipeline.js`.
- `README.md`, `README.zh-CN.md`, `README.ja.md`
  - feature text updated to describe resilient resumable/retry segmented upload.

Verification (local):
- `python -m py_compile app/routes/upload.py` — pass.
- `node --check app/static/js/pipeline.js` — pass.
- `node --check app/static/js/edit.js` — pass.

Notes:
- Existing section/part size policy was tightened to `SEGMENT_SIZE = 64 MB` so
  each multipart request stays comfortably below the Apache reverse-proxy body
  limits; existing `videos/.segments/` storage location is reused.

## 27. Manual retry after failed upload stage (Session: 2026-11-02)

**Confirmed scope:** when the browser has already finished probing/transcoding and
fails during the actual upload request, the user can retry that upload without
refreshing the page or re-running the transcode step.

Status: implemented locally.

Implementation:
- `app/static/js/upload.js`
  - each queued item now keeps the prepared upload blob + metadata.
  - failed upload-stage items show a per-file `Retry` button and reuse the same
    transcoded output for the retry.
- `app/static/js/edit.js`
  - the single-file upload page now shows a `Retry upload` button when the final
    upload stage fails, reusing the prepared Blob and cover instead of forcing a
    full page reload or transcode rerun.
- `app/static/css/style.css`
  - added compact styles for the retry button in queue and progress states.

Verification:
- `node --check app/static/js/upload.js` — pass.
- `node --check app/static/js/edit.js` — pass.

## 28. Hardening batch - CSRF, session cookies, dependency pins, single-process guard, probe runner (Session: 2026-11-04 13:54:07)

**Confirmed scope (owner chat, 2026-11-04):** apply five findings from the
2026-11-04 project review. Owner decision recorded in chat for item 2: bring
**only** `documents/PROJECT_PROGRESS.md` under git; the rest of `documents/` and
all of `testing/` stay ignored.

### Pending Changes

| # | File(s) | Change | Why | Blast radius |
|---|---------|--------|-----|--------------|
| 28.1 | `app/csrf.py` (new) | Per-session CSRF token (`secrets.token_hex(32)`, session key `_csrf`); `csrf_field()` for templates; `protect()` as a `before_request` hook that rejects unsafe methods (POST/PUT/PATCH/DELETE) missing `_csrf` (form field) or `X-CSRF-Token` (header); 400 JSON for AJAX, flash + redirect for plain form posts | The repo has 0 CSRF tokens across 29 `<form>` tags, 8 `fetch` POST call sites and 2 XHR upload call sites; every state-changing POST is cookie-authenticated with no token | every POST endpoint |
| 28.2 | `app/static/js/csrf.js` (new) + `app/templates/base.html` | `<meta name="csrf-token">` in `<head>`; patch `window.fetch` and `XMLHttpRequest` so every same-origin unsafe request carries `X-CSRF-Token` | One hook covers all present and future AJAX call sites instead of editing 10 JS call sites by hand | every page (JS) |
| 28.3 | 27 templates (every `<form method="post">`) | Insert `{{ csrf_field() }}` as the first child of each POST form | Native (non-AJAX) form posts must submit the token as a field | all forms |
| 28.4 | `app/__init__.py`, `app/config.py`, `config.example.json` | `SESSION_COOKIE_HTTPONLY=True`, `SESSION_COOKIE_SAMESITE="lax"`, `SESSION_COOKIE_SECURE=bool(cfg.get("cookie_secure", True))`; log the effective value at startup; add `cookie_secure` to `DEFAULTS` and the example config | The cookie flags were never set explicitly (Flask default `SESSION_COOKIE_SAMESITE` is None, i.e. browser-dependent). Loopback dev keeps working because browsers treat `http://127.0.0.1` as a secure context | session handling |
| 28.5 | `.gitignore` | Re-include `documents/PROJECT_PROGRESS.md` after the `documents/*` rule | The doc-first Prime Directive currently keeps its single source of truth out of version control | git bookkeeping only |
| 28.6 | `requirements.txt`, `requirements-dev.txt` (new) | Pin the 7 direct deps to the versions proven in `.venv` (Flask 3.1.3, PyDrive2 1.21.3, waitress 3.0.2, google-api-python-client 2.201.0, google-auth-httplib2 0.4.4, httplib2 0.32.0, oauth2client 4.1.3); pin pyright 1.1.414 in the dev file | Unpinned deps make the VPS deploy (which runs `pip install -r requirements.txt` on every `start.sh`) non-reproducible | dependency install |
| 28.7 | `.gitignore`, `app/__init__.py`, README (EN/ja/zh-CN) | Keep `app/static/js/ffmpeg/ffmpeg-core.wasm` (32 MB, Stage-2 fallback only) out of git; `create_app` logs a warning when it is absent; README documents the one-time copy | A 32 MB binary in git slows every clone and every boot-time `git pull`, while mediabunny (WebCodecs) is the primary engine | upload fallback path only |
| 28.8 | `app/runtime_guard.py` (new) | Startup advisory check: env markers (`WEB_CONCURRENCY`, `GUNICORN_CMD_ARGS`, `UVICORN_WORKERS`) plus a heartbeat lock file `storage/.app.lock` (JSON pid, mtime refreshed every 10 s, treated stale after 45 s) that logs CRITICAL when a second live instance shares the storage dir | `presence.py` rooms, `drive_worker` progress, the watch-room scheduler and `_seg_sessions` are process-memory only; a multi-worker deployment silently corrupts online counts, upload progress and watch-together sync | startup only |
| 28.9 | `app/db.py` | `_ALLOWED_UPDATE_COLUMNS` whitelist for `users`/`videos` + a `_set_columns()` helper used by `update_user`/`update_video` | `", ".join(f"{k} = ?" for k in kwargs)` interpolates column names straight out of kwargs; safe today only because every caller hard-codes them - one careless caller away from SQL injection | db write layer |
| 28.10 | `testing/run_probes.py` (new) | Discover and run `testing/probe_*.py` and `testing/smoke_*.py` with a per-probe timeout, a captured output tail, a summary table, and a non-zero exit when any probe fails; flags `--list`, `--only`, `--timeout`, `--with-node` | 17 regression probes exist but today each must be launched by hand | testing only |
| 28.11 | `.clinerules/AGENTS.md` | Replace the stale verification block (`python -c "import main; main.build_gui()"`, "expect 324 blocks" - copied from an unrelated GUI project) with this project's real commands | The file actively misleads anyone verifying a change, including a fresh agent run | docs only |
| 28.12 | `start.sh` | Update the pull comment so it no longer claims `documents/PROJECT_PROGRESS.md` is git-ignored (28.5 made it tracked) and warns that a hand-edited copy on the VPS must be moved aside before the first pull that tracks it | The comment would contradict the new `.gitignore` for the person deploying the change | comments only |
| 28.13 | `README.zh-CN.md` | Same Chinese block as the EN/ja READMEs (single-process rule, probe runner, CSRF/cookie/pin notes, ffmpeg.wasm copy). The first attempt appended the Japanese text by mistake, so the file was truncated back to its original content, re-appended in Chinese, and verified to contain no kana code points | The zh-CN README would otherwise be half Japanese | docs only |

**Accepted risk (documented, not fixed in this batch):** `/auth/logout` stays a
GET link, so a cross-site forced logout is possible. It is an annoyance, not a
data risk, and SameSite=Lax already blocks cross-site unsafe requests; turning
the sidebar chip's logout into a POST form is deferred to its own entry.

**Status:** implemented and verified locally on 2026-11-04 14:22:44; the working
tree is left uncommitted for the owner (project rule: the agent never commits).

### Change Log

#### 28.1-28.3 CSRF guard - implemented 2026-11-04 14:22:44

- `app/csrf.py` (new, 131 lines): `token_for_session()` lazily creates a
  `secrets.token_hex(32)` under the session key `_csrf`; `csrf_field()` returns
  the hidden input; `protect()` runs as a `before_request` hook and rejects
  POST/PUT/PATCH/DELETE when neither the `_csrf` form field nor the
  `X-CSRF-Token` header matches. The header is read **before** the body so a
  multipart upload is never parsed twice. AJAX requests (`X-Requested-With` or
  `Accept: application/json`) get a 400 JSON body, plain form posts get a flash
  message plus redirect back. The hook is skipped when
  `app.config["TESTING"]` is true - every existing probe posts without a token
  and would otherwise fail 16 tests; `testing/probe_csrf.py` covers the guard
  itself in non-TESTING mode instead.
- `app/static/js/csrf.js` (new): reads `<meta name="csrf-token">`, patches
  `window.fetch` and `XMLHttpRequest.prototype.open/send`, and adds
  `X-CSRF-Token` only to unsafe **same-origin** requests, so third-party calls
  (Drive, analytics) never receive the session token. Exposes
  `window.SVS_CSRF = { token, field }` for page scripts.
- `app/templates/base.html`: added the meta tag plus the `csrf.js` script tag
  (versioned through `static_version`) before `inpage.js`. `csrf_token` is
  injected as a **string** context global, so templates use `{{ csrf_token }}`
  and not `{{ csrf_token() }}`.
- 27 `<form method="post">` tags across 16 templates now carry
  `{{ csrf_field() }}` as their first child. `live_view.html` and
  `together_view.html` have no POST form (their requests are built in JS) and
  are covered by the header patch; `async.js` builds `new FormData(form)` so it
  also picks up the hidden field automatically.

#### 28.4 Session cookie flags - implemented 2026-11-04 14:22:44

- `app/__init__.py`: `SESSION_COOKIE_HTTPONLY=True`,
  `SESSION_COOKIE_SAMESITE="lax"`,
  `SESSION_COOKIE_SECURE=bool(cfg.get("cookie_secure", True))`, and the
  effective combination is logged once at startup.
- `app/config.py` `DEFAULTS` and `config.example.json` gained `cookie_secure`
  (default `true`). Existing `config.json` files keep working: the key is
  optional and defaults to secure.

#### 28.5, 28.6, 28.7 Dependency pins and the 32 MB asset - implemented 2026-11-04 14:22:44

- `.gitignore`: `documents/*` is now followed by `!documents/PROJECT_PROGRESS.md`
  so the doc-first source of truth is versioned; `testing/` and the other
  documents stay ignored. `app/static/js/ffmpeg/ffmpeg-core.wasm` is ignored.
- `requirements.txt` pins Flask 3.1.3, PyDrive2 1.21.3, waitress 3.0.2,
  google-api-python-client 2.201.0, google-auth-httplib2 0.4.4, httplib2 0.32.0,
  oauth2client 4.1.3. `requirements-dev.txt` (new) pulls `requirements.txt` and
  pins pyright 1.1.414; `start.sh` never installs the dev file.
- `app/__init__.py` logs one WARNING when `ffmpeg-core.wasm` is missing (the
  mediabunny/WebCodecs path is unaffected), and the file joined the
  `_static_version()` content hash so cache-busting follows the asset.
- `README.md`, `README.ja.md`, `README.zh-CN.md` gained the single-process note,
  the probe-runner command, the security/dependency notes and the one-time
  `cp /somewhere/ffmpeg-core.wasm ...` instruction.

#### 28.8 Single-process guard - implemented 2026-11-04 14:22:44

- `app/runtime_guard.py` (new): `check(storage_dir)` looks for multi-worker
  launch markers (`WEB_CONCURRENCY`, `UVICORN_WORKERS`, `GUNICORN_CMD_ARGS`) and
  maintains `storage/.app.lock` (JSON pid/start time, mtime refreshed every
  10 s, considered stale after 45 s). A second live pid logs CRITICAL. It is
  advisory only - no `os.kill`, no startup abort - and cleans the lock via
  `atexit`. `create_app` logs every returned warning.

#### 28.9 Update-column whitelist - implemented 2026-11-04 14:22:44

- `app/db.py`: `_ALLOWED_UPDATE_COLUMNS` for `users` and `videos` plus
  `_set_columns(table, kwargs)`, which raises `ValueError` before any SQL is
  built when a caller passes an unknown column name. `update_user` and
  `update_video` now build their `SET` clause through it.

#### 28.10 Probe runner - implemented 2026-11-04 14:22:44

- `testing/run_probes.py` (new): discovers `probe_*` / `smoke_*`, runs each in
  its own subprocess with `cwd` set to the project root, captures stdout+stderr,
  prints a PASS/FAIL table with per-probe seconds, tails the output of failures,
  and exits non-zero if anything failed. Flags: `--list`, `--only <substring>`,
  `--timeout <seconds>` (default 150), `--tail <lines>`, `--with-node` (adds the
  `*.cjs` headless-browser probes when `node` is on PATH).
- 11 probes that were missing `app.config["TESTING"] = True` got it inserted
  (admin_logs, admin_pages, base_path, cache_flow, home_cards, no_reload,
  notifications, offline_red_dot, req19, tag_routes, upload_bitrate) so they stay
  independent of the CSRF guard.

#### 28.11-28.13 Docs and comments - implemented 2026-11-04 14:22:44

- `.clinerules/AGENTS.md`: the "Verification workflow" block no longer tells
  anyone to run `main.build_gui()` / "expect 324 blocks" (left over from an
  unrelated GUI project). It now lists `py_compile`, `compileall`, `pyright`
  (with the `requirements-dev.txt` install note), `testing\run_probes.py` and its
  flags, the `probe_csrf.py`/TESTING caveat, the temp-`.py` habit, and the fact
  that no Python probe renders a browser page.
- `start.sh`: the pull comment no longer claims
  `documents/PROJECT_PROGRESS.md` is git-ignored; it now warns that the tracked
  copy on the VPS must be committed or moved aside before the first pull,
  otherwise `git pull --ff-only` aborts and the script exits 1.
- `README.zh-CN.md`: rebuilt from its original Chinese content plus the Chinese
  equivalent of the new EN/ja sections.

### Verification (all commands run 2026-11-04 from the project venv)

- `python -m compileall -q app main.py wsgi.py` - exit 0.
- `python -m pyright app testing\probe_csrf.py testing\run_probes.py` -
  `0 errors, 0 warnings, 0 informations`.
- `python testing\probe_csrf.py` - `ALL CSRF CHECKS PASSED`: a POST without a
  token is rejected (JSON 400 for the AJAX variant, flash+redirect for the form
  variant), a wrong token is rejected, a correct form field and a correct
  header are both accepted, and GET requests are untouched.
- `python testing\run_probes.py --timeout 90` - **16/16 probes passed**
  (slowest: `probe_offline_red_dot.py` 31.5 s, `probe_notifications.py` 16.5 s,
  everything else under 2 s). The `*.cjs` probes were not run here; they need
  Chrome and network, and `--with-node` is the switch for them.
- `README.zh-CN.md` re-read programmatically: 0 kana-range code points, 66 lines,
  UTF-8 without BOM; `README.md` and `README.ja.md` unchanged and consistent.
- Not verified by a probe (needs a human browser pass): the visual result of the
  new meta/script tags in `base.html`, and the flash+redirect path when a form
  is submitted with a stale session.

### Deferred / accepted in this batch

- `/auth/logout` stays a GET link (cross-site forced logout = annoyance, not a
  data risk; SameSite=Lax already blocks cross-site unsafe methods).
- Probes run with `TESTING=True`, so the CSRF guard is only exercised by
  `probe_csrf.py`; converting the other probes to token-carrying posts is a
  separate entry.
- Git bookkeeping is the owner's, by rule: `git add documents/PROJECT_PROGRESS.md`
  and `git rm --cached app/static/js/ffmpeg/ffmpeg-core.wasm` (the local 32 MB
  file stays in place).

## 29. Research - article-style video optimisation, and can the VPS split existing videos? (Session: 2026-11-04 14:24:55)

**Owner question (chat, 2026-11-04):** are the techniques in
<https://alienzhou.github.io/fe-performance-journey/5-subresources/video.html>
worth applying here, and if videos need splitting, does
`ssh vsakuya@hk.vsakuya.xyz` have enough performance to split the existing
library? **No code was changed in this section** - measurements, one defect
found, and proposals awaiting approval.

### 29.0 The VPS, as measured (2026-11-04, via ssh)

- `Linux vsakuya-hk-server`, Ubuntu, kernel 7.0.0-38, **1 vCPU** Intel Xeon
  E5-2696 v3 @ 2.30 GHz (Haswell: SSE4.2/AES/AVX/AVX2, **no AVX-512**),
  **956 MiB RAM** (412 MiB available at rest), 511 MiB swap with 268 MiB already
  used, `/dev/sda4` 24 GB with **9.9 GB free (56 % used)**, `/tmp` is a 479 MB
  tmpfs (temp files there are RAM, not disk). Load average 1.46 with
  `my_video.service` (Waitress via `start.sh`), `my_live.service`
  (node-media-server) and Apache running.
- ffmpeg 8.0.1 available, built with libsvtav1 / libx264 / libaom / dav1d /
  libvpx / x265. Cached videos: **6.2 GB total**; the largest is
  `app/storage/videos/c305f7f2862de533.mp4` = **959 MB**, AV1, 1920x1080,
  **60 fps**, 6.82 Mbps, 1151.9 s.
- Benchmarks (all run with `nice -19`, one thread, then `~/bench` removed):
  - `-c copy` 60 s cut of the 959 MB file: **0.31 s** (191x realtime).
  - HLS remux with `-c copy -f hls -hls_time 6`: **0.45-0.71 s** (131x realtime).
  - Whole-file HLS stream copy of the 1151.9 s file: **13.4 s** (89.8x realtime)
    - but it produced only **9 segments / 989 MB**, i.e. ~128 s and ~110 MB per
      segment (see 29.2).
  - x264 `veryfast` crf 28, 1 thread, **20 s** clip: **70.2 s** = **0.28x
    realtime**. Extrapolated to the 19-minute file: **~67 minutes of one core
    pinned at 100 %** on a box already at load 1.46, and SVT-AV1 software encode
    is several times slower than that. RAM stayed healthy (404 MiB free after).

### 29.1 The real bottleneck is the keyframe interval, and it is a bug

Packet-flag probe over three cached videos: keyframes sit at **0, 166.7, 300.0,
466.7, 600.0, 766.7, 900.0, 1066.7 s** - gaps of 133.3-166.7 s (8 000-10 000
frames at 60 fps). 959 MB file: 8 keyframes; 542.9 s file: 4; 228.5 s file: 2.

Cause, found in the source: `app/static/js/pipeline.js:450` and
`app/static/js/edit.js:863` both pass

    keyFrameInterval: 300,

mediabunny documents `keyFrameInterval` as **"the interval, in seconds"**
(vendored `mediabunny.min.mjs` computes `Math.floor(sample.timestamp / interval)`
and mediabunny uses floating-point **second** timestamps; the library default is
2 s). So the app asks for a keyframe every **300 seconds**, which is exactly
where 300.0 / 600.0 / 900.0 appear in the measurement; the hardware encoder
inserted extra keyframes at ~166.7 s on its own. The value was almost certainly
meant as 300 *frames* (5 s at 60 fps). Nothing in `PROJECT_PROGRESS.md` or the
code comments records a deliberate 300-second GOP.

Consequences of a 133-167 s GOP: a seek must download and decode from the
previous keyframe (that is why `send_file(..., conditional=True)` still feels
slow and why `seek_settle_seconds` had to grow to 60.0 in `config.example.json`
for Watch Together); server-side `-c copy` segmenting cannot produce fine
segments (the 9-segment result above is exactly this failure); and any
partial-download strategy has to fetch ~100 MB before it can show a frame from
the middle of a video.

### 29.2 Article technique -> this project

| Article technique | Current state here | Verdict |
|---|---|---|
| 1. Right format (AV1 ≈30 % smaller than VP9, ≈45-50 % smaller than x264; multiple `<source>`) | Already true: `WEBCODECS_CODEC_PRIORITY = ["av1", "avc"]`, MP4 output, and `ensure_faststart()` in `app/mp4.py` (called from `upload.py:420`) moves `moov` to the front, so the Drive copy inherits faststart | Done, nothing to gain |
| 2. Compress the video (HandBrake class) | Already stronger than the article: browser re-encodes to the admin's bitrate cap with `bitrateMode: "constant"` (CBR, because VBR overshoots ~2x) | Only preset tuning left; a server-side re-encode is out of the question (see 29.3) |
| 3. Drop unnecessary audio tracks | `tracks: "primary"` already keeps exactly one video + one audio track, and audio is *copied* (never re-encoded) | Small win left: a "no audio" switch for muted clips (~2-3 % of file size), client-side |
| 4. Streams (HLS / DASH, `.m3u8` + segments) | Not used. Playback is one progressive faststart MP4 served with HTTP Range (`watch.py:381` `send_file(..., conditional=True)`) **after the whole file has been downloaded from Drive** (`watch.py:93 start_cache`) | Feasible but not free; see 29.3 and 29.4 |
| 5. Remove unnecessary video (media queries) | Not applicable to a video-sharing site - the home page renders cover JPEGs, not video previews | Nothing to do |

### 29.3 Answer to the VPS question: splitting yes, re-encoding no

- **Split / remux / segment (no re-encode)**: trivially affordable - 89-191x
  realtime, a few seconds of CPU, memory impact invisible. ffmpeg 8 also
  supports `-hls_segment_type fmp4` + `init.mp4` for fragmented-MPEG4 HLS.
- **Re-encode (which is what a shorter GOP, a format change, or "drop the audio"
  on existing files all require)**: **not affordable.** 0.28x realtime for x264
  `veryfast` on one core means ~67 minutes of a pinned single vCPU per 19-minute
  video while Flask, node-media-server and Apache share that same core, and an
  SVT-AV1 encode is multiples worse. Adding keyframes to an existing file is not
  a copy operation - the affected GOPs must be re-encoded.
- **Disk**: 9.9 GB free vs 6.2 GB already cached. HLS while keeping the original
  MP4 doubles the footprint (959 MB -> 989 MB in segments for one file), so
  HLS for the whole library does not fit today.
- Conclusion: the existing library cannot be re-cut on the VPS. Fixing the GOP
  of an existing video means re-encoding it once on the owner's machine (the
  existing edit/clip page already does this in the browser: roughly 10-40 minutes
  for a 19-minute clip) and re-uploading it.

### 29.4 Pending Changes (registered, **not implemented** - owner approval needed)

| # | File(s) | Change | Why | Blast radius |
|---|---------|--------|-----|--------------|
| 29.1 | `app/static/js/pipeline.js`, `app/static/js/edit.js` | `keyFrameInterval: 300` -> `keyFrameInterval: 2` (mediabunny unit is seconds; library default is 2) so every new upload gets a 2-second GOP | 29.1 above: a 133-167 s GOP is the direct cause of slow seeking and is what makes any segment/partial-download strategy useless; under CBR the file size does not change, only a few percent of quality is spent on the extra keyframes | every future upload/transcode/clip; existing files unchanged |
| 29.2 | `app/routes/watch.py`, `app/static/js/watch*.js` | Prefetch a byte **range** from Drive (first ~4 MB + the range around the requested seek) instead of downloading the whole file before playback | Cuts the "wait for 959 MB" wall and the LRU pressure; needs a Drive range-capable fetch path, a partial-file cache state, and a plan for Drive's expiring direct links and quota/anti-bot limits | watch/cache path |
| 29.3 | upload pipeline | Optional "no audio" switch (drop the audio track client-side) | Article item 3; the cheapest remaining size win | upload options only |
| 29.4 | watch page + `requirements` | HLS/fMP4 A/B experiment on **one** video: server-side `-c copy -f hls -hls_time 6 -hls_segment_type fmp4` after 29.1 lands, played through `hls.js` | Proves whether segmentation is worth its cost (new JS dependency, ~2x files, Drive mirroring and the LRU model both need an answer) **without** pretending the current GOP allows it | experiment in `testing/`, no production route change |

### 29.5 Explicitly ruled out

- Server-side re-encoding of the library (CPU: see 29.3).
- Multi-`<source>` codec negotiation (the library is already AV1; a second
  H.264 copy would double storage for a browser population that already decodes
  AV1).
- HLS across the whole library today (storage: 9.9 GB free vs 6.2 GB cached).

**Status:** research recorded 2026-11-04 14:24:55. The owner approved 29.1 on
2026-11-04 14:36:58 and it is implemented as 30.1 below; 29.2-29.4 stay in
this table until approved.

## 30. HLS plan - keyframe interval fixed, segmentation architecture registered (Session: 2026-11-04 14:36:58)

**Owner direction (chat, 2026-11-04 14:36:58):** 29.1 approved. "What I want to
implement now is HLS. The hashed-filename storage could become a hashed-folder,
and the source can be deleted after segmenting so there is room. Should the HLS
segments be produced in the browser or on the server, and would slicing too fine
hurt upload speed?"

### 30.0 Design decision recorded before any code: segment on the server

1. **The browser keeps producing one complete progressive MP4; the server splits
   it with `-c copy`.** Splitting in the browser would replace today's ~15 upload
   parts (`SEGMENT_SIZE = 64 MB`, `pipeline.js:35`, one authenticated
   `POST /upload/seg/part` per part with `part_index` and a server checkpoint)
   with 192-576 parts (a 6.82 Mbps CBR stream is 5.1 MB per 6 s slice, 1.7 MB per
   2 s slice). Every extra part is another cookie+CSRF multipart round trip,
   another checkpoint row, and another chance to fail mid-transfer - and this
   project explicitly optimises for unstable uplinks. A half-uploaded playlist
   also becomes a state that has to be garbage-collected, and mirroring segments
   to Drive would mean ~192 Drive objects per video instead of one.
2. **Answer to "too fine?": the slice length must stay a multiple of the GOP.**
   A segment always starts at a keyframe, so slicing finer than the GOP forces
   extra keyframes and costs real bits (an AV1 1080p60 keyframe is tens of KB;
   1 s slices over 19 minutes land near +5 %). With the 2 s GOP from 30.1, a 6 s
   slice (3 GOPs) is the sweet spot: fine enough to seek into, coarse enough to
   keep the request count and the keyframe overhead down.
3. **Server-side splitting is already proven cheap on this VPS** (29.0): 13.4 s
   for the 959 MB file at 89.8x realtime, copy-only, no memory pressure. What is
   *not* affordable is a server-side re-encode (0.28x realtime), which is exactly
   why the GOP has to be fixed at encode time in the browser instead.
4. **Therefore the derived data is a cache, not a second copy.** The Drive object
   stays one MP4 (checksums, export/import, the download button and the resumable
   upload protocol all keep working); segments are regenerable from it in seconds
   and are the first thing the LRU throws away.

### 30.1 Pending Changes

| # | File(s) | Change | Why | Blast radius |
|---|---------|--------|-----|--------------|
| 30.1 | `app/static/js/pipeline.js`, `app/static/js/edit.js`, `app/templates/edit.html`, `app/templates/upload.html`, `app/__init__.py` | `keyFrameInterval: 300` -> `2` with a comment stating mediabunny's unit is **seconds**; add `?v={{ static_version }}` to the `edit.js` / `upload.js` script tags and add `edit.js`, `upload.js`, `pipeline.js` to the `_static_version()` hash list | 29.1 (133-167 s GOP blocks seeking and blocks copy-only segmentation). `edit.js`/`upload.js` are today the only page scripts loaded without a cache-busting query, so an encoder change would otherwise not reach clients that cached them | every future upload/transcode/clip; static cache keys |
| 30.2 | VPS only, no repo change | After one video re-encoded with the 2 s GOP exists, run `ffmpeg -i in.mp4 -c copy -f hls -hls_time 6 -hls_segment_type fmp4 -master_pl_name master.m3u8 ...` and record segment count, sizes, elapsed time | validates 30.0 against a real file before touching storage code (the current library cannot be used for this: its GOP is 133-167 s) | none |
| 30.3 | `app/storage.py`, `app/db.py`, `app/routes/watch.py`, `app/drive_worker.py`, `app/routes/admin.py` | Hashed **folder** per video: `storage/videos/<hash>/` with `<hash>.mp4` plus a `<hash>.stream/` (`master.m3u8`, `init.mp4`, `seg-*.m4s`); `actual_cache_bytes()` must sum folder contents (`os.path.getsize` on a directory is not the cache size), `_evict()` must remove a tree, `db` gains `stream_dir` / `stream_state` | every cache path today assumes one flat file (`os.path.getsize` in `actual_cache_bytes:68`, `path.unlink()` in `_evict:147`); a folder laid down without these changes silently leaks disk and breaks the cap | cache/LRU, eviction, admin export/import, download button |
| 30.4 | `app/hls.py` (new) | `ensure_stream(video)`: when the stream folder is missing/incomplete, run ffmpeg copy-only (`-c copy -f hls -hls_time <cfg> -hls_segment_type fmp4 -init_mp4 1 -master_pl_name master.m3u8`) under `nice -19`, one thread, hard timeout, write into a temp name then rename atomically; driven from the background worker queue and **never from a request handler**; per-video lock so two viewers cannot spawn two ffmpegs; `stream_state` recorded in the DB | segmentation must never re-encode (29.3) and must be reproducible from the Drive MP4 at any time | stream path only |
| 30.5 | `app/templates/watch.html`, `app/static/js/watch-player.js` (new), `app/static/js/vendor/hls.min.js` (new, vendored) | Play HLS when `stream_state == 'ready'`, otherwise keep the current progressive `send_file(..., conditional=True)` path; fMP4 segments only (AV1 in MPEG-TS is unreliable for `hls.js`) | the 8-10 existing videos have a 133-167 s GOP and must keep playing through the old path until re-uploaded | watch page |
| 30.6 | `app/config.py`, `config.example.json`, admin settings page | `hls_enabled` (default **false**), `hls_segment_seconds` (6), `hls_keep_source` (true) | one switch that turns the whole feature off without a code change, and one knob that decides whether the source MP4 stays next to its segments | config only |
| 30.7 | `testing/probe_hls_flow.py` (new) | Build/fixture a tiny clip with a short GOP, run `ensure_stream`, assert the playlist + segment count + `actual_cache_bytes()` accounting for a folder, and skip cleanly when ffmpeg is absent | 30.3 is where a regression would leak disk silently | testing |

### 30.2 Open decisions for the owner (answer before 30.3 is coded)

- **Delete the source MP4 after segmenting?** Recommended default: **keep it**
  (`hls_keep_source: true`) and let the LRU drop `.stream/` folders first, because
  segments are worth ~+3 % over the source (measured 959 MB -> 989 MB) and the
  source is what makes them regenerable in 13 s. If disk is the binding limit,
  `hls_keep_source: false` means "segments only", and re-cutting then requires the
  full Drive download first - which is what the current code already does today.
- **What mirrors to Drive?** Recommended: the single MP4 only. Mirroring segments
  multiplies Drive objects and breaks the checksum-based export/import.
- **Who serves segments?** `send_file` from Flask is fine at this scale (192 small
  requests against 50 waitress threads). An Apache `AliasMatch` could bypass Flask,
  but it would bypass `login_required` too, so it needs a signed short-lived
  stream token first. Not decided; not needed for the first version.

**Status:** 30.1 registered 2026-11-04 14:36:58 and implemented in the same
session (Change Log below). 30.2-30.7 are proposals and stay here until the owner
approves them.

### 30.3 Stream lifecycle (recorded 2026-11-04 14:52:15, answers "when do we segment?")

Drive always holds **one complete MP4 per video** - it is the immutable source of
truth. Everything on the server (the MP4 copy *and* its segments) is a derived,
regenerable cache. Segmentation therefore has exactly two triggers, and both go
through the same helper:

- **Eager (new uploads).** The local MP4 is already whole on disk the moment the
  last 64 MB part lands, and `upload.py:420` already faststarts it. Queue the
  copy-only segmentation on the background worker right after that (ordering
  against the Drive mirror does not matter - segments are cache, the mirror is the
  backup). Doing it here means no viewer ever waits for ffmpeg, and it keeps ffmpeg
  off the request path entirely.
- **Lazy (cold cache).** A video whose local copy was evicted: today `start_cache`
  downloads the whole MP4 from Drive before anything plays. Version 1 keeps that
  shape and just adds a segmentation step after the download (measured cost: 1 s of
  wall time per 30 s of video, ~13 s for the 959 MB file). Version 2 can improve it
  further by *streaming through* ffmpeg - the first segment only needs the first few
  MB of the source (faststart makes that possible), so playback could start while
  the rest of the file is still downloading. That is a separate change, not v1.

`stream_state` per video: `none` (never built) -> `building` (ffmpeg running,
watched by a per-video lock) -> `ready` (playlist + segments validated, HLS is
offered) -> `stale` (source replaced by a re-cut/upload, or the segments were
evicted). The watch page offers HLS only when `stream_state == 'ready'`; every
other state, and every video whose GOP is still 133-167 s, keeps the current
progressive `send_file(..., conditional=True)` path.

Two facts that settle the "delete the source after segmenting?" question:

- Measured container overhead of fMP4 segments over the source MP4 is **+0.03 %**
  (30.2). Read correctly that means the two copies cost the same: keeping both doubles
  the local footprint, so segments-only is the half-size option and keeping both is the
  regenerate-in-13-seconds option. The original wording of this entry (deleting the
  source saves nothing) was wrong and is superseded by 31.1, where the owner chose
  segments-only (`hls_keep_source: false`).
- Remuxing needs to read the whole source, so a segment set can only be
  regenerated from the MP4 (local or in Drive), never from other segments. That is
  why the MP4 stays the mirrored object and the thing the download button serves.

### Change Log

#### 30.1 keyframe interval + static cache-busting - implemented 2026-11-04 14:40:55

- `app/static/js/pipeline.js:450` and `app/static/js/edit.js:863`:
  `keyFrameInterval: 300` -> `keyFrameInterval: 2`, each carrying a comment that
  mediabunny's unit is **seconds** and pointing back to this document. New
  uploads now get a keyframe every 2 s instead of one every 300 s. Output size is
  effectively unchanged because the encoder runs CBR (`bitrateMode: "constant"`);
  the price is a few percent of the bits going to keyframes instead of residuals.
- `app/templates/edit.html:183` and `app/templates/upload.html:115`: the only two
  page scripts that had no cache-busting query now carry `?v={{ static_version }}`.
  Flask 3.1.3 keeps `SEND_FILE_MAX_AGE_DEFAULT = None`, so `send_file` answers
  statics with `Cache-Control: no-cache` plus an ETag and clients revalidate
  anyway (werkzeug sets `cache_control.no_cache = True` whenever `max_age` is
  unset) - the query is the belt-and-braces layer, and it makes the URL change
  exactly when the encoder settings change.
- `app/__init__.py` `_static_version()`: the hash list gained `static/js/edit.js`,
  `static/js/upload.js`, `static/js/pipeline.js`, so touching the upload pipeline
  rotates the version of every versioned asset.
- Existing library is untouched by design: the cached videos keep their 133-167 s
  GOP until the owner re-encodes them in the browser through the edit/clip page
  (zero VPS CPU) and re-uploads.

Verification (2026-11-04, project venv + node v26.10.0):

- `node --check` on `app/static/js/edit.js`, `app/static/js/pipeline.js`,
  `app/static/js/upload.js` - all exit 0.
- `python -m compileall -q app main.py wsgi.py` - exit 0.
- `python -m pyright app` - `0 errors, 0 warnings, 0 informations`.
- `python testing/run_probes.py --timeout 90` - **16/16 probes passed**, timings
  unchanged (slowest `probe_offline_red_dot.py` 31.5 s).
- Not covered by any probe: the keyframe spacing of a real browser-encoded output.
  Confirm it by re-encoding one clip through `/upload/edit`, letting it cache on
  the VPS, and re-running the packet-flag probe - keyframes should then be ~2 s
  apart, which is also the precondition for 30.2.

#### 30.2 (preliminary run) copy-only fMP4/HLS measured on the VPS - 2026-11-04 14:45:42

No repo change, as registered. Scratch dir `~/hlstest-20261104` on
`vsakuya-hk-server`, removed after the run; `/` stayed at 9.9 GB free.

- Synthetic source: `testsrc2` 1280x720@30, 30 s,
  `-c:v libx264 -preset ultrafast -b:v 3000k -g 60 -movflags +faststart` - GOP 60
  frames = 2 s, i.e. the same GOP that 30.1 now asks the browser for. Encode took
  7 s (`nice -19`, load average 0.51). `ffprobe` packet flags confirmed keyframes
  at exactly 0, 2, 4 ... 28 s.
- Segmentation, copy only:
  `ffmpeg -i src.mp4 -c copy -f hls -hls_time 6 -hls_segment_type fmp4
  -hls_flags independent_segments -master_pl_name master.m3u8
  -hls_segment_filename "seg-%03d.m4s" stream.m3u8` -> **1 s elapsed** for 30 s of
  video, **5 segments of exactly 6.000 s** (no keyframe rounding penalty, which is
  what the 133-167 s GOP caused before), `init.mp4` 826 B, `stream.m3u8` 292 B
  (`EXT-X-VERSION:7`, `EXT-X-INDEPENDENT-SEGMENTS`, `EXT-X-MAP:URI="init.mp4"`),
  `master.m3u8` 141 B.
- Sizes: segments total 11,343,778 B vs source 11,340,312 B = **+0.03 %**. The
  same measurement with MPEG-TS segments on the 959 MB library file had been
  +3 %, so the container choice matters and fMP4 is the one to use (it is also the
  only variant that carries AV1 dependably).
- Integrity: `ffmpeg -i stream.m3u8 -f null -` decoded the whole playlist with no
  errors; `cat init.mp4 seg-003.m4s` probed as a valid 1280x720 stream whose first
  keyframe is at pts 18.0 s, i.e. segment 3 starts exactly on a GOP boundary.
- Gotcha for the future probe (30.7): a standalone `.m4s` cannot be probed on its
  own (`trun track id unknown, no tfhd was found`). Validate the playlist, or
  `init.mp4` + the segment concatenated.
- Still open for 30.2 proper: the identical command against a **real** library
  file, which requires one video re-encoded with the 30.1 GOP first.

## 31. Final HLS implementation plan (registered 2026-11-04 14:57:50 - owner sign-off required before any code)

**Owner decisions recorded in chat (2026-11-04):** (1) Drive mirrors the single
complete MP4 only - segments never go to Drive. (2) Segmentation runs inside the
server (Flask process, background queue) and segments are served by Flask too -
no Apache `AliasMatch`, so `login_required` stays the only auth story. (3) The local
cache then holds only segmented videos for segmented titles - confirmed, and made the
default (`hls_keep_source: false`, see 31.1). (4) The owner arranges this plan first;
31.1-31.9 stay unimplemented until that arranged version is approved.

### 31.1 Storage layout and what the cache actually holds

One directory per video under `storage/videos/`:

    storage/videos/<hash>/
        <hash>.mp4            # local copy of the Drive object (transient)
        stream/
            master.m3u8       # EXT-X-VERSION:7 media playlist (EXT-X-MAP -> init.mp4)
            init.mp4          # 826 B in the measured test
            seg-000.m4s ...   # fMP4 segments, 6 s each

Two facts decide the policy:

- `segments ~= MP4` in size (+0.03 %, 30.2). So keeping **both** doubles the local
  footprint, and **segments-only is the half-size option**. The owner's instinct is
  correct and becomes the default: `hls_keep_source: false` - once a stream is
  validated, the local MP4 is deleted, and the cache holds only `stream/` for
  segmented videos (un-segmented ones - the current 8-10 library files with a
  133-167 s GOP, and in-flight uploads - keep their flat `.mp4` exactly as today).
- Dropping the MP4 does not make the video unplayable or undownloadable:
  `init.mp4 + seg-*.m4s` concatenated **is** a valid fragmented MP4, so the
  download button is served by concatenating segments (no Drive round trip), and
  re-cutting a video still needs the MP4, which is re-downloaded from Drive.
- `local_filename` keeps its meaning and just gains a directory component for new
  uploads (`"<hash>/<hash>.mp4"`). Every existing consumer already does
  `os.path.join(videos_dir, local_filename)`, so nothing else has to know. Existing
  rows stay flat and keep working; no forced migration of old files.

Peak disk during one upload with HLS enabled is MP4 + segments (~2x) until
segmentation validates and the MP4 is dropped, so `ensure_space()` must reserve
`incoming_bytes * 2` when `hls_enabled` is true (otherwise a 959 MB upload can pass
the pre-check and then fail mid-segmentation).

### 31.2 Lifecycle

    browser transcode -> 64 MB parts -> assemble -> ensure_faststart (upload.py:420)
      -> drive_worker._run: Drive mirror -> status="ready"  (drive_worker.py:182)
      -> NEW hook, same worker, same thread: hls.ensure_stream(video, reason="post-mirror")
           build in <hash>/stream.tmp/ -> validate -> rename to stream/ -> stream_state='ready'
           -> hls_keep_source=false: remove <hash>.mp4, local_filename=None (stream keeps it cached)
      -> watch page: stream_state=='ready' ? hls.js : today's progressive /watch/<id>/stream

    cold cache (evicted): start_cache downloads the MP4 from Drive -> ensure_stream(reason="post-download")
      -> serve HLS. v2 (not in this plan): stream Drive straight through ffmpeg so the first
         segment is playable while the rest of the file is still downloading.

`stream_state`: `none` -> `building` -> `ready` -> `stale` (source replaced by a
re-cut/re-upload, or the `stream/` tree was evicted) -> `failed`. Only `ready`
offers HLS. Building never happens inside an HTTP request; a request may read the
state and show "preparing..." only.

### 31.3 Pending Changes

| # | File(s) | Change | Why | Blast radius |
|---|---------|--------|-----|--------------|
| 31.1 | `app/hls.py` (new) | `ensure_stream(video, videos_dir, app_config, reason)`: copy-only ffmpeg (`-c copy -f hls -hls_time <cfg> -hls_segment_type fmp4 -hls_flags independent_segments -master_pl_name master.m3u8 -hls_segment_filename "seg-%03d.m4s"`), argv list (no shell), absolute paths, `nice -19` on POSIX, `subprocess.run(timeout=cfg["HLS_TIMEOUT_SECONDS"])`, build under `stream.tmp/`, validate then atomic rename; per-video lock; idempotent when already `ready`; returns the state | segmentation must never re-encode (29.3) and must be reproducible from the Drive MP4; a viewer must never wait for ffmpeg in a request thread | new module only |
| 31.2 | `app/sql/schema.sql`, `app/sql/migrations/videos_add_stream_state.sql` (new), `app/db.py` `_migrate`, `_ALLOWED_UPDATE_COLUMNS` | add `videos.stream_state TEXT NOT NULL DEFAULT 'none'` (+ `stream_updated_at`), following the existing `sql/migrations` idiom (§14.1); whitelist the new columns so `update_video` accepts them | 28.9's whitelist would otherwise reject the new column | db write/read layer |
| 31.3 | `app/routes/upload.py` `_unique_video_name`, `app/routes/watch.py` `_cached_filename`, `app/storage.py` `save_upload_as` | new videos live in `videos/<hash>/<hash>.mp4` (mkdir on write); old flat names keep working | one directory per video is the unit the LRU and the downloader both need | upload + cache write paths |
| 31.4 | `app/storage.py` `actual_cache_bytes`, `_evict`, `ensure_space` | folder-aware size sum (`_entry_bytes`: file -> `getsize`, dir -> walk); `_evict` removes the whole `<hash>/` tree (`shutil.rmtree`) instead of `unlink()`; `ensure_space` reserves `incoming * 2` when `hls_enabled` | today `getsize` on a directory and `unlink` on a directory are both wrong; the 2x peak is the difference between a working upload and one that dies at 60 % | cache/LRU, admin storage stats |
| 31.5 | `app/drive_worker.py` `_run` (after `status="ready"`) and `app/routes/watch.py` cache worker (after a Drive download) | call `hls.ensure_stream(...)` from the background worker, then honour `hls_keep_source` (delete the local MP4 when the stream validated); mark `stale` on re-upload and on eviction | this is the only place where the MP4 is known-complete and durable in Drive | upload completion + cold-cache playback |
| 31.6 | `app/routes/watch.py` (new routes), `app/templates/watch.html`, `app/static/js/hls-player.js` (new), `app/static/js/vendor/hls/hls.min.js` (new, vendored) | `GET /watch/<id>/hls/playlist.m3u8`, `/hls/init.mp4`, `/hls/<path:segment>` (all `login_required`, segment filename validated against the stream dir, `Cache-Control: private, max-age=3600` for segments, no-cache for the playlist); the player picks hls.js only when `stream_state == 'ready'` and the browser can be trusted to decode AV1 in fMP4, otherwise it keeps today's `<video src="/watch/<id>/stream">` | keeps the current progressive path as the fallback for the existing library and for browsers where AV1-in-HLS is unreliable | watch page |
| 31.7 | `app/routes/watch.py` `download_video` | when the local MP4 is gone but the stream is complete, serve `init.mp4 + seg-*.m4s` concatenated as `video/mp4` | a fragmented MP4 built from the segments is a normal playable file, so the download button survives segments-only caching | download path |
| 31.8 | `app/config.py` `DEFAULTS`, `config.example.json`, `app/routes/admin.py` + `admin_settings.html` | `hls_enabled` (**false**), `hls_segment_seconds` (6), `hls_keep_source` (**false**), `hls_timeout_seconds` (600), `ffmpeg_path` (auto-detect); admin badges for `stream_state` and an admin-only "Rebuild stream" POST action | one switch to ship the feature dark and one action to recover a stream without a redeploy | config + admin pages |
| 31.9 | `testing/probe_hls_flow.py`, `testing/probe_cache_dirs.py` (new) | `probe_cache_dirs.py`: fake a `<hash>/stream/` tree, assert `actual_cache_bytes()` counts it and `_evict()` removes the tree (no ffmpeg needed, runs everywhere). `probe_hls_flow.py`: requires `shutil.which("ffmpeg")`, builds a 30 s 2 s-GOP fixture, runs `ensure_stream`, asserts segment count / `#EXTINF` values / `stream_state`, exits 0 with a "skipped: no ffmpeg" note when the binary is absent | 31.4 is where a silent disk-accounting leak would live, and the HLS assertions need ffmpeg, which is present on the dev box (`C:\Tools\ffmpeg\bin\ffmpeg.exe`) and on the VPS (8.0.1) but must not be assumed on a clone | testing |
| 31.10 | `app/templates/together_view.html`, `app/static/js/stream-player.js` (new) | One player helper shared by the watch page and the Watch Together page: HLS playlist when `stream_state == 'ready'`, otherwise today's `/watch/<id>/stream`; `_is_cached` becomes `_is_playable_locally` (MP4 **or** ready stream) so a segments-only video still opens the player instead of the Drive-download panel | 31.7 blockers: with the MP4 dropped, `watch.stream` 503s and Watch Together would break | watch page + together page |

### 31.4 Execution order

1. **P0 gate (no code).** Owner re-encodes one clip in the browser (30.1 is live),
   uploads it, and the packet probe on the VPS confirms ~2 s keyframes. Without
   that, nothing below can be measured on a real file.
2. **P1** 31.1 + 31.2 + 31.9 (`probe_cache_dirs.py` part) - module, column,
   probes; `hls_enabled` still false, nothing user-visible.
3. **P2** 31.3 + 31.4 - folder layout + cache accounting.
4. **P3** 31.5 + 31.8 - background trigger, keep_source drop, config/admin knobs.
5. **P4** 31.6 + 31.7 + 31.10 - player helper (watch + together), segment routes, download-from-segments.
6. **P5** VPS rollout (§31.6 below) and only then `hls_enabled = true`.

Rollback at any point: `hls_enabled=false` puts every video back on the progressive
path; deleting every `stream/` directory loses nothing because Drive holds the MP4.

### 31.5 Risks and how the plan answers them

| Risk | Answer in the plan |
|---|---|
| ffmpeg becomes a subprocess the web app can invoke | argv list only (never `shell=True`), fixed flags, source path derived from the DB row, per-video lock, hard timeout, copy-only (no codec option is ever passed) |
| AV1 inside HLS | fMP4 only (`-hls_segment_type fmp4`); player feature-detects and falls back to progressive MP4, which already works today in the browsers in use |
| ffmpeg availability differs per machine | `probe_hls_flow.py` skips with exit 0 when `shutil.which("ffmpeg")` is empty; the dev box has `C:\Tools\ffmpeg\bin\ffmpeg.exe` (N-128121-g86444dd368-20261027) and the VPS has ffmpeg 8.0.1, so both sides can validate |
| Double disk use during one upload | `ensure_space(incoming * 2)` while `hls_enabled`, MP4 dropped right after validation |
| Existing 8-10 videos | keep the progressive path; they are only wrong for segmentation because of their 133-167 s GOP, which only a browser-side re-cut can fix |
| A half-built stream | everything is built under `stream.tmp/` and renamed only after validation, so a partial tree is never `ready` |

### 31.6 VPS rollout steps (owner runs `git pull` / `./start.sh` on hk.vsakuya.xyz)

1. Move the VPS's hand-edited `documents/PROJECT_PROGRESS.md` aside first (30.1
   made it tracked, `git pull --ff-only` would otherwise abort).
2. `git rm --cached app/static/js/ffmpeg/ffmpeg-core.wasm` once, keeping the file.
3. Pull with `hls_enabled` absent/false, restart `my_video.service`, confirm the
   migration logged `stream_state` and nothing user-facing changed.
4. Re-cut + upload one clip from the owner's PC, confirm keyframes ~2 s and
   segments 6.000 s, confirm `stream_state='ready'` and that the local MP4 was
   dropped while playback worked.
5. Flip `hls_enabled=true` in `config.json`, restart, watch the log line for the
   segment count and elapsed time of the first real file.

### 31.7 Player integration facts measured in the repo (2026-11-04 15:03:03)

Checked directly against the vendored player. P4 is smaller than 31.6 first assumed, and
segments-only turns out to create two blockers that must be fixed in the same phase.

- `app/static/vendor/dplayer/DPlayer.min.js` (304,629 B) does **not** bundle hls.js. Its
  `initMSE` has a `case "hls"` branch that needs the global `window.Hls` (vendored hls.js)
  plus `options.pluginOptions.hls`; without it DPlayer shows `Error: Can't find Hls.` It
  also falls back to `type = "normal"` when the element answers
  `canPlayType("application/x-mpegURL")` (Safari plays HLS natively), so hls.js is not
  needed there.
- `video.type: 'auto'` is enough: the auto branch matches /m3u8(#|\?|$)/i on the src, so
  `GET /watch/<id>/hls/playlist.m3u8` is recognised as HLS with no special type string.
  `hls.min.js` must load **before** the DPlayer constructor runs (both templates load
  DPlayer.min.js in `<head>` with `?v={{ static_version }}`).
- Two call sites, not one: `app/templates/watch.html:139` + `149-155` and
  `app/templates/together_view.html:45` + `56-62` both pass `url_for('watch.stream')` with
  `type: 'video'`. The Watch Together sync uses currentTime and seek, which HLS handles
  just as well - and better, because a 2 s GOP seeks accurately where 133-167 s GOP cannot.
- **Blocker 1.** `watch.stream` aborts 503 when `local_filename` is empty or the file is
  missing (`app/routes/watch.py:373-379`), and `_is_cached` (`app/routes/watch.py:74-79`) is
  the predicate that decides whether the watch page shows the player or the Drive-download
  panel. With `hls_keep_source: false` both are false for a segmented video, so the page
  would offer a Drive download instead of playing. Fix: make the predicate mean playable
  locally = MP4 present **or** `stream_state == 'ready'`, and let `stream` and `download`
  answer from the stream tree.
- **Blocker 2.** `together_view.html` plays through `watch.stream`, so a segments-only video
  would break Watch Together outright. Fix: 31.10 below.
- New pending change in the same blast radius as 31.6: **31.10**
  `app/templates/together_view.html` and `app/static/js/stream-player.js` (new) - one helper
  shared by both templates that picks `hls/playlist.m3u8` when `stream_state == 'ready'` and
  hls.js is usable, otherwise today's `/watch/<id>/stream`.

P0 gate addition: the library is AV1, so the P0 measurement must run the copy-only HLS
command on a real **AV1** clip (30.2 used H.264) and the P4 browser check must confirm
AV1-in-fMP4 playback through hls.js in the browser actually in use.

**Status:** registered 2026-11-04 14:57:50, amended 2026-11-04 15:03:03 with 31.7. The
owner arranges the plan; **31.1-31.10 remain unimplemented** and no code file was touched.
**Amended 2026-11-04 17:10:39 by 33: all ten are implemented and measured locally; the line
above is kept as the record of when the plan was signed off.**

## 32. Execution started + the no-re-upload repair path (Session: 2026-11-04 15:21:17)

Owner sign-off in chat: 31 approved as arranged, git commit is allowed for this work,
plus one requirement: **the videos already in the library must become HLS-capable
without the owner re-encoding and re-uploading them.**

### 32.0 Measurements that decide the repair design (VPS, scratch ~/repairtest-20261104)

- Library file c305f7f2862de533.mp4: AV1 1920x1080 60 fps, 6,815,960 bps, 1151.872 s,
  1,005,551,388 B, audio **aac**. Keyframes found in a 20 s window: **1**.
- Copy-only cutting is keyframe-snapped: -ss 30 -t 20 -c copy produced a ~52 s excerpt.
  There is no frame-accurate server-side trim without re-encode.
- Copy-only HLS of that sparse-GOP excerpt: 1 s, **one** segment of 50.000 s
  (EXT-X-TARGETDURATION:50). Valid HLS, useless seeking - this is all 31.1 alone can
  do for the existing library.
- Server re-encode cost on the VPS (1 vCPU, nice -19, -threads 1, crf 28, GOP 60):

  | preset | 50 s clip | realtime factor | whole 1151.9 s file | output vs source |
  |---|---|---|---|---|
  | libx264 ultrafast | 77 s | 0.65x | **~30 min** | 28.5 MB vs 44.9 MB (63 %) |
  | libx264 veryfast | 128 s | 0.39x | ~49 min | 10.9 MB (24 %) |
  | libx264 faster | 185 s | 0.27x | ~71 min | 12.8 MB |
  | libsvtav1 preset 6 (AV1 to AV1) | **KILLED, OOM** | - | - | 956 MiB box, 484 MB free |

- Repaired output: keyframes every **1.000 s**, then copy-only HLS in **<1 s** into
  6.000 s segments (EXT-X-TARGETDURATION:6, EXT-X-PLAYLIST-TYPE:VOD,
  EXT-X-MAP:URI="init.mp4", init.mp4 837 B). -c:a copy keeps the AAC track intact.
- Repair therefore = **libx264** (decodes in every browser, and only 63 % of the AV1
  size). AV1 re-encoding on this VPS is ruled out by the OOM kill, not by taste.

### 32.1 Pending Changes

| # | File(s) | Change | Why | Blast radius |
|---|---------|--------|-----|--------------|
| 32.1 | app/hls.py | Second mode repair: step 1 re-encode the source to repaired.mp4 with -c:v libx264 -preset cfg -crf cfg -g round(seg*fps) -keyint_min same -sc_threshold 0 -threads 1 -c:a copy -movflags +faststart; step 2 run the copy-only HLS over repaired.mp4; step 3 delete repaired.mp4 once the stream validates | the library GOP is 133-167 s, so copy-only can only build 50 s segments; the Drive original stays the source of truth and is never modified | hls module |
| 32.2 | app/sql/migrations/videos_add_stream_state.sql, app/sql/schema.sql, app/db.py | stream_dir (relative path of the stream tree), stream_state, stream_source (copy / repair), stream_updated_at, all four added to _ALLOWED_UPDATE_COLUMNS | 31.2 planned only stream_state, but with hls_keep_source false the row must still know where its segments live | db layer |
| 32.3 | app/routes/admin.py, app/templates/admin_videos.html | Per-video stream badge (none / building / repairing / ready / failed) plus two admin POSTs: rebuild stream (copy-only) and repair GOP (CPU-bound x264) | the owner asked for a fix that needs no re-upload, and an admin action is the only place that may spend server CPU deliberately | admin pages |
| 32.4 | app/hls.py, app/config.py | Repair guardrails: one repair at a time (module lock, non-blocking), nice -19 on POSIX, -threads 1, hls_repair_timeout_seconds (7200), refuse unless ensure_space(source * 1.35) passes, hls_repair_enabled knob | 30 min of single-core saturation is acceptable only when explicit, off-peak and bounded; the OOM kill shows how little headroom this box has | config + hls module |
| 32.5 | testing/probe_hls_repair.py (new) | Build a tiny sparse-GOP fixture, run ensure_stream(mode="repair"), assert the playlist segments are shorter than the fixture GOP and that stream_source records repair; skip with exit 0 when ffmpeg is absent | repair is the only path that ever passes a codec option, so it needs its own assertion | testing |

### 32.2 Decisions recorded before code

- The repaired stream is a **derived cache**, never a new source: the Drive object and
  the DB row (title, tags, cover, comments, folder) are untouched, so nothing is
  re-uploaded.
- Repair output is H.264 video with the AAC track copied. Deleting stream/ restores the
  original state, and the copy-only path stays available for videos whose GOP is fine.
- Repair is admin-triggered only, never automatic on upload: an upload path that spends
  30 minutes of CPU per file is how a weak server dies.


## 33. HLS landed: 31.1-31.10 + 32.1-32.5 implemented, measured, corrected (Session: 2026-11-04 17:10:39)

Owner sign-off carried from 32: implement 31 as arranged, implement the 32 repair path (the
existing library must become HLS-capable without a re-upload), and git commit is allowed for
this work. Everything below ran for the first time in this session, on the dev box.

### 33.0 Pending Changes added during implementation

Each row was found by running a probe or a real build, not predicted by 31.x/32.x. They are
registered here and implemented in this same session.

| # | File(s) | Change | Why | Blast radius |
|---|---------|--------|-----|--------------|
| 33.1 | `app/hls.py` `_run_ffmpeg`, `_copy_argv`, `ensure_stream` | run ffmpeg with `cwd` = the `stream.tmp/` directory and hand the hls muxer **relative** output names (`master.m3u8`, `seg-%03d.m4s`) | the muxer writes the `EXT-X-MAP` init segment relative to the **process** working directory, not relative to the playlist. With the absolute playlist path ffmpeg wrote `init.mp4` into the project root and `validate_stream` rejected a build whose segments were all correct: `probe_hls_flow.py` died on "init segment missing" | hls module - without this nothing ever reaches `ready` |
| 33.2 | `app/hls.py` `playlist_init_name`, `_INIT_NAME_RE`, `validate_stream`, `iter_stream_bytes`; `app/routes/watch.py hls_asset` | read the init file name out of `EXT-X-MAP:URI="..."` instead of hardcoding `INIT_NAME`, and reject any init URI that is not `^[A-Za-z0-9._-]+\.mp4$` or contains `..` | the name belongs to the playlist, not to the app; the asset route must only ever open a file the validated stream declares | hls module + segment route |
| 33.3 | `app/hls.py stream_bytes_length` | sum the init segment plus the playlist segments; no longer `glob("*")` | the old version counted `master.m3u8`, which `iter_stream_bytes` never emits, so the reassembled download advertised 652,092 B and sent 651,775 B | download route |
| 33.4 | `app/routes/watch.py _attachment_header` | assemble the RFC 6266 attachment header by hand (ASCII fallback + `filename*=UTF-8''`), stripping quotes and control characters | **Werkzeug 3.1.9 has no `ContentDisposition` class**: the import that 31.7 shipped with raised ImportError the first time the segments-only download ran | download route |
| 33.5 | `app/hls.py _GOP_BOUND_RATIO`, `longest_segment_seconds`, `ensure_stream` | a **copy-only** tree whose longest `#EXTINF` exceeds `segment_seconds * 1.25` is discarded: `stream_state` stays `none`, `stream_dir` stays NULL, the MP4 is kept, nothing is renamed. A tree that is already `ready` is never downgraded | a copy-only cut can only break on keyframes that exist, so a segment far longer than the target is the keyframe interval itself. 32.0 measured exactly that on the VPS (one 50.000 s segment from a 52 s excerpt); serving the library as such is unseekable **and** stores a full duplicate of the MP4 in the cache. Discarding it keeps today's progressive playback and leaves an honest badge for Repair GOP | hls module - it decides whether a video gets HLS at all |
| 33.6 | `app/hls.py ensure_stream` (repair branch) | log the ratio when repaired segments exceed 120 % of the MP4 they are about to replace, and point at `hls_repair_crf` | 32.0 measured 63 % on real AV1 video, but the synthetic probe fixture grew to 102 %: repair size is a tuning knob, and disk is the whole reason `hls_keep_source` is false | log only |
| 33.7 | `app/storage.py is_locally_playable`, `_next_evict_candidate`, `app/sql/videos/list_lru_cached.sql` | an eviction candidate is a row with `local_filename` **or** `stream_dir`; new predicate "MP4 exists, or `stream_state == 'ready'` and the tree validates", used by `watch` and `together` | with `hls_keep_source: false` a played-out video has no MP4, so the old SQL never selected it (a segments-only cache could never be evicted) and both pages thought it was unplayable | cache/LRU + watch/together gating |
| 33.8 | `app/sql/migrations/videos_add_stream_state.sql`, `app/db.py _migrate` | migration line 4 was missing its `--` marker, so `executescript` raised `sqlite3.OperationalError: near "("`; `_migrate` now also refuses to start when `stream_state` exists but `stream_dir` / `stream_source` / `stream_updated_at` do not | the migration had **never run anywhere**: the dev DB gained the columns in this session, and the VPS DB was checked to have none of the four (`PRAGMA table_info(videos)`) | db migration - it gates the whole feature |
| 33.9 | `app/routes/upload.py`, `app/routes/watch.py` | `_remove_upload_quiet` + `_rmdir_if_empty` on both upload failure paths; `makedirs` before assembling a segmented upload; `start_cache` reserves space (`storage.ensure_space`) and answers JSON 507 when the reservation fails; `_cached_filename` reuses the existing `<hash>/` folder when the row stores `stream_dir` | a foldered video cannot be cleaned with a bare `unlink()`, and a Drive download that starts without a reservation is how a 9.9 GB disk gets corrupted mid-write | upload + cache paths |
| 33.10 | `app/db.py` (see 33.8) and `documents/PROJECT_PROGRESS.md` | repaired two lines corrupted by an earlier scripted edit (4289 lost `v` to a vertical tab, 4295 lost `t` to a tab) | a progress line that reads "ideo.type" is not evidence anyone can follow | docs |

### 33.1 Change Log

#### 31.1-31.10 - implemented 2026-11-04 17:10:39

- **31.1** `app/hls.py` (new, 736 lines): `settings`, `ffmpeg_binary`, `can_build`,
  `stream_dir_for`, `source_path_for`, `validate_stream`, `playlist_init_name`,
  `playlist_segment_names`, `longest_segment_seconds`, `iter_stream_bytes`,
  `stream_bytes_length`, `_copy_argv`, `_repair_argv`, `ensure_stream`, `mark_stale`,
  `start_build`, `describe`. ffmpeg is argv-list only (never `shell=True`), copy-only for
  mode `copy`, `nice -19` on POSIX, hard `subprocess.run(timeout=...)`, everything built
  under `<hash>/stream.tmp/` and renamed only after `validate_stream` passes, per-video lock
  held non-blocking, idempotent when the tree already validates.
- **31.2** `videos.stream_dir`, `stream_state`, `stream_source`, `stream_updated_at` in
  `app/sql/schema.sql`, one migration file, `_ALLOWED_UPDATE_COLUMNS` extended. See 33.8 for
  the migration bug this session found.
- **31.3** `_unique_video_name` returns `<hash16>/<hash16>.mp4`; `save_upload_as` mkdirs;
  old flat names still resolve (`_cached_filename` accepts both shapes).
- **31.4** `storage._entry_bytes` (file -> size, dir -> walk), `actual_cache_bytes` fed by the
  new `db.list_cache_paths()`, `_evict` `rmtree` + empty-parent prune, `ensure_space` peak
  factor 2.0 while `hls_enabled` (1.35 for a repair, see 32.4).
- **31.5** `app/drive_worker.py` calls `hls.ensure_stream(..., reason="post-mirror")` right
  after the row turns `ready`; `watch.start_cache`'s worker calls it with
  `reason="post-download"`. Both are warning-only: a failed build must never lose a video
  that plays progressively.
- **31.6** `GET /watch/<id>/hls/playlist.m3u8` (`application/vnd.apple.mpegurl`,
  `Cache-Control: private, no-cache`) and `GET /watch/<id>/hls/<asset>` where `<asset>` must
  equal the playlist's init name or appear in `playlist_segment_names`
  (`private, max-age=3600`, `send_file(conditional=True)` so Range works). Both
  `login_required`; requests never build a stream. Vendored `app/static/vendor/hls/hls.min.js`
  (539,902 B, UMD -> `globalThis.Hls`) + `LICENSE`.
- **31.7** `download` reassembles `init.mp4` + every segment in playlist order as one streamed
  `video/mp4` body with an honest `Content-Length` (33.3) and a hand-built attachment header
  (33.4). `stream` stays MP4-only and 503s for a segments-only video, which is expected: HLS
  is that video's player path.
- **31.8** `app/config.py` DEFAULTS + `config.example.json`: `hls_enabled` **false**,
  `hls_repair_enabled` true, `hls_segment_seconds` 6, `hls_keep_source` **false**,
  `hls_timeout_seconds` 600, `hls_repair_timeout_seconds` 7200, `hls_repair_preset`
  "ultrafast", `hls_repair_crf` 26, `ffmpeg_path` "". `admin_videos.html` shows a stream badge
  and two POST actions; `admin_settings.html` shows a read-only panel (segment length, ffmpeg
  path, preset/CRF, per-state counts) because the switches live in `config.json`.
- **31.9** `testing/probe_hls_flow.py` and `testing/probe_cache_dirs.py` (new). Note:
  `.gitignore` ignores `testing/*`, so the probes stay local like every earlier probe.
- **31.10** `app/static/js/stream-player.js` (new) `SVS_STREAM.pick()` -> `{url, type, hlsJs}`
  (HLS only when `stream_state == 'ready'` and hls.js is supported, or the element plays
  mpegURL natively; otherwise today's `/watch/<id>/stream`), plus `attachRecovery()` which
  calls `startLoad()` on a fatal NETWORK_ERROR and `recoverMediaError()` on a fatal
  MEDIA_ERROR. Loaded before DPlayer in both `watch.html` and `together_view.html`, and listed
  in `_static_version()` so a redeploy busts both new assets.

### 32.1-32.5 - implemented 2026-11-04 17:10:39

- **32.1** `ensure_stream(mode="repair")`: step 1 re-encodes the source to `repaired.mp4` with
  `-c:v libx264 -preset <cfg> -crf <cfg> -g round(seg*fps) -keyint_min same -sc_threshold 0
  -threads 1 -c:a copy -movflags +faststart`; step 2 runs the copy-only HLS pass over it;
  step 3 deletes `repaired.mp4` once the stream validates. GOP is derived from the row's `fps`
  (fallback 30) so 6 s of video means 180 frames at 30 fps and 360 at 60 fps.
- **32.2** four bookkeeping columns as planned; with `hls_keep_source: false` the row keeps
  `stream_dir` so a video with no MP4 is still findable, playable, evictable and downloadable.
- **32.3** `admin_videos.html` badge (`none` / `building` / `repairing` / `ready` (+ source) /
  `failed`) and two plain POST forms with `csrf_field()` + `data-confirm`: Rebuild stream
  (copy-only) and Repair GOP. `_stream_action` refuses with an explicit flash when HLS is off,
  ffmpeg is missing, repair is disabled, the video has no local copy ("download it from Drive
  first"), or the repair does not fit on the disk. Nothing here runs ffmpeg in the request.
- **32.4** one repair at a time (module lock, non-blocking), `-threads 1`, `nice -19` on POSIX,
  `hls_repair_timeout_seconds` 7200, `hls_repair_enabled`, and the hard guard: a repair only
  starts when `storage.ensure_space(source * 1.35)` passes **above** the free-space reserve,
  else `StreamError("repair refused: needs ~N bytes...")` -> `stream_state='failed'` with the
  source untouched.
- **32.5** `testing/probe_hls_repair.py` (see 33.4 for how it changed).

### 33.2 Measurements in this session (dev box, ffmpeg N-128121-g86444dd368-20261027)

`probe_hls_flow.py` - 30.000 s fixture (320x240, 30 fps, GOP 60 frames = 2.000 s), target 6 s:

- 5 segments, longest `#EXTINF` **6.000 s**, total 30.000 s: keyframe-snapped cutting is exact
  when the keyframe interval is shorter than the target.
- tree = `init.mp4` 1,360 B + 5 segments = **651,775 B**; `hls_keep_source` false deleted the
  MP4 and the row still played, cached, evicted and downloaded from the tree alone.
- playlist route 200; `seg-000.m4s` 200; `seg-999.m4s`, `master.m3u8`, `init.m4s`,
  `seg-000.mp4` all 404 (the route can only open what the playlist lists).
- `/watch/<id>/stream` 503 by design; `/download` 200 `video/mp4`, 651,775 B, with `ftyp` and
  `styp`/`moof`/`mdat` boxes -> the reassembled file is a real fragmented MP4.
- the watch page loaded `vendor/hls/hls.min.js` + `js/stream-player.js` and rendered the player
  instead of the Drive-download panel (31.7 blocker 1 closed).
- a second `ensure_stream` on the same row rebuilt nothing and returned the same segment list.

`probe_hls_repair.py` - 20.000 s fixture, GOP 300 frames = **10.000 s** (deliberately like the
library's 133-167 s):

- raw copy-only ffmpeg: 2 segments, longest **10.000 s** - the 29.3/32.0 useless-HLS result,
  reproduced locally instead of only on the VPS.
- `ensure_stream(copy)` -> `none`, MP4 kept, no `stream/` dir, no `.tmp` left: 33.5 in action.
- `ensure_stream(repair)` -> `ready`: 4 segments, longest **6.000 s**, `stream_source='repair'`,
  tree 418,872 B against a 412,225 B source (ratio 1.02 on synthetic `testsrc`; 0.63 on real
  AV1 per 32.0 - the size is a `hls_repair_crf` knob, not a constant).
- tree held only `init.mp4`, `master.m3u8`, `seg-000..003.m4s`: no `repaired.mp4` scaffolding.
- with `storage.ensure_space` monkeypatched to refuse: `stream_state='failed'`, source intact,
  no `.tmp` tree.

`probe_cache_dirs.py` - foldered name + `save_upload_as` mkdir; `actual_cache_bytes` counted the
tree (17,411 B); a stream-only row was playable **and** listed by `list_lru_cached()`; `_evict`
removed the whole tree and cleared `stream_dir`/`stream_state`.

Dev DB after the migration: 8 video rows, every one `stream_state='none'` - correct, because
`config.json` still has `hls_enabled` false, so nothing user-visible changed.

### 33.3 Verification (2026-11-04, project venv)

- `python testing/run_probes.py --timeout 90` -> **19/19 probes passed** (the 16 earlier ones
  plus `probe_cache_dirs.py`, `probe_hls_flow.py`, `probe_hls_repair.py`).
- `python -m pyright app` -> 0 errors, 0 warnings, 0 informations.
- `python -m compileall -q app main.py wsgi.py testing` -> exit 0.
- VPS (ssh): `PRAGMA table_info(videos)` lists none of the four stream columns, so 33.8's
  migration will run there in one clean step; the 198 MB scratch `~/repairtest-20261104` was
  deleted and `/` is back to 9.9 GB free (13 GB used of 24 GB).
- dev-box scratch deleted: the stray `init.mp4` in the project root that 33.1 caused,
  `storage/_probe_scratch`, and two leftover probe video folders (verified unreferenced by any
  `videos` row first).

### 33.4 What this supersedes

- 31's closing status line ("31.1-31.10 remain unimplemented and no code file was touched") is
  superseded by this section: all ten landed.
- 32.1-32.5 landed. **32.5's probe changed shape**: it now measures copy-only over a sparse GOP
  with raw ffmpeg (so the evidence survives), asserts that the app *discards* that tree, and
  only then asserts the repair that follows - which is the sequence the library needs.
- 31.4's `incoming * 2` reservation stays; a repair additionally needs `source * 1.35` **above**
  the reserve, and the admin page pre-checks that number in MB so a doomed repair is refused
  while an admin is looking at the badge instead of 25 minutes into an encode.

### 33.5 Rollout order (owner runs on hk.vsakuya.xyz; 31.6 still applies)

1. Move the VPS's hand-edited `documents/PROJECT_PROGRESS.md` aside, then pull.
2. `git rm --cached app/static/js/ffmpeg/ffmpeg-core.wasm` once, keeping the file.
3. Pull with `hls_enabled` absent/false, restart the service, and confirm
   `PRAGMA table_info(videos)` now lists the four stream columns with nothing user-visible changed.
4. Re-cut + upload one clip with the 30.1 keyframe interval: confirm `stream_state='ready'`,
   segments ~6.000 s, and the local MP4 dropped while playback worked.
5. Flip `hls_enabled=true` and restart. New uploads now segment themselves; a sparse-GOP upload
   logs "copy-only tree discarded" and stays progressive (33.5) instead of spending the disk on
   one giant unseekable segment.
6. Repair the existing library one video at a time: Admin -> Videos -> Repair GOP. Budget ~1 s of
   single-core per second of video at preset `ultrafast` (~30 min for the 1,151.872 s file) and
   ~63 % of the source size. Only one repair runs at a time. Drive and every DB field stay
   untouched, which is the no-re-upload answer.

Rollback at any point: `hls_enabled=false` puts every video back on the progressive path, and
deleting each `stream/` tree loses nothing because Drive still holds the MP4.

### 33.6 Not verified in this session

- **AV1 inside fMP4 through hls.js in the browser actually in use** - the P0/P4 gate from 31.7.
  Everything above proves the server builds and serves a valid VOD fMP4 tree; no browser has been
  observed playing an AV1 HLS stream yet. If it fails, `hls_keep_source` + the progressive path
  are still the working answer and the repair output (H.264) is the fallback that will work.
- Nothing of 33 ran on the VPS except the checks in 33.3. The dev box is ffmpeg N-128121, the VPS
  is 8.0.1: same flags, assumed identical, untested there.
- A real ~1 GB library repair end to end. Only a 20 s fixture was repaired; the guards measured
  so far are the admin pre-check and `ensure_space`.
- Watch Together over HLS in two browsers. The helper is wired and the suite passes, but no
  two-browser session ran against a segments-only video.
- hls.js retry tuning is deliberately untouched so a first-run failure stays visible. If a flaky
  WAN shows fragment stalls, the knob is DPlayer `options.pluginOptions.hls` =
  `{manifestLoadingMaxRetry: 4, levelLoadingMaxRetry: 4, fragLoadingMaxRetry: 6}` - deferred.

### 33.7 Files touched in this session

`app/hls.py` (new), `app/static/js/stream-player.js` (new), `app/static/vendor/hls/*` (new,
vendored), `app/db.py`, `app/storage.py`, `app/drive_worker.py`, `app/__init__.py`,
`app/routes/watch.py`, `app/routes/admin.py`, `app/routes/together.py`, `app/routes/upload.py`,
`app/sql/migrations/videos_add_stream_state.sql`, `app/sql/videos/list_lru_cached.sql`,
`app/templates/watch.html`, `app/templates/together_view.html`, `app/templates/admin_videos.html`,
`app/templates/admin_settings.html`, `testing/probe_hls_flow.py`, `testing/probe_hls_repair.py`,
`testing/probe_cache_dirs.py` (untracked by `.gitignore`, like every other probe).

## 34. Segmentation trigger points, and the self-eviction defect they exposed (Session: 2026-11-04 17:53:01)

Owner question answered here: at which points does an already-cached video get split into segments?
Answering it meant reading every call site, and the reading found one real defect in the disk-reservation
code shipped by 33. It is fixed in this section.

### 34.0 Pending Changes found while answering

| # | File(s) | Change | Why | Blast radius |
|---|---------|--------|-----|--------------|
| 34.1 | `app/storage.py ensure_space` + `_next_evict_candidate`, `app/hls.py ensure_stream`, `app/routes/watch.py start_cache` | new `protect` argument: a disk reservation may not evict the videos it names | an HLS build asks `ensure_space` for room for the segments of a video that is itself a cached, Drive-backed LRU candidate, so the reservation could delete the exact MP4 it was about to segment. `start_cache` protects the row it is downloading for the same reason | cache eviction + every HLS build |
| 34.2 | `app/hls.py ensure_stream` | the reservation moved out of the repair-only branch so **every** build reserves before ffmpeg starts: 1.35x the source for a repair, 2.0x for a copy | a copy build briefly holds the MP4 **and** a near-complete duplicate of it as segments (30.2 measured equal sizes); 31.4's `incoming * 2` only ever covered the upload path, so an admin Rebuild had no headroom check at all | hls module - it decides whether a build may start |
| 34.3 | `app/routes/admin.py _stream_action` | the MB pre-check now covers `rebuild` (copy) as well as `repair`, and the message says a build may evict LRU videos to make room | only repair was pre-checked, and "free cache space first" stopped being honest advice once 34.1 let a build free that space itself | admin videos table |
| 34.4 | `app/storage.py ensure_space` docstring | replaced the backslash-escaped backticks left by an earlier scripted edit with the RST double backticks used by every other line of that docstring | a plain Python docstring renders a backslash literally, so the line printed an escape sequence nobody wrote on purpose | docs only |

### 34.1 Change Log (2026-11-04 17:53:01)

- **34.1** `app/storage.py`: `ensure_space(base_dir, incoming_bytes, app_config, peak_factor=None,
  protect=None)` forwards `protect` to `_next_evict_candidate(base_dir, app_config, protected)`, which now
  skips `video["id"] in protected_set` next to the existing "still uploading" skip. The 33.7 candidate rule
  (a row with `local_filename` **or** `stream_dir`) is unchanged, so a segments-only cache entry stays
  reclaimable - only the video about to be written is excluded. `app/hls.py` passes `protect=[video_id]`
  and `watch.start_cache` does the same; `enforce_cache_limit` keeps its own `in_flight_ids` path (33.9).
- **34.2** `app/hls.py ensure_stream`: the guard sits under the repair-slot branch inside the same `try`, so
  both modes share it - `factor = 1.35 if mode == MODE_REPAIR else 2.0`, then `storage.ensure_space(
  str(base.parent), source.stat().st_size, app_config, peak_factor=factor, protect=[video_id])`, raising
  `StreamError("<mode> refused: needs ~N bytes of headroom above the free-space reserve")`. The existing
  `except StreamError` handler turns that into `stream_state='failed'`, deletes `stream.tmp/` and leaves
  the source MP4 on disk, which is the behaviour `probe_hls_repair.py` asserts.
- **34.3** `app/routes/admin.py`: one pre-check for both buttons, `needed = source * (1.35 repair / 2.0
  copy)` against `free_space - reserve`, flashing "... needs about N MB of free headroom for this file,
  this server has M MB: free cache space first (a build may evict LRU videos to make room)." The hard
  guard stays `ensure_space` inside `ensure_stream`, because an admin page cannot be trusted to race with
  an upload worker or another viewer's cache job.
- **34.4** docstring text corrected in the lines 34.1 touched.
### 34.2 Where an existing cached video gets segmented (the answer)

Three call sites. Nothing else in the app builds a stream.

| # | Call site | Fires when | Mode |
|---|-----------|------------|------|
| 1 | `app/drive_worker.py:198` `hls.ensure_stream(..., reason="post-mirror")` | a mirror job finishes and the row goes `ready` - **new uploads and re-uploads only**; rows already `ready` are never re-processed | copy |
| 2 | `app/routes/watch.py:269` `hls.ensure_stream(..., reason="post-download")` | the background `_run_cache` worker finishes re-downloading the file from Drive (watch page "Download & play") | copy |
| 3 | `app/routes/admin.py:422` `hls.start_build(...)`, fed by `POST /admin/videos/<id>/stream/rebuild` (copy) and `POST /admin/videos/<id>/stream/repair` (repair) | an admin clicks the button, and `source_path_for` finds a local MP4 - otherwise the request redirects with the download-it-first flash | copy or repair |

Never a build: every viewer-facing route is read-only (`/hls/playlist.m3u8`, `/hls/<asset>`, `/stream`,
`/download`), and there is no cron, queue or scan that backfills videos whose `stream_state` is `none`.
The existing library therefore does not segment itself: it needs a re-upload (1), a re-cache (2) or
Repair GOP (3).

Undoing a tree: `storage.delete_local_cache` and LRU eviction `rmtree` the whole video folder and set
`stream_dir = NULL`, `stream_state = 'none'`; `watch.py:110` calls `hls.mark_stale` when a stored tree
fails `validate_stream` on read; `hls_enabled = false` makes `ensure_stream` return on its first line.

Current effect on users: none. `config.json` on this dev box has no `hls_*` keys, so `hls.settings()`
falls back to `enabled = False` (`app/hls.py:107`) and all three entry points return immediately.

### 34.3 What is waiting on the VPS when the flag is flipped (re-measured 2026-11-04 17:53:01 over ssh)

| measure | value |
|---------|-------|
| rows in `videos` | 94 |
| rows naming a local MP4 | 91 |
| rows whose MP4 is actually on disk | 86 (3 with `local_filename` NULL: ids 7/14/15; 5 whose file is gone: ids 22/24/26/27/28) |
| bytes / duration of those 86 files | 6,260 MB / 14,384.8 s |
| `du -sm storage/videos` | 6,261 MB |
| free space on `/` | 10,094 MB (12,447 of 23,585 used) |
| `videos.stream_*` columns | absent - the 33.8 migration will run once, cleanly |
| ffmpeg | 8.0.1-3ubuntu2 |
| full-library Repair GOP budget | ~0.65x realtime measured in 32.0, so about 6 h of one core for 14,385 s of video, one repair at a time |

None of those 86 files is segmented by any of them sitting there: (1) only runs for a new mirror job and
(2) only when a viewer pulls the file back off Drive. So 33.5 step 6 - Repair GOP per video - stays the
way to convert the existing library without a re-upload, and the 34.1/34.2 guards are what keep a repair
on 10 GB of free space from eating its own source.

### 34.4 Dev-box inventory, and one honest gap

| measure | value |
|---------|-------|
| rows | 8 |
| rows with a local MP4 on disk | 5 (ids 36/37/38/41/42), 571 MB / 715.4 s |
| rows with no local file | 3 (ids 20/32/33) |
| `videos.stream_*` columns | present - the migration already ran here |

`storage/videos/` also holds names no DB row owns: `79078584e925fdfc.mp4` (27,615,193 B),
`a6ee2a8c18a106a0.mp4` (27,615,193 B), `HELLDIVERS2-2026-02-2315-36-54.mp4` (835,703,279 B), the empty
folders `.segments/`, `2ce21cff12a7b8ad/`, `7d75aa19a2255172/` and the probe leftover
`probehlsflow123456/stream`. `actual_cache_bytes()` and `_next_evict_candidate` walk DB rows, so about
891 MB of orphans is neither counted against the admin cache cap nor evictable. **Deferred**, not fixed
in this session and not registered as approved: an orphan sweep (admin listing + delete).

### 34.5 Verification (2026-11-04 17:53:01, project venv)

- `python -m compileall -q app main.py wsgi.py testing` - exit 0.
- `python -m pyright app` - 0 errors, 0 warnings, 0 informations.
- `python testing/run_probes.py --timeout 90` - **19/19 probes passed**. `probe_hls_flow.py` (1.0 s) and
  `probe_hls_repair.py` (1.2 s) are the two that touch this code; the repair probe's forced-failure case
  (`storage.ensure_space = lambda *a, **k: False`, line 187) still refuses, still leaves the source MP4
  intact and still lands on `failed`, because its stub swallows the new `protect` kwarg.
- VPS numbers re-read over ssh: `rows 94 nonnull 91 on_disk 86 mib 6260 dur 14384.8`, `stream_cols []`,
  `null_local [7, 14, 15]`, `missing_file [22, 24, 26, 27, 28]`, `df -m /` 10,094 MB free.
- Dev numbers from `testing/inv_local.py` (untracked helper; `.gitignore` covers `testing/*`).

### 34.6 Still unverified

33.6 stands unchanged and nothing here narrows it: no build has run on the VPS, no browser has been
observed playing AV1 over HLS, no real ~1 GB repair has been run, Watch Together over HLS has had no
two-browser session, and the new guards have only been exercised against 20-30 s fixtures. This session
changed code on the dev box only.

## 35. Admin 'Drop cache' - free one video's disk without deleting the video (Session: 2026-11-04 18:02:25)

Owner request: the Videos table had exactly one destructive action - Delete - which removes the Drive
file, the row and the local copy together. Dropping the local cache of a single video has to be its
own button: it is the manual counterpart of the LRU eviction 34.1 made safe, and on a 10 GB disk it is
the difference between refusing a repair and running it.

### 35.0 Pending Changes

| # | File(s) | Change | Why | Blast radius |
|---|---------|--------|-----|--------------|
| 35.1 | `app/storage.py` | new public `cache_footprint(base_dir, video)` (bytes one video's MP4 plus stream tree occupy, built on the existing `_entry_bytes`), `cache_drop_blocker(video)` (the Drive-copy / mid-upload rule) and `drop_local_cache(base_dir, video)` returning the bytes freed; `_evict` gains an optional `app_config` so the public path reuses the same removal code | the eviction path already knows how to delete a folder-shaped cache entry correctly (rmtree on the tree, prune the empty `<hash>/`, clear `local_filename` / `stream_dir` / `stream_state`). Copying it for a button is how the two drift apart | storage |
| 35.2 | `app/routes/admin.py` | new `POST /admin/videos/<id>/cache/delete` (`delete_video_cache`) plus a `_video_action_answer` helper so the action answers JSON to the async table and flash + redirect to a plain form | 16.1 rule: a mutating admin action updates the table without a page reload | admin routes |
| 35.3 | `app/templates/admin_videos.html` | a Drop cache form (`data-async data-async-action="drop-cache"`) whose confirm line names what survives, `data-cached` and `data-stream-state` hooks on the two state cells, and a page handler that flips them to no / none and disables the button | the row has to show the new truth immediately, and the confirm dialog is where the admin learns that Drive, the row, the cover and the comments are untouched | admin videos table |
| 35.4 | `testing/probe_admin_cache_drop.py` (new) | build a foldered cache entry, drop it through the route, assert the tree is gone, the flags are cleared, the Drive id and the row survive and the freed bytes are reported; assert the three refusals (no Drive copy, `status='uploading'`, `stream_state` building / repairing) | those refusals are the only thing standing between this button and a deleted original | testing |

Refusals chosen for 35.1/35.2: no `google_drive_file_id` (the local file would then be the only copy -
the same rule `hls._drop_local_source` enforces), `status='uploading'` (the file is being written right
now), and `stream_state` in `building` / `repairing` (ffmpeg owns that tree and would be writing into a
directory being rmtree'd).

### 35.1 Change Log (2026-11-04 18:09:10)

- **35.1** `app/storage.py`: three public helpers built on the eviction code -
  `cache_footprint(base_dir, video)` (sums `_entry_bytes` over `local_filename` and
  `stream_dir`, so a foldered entry counts its segments), `cache_drop_blocker(video)` (the
  refusal text, or None) and `drop_local_cache(base_dir, video)` (measure, then `_evict`,
  return the bytes). `_evict` gained `app_config: Optional[dict] = None` because the public
  path has no config to pass while every eviction caller still does. The rule lives in
  storage rather than in the route because 34.1 just demonstrated that a cache removal
  without the Drive-copy / mid-upload rule is a data-loss bug, and a second copy of that
  rule inside a button is how the two drift apart.
- **35.2** `app/routes/admin.py`: `_video_action_answer(ok, message, **extra)` answers JSON
  to a `data-async` form and flash + redirect otherwise (16.1, 400 on refusal), plus
  `POST /admin/videos/<id>/cache/delete` -> `delete_video_cache`. Guard order: the row exists;
  no stream job owns the tree (`building` / `repairing`); `cache_drop_blocker`; then the
  nothing-cached case, which is an honest success rather than an error. Success logs
  `admin: id=N local cache dropped (B bytes freed)` and answers `{ok, message, video_id,
  freed_bytes, cached: false, stream_state: none}`. `_fmt_mb` renders the freed size as MB so
  this flash reads like the headroom flashes of 34.3.
- **35.3** `app/templates/admin_videos.html`: a Drop cache button between Repair GOP and
  Delete, `data-async data-async-action="drop-cache"`, with a confirm line that names what
  survives; `data-cached` on the Cached cell and `data-stream-state` on the HLS badge; the
  page handler flips both to no / none and disables the button so a second click cannot
  pretend to free the same bytes again.
- **35.4** `testing/probe_admin_cache_drop.py` (new): footprint, form wiring, the drop, the
  three refusals with every fixture file asserted still on disk, the nothing-cached no-op and
  the non-AJAX 302 path. Each fixture case is 21,219 B (4,096 B MP4 + playlist + init +
  2 segments) in the folder shape 31.4 introduced.

### 35.2 What the button does and what it refuses to do

| it does | it does not |
|---------|-------------|
| delete `storage/videos/<hash>/<hash>.mp4` and `storage/videos/<hash>/stream/`, pruning the empty `<hash>/` | touch the Drive file, the row, title, description, tags, cover, comments, view counts or folder placement |
| clear `local_filename` and `stream_dir` and set `stream_state` back to `none`, so the watch page stops advertising a stream that is gone | take the video out of home / search / My Videos - it is still a Drive-backed video, playable through Download and play |
| report the exact bytes freed in the toast and in `app.log` | act on a video whose Drive mirror has not completed - refused by design |

Refusals, all asserted by 35.4: no `google_drive_file_id`; `status` is `uploading`;
`stream_state` is `building` or `repairing`.

### 35.3 Verification (2026-11-04 18:09:10, project venv)

- `python -m compileall -q app main.py wsgi.py testing` - exit 0.
- `python -m pyright app` - 0 errors, 0 warnings, 0 informations.
- `python testing/probe_admin_cache_drop.py` - ALL DROP-CACHE PROBES PASSED, printing the three
  refusal messages verbatim and `freed 21219 B, row kept with its Drive id`.
- `python testing/run_probes.py --timeout 90` - **20/20 probes passed** (the suite grew with 35.4).

### 35.4 Deferred from this section

- The button is admin-only (`@admin_required`, under `/admin`). Mirroring it on the owner page
  (`app/templates/user.html`) needs the same ownership check `user.delete_video` does - not
  requested, not built.
- The Cached column still says yes / no rather than a byte size. Per-row footprints would mean
  a stat walk per row on every render (86 cached rows on the VPS, each with a segment tree);
  the freed-bytes number is reported at drop time instead.
- Nothing of 35 has run on the VPS: it is dev-side code committed on top of 34, and it changes
  no behaviour until `hls_enabled` is flipped and a stream tree actually exists to drop.

## 36. Batch stream builds - Rebuild all / Repair all (Session: 2026-11-04 19:20:31)

Owner request in plain terms: clicking Rebuild stream one row at a time is too slow, so
every rebuild-related control needs a Rebuild all next to it. On the VPS "one row at a
time" is 86 cached rows - 86 confirm dialogs, 86 POSTs, 86 badges to watch, 86 chances
to click the wrong one. A batch here is not a new kind of build: it is the same
``hls.ensure_stream`` call made in order by one background thread, with one button that
starts it, one banner that reports it, and one button that stops it.

### 36.0 Pending Changes (2026-11-04 19:20:31)

| # | File(s) | Change | Why | Blast radius |
|---|---------|--------|-----|--------------|
| 36.1 | `app/batch.py` (new) | one-slot sequential batch runner built on top of `hls.ensure_stream`: `status()`, `start(videos_dir, app_config, mode)`, `request_stop()`, per-mode candidate rules, a per-item 33.8 headroom pre-check, a cancel flag, and one progress dict the page renders | 32.4/33.8 established that every build must reserve its disk before ffmpeg starts; a batch that re-checks nothing per item is how 86 builds turn into 86 `failed` badges and an over-full cache. The rules belong in one module rather than in the route so a batch and a single row cannot drift apart | hls batch runner, storage headroom |
| 36.2 | `app/routes/admin.py` | four routes - `POST /admin/videos/stream/rebuild-all`, `POST /admin/videos/stream/repair-all`, `POST /admin/videos/stream/batch/stop`, `GET /admin/videos/stream/batch` - behind one `_batch_action` gate that reuses the `_stream_action` refusals (HLS off / no ffmpeg / repair off / nothing to do); `_video_action_answer` now also echoes the refusal into `error` | the two start buttons must refuse for exactly the reasons a per-row button refuses, and async.js toasts `j.error`, so a refusal that only fills `message` reaches the admin as "Something went wrong" | admin routes, admin videos async wiring |
| 36.3 | `app/templates/admin_videos.html`, `app/static/css/style.css` | a toolbar above the table (Rebuild all, Repair all, Stop while running) and a `#hls-batch` banner server-rendered from `batch.status()`, polled every 3 s only while a batch is running | 16.1: a mutating admin action updates the page without a reload; the banner is also where the ~1 s-per-second-of-video cost of a repair is stated before the owner commits to it | admin videos page |
| 36.4 | `testing/probe_batch_rebuild.py` (new) | stub `hls.ensure_stream` (no ffmpeg) and assert candidate selection per mode, the skip rules, double-start refusal, the headroom skip, stop between items, the JSON routes and the rendered buttons | the candidate rules and the skip-vs-abort choice are the whole design; without a probe they are a paragraph in this document | testing |

Four decisions this section is built on:

1. **One slot, strictly sequential.** The VPS is 1 vCPU / 956 MiB and `hls.py` already
   serialises repairs with `_repair_slot` (32.4). 86 parallel ffmpegs on that box is the
   OOM story 32.0 already measured with `libsvtav1`. `start` therefore refuses while a
   batch is running, and the worker walks the candidate list one video at a time.
2. **Candidate rules instead of a force flag.**

   | button | candidate | skipped |
   |--------|-----------|---------|
   | Rebuild all (copy) | has a local MP4 **and** `stream_state` is `none` / `failed` | `ready` (any source - it is already playable), `building` / `repairing` (a job owns that tree), no local copy, row deleted mid-run |
   | Repair all (repair) | has a local MP4 **and** is not already `ready (repair)` | `building` / `repairing`, `ready (repair)`, no local copy, row deleted mid-run |

   `ensure_stream` keeps its 32.3 behaviour - the per-row buttons still re-cut a tree
   built by the other mode - and a batch deliberately does not: rebuild-all never
   destroys a working stream, repair-all never re-encodes a video it already repaired.
   No `force` parameter was added (deferred, 36.5).
3. **Skip, never abort.** Each item gets the same headroom pre-check `_stream_action`
   does (32.4 + 33.8: free space minus the reserve, against source bytes x 1.35 for a
   repair and x 2.0 for a copy). A 1 GB file that cannot fit is recorded as skipped and
   the batch moves on to the files that can. Aborting the whole batch would be 86
   one-click attempts wearing a different label.
4. **Stop means between videos.** `request_stop()` sets a flag the worker checks before
   the next item; an ffmpeg already running finishes and its tree lands normally. A
   killed-in-the-middle encode is how a `.tmp` tree gets abandoned.

The honest number that comes with decision 2: repair is roughly 1 s of single-core per
second of video (32.0), so for the measured VPS library (14,384.8 s cached) repair-all
is about 4 h of one core at 0.65x realtime and about 6 h at 1.0x. `start` sums the
durations of the candidate set and puts the estimate in both the response message and
the banner, so the owner reads the cost before committing to it rather than after.


### 36.1 Change Log (2026-11-04 19:40:36)

- **36.1** `app/batch.py` (new, 408 lines): one process-wide batch slot (`_guard` +
  `_state` + `_cancel`) with three public functions - `status()`,
  `start(videos_dir, app_config, mode)` and `request_stop()` - plus `plan()` and
  `blocker_for()`, which are the candidate rules of 36.0 decision 2 in one place so a
  batch and a per-row button cannot disagree about who is ready. The worker thread
  re-reads its own row and re-runs both rules per item (a viewer, an eviction or a
  Drop cache can have changed the picture since the queue was built), then calls
  `hls.ensure_stream` **as a module attribute**, which is what lets 36.4 test all of
  this without ffmpeg. It owns no ffmpeg logic and touches no Drive object. The
  summary line is the same text the banner shows and the log gets.
- **36.2** `app/routes/admin.py`: `_batch_action(mode)` reuses the four single-row
  refusals (HLS off / no ffmpeg / repair off / nothing to queue) and four routes -
  `POST /admin/videos/stream/rebuild-all`, `POST /admin/videos/stream/repair-all`,
  `POST /admin/videos/stream/batch/stop`, `GET /admin/videos/stream/batch`. `videos()`
  now passes `hls_batch=batch.status()` so the banner renders from the same dict the
  poller reads. `_video_action_answer` also copies a refusal into `error`, which fixes
  a 35 gap: async.js toasts `j.error`, so every 35 refusal (and every 36 refusal)
  reached the admin as "Something went wrong" instead of as its reason.
- **36.3** `app/templates/admin_videos.html` + `app/static/css/style.css`: a toolbar
  above the table (Rebuild all, Repair all, Stop batch) and a `#hls-batch` banner with
  a `.progress` bar, the summary line and up to 12 per-item notes. The poller runs
  every 3 s only while a batch is running, stops itself when the slot is free, and
  toasts "refresh the page for the new Stream badges" at the end - the Stream badges
  in the table are server-rendered and are deliberately not rewritten row by row.
- **36.4** `testing/probe_batch_rebuild.py` (new, 314 lines): six fixture rows (a
  plain one, ready-copy, ready-repair, mid-build, no local file, and a 40 MB one),
  `hls.ensure_stream` replaced by a recorder that also moves the DB row, and
  `db.list_all_videos` replaced by the fixture list so no case can queue a build on a
  video this box really owns.

### 36.2 What each button queues

| button | queued | refused before the queue is even built |
|--------|--------|----------------------------------------|
| Rebuild all | local MP4 present and `stream_state` is `none` / `failed` | HLS off, no ffmpeg, a batch already running, nothing to queue |
| Repair all | local MP4 present and not already `ready (repair)` - so a `ready (copy)` row is queued, a sparse-GOP row is what this exists for | the same four, plus `hls_repair_enabled = false` |
| Stop batch | nothing: it ends the queue at the next item | "No batch is running." |

Rows skipped *inside* a running queue are counted and named: `building` /
`repairing` (a job owns that tree), `ready` (rebuild-all never re-cuts a working
stream, repair-all never re-encodes a repaired video), no local copy, row deleted
mid-run, and a per-item headroom refusal. A headroom refusal skips that row and
continues - measured output from 36.4:

```
batch: copy queued 2 video(s), skipped 4, estimated about 1 s on one core
batch: id=142 -> ready (-)
batch: id=147 -> skipped (needs ~80 MB of headroom, this server has 20 MB: drop a cache first)
batch: Rebuild batch finished: 2 of 2 video(s) processed - 1 ready, 1 skipped, 0 failed, 0 discarded
```

Two things the banner says out loud, because both are the cost of using this: a repair
is roughly 1 s of single-core per second of video (32.0), so repair-all on the 14,384.8
s cached seconds of the VPS library is ~4 h at the measured 0.65x realtime and ~6 h at
1.0x; and a copy rebuild on the same library is ~8 min. Those numbers are what
`estimated_seconds` / `estimate_text` in `status()` put in the response message before
the queue starts.

### 36.3 Verification (2026-11-04 19:40:36, project venv)

- `python -m compileall -q app main.py wsgi.py testing` - exit 0.
- `python -m pyright app` - 0 errors, 0 warnings, 0 informations.
- `python testing/probe_batch_rebuild.py` - ALL BATCH PROBES PASSED: the copy plan
  queued ids 142/147 and skipped 143/144/145/146 with the four reason strings
  asserted verbatim; the repair plan queued 142/143/147 (a `ready (copy)` row *is* a
  repair candidate) and skipped 144 ("already repaired"); the second `start` refused
  ("A Repair batch is already running: 0 of 3 video(s) done"); the stop landed between
  items (2 of 3 processed, `stopped = true`); the 40 MB row was skipped while the 1 MB
  row was built; the discarded copy tree landed in its own bucket with the note
  "copy-only tree discarded - needs Repair GOP"; an empty queue refused with
  "Nothing to queue: 6 row(s) were skipped..."; every refusal JSON carried
  `error == message`; and `/admin/videos` rendered Rebuild all, Repair all, the idle
  (hidden) Stop batch, and the `data-batch-*` hooks.
- `python testing/run_probes.py --timeout 90` - **21/21 probes passed** (the suite grew
  with 36.4).
- One honest incident on the way: the first 36.4 run crashed inside its own fixture
  setup (`NOT NULL constraint failed: videos.stream_source` - the column is `NOT NULL
  DEFAULT ''`, so a fixture must write `""` and not `None`) before its teardown list
  was filled, which left row id=135 `Probe Batch A` in the dev DB pointing at a path a
  later run deleted. `probe_home_cards.py` and `probe_no_reload.py` then failed because
  the home page hides that row. It was removed with a one-off untracked helper
  (`testing/_purge_batch.py`, which only matches `Probe Batch%` titles), 36.4 was
  hardened so a fixture that fails mid-creation deletes its own row and folder, and the
  suite re-ran green. `testing/_peek_batch.py` is the read-only version of the same check.

### 36.4 Deferred from this section

- **No force flag.** `ensure_stream` still re-cuts a tree built by the other mode when
  a *per-row* button asks for it (32.3); a batch deliberately never does. If an owner
  ever wants "re-cut everything including the good ones", that is a `force` parameter
  on `plan`/`start` and a confirm line, not a change to these rules.
- **A restart abandons the queue.** The batch slot is process memory: a redeploy or a
  restart mid-run leaves the banner idle on the next page load and can leave the row
  that was mid-ffmpeg at `building` / `repairing`. Both the batch and Drop cache treat
  those states as "a job owns it", so such a row is then skipped by everything until it
  is cleared. Clearing it honestly needs job ownership (pid + heartbeat or a lease),
  not a guess from a timestamp.
- **The banner does not rewrite the table.** Stream badges are server-rendered, so the
  poller says "refresh the page" when a batch ends rather than patching 86 rows from
  JSON. A per-row live badge would need the batch to report each row state and the row
  to carry `data-video-id` - not built, not needed for the button to be useful.
- 35.4 deferrals (byte sizes in the Cached column, no owner-side mirror on
  `user.html`) still stand, and so does 34.4: the ~891 MB of orphan cache files on this
  dev box belongs to no DB row, so neither `actual_cache_bytes`, the LRU evictor nor a
  batch can see or reclaim them.
- Nothing of 36 has run against a real video anywhere: `hls_enabled` is still absent
  from the dev `config.json`, no `videos.stream_*` columns exist on the VPS yet, and
  every batch in this section was executed by the 36.4 recorder.


## 37. Deleting a user works now: two FK defects, one 500, and the files left behind (Session: 2026-11-04 22:14:07)

### 37.0 The report, and the diagnosis done before writing any code

Report: pressing **Delete** on a user in Admin → Users produced only
"Something went wrong."

That sentence is `app/static/js/async.js`'s fallback (`j.error || 'Something went
wrong.'`), and it means exactly one thing: the server answered **non-JSON**, i.e. a 500.
Measured before any fix was written:

- `app/routes/admin.py::delete_user` called `db.delete_user` with no `try`, so any
  `sqlite3.IntegrityError` escaped as a Flask 500 HTML page that `async.js` could not
  parse. Same class of gap as the one 36.1 closed for the Videos table.
- Two FK shapes in the live schema made that IntegrityError near-certain for a real
  user. Both were read out of the production database over ssh first
  (`PRAGMA table_info(notifications)` → `actor_id` notnull = 1;
  `PRAGMA foreign_key_list(live_rooms)` → `owner_id` → `NO ACTION`), and reproduced
  minimally (`testing/_fk_check.txt`): `actor delete FAILED: NOT NULL constraint
  failed: notifications.actor_id`.
- The VPS counts said who would fail: id=2 VSakuya (1 room, 6 authored notifications),
  id=3 DD1969 (1 room, 4), id=5 Birdsta (14), id=6 Lyushin (7), id=8 test (2). Only id=1
  admin and id=7 CTX had nothing blocking them.
- A third and quieter defect: the cascade removed a user's video **rows** and nothing
  else. Deleting DD1969 would have dropped 69 rows and left 69 Drive files, MP4 caches,
  HLS segment trees, covers and an avatar behind, with no row left that could name them.

### 37.1 notifications.actor_id is nullable

`ON DELETE SET NULL` cannot run against a `NOT NULL` column: rather than skipping the
action, SQLite aborts the whole parent delete. The column is plain `INTEGER` now, and the
row keeps its denormalized `actor_username` / `actor_avatar_filename` snapshot - which is
the whole reason those columns exist (§21): another user's inbox still renders
"Tester commented on ..." after Tester is gone.

- `app/sql/schema.sql` and `app/sql/migrations/create_notifications.sql` carry the
  corrected DDL, so a fresh database is born right.
- `app/sql/migrations/notifications_fix_actor_fk.sql` repairs an existing one in place:
  create `notifications__new`, copy every column explicitly, drop, rename, and recreate
  `idx_notifications_user` / `idx_notifications_unread` (dropping a table drops its
  indexes, including the partial one that makes the unread badge cheap).
- `db._migrate` runs it only when the live schema still reports NOT NULL, so a corrected
  database is never rebuilt again.

### 37.2 live_rooms.owner_id cascades

`live_rooms_add_owner_id.sql` (§14.8) added `owner_id INTEGER REFERENCES users(id)` with
no action, and SQLite defaults that to NO ACTION - so every user who owned a live room was
undeletable, whatever else was clean. Rooms belong to their owner, so the cascade is the
intent, and `live_messages` already cascaded with its room, so room and chat go together.

- `app/sql/migrations/live_rooms_fix_owner_fk.sql` (same rebuild shape as
  `live_rooms_drop_code.sql`), detected by `PRAGMA foreign_key_list` rather than assumed.
- Both rebuilds run through one new helper, `db._rebuild_table`: commit (the pragma is a
  no-op inside a transaction), `PRAGMA foreign_keys = OFF`, executescript, commit,
  `PRAGMA foreign_key_check`, `foreign_keys = ON` - and a `RuntimeError` naming the
  dangling rows if that check is not empty. A live database is never left holding broken
  references.
- New schema-reading helpers next to them: `db._column_is_not_null`, `db._fk_on_delete`.
### 37.3 The route answers instead of crashing

`_video_action_answer` grew into `_action_answer(endpoint, ok, message, **extra)` plus two
one-line shorthands (`_video_action_answer`, `_user_action_answer`), so the Users table
gets the 36.1 rule - every refusal fills `error`, so `async.js` toasts the reason instead
of the fallback text - without redirecting to the Videos page.

`admin.delete_user` now refuses a self-delete and an unknown id with a stated reason,
purges the user's files, calls `db.delete_user` inside `try / except` with
`logger.exception` (the traceback lands in `app.log`, where before it never arrived at
all), and on success reports what went with them:

    Deleted 'DD1969'. 69 videos, 7 comments and 1 live room went with them.
    Freed 1234.5 MB of local cache.

`_fmt_mb` is reused above 1 MB and kilobytes are printed below it, so a small delete
cannot report "Freed 0.0 MB" for files that really did go.

`db.delete_user` returns the impact dict it measured **before** the delete
(`sql/users/counts.sql` via `db.user_impact`) because after the cascade the numbers no
longer exist anywhere. The same three counts were added to `sql/users/list.sql` and
`_user_row.html` renders them under the username, so the admin reads "18 videos · 35
comments · 1 live room" before pressing a button whose confirm text now names what it
owns and says it cannot be undone.

### 37.4 Files go before rows

`storage.purge_video_files(video, base_dir, covers_dir)` is the one definition of "delete
a video's files": the Drive copy (lazy `from . import drive`, the same reason
`is_locally_playable` imports `hls` lazily - storage stays importable on its own), the
cover, then `drop_local_cache`, which is 35's `_evict` path: MP4 copy, HLS segment tree,
and the empty `<hash>/` folder pruned once both are gone. `storage.remove_stored_file` is
the small shared best-effort unlink used for covers, room covers and avatars; both stay
best effort on purpose, same policy as the evictor, so a locked file never blocks a delete
the admin already committed to.

- `app/routes/user.py::_purge_video_files` now delegates to it. That closes an older bug
  nobody had noticed: the owner's own Delete button removed `local_filename` and the
  cover but left the stream tree, and the folder holding both, on disk.
- `admin._purge_user_files(user_id)` runs it over `db.list_videos_by_owner`, plus the
  live room's cover and the user's avatar, **before** `db.delete_user`, for exactly one
  reason: the cascade is what erases the rows that carry the paths.

### 37.5 Verification (2026-11-04 22:45:01, project venv)

- New probe `testing/probe_admin_user_delete.py`, four isolated cases so a failure names
  which defect came back: (1) the repaired schema read back from PRAGMA, and the rebuild
  idempotent - a second `init_db()` moves nothing and leaves no `*__new` table;
  (2) an actor-only user deleted, and the recipient's notification kept with
  `actor_id IS NULL` and `actor_username` still readable;
  (3) a room owner deleted with the room and its chat cascaded;
  (4) a victim with 3 videos / 2 comments / 1 room / 1 folder deleted through the route,
  with every file gone - 3 cache folders, 3 covers, 1 room cover, 1 avatar, 3 recorded
  Drive ids - while the other user, their video and both notifications addressed to them
  survived, the keeper's video kept (only the comments authored by deleted users left it)
  and the deleted row gone from the rendered Users table. Then the three refusals
  (self-delete, unknown id, and a forced `IntegrityError`) each came back as JSON 400
  with `error` filled. `drive.delete_file` is replaced by a recorder, so no probe here
  touches Google Drive.
- Case 5 was added after that first green run, because the delegation in 37.4 had
  quietly broken the owner's own Delete button while nothing tested it:
  `routes/user.py` handed `purge_video_files` the videos directory where the helper
  expects its parent, so `drop_local_cache` looked inside `videos/videos/`, reported
  0 bytes freed and left the MP4 copy and the whole stream tree on disk - with the
  DB row still gone, which is all the earlier cases actually check. Put the wrong
  argument back and the case fails as
  `the owner kept a cache folder: storage\videos\<hash>`; give it the parent and it
  passes. No probe before this one had ever posted to
  `/user/videos/<id>/delete` with real files on disk.
- `python testing/run_probes.py --timeout 90` - **22/22 probes passed** (re-run
  after case 5 landed: still 22/22; the count is probe files, and case 5 lives in
  the same file as 1-4).
- `python -m pyright app` - 0 errors, 0 warnings; `python -m compileall -q app main.py` clean.
- Rehearsal against a **copy** of the real dev DB (`testing/_peek_copy.py`, throwaway:
  `db.DB_PATH` repointed at `storage/data/_copy.db`, file deleted at the end). Before:
  users 2, videos 8, comments 6, live_rooms 2, notifications 2, folders 1. After the
  migrations: `actor_id` notnull 0 and `owner_id → CASCADE`. `db.delete_user(22)` -
  Tester, the exact case that used to abort - and `db.delete_user(2)` - admin, 8 videos +
  2 rooms - both returned their impact dict and left `PRAGMA foreign_key_check` empty.
- The dev DB itself (migrated by the probe run) was re-checked afterwards: all 8 videos,
  6 comments, 2 rooms and both notifications survived the table rebuild,
  `foreign_key_check` and `integrity_check` both clean, no scratch tables left.

### 37.6 Deferred from this section

- **None of this is deployed.** The VPS still runs the pre-31 code with both broken FK
  definitions, so production fails the same way until this is pushed and the service
  restarted. `db.init_db()` repairs 37.1 and 37.2 on the first start after a pull, but
  a pull ships 31 through 37 together - HLS trees, the hash-folder cache layout,
  Rebuild all and Repair all - which makes deployment a decision, not a side effect.
- **Deleting a user who owns videos now deletes their Drive files too.** Purging
  before the cascade is what the button claims to do, but there is no "unlink the
  account, keep the videos" mode and no recovery path. The per-user counts in the
  Users table and the rewritten confirm text are the only warning before it runs.
- **New orphans only.** What earlier deletes already stranded - Drive copies, cache
  folders and stream trees that no row points at - is still unreclaimed, so 34.4
  (~891 MB on this dev box) and the 36.4 deferrals stand unchanged.
- **`drive.delete_file` has never been exercised end to end.** Every probe in 37.5
  replaces it with a recorder, so the Drive-side deletion is verified by the arguments
  it was called with, not by a file actually disappearing from Drive.

