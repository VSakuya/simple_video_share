# mpegts.js 1.8.2 (AV1 live compatibility build)

Upstream: https://github.com/xqq/mpegts.js/releases/tag/v1.8.2

Downloaded from the official npm registry, pinned to 1.8.2. The npm tarball's
published SHA-512 integrity was checked before extracting the distribution
bundle. See package-metadata.json for the tarball URL, upstream bundle hash,
vendored bundle hash, and patch list.

LICENSE contains the upstream Apache-2.0 license. mpegts.js.LICENSE.txt preserves
the third-party license notices accompanying the npm distribution bundle.

## Local compatibility patches

The release supports Enhanced FLV AV1 but local browser verification exposed:

1. Optional Enhanced FLV AV1 packet type 4 (video metadata/colorInfo) was treated
   as an unsupported packet and stopped playback. The AV1 packet handler now
   skips that optional metadata. It does not drop sequence headers or frames.
2. AV1 media information was checked before frame dimensions were available and
   was not emitted again after they were parsed. Complete media information is
   now emitted after frame-header parsing so diagnostics/capability checks work.
3. Per-OBU and keyframe metadata console debug output was removed.

The patch does not implement HDR color-metadata rendering. The verified fixture
is AV1 Main-profile 8-bit SDR, realtime zero-lookahead encoding, with AAC audio.
Do not claim support for every AV1/HDR/offline-prediction combination from this
test. Browsers still need native MSE AV1 decoding capability.

## Reproduce

1. Download https://registry.npmjs.org/mpegts.js/-/mpegts.js-1.8.2.tgz and verify
   the published tarball integrity recorded in package-metadata.json.
2. Extract package/dist/mpegts.js. Its unmodified SHA-256 must be
   bda31748736a69cb610c2edf4623e633f1f4f47b5bda83668c8d287e51b0c3a8.
3. Run the maintenance script (not used by the website at runtime):

   `python apply-av1-metadata-patch.py path/to/mpegts.min.js`

4. Verify the result against vendoredSha256 in package-metadata.json, then run
   the local AV1/H.264 browser probes under testing/.

The live controller disables both mpegts.js latency-chasing mechanisms because
the site's own controller owns playbackRate and exceptional seeks.
