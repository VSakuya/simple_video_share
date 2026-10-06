SELECT id, user_id, type, actor_id, actor_username, actor_avatar_filename,
       video_id, video_title, comment_id, data, read_at, created_at
FROM notifications
WHERE user_id = ?
ORDER BY id DESC
LIMIT {limit}