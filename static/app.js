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
const clearButton = document.querySelector("#clearButton");
const exportButton = document.querySelector("#exportButton");
const downloadLink = document.querySelector("#downloadLink");
const outputVideo = document.querySelector("#outputVideo");
const outputEmpty = document.querySelector("#outputEmpty");
const sourceTab = document.querySelector("#sourceTab");
const outputTab = document.querySelector("#outputTab");
const sourceView = document.querySelector("#sourceView");
const outputView = document.querySelector("#outputView");
const statusText = document.querySelector("#statusText");
const jobLog = document.querySelector("#jobLog");
const jobBadge = document.querySelector("#jobBadge");
const renderModeInputs = document.querySelectorAll("input[name='renderMode']");
const nearbySettings = document.querySelector("#nearbySettings");
const searchBefore = document.querySelector("#searchBefore");
const searchAfter = document.querySelector("#searchAfter");

let cues = [];
let editingCueId = null;
let pollTimer = null;
let videoClockTimer = null;
let loadedOutputJobId = null;

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

function setActiveView(viewName) {
  const showOutput = viewName === "output";
  sourceTab.classList.toggle("active", !showOutput);
  outputTab.classList.toggle("active", showOutput);
  sourceView.classList.toggle("active", !showOutput);
  outputView.classList.toggle("active", showOutput);
}

function updateNearbySettingsState() {
  const selectedMode = document.querySelector("input[name='renderMode']:checked").value;
  const isNearby = selectedMode === "nearby";
  nearbySettings.classList.toggle("hidden", !isNearby);
  searchBefore.disabled = !isNearby;
  searchAfter.disabled = !isNearby;
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
  jobBadge.textContent = state.status || "idle";
  jobBadge.className = state.status || "idle";
  renderButton.disabled = state.status === "running";
  clearButton.disabled = state.status === "running";
  renderModeInputs.forEach((input) => {
    input.disabled = state.status === "running";
  });
  updateNearbySettingsState();

  if (state.hasVideo && !video.src) {
    video.src = `/api/video?cache=${Date.now()}`;
  }

  if (!state.hasVideo && video.src) {
    video.pause();
    video.removeAttribute("src");
    video.load();
    timeline.value = "0";
    timeline.max = "0";
    updateTimeReadout();
  }

  if (state.status === "done" && state.hasOutput) {
    outputTab.disabled = false;
    outputEmpty.classList.add("hidden");
    outputVideo.classList.remove("hidden");
    downloadLink.classList.remove("hidden");
    if (loadedOutputJobId !== state.jobId) {
      outputVideo.src = `/api/output-video?cache=${Date.now()}`;
      loadedOutputJobId = state.jobId;
    }
    setActiveView("output");
  } else {
    outputTab.disabled = true;
    outputEmpty.classList.remove("hidden");
    outputVideo.classList.add("hidden");
    downloadLink.classList.add("hidden");
    if (outputVideo.src) {
      outputVideo.pause();
      outputVideo.removeAttribute("src");
      outputVideo.load();
    }
    loadedOutputJobId = null;
    setActiveView("source");
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
    setActiveView("source");
  } catch (error) {
    setStatus(error.message);
  }
});

sourceTab.addEventListener("click", () => setActiveView("source"));
outputTab.addEventListener("click", () => {
  if (!outputTab.disabled) setActiveView("output");
});

renderModeInputs.forEach((input) => {
  input.addEventListener("change", updateNearbySettingsState);
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
    const selectedMode = document.querySelector("input[name='renderMode']:checked").value;
    const renderPayload = { mode: selectedMode };
    if (selectedMode === "nearby") {
      renderPayload.searchBefore = Number(searchBefore.value);
      renderPayload.searchAfter = Number(searchAfter.value);
    }
    await fetchJson("/api/render", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(renderPayload),
    });
    setActiveView("source");
    await refreshState();
  } catch (error) {
    setStatus(error.message);
  }
});

clearButton.addEventListener("click", async () => {
  try {
    await fetchJson("/api/clear", { method: "POST" });
    cues = [];
    editingCueId = null;
    cueText.value = "";
    videoInput.value = "";
    renderCueList();
    setActiveView("source");
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
