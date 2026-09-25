SELECT v.*, u.username AS owner_name, f.name AS folder_name
FROM videos v
JOIN users u   ON v.owner_id = u.id
LEFT JOIN folders f ON v.folder_id = f.id
ORDER BY v.uploaded_at DESC
