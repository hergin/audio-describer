# Audio Description Video Builder

This script reads WebVTT audio-description cues, generates TTS audio, inserts freeze-frame pauses into the source video, and writes a longer MP4 that preserves the original video content.

## Create And Use A Virtual Environment

From this folder in your terminal:

### Windows (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If PowerShell blocks activation scripts, run this once for the current terminal session:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

### macOS (Terminal / zsh / bash)

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If `python3` is not found, install Python 3.11+ from [python.org](https://www.python.org/downloads/) or via Homebrew:

```bash
brew install python@3.11
```

## Run The Local GUI

With the virtual environment activated:

```bash
python gui_app.py
```

The GUI lets you select a video, preview it, add audio-description cues at the current playback time, export those cues as WebVTT, and render the final described MP4. Uploaded videos, cue data, and completed GUI outputs are stored under `_gui_workspace`.

## Run via Command Line (alternative)

With the virtual environment activated:

```bash
python build_audio_described_video.py
```

The default inputs and output are:

```text
Input video: sample-video.mp4
Input VTT:   sample-ad-vtt-frompanopto.txt
Output MP4:  sample-video-audio-described.mp4
Work dir:    _ad_build
```

To choose custom files:

```bash
python build_audio_described_video.py --video sample-video.mp4 --vtt sample-ad-vtt-frompanopto.txt --output output.mp4
```

To force a specific video bitrate:

```bash
python build_audio_described_video.py --video-bitrate-kbps 11381
```

To keep intermediate files for troubleshooting:

```bash
python build_audio_described_video.py --keep-temp
```

## Notes

- Tested with Python 3.11 (should work with later versions as well).
- On macOS, you may need to use `python3` instead of `python` if both Python 2 and Python 3 are installed system-wide. Once the virtual environment is activated, `python` will point to the correct interpreter.
- On macOS, if you encounter permission errors when running the GUI, ensure your terminal application has been granted necessary permissions in **System Settings → Privacy & Security** (e.g., Files and Folders, Accessibility if needed).
- Keep the terminal open while the script runs; generating TTS and re-encoding 1080p video can take a while.
- The script uses `imageio-ffmpeg` to find the bundled ffmpeg binary automatically, so no separate ffmpeg installation is required on either platform.
- Intermediate TTS, audio, frame, and segment files are written to `_ad_build` while the script runs, then removed after a successful output unless `--keep-temp` is used.
- To deactivate the virtual environment when finished, run `deactivate` in your terminal on either platform.