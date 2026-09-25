SELECT v.*, u.username AS owner_name, u.avatar_filename AS owner_avatar_filename,
       f.name AS folder_name
FROM videos v
JOIN users u   ON v.owner_id = u.id
LEFT JOIN folders f ON v.folder_id = f.id
WHERE v.status = 'ready'
ORDER BY v.uploaded_at DESC
