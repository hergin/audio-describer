import argparse
import asyncio
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from build_audio_described_video import (
    DEFAULT_VIDEO_BITRATE_KBPS,
    UserFacingError,
    concat_segments,
    encode_mp3_to_segment_audio,
    extract_frame,
    make_original_segment,
    media_duration,
    parse_vtt,
    probe_media,
    synthesize_tts,
    validate_inputs,
)
from build_smart_ad_video import (
    CuePlan,
    detect_silence,
    make_continue_with_ad_segment,
    make_pause_overflow_segment,
)


SEARCH_BEFORE_SECONDS = 2.0
SEARCH_AFTER_SECONDS = 4.0


@dataclass(frozen=True)
class NearbyCuePlan(CuePlan):
    requested_start: float
    placement_drift: float


def choose_nearby_start(
    requested_start: float,
    ad_duration: float,
    source_duration: float,
    silent_spans,
    previous_source_time: float,
    search_before: float,
    search_after: float,
) -> tuple[float, float]:
    search_start = max(previous_source_time, requested_start - search_before)
    search_end = min(source_duration, requested_start + search_after)
    candidates: list[tuple[int, float, float, float, float]] = []

    for span in silent_spans:
        quiet_start = max(span.start, search_start)
        quiet_end = min(span.end, search_end)
        if quiet_end <= quiet_start:
            continue
        silence_duration = quiet_end - quiet_start

        if silence_duration >= ad_duration:
            latest_start_that_fits = quiet_end - ad_duration
            candidate_start = min(max(requested_start, quiet_start), latest_start_that_fits)
            fits_ad = 1
            available_from_start = quiet_end - candidate_start
        else:
            candidate_start = quiet_start
            fits_ad = 0
            available_from_start = quiet_end - candidate_start

        drift = abs(candidate_start - requested_start)
        candidates.append((fits_ad, silence_duration, -drift, candidate_start, available_from_start))

    if not candidates:
        return requested_start, 0.0

    candidates.sort(reverse=True)
    fits_ad, silence_duration, negative_drift, cue_start, available_silence = candidates[0]
    return cue_start, available_silence


def build_nearby_silence_ad_video(
    video_path: Path,
    vtt_path: Path,
    output_path: Path,
    work_dir: Path,
    bitrate_kbps: int | None,
    keep_temp: bool,
    search_before: float,
    search_after: float,
) -> None:
    validate_inputs(video_path, vtt_path, output_path, work_dir)
    if bitrate_kbps is not None and bitrate_kbps <= 0:
        raise UserFacingError("--video-bitrate-kbps must be a positive integer.")
    if search_before < 0 or search_after < 0:
        raise UserFacingError("Search window values must be non-negative.")

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
        silent_spans = detect_silence(video_path, source_duration)
        print(f"Found {len(silent_spans)} quiet gap(s) in audio")

        mp3_paths = asyncio.run(synthesize_tts(cues, tts_dir))

        plans: list[NearbyCuePlan] = []
        previous_source_time = 0.0
        for cue, mp3_path in zip(cues, mp3_paths):
            if cue.start > source_duration:
                print(f"Cue {cue.index} is past the end of the video, skipping")
                continue

            cue_audio_path = audio_dir / f"cue_{cue.index:03d}.m4a"
            encode_mp3_to_segment_audio(mp3_path, cue_audio_path)
            ad_duration = media_duration(cue_audio_path)
            cue_start, available_silence = choose_nearby_start(
                requested_start=cue.start,
                ad_duration=ad_duration,
                source_duration=source_duration,
                silent_spans=silent_spans,
                previous_source_time=previous_source_time,
                search_before=search_before,
                search_after=search_after,
            )
            continue_duration = min(ad_duration, available_silence, max(0.0, source_duration - cue_start))
            pause_duration = max(0.0, ad_duration - continue_duration)
            drift = cue_start - cue.start

            if cue_start < previous_source_time:
                raise UserFacingError(
                    f"Cue {cue.index} would overlap a previous AD segment. "
                    "Move cues farther apart or reduce the nearby search window."
                )

            plans.append(
                NearbyCuePlan(
                    cue_index=cue.index,
                    cue_start=cue_start,
                    cue_audio_path=cue_audio_path,
                    ad_duration=ad_duration,
                    continue_duration=continue_duration,
                    pause_duration=pause_duration,
                    requested_start=cue.start,
                    placement_drift=drift,
                )
            )
            previous_source_time = cue_start + continue_duration

            if pause_duration > 0.001:
                print(f"Cue {cue.index}: moved to {cue_start:.1f}s ({drift:+.1f}s, pauses {pause_duration:.1f}s)")
            else:
                print(f"Cue {cue.index}: moved to {cue_start:.1f}s ({drift:+.1f}s, fits in gap)")

        segment_paths: list[Path] = []
        previous_source_time = 0.0

        for plan in plans:
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
        description="Search nearby quiet gaps for AD, then pause only for overflow."
    )
    parser.add_argument("--video", type=Path, default=Path("sample-video.mp4"))
    parser.add_argument("--vtt", type=Path, default=Path("vtt-sample.txt"))
    parser.add_argument("--output", type=Path, default=Path("sample-video-nearby-silence-ad.mp4"))
    parser.add_argument("--work-dir", type=Path, default=Path("_nearby_silence_ad_build"))
    parser.add_argument("--video-bitrate-kbps", type=int, default=None)
    parser.add_argument("--search-before", type=float, default=SEARCH_BEFORE_SECONDS)
    parser.add_argument("--search-after", type=float, default=SEARCH_AFTER_SECONDS)
    parser.add_argument("--keep-temp", action="store_true")
    args = parser.parse_args()

    try:
        build_nearby_silence_ad_video(
            video_path=args.video.resolve(),
            vtt_path=args.vtt.resolve(),
            output_path=args.output.resolve(),
            work_dir=args.work_dir.resolve(),
            bitrate_kbps=args.video_bitrate_kbps,
            keep_temp=args.keep_temp,
            search_before=args.search_before,
            search_after=args.search_after,
        )
    except UserFacingError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nCancelled by user.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
