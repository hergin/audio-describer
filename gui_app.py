import json
import shutil
import subprocess
import sys
import traceback
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSplitter,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from build_audio_described_video import UserFacingError, build_video, seconds_to_timestamp
from build_nearby_silence_ad_video import build_nearby_silence_ad_video
from build_smart_ad_video import build_smart_ad_video
from mix_ad_into_original_audio import build_mixed_ad_video

BASE_DIR = Path(__file__).resolve().parent
GUI_WORKSPACE = BASE_DIR / "_gui_workspace"
OUTPUT_DIR = GUI_WORKSPACE / "output"
CUES_PATH = GUI_WORKSPACE / "cues.json"
VTT_PATH = GUI_WORKSPACE / "cues.vtt"


def ensure_workspace():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def fmt(ms):
    s = max(0, ms / 1000.0)
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    r = s - h * 3600 - m * 60
    return f"{h:02d}:{m:02d}:{r:06.3f}"


def write_vtt(cues, path):
    lines = ["WEBVTT", ""]
    for i, c in enumerate(sorted(cues, key=lambda x: float(x["start"])), 1):
        start = float(c["start"])
        end = float(c.get("end") or start + 0.001)
        if end <= start:
            end = start + 0.001
        lines.extend([str(i), f"{seconds_to_timestamp(start)} --> {seconds_to_timestamp(end)}",
                       c["text"].strip(), ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def read_cues():
    if not CUES_PATH.exists():
        return []
    return json.loads(CUES_PATH.read_text(encoding="utf-8"))


def save_cues(cues):
    ensure_workspace()
    CUES_PATH.write_text(json.dumps(sorted(cues, key=lambda c: c["start"]), indent=2), encoding="utf-8")


class LogCapture:
    def __init__(self, sig):
        self._sig = sig
    def write(self, t):
        t = t.rstrip("\n")
        if t:
            self._sig.emit(t)
        return len(t)
    def flush(self):
        pass


class RenderWorker(QThread):
    status_changed = Signal(str, str)
    log_message = Signal(str)

    def __init__(self, mode, video_path, vtt_path, output_path, work_dir,
                 search_before=2.0, search_after=4.0):
        super().__init__()
        self.mode = mode
        self.video_path = video_path
        self.vtt_path = vtt_path
        self.output_path = output_path
        self.work_dir = work_dir
        self.search_before = search_before
        self.search_after = search_after
        self.final_status = "failed"

    def run(self):
        old = sys.stdout
        sys.stdout = LogCapture(self.log_message)
        try:
            self.status_changed.emit("running", f"{self.mode.title()} AD render started")
            if self.mode == "extended":
                build_video(self.video_path, self.vtt_path, self.output_path,
                            self.work_dir, bitrate_kbps=None, keep_temp=False)
            elif self.mode == "mixed":
                build_mixed_ad_video(self.video_path, self.vtt_path, self.output_path,
                                     self.work_dir, keep_temp=False)
            elif self.mode == "smart":
                build_smart_ad_video(self.video_path, self.vtt_path, self.output_path,
                                     self.work_dir, bitrate_kbps=None, keep_temp=False)
            elif self.mode == "nearby":
                build_nearby_silence_ad_video(
                    self.video_path, self.vtt_path, self.output_path,
                    self.work_dir, bitrate_kbps=None, keep_temp=False,
                    search_before=self.search_before, search_after=self.search_after)
            self.final_status = "done"
            self.status_changed.emit("done", "Render complete")
        except UserFacingError as e:
            self.final_status = "failed"
            self.status_changed.emit("failed", str(e))
        except Exception:
            self.final_status = "failed"
            self.status_changed.emit("failed", traceback.format_exc())
        finally:
            sys.stdout = old


class MarkerSlider(QSlider):
    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self._marks = []

    def set_marks(self, ms_list):
        self._marks = list(ms_list)
        self.update()

    def paintEvent(self, ev):
        super().paintEvent(ev)
        if not self._marks or self.maximum() == 0:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#f472b6"))
        margin = 7
        w = self.width() - 2 * margin
        for ms in self._marks:
            x = margin + (ms / self.maximum()) * w
            p.drawRoundedRect(int(x) - 1, 0, 3, self.height(), 1, 1)
        p.end()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Audio Description Builder")
        self.setMinimumSize(1100, 700)
        self.resize(1300, 800)

        self.cues = []
        self.editing_id = None
        self.video_path = None
        self.output_path = None
        self.worker = None

        ensure_workspace()

        root = QWidget()
        self.setCentralWidget(root)
        root_lay = QVBoxLayout(root)
        root_lay.setContentsMargins(0, 0, 0, 0)
        root_lay.setSpacing(0)

        # ── Top bar ──
        bar = QWidget()
        bar.setObjectName("topbar")
        bar_lay = QHBoxLayout(bar)
        bar_lay.setContentsMargins(24, 12, 24, 12)
        title = QLabel("Audio Description Builder")
        title.setObjectName("title")
        bar_lay.addWidget(title)
        bar_lay.addStretch()
        self.status = QLabel("Ready")
        self.status.setObjectName("status")
        bar_lay.addWidget(self.status)
        bar_lay.addSpacing(24)
        self.open_btn = self._btn("Open Video", "secondary")
        self.render_btn = self._btn("Render", "primary")
        self.locate_btn = self._btn("Locate Output", "secondary")
        self.locate_btn.setVisible(False)
        self.clear_btn = self._btn("Clear", "ghost")
        bar_lay.addWidget(self.open_btn)
        bar_lay.addWidget(self.render_btn)
        bar_lay.addWidget(self.locate_btn)
        bar_lay.addWidget(self.clear_btn)
        root_lay.addWidget(bar)

        # ── Body ──
        body = QSplitter(Qt.Horizontal)
        body.setObjectName("body")
        body.setHandleWidth(1)

        # Left: video
        left = QWidget()
        left.setObjectName("video-area")
        ll = QVBoxLayout(left)
        ll.setContentsMargins(24, 20, 12, 20)
        ll.setSpacing(10)

        self.view_stack = QStackedWidget()

        # Source view
        self.source_vid = QVideoWidget()
        self.source_vid.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.source_player = QMediaPlayer()
        self.source_audio = QAudioOutput()
        self.source_player.setVideoOutput(self.source_vid)
        self.source_player.setAudioOutput(self.source_audio)
        self.source_player.errorOccurred.connect(self._on_player_error)
        self.view_stack.addWidget(self.source_vid)

        # Output view
        output_container = QWidget()
        oc_lay = QVBoxLayout(output_container)
        oc_lay.setContentsMargins(0, 0, 0, 0)
        self.output_vid = QVideoWidget()
        self.output_vid.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.output_player = QMediaPlayer()
        self.output_audio = QAudioOutput()
        self.output_player.setVideoOutput(self.output_vid)
        self.output_player.setAudioOutput(self.output_audio)
        oc_lay.addWidget(self.output_vid)
        self.view_stack.addWidget(output_container)

        ll.addWidget(self.view_stack, 1)

        # Slider row
        self.slider = MarkerSlider()
        self.slider.setRange(0, 0)
        ll.addWidget(self.slider)

        # Transport
        transport = QHBoxLayout()
        self.play_btn = QPushButton("▶")
        self.play_btn.setObjectName("play-btn")
        self.play_btn.setFixedHeight(28)
        transport.addWidget(self.play_btn)
        transport.addSpacing(8)
        self.time_lbl = QLabel("00:00:00.000")
        self.time_lbl.setObjectName("timecode")
        transport.addWidget(self.time_lbl)
        self.dur_lbl = QLabel("/ 00:00:00.000")
        self.dur_lbl.setObjectName("duration")
        transport.addWidget(self.dur_lbl)
        transport.addStretch()

        # View toggle
        self.src_btn = self._btn("Source", "tab-active")
        self.out_btn = self._btn("Output", "tab")
        self.out_btn.setEnabled(False)
        transport.addWidget(self.src_btn)
        transport.addWidget(self.out_btn)
        ll.addLayout(transport)

        body.addWidget(left)

        # Right: controls
        right_scroll = QScrollArea()
        right_scroll.setObjectName("sidebar-scroll")
        right_scroll.setWidgetResizable(True)
        right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        right_scroll.viewport().setAutoFillBackground(False)
        right_scroll.setFrameShape(QFrame.NoFrame)

        right = QWidget()
        right.setObjectName("sidebar")
        rl = QVBoxLayout(right)
        rl.setContentsMargins(12, 20, 24, 20)
        rl.setSpacing(20)

        # ── Cue input section ──
        rl.addWidget(self._section("Add Description"))
        self.cue_time_lbl = QLabel("at 00:00:00.000")
        self.cue_time_lbl.setObjectName("cue-time")
        rl.addWidget(self.cue_time_lbl)
        self.cue_text = QTextEdit()
        self.cue_text.setObjectName("cue-input")
        self.cue_text.setPlaceholderText("What's happening on screen...")
        self.cue_text.setMaximumHeight(80)
        rl.addWidget(self.cue_text)
        cue_btns = QHBoxLayout()
        self.add_cue_btn = self._btn("Pause && Add", "accent")
        self.save_cue_btn = self._btn("Save", "primary")
        cue_btns.addWidget(self.add_cue_btn)
        cue_btns.addWidget(self.save_cue_btn)
        rl.addLayout(cue_btns)
        self.cue_text.setEnabled(False)
        self.add_cue_btn.setEnabled(False)
        self.save_cue_btn.setEnabled(False)

        # ── Cue list ──
        rl.addWidget(self._separator())
        cue_header = QHBoxLayout()
        cue_header.addWidget(self._section("Cues"))
        cue_header.addStretch()
        self.save_cues_btn = self._btn("Save", "ghost")
        self.load_cues_btn = self._btn("Load", "ghost")
        self.export_btn = self._btn("Export VTT", "ghost")
        cue_header.addWidget(self.save_cues_btn)
        cue_header.addWidget(self.load_cues_btn)
        cue_header.addWidget(self.export_btn)
        rl.addLayout(cue_header)

        self.cue_container = QWidget()
        self.cue_lay = QVBoxLayout(self.cue_container)
        self.cue_lay.setContentsMargins(0, 0, 0, 0)
        self.cue_lay.setSpacing(6)
        rl.addWidget(self.cue_container)

        # ── Render section ──
        rl.addWidget(self._separator())
        rl.addWidget(self._section("Render Settings"))

        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.mode_descs = {
            "extended": "Freezes video during each description, making it longer.",
            "mixed": "Lowers original audio and overlays description. Same length.",
            "smart": "Fills silent gaps first, pauses only for overflow.",
            "nearby": "Moves descriptions to nearby quiet moments.",
        }
        modes = [
            ("extended", "Extended"),
            ("mixed", "Mixed"),
            ("smart", "Smart"),
            ("nearby", "Nearby"),
        ]
        mode_row = QHBoxLayout()
        mode_row.setSpacing(6)
        for val, label in modes:
            b = QPushButton(label)
            b.setObjectName("mode-btn")
            b.setCheckable(True)
            b.setProperty("mode", val)
            if val == "smart":
                b.setChecked(True)
            self.mode_group.addButton(b)
            mode_row.addWidget(b)
        rl.addLayout(mode_row)

        self.mode_desc_lbl = QLabel(self.mode_descs["smart"])
        self.mode_desc_lbl.setObjectName("mode-desc")
        self.mode_desc_lbl.setWordWrap(True)
        rl.addWidget(self.mode_desc_lbl)

        self.nearby_row = QWidget()
        nr_lay = QHBoxLayout(self.nearby_row)
        nr_lay.setContentsMargins(0, 0, 0, 0)
        nr_lay.addWidget(QLabel("Before"))
        self.search_before = QDoubleSpinBox()
        self.search_before.setRange(0, 60)
        self.search_before.setSingleStep(0.25)
        self.search_before.setValue(2.0)
        self.search_before.setSuffix("s")
        nr_lay.addWidget(self.search_before)
        nr_lay.addSpacing(8)
        nr_lay.addWidget(QLabel("After"))
        self.search_after = QDoubleSpinBox()
        self.search_after.setRange(0, 60)
        self.search_after.setSingleStep(0.25)
        self.search_after.setValue(4.0)
        self.search_after.setSuffix("s")
        nr_lay.addWidget(self.search_after)
        rl.addWidget(self.nearby_row)
        self.nearby_row.setVisible(False)

        # ── Job status ──
        rl.addWidget(self._separator())
        job_head = QHBoxLayout()
        job_head.addWidget(self._section("Job"))
        self.badge = QLabel("IDLE")
        self.badge.setObjectName("badge")
        self.badge.setProperty("state", "idle")
        job_head.addStretch()
        job_head.addWidget(self.badge)
        rl.addLayout(job_head)

        self.log = QPlainTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(140)
        rl.addWidget(self.log)



        rl.addStretch()
        right_scroll.setWidget(right)
        body.addWidget(right_scroll)

        body.setSizes([700, 400])
        body.setStretchFactor(0, 3)
        body.setStretchFactor(1, 1)
        root_lay.addWidget(body, 1)

        # ── Signals ──
        self.open_btn.clicked.connect(self._open_video)
        self.render_btn.clicked.connect(self._start_render)
        self.clear_btn.clicked.connect(self._clear)
        self.add_cue_btn.clicked.connect(self._pause_and_add)
        self.save_cue_btn.clicked.connect(self._save_cue)
        self.export_btn.clicked.connect(self._export_vtt)
        self.save_cues_btn.clicked.connect(self._save_cues_file)
        self.load_cues_btn.clicked.connect(self._load_cues_file)
        self.locate_btn.clicked.connect(self._locate_output)
        self.src_btn.clicked.connect(lambda: self._switch_view("source"))
        self.out_btn.clicked.connect(lambda: self._switch_view("output"))
        self.source_player.positionChanged.connect(self._on_pos)
        self.source_player.durationChanged.connect(self._on_dur)
        self.output_player.positionChanged.connect(self._on_out_pos)
        self.output_player.durationChanged.connect(self._on_out_dur)
        self.slider.sliderMoved.connect(self._on_slider_seek)
        self.mode_group.buttonClicked.connect(self._on_mode)
        self.play_btn.clicked.connect(self._toggle_play)
        self.source_player.playbackStateChanged.connect(self._on_playback_state)
        self.output_player.playbackStateChanged.connect(self._on_playback_state)

        self._rebuild_cues()

    def _btn(self, text, style):
        b = QPushButton(text)
        b.setProperty("btnStyle", style)
        return b

    def _section(self, text):
        l = QLabel(text)
        l.setObjectName("section")
        return l

    def _separator(self):
        f = QFrame()
        f.setFrameShape(QFrame.HLine)
        f.setObjectName("sep")
        return f

    def _on_player_error(self, error, msg):
        self.status.setText(f"Player error: {msg}")

    def _on_pos(self, ms):
        if self.view_stack.currentIndex() != 0:
            return
        if not self.slider.isSliderDown():
            self.slider.setValue(ms)
        self.time_lbl.setText(fmt(ms))
        self.cue_time_lbl.setText(f"at {fmt(ms)}")

    def _on_dur(self, ms):
        if self.view_stack.currentIndex() == 0:
            self.slider.setMaximum(ms)
            self.dur_lbl.setText(f"/ {fmt(ms)}")

    def _on_out_pos(self, ms):
        if self.view_stack.currentIndex() == 1:
            if not self.slider.isSliderDown():
                self.slider.setValue(ms)
            self.time_lbl.setText(fmt(ms))

    def _on_out_dur(self, ms):
        if self.view_stack.currentIndex() == 1:
            self.slider.setMaximum(ms)
            self.dur_lbl.setText(f"/ {fmt(ms)}")

    def _on_slider_seek(self, ms):
        if self.view_stack.currentIndex() == 1:
            self.output_player.setPosition(ms)
        else:
            self.source_player.setPosition(ms)

    def _switch_view(self, view):
        if view == "source":
            self.output_player.pause()
            self.view_stack.setCurrentIndex(0)
            self.src_btn.setProperty("btnStyle", "tab-active")
            self.out_btn.setProperty("btnStyle", "tab")
            dur = self.source_player.duration()
            self.slider.setMaximum(dur)
            self.slider.setValue(self.source_player.position())
            self.dur_lbl.setText(f"/ {fmt(dur)}")
            self.time_lbl.setText(fmt(self.source_player.position()))
            self.slider.set_marks([int(c["start"] * 1000) for c in self.cues])
        else:
            self.source_player.pause()
            self.view_stack.setCurrentIndex(1)
            self.src_btn.setProperty("btnStyle", "tab")
            self.out_btn.setProperty("btnStyle", "tab-active")
            dur = self.output_player.duration()
            self.slider.setMaximum(dur)
            self.slider.setValue(self.output_player.position())
            self.dur_lbl.setText(f"/ {fmt(dur)}")
            self.time_lbl.setText(fmt(self.output_player.position()))
            self.slider.set_marks([])
        self.src_btn.style().unpolish(self.src_btn)
        self.src_btn.style().polish(self.src_btn)
        self.out_btn.style().unpolish(self.out_btn)
        self.out_btn.style().polish(self.out_btn)

    def _toggle_play(self):
        player = self.output_player if self.view_stack.currentIndex() == 1 else self.source_player
        if player.playbackState() == QMediaPlayer.PlayingState:
            player.pause()
        else:
            player.play()

    def _on_playback_state(self, state):
        self.play_btn.setText("⏸" if state == QMediaPlayer.PlayingState else "▶")

    def _on_mode(self, btn=None):
        checked = self.mode_group.checkedButton()
        mode = checked.property("mode") if checked else "smart"
        self.nearby_row.setVisible(mode == "nearby")
        self.mode_desc_lbl.setText(self.mode_descs.get(mode, ""))

    def _open_video(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Video", "",
            "Video (*.mp4 *.mkv *.avi *.mov *.webm *.m4v);;All (*)")
        if not path:
            return
        self.video_path = Path(path)
        self.source_player.setSource(QUrl.fromLocalFile(path))
        self.source_player.play()
        self.editing_id = None
        self._rebuild_cues()
        self._switch_view("source")
        self.cue_text.setEnabled(True)
        self.add_cue_btn.setEnabled(True)
        self.save_cue_btn.setEnabled(True)
        self.status.setText("Video loaded")

    def _pause_and_add(self):
        self.source_player.pause()
        self.cue_text.setFocus()

    def _save_cue(self):
        text = self.cue_text.toPlainText().strip()
        if not text:
            self.status.setText("Enter description text first")
            return
        start = self.source_player.position() / 1000.0
        if self.editing_id:
            self.cues = [{**c, "start": round(start, 3), "end": round(start + 0.001, 3), "text": text}
                         if c["id"] == self.editing_id else c for c in self.cues]
            self.editing_id = None
        else:
            self.cues.append({"id": str(uuid4()), "start": round(start, 3),
                              "end": round(start + 0.001, 3), "text": text})
        self.cues.sort(key=lambda c: c["start"])
        save_cues(self.cues)
        self.cue_text.clear()
        self._rebuild_cues()
        self.status.setText("Cue saved")

    def _edit_cue(self, cid, start, text):
        self.editing_id = cid
        self.source_player.setPosition(int(start * 1000))
        self.source_player.pause()
        self._switch_view("source")
        self.cue_text.setPlainText(text)
        self.cue_text.setFocus()

    def _delete_cue(self, cid):
        self.cues = [c for c in self.cues if c["id"] != cid]
        save_cues(self.cues)
        self._rebuild_cues()

    def _rebuild_cues(self):
        while self.cue_lay.count() > 0:
            item = self.cue_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not self.cues:
            lbl = QLabel("No cues yet")
            lbl.setObjectName("empty")
            lbl.setAlignment(Qt.AlignCenter)
            self.cue_lay.addWidget(lbl)
        else:
            for c in self.cues:
                card = QWidget()
                card.setObjectName("cue-card")
                cl = QVBoxLayout(card)
                cl.setContentsMargins(10, 8, 10, 8)
                cl.setSpacing(4)
                top = QHBoxLayout()
                ts = QPushButton(fmt(int(c["start"] * 1000)))
                ts.setObjectName("cue-ts")
                s = c["start"]
                ts.clicked.connect(lambda _, s=s: (
                    self.source_player.setPosition(int(s * 1000)),
                    self.source_player.pause(),
                    self._switch_view("source")))
                top.addWidget(ts)
                top.addStretch()
                eb = QPushButton("Edit")
                eb.setProperty("btnStyle", "ghost")
                eb.setFixedHeight(26)
                cid, cs, ct = c["id"], c["start"], c["text"]
                eb.clicked.connect(lambda _, i=cid, s=cs, t=ct: self._edit_cue(i, s, t))
                top.addWidget(eb)
                db = QPushButton("Del")
                db.setProperty("btnStyle", "danger")
                db.setFixedHeight(26)
                db.clicked.connect(lambda _, i=cid: self._delete_cue(i))
                top.addWidget(db)
                cl.addLayout(top)
                tl = QLabel(c["text"])
                tl.setWordWrap(True)
                tl.setObjectName("cue-text")
                cl.addWidget(tl)
                self.cue_lay.addWidget(card)
        self.slider.set_marks([int(c["start"] * 1000) for c in self.cues])

    def _export_vtt(self):
        if not self.cues:
            QMessageBox.warning(self, "Export", "No cues to export.")
            return
        p, _ = QFileDialog.getSaveFileName(self, "Export VTT", "cues.vtt", "WebVTT (*.vtt)")
        if p:
            write_vtt(self.cues, Path(p))
            self.status.setText("VTT exported")

    def _save_cues_file(self):
        if not self.cues:
            QMessageBox.warning(self, "Save Cues", "No cues to save.")
            return
        p, _ = QFileDialog.getSaveFileName(self, "Save Cues", "cues.json", "JSON (*.json)")
        if p:
            Path(p).write_text(json.dumps(self.cues, indent=2), encoding="utf-8")
            self.status.setText("Cues saved")

    def _load_cues_file(self):
        p, _ = QFileDialog.getOpenFileName(self, "Load Cues", "", "JSON (*.json)")
        if not p:
            return
        try:
            loaded = json.loads(Path(p).read_text(encoding="utf-8"))
            if not isinstance(loaded, list):
                raise ValueError("Expected a list of cues")
            self.cues = loaded
            self.cues.sort(key=lambda c: c["start"])
            save_cues(self.cues)
            self._rebuild_cues()
            self.status.setText(f"Loaded {len(self.cues)} cues")
        except Exception as e:
            QMessageBox.warning(self, "Load Cues", f"Failed to load: {e}")

    def _locate_output(self):
        if not self.output_path or not self.output_path.exists():
            return
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(self.output_path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(self.output_path)])
        else:
            subprocess.Popen(["xdg-open", str(self.output_path.parent)])

    def _start_render(self):
        if self.worker and self.worker.isRunning():
            return
        if not self.video_path or not self.video_path.exists():
            self.status.setText("Open a video first")
            return
        if not self.cues:
            self.status.setText("Add at least one cue")
            return
        save_cues(self.cues)
        write_vtt(self.cues, VTT_PATH)
        mode = self.mode_group.checkedButton().property("mode")
        sfx = {"extended": "extended-ad", "mixed": "mixed-ad",
               "smart": "smart-ad", "nearby": "nearby-ad"}
        self.output_path = OUTPUT_DIR / f"{self.video_path.stem}-{sfx[mode]}.mp4"
        self.output_path.unlink(missing_ok=True)
        work = GUI_WORKSPACE / f"render_{uuid4()}"
        self.log.clear()
        self._set_badge("running")
        self.render_btn.setEnabled(False)
        self.clear_btn.setEnabled(False)
        for b in self.mode_group.buttons():
            b.setEnabled(False)
        self.worker = RenderWorker(mode, self.video_path, VTT_PATH, self.output_path,
                                   work, self.search_before.value(), self.search_after.value())
        self.worker.status_changed.connect(self._on_status)
        self.worker.log_message.connect(self.log.appendPlainText)
        self.worker.finished.connect(self._on_done)
        self.worker.start()

    def _on_status(self, st, msg):
        if st == "failed" and "\n" in msg:
            # Traceback — show only the last line in the status bar
            short = msg.strip().splitlines()[-1]
            self.status.setText(short)
            self.log.appendPlainText(f"Error: {short}")
        else:
            self.status.setText(msg)
            self.log.appendPlainText(msg)
        self._set_badge(st)

    def _on_done(self):
        self.render_btn.setEnabled(True)
        self.clear_btn.setEnabled(True)
        for b in self.mode_group.buttons():
            b.setEnabled(True)
        if self.worker and self.worker.final_status == "done" and self.output_path.exists():
            self.output_player.setSource(QUrl.fromLocalFile(str(self.output_path)))
            self.out_btn.setEnabled(True)
            self.locate_btn.setVisible(True)
            self._switch_view("output")

    def _set_badge(self, st):
        self.badge.setText(st.upper())
        self.badge.setProperty("state", st)
        self.badge.style().unpolish(self.badge)
        self.badge.style().polish(self.badge)

    def _clear(self):
        if self.worker and self.worker.isRunning():
            QMessageBox.warning(self, "Clear", "Wait for the render to finish.")
            return
        self.source_player.stop()
        self.source_player.setSource(QUrl())
        self.output_player.stop()
        self.output_player.setSource(QUrl())
        self.cues.clear()
        self.editing_id = None
        self.video_path = None
        self.output_path = None
        self.cue_text.clear()
        shutil.rmtree(GUI_WORKSPACE, ignore_errors=True)
        ensure_workspace()
        self._rebuild_cues()
        self.slider.setRange(0, 0)
        self.time_lbl.setText("00:00:00.000")
        self.dur_lbl.setText("/ 00:00:00.000")
        self.cue_time_lbl.setText("at 00:00:00.000")
        self.status.setText("Ready")
        self._set_badge("idle")
        self.cue_text.setEnabled(False)
        self.add_cue_btn.setEnabled(False)
        self.save_cue_btn.setEnabled(False)
        self.log.clear()
        self.out_btn.setEnabled(False)
        self.locate_btn.setVisible(False)
        self._switch_view("source")


STYLE = """
* { font-family: "Segoe UI", sans-serif; }

QMainWindow { background: #0f111a; }

/* ── Top bar ── */
#topbar { background: #181b28; border-bottom: 1px solid #2a2d3a; }
#title  { color: #e2e8f0; font-size: 18px; font-weight: 700; letter-spacing: -0.3px; }
#status { color: #64748b; font-size: 13px; }

/* ── Body ── */
#body, #video-area, #sidebar, #sidebar-scroll { background: #0f111a; }
QSplitter::handle { background: #1e2030; }

/* ── Video ── */
QVideoWidget { background: #000000; border-radius: 6px; }

/* ── Slider ── */
QSlider::groove:horizontal { height: 4px; background: #2a2d3a; border-radius: 2px; }
QSlider::handle:horizontal { width: 12px; height: 12px; margin: -4px 0;
    background: #818cf8; border-radius: 6px; }
QSlider::handle:horizontal:hover { background: #a5b4fc; }

/* ── Timecode ── */
#timecode { color: #e2e8f0; font-family: "Cascadia Code","Consolas",monospace;
            font-size: 14px; font-weight: 600; }
#duration { color: #475569; font-family: "Cascadia Code","Consolas",monospace;
            font-size: 14px; margin-left: 4px; }

/* ── Buttons ── */
QPushButton[btnStyle="primary"] {
    background: #6366f1; color: #fff; border: none; border-radius: 6px;
    padding: 7px 18px; font-weight: 600; font-size: 13px; }
QPushButton[btnStyle="primary"]:hover { background: #818cf8; }
QPushButton[btnStyle="primary"]:pressed { background: #4f46e5; }
QPushButton[btnStyle="primary"]:disabled { background: #3730a3; color: #6366f1; }

QPushButton[btnStyle="accent"] {
    background: #7c3aed; color: #fff; border: none; border-radius: 6px;
    padding: 7px 18px; font-weight: 600; font-size: 13px; }
QPushButton[btnStyle="accent"]:hover { background: #8b5cf6; }

QPushButton[btnStyle="secondary"] {
    background: #1e2030; color: #cbd5e1; border: 1px solid #2a2d3a; border-radius: 6px;
    padding: 7px 18px; font-weight: 600; font-size: 13px; }
QPushButton[btnStyle="secondary"]:hover { background: #2a2d3a; color: #e2e8f0; }

QPushButton[btnStyle="ghost"] {
    background: transparent; color: #64748b; border: none; border-radius: 4px;
    padding: 5px 12px; font-size: 12px; }
QPushButton[btnStyle="ghost"]:hover { color: #94a3b8; background: #1e2030; }

QPushButton[btnStyle="danger"] {
    background: transparent; color: #f87171; border: none; border-radius: 4px;
    padding: 5px 10px; font-size: 12px; }
QPushButton[btnStyle="danger"]:hover { background: #1e2030; color: #fca5a5; }

QPushButton[btnStyle="tab"], QPushButton[btnStyle="tab-active"] {
    border: none; border-radius: 4px; padding: 5px 14px; font-size: 12px; font-weight: 600; }
QPushButton[btnStyle="tab"] { background: transparent; color: #475569; }
QPushButton[btnStyle="tab"]:hover { color: #94a3b8; }
QPushButton[btnStyle="tab"]:disabled { color: #2a2d3a; }
QPushButton[btnStyle="tab-active"] { background: #6366f1; color: #fff; }

/* ── Play button ── */
#play-btn {
    background: transparent; color: #e2e8f0; border: 1px solid #2a2d3a; border-radius: 4px;
    padding: 0 10px; font-size: 13px; }
#play-btn:hover { background: #1e2030; border-color: #475569; }
#play-btn:pressed { background: #2a2d3a; }

/* ── Mode buttons ── */
#mode-btn {
    background: #181b28; color: #94a3b8; border: 1px solid #2a2d3a; border-radius: 6px;
    padding: 8px 0; font-size: 13px; font-weight: 600; }
#mode-btn:hover { border-color: #6366f1; color: #c7d2fe; }
#mode-btn:checked { background: #1e1b4b; border-color: #6366f1; color: #c7d2fe; }
#mode-btn:disabled { color: #334155; border-color: #1e2030; }
#mode-desc { color: #64748b; font-size: 12px; font-style: italic; }

/* ── Section labels ── */
#section { color: #94a3b8; font-size: 14px; font-weight: 700; letter-spacing: 0.3px; }
#sep { color: #1e2030; }

/* ── Cue input ── */
#cue-time { color: #6366f1; font-family: "Cascadia Code","Consolas",monospace;
            font-size: 13px; font-weight: 600; }
#cue-input { background: #181b28; color: #e2e8f0; border: 1px solid #2a2d3a;
             border-radius: 6px; padding: 8px; font-size: 14px; }
#cue-input:focus { border-color: #6366f1; }

/* ── Cue cards ── */
#cue-card { background: #181b28; border: 1px solid #2a2d3a; border-radius: 8px; }
#cue-card:hover { border-color: #334155; }
#cue-ts { background: transparent; color: #818cf8; border: none; padding: 2px 6px;
          font-family: "Cascadia Code","Consolas",monospace; font-size: 12px; font-weight: 600; }
#cue-ts:hover { color: #a5b4fc; }
#cue-text { color: #94a3b8; font-size: 13px; line-height: 1.4; }
#empty { color: #334155; font-style: italic; padding: 16px; }

/* ── Badge ── */
#badge { font-size: 11px; font-weight: 700; padding: 3px 10px; border-radius: 10px;
         letter-spacing: 0.5px; }
QLabel[state="idle"]    { background: #1e2030; color: #64748b; }
QLabel[state="running"] { background: #422006; color: #fbbf24; }
QLabel[state="done"]    { background: #052e16; color: #4ade80; }
QLabel[state="failed"]  { background: #450a0a; color: #f87171; }

/* ── Log ── */
#log { background: #0a0c14; color: #4ade80; border: 1px solid #1e2030; border-radius: 6px;
       padding: 8px; font-family: "Cascadia Code","Consolas",monospace; font-size: 11px; }

/* ── Spinbox ── */
QDoubleSpinBox { background: #181b28; color: #cbd5e1; border: 1px solid #2a2d3a;
                 border-radius: 4px; padding: 4px 8px; }
QLabel { color: #94a3b8; }

/* ── Cue scroll area ── */
#sidebar-scroll { background: #0f111a; }
#sidebar-scroll > QWidget { background: #0f111a; }

/* ── Scrollbar ── */
QScrollBar:vertical { width: 5px; background: transparent; }
QScrollBar::handle:vertical { background: #2a2d3a; border-radius: 2px; min-height: 20px; }
QScrollBar::handle:vertical:hover { background: #475569; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { height: 0; background: none; }
"""


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
