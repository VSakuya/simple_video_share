-- 37.1: notifications.actor_id was NOT NULL while its FK action is ON DELETE SET
-- NULL. SQLite cannot set a NOT NULL column to NULL, so deleting ANY user who ever
-- triggered a notification (i.e. anyone who commented on or replied to someone
-- else's video) aborted with
--     NOT NULL constraint failed: notifications.actor_id
-- and the admin Delete button turned into a bare 500 / "Something went wrong".
-- The table is rebuilt with a nullable actor_id: the row keeps its denormalized
-- actor_username / actor_avatar_filename snapshot, so it still renders after the
-- actor is gone (same pattern as live_messages).
--
-- db._migrate runs this only when PRAGMA table_info still reports actor_id as
-- NOT NULL, with PRAGMA foreign_keys OFF, then verifies with foreign_key_check.
CREATE TABLE notifications__new (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id               INTEGER NOT NULL,
    type                  TEXT    NOT NULL,
    actor_id              INTEGER,
    actor_username        TEXT,
    actor_avatar_filename TEXT,
    video_id              INTEGER,
    video_title           TEXT,
    comment_id            INTEGER,
    data                  TEXT,
    read_at               TEXT,
    created_at            TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id)  REFERENCES users  (id) ON DELETE CASCADE,
    FOREIGN KEY (actor_id) REFERENCES users  (id) ON DELETE SET NULL,
    FOREIGN KEY (video_id) REFERENCES videos (id) ON DELETE CASCADE
);
INSERT INTO notifications__new (
    id, user_id, type, actor_id, actor_username, actor_avatar_filename,
    video_id, video_title, comment_id, data, read_at, created_at
)
SELECT
    id, user_id, type, actor_id, actor_username, actor_avatar_filename,
    video_id, video_title, comment_id, data, read_at, created_at
FROM notifications;
DROP TABLE notifications;
ALTER TABLE notifications__new RENAME TO notifications;
-- Dropping the table dropped its indexes too, so recreate both (the partial one
-- is what makes the unread badge cheap).
CREATE INDEX IF NOT EXISTS idx_notifications_user   ON notifications (user_id, id);
CREATE INDEX IF NOT EXISTS idx_notifications_unread ON notifications (user_id) WHERE read_at IS NULL;
