-- 37.2: live_rooms.owner_id was added by ALTER TABLE ... REFERENCES users(id) with
-- no ON DELETE action, and SQLite defaults that to NO ACTION: a user who owns a
-- live room could never be deleted ("FOREIGN KEY constraint failed").
-- Rooms belong to their owner (14.8), so cascading is the intent; live_messages
-- already cascades away with its room.
--
-- Same table-rebuild pattern as live_rooms_drop_code.sql. db._migrate runs this
-- only when PRAGMA foreign_key_list still reports NO ACTION for owner_id, with
-- PRAGMA foreign_keys OFF, and verifies the swap with foreign_key_check.
CREATE TABLE live_rooms__new (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT,
    title           TEXT    NOT NULL,
    description     TEXT,
    owner_id        INTEGER REFERENCES users(id) ON DELETE CASCADE,
    cover_filename  TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
INSERT INTO live_rooms__new (id, url, title, description, owner_id, cover_filename, created_at)
SELECT id, url, title, description, owner_id, cover_filename, created_at FROM live_rooms;
DROP TABLE live_rooms;
ALTER TABLE live_rooms__new RENAME TO live_rooms;
