"""Drive replacement uses update, preserves identity and verifies committed media."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from app import drive


class DriveReplacementTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'fixed.mp4'
        self.path.write_bytes(b'fixed video payload')
        self.metadata = {'id': 'original-id', 'fileSize': str(self.path.stat().st_size),
                         'md5Checksum': hashlib.md5(self.path.read_bytes()).hexdigest()}
        self.service = MagicMock()
        self.http = MagicMock()
        self.service.files.return_value.get.return_value.execute.return_value = {'id': 'original-id', 'fileSize': '1', 'md5Checksum': 'old'}
        self.service.files.return_value.update.return_value.next_chunk.return_value = (None, self.metadata)
        ctx = patch.object(drive, '_service_and_http', return_value=(self.service, self.http))
        ctx.start()
        self.addCleanup(ctx.stop)

    def test_updates_same_id_preserving_metadata(self):
        drive.replace_video_file(str(self.path), 'original-id')
        args = self.service.files.return_value.update.call_args.kwargs
        self.assertEqual(args['fileId'], 'original-id')
        self.assertEqual(args['body'], {})
        self.assertTrue(args['newRevision'])
        self.service.files.return_value.insert.assert_not_called()
        self.service.files.return_value.delete.assert_not_called()

    def test_completed_response_lost_does_not_upload_again(self):
        self.service.files.return_value.get.return_value.execute.return_value = self.metadata
        drive.replace_video_file(str(self.path), 'original-id')
        self.service.files.return_value.update.assert_not_called()

    def test_checksum_mismatch_is_failure(self):
        self.service.files.return_value.update.return_value.next_chunk.return_value = (None, dict(self.metadata, md5Checksum='wrong'))
        with self.assertRaisesRegex(RuntimeError, 'verification failed'):
            drive.replace_video_file(str(self.path), 'original-id')
        self.assertTrue(self.path.is_file())
        self.service.files.return_value.delete.assert_not_called()

    def test_expired_resume_session_restarts_update_not_insert(self):
        import httplib2
        from googleapiclient.errors import HttpError
        request = self.service.files.return_value.update.return_value
        request.next_chunk.side_effect = [HttpError(httplib2.Response({'status': '410'}), b'expired'), (None, self.metadata)]
        on_session = MagicMock()
        drive.replace_video_file(str(self.path), 'original-id', resumable_uri='expired-uri', on_session=on_session)
        self.assertEqual(self.service.files.return_value.update.call_count, 2)
        on_session.assert_called_with(None)
        self.service.files.return_value.insert.assert_not_called()


if __name__ == '__main__':
    unittest.main()
