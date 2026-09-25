INSERT INTO videos
(title, owner_id, folder_id, local_filename, cover_filename,
 size_bytes, description, duration, resolution, codec, bitrate, fps,
 google_drive_file_id, drive_path, drive_filename, status)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
