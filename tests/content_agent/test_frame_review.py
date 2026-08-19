"""Tests for skills/generating-videos/scripts/frame_review.py — the frame-level review gate.

These run the real script against a synthetic ffmpeg clip and assert that every frame a reviewer
must look at is actually produced and addressable by timestamp. Skipped when ffmpeg is absent.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "skills" / "generating-videos" / "scripts" / "frame_review.py"
RUBRIC = REPOSITORY / "skills" / "generating-videos" / "references" / "frame-review.md"
SKILL = REPOSITORY / "skills" / "generating-videos" / "SKILL.md"

HAVE_FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


def make_clip(path: Path, seconds: float = 4.0, size: str = "270x480") -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
         f"testsrc=duration={seconds}:size={size}:rate=24",
         "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not on PATH")
class FrameReviewScriptTests(unittest.TestCase):
    def run_script(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True, text=True, check=True,
        )

    def test_extracts_every_frame_at_requested_fps_and_builds_sheets(self):
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            make_clip(clip, seconds=4.0)
            out = Path(tmp) / "fr"
            res = self.run_script(str(clip), "--out", str(out), "--fps", "2", "--cols", "4", "--rows", "2")

            review_dir = out / "clip"
            manifest = json.loads((review_dir / "review.json").read_text())
            # 4 s at 2 fps -> 8 frames, every one addressable by timestamp
            self.assertEqual(manifest["frame_count"], 8)
            self.assertEqual([f["t"] for f in manifest["frames"]], [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5])
            for f in manifest["frames"]:
                self.assertTrue(os.path.isfile(f["path"]), f["path"])
                self.assertIn(f"t{f['t']:.2f}s", os.path.basename(f["path"]))
            # 8 frames on a 4x2 grid -> exactly one sheet, and its legend maps tiles to timestamps
            self.assertEqual(len(manifest["sheets"]), 1)
            self.assertTrue(os.path.isfile(manifest["sheets"][0]))
            self.assertEqual(manifest["sheet_legend"]["sheet_01.jpg"], [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5])
            self.assertEqual(manifest["grid"], {"cols": 4, "rows": 2, "order": "row-major"})
            self.assertEqual(manifest["rubric"], "references/frame-review.md")
            # last stdout line is a machine-readable summary
            summary = json.loads(res.stdout.strip().splitlines()[-1])
            self.assertEqual(summary["clips"][0]["frames"], 8)

    def test_partial_last_sheet_is_padded_not_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            make_clip(clip, seconds=3.0)  # 3 s @2fps = 6 frames
            out = Path(tmp) / "fr"
            self.run_script(str(clip), "--out", str(out), "--fps", "2", "--cols", "2", "--rows", "2")
            manifest = json.loads((out / "clip" / "review.json").read_text())
            self.assertEqual(manifest["frame_count"], 6)
            # 6 frames on 2x2 -> 2 sheets; second holds the 2 leftover frames (padded)
            self.assertEqual(len(manifest["sheets"]), 2)
            self.assertEqual(manifest["sheet_legend"]["sheet_02.jpg"], [2.0, 2.5])

    def test_crop_writes_one_crop_per_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            make_clip(clip, seconds=2.0)
            out = Path(tmp) / "fr"
            self.run_script(str(clip), "--out", str(out), "--fps", "2", "--crop", "0.25:0.25:0.5:0.5")
            manifest = json.loads((out / "clip" / "review.json").read_text())
            self.assertEqual(len(manifest["crops"]), manifest["frame_count"])
            for c in manifest["crops"]:
                self.assertTrue(os.path.isfile(c))

    def test_bad_crop_spec_fails_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            make_clip(clip, seconds=1.0)
            proc = subprocess.run(
                [sys.executable, str(SCRIPT), str(clip), "--out", tmp, "--crop", "nonsense"],
                capture_output=True, text=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("--crop must be", proc.stderr)


class FrameReviewContractTests(unittest.TestCase):
    """The skill must make the review a gate, and the rubric must cover the failure classes that
    actually bit us (anatomy, consistency, physics, scene/camera, direction)."""

    def test_skill_routes_every_clip_through_frame_review_before_return_and_stitch(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("references/frame-review.md", text)
        self.assertIn("scripts/frame_review.py", text)
        review_at = text.index("Step 7: Review the frames")
        return_at = text.index("Step 8: Return")
        self.assertLess(review_at, return_at)
        self.assertIn("never stitch around a failed shot", text)

    def test_rubric_names_the_failure_classes_and_timestamped_reporting(self):
        text = RUBRIC.read_text(encoding="utf-8")
        for needle in ("Anatomy", "Subject and product consistency", "Physics", "Scene and camera",
                       "Direction", "hard", "soft", "t=", "every frame"):
            self.assertIn(needle, text, needle)


if __name__ == "__main__":
    unittest.main()
