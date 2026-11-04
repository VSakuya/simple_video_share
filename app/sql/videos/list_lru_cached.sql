-- Cached, evictable videos in LRU order (least-recently-accessed first).
-- A video is evictable when it has a local cache entry AND a Drive copy to fall
-- back to (google_drive_file_id). Since §31.1 an entry is the MP4 copy OR the
-- HLS segment tree (hls_keep_source clears local_filename once segments validated),
-- so both columns count. Pinned videos are excluded (L58) so they are never
-- evicted ahead of ordinary ones, even when the cache is full.
SELECT * FROM videos
WHERE (local_filename IS NOT NULL OR stream_dir IS NOT NULL)
  AND google_drive_file_id IS NOT NULL
  AND is_pinned = 0
ORDER BY COALESCE(last_accessed, uploaded_at) ASC
