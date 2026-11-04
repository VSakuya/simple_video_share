SELECT u.*,
       (SELECT COUNT(*) FROM videos v    WHERE v.owner_id  = u.id) AS video_count,
       (SELECT COUNT(*) FROM comments c  WHERE c.author_id = u.id) AS comment_count,
       (SELECT COUNT(*) FROM live_rooms r WHERE r.owner_id = u.id) AS room_count
FROM users u
ORDER BY u.id
