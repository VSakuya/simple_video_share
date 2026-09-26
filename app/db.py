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


_SQL_DIR = Path(__file__).parent / "sql"
_sql_cache: dict[str, str] = {}


def _sql(feature: str, name: str) -> str:
    """Return the named query text from ``sql/<feature>/<name>.sql`` (cached).

    All CRUD SQL lives in these files (no hard-coded SQL in code, §14.1). The
    caller may substitute a ``{placeholder}`` token with a validated value.
    """
    key = f"{feature}/{name}"
    cached = _sql_cache.get(key)
    if cached is None:
        cached = (_SQL_DIR / feature / f"{name}.sql").read_text(encoding="utf-8")
        _sql_cache[key] = cached
    return cached


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply small additive migrations to pre-existing databases (idempotent).

    Fresh databases already have these columns from ``sql/schema.sql``; this only
    back-fills columns on older DBs so a live install does not break. Each DDL
    step lives in ``sql/migrations/*.sql`` (§14.1); only the "is the column
    already there?" check is inlined here.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "avatar_filename" not in cols:
        conn.execute(_sql("migrations", "users_add_avatar"))
    if "must_change_password" not in cols:
        conn.execute(_sql("migrations", "users_add_must_change"))
    if "last_login_at" not in cols:
        conn.execute(_sql("migrations", "users_add_last_login_at"))

    video_cols = {row[1] for row in conn.execute("PRAGMA table_info(videos)").fetchall()}
    if "drive_filename" not in video_cols:
        conn.execute(_sql("migrations", "videos_add_drive_filename"))
    if "description" not in video_cols:
        conn.execute(_sql("migrations", "videos_add_description"))
    if "status" not in video_cols:
        # Existing rows were uploaded synchronously (the Drive upload blocked the
        # HTTP request), so every pre-existing video is already 'ready'.
        conn.execute(_sql("migrations", "videos_add_status"))
    if "drive_resumable_uri" not in video_cols:
        conn.execute(_sql("migrations", "videos_add_drive_resumable_uri"))
    if "is_pinned" not in video_cols:
        conn.execute(_sql("migrations", "videos_add_pinned"))

    comment_cols = {row[1] for row in conn.execute("PRAGMA table_info(comments)").fetchall()}
    if "parent_id" not in comment_cols:
        conn.execute(_sql("migrations", "comments_add_parent"))
    conn.execute(_sql("migrations", "comments_index_parent"))

    folder_cols = {row[1] for row in conn.execute("PRAGMA table_info(folders)").fetchall()}
    if "parent_id" not in folder_cols:
        conn.execute(_sql("migrations", "folders_add_parent"))
    conn.execute(_sql("migrations", "folders_index_parent"))

    # Tags (§bug L67): a flat library + video<->tag junction. These are brand-new
    # tables, so a single idempotent DDL script (executescript) is all that's
    # needed on live databases; fresh ones get them from schema.sql.
    conn.executescript(_sql("migrations", "create_tags"))

    # Live rooms: code -> url (§14.6). Old rows keep their stream as
    # "/live/<code>.flv"; the auto-seeded "VSakuya" example gets a neutral title;
    # the legacy code column is then dropped (table rebuild).
    live_cols = {row[1] for row in conn.execute("PRAGMA table_info(live_rooms)").fetchall()}
    if "url" not in live_cols:
        conn.execute(_sql("migrations", "live_rooms_add_url"))
    if "code" in live_cols:
        conn.execute(_sql("migrations", "live_rooms_code_to_url"))
        conn.execute(_sql("migrations", "live_rooms_retitle_vsakuya"))
        conn.executescript(_sql("migrations", "live_rooms_drop_code"))
    # Per-user live rooms + description (§14.8).
    if "description" not in live_cols:
        conn.execute(_sql("migrations", "live_rooms_add_description"))
    if "owner_id" not in live_cols:
        conn.execute(_sql("migrations", "live_rooms_add_owner_id"))


def init_db() -> None:
    """Create all tables (from ``sql/schema.sql``), migrate, and seed defaults."""
    _ensure_dirs()
    conn = get_db()
    conn.executescript(_load_schema())
    _migrate(conn)
    # Seed default settings (only if table is empty)
    if conn.execute(_sql("settings", "count")).fetchone()[0] == 0:
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
        conn.executemany(_sql("settings", "insert"), list(defaults.items()))
    # Seed a default live room (only if the table is empty). ``url`` is a neutral
    # sample stream link (no private room name) per §14.6. The sample room has no
    # owner (it is not a user's room).
    if conn.execute(_sql("live_rooms", "list")).fetchone() is None:
        conn.execute(
            _sql("live_rooms", "insert"),
            ("https://example.com/sample.flv", "Sample Live Room", None, None, None),
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
        count = conn.execute(_sql("users", "count")).fetchone()[0]
        is_admin = 1 if count == 0 else 0
    cur = conn.execute(
        _sql("users", "insert"),
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
        _sql("users", "get_by_username"), (username,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> Optional[dict[str, Any]]:
    conn = get_db()
    row = conn.execute(
        _sql("users", "get_by_id"), (user_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def list_users() -> list[dict[str, Any]]:
    conn = get_db()
    rows = conn.execute(_sql("users", "list")).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def count_users() -> int:
    conn = get_db()
    row = conn.execute(_sql("users", "count")).fetchone()
    conn.close()
    return int(row[0]) if row else 0


def update_user(user_id: int, **kwargs: Any) -> None:
    if not kwargs:
        return
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [user_id]
    conn = get_db()
    conn.execute(_sql("users", "update").replace("{set_clause}", sets), vals)
    conn.commit()
    conn.close()


def delete_user(user_id: int) -> None:
    conn = get_db()
    conn.execute(_sql("users", "delete"), (user_id,))
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
    conn.execute(_sql("login_attempts", "prune"))


def record_failed_login(username: str, ip: str) -> None:
    conn = get_db()
    _prune_old_attempts(conn)
    conn.execute(
        _sql("login_attempts", "insert"),
        (username, ip),
    )
    conn.commit()
    conn.close()


def clear_failed_logins(username: str) -> None:
    conn = get_db()
    conn.execute(_sql("login_attempts", "clear"), (username,))
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
            _sql("login_attempts", "count_in_window").replace("{column}", column),
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

def create_folder(name: str, owner_id: int, parent_id: Optional[int] = None) -> int:
    conn = get_db()
    cur = conn.execute(
        _sql("folders", "insert"),
        (name, owner_id, parent_id),
    )
    conn.commit()
    folder_id = cur.lastrowid
    assert folder_id is not None
    conn.close()
    return folder_id


def list_folders(owner_id: int) -> list[dict[str, Any]]:
    conn = get_db()
    rows = conn.execute(
        _sql("folders", "list_by_owner"), (owner_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_folder(folder_id: int) -> Optional[dict[str, Any]]:
    conn = get_db()
    row = conn.execute(
        _sql("folders", "get_by_id"), (folder_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row is not None else None


def rename_folder(folder_id: int, new_name: str) -> None:
    conn = get_db()
    conn.execute(_sql("folders", "rename"), (new_name, folder_id))
    conn.commit()
    conn.close()


def delete_folder(folder_id: int) -> None:
    """Delete a folder, reparenting its children to its grandparent.

    The folder's videos fall back to Root via the ``videos.folder_id`` FK
    (ON DELETE SET NULL). Its immediate subfolders are re-pointed at this
    folder's parent (or Root), so the rest of the hierarchy is preserved.
    """
    conn = get_db()
    row = conn.execute(
        _sql("folders", "get_parent"), (folder_id,)
    ).fetchone()
    grandparent = row["parent_id"] if row is not None else None
    if grandparent is None:
        conn.execute(
            _sql("folders", "reparent_to_root"), (folder_id,)
        )
    else:
        conn.execute(
            _sql("folders", "reparent"),
            (grandparent, folder_id),
        )
    conn.execute(_sql("folders", "delete"), (folder_id,))
    conn.commit()
    conn.close()


def list_root_folders() -> list[dict[str, Any]]:
    """Top-level folders (no parent), across all owners, for the home page."""
    conn = get_db()
    rows = conn.execute(
        _sql("folders", "list_root")
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_child_folders(folder_id: int) -> list[dict[str, Any]]:
    """Immediate subfolders of ``folder_id``, across all owners."""
    conn = get_db()
    rows = conn.execute(
        _sql("folders", "list_children"), (folder_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_folder_videos(folder_id: Optional[int]) -> list[dict[str, Any]]:
    """Ready videos inside ``folder_id``; ``None`` means Root (no folder)."""
    conn = get_db()
    if folder_id is None:
        rows = conn.execute(
            _sql("videos", "list_folder_root")
        ).fetchall()
    else:
        rows = conn.execute(
            _sql("videos", "list_folder"),
            (folder_id,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def folders_tree(owner_id: int) -> list[dict[str, Any]]:
    """The owner's folders as a nested list; each item has a ``children`` list."""
    conn = get_db()
    rows = conn.execute(
        _sql("folders", "list_by_owner"), (owner_id,)
    ).fetchall()
    conn.close()
    folders = [dict(r) for r in rows]
    # Initialize ``children`` for every folder up front: a child can sort before
    # its parent (rows are ordered by name), so the parent's list must exist
    # before we append to it.
    for f in folders:
        f["children"] = []
    by_id: dict[int, dict[str, Any]] = {f["id"]: f for f in folders}
    roots: list[dict[str, Any]] = []
    for f in folders:
        pid = f.get("parent_id")
        if pid is not None and pid in by_id:
            by_id[pid]["children"].append(f)
        else:
            roots.append(f)

    def sort(nodes: list[dict[str, Any]]) -> None:
        nodes.sort(key=lambda x: (x["name"] or "").lower())
        for n in nodes:
            sort(n["children"])

    sort(roots)
    return roots


def folders_with_depth(owner_id: int) -> list[dict[str, Any]]:
    """Flat, depth-annotated list of the owner's folders (parent before child)."""
    flat: list[dict[str, Any]] = []

    def walk(nodes: list[dict[str, Any]], depth: int) -> None:
        for f in nodes:
            item = {k: v for k, v in f.items() if k != "children"}
            item["depth"] = depth
            flat.append(item)
            walk(f["children"], depth + 1)

    walk(folders_tree(owner_id), 0)
    return flat


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
        _sql("videos", "insert"),
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
    row = conn.execute(
        _sql("videos", "get_by_id"),
        (video_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def list_all_videos() -> list[dict[str, Any]]:
    conn = get_db()
    rows = conn.execute(
        _sql("videos", "list_all")
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
        _sql("videos", "list_public")
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_uploading_videos() -> list[dict[str, Any]]:
    """Return videos whose Drive upload has not finished (``status='uploading'``)."""
    conn = get_db()
    rows = conn.execute(
        _sql("videos", "list_uploading")
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
        _sql("videos", "sum_cached_bytes")
    ).fetchone()
    conn.close()
    return int(row["total"]) if row else 0


def list_videos_by_owner(owner_id: int) -> list[dict[str, Any]]:
    conn = get_db()
    rows = conn.execute(
        _sql("videos", "list_by_owner"),
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
    conn.execute(_sql("videos", "update").replace("{set_clause}", sets), vals)
    conn.commit()
    conn.close()


def delete_video(video_id: int) -> None:
    conn = get_db()
    conn.execute(_sql("videos", "delete"), (video_id,))
    conn.commit()
    conn.close()


def touch_video(video_id: int) -> None:
    """Update last_accessed timestamp (for LRU eviction)."""
    conn = get_db()
    conn.execute(
        _sql("videos", "touch"),
        (video_id,),
    )
    conn.commit()
    conn.close()


def list_lru_cached() -> list[dict[str, Any]]:
    """Return cached videos (have local_filename) oldest-accessed first,
    for LRU eviction. Only videos that have a Drive copy are evictable."""
    conn = get_db()
    rows = conn.execute(
        _sql("videos", "list_lru_cached")
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    conn = get_db()
    row = conn.execute(
        _sql("settings", "get"), (key,)
    ).fetchone()
    conn.close()
    if row:
        return row["value"]
    return default


def set_setting(key: str, value: str) -> None:
    conn = get_db()
    conn.execute(
        _sql("settings", "upsert"),
        (key, value),
    )
    conn.commit()
    conn.close()


def get_all_settings() -> dict[str, str]:
    conn = get_db()
    rows = conn.execute(_sql("settings", "all")).fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------

def add_comment(video_id: int, author_id: int, body: str, parent_id: Optional[int] = None) -> int:
    conn = get_db()
    cur = conn.execute(
        _sql("comments", "insert"),
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
        _sql("comments", "list"),
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
    row = conn.execute(_sql("comments", "get_by_id"), (comment_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_comment(comment_id: int) -> None:
    """Delete a comment and all of its replies (application-layer cascade)."""
    conn = get_db()
    conn.execute(_sql("comments", "delete_replies"), (comment_id,))
    conn.execute(_sql("comments", "delete"), (comment_id,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Live rooms (§14.6)
# ---------------------------------------------------------------------------

def list_live_rooms() -> list[dict[str, Any]]:
    """All live rooms, in creation order (for the live list page)."""
    conn = get_db()
    rows = conn.execute(_sql("live_rooms", "list")).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_live_room(room_id: int) -> Optional[dict[str, Any]]:
    conn = get_db()
    row = conn.execute(
        _sql("live_rooms", "get_by_id"), (room_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_live_room_by_owner(owner_id: int) -> Optional[dict[str, Any]]:
    """A user's own live room (at most one), or None."""
    conn = get_db()
    row = conn.execute(_sql("live_rooms", "get_by_owner"), (owner_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def create_live_room(
    url: str,
    title: str,
    owner_id: Optional[int] = None,
    description: Optional[str] = None,
    cover_filename: Optional[str] = None,
) -> int:
    conn = get_db()
    cur = conn.execute(
        _sql("live_rooms", "insert"),
        (url, title, description, owner_id, cover_filename),
    )
    conn.commit()
    room_id = cur.lastrowid
    assert room_id is not None
    conn.close()
    return room_id


def update_live_room(
    room_id: int,
    url: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    cover_filename: Optional[str] = None,
) -> None:
    """Update a room's stream link, title, description and/or cover.

    ``None`` means "leave unchanged" (a cover is only sent when a new file was
    uploaded, otherwise the existing cover is kept).
    """
    conn = get_db()
    if url is not None:
        conn.execute(_sql("live_rooms", "update_url"), (url, room_id))
    if title is not None:
        conn.execute(_sql("live_rooms", "update_title"), (title, room_id))
    if description is not None:
        conn.execute(_sql("live_rooms", "update_description"), (description, room_id))
    if cover_filename is not None:
        conn.execute(
            _sql("live_rooms", "update_cover"),
            (cover_filename, room_id),
        )
    conn.commit()
    conn.close()


def delete_live_room(room_id: int) -> None:
    conn = get_db()
    conn.execute(_sql("live_rooms", "delete"), (room_id,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Tags (§bug L67)
# ---------------------------------------------------------------------------

def create_tag(name: str) -> int:
    """Insert a tag (or return the existing id if ``name`` is already in use)."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Tag name is required.")
    conn = get_db()
    row = conn.execute(_sql("tags", "get_by_name"), (name,)).fetchone()
    if row is not None:
        conn.close()
        return int(row["id"])
    cur = conn.execute(_sql("tags", "insert"), (name,))
    conn.commit()
    tag_id = cur.lastrowid
    assert tag_id is not None
    conn.close()
    return int(tag_id)


def list_tags() -> list[dict[str, Any]]:
    """All tags (id + name), case-insensitive alphabetical."""
    conn = get_db()
    rows = conn.execute(_sql("tags", "list")).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_tag(tag_id: int) -> dict[str, Any] | None:
    """One tag by id, or ``None`` if it does not exist."""
    conn = get_db()
    row = conn.execute(_sql("tags", "get_by_id"), (tag_id,)).fetchone()
    conn.close()
    return dict(row) if row is not None else None


def delete_tag(tag_id: int) -> None:
    """Delete a tag; its ``video_tags`` links cascade away (ON DELETE CASCADE)."""
    conn = get_db()
    conn.execute(_sql("tags", "delete"), (tag_id,))
    conn.commit()
    conn.close()


def get_video_tags(video_id: int) -> list[dict[str, Any]]:
    """The tags on one video, as ``{"id", "name"}`` dicts (alphabetical)."""
    conn = get_db()
    rows = conn.execute(_sql("tags", "video_tags"), (video_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def bulk_video_tags(video_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """Map each id in ``video_ids`` to its list of ``{"id","name"}`` tags.

    One query; ids with no tags map to an empty list. Used to annotate the home
    gallery so the search box can match on tags (§bug L67).
    """
    if not video_ids:
        return {}
    qmarks = ",".join("?" * len(video_ids))
    conn = get_db()
    rows = conn.execute(
        _sql("tags", "video_tags_bulk").replace("{q}", qmarks),
        tuple(video_ids),
    ).fetchall()
    conn.close()
    out: dict[int, list[dict[str, Any]]] = {vid: [] for vid in video_ids}
    for r in rows:
        out.setdefault(int(r["video_id"]), []).append(
            {"id": int(r["tag_id"]), "name": r["name"]},
        )
    return out


def video_tag_ids(video_id: int) -> list[int]:
    """The tag ids on a video (for pre-selecting the editor's tag checkboxes)."""
    conn = get_db()
    rows = conn.execute(_sql("video_tags", "ids"), (video_id,)).fetchall()
    conn.close()
    return [int(r[0]) for r in rows]


def set_video_tags(video_id: int, tag_ids: list[int]) -> None:
    """Replace a video's tags with exactly ``tag_ids`` (clear then insert)."""
    conn = get_db()
    conn.execute(_sql("video_tags", "clear"), (video_id,))
    for tid in tag_ids:
        conn.execute(_sql("video_tags", "insert"), (video_id, int(tid)))
    conn.commit()
    conn.close()

