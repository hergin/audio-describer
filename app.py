import json
import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

from build_audio_described_video import (
    UserFacingError,
    build_video,
    seconds_to_timestamp,
)


BASE_DIR = Path(__file__).resolve().parent
GUI_WORKSPACE = BASE_DIR / "_gui_workspace"
UPLOAD_DIR = GUI_WORKSPACE / "input"
OUTPUT_DIR = GUI_WORKSPACE / "output"
CUES_PATH = GUI_WORKSPACE / "cues.json"
VTT_PATH = GUI_WORKSPACE / "cues.vtt"


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024 * 1024


@dataclass
class AppState:
    video_path: Path | None = None
    video_name: str | None = None
    output_path: Path | None = None
    job_id: str | None = None
    status: str = "idle"
    message: str = "Ready"
    logs: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


state = AppState()


def ensure_workspace() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def read_cues() -> list[dict]:
    if not CUES_PATH.exists():
        return []
    return json.loads(CUES_PATH.read_text(encoding="utf-8"))


def write_cues(cues: list[dict]) -> None:
    ensure_workspace()
    cues = sorted(cues, key=lambda cue: float(cue["start"]))
    CUES_PATH.write_text(json.dumps(cues, indent=2), encoding="utf-8")


def write_vtt(cues: list[dict], path: Path) -> None:
    lines = ["WEBVTT", ""]
    for index, cue in enumerate(sorted(cues, key=lambda item: float(item["start"])), start=1):
        start = float(cue["start"])
        end = float(cue.get("end") or start + 0.001)
        if end <= start:
            end = start + 0.001
        text = str(cue["text"]).strip()
        lines.extend(
            [
                str(index),
                f"{seconds_to_timestamp(start)} --> {seconds_to_timestamp(end)}",
                text,
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def public_state() -> dict:
    with state.lock:
        return {
            "videoName": state.video_name,
            "hasVideo": state.video_path is not None and state.video_path.exists(),
            "hasOutput": state.output_path is not None and state.output_path.exists(),
            "jobId": state.job_id,
            "status": state.status,
            "message": state.message,
            "logs": state.logs[-50:],
        }


def set_job_status(status: str, message: str) -> None:
    with state.lock:
        state.status = status
        state.message = message
        state.logs.append(message)


@app.get("/")
def index():
    ensure_workspace()
    return render_template("index.html")


@app.get("/api/state")
def api_state():
    return jsonify(public_state())


@app.post("/api/upload")
def api_upload():
    ensure_workspace()
    file = request.files.get("video")
    if file is None or not file.filename:
        return jsonify({"error": "Choose a video file first."}), 400

    filename = secure_filename(file.filename)
    if not filename:
        filename = "input-video.mp4"
    video_path = UPLOAD_DIR / filename
    file.save(video_path)

    with state.lock:
        state.video_path = video_path
        state.video_name = filename
        state.output_path = None
        state.status = "idle"
        state.message = "Video loaded"
        state.logs = ["Video loaded"]

    write_cues([])
    return jsonify(public_state())


@app.get("/api/video")
def api_video():
    with state.lock:
        video_path = state.video_path
    if video_path is None or not video_path.exists():
        return jsonify({"error": "No video loaded."}), 404
    return send_file(video_path, conditional=True)


@app.get("/api/cues")
def api_get_cues():
    return jsonify({"cues": read_cues()})


@app.post("/api/cues")
def api_save_cues():
    payload = request.get_json(silent=True) or {}
    raw_cues = payload.get("cues", [])
    cues = []
    for index, cue in enumerate(raw_cues, start=1):
        text = str(cue.get("text", "")).strip()
        if not text:
            continue
        try:
            start = float(cue.get("start"))
        except (TypeError, ValueError):
            return jsonify({"error": f"Cue {index} has an invalid start time."}), 400
        if start < 0:
            return jsonify({"error": f"Cue {index} start time cannot be negative."}), 400
        cues.append(
            {
                "id": str(cue.get("id") or uuid4()),
                "start": round(start, 3),
                "end": round(float(cue.get("end") or start + 0.001), 3),
                "text": text,
            }
        )
    write_cues(cues)
    return jsonify({"cues": read_cues()})


@app.get("/api/export-vtt")
def api_export_vtt():
    cues = read_cues()
    if not cues:
        return jsonify({"error": "No cues to export."}), 400
    write_vtt(cues, VTT_PATH)
    return send_file(VTT_PATH, as_attachment=True, download_name="audio-description-cues.vtt")


@app.post("/api/render")
def api_render():
    with state.lock:
        if state.status == "running":
            return jsonify({"error": "A render job is already running."}), 409
        video_path = state.video_path

    if video_path is None or not video_path.exists():
        return jsonify({"error": "Load a video before rendering."}), 400

    cues = read_cues()
    if not cues:
        return jsonify({"error": "Add at least one audio description cue."}), 400

    write_vtt(cues, VTT_PATH)
    output_path = OUTPUT_DIR / f"{video_path.stem}-audio-described.mp4"
    job_id = str(uuid4())

    with state.lock:
        state.job_id = job_id
        state.status = "running"
        state.message = "Render started"
        state.output_path = output_path
        state.logs = ["Render started"]

    thread = threading.Thread(
        target=render_worker,
        args=(job_id, video_path, VTT_PATH, output_path),
        daemon=True,
    )
    thread.start()
    return jsonify(public_state())


def render_worker(job_id: str, video_path: Path, vtt_path: Path, output_path: Path) -> None:
    try:
        set_job_status("running", "Generating TTS and rendering video")
        build_video(
            video_path=video_path,
            vtt_path=vtt_path,
            output_path=output_path,
            work_dir=GUI_WORKSPACE / f"render_{job_id}",
            bitrate_kbps=None,
            keep_temp=False,
        )
        set_job_status("done", "Render complete")
    except UserFacingError as exc:
        set_job_status("failed", str(exc))
    except Exception:
        set_job_status("failed", traceback.format_exc())


@app.get("/api/download")
def api_download():
    with state.lock:
        output_path = state.output_path
    if output_path is None or not output_path.exists():
        return jsonify({"error": "No completed output is available."}), 404
    return send_file(output_path, as_attachment=True)


@app.get("/api/output-video")
def api_output_video():
    with state.lock:
        output_path = state.output_path
    if output_path is None or not output_path.exists():
        return jsonify({"error": "No completed output is available."}), 404
    return send_file(output_path, conditional=True)


if __name__ == "__main__":
    ensure_workspace()
    app.run(host="127.0.0.1", port=8000, debug=False)
