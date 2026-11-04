"""Durable journal for publishing repaired MP4s to the existing Drive object."""
import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any

from . import db, drive
from .filelock import replace_file


def journal_path(base: Path, video_id: int) -> Path:
    return base / '.hls-jobs' / f'{video_id}.repair.json'


def pending(base: Path, video_id: int) -> bool:
    return journal_path(base, video_id).is_file()


def mirrored(base: Path, video: dict) -> bool:
    try:
        receipt = json.loads((base / '.hls-jobs' / f"{video['id']}.mirrored.json").read_text(encoding='utf-8'))
        return receipt.get('drive_id') == video.get('google_drive_file_id')
    except (OSError, ValueError, TypeError):
        return False


def needs_sync(base: Path, video: dict) -> bool:
    if pending(base, int(video['id'])):
        return True
    from . import hls
    if video.get('stream_source') != hls.MODE_REPAIR or mirrored(base, video):
        return False
    tree = hls.stream_dir_for(base, video)
    return tree is not None and hls.validate_stream(tree, video.get('duration'))[0]


def _receipt(base: Path, video_id: int, job: dict) -> None:
    path = base / '.hls-jobs' / f'{video_id}.mirrored.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8') as fh:
        json.dump({'drive_id': job['drive_id'], 'md5': job['md5'], 'size': job['size']}, fh)
        fh.flush()
        os.fsync(fh.fileno())
    replace_file(temp, path)


def prepare_legacy(base: Path, video: dict, app_config: Any) -> None:
    """Rewrap an existing repaired HLS tree without re-encoding it."""
    import uuid
    from . import hls, storage
    tree = hls.stream_dir_for(base, video)
    if tree is None or not hls.validate_stream(tree, video.get('duration'))[0]:
        raise RuntimeError('Existing repaired segments are unavailable')
    cfg = hls.settings(app_config)
    ffmpeg = hls.ffmpeg_binary(cfg)
    if not ffmpeg:
        raise RuntimeError('ffmpeg is required to rewrap repaired segments')
    size = hls.stream_bytes_length(tree)
    if not storage.ensure_space(str(base.parent), size, app_config, peak_factor=2.1, protect=[video['id']]):
        raise RuntimeError('Not enough space to prepare repaired MP4 for Drive')
    root = tree.name.split('.gen-')[0]
    published = tree.parent / (root + '.gen-' + uuid.uuid4().hex)
    temp = published.with_name(published.name + hls.TMP_SUFFIX)
    source = hls.source_path_for(base, video) or tree.parent / f"video-{video['id']}.mp4"
    temp.mkdir(parents=True)
    try:
        db.update_video(video['id'], stream_state=hls.STATE_REPAIRING)
        repaired = temp / hls.REPAIRED_NAME
        hls._run_ffmpeg([ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
                         '-i', str((tree / hls.PLAYLIST_NAME).resolve()), '-c', 'copy',
                         '-movflags', '+faststart', str(repaired.resolve())],
                        cfg['timeout_seconds'], 'rewrap existing repair', temp)
        hls._run_ffmpeg(hls._copy_argv(ffmpeg, repaired, temp / hls.PLAYLIST_NAME, cfg),
                        cfg['timeout_seconds'], 'segment existing repair', temp)
        if not hls.validate_stream(temp, video.get('duration'))[0]:
            raise RuntimeError('Rewrapped repair stream failed validation')
        prepare(base, video, source, temp, published)
    except Exception:
        hls._rmtree_quiet(temp)
        raise


def _save(base: Path, video_id: int, job: dict) -> None:
    path = journal_path(base, video_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8') as fh:
        json.dump(job, fh)
        fh.flush()
        os.fsync(fh.fileno())
    replace_file(temp, path)


def _path(base: Path, relative: str) -> Path:
    result = (base / relative).resolve()
    if not result.is_relative_to(base.resolve()):
        raise RuntimeError('Invalid repair journal path')
    return result


def prepare(base: Path, video: dict, source: Path, temp: Path, published: Path) -> None:
    from . import hls
    repaired = temp / hls.REPAIRED_NAME
    with repaired.open('rb') as fh:
        checksum = hashlib.file_digest(fh, 'md5').hexdigest()
    _save(base, int(video['id']), {
        'drive_id': video['google_drive_file_id'], 'source': source.relative_to(base).as_posix(),
        'temp': temp.relative_to(base).as_posix(), 'published': published.relative_to(base).as_posix(),
        'size': repaired.stat().st_size, 'md5': checksum, 'session': None,
    })


def finish(base: Path, video_id: int, app_config: Any) -> str:
    from . import hls
    job = json.loads(journal_path(base, video_id).read_text(encoding='utf-8'))
    video = db.get_video_by_id(video_id)
    if not video or video.get('google_drive_file_id') != job['drive_id']:
        raise RuntimeError('Video or Drive identity changed; repair sync stopped')
    source = _path(base, job['source'])
    temp = _path(base, job['temp'])
    published = _path(base, job['published'])
    tree = temp if temp.is_dir() else published
    ok, reason = hls.validate_stream(tree, video.get('duration'))
    if not ok:
        raise RuntimeError('Pending repair stream invalid: ' + reason)
    repaired = tree / hls.REPAIRED_NAME
    payload = repaired if repaired.is_file() else source
    with payload.open('rb') as fh:
        digest = hashlib.file_digest(fh, 'md5').hexdigest()
    if payload.stat().st_size != job['size'] or digest != job['md5']:
        raise RuntimeError('Pending repaired MP4 failed integrity validation')
    db.update_video(video_id, stream_state=hls.STATE_REPAIRING)
    def report(uploaded, total):
        hls._write_job(base, video_id, running=True, stage='syncing_drive',
                       percent=95 + min(4, int(4 * uploaded / max(1, total))),
                       uploaded_bytes=uploaded, total_bytes=total)
    def session(uri):
        job['session'] = uri
        _save(base, video_id, job)
    report(0, job['size'])
    drive.replace_video_file(str(payload), str(job['drive_id']), progress_cb=report,
                             resumable_uri=job.get('session'), on_session=session)
    # Drive is now verified. Only now replace the local source and publish the tree.
    if repaired.is_file():
        replace_file(repaired, source)
    if temp.is_dir():
        temp.rename(published)
    metadata = dict(size_bytes=job['size'], codec='h264', local_filename=job['source'],
                    stream_dir=job['published'], stream_state=hls.STATE_READY,
                    stream_source=hls.MODE_REPAIR, stream_updated_at=hls._now_text())
    duration = float(video.get('duration') or 0)
    if duration > 0:
        metadata['bitrate'] = round(job['size'] * 8 / duration / 1000)
    db.update_video(video_id, **metadata)
    _receipt(base, video_id, job)
    journal_path(base, video_id).unlink()
    if not hls.settings(app_config)['keep_source']:
        hls._drop_local_source(base, db.get_video_by_id(video_id) or video,
                               hls.settings(app_config), 'repaired Drive mirror verified')
    return hls.STATE_READY


def resume_pending(base: Path, app_config: Any) -> None:
    """Resume sequentially so startup never fans out CPU/Drive repair jobs."""
    jobs = list((base / '.hls-jobs').glob('*.repair.json'))
    if not jobs:
        return
    def run():
        from . import hls
        for path in jobs:
            try:
                video_id = int(path.name.split('.')[0])
                video = db.get_video_by_id(video_id)
                if video:
                    hls.ensure_stream(base, video, app_config, mode=hls.MODE_REPAIR, reason='resume Drive repair')
            except Exception:
                hls.logger.exception('Could not resume pending repair %s', path.name)
    threading.Thread(target=run, name='repair-sync-resume', daemon=True).start()
