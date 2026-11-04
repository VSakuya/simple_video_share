SELECT local_filename, stream_dir FROM videos
WHERE local_filename IS NOT NULL OR stream_dir IS NOT NULL
