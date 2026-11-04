-- §37.3: how much a user owns. Read before DELETE (the cascade erases the child
-- rows) so the admin page can warn about it and the success message can report it.
-- One placeholder per subquery: SQLite has no named parameters here, so the route
-- passes the same user_id four times.
SELECT
    (SELECT COUNT(*) FROM videos v     WHERE v.owner_id  = ?) AS video_count,
    (SELECT COUNT(*) FROM comments c   WHERE c.author_id = ?) AS comment_count,
    (SELECT COUNT(*) FROM live_rooms r WHERE r.owner_id  = ?) AS room_count,
    (SELECT COUNT(*) FROM folders f    WHERE f.owner_id  = ?) AS folder_count
