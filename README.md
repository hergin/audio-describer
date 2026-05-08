# Audio Description Video Builder

This script reads WebVTT audio-description cues, generates TTS audio, inserts freeze-frame pauses into the source video, and writes a longer MP4 that preserves the original video content.

## Create And Use A Virtual Environment

From this folder in PowerShell:

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

## Run

With the virtual environment activated:

```powershell
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

```powershell
python build_audio_described_video.py --video sample-video.mp4 --vtt sample-ad-vtt-frompanopto.txt --output output.mp4
```

To force a specific video bitrate:

```powershell
python build_audio_described_video.py --video-bitrate-kbps 11381
```

To keep intermediate files for troubleshooting:

```powershell
python build_audio_described_video.py --keep-temp
```

## Notes

- Keep the terminal open while the script runs; generating TTS and re-encoding 1080p video can take a while.
- The script uses `imageio-ffmpeg` to find the bundled ffmpeg binary automatically.
- Intermediate TTS, audio, frame, and segment files are written to `_ad_build` while the script runs, then removed after a successful output unless `--keep-temp` is used.
