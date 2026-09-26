-- Simple Video Share — SQLite schema (source of truth).
-- Loaded by db.init_db(). For new tables/columns, add a new .sql file under sql/
-- and apply it in order (see db._migrate for additive changes to live DBs).

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT    NOT NULL UNIQUE,
    password_hash   TEXT    NOT NULL,
    is_admin             INTEGER NOT NULL DEFAULT 0,
    avatar_filename      TEXT,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    last_login_at        TEXT,
    created_at           TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS folders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    owner_id    INTEGER NOT NULL,
    parent_id   INTEGER,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (owner_id)  REFERENCES users   (id) ON DELETE CASCADE,
    FOREIGN KEY (parent_id) REFERENCES folders (id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS videos (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    title                 TEXT    NOT NULL,
    description           TEXT,
    owner_id              INTEGER NOT NULL,
    folder_id             INTEGER,
    google_drive_file_id  TEXT,
    drive_path            TEXT,
    drive_filename        TEXT,
    drive_resumable_uri   TEXT,
    local_filename        TEXT,
    cover_filename        TEXT,
    duration              REAL,
    resolution            TEXT,
    codec                 TEXT,
    bitrate               INTEGER,
    fps                   REAL,
    size_bytes            INTEGER,
    status                TEXT    NOT NULL DEFAULT 'ready',
    view_count            INTEGER NOT NULL DEFAULT 0,
    is_pinned             INTEGER NOT NULL DEFAULT 0,
    last_accessed         TEXT,
    created_at            TEXT    NOT NULL DEFAULT (datetime('now')),
    uploaded_at           TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (owner_id)  REFERENCES users   (id) ON DELETE CASCADE,
    FOREIGN KEY (folder_id) REFERENCES folders (id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Failed login attempts, used for the sliding-window lockout (anti-stuffing).
CREATE TABLE IF NOT EXISTS login_attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    username     TEXT    NOT NULL,
    ip           TEXT    NOT NULL,
    attempted_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Comments on videos (plain text, kaomoji allowed, no HTML). Authors are users;
-- deleting a video or a user cascades away their comments.
-- Two-level nesting: parent_id NULL = top-level; non-NULL = reply to that comment.
CREATE TABLE IF NOT EXISTS comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id    INTEGER NOT NULL,
    author_id   INTEGER NOT NULL,
    parent_id   INTEGER,
    body        TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (video_id)  REFERENCES videos (id) ON DELETE CASCADE,
    FOREIGN KEY (author_id) REFERENCES users  (id) ON DELETE CASCADE,
    FOREIGN KEY (parent_id) REFERENCES comments (id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_comments_video ON comments (video_id, id);

-- Live streaming rooms (§13.41, link-based per §14.6, per-user per §14.8).
-- ``url`` is the full stream link (an internal "/live/<code>.flv" or any
-- external URL); the live page is /live/<id>. Each user owns at most one room
-- (``owner_id``); a user's room shows in the public live list and is managed
-- from the user's own account page. ``cover_filename`` is stored under the
-- shared covers directory and served by the home.cover route.
CREATE TABLE IF NOT EXISTS live_rooms (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT,
    title           TEXT    NOT NULL,
    description     TEXT,
    owner_id        INTEGER REFERENCES users(id),
    cover_filename  TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
