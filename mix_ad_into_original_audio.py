import argparse
import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

import edge_tts
import imageio_ffmpeg

from build_audio_described_video import UserFacingError, parse_vtt, probe_media


VOICE = "en-US-AriaNeural"
AAC_BITRATE = "192k"
DUCK_VOLUME = 0.25


def run_ffmpeg(args: list[str]) -> None:
    try:
        subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), *args],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or "").strip()
        raise UserFacingError(f"ffmpeg failed while mixing AD audio.\n\n{details}") from exc


async def synthesize_cues(cues, output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for cue in cues:
        path = output_dir / f"cue_{cue.index:03d}.mp3"
        print(f"TTS cue {cue.index}: {cue.text}")
        try:
            await edge_tts.Communicate(cue.text, VOICE).save(str(path))
        except Exception as exc:
            raise UserFacingError(f"Could not generate TTS for cue {cue.index}.") from exc
        paths.append(path)
    return paths


def get_duration(path: Path) -> float:
    return float(probe_media(path)["duration"])


def build_volume_expression(cues, cue_audio_paths: list[Path]) -> str:
    expression = "1"
    for cue, audio_path in zip(cues, cue_audio_paths):
        start = float(cue.start)
        end = start + get_duration(audio_path)
        expression = f"if(between(t,{start:.3f},{end:.3f}),{DUCK_VOLUME},{expression})"
    return expression


def mix_audio(video_path: Path, cue_audio_paths: list[Path], cues, output_path: Path) -> None:
    source_duration = float(probe_media(video_path)["duration"])
    volume_expression = build_volume_expression(cues, cue_audio_paths)

    args = ["-y", "-i", str(video_path)]
    for audio_path in cue_audio_paths:
        args.extend(["-i", str(audio_path)])

    filter_parts = [
        (
            "[0:a:0]aresample=48000,"
            "aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"volume='{volume_expression}':eval=frame[orig]"
        )
    ]

    mix_inputs = ["[orig]"]
    for index, cue in enumerate(cues, start=1):
        delay_ms = max(0, round(float(cue.start) * 1000))
        label = f"cue{index}"
        filter_parts.append(
            f"[{index}:a:0]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"adelay={delay_ms}|{delay_ms},atrim=0:{source_duration:.6f}[{label}]"
        )
        mix_inputs.append(f"[{label}]")

    filter_parts.append(
        f"{''.join(mix_inputs)}amix=inputs={len(mix_inputs)}:duration=first:dropout_transition=0,"
        "alimiter=limit=0.95[mixed]"
    )

    args.extend(
        [
            "-filter_complex",
            ";".join(filter_parts),
            "-map",
            "0:v:0",
            "-map",
            "[mixed]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-b:a",
            AAC_BITRATE,
            "-shortest",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    run_ffmpeg(args)


def build_mixed_ad_video(
    video_path: Path,
    vtt_path: Path,
    output_path: Path,
    work_dir: Path,
    keep_temp: bool,
) -> None:
    if not video_path.is_file():
        raise UserFacingError(f"Video not found: {video_path}")
    if not vtt_path.is_file():
        raise UserFacingError(f"VTT file not found: {vtt_path}")
    if video_path == output_path:
        raise UserFacingError("Output must be different from input video.")

    if work_dir.exists():
        shutil.rmtree(work_dir)
    tts_dir = work_dir / "tts"

    try:
        cues = parse_vtt(vtt_path)
        if not cues:
            raise UserFacingError(f"No cues found in: {vtt_path}")

        cue_audio_paths = asyncio.run(synthesize_cues(cues, tts_dir))
        mix_audio(video_path, cue_audio_paths, cues, output_path)
    finally:
        if not keep_temp:
            shutil.rmtree(work_dir, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Keep video length unchanged and mix AD speech into the original audio."
    )
    parser.add_argument("--video", type=Path, default=Path("sample-video.mp4"))
    parser.add_argument("--vtt", type=Path, default=Path("vtt-sample.txt"))
    parser.add_argument("--output", type=Path, default=Path("sample-video-ad-mixed.mp4"))
    parser.add_argument("--work-dir", type=Path, default=Path("_ad_mix_build"))
    parser.add_argument("--keep-temp", action="store_true")
    args = parser.parse_args()

    video_path = args.video.resolve()
    vtt_path = args.vtt.resolve()
    output_path = args.output.resolve()
    work_dir = args.work_dir.resolve()

    try:
        build_mixed_ad_video(video_path, vtt_path, output_path, work_dir, args.keep_temp)
    except UserFacingError as exc:
        sys.exit(f"Error: {exc}")

    print(f"Done: {output_path}")


if __name__ == "__main__":
    main()
