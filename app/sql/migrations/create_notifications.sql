-- Message notifications (§21): one row per notification event. ``type`` is the
-- extensibility axis ('video_comment' | 'comment_reply' today; new kinds add a
-- type string + optional ``data`` JSON keys without a schema change). Actor
-- name/avatar and the video title are denormalized snapshots at creation time
-- (same pattern as live_messages) so a deleted actor or renamed video still
-- renders. Deleting the recipient or the video cascades the row away; deleting
-- the actor keeps the row (snapshot stays renderable).

CREATE TABLE IF NOT EXISTS notifications (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id               INTEGER NOT NULL,
    type                  TEXT    NOT NULL,
    actor_id              INTEGER NOT NULL,
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
CREATE INDEX IF NOT EXISTS idx_notifications_user   ON notifications (user_id, id);
CREATE INDEX IF NOT EXISTS idx_notifications_unread ON notifications (user_id) WHERE read_at IS NULL;