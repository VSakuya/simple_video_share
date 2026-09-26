-- §bug L67: tag library + video<->tag junction. New tables, so this is a
-- single idempotent DDL script (applied via executescript, no column checks).
CREATE TABLE IF NOT EXISTS tags (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT    NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS video_tags (
    video_id  INTEGER NOT NULL,
    tag_id    INTEGER NOT NULL,
    PRIMARY KEY (video_id, tag_id),
    FOREIGN KEY (video_id) REFERENCES videos (id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id)   REFERENCES tags   (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_video_tags_tag ON video_tags (tag_id);