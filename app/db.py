"""Database layer: SQLite schema, connection helpers, and CRUD queries."""

import sqlite3
from pathlib import Path
from typing import Optional, Any

# Anchor to the project root so the DB lives under ``storage/`` regardless of
# the current working directory.
_ROOT_DIR = Path(__file__).resolve().parents[1]
DB_DIR = _ROOT_DIR / "storage" / "data"
DB_PATH = DB_DIR / "app.db"


def _ensure_dirs() -> None:
    """Create data directory if it does not exist."""
    DB_DIR.mkdir(parents=True, exist_ok=True)


def get_db() -> sqlite3.Connection:
    """Return a new SQLite connection with row factory set."""
    _ensure_dirs()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _load_schema() -> str:
    """Return the SQL schema text from ``sql/schema.sql`` (source of truth)."""
    schema_path = Path(__file__).parent / "sql" / "schema.sql"
    return schema_path.read_text(encoding="utf-8")


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply small additive migrations to pre-existing databases (idempotent).

    Fresh databases already have these columns from ``sql/schema.sql``; this only
    back-fills columns on older DBs so a live install does not break.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "avatar_filename" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN avatar_filename TEXT")
    if "must_change_password" not in cols:
        conn.execute(
            "ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0"
        )

    video_cols = {row[1] for row in conn.execute("PRAGMA table_info(videos)").fetchall()}
    if "drive_filename" not in video_cols:
        conn.execute("ALTER TABLE videos ADD COLUMN drive_filename TEXT")
    if "description" not in video_cols:
        conn.execute("ALTER TABLE videos ADD COLUMN description TEXT")
    if "status" not in video_cols:
        # Existing rows were uploaded synchronously (the Drive upload blocked the
        # HTTP request), so every pre-existing video is already 'ready'.
        conn.execute(
            "ALTER TABLE videos ADD COLUMN status TEXT NOT NULL DEFAULT 'ready'"
        )
    if "drive_resumable_uri" not in video_cols:
        conn.execute("ALTER TABLE videos ADD COLUMN drive_resumable_uri TEXT")

    comment_cols = {row[1] for row in conn.execute("PRAGMA table_info(comments)").fetchall()}
    if "parent_id" not in comment_cols:
        conn.execute("ALTER TABLE comments ADD COLUMN parent_id INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_parent ON comments (parent_id)")


def init_db() -> None:
    """Create all tables (from ``sql/schema.sql``), migrate, and seed defaults."""
    _ensure_dirs()
    conn = get_db()
    conn.executescript(_load_schema())
    _migrate(conn)
    # Seed default settings (only if table is empty)
    cur = conn.execute("SELECT COUNT(*) FROM settings")
    count = cur.fetchone()[0]
    if count == 0:
        defaults = {
            "default_bitrate": "5000",
            "default_codec": "av1",
            "fallback_codec": "h264",
            "max_resolution": "1080",
            "max_fps": "60",
            "min_free_space_bytes": str(500 * 1024 * 1024),
            "max_cache_mb": "0",
            "cache_root": "videos",
            "covers_root": "covers",
        }
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?)",
            list(defaults.items()),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def create_user(
    username: str,
    password_hash: str,
    is_admin: Optional[int] = None,
    must_change_password: int = 0,
) -> int:
    """Insert a user.

    ``is_admin``: ``None`` auto-sets admin for the very first user (bootstrap);
    otherwise the given 0/1 flag is used (admin "create user").
    ``must_change_password``: flag the account so its first login is forced
    through a mandatory password change (see the auth blueprint).
    """
    conn = get_db()
    if is_admin is None:
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        is_admin = 1 if count == 0 else 0
    cur = conn.execute(
        "INSERT INTO users (username, password_hash, is_admin, must_change_password) "
        "VALUES (?, ?, ?, ?)",
        (username, password_hash, is_admin, must_change_password),
    )
    conn.commit()
    user_id = cur.lastrowid
    assert user_id is not None
    conn.close()
    return user_id


def ensure_default_admin() -> None:
    """Create the default ``admin``/``admin`` account on first boot.

    Runs on every startup but is a no-op once any user exists. The bootstrap
    account is flagged ``must_change_password`` so its first login is forced
    through a mandatory password change.
    """
    if count_users() > 0:
        return
    from werkzeug.security import generate_password_hash
    create_user(
        "admin",
        generate_password_hash("admin"),
        is_admin=1,
        must_change_password=1,
    )


def get_user_by_username(username: str) -> Optional[dict[str, Any]]:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> Optional[dict[str, Any]]:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def list_users() -> list[dict[str, Any]]:
    conn = get_db()
    rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def count_users() -> int:
    conn = get_db()
    row = conn.execute("SELECT COUNT(*) FROM users").fetchone()
    conn.close()
    return int(row[0]) if row else 0


def update_user(user_id: int, **kwargs: Any) -> None:
    if not kwargs:
        return
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [user_id]
    conn = get_db()
    conn.execute(f"UPDATE users SET {sets} WHERE id = ?", vals)
    conn.commit()
    conn.close()


def delete_user(user_id: int) -> None:
    conn = get_db()
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()


# --- login lockout (anti credential-stuffing) -----------------------------

#: Max failed attempts for one username within ``LOGIN_WINDOW_MINUTES``.
LOGIN_MAX_ATTEMPTS = 5
#: Max failed attempts from one IP within ``LOGIN_WINDOW_MINUTES``.
LOGIN_IP_MAX_ATTEMPTS = 20
#: Sliding window (minutes) used to count recent failed attempts.
LOGIN_WINDOW_MINUTES = 15


def _prune_old_attempts(conn: sqlite3.Connection) -> None:
    """Drop failed-attempt rows older than a day to keep the table small."""
    conn.execute(
        "DELETE FROM login_attempts WHERE attempted_at < datetime('now', '-1 day')"
    )


def record_failed_login(username: str, ip: str) -> None:
    conn = get_db()
    _prune_old_attempts(conn)
    conn.execute(
        "INSERT INTO login_attempts (username, ip, attempted_at) "
        "VALUES (?, ?, datetime('now'))",
        (username, ip),
    )
    conn.commit()
    conn.close()


def clear_failed_logins(username: str) -> None:
    conn = get_db()
    conn.execute("DELETE FROM login_attempts WHERE username = ?", (username,))
    conn.commit()
    conn.close()


def login_lockout_seconds(username: str, ip: str) -> int:
    """Return seconds until the given username or IP may attempt login again.

    ``0`` means not locked. Applies the per-username and per-IP sliding-window
    limits and returns whichever would keep the caller out longer.
    """
    from datetime import datetime, timedelta, timezone

    window = LOGIN_WINDOW_MINUTES
    remaining = 0
    conn = get_db()
    checks = (
        ("username", username, LOGIN_MAX_ATTEMPTS),
        ("ip", ip, LOGIN_IP_MAX_ATTEMPTS),
    )
    for column, value, limit in checks:
        row = conn.execute(
            f"SELECT MIN(attempted_at) AS oldest, COUNT(*) AS n "
            f"FROM login_attempts WHERE {column} = ? "
            f"AND attempted_at > datetime('now', ?)",
            (value, f"-{window} minutes"),
        ).fetchone()
        if row and row["n"] >= limit:
            oldest = datetime.strptime(row["oldest"], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
            unlock = oldest + timedelta(minutes=window)
            remaining = max(
                remaining, int((unlock - datetime.now(timezone.utc)).total_seconds())
            )
    conn.close()
    return remaining


# ---------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------

def create_folder(name: str, owner_id: int) -> int:
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO folders (name, owner_id) VALUES (?, ?)", (name, owner_id)
    )
    conn.commit()
    folder_id = cur.lastrowid
    assert folder_id is not None
    conn.close()
    return folder_id


def list_folders(owner_id: int) -> list[dict[str, Any]]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM folders WHERE owner_id = ? ORDER BY name", (owner_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def rename_folder(folder_id: int, new_name: str) -> None:
    conn = get_db()
    conn.execute("UPDATE folders SET name = ? WHERE id = ?", (new_name, folder_id))
    conn.commit()
    conn.close()


def delete_folder(folder_id: int) -> None:
    conn = get_db()
    conn.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Videos
# ---------------------------------------------------------------------------

def create_video(
    title: str,
    owner_id: int,
    folder_id: Optional[int],
    local_filename: Optional[str],
    cover_filename: Optional[str],
    size_bytes: int,
    description: Optional[str] = None,
    duration: Optional[float] = None,
    resolution: Optional[str] = None,
    codec: Optional[str] = None,
    bitrate: Optional[int] = None,
    fps: Optional[float] = None,
    google_drive_file_id: Optional[str] = None,
    drive_path: Optional[str] = None,
    drive_filename: Optional[str] = None,
    status: str = "ready",
) -> int:
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO videos
           (title, owner_id, folder_id, local_filename, cover_filename,
            size_bytes, description, duration, resolution, codec, bitrate, fps,
            google_drive_file_id, drive_path, drive_filename, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (title, owner_id, folder_id, local_filename, cover_filename,
         size_bytes, description, duration, resolution, codec, bitrate, fps,
         google_drive_file_id, drive_path, drive_filename, status),
    )
    conn.commit()
    video_id = cur.lastrowid
    assert video_id is not None
    conn.close()
    return video_id


def get_video_by_id(video_id: int) -> Optional[dict[str, Any]]:
    conn = get_db()
    row = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_all_videos() -> list[dict[str, Any]]:
    conn = get_db()
    rows = conn.execute(
        """SELECT v.*, u.username AS owner_name, f.name AS folder_name
           FROM videos v
           JOIN users u   ON v.owner_id = u.id
           LEFT JOIN folders f ON v.folder_id = f.id
           ORDER BY v.uploaded_at DESC"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_public_videos() -> list[dict[str, Any]]:
    """Return only fully-synced videos (``status='ready'``) for the home page.

    Videos still uploading to Drive (``status='uploading'``) or that failed
    (``status='failed'``) are cached locally and viewable by their owner but are
    hidden from the shared home gallery until the Drive upload completes.
    """
    conn = get_db()
    rows = conn.execute(
        """SELECT v.*, u.username AS owner_name, f.name AS folder_name
           FROM videos v
           JOIN users u   ON v.owner_id = u.id
           LEFT JOIN folders f ON v.folder_id = f.id
           WHERE v.status = 'ready'
           ORDER BY v.uploaded_at DESC"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_uploading_videos() -> list[dict[str, Any]]:
    """Return videos whose Drive upload has not finished (``status='uploading'``)."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM videos WHERE status = 'uploading'"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def sum_cached_bytes() -> int:
    """Return the total recorded size of locally-cached videos (bytes).

    Used to enforce the admin-configured cache capacity cap. ``size_bytes`` is
    the upload-time size, so this is a cheap SUM with no filesystem stat.
    """
    conn = get_db()
    row = conn.execute(
        "SELECT COALESCE(SUM(size_bytes), 0) AS total FROM videos WHERE local_filename IS NOT NULL"
    ).fetchone()
    conn.close()
    return int(row["total"]) if row else 0


def list_videos_by_owner(owner_id: int) -> list[dict[str, Any]]:
    conn = get_db()
    rows = conn.execute(
        """SELECT v.*, f.name AS folder_name
           FROM videos v
           LEFT JOIN folders f ON v.folder_id = f.id
           WHERE v.owner_id = ?
           ORDER BY v.uploaded_at DESC""",
        (owner_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_video(video_id: int, **kwargs: Any) -> None:
    if not kwargs:
        return
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [video_id]
    conn = get_db()
    conn.execute(f"UPDATE videos SET {sets} WHERE id = ?", vals)
    conn.commit()
    conn.close()


def delete_video(video_id: int) -> None:
    conn = get_db()
    conn.execute("DELETE FROM videos WHERE id = ?", (video_id,))
    conn.commit()
    conn.close()


def touch_video(video_id: int) -> None:
    """Update last_accessed timestamp (for LRU eviction)."""
    conn = get_db()
    conn.execute(
        "UPDATE videos SET last_accessed = datetime('now') WHERE id = ?",
        (video_id,),
    )
    conn.commit()
    conn.close()


def list_lru_cached() -> list[dict[str, Any]]:
    """Return cached videos (have local_filename) oldest-accessed first,
    for LRU eviction. Only videos that have a Drive copy are evictable."""
    conn = get_db()
    rows = conn.execute(
        """SELECT * FROM videos
           WHERE local_filename IS NOT NULL
             AND google_drive_file_id IS NOT NULL
           ORDER BY COALESCE(last_accessed, uploaded_at) ASC"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    conn = get_db()
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (key,)
    ).fetchone()
    conn.close()
    if row:
        return row["value"]
    return default


def set_setting(key: str, value: str) -> None:
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (key, value),
    )
    conn.commit()
    conn.close()


def get_all_settings() -> dict[str, str]:
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------

def add_comment(video_id: int, author_id: int, body: str, parent_id: Optional[int] = None) -> int:
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO comments (video_id, author_id, parent_id, body) VALUES (?, ?, ?, ?)",
        (video_id, author_id, parent_id, body),
    )
    conn.commit()
    comment_id = cur.lastrowid
    assert comment_id is not None
    conn.close()
    return comment_id


def list_comments(video_id: int) -> list[dict[str, Any]]:
    """Return a video's top-level comments with their replies nested under
    ``comment['replies']``, all ordered oldest-first."""
    conn = get_db()
    rows = conn.execute(
        """SELECT c.id, c.parent_id, c.body, c.created_at,
                  u.username, u.avatar_filename
           FROM comments c
           JOIN users u ON c.author_id = u.id
           WHERE c.video_id = ?
           ORDER BY c.id""",
        (video_id,),
    ).fetchall()
    conn.close()

    # Build a map: parent_id -> list of comment dicts (ordered by id).
    top: list[dict[str, Any]] = []
    by_parent: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        comment = dict(row)
        parent = comment.pop("parent_id", None)
        if parent is None:
            top.append(comment)
        else:
            by_parent.setdefault(parent, []).append(comment)

    # Attach replies to each top-level comment.
    for comment in top:
        comment["replies"] = by_parent.get(comment["id"], [])
    return top


def get_comment_by_id(comment_id: int) -> Optional[dict[str, Any]]:
    conn = get_db()
    row = conn.execute("SELECT * FROM comments WHERE id = ?", (comment_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_comment(comment_id: int) -> None:
    """Delete a comment and all of its replies (application-layer cascade)."""
    conn = get_db()
    conn.execute("DELETE FROM comments WHERE parent_id = ?", (comment_id,))
    conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
    conn.commit()
    conn.close()
