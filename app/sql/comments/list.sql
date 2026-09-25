SELECT c.id, c.parent_id, c.body, c.created_at, c.author_id,
       u.username, u.avatar_filename
FROM comments c
JOIN users u ON c.author_id = u.id
WHERE c.video_id = ?
ORDER BY c.id
