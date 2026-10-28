"""Apply documented AV1 live compatibility fixes to mpegts.js 1.8.2.

Usage: python apply-av1-metadata-patch.py path/to/mpegts.min.js
The file must be the verified official npm bundle or the already-patched bundle.
"""
import hashlib
from pathlib import Path
import sys

UPSTREAM = "bda31748736a69cb610c2edf4623e633f1f4f47b5bda83668c8d287e51b0c3a8"
target = Path(sys.argv[1])
original = target.read_bytes()
text = original.decode("utf-8")
start = text.index("_parseEnhancedAV1VideoPacket=function")
end = text.index(".prototype._parseAVCDecoderConfigurationRecord", start)
section = text[start:end]
old = "if(2!==o)"
new = "if(2!==o&&4!==o)"
if new not in section:
    assert hashlib.sha256(original).hexdigest() == UPSTREAM, "Unexpected upstream bundle"
    assert section.count(old) == 1, "Unexpected AV1 packet handler"
    text = text[:start] + section.replace(old, new, 1) + text[end:]
    print("Patched AV1 packet type 4 to skip optional metadata without rejecting playback.")
start = text.index("_parseAV1VideoData=function")
end = text.index("}},e}()", start)
section = text[start:end]
old = "f.sarDen=c.sarRatio.height,"
new = old + "f.isComplete()&&this._onMediaInfo(f),"
if new not in section:
    assert section.count(old) == 1, "Unexpected AV1 media-info handler"
    text = text[:start] + section.replace(old, new, 1) + text[end:]
    print("Patched AV1 media-info dispatch after dimensions become available.")
# Upstream logs an OBU type for every parsed OBU and metadata for keyframes.
text = text.replace("console.log(r),1===r?", "1===r?")
text = text.replace("console.log(h),c.codecWidth=", "c.codecWidth=")
target.write_bytes(text.encode("utf-8"))
print("Vendored SHA-256:", hashlib.sha256(target.read_bytes()).hexdigest())
