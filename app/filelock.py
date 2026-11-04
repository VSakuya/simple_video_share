from contextlib import contextmanager
import os
from pathlib import Path


@contextmanager
def file_lock(path: Path):
    """Nonblocking OS lock; released even when a worker is killed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as fh:
        fh.seek(0)
        if os.fstat(fh.fileno()).st_size == 0:
            fh.write(b'0')
            fh.flush()
        fh.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            raise LockBusy(str(path)) from exc
        try:
            yield
        finally:
            fh.seek(0)
            if os.name == 'nt':
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fh, fcntl.LOCK_UN)


class LockBusy(RuntimeError):
    pass


def replace_file(source: Path, destination: Path) -> None:
    """Windows readers can briefly prevent an atomic rename."""
    import time
    for attempt in range(20):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.01)
