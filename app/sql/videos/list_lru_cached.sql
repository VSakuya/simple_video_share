SELECT * FROM videos
WHERE local_filename IS NOT NULL
  AND google_drive_file_id IS NOT NULL
ORDER BY COALESCE(last_accessed, uploaded_at) ASC
