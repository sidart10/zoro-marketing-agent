"""Tests for the workspace-organization contract: active-project routing, the filename grammar,
and the hygiene checker. See contracts/content-agent/workspace-organization-v1.md."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import patch

import base64

from supercmo_skills import paths, client
from supercmo_skills.client import _sanitize_label, media_stem

REPOSITORY = Path(__file__).resolve().parents[2]
CHECKER = REPOSITORY / "scripts" / "check_workspace_hygiene.py"
ENVIRONMENT_PATHS = {
    "SUPERCMO_OUTPUT_DIR": "",
    "SUPERCMO_SCRATCH_DIR": "",
    "SUPERCMO_CACHE_DIR": "",
    "SUPERCMO_PROJECTION_DIR": "",
}


def make_active_root(tmp: str) -> Path:
    root = Path(tmp) / "content-agent"
    root.mkdir()
    (root / "content-agent.config.json").write_text(
        '{"schema_version":1,"canonical_root_name":"content-agent","workspace":"workspace"}\n',
        encoding="utf-8",
    )
    (root / "workspace" / "projects").mkdir(parents=True)
    (root / "workspace" / "media" / "generated").mkdir(parents=True)
    return root


class ActiveProjectRoutingTests(unittest.TestCase):
    def test_set_get_clear_and_output_routing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_active_root(tmp)
            with chdir(root), patch.dict(os.environ, ENVIRONMENT_PATHS, clear=False):
                self.assertIsNone(paths.active_project())
                base = paths.output_dir()
                self.assertTrue(base.endswith(os.path.join("media", "generated")))

                res = paths.set_active_project("riwayat-rida-suit-set")
                self.assertTrue(res["ok"], res)
                self.assertTrue((root / "workspace" / "projects" / "riwayat-rida-suit-set").is_dir())
                self.assertEqual(paths.active_project(), "riwayat-rida-suit-set")
                self.assertTrue(paths.output_dir().endswith(
                    os.path.join("media", "generated", "riwayat-rida-suit-set")))

                # explicit arg and env override still win over the project bucket
                explicit = str(root / "workspace" / "media" / "generated" / "elsewhere")
                self.assertEqual(Path(paths.output_dir(explicit)).resolve(), Path(explicit).resolve())

                cleared = paths.clear_active_project()
                self.assertTrue(cleared["ok"])
                self.assertIsNone(paths.active_project())
                self.assertEqual(paths.output_dir(), base)

    def test_invalid_slugs_are_rejected_and_bad_pointer_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_active_root(tmp)
            with chdir(root), patch.dict(os.environ, ENVIRONMENT_PATHS, clear=False):
                for bad in ("", "UPPER", "has space", "a", "-lead", "x" * 80, "dot.name"):
                    self.assertFalse(paths.set_active_project(bad)["ok"], bad)
                pointer = root / "workspace" / "projects" / ".active-project.json"
                pointer.write_text('{"slug": "NOT VALID"}', encoding="utf-8")
                self.assertIsNone(paths.active_project())
                pointer.write_text("not json", encoding="utf-8")
                self.assertIsNone(paths.active_project())
                self.assertTrue(paths.output_dir().endswith(os.path.join("media", "generated")))


class FilenameGrammarTests(unittest.TestCase):
    def test_sanitize_label(self):
        self.assertEqual(_sanitize_label("shot1-walkin"), "shot1-walkin")
        self.assertEqual(_sanitize_label("Shot 1 — Walk In!"), "shot-1-walk-in")
        self.assertEqual(_sanitize_label("  --weird--  "), "weird")
        self.assertIsNone(_sanitize_label("!!!"))
        self.assertIsNone(_sanitize_label(None))
        self.assertIsNone(_sanitize_label(42))
        self.assertLessEqual(len(_sanitize_label("x" * 200)), 48)

    def test_media_stem_grammar(self):
        self.assertEqual(media_stem("video", "kling-3.0-pro", "deadbeef"),
                         "video_kling-3.0-pro_deadbeef")
        self.assertEqual(media_stem("image", "gpt/image-2", "deadbeef", "endcard", 0),
                         "endcard_image_gpt-image-2_deadbeef_0")
        # produced names must satisfy the checker's grammar
        import re
        from check_workspace_hygiene import MEDIA_NAME_RE  # type: ignore
        for stem in (media_stem("video", "kling-3.0-pro", "0a1b2c3d", "shot1-walkin"),
                     media_stem("image", "nano-banana-pro", "0a1b2c3d", None, 2),
                     media_stem("audio", "eleven-v3", "0a1b2c3d")):
            self.assertTrue(MEDIA_NAME_RE.match(stem + ".mp4"), stem)


class PersistenceAndHandleTests(unittest.TestCase):
    """Offline (b64) persistence: naming grammar on disk, project routing, and the submit-time
    destination pin that keeps a mid-flight project switch from rerouting a finished job."""

    def test_persist_media_writes_labeled_file_into_active_project_bucket(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_active_root(tmp)
            with chdir(root), patch.dict(os.environ, ENVIRONMENT_PATHS, clear=False):
                paths.set_active_project("riwayat-test-set")
                result = {"ok": True, "model": "kling-3.0-pro",
                          "video": {"b64": base64.b64encode(b"clip-bytes").decode(),
                                    "content_type": "video/mp4"}}
                out = client._persist_media(result, None, "video", label="shot1-walkin")
                path = Path(out["video"]["path"])
                self.assertTrue(path.is_file())
                self.assertEqual(path.parent.name, "riwayat-test-set")
                self.assertRegex(path.name, r"^shot1-walkin_video_kling-3\.0-pro_[0-9a-f]{8}\.mp4$")
                self.assertNotIn("b64", out["video"])  # inline payload dropped once persisted

    def test_handle_pins_destination_at_submit_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_active_root(tmp)
            with chdir(root), patch.dict(os.environ, ENVIRONMENT_PATHS, clear=False):
                paths.set_active_project("project-a")

                class QueuedProvider:
                    BYOK_ENV = "FAL_KEY"
                    def video_submit(self, route, payload, key):
                        return {"ok": True, "request_id": "r1",
                                "status_url": "https://q/e/s", "response_url": "https://q/e"}

                handle = client._submit_or_run(
                    "video", "kling-3.0-pro", {"prompt": "x"}, "direct", QueuedProvider(),
                    "route", None, wait=False, deadline_s=None, label="Shot 2!")
                self.assertEqual(handle.get("status"), "pending")
                self.assertEqual(handle.get("label"), "shot-2")
                self.assertTrue(handle["output_dir"].endswith(os.path.join("media", "generated", "project-a")))

                # the user switches projects while the job renders...
                paths.set_active_project("project-b")
                result = {"ok": True, "model": "kling-3.0-pro",
                          "video": {"b64": base64.b64encode(b"clip").decode(),
                                    "content_type": "video/mp4"}}
                out = client._persist_media(result, handle["output_dir"], "video", handle.get("label"))
                # ...and the finished clip still lands where it was aimed, label intact
                self.assertEqual(Path(out["video"]["path"]).parent.name, "project-a")
                self.assertTrue(Path(out["video"]["path"]).name.startswith("shot-2_video_"))

    def test_media_stem_sanitizes_hostile_model_ids(self):
        self.assertEqual(media_stem("video", "wan_2.7 (beta)!", "0a1b2c3d"),
                         "video_wan-2.7-beta_0a1b2c3d")
        from check_workspace_hygiene import MEDIA_NAME_RE  # type: ignore
        self.assertTrue(MEDIA_NAME_RE.match(media_stem("video", "weird/model_v2", "0a1b2c3d") + ".mp4"))


class HygieneCheckerTests(unittest.TestCase):
    def run_checker(self, workspace: Path, *flags):
        proc = subprocess.run(
            [sys.executable, str(CHECKER), "--workspace", str(workspace), "--json", *flags],
            capture_output=True, text=True, cwd=workspace.parent,
        )
        try:
            return proc.returncode, json.loads(proc.stdout)
        except json.JSONDecodeError:
            self.fail(f"checker produced no JSON: rc={proc.returncode} out={proc.stdout!r} err={proc.stderr!r}")

    def test_clean_workspace_passes_and_violations_are_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_active_root(tmp)
            ws = root / "workspace"
            for d in ("archive", "cache", "channels", "evaluations", "library",
                      "migrations", "projections", "secrets"):
                (ws / d).mkdir(exist_ok=True)
            project = ws / "projects" / "riwayat-test-set"
            project.mkdir()
            (project / "README.md").write_text("# notes\n", encoding="utf-8")
            (project / "FINAL_test.mp4").write_bytes(b"x")
            (ws / "media" / "generated" / "riwayat-test-set").mkdir()

            rc, report = self.run_checker(ws)
            self.assertEqual(rc, 0, report)
            self.assertEqual(report["errors"], [])
            self.assertEqual(report["warnings"], [])

            # structural violation: stray root entry -> error (rc 1)
            (ws / "random-stuff").mkdir()
            rc, report = self.run_checker(ws)
            self.assertEqual(rc, 1)
            self.assertTrue(any("random-stuff" in e for e in report["errors"]))
            (ws / "random-stuff").rmdir()

            # curation debt: a tool workdir at the inbox root -> warning, not error
            (ws / "media" / "generated" / "frame_review").mkdir()
            rc, report = self.run_checker(ws)
            self.assertEqual(rc, 0, report)
            self.assertTrue(any("frame_review" in w for w in report["warnings"]))
            (ws / "media" / "generated" / "frame_review").rmdir()

            # curation debt: loose inbox file -> warning (rc 0, rc 2 under --strict)
            (ws / "media" / "generated" / "video_kling-3.0-pro_0a1b2c3d.mp4").write_bytes(b"x")
            rc, report = self.run_checker(ws)
            self.assertEqual(rc, 0)
            self.assertTrue(any("loose file" in w for w in report["warnings"]))
            rc, _ = self.run_checker(ws, "--strict")
            self.assertEqual(rc, 2)

            # a project without README/FINAL is flagged as debt, not an error
            (ws / "projects" / "riwayat-bare").mkdir()
            rc, report = self.run_checker(ws)
            self.assertEqual(rc, 0)
            self.assertTrue(any("riwayat-bare" in w and "README" in w for w in report["warnings"]))

            # non-secret-shaped file in secrets/ -> error
            (ws / "secrets" / "notes.txt").write_text("hi", encoding="utf-8")
            rc, report = self.run_checker(ws)
            self.assertEqual(rc, 1)
            self.assertTrue(any("secrets" in e for e in report["errors"]))


if __name__ == "__main__":
    unittest.main()
