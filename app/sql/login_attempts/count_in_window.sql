SELECT MIN(attempted_at) AS oldest, COUNT(*) AS n FROM login_attempts WHERE {column} = ? AND attempted_at > datetime('now', ?)
