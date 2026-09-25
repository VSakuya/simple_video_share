ALTER TABLE live_rooms ADD COLUMN owner_id INTEGER REFERENCES users(id)
