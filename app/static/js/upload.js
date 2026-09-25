// upload.js — main upload page: multi-file queue, auto-transcode, no clip.
//
// Each queued video is probed, transcoded (1080p / admin bitrate, CBR) and
// uploaded in order. The clip/editor functionality lives on the edit sub-page
// (/upload/edit) which reuses the same shared pipeline (pipeline.js).

import {
  probeVideo,
  processVideo,
  extractCover,
  uploadVideo,
  createLogger,
  makeSetProgress,
  cancelActiveTranscode,
  fmtMB,
} from "./pipeline.js";

const SVS_BASE = (typeof window !== "undefined" && window.SVS_BASE) ? window.SVS_BASE : "";
const $ = (id) => document.getElementById(id);

const dropZone = $("drop-zone");
const fileInput = $("file-input");
const queueList = $("queue-list");
const queueCount = $("queue-count");
const fileLabel = $("file-label");
const maxFpsInput = $("max-fps-input");
const folderSelect = $("folder-select");
const folderNewName = $("folder-new-name");
const folderInput = $("folder-input");
const startBtn = $("start-button");
const progressPanel = $("progress-panel");
const progressFill = $("progress-fill");
const progressStatus = $("progress-status");
const progressSpeed = $("progress-speed");
const logConsole = $("log-console");

// --- Client logging (console + on-page panel + POST to the server) ---
function appendLine(level, line) {
  if (!logConsole) return;
  const row = document.createElement("div");
  row.className = "upload-log__line upload-log__line--" + level;
  row.textContent = line;
  logConsole.appendChild(row);
  while (logConsole.childElementCount > 400) logConsole.removeChild(logConsole.firstElementChild);
  logConsole.scrollTop = logConsole.scrollHeight;
}
const log = createLogger(SVS_BASE + "/upload/log", appendLine);

// Page context handed to the shared pipeline: the logger plus the progress
// elements for the file currently being processed.
const ctx = {
  log,
  progressPanel,
  progressFill,
  progressStatus,
  progressSpeed,
};
ctx.setProgress = makeSetProgress(ctx);

// Per-upload FPS cap the user picks in the form (clamped to 60). Read live so it
// can be changed any time before the queue starts.
function getMaxFps() {
  const n = maxFpsInput ? parseInt(maxFpsInput.value, 10) : NaN;
  if (!Number.isFinite(n) || n <= 0) return 60;
  return Math.min(60, n);
}

// --- Queue state ---
let queue = []; // { file, title, sizeMB, status, statusEl }
let processing = false;

function updateCount() {
  queueCount.textContent = queue.length ? "(" + queue.length + ")" : "";
  startBtn.disabled = processing || queue.length === 0;
}

function renderQueueItem(item) {
  const row = document.createElement("div");
  row.className = "upload-queue__item";
  const name = document.createElement("span");
  name.className = "upload-queue__name";
  name.textContent = item.title;
  const meta = document.createElement("span");
  meta.className = "upload-queue__meta muted";
  meta.textContent = item.sizeMB + " MB";
  const status = document.createElement("span");
  status.className = "upload-queue__status muted";
  status.textContent = item.status;
  row.appendChild(name);
  row.appendChild(meta);
  row.appendChild(status);
  queueList.appendChild(row);
  item.statusEl = status;
  item.rowEl = row;
}

function setItemStatus(item, status, isError) {
  item.status = status;
  if (item.statusEl) {
    item.statusEl.textContent = status;
    item.statusEl.classList.toggle("upload-queue__status--error", !!isError);
  }
}

// Add files (from a picker or drag-and-drop) to the queue. Non-video files are
// ignored. Re-selecting the same files is fine — each pick is appended.
function addFiles(files) {
  let added = 0;
  for (const f of files) {
    if (!f.type.startsWith("video/")) continue;
    const item = {
      file: f,
      title: f.name.replace(/\.[^.]+$/, "") || "video",
      sizeMB: (f.size / 1024 / 1024).toFixed(1),
      status: "Queued",
      statusEl: null,
      rowEl: null,
    };
    queue.push(item);
    renderQueueItem(item);
    added++;
    log("info", "Queued: " + f.name + " (" + item.sizeMB + " MB)");
  }
  if (added) fileLabel.textContent = files.length + " file(s) selected";
  updateCount();
}

// Resolve the folder selection (applies to the whole queue) into the hidden
// `folder-input`. "New folder…" is created server-side first (JSON).
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

// Run the full pipeline for one queued file (no clip): probe -> transcode ->
// re-probe the output -> extract cover -> upload.
async function processOne(item) {
  setItemStatus(item, "Probing…");
  const probe = await probeVideo(item.file, ctx);

  setItemStatus(item, "Transcoding…");
  const { blob, info } = await processVideo(item.file, { probe, clip: null, maxFps: getMaxFps() }, ctx);

  // Re-probe the file actually uploaded (transcoded, or the source on direct
  // pass) so stored metadata reflects THAT file, not the original source.
  const outInfo = (await probeVideo(blob, ctx).catch(() => null)) || info;

  setItemStatus(item, "Extracting cover…");
  const cover = await extractCover(item.file, probe ? probe.duration : 0, ctx);

  setItemStatus(item, "Uploading…");
  await uploadVideo(
    blob,
    { title: item.title, description: "", folderId: folderInput.value || "", cover, info: outInfo },
    ctx
  );
  setItemStatus(item, "Done");
}

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

// Process the whole queue sequentially. A single file failing does not stop the
// rest — it is marked as an error and the queue moves on.
async function startQueue() {
  if (processing || !queue.length) return;
  processing = true;
  startBtn.disabled = true;
  startBtn.textContent = "Processing…";
  log("info", "Queue started (" + queue.length + " file(s))");
  try {
    await resolveFolder();
  } catch (err) {
    log("error", "Folder selection failed: " + (err.message || err));
    processing = false;
    startBtn.disabled = false;
    startBtn.textContent = "Upload queue";
    return;
  }
  for (const item of queue) {
    try {
      await processOne(item);
    } catch (err) {
      const msg = err && err.message ? err.message : String(err);
      setItemStatus(item, "Error: " + msg, true);
      log("error", item.title + " failed: " + msg);
    }
  }
  cancelActiveTranscode();
  processing = false;
  startBtn.textContent = "Upload queue";
  updateCount();
  log("info", "Queue complete");
  showSuccess();
}

// --- Event wiring ---
fileInput.addEventListener("change", () => addFiles(fileInput.files));
dropZone.addEventListener("click", () => fileInput.click());
dropZone.addEventListener("dragover", (e) => {
  e.preventDefault();
  dropZone.classList.add("drag-over");
});
dropZone.addEventListener("dragleave", () => dropZone.classList.remove("drag-over"));
dropZone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropZone.classList.remove("drag-over");
  addFiles(e.dataTransfer.files);
});
folderSelect.addEventListener("change", () => {
  folderNewName.classList.toggle("hidden", folderSelect.value !== "__new__");
});
startBtn.addEventListener("click", () => { void startQueue(); });
window.addEventListener("beforeunload", () => cancelActiveTranscode());

const logClearBtn = $("log-clear");
if (logClearBtn) logClearBtn.addEventListener("click", () => { logConsole.innerHTML = ""; });

updateCount();