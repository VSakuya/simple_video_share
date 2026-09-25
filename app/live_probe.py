"""Server-side on-air probe for live streams (§14.7).

The media server (Node-Media-Server behind the reverse proxy) answers
``200 OK`` plus the 9-byte FLV header *immediately* even when nobody is
pushing, and then the stream simply stalls (recorded 2026-09-25: a non-live
room sent 13 bytes in 20 s — the header and the start of an empty
``onMetaData`` tag, no media). HTTP status alone therefore cannot decide
on/off air. The only reliable signal is the arrival of a real media tag:
a video tag (FLV tag type 1) or an audio tag (type 8).

``probe_stream`` reads the stream for up to ``PROBE_TIMEOUT`` seconds and
returns ``True`` as soon as such a tag arrives; anything else (4xx/5xx,
missing FLV signature, stalled or closed stream, timeout) is off air.
"""

import base64
import urllib.request

PROBE_TIMEOUT = 6.0  # seconds; also the per-read socket timeout

FLV_SIGNATURE = b"FLV"
FLV_VIDEO_TAG = 1
FLV_AUDIO_TAG = 8


def probe_stream(url: str, user: str = "", password: str = "") -> bool:
    """Return ``True`` only when a video or audio FLV tag arrives in time.

    ``user``/``password`` are Basic-auth credentials for the stream server
    (empty = the stream is open).
    """
    headers = {"User-Agent": "simple-video-share/live-probe"}
    if user:
        token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT) as resp:
            if resp.status != 200:
                return False
            head = resp.read(9)
            if len(head) < 9 or head[:3] != FLV_SIGNATURE:
                return False
            # Walk the tags: skip anything that is not media (e.g. the
            # onMetaData script tag, type 18) until a video/audio tag shows.
            while True:
                tag_type = resp.read(1)
                if not tag_type:
                    return False  # stream closed
                if tag_type[0] in (FLV_VIDEO_TAG, FLV_AUDIO_TAG):
                    return True
                size_bytes = resp.read(3)
                if len(size_bytes) < 3:
                    return False
                size = int.from_bytes(size_bytes, "big")
                # timestamp (3) + timestampExtended (1) + previousTagSize (3)
                if len(resp.read(7)) < 7:
                    return False
                while size > 0:  # skip the tag payload
                    chunk = resp.read(min(65536, size))
                    if not chunk:
                        return False
                    size -= len(chunk)
    except Exception:
        # Any failure (401/404, DNS, timeout on a stalled stream, ...) is
        # reported as off air.
        return False
