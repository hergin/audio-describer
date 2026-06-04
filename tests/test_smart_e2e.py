"""End-to-end tests for smart AD rendering.

These run the *entire* ffmpeg pipeline (silence detection, segment building,
concatenation / audio mixing) against tiny synthetic videos generated on the
fly. Only the TTS vendor call is stubbed out, with fixed-length silent clips,
so the duration math is deterministic and no network is required.

Run from the project root with:

    python -m unittest discover tests
"""

import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import imageio_ffmpeg

# Make the top-level project modules importable when run via `unittest discover`.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import build_smart_ad_video  # noqa: E402
from build_audio_described_video import (  # noqa: E402
    probe_media,
    run_ffmpeg_probe,
    seconds_to_timestamp,
)
from build_smart_ad_video import build_smart_ad_video as run_smart  # noqa: E402

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


# ──────────────────────────── fixture helpers ────────────────────────────


def _ffmpeg(args: list[str]) -> None:
    subprocess.run(
        [FFMPEG, *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def make_tone_video(path: Path, duration: int = 5) -> None:
    """320x240 test pattern with a continuous tone — i.e. no silent gaps."""
    _ffmpeg(
        [
            "-y",
            "-f", "lavfi", "-i", f"testsrc=duration={duration}:size=320x240:rate=25",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-shortest", str(path),
        ]
    )


def make_gap_video(path: Path, duration: int = 5) -> None:
    """Same clip, but the audio is muted from 2s–4s, creating a real silent gap."""
    _ffmpeg(
        [
            "-y",
            "-f", "lavfi", "-i", f"testsrc=duration={duration}:size=320x240:rate=25",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
            "-filter:a", "volume=enable='between(t,2,4)':volume=0",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-shortest", str(path),
        ]
    )


def write_vtt(path: Path, cues: list[tuple[float, str]]) -> None:
    lines = ["WEBVTT", ""]
    for i, (start, text) in enumerate(cues, 1):
        end = start + 0.001
        lines += [
            str(i),
            f"{seconds_to_timestamp(start)} --> {seconds_to_timestamp(end)}",
            text,
            "",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")


def make_stub_tts(ad_seconds: float = 1.0):
    """Async stand-in for synthesize_tts that emits fixed-length silent clips.

    Returning a known duration is what makes the smart-mode placement and the
    output-duration assertions deterministic.
    """

    async def _stub(cues, tts_dir: Path):
        tts_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for cue in cues:
            clip = tts_dir / f"cue_{cue.index:03d}.wav"
            _ffmpeg(
                [
                    "-y",
                    "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                    "-t", f"{ad_seconds}", str(clip),
                ]
            )
            paths.append(clip)
        return paths

    return _stub


# ──────────────────────────────── tests ──────────────────────────────────


@unittest.skipUnless(FFMPEG and Path(FFMPEG).exists(), "ffmpeg binary not available")
class SmartRenderE2E(unittest.TestCase):
    def test_overflow_branch_extends_duration(self):
        """No silence in the source -> description overflows into a freeze-frame pause."""
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            video, vtt, out, work = (
                tmp / "in.mp4",
                tmp / "cues.vtt",
                tmp / "out.mp4",
                tmp / "work",
            )
            make_tone_video(video)
            write_vtt(vtt, [(2.5, "A description.")])

            with mock.patch.object(
                build_smart_ad_video, "synthesize_tts", make_stub_tts(ad_seconds=1.0)
            ):
                run_smart(video, vtt, out, work)

            # 1) valid, non-empty output
            self.assertTrue(out.exists())
            self.assertGreater(out.stat().st_size, 0)

            meta = probe_media(out)

            # 2) duration math: 5s source + 1s pause overflow ≈ 6s
            self.assertAlmostEqual(meta["duration"], 6.0, delta=0.5)

            # 3) stream properties preserved / correct
            self.assertEqual((meta["width"], meta["height"]), (320, 240))
            info = run_ffmpeg_probe(["-hide_banner", "-i", str(out)])
            self.assertIn("h264", info)
            self.assertIn("aac", info)
            self.assertIn("yuv420p", info)

    def test_fits_in_silence_branch_keeps_duration(self):
        """Description fits inside the source's silent gap -> length is unchanged."""
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            video, vtt, out, work = (
                tmp / "in.mp4",
                tmp / "cues.vtt",
                tmp / "out.mp4",
                tmp / "work",
            )
            make_gap_video(video)  # silent 2s–4s
            write_vtt(vtt, [(2.5, "A description.")])

            with mock.patch.object(
                build_smart_ad_video, "synthesize_tts", make_stub_tts(ad_seconds=1.0)
            ):
                run_smart(video, vtt, out, work)

            self.assertTrue(out.exists())
            self.assertGreater(out.stat().st_size, 0)

            meta = probe_media(out)

            # 1s description tucks into the 2s gap -> no added length
            self.assertAlmostEqual(meta["duration"], 5.0, delta=0.5)
            self.assertEqual((meta["width"], meta["height"]), (320, 240))


if __name__ == "__main__":
    unittest.main()
