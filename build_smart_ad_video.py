import argparse
import asyncio
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import imageio_ffmpeg

from build_audio_described_video import (
    DEFAULT_VIDEO_BITRATE_KBPS,
    UserFacingError,
    audio_encode_args,
    concat_segments,
    encode_mp3_to_segment_audio,
    extract_frame,
    make_original_segment,
    make_pause_segment,
    media_duration,
    parse_vtt,
    probe_media,
    seconds_to_timestamp,
    synthesize_tts,
    validate_inputs,
    video_encode_args,
)


SILENCE_NOISE_DB = -35
SILENCE_MIN_DURATION = 0.25
DUCK_VOLUME = 0.25


@dataclass(frozen=True)
class SilenceSpan:
    start: float
    end: float


@dataclass(frozen=True)
class CuePlan:
    cue_index: int
    cue_start: float
    cue_audio_path: Path
    ad_duration: float
    continue_duration: float
    pause_duration: float
    requested_start: float


def run_ffmpeg(args: list[str]) -> subprocess.CompletedProcess:
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
        raise UserFacingError(f"ffmpeg failed while building smart AD video.\n\n{details}") from exc


def detect_silence(video_path: Path, source_duration: float) -> list[SilenceSpan]:
    completed = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-hide_banner",
            "-i",
            str(video_path),
            "-af",
            f"silencedetect=noise={SILENCE_NOISE_DB}dB:d={SILENCE_MIN_DURATION}",
            "-f",
            "null",
            "-",
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    output = completed.stdout + completed.stderr
    if completed.returncode != 0:
        raise UserFacingError(f"Could not detect silence in source audio.\n\n{output[-2000:]}")

    spans: list[SilenceSpan] = []
    current_start: float | None = None
    for line in output.splitlines():
        start_match = re.search(r"silence_start:\s*([0-9.]+)", line)
        if start_match:
            current_start = float(start_match.group(1))
            continue

        end_match = re.search(r"silence_end:\s*([0-9.]+)", line)
        if end_match and current_start is not None:
            end = float(end_match.group(1))
            if end > current_start:
                spans.append(SilenceSpan(current_start, end))
            current_start = None

    if current_start is not None and current_start < source_duration:
        spans.append(SilenceSpan(current_start, source_duration))

    return spans


def silence_available_from(cue_start: float, spans: list[SilenceSpan], source_duration: float) -> float:
    for span in spans:
        if span.start <= cue_start < span.end:
            return max(0.0, min(span.end, source_duration) - cue_start)
    return 0.0


def choose_start_within_current_silence(
    requested_start: float,
    ad_duration: float,
    spans: list[SilenceSpan],
    source_duration: float,
) -> tuple[float, float]:
    for span in spans:
        if span.start <= requested_start < span.end:
            silence_end = min(span.end, source_duration)
            latest_start_that_fits = silence_end - ad_duration
            cue_start = max(span.start, min(requested_start, latest_start_that_fits))
            return cue_start, max(0.0, silence_end - cue_start)
    return requested_start, 0.0


def make_continue_with_ad_segment(
    video_path: Path,
    cue_audio_path: Path,
    source_start: float,
    duration: float,
    output_path: Path,
    fps: float,
    bitrate_kbps: int,
) -> None:
    if duration <= 0.001:
        return

    run_ffmpeg(
        [
            "-y",
            "-ss",
            seconds_to_timestamp(source_start),
            "-t",
            f"{duration:.6f}",
            "-i",
            str(video_path),
            "-i",
            str(cue_audio_path),
            "-filter_complex",
            (
                "[0:a:0]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"volume={DUCK_VOLUME}[orig];"
                "[1:a:0]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                f"atrim=0:{duration:.6f}[ad];"
                "[orig][ad]amix=inputs=2:duration=first:dropout_transition=0,"
                "alimiter=limit=0.95[mixed]"
            ),
            "-map",
            "0:v:0",
            "-map",
            "[mixed]",
            *video_encode_args(fps, bitrate_kbps),
            *audio_encode_args(),
            "-movflags",
            "+faststart",
            "-avoid_negative_ts",
            "make_zero",
            str(output_path),
        ]
    )


def make_pause_overflow_segment(
    frame_path: Path,
    cue_audio_path: Path,
    audio_offset: float,
    duration: float,
    output_path: Path,
    fps: float,
    bitrate_kbps: int,
) -> None:
    if duration <= 0.001:
        return

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
            "-ss",
            f"{audio_offset:.6f}",
            "-t",
            f"{duration:.6f}",
            "-i",
            str(cue_audio_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            *video_encode_args(fps, bitrate_kbps),
            *audio_encode_args(),
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )


def build_duck_volume_expression(plans: list[CuePlan]) -> str:
    expression = "1"
    for plan in plans:
        start = plan.cue_start
        end = plan.cue_start + plan.ad_duration
        expression = f"if(between(t,{start:.3f},{end:.3f}),{DUCK_VOLUME},{expression})"
    return expression


def make_no_pause_smart_output(
    video_path: Path,
    plans: list[CuePlan],
    output_path: Path,
    source_duration: float,
) -> None:
    args = ["-y", "-i", str(video_path)]
    for plan in plans:
        args.extend(["-i", str(plan.cue_audio_path)])

    filter_parts = [
        (
            "[0:a:0]aresample=48000,"
            "aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"volume='{build_duck_volume_expression(plans)}':eval=frame[orig]"
        )
    ]

    mix_inputs = ["[orig]"]
    for index, plan in enumerate(plans, start=1):
        delay_ms = max(0, round(plan.cue_start * 1000))
        label = f"ad{index}"
        filter_parts.append(
            f"[{index}:a:0]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"adelay={delay_ms}|{delay_ms},atrim=0:{source_duration:.6f}[{label}]"
        )
        mix_inputs.append(f"[{label}]")

    filter_parts.append(
        f"{''.join(mix_inputs)}amix=inputs={len(mix_inputs)}:duration=first:dropout_transition=0,"
        "alimiter=limit=0.95[mixed]"
    )

    run_ffmpeg(
        [
            *args,
            "-filter_complex",
            ";".join(filter_parts),
            "-map",
            "0:v:0",
            "-map",
            "[mixed]",
            "-c:v",
            "copy",
            *audio_encode_args(),
            "-shortest",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )


def build_smart_ad_video(
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

    if work_dir.exists():
        shutil.rmtree(work_dir)
    frames_dir = work_dir / "frames"
    tts_dir = work_dir / "tts_mp3"
    audio_dir = work_dir / "tts_aac"
    segments_dir = work_dir / "segments"
    for directory in (frames_dir, tts_dir, audio_dir, segments_dir):
        directory.mkdir(parents=True, exist_ok=True)

    try:
        spans = detect_silence(video_path, source_duration)
        print(f"Found {len(spans)} silent gap(s) in audio")

        mp3_paths = asyncio.run(synthesize_tts(cues, tts_dir))

        plans: list[CuePlan] = []
        for cue, mp3_path in zip(cues, mp3_paths):
            if cue.start > source_duration:
                print(f"Cue {cue.index} is past the end of the video, skipping")
                continue

            cue_audio_path = audio_dir / f"cue_{cue.index:03d}.m4a"
            encode_mp3_to_segment_audio(mp3_path, cue_audio_path)
            ad_duration = media_duration(cue_audio_path)
            cue_start, available_silence = choose_start_within_current_silence(
                cue.start,
                ad_duration,
                spans,
                source_duration,
            )
            continue_duration = min(ad_duration, available_silence, max(0.0, source_duration - cue_start))
            pause_duration = max(0.0, ad_duration - continue_duration)
            plans.append(
                CuePlan(
                    cue_index=cue.index,
                    cue_start=cue_start,
                    cue_audio_path=cue_audio_path,
                    ad_duration=ad_duration,
                    continue_duration=continue_duration,
                    pause_duration=pause_duration,
                    requested_start=cue.start,
                )
            )
            if pause_duration > 0.001:
                print(f"Cue {cue.index}: placed at {cue_start:.1f}s (pauses {pause_duration:.1f}s)")
            else:
                print(f"Cue {cue.index}: placed at {cue_start:.1f}s (fits in silence)")

        segment_paths: list[Path] = []
        previous_source_time = 0.0

        if plans and all(plan.pause_duration <= 0.001 for plan in plans):
            print("No pauses needed — mixing audio directly")
            make_no_pause_smart_output(video_path, plans, output_path, source_duration)
            output_duration = media_duration(output_path)
            print("Render finished!")
            print(f"Original length: {source_duration:.1f}s")
            print(f"Output length: {output_duration:.1f}s")
            return

        for plan in plans:
            if plan.cue_start < previous_source_time:
                raise UserFacingError(
                    f"Cue {plan.cue_index} overlaps a previous smart AD segment. "
                    "Move cues farther apart or use Extended AD mode."
                )

            source_path = segments_dir / f"{len(segment_paths):04d}_source_to_cue_{plan.cue_index:03d}.mp4"
            if make_original_segment(
                video_path,
                previous_source_time,
                plan.cue_start,
                source_path,
                fps,
                target_bitrate,
            ):
                segment_paths.append(source_path)

            if plan.continue_duration > 0.001:
                continue_path = segments_dir / f"{len(segment_paths):04d}_continue_ad_{plan.cue_index:03d}.mp4"
                make_continue_with_ad_segment(
                    video_path,
                    plan.cue_audio_path,
                    plan.cue_start,
                    plan.continue_duration,
                    continue_path,
                    fps,
                    target_bitrate,
                )
                segment_paths.append(continue_path)

            if plan.pause_duration > 0.001:
                freeze_time = min(plan.cue_start + plan.continue_duration, source_duration)
                frame_path = frames_dir / f"cue_{plan.cue_index:03d}.png"
                extract_frame(video_path, freeze_time, frame_path)

                pause_path = segments_dir / f"{len(segment_paths):04d}_pause_overflow_{plan.cue_index:03d}.mp4"
                make_pause_overflow_segment(
                    frame_path,
                    plan.cue_audio_path,
                    plan.continue_duration,
                    plan.pause_duration,
                    pause_path,
                    fps,
                    target_bitrate,
                )
                segment_paths.append(pause_path)

            previous_source_time = plan.cue_start + plan.continue_duration

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
        print("Render finished!")
        print(f"Original length: {source_duration:.1f}s")
        print(f"Output length: {output_duration:.1f}s")
        print(f"Added: {output_duration - source_duration:.1f}s")
    finally:
        if not keep_temp:
            shutil.rmtree(work_dir, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Use source silence for AD first, then pause only for AD overflow."
    )
    parser.add_argument("--video", type=Path, default=Path("sample-video.mp4"))
    parser.add_argument("--vtt", type=Path, default=Path("vtt-sample.txt"))
    parser.add_argument("--output", type=Path, default=Path("sample-video-smart-ad.mp4"))
    parser.add_argument("--work-dir", type=Path, default=Path("_smart_ad_build"))
    parser.add_argument("--video-bitrate-kbps", type=int, default=None)
    parser.add_argument("--keep-temp", action="store_true")
    args = parser.parse_args()

    try:
        build_smart_ad_video(
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
