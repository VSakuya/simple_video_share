"""MP4 box-level utilities (byte reordering only — never re-encodes).

Currently provides ``ensure_faststart``: moves the top-level ``moov`` atom right
after ``ftyp`` so that progressive HTTP delivery (Flask ``send_file`` with Range
support) can start playback as soon as the header has been downloaded, instead of
waiting for the whole file. Only the chunk offsets inside ``stco``/``co64`` boxes
need rewriting (they hold absolute file offsets for the mdat payload); every box
keeps its size, so nothing else changes.
"""

import logging
import os
import struct
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("simple_video_share.mp4")

# A parsed box: (box_type, total_size_incl_header, absolute_offset, header_size).
# header_size is 8 for a normal box, 16 when the 64-bit extended size is used.
Box = Tuple[bytes, int, int, int]

# First child box type(s) expected inside each container we descend into
# (moov -> trak -> mdia -> minf -> stbl). Per ISO/IEC 14496-12 these are always
# present and come first; matching them lets ``_child_base`` tell whether a
# container is a FullBox (child after a 4-byte version/flags field) or a plain
# container (child right after the header) -- moov in particular is spec'd as a
# FullBox yet many encoders write it as a plain container.
_FIRST_CHILD: Dict[bytes, Tuple[bytes, ...]] = {
    b"moov": (b"mvhd",),
    b"trak": (b"tkhd",),
    b"mdia": (b"mdhd",),
    b"minf": (b"vmhd", b"smhd", b"hmhd", b"sthd"),
    b"stbl": (b"stsd",),
}


def ensure_faststart(path: str) -> bool:
    """Move the ``moov`` atom to the front of the MP4 file at ``path`` (faststart).

    Returns True when the file is left as-is (already faststart, fragmented, not a
    progressive MP4, or unparseable) or after a successful reorder, and False on a
    read error or when a reorder could not be completed. The rewrite is atomic
    (temp file + ``os.replace``) and never changes the file size; on any parse
    error the original file is left untouched.
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as exc:
        logger.warning("ensure_faststart: cannot read %s: %s", path, exc)
        return False

    try:
        new_data = _faststart(data)
    except (ValueError, struct.error) as exc:
        # Not a progressive MP4 we can reorder (or unparseable): leave it as-is.
        logger.warning("ensure_faststart: %s left untouched (%s)", path, exc)
        return True
    if new_data is None:
        return True  # already faststart (or a file type we do not reorder)

    # Sanity: the result must still parse, with moov before mdat.
    try:
        boxes = _parse_top_level(new_data)
        moov_off = next(b[2] for b in boxes if b[0] == b"moov")
        mdat_off = next(b[2] for b in boxes if b[0] == b"mdat")
        if moov_off >= mdat_off:
            raise ValueError("moov still not before mdat after reorder")
    except (StopIteration, ValueError, struct.error) as exc:
        logger.warning("ensure_faststart: sanity check failed, kept original (%s)", exc)
        return False

    # Atomic replace: write a temp file in the same directory, then rename over.
    tmp_path = path + ".faststart.tmp"
    try:
        with open(tmp_path, "wb") as f:
            f.write(new_data)
        if os.path.getsize(tmp_path) != len(data):
            raise ValueError("size changed during rewrite")
        os.replace(tmp_path, path)
    except (OSError, ValueError) as exc:
        logger.warning("ensure_faststart: rewrite of %s failed: %s", path, exc)
        try:
            os.remove(tmp_path)
        except OSError:
            pass  # best-effort cleanup
        return False
    logger.info("ensure_faststart: moved moov atom to front of %s", os.path.basename(path))
    return True


def _faststart(data: bytes) -> Optional[bytes]:
    """Return reordered bytes (moov up front), or None when no change is needed.

    Raises ValueError/struct.error when the file is not a parseable progressive MP4.
    """
    boxes = _parse_top_level(data)
    types = [b[0] for b in boxes]
    if b"moov" not in types or b"mdat" not in types:
        return None  # not a progressive MP4 we handle
    if b"mvex" in types:
        return None  # fragmented MP4: moov placement is its own convention
    mdats = [b for b in boxes if b[0] == b"mdat"]
    if len(mdats) != 1:
        return None  # multiple top-level mdat boxes: not supported, leave as-is
    moov = next(b for b in boxes if b[0] == b"moov")
    mdat = mdats[0]
    if moov[2] < mdat[2]:
        return None  # already faststart

    # New order: ftyp first (if present), then moov, then everything else
    # (mdat and any free/skip boxes) in their original relative order.
    new_order: List[Box] = [b for b in boxes if b[0] == b"ftyp"]
    new_order.append(moov)
    new_order.extend(
        b for b in boxes if b[0] != b"ftyp" and b[2] != moov[2]
    )

    # Delta of the mdat payload's absolute offset (stco/co64 store those).
    new_mdat_payload = sum(b[1] for b in new_order if b[2] != mdat[2]) + mdat[3]
    old_mdat_payload = mdat[2] + mdat[3]
    delta = new_mdat_payload - old_mdat_payload

    out = bytearray()
    moov_new_off = 0
    for box in new_order:
        if box[0] == b"moov":
            moov_new_off = len(out)
        out += data[box[2]:box[2] + box[1]]
    # Patch chunk offsets inside the moved moov (now located at moov_new_off).
    _patch_chunk_offsets(out, (b"moov", moov[1], moov_new_off, moov[3]), delta)
    return bytes(out)


def _parse_top_level(data: bytes) -> List[Box]:
    """Parse the top-level boxes of an MP4 file. Raises ValueError/struct.error."""
    boxes: List[Box] = []
    off = 0
    n = len(data)
    while off < n:
        if off + 8 > n:
            raise ValueError("truncated box header at offset %d" % off)
        size = struct.unpack_from(">I", data, off)[0]
        box_type = data[off + 4:off + 8]
        hdr = 8
        if size == 1:
            if off + 16 > n:
                raise ValueError("truncated 64-bit box header")
            size = struct.unpack_from(">Q", data, off + 8)[0]
            hdr = 16
        elif size == 0:
            size = n - off
        if size < hdr or off + size > n:
            raise ValueError("box %r extends beyond file end" % box_type)
        boxes.append((box_type, size, off, hdr))
        off += size
    return boxes


def _iter_children(data: bytes | bytearray, start: int, end: int) -> List[Box]:
    """Parse the child boxes inside [start, end). Raises ValueError/struct.error."""
    boxes: List[Box] = []
    off = start
    while off < end:
        if off + 8 > end:
            raise ValueError("truncated child box header at offset %d" % off)
        size = struct.unpack_from(">I", data, off)[0]
        box_type = bytes(data[off + 4:off + 8])
        hdr = 8
        if size == 1:
            if off + 16 > end:
                raise ValueError("truncated 64-bit child box header")
            size = struct.unpack_from(">Q", data, off + 8)[0]
            hdr = 16
        elif size == 0:
            size = end - off
        if size < hdr or off + size > end:
            raise ValueError("child box %r extends beyond container" % box_type)
        boxes.append((box_type, size, off, hdr))
        off += size
    return boxes


def _patch_chunk_offsets(data: bytearray, moov: Box, delta: int) -> None:
    """Add ``delta`` to every chunk offset (stco/co64) inside the ``moov`` box.

    stco/co64 only ever appear at moov/trak/mdia/minf/stbl, so we follow that
    exact path instead of a generic descent: leaf boxes such as tkhd are NOT
    containers and must not be parsed as such.
    """
    for trak in _children(data, moov, b"trak"):
        for mdia in _children(data, trak, b"mdia"):
            for minf in _children(data, mdia, b"minf"):
                for stbl in _children(data, minf, b"stbl"):
                    for box in _children(data, stbl, b"stco") + _children(data, stbl, b"co64"):
                        _rewrite_offsets(data, box, delta)


def _children(data: bytes | bytearray, parent: Box, wanted: bytes) -> List[Box]:
    """Return the child boxes of type ``wanted`` inside the container ``parent``.

    ``parent`` is either a FullBox or a plain container; ``_child_base`` resolves
    where its child boxes actually start.
    """
    start = _child_base(data, parent)
    end = parent[2] + parent[1]
    return [b for b in _iter_children(data, start, end) if b[0] == wanted]


def _child_base(data: bytes | bytearray, parent: Box) -> int:
    """Return the offset where ``parent``'s child boxes start.

    A FullBox has a 4-byte version/flags field between its header and its first
    child; a plain container does not. ``moov`` is spec'd as a FullBox, yet many
    encoders (and the files we serve) write it as a plain container, so we test
    both offsets and keep the one whose first child has an expected type.
    """
    base = parent[2] + parent[3]  # end of the 8- or 16-byte header
    end = parent[2] + parent[1]
    expected = _FIRST_CHILD.get(parent[0], ())
    for off in (0, 4):
        p = base + off
        if p + 8 > end:
            continue
        if data[p + 4:p + 8] in expected:
            return p
    raise ValueError("cannot locate first child of box %r" % parent[0])


def _rewrite_offsets(data: bytearray, box: Box, delta: int) -> None:
    """Add ``delta`` to every offset entry of an stco (32-bit) or co64 (64-bit) box.

    Both are FullBoxes: version/flags (4 bytes) + entry_count (4 bytes), then
    the chunk offsets. Only the offset values change, so the box size is kept.
    """
    box_type, size, off, hdr = box
    if box_type == b"stco":
        fmt, step = ">I", 4
    elif box_type == b"co64":
        fmt, step = ">Q", 8
    else:
        return
    entry_count = (size - hdr - 8) // step
    for i in range(entry_count):
        pos = off + hdr + 8 + i * step
        value = struct.unpack_from(fmt, data, pos)[0]
        struct.pack_into(fmt, data, pos, value + delta)

