"""Regression: video_stitch must not stream-copy clips whose stream order differs.

ffmpeg's concat demuxer pairs streams across files by index. A clip written audio-first (stream 0 =
aac, stream 1 = h264 — Kling 3.0 output does this) concatenated after a video-first clip gets its
audio spliced into the video track, ffmpeg exits 0, and the "stitched" file reports ~65 s for three
6 s clips. Seen 2026-08-19 on the Rida reel. These tests build that exact layout and assert the
stitcher re-encodes instead and the result duration adds up. Skipped without ffmpeg.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import chdir, contextmanager
from pathlib import Path
from unittest.mock import patch

import supercmo_skills.stitch as stitch

HAVE_FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")
# Run from a plain temp dir (no content-agent.config.json) so outputs are the public defaults
# and the private-workspace boundary doesn't apply to the temp output path.
PUBLIC_ENV = {"SUPERCMO_OUTPUT_DIR": "", "SUPERCMO_SCRATCH_DIR": "",
              "SUPERCMO_CACHE_DIR": "", "SUPERCMO_PROJECTION_DIR": ""}


@contextmanager
def public_cwd(tmp):
    with chdir(tmp), patch.dict(os.environ, PUBLIC_ENV, clear=False):
        yield


def make_clip(path: Path, seconds: float, audio_first: bool) -> None:
    """A tiny h264+aac test clip; `audio_first` writes the aac stream as stream 0."""
    maps = ["-map", "1:a", "-map", "0:v"] if audio_first else ["-map", "0:v", "-map", "1:a"]
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=160x288:rate=24",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}:sample_rate=44100",
         *maps, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
        check=True,
    )


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not on PATH")
class StitchStreamOrderTests(unittest.TestCase):
    def test_layout_detects_stream_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            va = Path(tmp) / "va.mp4"
            av = Path(tmp) / "av.mp4"
            make_clip(va, 1.0, audio_first=False)
            make_clip(av, 1.0, audio_first=True)
            self.assertEqual(stitch._layout(str(va)), ["video", "audio"])
            self.assertEqual(stitch._layout(str(av)), ["audio", "video"])
            self.assertTrue(stitch._copy_safe([str(va), str(va)]))
            self.assertFalse(stitch._copy_safe([str(va), str(av)]))

    def test_mixed_stream_order_clips_stitch_to_the_right_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "a.mp4"
            b = Path(tmp) / "b.mp4"
            c = Path(tmp) / "c.mp4"
            make_clip(a, 2.0, audio_first=False)
            make_clip(b, 2.0, audio_first=True)
            make_clip(c, 2.0, audio_first=True)
            out = Path(tmp) / "out.mp4"
            with public_cwd(tmp):
                res = stitch.video_stitch([str(a), str(b), str(c)], output=str(out))
            self.assertTrue(res.get("ok"), res)
            self.assertAlmostEqual(res["duration"], 6.0, delta=0.6)
            # and the video stream really is ~6 s (not the audio-spliced 60+ s we saw)
            self.assertAlmostEqual(stitch._duration(str(out)), 6.0, delta=0.6)

    def test_same_order_clips_still_take_the_fast_copy_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "a.mp4"
            b = Path(tmp) / "b.mp4"
            make_clip(a, 2.0, audio_first=False)
            make_clip(b, 2.0, audio_first=False)
            out = Path(tmp) / "out.mp4"
            with public_cwd(tmp):
                res = stitch.video_stitch([str(a), str(b)], output=str(out))
            self.assertTrue(res.get("ok"), res)
            self.assertAlmostEqual(res["duration"], 4.0, delta=0.5)


if __name__ == "__main__":
    unittest.main()
