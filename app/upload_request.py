"""Spool multipart files to the application's disk, not a quota-limited /tmp."""
from pathlib import Path
from tempfile import SpooledTemporaryFile
from flask import Request, current_app


class UploadRequest(Request):
    def _get_file_stream(self, total_content_length, content_type, filename=None, content_length=None):
        directory = Path(current_app.config['UPLOAD_TEMP_DIR'])
        directory.mkdir(parents=True, exist_ok=True)
        return SpooledTemporaryFile(max_size=512 * 1024, mode='rb+', dir=directory)
