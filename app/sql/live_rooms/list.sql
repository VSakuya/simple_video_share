SELECT lr.id, lr.url, lr.title, lr.description, lr.owner_id,
       lr.cover_filename, lr.created_at,
       u.username AS owner_username, u.avatar_filename AS owner_avatar
FROM live_rooms lr
LEFT JOIN users u ON u.id = lr.owner_id
ORDER BY lr.id
