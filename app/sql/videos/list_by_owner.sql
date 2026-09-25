SELECT v.*, f.name AS folder_name
FROM videos v
LEFT JOIN folders f ON v.folder_id = f.id
WHERE v.owner_id = ?
ORDER BY v.uploaded_at DESC
