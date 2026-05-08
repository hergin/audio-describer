const videoInput = document.querySelector("#videoInput");
const video = document.querySelector("#video");
const timeline = document.querySelector("#timeline");
const markers = document.querySelector("#markers");
const timeReadout = document.querySelector("#timeReadout");
const addCueButton = document.querySelector("#addCueButton");
const cueForm = document.querySelector("#cueForm");
const cueStart = document.querySelector("#cueStart");
const cueText = document.querySelector("#cueText");
const cueList = document.querySelector("#cueList");
const renderButton = document.querySelector("#renderButton");
const exportButton = document.querySelector("#exportButton");
const downloadLink = document.querySelector("#downloadLink");
const outputPanel = document.querySelector("#outputPanel");
const outputVideo = document.querySelector("#outputVideo");
const statusText = document.querySelector("#statusText");
const jobLog = document.querySelector("#jobLog");

let cues = [];
let editingCueId = null;
let pollTimer = null;
let videoClockTimer = null;

function formatTime(seconds) {
  const safeSeconds = Math.max(0, Number(seconds) || 0);
  const hours = Math.floor(safeSeconds / 3600);
  const minutes = Math.floor((safeSeconds % 3600) / 60);
  const remainder = safeSeconds - hours * 3600 - minutes * 60;
  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${remainder
    .toFixed(3)
    .padStart(6, "0")}`;
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || "Request failed");
  }
  return payload;
}

function setStatus(message) {
  statusText.textContent = message;
}

function updateTimeReadout() {
  const currentTime = formatTime(video.currentTime);
  timeReadout.textContent = `${currentTime} / ${formatTime(video.duration)}`;
  cueStart.textContent = currentTime;
  if (!Number.isNaN(video.duration)) {
    timeline.max = String(video.duration);
    timeline.value = String(video.currentTime);
  }
}

function startVideoClock() {
  if (videoClockTimer) return;
  videoClockTimer = window.setInterval(updateTimeReadout, 100);
}

function stopVideoClock() {
  if (!videoClockTimer) return;
  window.clearInterval(videoClockTimer);
  videoClockTimer = null;
  updateTimeReadout();
}

function renderMarkers() {
  markers.innerHTML = "";
  const duration = video.duration || 0;
  if (!duration) return;

  for (const cue of cues) {
    const marker = document.createElement("div");
    marker.className = "marker";
    marker.style.left = `${Math.min(100, Math.max(0, (cue.start / duration) * 100))}%`;
    markers.appendChild(marker);
  }
}

function renderCueList() {
  cueList.innerHTML = "";
  if (!cues.length) {
    const empty = document.createElement("p");
    empty.textContent = "No cues yet.";
    cueList.appendChild(empty);
    renderMarkers();
    return;
  }

  for (const cue of cues) {
    const item = document.createElement("article");
    item.className = "cue-item";

    const meta = document.createElement("div");
    meta.className = "cue-meta";

    const time = document.createElement("button");
    time.type = "button";
    time.className = "secondary";
    time.textContent = formatTime(cue.start);
    time.addEventListener("click", () => {
      video.currentTime = cue.start;
      video.pause();
    });

    const actions = document.createElement("div");
    actions.className = "cue-actions";

    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "secondary";
    edit.textContent = "Edit";
    edit.addEventListener("click", () => {
      editingCueId = cue.id;
      video.currentTime = cue.start;
      video.pause();
      cueText.value = cue.text;
      cueText.focus();
    });

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "secondary";
    remove.textContent = "Delete";
    remove.addEventListener("click", async () => {
      cues = cues.filter((itemCue) => itemCue.id !== cue.id);
      await saveCues();
    });

    actions.append(edit, remove);
    meta.append(time, actions);

    const text = document.createElement("div");
    text.className = "cue-text";
    text.textContent = cue.text;

    item.append(meta, text);
    cueList.appendChild(item);
  }

  renderMarkers();
}

async function loadCues() {
  const payload = await fetchJson("/api/cues");
  cues = payload.cues;
  renderCueList();
}

async function saveCues() {
  const payload = await fetchJson("/api/cues", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cues }),
  });
  cues = payload.cues;
  editingCueId = null;
  cueText.value = "";
  renderCueList();
}

async function refreshState() {
  const state = await fetchJson("/api/state");
  setStatus(state.message || "Ready");
  jobLog.textContent = (state.logs || []).join("\n");
  renderButton.disabled = state.status === "running";

  if (state.hasVideo && !video.src) {
    video.src = `/api/video?cache=${Date.now()}`;
  }

  if (state.hasOutput) {
    outputPanel.classList.remove("hidden");
    if (!outputVideo.src || state.status === "done") {
      outputVideo.src = `/api/output-video?cache=${Date.now()}`;
    }
  } else {
    outputPanel.classList.add("hidden");
    outputVideo.removeAttribute("src");
    outputVideo.load();
  }

  if (state.status === "running" && !pollTimer) {
    pollTimer = window.setInterval(refreshState, 2000);
  }

  if (state.status !== "running" && pollTimer) {
    window.clearInterval(pollTimer);
    pollTimer = null;
  }
}

videoInput.addEventListener("change", async () => {
  const file = videoInput.files[0];
  if (!file) return;

  const formData = new FormData();
  formData.append("video", file);
  setStatus("Uploading video");

  try {
    await fetchJson("/api/upload", {
      method: "POST",
      body: formData,
    });
    video.src = `/api/video?cache=${Date.now()}`;
    cues = [];
    renderCueList();
    await refreshState();
  } catch (error) {
    setStatus(error.message);
  }
});

video.addEventListener("loadedmetadata", () => {
  timeline.max = String(video.duration || 0);
  updateTimeReadout();
  renderMarkers();
});

video.addEventListener("timeupdate", updateTimeReadout);
video.addEventListener("seeking", updateTimeReadout);
video.addEventListener("seeked", updateTimeReadout);
video.addEventListener("play", startVideoClock);
video.addEventListener("pause", stopVideoClock);
video.addEventListener("ended", stopVideoClock);

timeline.addEventListener("input", () => {
  video.currentTime = Number(timeline.value);
  updateTimeReadout();
});

addCueButton.addEventListener("click", () => {
  updateTimeReadout();
  cueText.focus();
});

cueForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  updateTimeReadout();
  const start = Number(video.currentTime || 0);
  const text = cueText.value.trim();
  if (!text) {
    setStatus("Cue text is required");
    return;
  }

  if (editingCueId) {
    cues = cues.map((cue) =>
      cue.id === editingCueId ? { ...cue, start, end: start + 0.001, text } : cue
    );
  } else {
    cues.push({
      id: crypto.randomUUID(),
      start,
      end: start + 0.001,
      text,
    });
  }

  try {
    await saveCues();
    setStatus("Cue saved");
  } catch (error) {
    setStatus(error.message);
  }
});

renderButton.addEventListener("click", async () => {
  try {
    await saveCues();
    await fetchJson("/api/render", { method: "POST" });
    await refreshState();
  } catch (error) {
    setStatus(error.message);
  }
});

exportButton.addEventListener("click", () => {
  window.location.href = "/api/export-vtt";
});

refreshState()
  .then(loadCues)
  .catch((error) => setStatus(error.message));
