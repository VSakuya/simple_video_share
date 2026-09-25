"""Server-side on-air probe for live streams (§14.7).

The media server (Node-Media-Server behind the reverse proxy) answers
``200 OK`` plus the 9-byte FLV header *immediately* even when nobody is
pushing, and then the stream simply stalls (recorded 2026-09-25: a non-live
room sent 13 bytes in 20 s — the header plus PreviousTagSize0, no FLV tags).
HTTP status alone therefore cannot decide
on/off air. The only reliable signal is the arrival of a real media tag:
a video tag (FLV tag type 1) or an audio tag (type 8).

``probe_stream`` reads the stream for up to ``PROBE_TIMEOUT`` seconds and
returns ``True`` as soon as such a tag arrives; anything else (4xx/5xx,
missing FLV signature, stalled or closed stream, timeout) is off air.

FLV framing that the probe must account for:

- 9-byte header: ``FLV`` + version + type flags + 4-byte data offset.
- ``PreviousTagSize0``: 4 zero bytes, present *before* the first tag.
- Each tag: an 11-byte header (type 1, dataSize 3, timestamp 3,
  timestampExtended 1, streamID 3), ``dataSize`` payload bytes, then a
  4-byte ``PreviousTagSize``. All of the header/payload/PreviousTagSize of
  any non-media tag must be consumed before the next tag is read, or the
  walk desyncs and real media tags are misread as garbage.

``probe_stream`` bounds the whole walk by a total deadline (``PROBE_TIMEOUT``
seconds), so a live stream that keeps sending non-media data can never loop
forever; it also doubles as the per-read socket timeout for a stalled one.
"""

import base64
import time
import urllib.request

PROBE_TIMEOUT = 6.0  # total budget in seconds; also the per-read socket timeout

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
    deadline = time.monotonic() + PROBE_TIMEOUT

    def expired() -> bool:
        return time.monotonic() > deadline

    try:
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT) as resp:
            if resp.status != 200:
                return False

            def read(n: int) -> bytes:
                if expired():
                    return b""
                return resp.read(n)

            head = read(9)
            if len(head) < 9 or head[:3] != FLV_SIGNATURE:
                return False
            if len(read(4)) < 4:  # PreviousTagSize0
                return False
            # Walk the tags: skip anything that is not media (e.g. the
            # onMetaData script tag, type 18) until a video/audio tag shows.
            while not expired():
                tag_type = read(1)
                if not tag_type:
                    return False
                if tag_type[0] in (FLV_VIDEO_TAG, FLV_AUDIO_TAG):
                    return True
                size_bytes = read(3)
                if len(size_bytes) < 3:
                    return False
                size = int.from_bytes(size_bytes, "big")
                if len(read(7)) < 7:  # timestamp + tsExt + streamID
                    return False
                while size > 0:  # skip the tag payload
                    chunk = read(min(65536, size))
                    if not chunk:
                        return False
                    size -= len(chunk)
                if len(read(4)) < 4:  # this tag's PreviousTagSize
                    return False
            return False  # deadline reached with no media tag
    except Exception:
        # Any failure (401/404, DNS, timeout on a stalled stream, ...) is
        # reported as off air.
        return False
