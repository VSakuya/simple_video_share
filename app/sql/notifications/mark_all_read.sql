UPDATE notifications
SET read_at = datetime('now')
WHERE user_id = ? AND read_at IS NULL