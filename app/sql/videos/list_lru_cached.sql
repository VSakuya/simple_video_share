-- Cached, evictable videos in LRU order (least-recently-accessed first).
-- A video is evictable only when it has BOTH a local copy (local_filename) and
-- a Drive copy to fall back to (google_drive_file_id). Pinned videos are
-- excluded (L58) so they are never evicted ahead of ordinary ones, even when
-- the cache is full.
SELECT * FROM videos
WHERE local_filename IS NOT NULL
  AND google_drive_file_id IS NOT NULL
  AND is_pinned = 0
ORDER BY COALESCE(last_accessed, uploaded_at) ASC
