DELETE FROM login_attempts WHERE attempted_at < datetime('now', '-1 day')
