SELECT COALESCE(SUM(size_bytes), 0) AS total FROM videos WHERE local_filename IS NOT NULL
