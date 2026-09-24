// upload.js — client-side transcode / clip / cover, then (segmented) upload.
//
// Pipeline (project_requirements.md, Section 3 + R4/R5):
//   1. Probe the source (duration / height / fps / bitrate) — via mediabunny
//      (WebCodecs) when available, else an HTML5 <video> element.
//   2. Direct pass when there is no clip and all caps are already met;
//      otherwise transcode with mediabunny (WebCodecs, hardware-accelerated,
//      AV1 with H.264 fallback, caps: 1080p / 60fps / 5000 kb/s). When
//      WebCodecs is unavailable or the transcode fails, the original file is
//      passed through so the upload is never lost. Plus an optional clip range.
//   3. Extract a cover from the 60s frame (mediabunny WebCodecs decode, else
//      an HTML5 <video> seek) or use a user-provided one.
//   4. Upload: single POST when the final file is <= 1 GB, otherwise split it
//      into <= 512 MB segments via /upload/seg/* (R4).
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
const MAX_BITRATE_KBPS = 5000;
// WebCodecs codec ids, in priority order, used by the mediabunny path.
// "av1" and "avc" are the ids mediabunny's capability checks understand.
const WEBCODECS_CODEC_PRIORITY = ["av1", "avc"];
// Same-origin vendored build (app/static/js/vendor) — no CDN, no CORS.
// Cache-bust: the import() response is cached in the browser HTTP cache, and early
// clients cached this URL with a wrong (text/plain) MIME type, which permanently
// blocked the module there. Bump ?v=N whenever the vendored bundle is replaced.
const MEDIABUNNY_BUNDLE = "./vendor/mediabunny.min.mjs?v=2";

const $ = (id) => document.getElementById(id);
const dropZone = $("drop-zone");
const fileInput = $("file-input");
const clipPanel = $("clip-panel");
const previewVideo = $("preview-video");
const progressPanel = $("progress-panel");
const progressFill = $("progress-fill");
const progressStatus = $("progress-status");
const coverInput = $("cover-input");
const coverPreview = $("cover-preview");
const coverPanel = $("cover-panel");
const coverCropDialog = $("cover-crop-dialog");
const coverCropImage = $("cover-crop-image");
const coverCropApply = $("cover-crop-apply");
const coverCropCancel = $("cover-crop-cancel");
let coverCropper = null;
const folderSelect = $("folder-select");
const folderNewName = $("folder-new-name");
const uploadForm = $("upload-form");
const titleInput = $("title-input");
const descriptionInput = $("description-input");
const folderInput = $("folder-input");
const fileLabel = $("file-label");
const videoMeta = $("video-meta");

// --- Client-side logging (console + on-page panel + POST to the server) ---
// Every pipeline stage is logged so a failure can be diagnosed from the
// server-side storage/logs/client.log without reopening DevTools.
const LOG_ENDPOINT = SVS_BASE + "/upload/log";
let _logBuffer = [];
let _logTimer = null;

function logTimestamp() {
  const d = new Date();
  const p = (n, w) => String(n).padStart(w, "0");
  return (
    d.getFullYear() + "-" + p(d.getMonth() + 1, 2) + "-" + p(d.getDate(), 2) +
    " " + p(d.getHours(), 2) + ":" + p(d.getMinutes(), 2) + ":" + p(d.getSeconds(), 2) +
    "." + p(d.getMilliseconds(), 3)
  );
}

// `level` is "info" | "warn" | "error" | "debug". The line is timestamped, shown
// on the page (#log-console), echoed to the console, and batched to /upload/log.
function clientLog(level, msg) {
  const line = logTimestamp() + "  " + String(level).toUpperCase().padEnd(5) + "  " + msg;
  if (level === "error") console.error("[upload] " + msg);
  else if (level === "warn") console.warn("[upload] " + msg);
  else console.log("[upload] " + msg);
  appendLogLine(level, line);
  _logBuffer.push(line);
  if (!_logTimer) _logTimer = setTimeout(flushLogs, 500);
}

function appendLogLine(level, line) {
  const el = $("log-console");
  if (!el) return;
  const row = document.createElement("div");
  row.className = "upload-log__line upload-log__line--" + level;
  row.textContent = line;
  el.appendChild(row);
  while (el.childElementCount > 400) el.removeChild(el.firstElementChild);
  el.scrollTop = el.scrollHeight;
}

function flushLogs() {
  _logTimer = null;
  if (!_logBuffer.length) return;
  const lines = _logBuffer;
  _logBuffer = [];
  fetch(LOG_ENDPOINT, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" },
    body: JSON.stringify({ lines }),
  }).catch(() => {
    /* diagnostic only - never let logging break the upload */
  });
}

const logClearBtn = $("log-clear");
if (logClearBtn) logClearBtn.addEventListener("click", () => { $("log-console").innerHTML = ""; });

let sourceFile = null;
let probeInfo = null; // { duration, width, height, fps, bitrateKbps }
let processedBlob = null; // final video Blob (original or transcoded)
let outputInfo = null; // { duration, width, height, fps, bitrateKbps } of processedBlob
let coverBlob = null;
let videoUrl = null; // object URL backing the preview <video>
let timelineDuration = 0; // seconds; total duration of the source video

// mediabunny (WebCodecs) state: the cached dynamic-import promise, the
// currently running conversion, and the AbortController that cancels it.
// The controller's .signal is handed to Conversion.execute() so the
// beforeunload hook (and a fresh codec attempt) can abort an in-flight run.
let mediabunny = null; // cached Promise of the mediabunny module (or null)
let activeConversion = null; // the in-flight mediabunny Conversion, if any
let activeTranscodeController = null; // AbortController for the in-flight conversion

// --- File selection (click or drag-and-drop) ---
function handleFiles(files) {
  const file = files[0];
  if (!file || !file.type.startsWith("video/")) return;
  sourceFile = file;
  clientLog("info", "File selected: " + file.name + " (" + (file.size / 1024 / 1024).toFixed(1) + " MB, " + file.type + ")");
  fileInput.files = files;
  titleInput.value = file.name.replace(/\.[^.]+$/, "");
  fileLabel.textContent = file.name;
  const sizeMB = (file.size / 1024 / 1024).toFixed(1);
  videoMeta.textContent = sizeMB + " MB";
  $("transcode-warning").classList.add("hidden");
  dropZone.classList.add("has-file");
  probeVideo(file).then((info) => {
    probeInfo = info;
    timelineDuration = info.duration || 0;
    clientLog(
      "info",
      "Probe: " + (info.duration || 0).toFixed(1) + "s, " +
        info.width + "x" + info.height + ", " + (info.fps || 0) + " fps, " +
        (info.bitrateKbps || 0) + " kbps"
    );
    clipPanel.classList.remove("hidden");
    $("clip-start").value = formatTime(0);
    $("clip-end").value = formatTime(info.duration);
    setupTimeline(file);
    const big = file.size > 2 * 1024 * 1024 * 1024;
    if (big) {
      progressStatus.textContent =
        "Large file — expect longer processing (or clip first).";
    }
  });
}

fileInput.addEventListener("change", () => handleFiles(fileInput.files));
dropZone.addEventListener("click", () => fileInput.click());
dropZone.addEventListener("dragover", (e) => {
  e.preventDefault();
  dropZone.classList.add("drag-over");
});
dropZone.addEventListener("dragleave", () => dropZone.classList.remove("drag-over"));
dropZone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropZone.classList.remove("drag-over");
  handleFiles(e.dataTransfer.files);
});

coverInput.addEventListener("change", onCoverSelected);
window.addEventListener("beforeunload", () => {
  cancelActiveTranscode();
  if (videoUrl) {
    URL.revokeObjectURL(videoUrl);
    videoUrl = null;
  }
});
uploadForm.addEventListener("submit", (e) => {
  e.preventDefault();
  void onUpload();
});

// A user-provided cover is cropped to 16:9 / 1280x720 in the browser before it
// is used (§13.21). The dialog is shared with the avatar-crop pattern.
function onCoverSelected() {
  const f = coverInput.files[0];
  if (!f) return;
  const url = URL.createObjectURL(f);
  const probe = new Image();
  probe.onload = () => {
    coverCropDialog.showModal();
    const card = coverCropDialog.querySelector(".dialog-card");
    const availW = card.clientWidth - 40;
    const availH = Math.round(window.innerHeight * 0.6);
    const w = probe.naturalWidth || 1, h = probe.naturalHeight || 1;
    const scale = Math.min(1, availW / w, availH / h);
    let src;
    if (scale < 1) {
      const c = document.createElement("canvas");
      c.width = Math.round(w * scale);
      c.height = Math.round(h * scale);
      c.getContext("2d").drawImage(probe, 0, 0, c.width, c.height);
      src = c.toDataURL("image/png");
      URL.revokeObjectURL(url);
    } else {
      src = url;
    }
    coverCropImage.onload = () => {
      if (coverCropper) coverCropper.destroy();
      coverCropper = new Cropper(coverCropImage, {
        aspectRatio: 16 / 9,
        background: true,
        autoCropArea: 0.8,
      });
    };
    coverCropImage.src = src;
  };
  probe.src = url;
}

coverCropApply.addEventListener("click", () => {
  if (!coverCropper) return;
  const canvas = coverCropper.getCroppedCanvas({ width: 1280, height: 720, imageSmoothingQuality: "high" });
  canvas.toBlob((blob) => {
    if (!blob) return;
    coverBlob = blob;
    coverPreview.src = URL.createObjectURL(blob);
    coverPreview.classList.remove("hidden");
    coverCropDialog.close();
  }, "image/jpeg");
});

coverCropCancel.addEventListener("click", () => {
  coverCropDialog.close();
  coverInput.value = ""; // no user cover -> the pipeline auto-extracts the 60s frame
  coverBlob = null;
  coverPreview.classList.add("hidden");
});

// Reveal the inline name field when "New folder…" is chosen.
folderSelect.addEventListener("change", () => {
  folderNewName.classList.toggle("hidden", folderSelect.value !== "__new__");
});

// Resolve the folder selection into the hidden `folder-input` before upload.
// "New folder…" is created server-side first (JSON) and its id is used.
async function resolveFolder() {
  const val = folderSelect.value;
  if (val === "__new__") {
    const name = folderNewName.value.trim();
    if (!name) {
      folderNewName.focus();
      throw new Error("Enter a name for the new folder");
    }
    const res = await fetch(SVS_BASE + "/user/folders", {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Requested-With": "XMLHttpRequest",
      },
      body: new URLSearchParams({ name }),
    });
    const j = await res.json();
    if (!j || !j.ok) throw new Error((j && j.error) || "Could not create the folder");
    folderInput.value = String(j.folder_id);
  } else {
    folderInput.value = val; // "" for Root, or a folder id
  }
}

let processing = false; // guards against double-submit while the pipeline runs

function onUpload() {
  if (!sourceFile || processing) return;
  processing = true;
  const button = $("upload-button");
  button.disabled = true; // Upload is unavailable until the pipeline finishes
  button.textContent = "Processing…";
  dropZone.classList.add("busy"); // freeze file re-selection while processing
  void (async () => {
    clientLog("info", "Upload pipeline started for " + sourceFile.name);
    try {
      await resolveFolder();
      $("progress-speed").classList.add("hidden");
      setProgress(0, "Preparing…");
      processedBlob = await processVideo();
      clientLog("info", "Processing done - final file " + fmtMB(processedBlob.size));
      // Re-probe the file that is actually uploaded (the transcode output, or the
      // source on direct pass) so the stored metadata reflects THAT file, not the
      // original source. A 2560x1440 source transcoded to 1080p must be stored as
      // 1920x1080, and a clip's duration differs from the source's.
      outputInfo = await probeVideo(processedBlob).catch(() => null);
      setProgress(0, "Extracting cover…");
      if (!coverBlob) {
        coverBlob = await extractCover();
        clientLog("info", "Cover extracted (" + Math.round(coverBlob.size / 1024) + " KB)");
      } else {
        clientLog("info", "Using user-provided cover");
      }
      setProgress(0, "Uploading…");
      if (processedBlob.size <= SEGMENT_THRESHOLD) {
        await singleUpload();
      } else {
        await segmentedUpload();
      }
      clientLog("info", "Upload complete - showing success screen");
      showSuccess(); // overlay + 3 s countdown to My Videos (§13.18)
    } catch (err) {
      cancelActiveTranscode(); // abort any in-flight conversion, reset its state
      const msg = err && err.message ? err.message : String(err);
      clientLog("error", "Upload pipeline failed: " + msg);
      progressStatus.textContent = "Error: " + msg;
      $("progress-speed").classList.add("hidden");
      processing = false;
      button.disabled = false;
      button.textContent = "Upload";
      dropZone.classList.remove("busy");
    }
  })();
}

// Success screen (§13.18): the server already has the file and the Drive upload
// now runs in the background. Navigate to My Videos after 3 s (or immediately
// via the button) — that page shows the live upload progress bar and a retry
// button if the Drive upload fails.
let successTimer = null;

function showSuccess() {
  $("success-overlay").classList.remove("hidden");
  const countdown = $("success-countdown");
  let n = 3;
  countdown.textContent = String(n);
  successTimer = setInterval(() => {
    n -= 1;
    if (n <= 0) {
      clearInterval(successTimer);
      successTimer = null;
      window.location.href = SVS_BASE + "/user";
      return;
    }
    countdown.textContent = String(n);
  }, 1000);
  $("success-go").addEventListener("click", () => {
    if (successTimer) clearInterval(successTimer);
    successTimer = null;
    window.location.href = SVS_BASE + "/user";
  });
}

// ---------------------------------------------------------------------------
// Probe + processing
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// mediabunny (WebCodecs) helpers — primary transcode / probe / cover path.
// If mediabunny is unavailable, processVideo falls back to ffmpeg.wasm, then
// to a hard error (never a silent passthrough). See processVideo.
// ---------------------------------------------------------------------------

// Cache the dynamic import so the ~683 KB bundle is fetched at most once. A
// failed load is cleared (mediabunny = null) so the next call can retry.
function loadMediabunny() {
  if (!mediabunny) {
    mediabunny = import(MEDIABUNNY_BUNDLE);
  }
  return mediabunny; // caller awaits the cached Promise / module namespace
}

// WebCodecs needs a secure context (HTTPS or localhost) AND at least one
// priority codec the browser can encode. Returns null when mediabunny can run,
// otherwise a human-readable reason: the original file then passes through,
// and that reason is surfaced in the UI so the fallback is never silent.
// Never throws.
async function mediabunnyUnavailableReason() {
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
    // Input validates `formats` with `instanceof InputFormat`, so it needs
    // *instances*, not the constructors. The bundle exports ALL_FORMATS, a
    // ready-made array of all 11 instantiated formats; fall back to building
    // them manually if a future bundle drops that export.
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
function cancelActiveTranscode() {
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
function probeVideo(file) {
  clientLog("info", "Probing via mediabunny (HTML5 <video> fallback if unavailable)...");
  return probeWithMediabunny(file).catch((e) => {
    clientLog("warn", "mediabunny probe failed (" + e + "), using HTML5 <video>.");
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
        fps: 30, // estimated; browser <video> exposes no fps
        bitrateKbps: duration > 0 ? Math.round(file.size / duration / 1000) : 0,
      };
      URL.revokeObjectURL(url);
      resolve(info);
    };
    v.onloadedmetadata = finish;
    v.onerror = finish;
  });
}

function clipRange() {
  const start = parseTime($("clip-start").value);
  const end = parseTime($("clip-end").value);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return null;
  // A clip spanning (essentially) the whole video is the same as "no clip" —
  // it must NOT force a transcode, or the direct-pass path (§req. 7) is lost.
  // The 1s tolerance absorbs the whole-second rounding in formatTime/parseTime.
  if (timelineDuration > 0 && start < 1 && end >= timelineDuration - 1) return null;
  return { start, end };
}

function needsTranscode() {
  if (clipRange()) return true;
  if (!probeInfo) return true; // unknown caps -> transcode to be safe
  if (probeInfo.height > MAX_HEIGHT) return true;
  if (probeInfo.fps > MAX_FPS) return true;
  if (probeInfo.bitrateKbps > MAX_BITRATE_KBPS) return true;
  return false;
}

async function processVideo() {
  // A fresh run starts with no transcode warning (a previous failure must not
  // leak into this one).
  $("transcode-warning").classList.add("hidden");
  $("transcode-warning").classList.remove("upload-warning--error");
  // Direct pass is legitimate ONLY when there is no clip and every cap is
  // already met (project_requirements.md decision table). In that case the
  // original file is compliant and is uploaded unchanged.
  if (!needsTranscode()) {
    clientLog(
      "info",
      "Direct pass - no clip and all caps met (height<=1080, fps<=60, bitrate<=5000 kbps); uploading the original file unchanged."
    );
    return sourceFile; // direct pass (compliant original)
  }
  const clip = clipRange();

  // Shared caps: cap the height only when it is known to exceed 1080p (never
  // upscale), cap fps only when over 60, and cap the bitrate at the admin
  // limit (preserve lower values). When the probe failed (probeInfo null)
  // fall back to the full caps so the 1080p / 5000 kb/s limits are respected.
  const capHeight = probeInfo ? (probeInfo.height > MAX_HEIGHT ? MAX_HEIGHT : 0) : MAX_HEIGHT;
  const capFps = probeInfo && probeInfo.fps > MAX_FPS ? MAX_FPS : 0;
  const bitrateKbps = Math.min(
    probeInfo ? probeInfo.bitrateKbps || MAX_BITRATE_KBPS : MAX_BITRATE_KBPS,
    MAX_BITRATE_KBPS
  );
  clientLog(
    "info",
    "Transcode required (clip=" +
      (clip ? clip.start + "-" + clip.end + "s" : "none") +
      "; caps: height<=1080, fps<=60, bitrate<=" + MAX_BITRATE_KBPS + " kbps)."
  );

  // Stage 1 - mediabunny (WebCodecs, hardware accelerated), AV1 then H.264.
  // `mediabunnyUnavailableReason()` returns null when it can run, otherwise
  // the reason we must fall back to ffmpeg.wasm.
  const reason = await mediabunnyUnavailableReason();
  if (reason === null) {
    const mb = await loadMediabunny(); // cached - same module namespace
    for (const codec of WEBCODECS_CODEC_PRIORITY) {
      if (!(await mb.canEncode(codec).catch(() => false))) {
        clientLog("warn", "mediabunny: browser cannot encode " + codec + ", skipping");
        continue;
      }
      cancelActiveTranscode(); // drop any stale signal before a fresh attempt
      activeTranscodeController = new AbortController();
      try {
        clientLog("info", "Transcoding with mediabunny (codec=" + codec + ").");
        const blob = await runMediabunny(sourceFile, {
          codec,
          bitrateKbps,
          capHeight,
          capFps,
          trimStart: clip ? clip.start : undefined,
          trimEnd: clip ? clip.end : undefined,
        });
        clientLog("info", "mediabunny done - output " + fmtMB(blob.size));
        return blob;
      } catch (err) {
        // A cancel (e.g. page unload) is not a codec failure - rethrow so the
        // upload aborts instead of falling back to the next engine.
        if (err && err.name === "ConversionCanceledError") throw err;
        clientLog("warn", "mediabunny codec " + codec + " failed: " + err + " - trying next");
      }
    }
    clientLog("warn", "mediabunny loaded but every codec attempt failed - falling back to ffmpeg.wasm");
  } else {
    clientLog("warn", "mediabunny unavailable: " + reason + " - falling back to ffmpeg.wasm");
  }

  // Stage 2 - ffmpeg.wasm (CPU). Works in any context (no secure-context /
  // WebCodecs requirement). AV1 (libsvtav1) then H.264 (libx264); audio is
  // copied first and re-encoded to AAC if the copy is not MP4-compatible.
  const ffmpegAttempts = [
    { encoder: "libsvtav1", audio: "copy" },
    { encoder: "libsvtav1", audio: "aac" },
    { encoder: "libx264", audio: "copy" },
    { encoder: "libx264", audio: "aac" },
  ];
  for (const attempt of ffmpegAttempts) {
    try {
      clientLog(
        "info",
        "Transcoding with ffmpeg.wasm (encoder=" + attempt.encoder + ", audio=" + attempt.audio + ")."
      );
      const blob = await runFfmpeg({
        encoder: attempt.encoder,
        audio: attempt.audio,
        bitrateKbps,
        capHeight,
        capFps,
        trimStart: clip ? clip.start : undefined,
        trimEnd: clip ? clip.end : undefined,
      });
      clientLog("info", "ffmpeg.wasm done - output " + fmtMB(blob.size));
      return blob;
    } catch (err) {
      clientLog(
        "warn",
        "ffmpeg.wasm " + attempt.encoder + "/" + attempt.audio + " failed: " + err + " - trying next"
      );
    }
  }

  // Stage 3 - hard stop. Per the requirements the original file may only be
  // uploaded when it is already compliant (the direct-pass case above). If a
  // required transcode could not run, BLOCK the upload rather than pass the
  // non-compliant original through.
  const why =
    reason !== null
      ? "mediabunny unavailable (" + reason + ") and ffmpeg.wasm failed for every encoder"
      : "mediabunny and ffmpeg.wasm both failed for every codec/encoder";
  throw failTranscode(why);
}

// A non-compliant file whose transcode failed is a HARD error - the upload is
// aborted (never silently passed through). Returns an Error to be thrown.
function failTranscode(why) {
  const message =
    "Transcode failed - the file was NOT uploaded (no silent passthrough). Reason: " + why;
  clientLog("error", message);
  const warn = $("transcode-warning");
  warn.textContent = message;
  warn.classList.add("upload-warning--error");
  warn.classList.remove("hidden");
  setProgress(0, "Transcode failed");
  return new Error(message);
}

// ffmpeg.wasm (CPU) fallback: streams the source through WORKERFS (so the
// whole file is never held as one ArrayBuffer), re-encodes with the given
// encoder, and returns the output MP4 as a Blob. `opts` mirrors runMediabunny.
const WORKERFS_INPUT_PATH = "/in/input.mp4";
async function runFfmpeg(opts) {
  const ffmpeg = new FFmpeg();
  const onLog = (e) => {
    const msg = e && e.message ? e.message : String(e);
    if (msg && msg.trim()) clientLog("debug", "[ffmpeg] " + msg.trim());
  };
  const onProgress = (p) => {
    // @ffmpeg/core@0.12 posts { progress, time } (both milliseconds).
    const ratio = p && p.time > 0 ? p.progress / p.time : 0;
    if (Number.isFinite(ratio)) setProgress(Math.min(1, Math.max(0, ratio)), "Transcoding...");
  };
  ffmpeg.on("log", onLog);
  ffmpeg.on("progress", onProgress);
  try {
    await ffmpeg.load({
      coreURL: SVS_BASE + "/static/js/ffmpeg/ffmpeg-core.js",
      wasmURL: SVS_BASE + "/static/js/ffmpeg/ffmpeg-core.wasm",
    });
    await ffmpeg.createDir("/in");
    // Fixed, safe node name for the streamed source (avoids odd characters).
    const input = new File([sourceFile], "input.mp4", { type: sourceFile.type });
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
    if (opts.audio === "aac") args.push("-c:a", "aac", "-b:a", "128k");
    else args.push("-c:a", "copy");
    args.push("output.mp4");
    clientLog("info", "[ffmpeg] exec: " + JSON.stringify(args));

    // exec() returns the exit code (it does NOT throw on non-zero) - §13.5.
    const code = await ffmpeg.exec(args);
    if (code !== 0) {
      throw new Error("ffmpeg exited with code " + code + " for " + JSON.stringify(args));
    }
    // Free the input from the WASM heap before reading the output back
    // (peak heap = max(input, output), not their sum) - §13.6.
    try {
      await ffmpeg.deleteFile(WORKERFS_INPUT_PATH);
    } catch {
      /* best-effort */
    }
    const data = await ffmpeg.readFile("output.mp4");
    if (!data || !data.length) throw new Error("ffmpeg produced no output");
    return new Blob([data], { type: "video/mp4" });
  } finally {
    // Release the whole WASM heap + WORKERFS mount after every attempt.
    ffmpeg.off("log", onLog);
    ffmpeg.off("progress", onProgress);
    try {
      ffmpeg.terminate();
    } catch {
      /* best-effort */
    }
  }
}

async function runMediabunny(file, opts) {
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
    quality: new mb.Quality({ bitrate: opts.bitrateKbps * 1000 }), // bitrate is bps
    forceTranscode: true, // re-encode the video (audio stays on copy)
  };
  // Caps are applied only when needed (a 0 value means "keep original").
  // When only height is capped, mediabunny auto-computes the width to preserve
  // the source aspect ratio (same effect as scale=-2:min(1080,ih)).
  if (opts.capHeight) videoOpts.height = opts.capHeight;
  if (opts.capFps) videoOpts.frameRate = opts.capFps;

  const conversionOpts = {
    input,
    output,
    tracks: "primary", // primary video + audio tracks only
    video: videoOpts,
    // audio omitted = copy (no re-encode); AAC re-encode is deferred.
  };
  if (typeof opts.trimStart === "number") {
    conversionOpts.trim = { start: opts.trimStart };
  }
  if (typeof opts.trimEnd === "number") {
    conversionOpts.trim = Object.assign({}, conversionOpts.trim, { end: opts.trimEnd });
  }

  const conversion = await mb.Conversion.init(conversionOpts); // static init()
  activeConversion = conversion;

  // Validate BEFORE execute(): an invalid output config (e.g. a copied audio
  // codec the MP4 muxer rejects) would make execute() throw before producing
  // anything — surface the exact reason here instead.
  if (!conversion.isValid) {
    clientLog(
      "warn",
      "mediabunny conversion invalid for codec " + opts.codec +
        "; discardedTracks: " + JSON.stringify(conversion.discardedTracks || [])
    );
    throw new Error("mediabunny conversion is invalid for codec " + opts.codec);
  }
  if (conversion.discardedTracks && conversion.discardedTracks.length > 0) {
    // Expected for non-primary tracks (e.g. extra audio), but keep it visible.
    clientLog(
      "warn",
      "mediabunny discarded tracks: " + JSON.stringify(conversion.discardedTracks)
    );
  }

  // onProgress MUST be assigned before execute() or no progress events fire.
  conversion.onProgress = (progress) => {
    setProgress(progress, "Transcoding…");
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

// Extract a cover: mediabunny (WebCodecs decode) first, HTML5 <video> seek
// fallback. Returns a JPEG Blob (shape unchanged — stored as coverBlob).
function extractCover() {
  clientLog("info", "Extracting cover (mediabunny decode, HTML5 fallback)...");
  return extractCoverWithMediabunny().catch((e) => {
    clientLog("warn", "mediabunny cover failed (" + e + "), using HTML5 <video>.");
    return extractCoverWithHtml5();
  });
}

async function extractCoverWithMediabunny() {
  const mb = await loadMediabunny();
  if (!mb) throw new Error("mediabunny unavailable");
  const input = new mb.Input({
    formats: getFormats(mb),
    source: new mb.BlobSource(sourceFile),
  });
  let sample = null;
  try {
    const vt = await input.getPrimaryVideoTrack();
    if (!vt) throw new Error("no video track");
    const duration = probeInfo ? probeInfo.duration : 0;
    const time = Math.min(60, duration); // same 60s mark as the HTML5 path
    // VideoSampleSink is stateless (no close/cancel method) — only the sample
    // must be released.
    sample = await new mb.VideoSampleSink(vt).getSample(time); // last frame at or before `time`
    if (!sample) throw new Error("no sample");
    const canvas = document.createElement("canvas");
    canvas.width = sample.displayWidth;
    canvas.height = sample.displayHeight;
    const ctx = canvas.getContext("2d");
    sample.drawWithFit(ctx, { fit: "contain" }); // aspect-fit, rotation applied
    return await new Promise((res, rej) =>
      canvas.toBlob((blob) => (blob ? res(blob) : rej(new Error("cover failed"))), "image/jpeg", 0.85)
    );
  } finally {
    if (sample) sample.close(); // must be released manually
    input.dispose(); // must be released manually
  }
}

function extractCoverWithHtml5() {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(sourceFile);
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

// ---------------------------------------------------------------------------
// Preview + clip (Set Start / Set End buttons)
// ---------------------------------------------------------------------------

// The clip range lives in the hidden #clip-start / #clip-end inputs; clipRange()
// reads them. These helpers keep the on-screen time fields in sync with the
// hidden inputs (and push manual edits back into them).
function currentClipStart() {
  const t = parseTime($("clip-start").value);
  return Number.isFinite(t) ? t : 0;
}

function currentClipEnd() {
  const t = parseTime($("clip-end").value);
  return Number.isFinite(t) && t > 0 ? t : timelineDuration || 0;
}

// Reflect the hidden clip inputs in the editable time fields next to the two
// buttons. Programmatic .value writes do not fire "change", so this is safe.
function updateClipLabels() {
  $("clip-start-input").value = formatTime(currentClipStart());
  $("clip-end-input").value = formatTime(currentClipEnd());
}

// Commit a manually edited time field into its hidden input, then re-sync both
// fields. A valid entry is normalized (e.g. "90" -> "1:30"); an invalid entry
// is reverted to the last valid value.
function onTimeInputChanged(fieldId, hiddenId) {
  const t = parseTime($(fieldId).value);
  if (Number.isFinite(t) && t >= 0) $(hiddenId).value = formatTime(t);
  updateClipLabels();
}

// Set Start / Set End capture the preview's current time when pressed. These
// buttons live for the whole page, so the listeners attach exactly once at
// module scope (re-selecting a file only re-points the preview src).
$("set-start-btn").addEventListener("click", () => {
  $("clip-start").value = formatTime(previewVideo.currentTime || 0);
  updateClipLabels();
});
$("set-end-btn").addEventListener("click", () => {
  $("clip-end").value = formatTime(previewVideo.currentTime || 0);
  updateClipLabels();
});
// The time fields are editable: committing a manual edit updates the hidden
// source of truth that clipRange() reads.
$("clip-start-input").addEventListener("change", () => onTimeInputChanged("clip-start-input", "clip-start"));
$("clip-end-input").addEventListener("change", () => onTimeInputChanged("clip-end-input", "clip-end"));
// Stop the preview at the set end so the user can confirm the cut point.
previewVideo.addEventListener("timeupdate", () => {
  const end = currentClipEnd();
  if (end > 0 && previewVideo.currentTime >= end) previewVideo.pause();
});

// Point the preview <video> at a newly selected file (and its object URL).
function setupTimeline(file) {
  if (videoUrl) URL.revokeObjectURL(videoUrl);
  videoUrl = URL.createObjectURL(file);
  previewVideo.src = videoUrl;
  // No forced mute: the user can play the preview with sound (§13.11). The
  // hidden probe/cover <video> elements stay muted.
  updateClipLabels();
}

// ---------------------------------------------------------------------------
// Upload (single or segmented)
// ---------------------------------------------------------------------------

function addMeta(fd) {
  // Prefer the re-probed output (reflects the transcoded/clipped file); fall
  // back to the source probe only if the output re-probe was skipped/failed.
  const info = outputInfo || probeInfo;
  if (info) {
    fd.append("duration", String(info.duration || ""));
    fd.append("resolution", info.width ? info.width + "x" + info.height : "");
    fd.append("bitrate", String(info.bitrateKbps || ""));
    fd.append("fps", String(info.fps || ""));
  }
}

// Human-readable megabytes (one decimal).
function fmtMB(bytes) {
  return (bytes / 1024 / 1024).toFixed(1) + " MB";
}

// POST via XMLHttpRequest so we get real upload progress (fetch exposes none).
// `onProgress(loadedBytes)` fires as bytes flow; resolves with the parsed JSON
// body when the server reports ok, otherwise rejects with an Error.
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
// bytes already sent before this request (segmented uploads) and `total` is
// the whole-file size, so the bar reflects overall progress. Speed is the
// rolling MB/s between throttled ticks; DOM writes are capped at ~8/s.
function makeProgressCb(offset, total) {
  let lastBytes = 0;
  let lastTime = 0;
  let lastRender = 0;
  const speedEl = $("progress-speed");
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
    progressFill.classList.remove("indeterminate");
    progressFill.style.width = pct + "%";
    speedEl.classList.remove("hidden");
    speedEl.textContent =
      pct + "%  ·  " + fmtMB(overall) + " / " + fmtMB(total) +
      "  ·  " + speed.toFixed(2) + " MB/s";
  };
}

async function singleUpload() {
  const fd = new FormData();
  fd.append("video", processedBlob, titleInput.value + ".mp4");
  if (coverBlob) fd.append("cover", coverBlob, "cover.jpg");
  fd.append("title", titleInput.value);
  fd.append("description", descriptionInput.value || "");
  fd.append("folder_id", folderInput.value || "");
  addMeta(fd);
  clientLog("info", "Uploading (single) " + fmtMB(processedBlob.size));
  setProgress(0, "Uploading…");
  await xhrPost(SVS_BASE + "/upload/submit", fd, makeProgressCb(0, processedBlob.size));
  clientLog("info", "Upload accepted by server");
  $("progress-speed").classList.add("hidden");
  setProgress(1, "Done");
}

async function segmentedUpload() {
  const totalParts = Math.ceil(processedBlob.size / SEGMENT_SIZE);
  clientLog("info", "Uploading (segmented) " + totalParts + " part(s) of " + fmtMB(processedBlob.size));
  const startRes = await fetch(SVS_BASE + "/upload/seg/start", {
    method: "POST",
    headers: { "X-Requested-With": "XMLHttpRequest" },
    body: new URLSearchParams({
      title: titleInput.value,
      total_parts: String(totalParts),
      expected_size: String(processedBlob.size),
      folder_id: folderInput.value || "",
    }),
  });
  const start = await startRes.json();
  if (!start.ok) throw new Error(start.error || "seg_start failed");
  const token = start.token;
  const totalBytes = processedBlob.size;

  for (let i = 0; i < totalParts; i++) {
    const part = processedBlob.slice(i * SEGMENT_SIZE, (i + 1) * SEGMENT_SIZE);
    const fd = new FormData();
    fd.append("token", token);
    fd.append("part_index", String(i));
    fd.append("part", part, "part_" + i + ".bin");
    setProgress(i / totalParts, "Uploading segment " + (i + 1) + "/" + totalParts + "…");
    await xhrPost(SVS_BASE + "/upload/seg/part", fd, makeProgressCb(i * SEGMENT_SIZE, totalBytes));
  }

  const fd = new FormData();
  fd.append("token", token);
  if (coverBlob) fd.append("cover", coverBlob, "cover.jpg");
  fd.append("description", descriptionInput.value || "");
  addMeta(fd);
  setProgress(1, "Finalizing…");
  await xhrPost(SVS_BASE + "/upload/seg/finish", fd, () => {});
  $("progress-speed").classList.add("hidden");
  setProgress(1, "Done");
}

// ---------------------------------------------------------------------------
// UI helpers
// ---------------------------------------------------------------------------

function setProgress(ratio, status) {
  progressPanel.classList.remove("hidden");
  const clamped = Math.min(1, Math.max(0, ratio));
  // A 0 ratio means "no measurable ratio yet" (Preparing / Extracting cover /
  // Uploading) — show the indeterminate animated bar so the user knows it is
  // alive; during transcoding the real 0→100% fills the bar (§13.11). The
  // inline width is cleared in that case so the CSS slide animation shows.
  if (clamped === 0) {
    progressFill.classList.add("indeterminate");
    progressFill.style.width = "";
  } else {
    progressFill.classList.remove("indeterminate");
    progressFill.style.width = Math.round(clamped * 100) + "%";
  }
  if (status) progressStatus.textContent = status;
}

function formatTime(sec) {
  if (!Number.isFinite(sec)) return "00:00";
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
}

function parseTime(str) {
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