SELECT video_id, COUNT(*) AS n
FROM comments
WHERE video_id IN ($ids)
GROUP BY video_id