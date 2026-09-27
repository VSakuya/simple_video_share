-- §16.8: live-room chat persistence. New table, so this is a single idempotent
-- DDL script (applied via executescript, no column checks).
CREATE TABLE IF NOT EXISTS live_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id         INTEGER NOT NULL,
    user_id         INTEGER,
    username        TEXT,
    avatar_filename TEXT,
    body            TEXT    NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (room_id) REFERENCES live_rooms (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_live_messages_room ON live_messages (room_id, id);