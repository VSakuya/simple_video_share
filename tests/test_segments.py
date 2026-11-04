"""Isolated regression tests for interrupted uploads and HLS publication.
Run: python -m unittest discover -s tests -p test_segments.py -v
No live database, Drive account or existing video is used.
"""
import hashlib
import io
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask
from app import db, hls, storage
from app.filelock import file_lock
from app.routes import upload, admin, watch


class SegmentsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / 'videos'
        self.base.mkdir()
        self.patches = [patch.object(db, 'DB_DIR', self.root / 'data'),
                        patch.object(db, 'DB_PATH', self.root / 'data/app.db')]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        db.init_db()
        self.uid = db.create_user('regression', 'unused', is_admin=1)
        self.app = Flask('regression', template_folder=str(Path('app/templates').resolve()))
        self.app.secret_key = 'test'
        self.app.config.update(TESTING=True, VIDEOS_DIR=self.base, COVERS_DIR=self.root / 'covers', HLS={'enabled': True, 'keep_source': True, 'segment_seconds': 2})
        self.app.register_blueprint(upload.upload_bp)
        self.app.register_blueprint(admin.admin_bp)
        self.app.register_blueprint(watch.watch_bp)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = self.uid
        for item in [patch.object(upload, 'SEGMENT_SIZE', 4),
                     patch.object(upload, '_check_space', return_value=(True, '')),
                     patch.object(upload.drive_worker.worker, 'enqueue'),
                     patch.object(storage, 'enforce_cache_limit')]:
            item.start()
            self.addCleanup(item.stop)
        self.parts = [b'abcd', b'ef']
        self.hashes = [hashlib.sha256(p).hexdigest() for p in self.parts]
        self.fp = hashlib.sha256(''.join(self.hashes).encode()).hexdigest()

    def start(self, **overrides):
        data = dict(title='video', expected_size=6, total_parts=2, fingerprint=self.fp)
        data.update(overrides)
        return self.client.post('/upload/seg/start', data=data)

    def part(self, token, index, payload=None, digest=None):
        return self.client.post('/upload/seg/part', data={'token': token, 'part_index': index,
            'sha256': digest or self.hashes[index], 'part': (io.BytesIO(self.parts[index] if payload is None else payload), 'part.bin')})

    def test_partial_part_not_resumed(self):
        token = self.start().json['token']
        self.assertEqual(self.part(token, 0, b'a').status_code, 400)
        folder = upload._seg_dir(self.base, token)
        (folder / 'part_00000').write_bytes(b'a')
        self.assertEqual(self.start(resume_token=token).json['uploaded_parts'], [])
        self.assertEqual(self.client.post('/upload/seg/finish', data={'token': token}).status_code, 400)

    def test_checksum_and_corruption(self):
        token = self.start().json['token']
        self.assertEqual(self.part(token, 0, b'zzzz').status_code, 400)
        self.assertEqual(self.part(token, 0).status_code, 200)
        (upload._seg_dir(self.base, token) / 'part_00000').write_bytes(b'zzzz')
        self.assertEqual(self.start(resume_token=token).json['uploaded_parts'], [])

    def test_content_identity(self):
        token = self.start().json['token']
        self.assertNotEqual(self.start(resume_token=token, fingerprint='a' * 64).json['token'], token)

    def test_finish_idempotent(self):
        token = self.start().json['token']
        for i in range(2):
            self.assertEqual(self.part(token, i).status_code, 200)
        first = self.client.post('/upload/seg/finish', data={'token': token})
        self.assertEqual(first.status_code, 200, first.json)
        second = self.client.post('/upload/seg/finish', data={'token': token})
        self.assertEqual(first.json['video_id'], second.json['video_id'])
        self.assertEqual(len(db.list_all_videos()), 1)
        video = db.get_video_by_id(first.json['video_id'])
        self.assertEqual((self.base / video['local_filename']).read_bytes(), b'abcdef')
        self.assertFalse(list(upload._seg_dir(self.base, token).glob('part_*')))

    def test_lock_and_cancel(self):
        token = self.start().json['token']
        with file_lock(self.base / '.segment-locks' / (token + '.lock')):
            self.assertEqual(self.part(token, 0).status_code, 409)
        self.assertEqual(self.client.post('/upload/seg/cancel', data={'token': token}).status_code, 200)
        self.assertFalse(upload._seg_dir(self.base, token).exists())

    def test_expiry(self):
        import os
        token = self.start().json['token']
        path = upload._seg_meta_path(self.base, token)
        os.utime(path, (time.time() - 8*86400,) * 2)
        upload.cleanup_segments(self.base)
        self.assertFalse(path.exists())

    def test_admin_page_and_login(self):
        import app as app_module
        import logging
        import re
        from werkzeug.security import generate_password_hash
        from app import drive_worker
        db.update_user(self.uid, password_hash=generate_password_hash('regression-password'))
        with patch.object(app_module, '_STORAGE_DIR', self.root), patch.object(drive_worker.worker, 'resume_incomplete'), patch('app.log.setup_logging', return_value=logging.getLogger('regression')):
            application = app_module.create_app()
        application.config['TESTING'] = True
        client = application.test_client()
        login = client.post('/auth/login', data={'username': 'regression', 'password': 'regression-password'})
        self.assertEqual(login.status_code, 302)
        # A video row exercises the progress controls, not just the empty table.
        db.create_video('page fixture', self.uid, None, None, None, 0)
        response = client.get('/admin/videos')
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-repair-progress', response.text)
        self.assertIn('pollRepairs', response.text)
        script = '\n'.join(re.findall(r'<script>([\s\S]*?)</script>', response.text))
        if shutil.which('node'):
            subprocess.run(['node', '--check'], input=script, text=True, check=True, capture_output=True)
        self.assertEqual(client.get('/admin/videos/stream/progress').status_code, 200)

    def make_video(self):
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            self.skipTest('ffmpeg unavailable')
        path = self.base / 'fixture' / 'source.mp4'
        path.parent.mkdir()
        subprocess.run([ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc=duration=4:size=160x120:rate=24',
                        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-g', '48', '-threads', '1', str(path)], check=True, capture_output=True)
        vid = db.create_video('fixture', self.uid, None, 'fixture/source.mp4', None, path.stat().st_size, duration=4, fps=24)
        return vid

    def test_real_ffmpeg_progress_and_versioned_playback(self):
        vid = self.make_video()
        with patch.object(storage, 'ensure_space', return_value=True):
            state = hls.ensure_stream(self.base, db.get_video_by_id(vid), self.app.config)
        self.assertEqual(state, hls.STATE_READY)
        old = db.get_video_by_id(vid)['stream_dir']
        response = self.client.get(f'/watch/{vid}/hls/playlist.m3u8')
        self.assertEqual(response.status_code, 200)
        self.assertIn('?v=', response.text)
        asset = next(line for line in response.text.splitlines() if line and not line.startswith('#'))
        snapshots = []
        with patch.object(storage, 'ensure_space', return_value=True):
            thread = threading.Thread(target=hls.ensure_stream, args=(self.base, db.get_video_by_id(vid), self.app.config), kwargs={'mode': 'repair'})
            thread.start()
            while thread.is_alive():
                snapshots.append(hls.progress(self.base, vid))
                time.sleep(.01)
            thread.join()
        self.assertTrue(any(j.get('stage') in ('encoding', 'segmenting') for j in snapshots))
        self.assertEqual(hls.progress(self.base, vid)['percent'], 100)
        self.assertNotEqual(db.get_video_by_id(vid)['stream_dir'], old)
        self.assertTrue((self.base / old).exists())
        with self.client.get(f'/watch/{vid}/hls/{asset}') as response:
            self.assertEqual(response.status_code, 200)
        progress = self.client.get('/admin/videos/stream/progress')
        self.assertEqual(progress.status_code, 200)
        self.assertEqual(progress.json['jobs'][0]['percent'], 100)

    def test_failed_rebuild_preserves_tree_and_restart_recovers(self):
        vid = self.make_video()
        with patch.object(storage, 'ensure_space', return_value=True):
            hls.ensure_stream(self.base, db.get_video_by_id(vid), self.app.config)
        before = db.get_video_by_id(vid)
        with patch.object(storage, 'ensure_space', return_value=True), patch.object(hls, '_run_ffmpeg', side_effect=OSError('disk failure')):
            result = hls.ensure_stream(self.base, before, self.app.config, mode='repair')
        self.assertEqual(result, hls.STATE_READY)
        self.assertEqual(db.get_video_by_id(vid)['stream_dir'], before['stream_dir'])
        self.assertEqual(hls.progress(self.base, vid)['stage'], 'failed')
        db.update_video(vid, stream_state='repairing')
        self.assertEqual(self.client.get(f'/watch/{vid}/hls/playlist.m3u8').status_code, 200)
        hls.recover_jobs(self.base)
        self.assertEqual(db.get_video_by_id(vid)['stream_state'], 'ready')
        self.assertEqual(hls.progress(self.base, vid)['stage'], 'interrupted')


if __name__ == '__main__':
    unittest.main()
