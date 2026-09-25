"""Google Drive integration (upload / download), following the ``test.py`` auth
pattern.

Auth is **lazy**: the Drive client is built on first use, not at import time,
so the app can start (and be tested) without a browser OAuth flow.

NOTE: ``credentials/`` is managed by the app at runtime and is read-only from
source code's perspective — never edit those files by hand.
"""

import hashlib
import logging
import os
import time
import uuid
from http.client import HTTPException as _HttpClientException
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

logger = logging.getLogger("simple_video_share.drive")

# Anchor to the project root (credentials/ sits at the repo top level).
_ROOT_DIR = Path(__file__).resolve().parents[1]
CREDENTIALS_DIR = str(_ROOT_DIR / "credentials")
CLIENT_CONFIG = os.path.join(CREDENTIALS_DIR, "client_secret.json")
CREDS_FILE = os.path.join(CREDENTIALS_DIR, "mycreds.txt")

# Cache the authenticated Drive client for the process lifetime.
_drive: Optional[GoogleDrive] = None

# Upload retry policy. Drive drops the TLS connection mid-upload on flaky lines
# (ssl.SSLEOFError / ConnectionResetError / timeout / stall). We upload resumably
# and resume the SAME session on each failure, so each retry only re-sends the
# in-flight chunk (not the whole file).
_UPLOAD_ATTEMPTS = 12                # max network drops to ride out per upload
_UPLOAD_RETRY_BACKOFF_SECONDS = 3    # base backoff; retry N waits N * this
_UPLOAD_CHUNK_RETRIES = 3            # googleapiclient retries for session start
_UPLOAD_CHUNK_SIZE = 10 * 1024 * 1024  # 10 MB; fewer round trips, still commits within the request timeout
_UPLOAD_REQUEST_TIMEOUT = 120        # seconds; caps a stalled PUT so we can resume


def _build_drive() -> GoogleDrive:
    """Create and authorise a GoogleDrive client (see test.py)."""
    custom_settings = {
        "client_config_backend": "file",
        "client_config_file": CLIENT_CONFIG,
        # Request a refresh_token so an expired access token can be renewed
        # without re-authorising (pydrive2 defaults to online-only access,
        # which stores no refresh_token).
        "get_refresh_token": True,
    }
    gauth = GoogleAuth(settings=custom_settings)
    gauth.LoadCredentialsFile(CREDS_FILE)

    if gauth.credentials is None:
        gauth.LocalWebserverAuth()
    elif gauth.access_token_expired and not _has_refresh_token(gauth):
        # Access token is expired and there is no refresh token (the stored
        # credentials were authorised without offline access). Re-authorise
        # interactively so a refresh token is obtained.
        gauth.LocalWebserverAuth()
    elif gauth.access_token_expired:
        gauth.Refresh()
    else:
        gauth.Authorize()

    # Authorize() builds the Drive API client (gauth.service), but the Refresh()
    # path only refreshes the token and leaves service as None. Build the client
    # here whenever it is missing so uploads/quota keep working across refreshes.
    assert gauth.credentials is not None
    if getattr(gauth, "service", None) is None:
        from googleapiclient.discovery import build
        if gauth.http is None:
            gauth.http = gauth._build_http()
        gauth.http = gauth.credentials.authorize(gauth.http)
        gauth.service = build("drive", "v2", http=gauth.http, cache_discovery=False)
    gauth.SaveCredentialsFile(CREDS_FILE)
    return GoogleDrive(gauth)


def _has_refresh_token(gauth: GoogleAuth) -> bool:
    """Return True when the loaded credentials carry a usable refresh token."""
    creds = gauth.credentials
    return bool(creds is not None and getattr(creds, "refresh_token", None))


def get_drive() -> GoogleDrive:
    """Return the shared authenticated Drive client (built lazily)."""
    global _drive
    if _drive is None:
        _drive = _build_drive()
    return _drive


def _service_and_http() -> Tuple[Any, Any]:
    """Return the authenticated Drive API client and its http transport.

    ``GoogleDrive.auth`` and its ``service`` / ``http`` members are typed
    Optional, but ``_build_drive`` always populates them; the asserts document
    that.
    """
    drive = get_drive()
    auth = drive.auth
    assert auth is not None
    service = auth.service
    http = auth.http
    assert service is not None and http is not None
    return service, http


# Short-lived cache so we don't hit the Drive API on every request.
# Value: (timestamp, total, used, remaining).
_quota_cache: Optional[Tuple[float, int, int, int]] = None
_QUOTA_TTL_SECONDS = 60.0


def get_drive_quota() -> Optional[Tuple[int, int, int]]:
    """Return the Drive account quota as (total, used, remaining) bytes.

    Uses the Drive API ``about`` endpoint. Returns None when credentials or
    the network are unavailable, so callers can fall back to local-only checks.
    """
    global _quota_cache
    now = time.time()
    if _quota_cache is not None and (now - _quota_cache[0]) < _QUOTA_TTL_SECONDS:
        _, total, used, remaining = _quota_cache
        return (total, used, remaining)
    try:
        # The API client lives on the auth object (drive.auth.service); the
        # GoogleDrive wrapper has no .client attribute.
        service, http = _service_and_http()
        about = service.about().get().execute(http=http)
        total = int(about.get("quotaBytesTotal") or 0)
        used = int(about.get("quotaBytesUsed") or 0)
        remaining = max(0, total - used)
        _quota_cache = (now, total, used, remaining)
        logger.info("drive quota: total=%d used=%d remaining=%d", total, used, remaining)
        return (total, used, remaining)
    except Exception as exc:
        logger.warning("drive quota check failed: %s", exc)
        return None


# Short-lived cache so the home page does not re-check the same file on every
# load. Value: (timestamp, exists). Only definitive results (True/False) are
# cached; unknown (API error) results are always re-checked.
_exists_cache: Dict[str, Tuple[float, bool]] = {}
_EXISTS_TTL_SECONDS = 120.0


def exists(file_id: str) -> Optional[bool]:
    """Check whether a Drive file still exists.

    Returns True (exists), False (definitively gone / HTTP 404), or None
    (unknown — the API call failed, e.g. network or auth). Callers must only
    treat a definitive False as "missing"; None means "leave it alone".
    """
    now = time.time()
    cached = _exists_cache.get(file_id)
    if cached is not None and (now - cached[0]) < _EXISTS_TTL_SECONDS:
        return cached[1]
    result: Optional[bool]
    try:
        service, http = _service_and_http()
        service.files().get(
            fileId=file_id, fields="id", supportsAllDrives=True
        ).execute(http=http)
        result = True
    except HttpError as exc:
        if exc.resp.status == 404:
            result = False
        else:
            logger.warning("drive exists (non-404) failed for %s: %s", file_id, exc)
            return None
    except Exception as exc:
        logger.warning("drive exists check failed for %s: %s", file_id, exc)
        return None
    _exists_cache[file_id] = (now, result)
    return result


def make_drive_filename(title: str, size_bytes: int) -> str:
    """Build a unique, Drive-safe filename for an upload.

    Same-title videos must not share a name in Drive, so the file is stored
    under a short SHA-256 digest (not the raw title). The digest mixes the
    title and size with a per-upload random component so even two identical
    uploads get distinct names; the human title stays in the DB for display.
    """
    seed = f"{title}|{size_bytes}|{uuid.uuid4().hex}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return f"{digest[:16]}.mp4"


def upload_file(
    local_path: str,
    title: str,
    folder_id: str,
    drive_filename: Optional[str] = None,
    progress_cb: Optional[Any] = None,
    resumable_uri: Optional[str] = None,
    on_session: Optional[Any] = None,
) -> str:
    """Upload a local file to the target Drive folder. Returns the file ID.

    ``drive_filename`` is the name the file is stored under in Drive; when
    omitted the human ``title`` is used. Same-title videos should pass a
    hash-based name (see ``make_drive_filename``) so they stay distinct.

    ``progress_cb`` (optional) is called as ``progress_cb(uploaded_bytes,
    total_bytes)`` after each committed chunk so a caller (e.g. the background
    worker) can surface transfer progress to the UI.

    ``resumable_uri`` (optional) is a session URI saved from an earlier attempt
    (e.g. a previous process run); the upload then continues from the last
    committed chunk instead of starting over. ``on_session`` (optional) is
    called as ``on_session(uri)`` whenever the session URI becomes known or
    changes, so a caller can persist it for a later resume.

    Uses a resumable upload driven by ``request.next_chunk()`` in a loop, so a
    dropped TLS connection (ssl.SSLEOFError / ConnectionResetError / timeout)
    resumes from the last committed chunk instead of restarting the whole file.
    pydrive2's ``Upload()`` runs a single ``execute()`` and aborts on the first
    network error, which makes large files (hundreds of MB) unreliable.
    """
    drive = get_drive()
    service, http = _service_and_http()
    # httplib2 defaults to no timeout, so a stalled (open-but-silent) connection
    # would hang forever. Cap each request so a stall times out and resumes.
    http.timeout = _UPLOAD_REQUEST_TIMEOUT
    name = drive_filename or title
    logger.info("drive upload: title=%r name=%r folder=%s path=%s", title, name, folder_id, local_path)

    total_bytes = os.path.getsize(local_path)
    session = resumable_uri
    while True:
        try:
            response, retries = _upload_session(
                service, http, drive, name, folder_id, local_path, total_bytes,
                progress_cb, session, on_session,
            )
            break
        except HttpError as exc:
            if session is not None and exc.resp.status in (404, 410):
                # The server no longer knows this session (it expired): start fresh.
                logger.warning("drive upload: saved session expired; starting a fresh one")
                session = None
                continue
            raise

    logger.info(
        "drive upload ok: title=%r id=%s (after %d resume(s))",
        title, response.get("id"), retries,
    )
    return str(response.get("id"))


def _upload_session(
    service: Any,
    http: Any,
    drive: Any,
    name: str,
    folder_id: str,
    local_path: str,
    total_bytes: int,
    progress_cb: Optional[Any],
    resumable_uri: Optional[str],
    on_session: Optional[Any],
) -> Tuple[Any, int]:
    """Run one resumable upload session. Returns ``(response, resume_count)``."""
    # Same metadata body pydrive2's own Upload() would send (Drive v2 names).
    gfile = drive.CreateFile({
        "title": name,
        "mimeType": "video/mp4",
        "parents": [{"id": folder_id}],
    })
    body = gfile.GetChanges()

    response = None
    retries = 0
    reported_uri = resumable_uri
    with open(local_path, "rb") as fh:
        media = MediaIoBaseUpload(
            fh, mimetype="video/mp4", chunksize=_UPLOAD_CHUNK_SIZE, resumable=True
        )
        request = service.files().insert(
            body=body,
            media_body=media,
            supportsAllDrives=True,
            fields="id",
        )
        if resumable_uri is not None:
            # Resume a session started by a previous process run: skip the
            # session-start POST and force the server progress query so
            # resumable_progress syncs to the last committed chunk.
            request.resumable_uri = resumable_uri
            request._in_error_state = True
        logger.info(
            "drive upload: starting resumable transfer (%d bytes, %d-byte chunks)%s",
            total_bytes, _UPLOAD_CHUNK_SIZE,
            " (resuming saved session)" if resumable_uri is not None else "",
        )
        while response is None:
            try:
                status, response = request.next_chunk(
                    http=http, num_retries=_UPLOAD_CHUNK_RETRIES
                )
                if status is not None:
                    uri = getattr(request, "resumable_uri", None)
                    if on_session is not None and uri and uri != reported_uri:
                        reported_uri = uri
                        on_session(uri)
                    frac = status.progress() or 0.0
                    logger.info("drive upload progress: %d%%", int(frac * 100))
                    if progress_cb is not None:
                        progress_cb(int(frac * total_bytes), total_bytes)
            except (OSError, _HttpClientException) as exc:
                # Covers SSL EOF, connection reset, timeout, and incomplete reads
                # (the connection dropped mid-transfer). The resumable session
                # survives it; the next next_chunk() query resumes from the last
                # committed chunk.
                retries += 1
                logger.warning(
                    "drive upload attempt %d/%d hit network error (%s); resuming",
                    retries, _UPLOAD_ATTEMPTS, exc,
                )
                if retries >= _UPLOAD_ATTEMPTS:
                    raise
                time.sleep(_UPLOAD_RETRY_BACKOFF_SECONDS * retries)
    return response, retries


def download_file(file_id: str, dest_path: str, progress_cb=None) -> str:
    """Download a Drive file to ``dest_path``. Returns ``dest_path``.

    ``progress_cb`` (optional) is called as ``progress_cb(transferred, total)``
    as the bytes arrive (pydrive2's ``GetContentFile`` callback), so the caller
    can report download progress.
    """
    drive = get_drive()
    gfile = drive.CreateFile({"id": file_id})
    gfile.GetContentFile(dest_path, callback=progress_cb)
    return dest_path


def delete_file(file_id: str) -> None:
    """Delete a Drive file by ID (Drive v2). Raises on failure; the caller
    decides how to handle it (the delete flow treats it as best-effort)."""
    service, http = _service_and_http()
    service.files().delete(
        fileId=file_id, supportsAllDrives=True
    ).execute(http=http)
    logger.info("drive delete: id=%s", file_id)


def _reset_for_tests() -> None:
    """Drop the cached client and quota (used by tests)."""
    global _drive, _quota_cache
    _drive = None
    _quota_cache = None
    _exists_cache.clear()
