"""video_stitch must emit square pixels (SAR 1:1) so DAR == width:height, even when one clip
carries a non-square SAR at the same pixel size as the others (a stream-copy concat would leak the
first clip's SAR over the whole output and the video plays squashed)."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import supercmo_skills.stitch as stitch


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def _geometry(path):
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,sample_aspect_ratio,display_aspect_ratio",
         "-of", "json", path],
        capture_output=True, text=True, timeout=30)
    return json.loads(out.stdout)["streams"][0]


def _make_clip(path, size, sar=None):
    """Synthetic 1 s clip with audio; `sar` (e.g. '16:9') stamps a non-square sample aspect."""
    vf = f"setsar={sar}" if sar else "setsar=1"
    subprocess.run(
        [FFMPEG, "-v", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=10:duration=1",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-vf", vf, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", path],
        check=True, capture_output=True, timeout=120)


class ScaleChainTests(unittest.TestCase):
    def test_scale_chain_normalises_sar_and_fits_without_stretching(self):
        chain = stitch._scale_chain(1080, 1920)
        self.assertTrue(chain.startswith("scale=iw*sar:ih,setsar=1,"))     # square pixels first
        self.assertIn("scale=1080:1920:force_original_aspect_ratio=decrease", chain)
        self.assertIn("pad=1080:1920:(ow-iw)/2:(oh-ih)/2", chain)           # letterbox, no stretch
        self.assertTrue(chain.endswith(",setsar=1"))                        # output SAR 1:1

    def test_sar_normalisation(self):
        for raw in (None, "", "N/A", "0:1", "1:1"):
            self.assertEqual(stitch._sar(raw), "1:1")
        self.assertEqual(stitch._sar("16:9"), "16:9")

    def test_same_size_different_sar_takes_scaled_path(self):
        """Equal WxH but different SAR must re-encode, not stream-copy."""
        geometries = iter([(1080, 1920, "16:9"), (1080, 1920, "1:1")])
        calls = {}

        def scaled(ffmpeg, clips, target, out):
            calls["target"] = target
            Path(out).write_bytes(b"stitched")
            return 0, ""

        with tempfile.TemporaryDirectory() as tmp:
            clips = [os.path.join(tmp, "a.mp4"), os.path.join(tmp, "b.mp4")]
            for c in clips:
                Path(c).write_bytes(b"clip")
            with (
                patch("supercmo_skills.stitch.shutil.which", return_value="ffmpeg"),
                patch("supercmo_skills.stitch._res", side_effect=lambda p: next(geometries)),
                patch("supercmo_skills.stitch._concat_copy") as copy,
                patch("supercmo_skills.stitch._concat_scaled", side_effect=scaled),
                patch("supercmo_skills.stitch._probe", return_value=(None, None, 8)),
                patch("supercmo_skills.stitch.paths.scratch_dir", return_value=tmp),
                patch("supercmo_skills.stitch.paths.output_dir", side_effect=lambda p=None: p or tmp),
            ):
                result = stitch.video_stitch(clips, output=os.path.join(tmp, "out.mp4"))
        self.assertTrue(result["ok"], result)
        copy.assert_not_called()
        self.assertEqual(tuple(calls["target"][:2]), (1080, 1920))


@unittest.skipUnless(FFMPEG and FFPROBE, "ffmpeg/ffprobe not installed")
class StitchAspectEndToEndTests(unittest.TestCase):
    def test_mixed_sar_clips_stitch_to_square_pixels(self):
        with tempfile.TemporaryDirectory() as tmp:
            square = os.path.join(tmp, "square.mp4")
            odd = os.path.join(tmp, "odd.mp4")                   # 1080x1920 pixels, SAR 16:9, DAR 1:1
            normal = os.path.join(tmp, "normal.mp4")             # 1080x1920 pixels, SAR 1:1, DAR 9:16
            _make_clip(square, "1440x1440")
            subprocess.run([FFMPEG, "-v", "error", "-y", "-i", square, "-vf", "scale=1080:1920",
                            "-c:a", "copy", odd], check=True, capture_output=True, timeout=120)
            _make_clip(normal, "1080x1920")
            self.assertEqual(_geometry(odd)["sample_aspect_ratio"], "16:9")   # repro precondition
            self.assertEqual(stitch._res(odd), (1080, 1920, "16:9"))

            with (
                patch("supercmo_skills.stitch.paths.scratch_dir", return_value=tmp),
                patch("supercmo_skills.stitch.paths.output_dir", side_effect=lambda p=None: p or tmp),
            ):
                result = stitch.video_stitch([odd, normal], output=os.path.join(tmp, "out.mp4"))
            self.assertTrue(result["ok"], result)
            g = _geometry(result["path"])
            self.assertEqual((g["width"], g["height"]), (1080, 1920))
            self.assertEqual(g["sample_aspect_ratio"], "1:1")
            self.assertEqual(g["display_aspect_ratio"], "9:16")


if __name__ == "__main__":
    unittest.main()
