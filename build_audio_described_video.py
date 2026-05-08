import argparse
import asyncio
import html
import json
import math
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import edge_tts
import imageio_ffmpeg


VOICE = "en-US-AriaNeural"
DEFAULT_VIDEO_BITRATE_KBPS = 11381
DEFAULT_AUDIO_BITRATE = "192k"


class UserFacingError(RuntimeError):
    pass


@dataclass(frozen=True)
class Cue:
    index: int
    start: float
    end: float
    raw_text: str
    text: str


def timestamp_to_seconds(value: str) -> float:
    value = value.strip().replace(",", ".")
    parts = value.split(":")
    if len(parts) == 3:
        hours, minutes, seconds = parts
    elif len(parts) == 2:
        hours = "0"
        minutes, seconds = parts
    else:
        raise ValueError(f"Invalid timestamp: {value}")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def seconds_to_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    remainder = seconds - hours * 3600 - minutes * 60
    return f"{hours:02d}:{minutes:02d}:{remainder:06.3f}"


def clean_vtt_text(raw_text: str) -> str:
    text = html.unescape(raw_text).replace("\ufeff", "")
    text = re.sub(r"<[^>]+>", " ", text)

    # Preserve words intentionally split by Panopto line wraps, such as
    # "model-view-\n controller".
    text = re.sub(r"([A-Za-z])-\s*\n\s*([A-Za-z])", r"\1-\2", text)
    text = re.sub(r"\s*\n\s*", " ", text)

    text = text.replace("•", ". ")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"(^|[.!?]\s+)-\s+", r"\1", text)
    text = re.sub(r"\.{2,}", ".", text)
    text = text.strip(" .")
    if text and text[-1] not in ".!?":
        text += "."
    return text


def parse_vtt(path: Path) -> list[Cue]:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise UserFacingError(f"Could not read VTT file: {path}") from exc

    cues: list[Cue] = []
    i = 0
    cue_number = 1

    while i < len(lines):
        line = lines[i].strip()
        if not line or line == "WEBVTT" or line.startswith(("NOTE", "STYLE", "REGION")):
            i += 1
            continue

        if "-->" not in line and i + 1 < len(lines) and "-->" in lines[i + 1]:
            i += 1
            line = lines[i].strip()

        if "-->" not in line:
            i += 1
            continue

        start_text, end_part = line.split("-->", 1)
        end_text = end_part.strip().split()[0]
        try:
            start = timestamp_to_seconds(start_text)
            end = timestamp_to_seconds(end_text)
        except ValueError as exc:
            raise UserFacingError(f"Invalid VTT timestamp near line {i + 1}: {line}") from exc
        i += 1

        text_lines: list[str] = []
        while i < len(lines) and lines[i].strip():
            text_lines.append(lines[i].rstrip())
            i += 1

        raw_text = "\n".join(text_lines)
        clean_text = clean_vtt_text(raw_text)
        if clean_text:
            cues.append(Cue(cue_number, start, end, raw_text, clean_text))
            cue_number += 1

    cues.sort(key=lambda cue: cue.start)
    return cues


def run_ffmpeg(args: list[str], *, quiet: bool = False) -> subprocess.CompletedProcess:
    if not quiet:
        print("ffmpeg", " ".join(args))
    try:
        return subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), *args],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or "").strip()
        if len(details) > 2000:
            details = details[-2000:]
        message = "ffmpeg failed while processing media."
        if details:
            message += f"\n\nffmpeg output:\n{details}"
        raise UserFacingError(message) from exc


def run_ffmpeg_probe(args: list[str]) -> str:
    completed = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), *args],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout + completed.stderr


def probe_media(path: Path) -> dict[str, float | int | str]:
    output = run_ffmpeg_probe(["-hide_banner", "-i", str(path)])
    duration_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output)
    if not duration_match:
        raise UserFacingError(f"Could not read media duration for: {path}")

    hours, minutes, seconds = duration_match.groups()
    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    video_line = next((line for line in output.splitlines() if " Video: " in line), "")
    audio_line = next((line for line in output.splitlines() if " Audio: " in line), "")

    resolution_match = re.search(r"(\d{2,5})x(\d{2,5})", video_line)
    fps_match = re.search(r"(\d+(?:\.\d+)?)\s*fps", video_line)
    video_bitrate_match = re.search(r"Video:.*?(\d+)\s*kb/s", video_line)
    container_bitrate_match = re.search(r"bitrate:\s*(\d+)\s*kb/s", output)
    sample_rate_match = re.search(r"(\d+)\s*Hz", audio_line)

    width = int(resolution_match.group(1)) if resolution_match else 1920
    height = int(resolution_match.group(2)) if resolution_match else 1080
    fps = float(fps_match.group(1)) if fps_match else 23.98
    video_bitrate = (
        int(video_bitrate_match.group(1))
        if video_bitrate_match
        else DEFAULT_VIDEO_BITRATE_KBPS
    )
    container_bitrate = int(container_bitrate_match.group(1)) if container_bitrate_match else 0
    sample_rate = int(sample_rate_match.group(1)) if sample_rate_match else 48000

    return {
        "duration": duration,
        "width": width,
        "height": height,
        "fps": fps,
        "video_bitrate_kbps": video_bitrate,
        "container_bitrate_kbps": container_bitrate,
        "sample_rate": sample_rate,
    }


async def synthesize_tts(cues: list[Cue], tts_dir: Path) -> list[Path]:
    tts_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for cue in cues:
        mp3_path = tts_dir / f"cue_{cue.index:03d}.mp3"
        print(f"TTS cue {cue.index}: {cue.text}")
        try:
            await edge_tts.Communicate(cue.text, VOICE).save(str(mp3_path))
        except Exception as exc:
            raise UserFacingError(
                f"Could not generate TTS for cue {cue.index}. Check your internet connection and edge-tts setup."
            ) from exc
        paths.append(mp3_path)
    return paths


def encode_mp3_to_segment_audio(mp3_path: Path, output_path: Path) -> None:
    run_ffmpeg(
        [
            "-y",
            "-i",
            str(mp3_path),
            "-vn",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-c:a",
            "aac",
            "-b:a",
            DEFAULT_AUDIO_BITRATE,
            str(output_path),
        ]
    )


def media_duration(path: Path) -> float:
    return float(probe_media(path)["duration"])


def extract_frame(video_path: Path, timestamp: float, output_path: Path) -> None:
    run_ffmpeg(
        [
            "-y",
            "-i",
            str(video_path),
            "-ss",
            seconds_to_timestamp(timestamp),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(output_path),
        ]
    )


def video_encode_args(fps: float, bitrate_kbps: int) -> list[str]:
    fps_text = f"{fps:.6f}".rstrip("0").rstrip(".")
    keyint = max(1, int(round(fps * 2)))
    return [
        "-r",
        fps_text,
        "-c:v",
        "libx264",
        "-preset",
        "slow",
        "-pix_fmt",
        "yuv420p",
        "-b:v",
        f"{bitrate_kbps}k",
        "-maxrate",
        f"{math.ceil(bitrate_kbps * 1.15)}k",
        "-bufsize",
        f"{bitrate_kbps * 2}k",
        "-x264-params",
        f"keyint={keyint}:min-keyint={keyint}:scenecut=0",
    ]


def audio_encode_args() -> list[str]:
    return ["-ar", "48000", "-ac", "2", "-c:a", "aac", "-b:a", DEFAULT_AUDIO_BITRATE]


def make_original_segment(
    video_path: Path,
    start: float,
    end: float,
    output_path: Path,
    fps: float,
    bitrate_kbps: int,
) -> bool:
    duration = end - start
    if duration <= 0.001:
        return False

    run_ffmpeg(
        [
            "-y",
            "-ss",
            seconds_to_timestamp(start),
            "-t",
            f"{duration:.6f}",
            "-i",
            str(video_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            *video_encode_args(fps, bitrate_kbps),
            *audio_encode_args(),
            "-movflags",
            "+faststart",
            "-avoid_negative_ts",
            "make_zero",
            str(output_path),
        ]
    )
    return True


def make_pause_segment(
    frame_path: Path,
    audio_path: Path,
    duration: float,
    output_path: Path,
    fps: float,
    bitrate_kbps: int,
) -> None:
    run_ffmpeg(
        [
            "-y",
            "-loop",
            "1",
            "-framerate",
            f"{fps:.6f}".rstrip("0").rstrip("."),
            "-t",
            f"{duration:.6f}",
            "-i",
            str(frame_path),
            "-i",
            str(audio_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-shortest",
            *video_encode_args(fps, bitrate_kbps),
            *audio_encode_args(),
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )


def concat_segments(segment_paths: list[Path], output_path: Path) -> None:
    list_path = output_path.with_suffix(".concat.txt")
    lines = []
    for path in segment_paths:
        safe_path = path.resolve().as_posix().replace("'", "'\\''")
        lines.append(f"file '{safe_path}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    run_ffmpeg(
        [
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    list_path.unlink(missing_ok=True)


def validate_inputs(video_path: Path, vtt_path: Path, output_path: Path, work_dir: Path) -> None:
    missing_paths = []
    if not video_path.exists():
        missing_paths.append(f"video file not found: {video_path}")
    elif not video_path.is_file():
        missing_paths.append(f"video path is not a file: {video_path}")

    if not vtt_path.exists():
        missing_paths.append(f"VTT file not found: {vtt_path}")
    elif not vtt_path.is_file():
        missing_paths.append(f"VTT path is not a file: {vtt_path}")

    if missing_paths:
        raise UserFacingError("Input file problem:\n- " + "\n- ".join(missing_paths))

    if video_path.resolve() == output_path.resolve():
        raise UserFacingError("Output path must be different from the input video path.")

    if work_dir.resolve() in {video_path.resolve(), vtt_path.resolve(), output_path.resolve()}:
        raise UserFacingError("Work directory must not be the same path as an input or output file.")

    if vtt_path.suffix.lower() not in {".vtt", ".txt"}:
        print(f"Warning: VTT file extension is '{vtt_path.suffix}', expected .vtt or .txt.")

    output_parent = output_path.parent
    if not output_parent.exists():
        try:
            output_parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise UserFacingError(f"Could not create output folder: {output_parent}") from exc

    if not output_parent.is_dir():
        raise UserFacingError(f"Output folder is not a directory: {output_parent}")

    try:
        ffmpeg_path = Path(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception as exc:
        raise UserFacingError("Could not locate the bundled ffmpeg binary from imageio-ffmpeg.") from exc

    if not ffmpeg_path.exists():
        raise UserFacingError(f"Bundled ffmpeg binary was not found: {ffmpeg_path}")


def build_video(
    video_path: Path,
    vtt_path: Path,
    output_path: Path,
    work_dir: Path,
    bitrate_kbps: int | None,
    keep_temp: bool,
) -> None:
    validate_inputs(video_path, vtt_path, output_path, work_dir)
    if bitrate_kbps is not None and bitrate_kbps <= 0:
        raise UserFacingError("--video-bitrate-kbps must be a positive integer.")

    cues = parse_vtt(vtt_path)
    if not cues:
        raise UserFacingError(f"No readable cues found in VTT file: {vtt_path}")

    metadata = probe_media(video_path)
    source_duration = float(metadata["duration"])
    fps = float(metadata["fps"])
    target_bitrate = bitrate_kbps or int(metadata["video_bitrate_kbps"]) or DEFAULT_VIDEO_BITRATE_KBPS

    print(json.dumps({**metadata, "target_video_bitrate_kbps": target_bitrate}, indent=2))
    print(f"Parsed {len(cues)} cue(s).")

    try:
        if work_dir.exists():
            shutil.rmtree(work_dir)
    except OSError as exc:
        raise UserFacingError(f"Could not remove existing work directory: {work_dir}") from exc

    frames_dir = work_dir / "frames"
    tts_dir = work_dir / "tts_mp3"
    audio_dir = work_dir / "tts_aac"
    segments_dir = work_dir / "segments"
    try:
        for directory in (frames_dir, tts_dir, audio_dir, segments_dir):
            directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UserFacingError(f"Could not create work directory: {work_dir}") from exc

    mp3_paths = asyncio.run(synthesize_tts(cues, tts_dir))

    segment_paths: list[Path] = []
    previous_source_time = 0.0

    for cue, mp3_path in zip(cues, mp3_paths):
        if cue.start > source_duration:
            print(f"Skipping cue {cue.index}; start is beyond the source video duration.")
            continue

        cue_audio_path = audio_dir / f"cue_{cue.index:03d}.m4a"
        encode_mp3_to_segment_audio(mp3_path, cue_audio_path)
        pause_duration = media_duration(cue_audio_path)

        original_path = segments_dir / f"{len(segment_paths):04d}_source_to_cue_{cue.index:03d}.mp4"
        if make_original_segment(
            video_path,
            previous_source_time,
            cue.start,
            original_path,
            fps,
            target_bitrate,
        ):
            segment_paths.append(original_path)

        frame_path = frames_dir / f"cue_{cue.index:03d}.png"
        extract_frame(video_path, cue.start, frame_path)

        pause_path = segments_dir / f"{len(segment_paths):04d}_pause_cue_{cue.index:03d}.mp4"
        make_pause_segment(frame_path, cue_audio_path, pause_duration, pause_path, fps, target_bitrate)
        segment_paths.append(pause_path)

        previous_source_time = cue.start

    tail_path = segments_dir / f"{len(segment_paths):04d}_source_tail.mp4"
    if make_original_segment(
        video_path,
        previous_source_time,
        source_duration,
        tail_path,
        fps,
        target_bitrate,
    ):
        segment_paths.append(tail_path)

    concat_segments(segment_paths, output_path)
    output_duration = media_duration(output_path)
    print(f"Done: {output_path}")
    print(f"Source duration: {source_duration:.3f}s")
    print(f"Output duration: {output_duration:.3f}s")
    print(f"Added duration: {output_duration - source_duration:.3f}s")

    if keep_temp:
        print(f"Temporary files kept in: {work_dir}")
    else:
        shutil.rmtree(work_dir, ignore_errors=True)
        print(f"Removed temporary files: {work_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Insert audio-description freeze-frame pauses into a video from WebVTT cues."
    )
    parser.add_argument("--video", type=Path, default=Path("sample-video.mp4"))
    parser.add_argument("--vtt", type=Path, default=Path("sample-ad-vtt-frompanopto.txt"))
    parser.add_argument("--output", type=Path, default=Path("sample-video-audio-described.mp4"))
    parser.add_argument("--work-dir", type=Path, default=Path("_ad_build"))
    parser.add_argument(
        "--video-bitrate-kbps",
        type=int,
        default=None,
        help=f"Override output video bitrate. Defaults to probed stream bitrate or {DEFAULT_VIDEO_BITRATE_KBPS}.",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Keep intermediate TTS, frame, audio, and segment files in the work directory.",
    )
    args = parser.parse_args()

    try:
        build_video(
            args.video.resolve(),
            args.vtt.resolve(),
            args.output.resolve(),
            args.work_dir.resolve(),
            args.video_bitrate_kbps,
            args.keep_temp,
        )
    except UserFacingError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nCancelled by user.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
