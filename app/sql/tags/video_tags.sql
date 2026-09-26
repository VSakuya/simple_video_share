SELECT t.id AS tag_id, t.name
  FROM video_tags vt
  JOIN tags t ON t.id = vt.tag_id
 WHERE vt.video_id = ?
 ORDER BY t.name COLLATE NOCASE