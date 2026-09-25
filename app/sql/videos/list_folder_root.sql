SELECT v.*, u.username AS owner_name,
       u.avatar_filename AS owner_avatar_filename
FROM videos v JOIN users u ON v.owner_id = u.id
WHERE v.status = 'ready' AND v.folder_id IS NULL
ORDER BY v.uploaded_at DESC
