SELECT vt.video_id, t.id AS tag_id, t.name
  FROM video_tags vt
  JOIN tags t ON t.id = vt.tag_id
 WHERE vt.video_id IN ({q})
 ORDER BY vt.video_id, t.name COLLATE NOCASE