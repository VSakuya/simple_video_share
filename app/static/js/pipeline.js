// pipeline.js — shared, page-agnostic upload pipeline.
//
// Extracted from upload.js so both the main upload page (multi-file queue,
// auto-transcode, no clip) and the edit/clip page (single file + clip) reuse
// the same probe / transcode / cover / upload logic without duplication.
//
// The pipeline never touches page-specific DOM directly. A page supplies a
// `ctx` object with callbacks + the progress elements it wants updated:
//   ctx = {
//     log: (level, msg) => {},          // page logger (console + panel + server)
//     progressPanel: Element,          // the progress panel to unhide
//     progressFill:  Element,          // the progress bar fill
//     progressStatus: Element,         // the status text line
//     progressSpeed: Element | null,   // the speed text line (upload phase)
//   }
//
// WebCodecs requires a secure context (HTTPS or localhost); in an insecure
// context the transcode stage is skipped and the original file passes through.

import { FFmpeg } from "./ffmpeg/lib/index.js";

// Base path the app is mounted under behind a reverse proxy ("" at the domain
// root). Set in base.html as window.SVS_BASE. Prefix every hard-coded
// app-relative URL with it so fetches / redirects / asset loads work under
// e.g. /video as well as at the root.
const SVS_BASE = (typeof window !== "undefined" && window.SVS_BASE) ? window.SVS_BASE : "";

const SEGMENT_THRESHOLD = 1024 * 1024 * 1024; // split when final size > 1 GB
const SEGMENT_SIZE = 512 * 1024 * 1024; // 512 MB per part
const MAX_HEIGHT = 1080;
const MAX_FPS = 60;
// Bitrate cap (kbps): the admin "default bitrate" setting, exposed by the page
// as window.SVS_MAX_BITRATE_KBPS. Falls back to 5000 when the value is missing
// or invalid (e.g. page loaded without the server var).
export const MAX_BITRATE_KBPS = (() => {
  const n = Number(typeof window !== "undefined" ? window.SVS_MAX_BITRATE_KBPS : NaN);
  return Number.isFinite(n) && n > 0 ? n : 5000;
})();
// WebCodecs codec ids, in priority order, used by the mediabunny path.
const WEBCODECS_CODEC_PRIORITY = ["av1", "avc"];
// Same-origin vendored build (app/static/js/vendor) — no CDN, no CORS.
const MEDIABUNNY_BUNDLE = "./vendor/mediabunny.min.mjs?v=2";

// Human-readable megabytes (one decimal).
export function fmtMB(bytes) {
  return (bytes / 1024 / 1024).toFixed(1) + " MB";
}

export function formatTime(sec) {
  if (!Number.isFinite(sec)) return "00:00";
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
}

export function parseTime(str) {
  if (!str) return NaN;
  const trimmed = str.trim();
  // A bare number is treated as seconds (e.g. "90" -> 1:30); handy for manual entry.
  if (!trimmed.includes(":")) {
    const sec = Number(trimmed);
    return Number.isFinite(sec) ? sec : NaN;
  }
  const parts = trimmed.split(":").map((x) => parseInt(x, 10));
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  return NaN;
}

// Build a page logger: timestamped lines echoed to the console, appended to the
// page's log panel via `appendLine(level, line)`, and batched to the server log
// endpoint. `appendLine` is page-specific (it knows its own log panel element).
export function createLogger(endpoint, appendLine) {
  let buffer = [];
  let timer = null;
  function p2(n) { return String(n).padStart(2, "0"); }
  function ts() {
    const d = new Date();
    return d.getFullYear() + "-" + p2(d.getMonth() + 1) + "-" + p2(d.getDate()) +
      " " + p2(d.getHours()) + ":" + p2(d.getMinutes()) + ":" + p2(d.getSeconds()) +
      "." + String(d.getMilliseconds()).padStart(3, "0");
  }
  function flush() {
    timer = null;
    if (!buffer.length) return;
    const lines = buffer;
    buffer = [];
    fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" },
      body: JSON.stringify({ lines }),
    }).catch(() => { /* diagnostic only - never let logging break the upload */ });
  }
  return function log(level, msg) {
    const line = ts() + "  " + String(level).toUpperCase().padEnd(5) + "  " + msg;
    if (level === "error") console.error("[upload] " + msg);
    else if (level === "warn") console.warn("[upload] " + msg);
    else console.log("[upload] " + msg);
    appendLine(level, line);
    buffer.push(line);
    if (!timer) timer = setTimeout(flush, 500);
  };
}

// ---------------------------------------------------------------------------
// mediabunny (WebCodecs) helpers — primary transcode / probe / cover path.
// ---------------------------------------------------------------------------

// Cache the dynamic import so the bundle is fetched at most once. A failed
// load is cleared (mediabunny = null) so the next call can retry.
let mediabunny = null;
let activeConversion = null;
let activeTranscodeController = null;

export function loadMediabunny() {
  if (!mediabunny) {
    mediabunny = import(MEDIABUNNY_BUNDLE);
  }
  return mediabunny; // caller awaits the cached Promise / module namespace
}

// WebCodecs needs a secure context AND at least one priority codec the browser
// can encode. Returns null when mediabunny can run, otherwise a human-readable
// reason: the original file then passes through and the reason is surfaced.
// Never throws.
export async function mediabunnyUnavailableReason() {
  if (!window.isSecureContext) {
    return "WebCodecs needs a secure context — open this page via http://127.0.0.1:8080 (localhost) or https://…";
  }
  let mb;
  try {
    mb = await loadMediabunny();
  } catch (err) {
    mediabunny = null; // allow a retry on the next call
    return "mediabunny.min.mjs failed to load (" + (err && err.message ? err.message : err) + ")";
  }
  if (!mb) return "mediabunny unavailable";
  for (const codec of WEBCODECS_CODEC_PRIORITY) {
    if (await mb.canEncode(codec).catch(() => false)) return null;
  }
  return "this browser can encode neither AV1 nor H.264";
}

// Input-format instances only exist once mediabunny is loaded, so build the
// list lazily. mediabunny auto-detects the actual format from this set.
let ALL_FORMATS = null;
function getFormats(mb) {
  if (ALL_FORMATS === null) {
    ALL_FORMATS = Array.isArray(mb.ALL_FORMATS)
      ? mb.ALL_FORMATS
      : [
          new mb.Mp4InputFormat(),
          new mb.QuickTimeInputFormat(),
          new mb.MatroskaInputFormat(),
          new mb.WebMInputFormat(),
          new mb.MpegTsInputFormat(),
          new mb.HlsInputFormat(),
          new mb.OggInputFormat(),
          new mb.FlacInputFormat(),
          new mb.Mp3InputFormat(),
          new mb.AdtsInputFormat(),
          new mb.WaveInputFormat(),
        ];
  }
  return ALL_FORMATS;
}

// Abort the in-flight mediabunny conversion (if any) and reset the shared
// transcode state. Safe to call repeatedly; used by the beforeunload hook and
// before each fresh codec attempt.
export function cancelActiveTranscode() {
  if (activeTranscodeController) {
    activeTranscodeController.abort();
    activeTranscodeController = null;
  }
  if (activeConversion) {
    activeConversion.cancel().catch(() => {});
    activeConversion = null;
  }
}

// Probe the source: mediabunny (accurate fps / bitrate, rotation-aware size)
// first, with an HTML5 <video> fallback if WebCodecs is unavailable.
// Returns { duration, width, height, fps, bitrateKbps }.
export function probeVideo(file, ctx) {
  ctx.log("info", "Probing via mediabunny (HTML5 <video> fallback if unavailable)...");
  return probeWithMediabunny(file).catch((e) => {
    ctx.log("warn", "mediabunny probe failed (" + e + "), using HTML5 <video>.");
    return probeWithHtml5(file);
  });
}

async function probeWithMediabunny(file) {
  const mb = await loadMediabunny();
  if (!mb) throw new Error("mediabunny unavailable");
  const input = new mb.Input({
    formats: getFormats(mb),
    source: new mb.BlobSource(file),
  });
  try {
    const vt = await input.getPrimaryVideoTrack();
    if (!vt) throw new Error("no video track");
    const duration = (await vt.getDurationFromMetadata()) || 0;
    const stats = await vt.computePacketStats();
    const fr = await vt.computeFrameRateMetrics();
    return {
      duration,
      width: vt.displayWidth, // already accounts for rotation (not codedWidth)
      height: vt.displayHeight,
      fps: fr.bestGuessFrameRate || fr.averageFrameRate || 0,
      bitrateKbps: Math.round((stats.averageBitrate || 0) / 1000),
    };
  } finally {
    input.dispose(); // mediabunny does not release it automatically
  }
}

function probeWithHtml5(file) {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file);
    const v = document.createElement("video");
    v.preload = "auto";
    v.muted = true;
    v.src = url;
    const finish = () => {
      const duration = v.duration || 0;
      const info = {
        duration,
        width: v.videoWidth || 0,
        height: v.videoHeight || 0,
        fps: 0, // HTML5 exposes no fps; best-effort 0
        bitrateKbps: duration > 0 ? Math.round(file.size / duration / 1000) : 0,
      };
      URL.revokeObjectURL(url);
      resolve(info);
    };
    v.onloadedmetadata = () => finish();
    v.onerror = () => {
      URL.revokeObjectURL(url);
      resolve({ duration: 0, width: 0, height: 0, fps: 0, bitrateKbps: 0 });
    };
  });
}

// Run the full transcode pipeline for one file and return the final Blob to
// upload. `opts.clip` is {start,end} or null (no clip). `ctx` carries the page's
// logger and progress elements. Returns { blob, info } where `info` is the
// source probe (the caller re-probes the output for stored metadata).
export async function processVideo(file, opts, ctx) {
  const log = ctx.log;
  // The effective bitrate cap: the page's custom cap (opts.maxBitrateKbps),
  // clamped to the admin hard maximum. Falls back to the admin max when the
  // page supplies no custom cap (e.g. the clip editor).
  const capBitrate = Math.min(
    (Number.isFinite(opts.maxBitrateKbps) && opts.maxBitrateKbps > 0)
      ? opts.maxBitrateKbps : MAX_BITRATE_KBPS,
    MAX_BITRATE_KBPS
  );
  // Direct pass is legitimate ONLY when there is no clip and every cap is
  // already met. In that case the original file is compliant and is uploaded
  // unchanged.
  const probe = opts.probe;
  let needs = !!opts.clip;
  if (!probe) needs = true; // unknown caps -> transcode to be safe
  else if (probe.height > MAX_HEIGHT) needs = true;
  else if (probe.fps > opts.maxFps) needs = true;
  else if (probe.bitrateKbps > capBitrate) needs = true;

  if (!needs) {
    log(
      "info",
      "Direct pass - no clip and all caps met (height<=" + MAX_HEIGHT + ", fps<=" + opts.maxFps + ", bitrate<=" + capBitrate + " kbps); uploading the original file unchanged."
    );
    return { blob: file, info: probe };
  }

  const clip = opts.clip;
  const capHeight = probe ? (probe.height > MAX_HEIGHT ? MAX_HEIGHT : 0) : MAX_HEIGHT;
  const capFps = probe && probe.fps > opts.maxFps ? opts.maxFps : 0;
  const bitrateKbps = Math.min(
    probe ? probe.bitrateKbps || capBitrate : capBitrate,
    capBitrate
  );
  log(
    "info",
    "Transcode required (clip=" +
      (clip ? clip.start + "-" + clip.end + "s" : "none") +
      "; caps: height<=1080, fps<=60, bitrate<=" + capBitrate + " kbps)."
  );

  // Stage 1 - mediabunny (WebCodecs, hardware accelerated), AV1 then H.264.
  const reason = await mediabunnyUnavailableReason();
  if (reason === null) {
    const mb = await loadMediabunny();
    for (const codec of WEBCODECS_CODEC_PRIORITY) {
      if (!(await mb.canEncode(codec).catch(() => false))) {
        log("warn", "mediabunny: browser cannot encode " + codec + ", skipping");
        continue;
      }
      cancelActiveTranscode(); // drop any stale signal before a fresh attempt
      activeTranscodeController = new AbortController();
      try {
        log("info", "Transcoding with mediabunny (codec=" + codec + ").");
        const blob = await runMediabunny(file, {
          codec, bitrateKbps, capHeight, capFps,
          trimStart: clip ? clip.start : undefined,
          trimEnd: clip ? clip.end : undefined,
        }, ctx);
        log("info", "mediabunny done - output " + fmtMB(blob.size));
        return { blob, info: probe };
      } catch (err) {
        log("warn", "mediabunny (" + codec + ") failed: " + err);
      }
    }
  } else {
    log("warn", "mediabunny unavailable: " + reason + " - falling back to ffmpeg.wasm.");
  }

  // Stage 2 - ffmpeg.wasm (software) as the last resort.
  for (const enc of ["libx264", "libvpx"]) {
    try {
      log("info", "Transcoding with ffmpeg.wasm (encoder=" + enc + ").");
      const blob = await runFfmpeg(file, {
        encoder: enc, bitrateKbps, capHeight, capFps,
        trimStart: clip ? clip.start : undefined,
        trimEnd: clip ? clip.end : undefined,
      }, ctx);
      log("info", "ffmpeg done - output " + fmtMB(blob.size));
      return { blob, info: probe };
    } catch (err) {
      log("warn", "ffmpeg (" + enc + ") failed: " + err);
    }
  }

  // A required transcode could not run - BLOCK the upload (never pass a
  // non-compliant original through).
  const why = reason !== null
    ? "mediabunny unavailable (" + reason + ") and ffmpeg.wasm failed for every encoder"
    : "mediabunny and ffmpeg.wasm both failed for every codec/encoder";
  throw new Error(why);
}

// ffmpeg.wasm transcode. Produces an output MP4 as a Blob. `opts` mirrors
// runMediabunny (codec/encoder, bitrateKbps, capHeight, capFps, trim).
const WORKERFS_INPUT_PATH = "/in/input.mp4";
async function runFfmpeg(file, opts, ctx) {
  const log = ctx.log;
  const ffmpeg = new FFmpeg();
  const onLog = (e) => {
    const msg = e && e.message ? e.message : String(e);
    if (msg && msg.trim()) log("debug", "[ffmpeg] " + msg.trim());
  };
  const onProgress = (p) => {
    // @ffmpeg/core@0.12 posts { progress, time } (both milliseconds).
    const ratio = p && p.time > 0 ? p.progress / p.time : 0;
    if (Number.isFinite(ratio)) ctx.setProgress(Math.min(1, Math.max(0, ratio)), "Transcoding...");
  };
  ffmpeg.on("log", onLog);
  ffmpeg.on("progress", onProgress);
  try {
    await ffmpeg.load({
      coreURL: SVS_BASE + "/static/js/ffmpeg/ffmpeg-core.js",
      wasmURL: SVS_BASE + "/static/js/ffmpeg/ffmpeg-core.wasm",
    });
    await ffmpeg.createDir("/in");
    const input = new File([file], "input.mp4", { type: file.type });
    await ffmpeg.mount("WORKERFS", { files: [input] }, "/in");

    const clipDur =
      typeof opts.trimStart === "number" && typeof opts.trimEnd === "number"
        ? opts.trimEnd - opts.trimStart
        : null;
    const videoFilters = [];
    if (opts.capHeight) videoFilters.push("scale=-2:min(" + MAX_HEIGHT + "\\,ih)");
    if (opts.capFps) videoFilters.push("fps=" + opts.capFps);

    const args = ["-nostdin", "-y"];
    if (typeof opts.trimStart === "number") args.push("-ss", String(opts.trimStart));
    args.push("-i", WORKERFS_INPUT_PATH);
    if (clipDur !== null) args.push("-t", String(clipDur));
    if (videoFilters.length) args.push("-vf", videoFilters.join(","));
    args.push(
      "-c:v",
      opts.encoder,
      "-b:v",
      String(opts.bitrateKbps) + "k",
      "-maxrate",
      String(opts.bitrateKbps) + "k",
      "-bufsize",
      String(opts.bitrateKbps * 2) + "k"
    );
    args.push("-c:a", "copy");
    args.push("output.mp4");
    log("info", "[ffmpeg] exec: " + JSON.stringify(args));

    // exec() returns the exit code (it does NOT throw on non-zero).
    const code = await ffmpeg.exec(args);
    if (code !== 0) {
      throw new Error("ffmpeg exited with code " + code + " for " + JSON.stringify(args));
    }
    try {
      await ffmpeg.deleteFile(WORKERFS_INPUT_PATH);
    } catch { /* best-effort */ }
    const data = await ffmpeg.readFile("output.mp4");
    if (!data || !data.length) throw new Error("ffmpeg produced no output");
    return new Blob([data], { type: "video/mp4" });
  } finally {
    ffmpeg.off("log", onLog);
    ffmpeg.off("progress", onProgress);
    try { ffmpeg.terminate(); } catch { /* best-effort */ }
  }
}

// mediabunny (WebCodecs, hardware-accelerated) transcode. Produces an output
// MP4 Blob. `opts` = { codec, bitrateKbps, capHeight, capFps, trimStart, trimEnd }.
async function runMediabunny(file, opts, ctx) {
  const log = ctx.log;
  const mb = await loadMediabunny();
  const input = new mb.Input({
    formats: getFormats(mb),
    source: new mb.BlobSource(file),
  });
  const target = new mb.BufferTarget();
  const output = new mb.Output({
    format: new mb.Mp4OutputFormat(),
    target,
  });

  const videoOpts = {
    codec: opts.codec,
    quality: new mb.Quality({ bitrate: opts.bitrateKbps * 1000, bitrateMode: "constant" }), // bitrate is bps; CBR pins the output to the cap (VBR overshoots ~2x)
    forceTranscode: true, // re-encode the video (audio stays on copy)
    hardwareAcceleration: 'prefer-hardware',
    keyFrameInterval: 300,
  };
  // Caps are applied only when needed (a 0 value means "keep original").
  if (opts.capHeight) videoOpts.height = opts.capHeight;
  if (opts.capFps) videoOpts.frameRate = opts.capFps;

  const conversionOpts = {
    input,
    output,
    tracks: "primary", // primary video + audio tracks only
    video: videoOpts,
    // audio omitted = copy (no re-encode).
  };
  if (typeof opts.trimStart === "number") {
    conversionOpts.trim = { start: opts.trimStart };
  }
  if (typeof opts.trimEnd === "number") {
    conversionOpts.trim = Object.assign({}, conversionOpts.trim, { end: opts.trimEnd });
  }

  const conversion = await mb.Conversion.init(conversionOpts);
  activeConversion = conversion;

  if (!conversion.isValid) {
    log(
      "warn",
      "mediabunny conversion invalid for codec " + opts.codec +
        "; discardedTracks: " + JSON.stringify(conversion.discardedTracks || [])
    );
    throw new Error("mediabunny conversion is invalid for codec " + opts.codec);
  }
  if (conversion.discardedTracks && conversion.discardedTracks.length > 0) {
    log("warn", "mediabunny discarded tracks: " + JSON.stringify(conversion.discardedTracks));
  }

  // onProgress MUST be assigned before execute() or no progress events fire.
  conversion.onProgress = (progress) => {
    ctx.setProgress(progress, "Transcoding…");
  };

  try {
    await conversion.execute(
      activeTranscodeController ? { pauseSignal: activeTranscodeController.signal } : undefined
    );
    if (!target.buffer) throw new Error("mediabunny produced no output buffer");
    return new Blob([target.buffer], { type: "video/mp4" });
  } finally {
    activeConversion = null;
    input.dispose(); // must be released manually
  }
}

// Extract a cover from the 60s frame (mediabunny WebCodecs decode, else an
// HTML5 <video> seek). Returns a JPEG Blob.
export function extractCover(file, duration, ctx) {
  ctx.log("info", "Extracting cover (mediabunny decode, HTML5 fallback)...");
  return extractCoverWithMediabunny(file, duration).catch((e) => {
    ctx.log("warn", "mediabunny cover failed (" + e + "), using HTML5 <video>.");
    return extractCoverWithHtml5(file);
  });
}

async function extractCoverWithMediabunny(file, duration) {
  const mb = await loadMediabunny();
  if (!mb) throw new Error("mediabunny unavailable");
  const input = new mb.Input({
    formats: getFormats(mb),
    source: new mb.BlobSource(file),
  });
  let sample = null;
  try {
    const vt = await input.getPrimaryVideoTrack();
    if (!vt) throw new Error("no video track");
    const time = Math.min(60, duration || 0); // same 60s mark as the HTML5 path
    sample = await new mb.VideoSampleSink(vt).getSample(time); // last frame at or before `time`
    if (!sample) throw new Error("no sample");
    const canvas = document.createElement("canvas");
    canvas.width = sample.displayWidth;
    canvas.height = sample.displayHeight;
    const ctx2d = canvas.getContext("2d");
    sample.drawWithFit(ctx2d, { fit: "contain" }); // aspect-fit, rotation applied
    return await new Promise((res, rej) =>
      canvas.toBlob((blob) => (blob ? res(blob) : rej(new Error("cover failed"))), "image/jpeg", 0.85)
    );
  } finally {
    if (sample) sample.close(); // must be released manually
    input.dispose(); // must be released manually
  }
}

function extractCoverWithHtml5(file) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const v = document.createElement("video");
    v.preload = "auto";
    v.muted = true;
    v.src = url;
    v.onloadeddata = () => {
      const target = Math.min(60, v.duration || 0);
      v.currentTime = target;
      v.onseeked = () => {
        const canvas = document.createElement("canvas");
        canvas.width = v.videoWidth || 640;
        canvas.height = v.videoHeight || 360;
        canvas.getContext("2d").drawImage(v, 0, 0);
        URL.revokeObjectURL(url);
        canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error("cover failed"))), "image/jpeg", 0.85);
      };
    };
    v.onerror = () => reject(new Error("cover probe failed"));
  });
}

// Build the page's progress-bar updater from the ctx elements. A 0 ratio means
// "no measurable ratio yet" (show the indeterminate animated bar); a real 0..1
// ratio fills the bar.
export function makeSetProgress(ctx) {
  return function setProgress(ratio, status) {
    if (ctx.progressPanel) ctx.progressPanel.classList.remove("hidden");
    const clamped = Math.min(1, Math.max(0, ratio));
    const fill = ctx.progressFill;
    if (clamped === 0) {
      fill.classList.add("indeterminate");
      fill.style.width = "";
    } else {
      fill.classList.remove("indeterminate");
      fill.style.width = Math.round(clamped * 100) + "%";
    }
    if (status && ctx.progressStatus) ctx.progressStatus.textContent = status;
  };
}

// POST via XMLHttpRequest so we get real upload progress (fetch exposes none).
function xhrPost(url, body, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url, true);
    xhr.setRequestHeader("X-Requested-With", "XMLHttpRequest");
    if (xhr.upload) xhr.upload.onprogress = (e) => onProgress(e.loaded);
    xhr.onload = () => {
      let j = null;
      try { j = JSON.parse(xhr.responseText); } catch { /* non-JSON body */ }
      if (xhr.status >= 200 && xhr.status < 300 && j && j.ok) resolve(j);
      else reject(new Error((j && j.error) || "Upload failed (" + xhr.status + ")"));
    };
    xhr.onerror = () => reject(new Error("Network error during upload"));
    xhr.send(body);
  });
}

// Build the XHR onProgress handler for one request. `offset` is the number of
// bytes already sent before this request (segmented uploads) and `total` is the
// whole-file size, so the bar reflects overall progress. Speed is the rolling
// MB/s between throttled ticks; DOM writes are capped at ~8/s.
function makeProgressCb(offset, total, ctx) {
  let lastBytes = 0;
  let lastTime = 0;
  let lastRender = 0;
  const speedEl = ctx.progressSpeed;
  return function onProgress(loaded) {
    const now = performance.now();
    if (lastTime === 0) { lastTime = now; lastRender = now; return; }
    if (now - lastRender < 120) return; // throttle DOM writes
    lastRender = now;
    const dt = (now - lastTime) / 1000;
    const speed = dt > 0 ? (loaded - lastBytes) / dt / 1048576 : 0; // MB/s
    lastBytes = loaded;
    lastTime = now;
    const overall = offset + loaded;
    const pct = total > 0 ? Math.round((overall / total) * 100) : 0;
    ctx.progressFill.classList.remove("indeterminate");
    ctx.progressFill.style.width = pct + "%";
    if (speedEl) {
      speedEl.classList.remove("hidden");
      speedEl.textContent =
        pct + "%  ·  " + fmtMB(overall) + " / " + fmtMB(total) +
        "  ·  " + speed.toFixed(2) + " MB/s";
    }
  };
}

function addMeta(fd, info) {
  // Prefer the re-probed output (reflects the transcoded file); fall back to
  // the source probe only if the output re-probe was skipped/failed.
  if (info) {
    fd.append("duration", String(info.duration || ""));
    fd.append("resolution", info.width ? info.width + "x" + info.height : "");
    fd.append("bitrate", String(info.bitrateKbps || ""));
    fd.append("fps", String(info.fps || ""));
  }
}

// Upload a final Blob (single POST, or split into <=512 MB segments).
// `meta` = { title, description, folderId, cover, info } (info is the re-probed
// output metadata used for the stored duration/resolution/bitrate/fps).
// §bug L67: append the chosen tags (meta.tagIds) to the upload FormData. The
// server caps at MAX_TAGS_PER_VIDEO and drops unknown/stale ids.
function appendTags(fd, meta) {
  if (meta.tagIds) for (const tid of meta.tagIds) fd.append("tags", String(tid));
}

export async function uploadVideo(blob, meta, ctx) {
  const log = ctx.log;
  if (blob.size <= SEGMENT_THRESHOLD) {
    const fd = new FormData();
    fd.append("video", blob, meta.title + ".mp4");
    if (meta.cover) fd.append("cover", meta.cover, "cover.jpg");
    fd.append("title", meta.title);
    fd.append("description", meta.description || "");
    fd.append("folder_id", meta.folderId || "");
    appendTags(fd, meta);
    addMeta(fd, meta.info);
    log("info", "Uploading (single) " + fmtMB(blob.size));
    ctx.setProgress(0, "Uploading…");
    await xhrPost(SVS_BASE + "/upload/submit", fd, makeProgressCb(0, blob.size, ctx));
    log("info", "Upload accepted by server");
    if (ctx.progressSpeed) ctx.progressSpeed.classList.add("hidden");
    ctx.setProgress(1, "Done");
    return;
  }
  const totalParts = Math.ceil(blob.size / SEGMENT_SIZE);
  log("info", "Uploading (segmented) " + totalParts + " part(s) of " + fmtMB(blob.size));
  const startRes = await fetch(SVS_BASE + "/upload/seg/start", {
    method: "POST",
    headers: { "X-Requested-With": "XMLHttpRequest" },
    body: new URLSearchParams({
      title: meta.title,
      total_parts: String(totalParts),
      expected_size: String(blob.size),
      folder_id: meta.folderId || "",
    }),
  });
  const start = await startRes.json();
  if (!start.ok) throw new Error(start.error || "seg_start failed");
  const token = start.token;
  const totalBytes = blob.size;
  for (let i = 0; i < totalParts; i++) {
    const part = blob.slice(i * SEGMENT_SIZE, (i + 1) * SEGMENT_SIZE);
    const fd = new FormData();
    fd.append("token", token);
    fd.append("part_index", String(i));
    fd.append("part", part, "part_" + i + ".bin");
    ctx.setProgress(i / totalParts, "Uploading segment " + (i + 1) + "/" + totalParts + "…");
    await xhrPost(SVS_BASE + "/upload/seg/part", fd, makeProgressCb(i * SEGMENT_SIZE, totalBytes, ctx));
  }
  const fd = new FormData();
  fd.append("token", token);
  if (meta.cover) fd.append("cover", meta.cover, "cover.jpg");
  fd.append("description", meta.description || "");
  appendTags(fd, meta);
  addMeta(fd, meta.info);
  ctx.setProgress(1, "Finalizing…");
  await xhrPost(SVS_BASE + "/upload/seg/finish", fd, () => {});
  if (ctx.progressSpeed) ctx.progressSpeed.classList.add("hidden");
  ctx.setProgress(1, "Done");
}