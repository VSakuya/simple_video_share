CREATE TABLE live_rooms__new (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT,
    title           TEXT    NOT NULL,
    cover_filename  TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
INSERT INTO live_rooms__new (id, url, title, cover_filename, created_at)
    SELECT id, url, title, cover_filename, created_at FROM live_rooms;
DROP TABLE live_rooms;
ALTER TABLE live_rooms__new RENAME TO live_rooms;
