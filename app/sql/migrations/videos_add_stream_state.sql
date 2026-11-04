-- §31.2 + §32.2: HLS stream bookkeeping columns on videos.
-- Four plain ALTERs; SQLite appends them and existing rows take the default,
-- so an installed database needs no data migration. Run through executescript
-- (one file, four statements) from db._migrate().
ALTER TABLE videos ADD COLUMN stream_dir TEXT;
ALTER TABLE videos ADD COLUMN stream_state TEXT NOT NULL DEFAULT 'none';
ALTER TABLE videos ADD COLUMN stream_source TEXT NOT NULL DEFAULT '';
ALTER TABLE videos ADD COLUMN stream_updated_at TEXT;
