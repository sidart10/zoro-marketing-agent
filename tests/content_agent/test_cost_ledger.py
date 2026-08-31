"""Tests for generation-cost capture: the `x-fal-billable-units` header → the result's `billing`
block → the workspace cost ledger (media/generated/.cost-ledger.jsonl) → per-project summary.
See contracts/content-agent/workspace-organization-v1.md ("Cost ledger")."""
import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from contextlib import chdir
from pathlib import Path
from unittest.mock import patch

import supercmo_env
from supercmo_skills import catalog, client, paths
from supercmo_skills.providers import fal

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


def billed_video_result(units: float | None = 293.625) -> dict:
    result = {"ok": True, "model": "seedance-2.0",
              "video": {"b64": base64.b64encode(b"clip-bytes").decode(),
                        "content_type": "video/mp4"}}
    if units is not None:
        result["billing"] = {"provider": "fal", "billable_units": units,
                             "usd_estimate": None, "price_basis": None}
    return result


def read_ledger(workspace: Path) -> list[dict]:
    ledger = workspace / "media" / "generated" / paths.COST_LEDGER_NAME
    if not ledger.is_file():
        return []
    return [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line]


class HeaderCaptureTests(unittest.TestCase):
    """supercmo_env._request/_request_raw expose response headers via the optional `meta` dict
    without changing their return shapes."""

    class _Response:
        status = 200
        headers = {"X-Fal-Billable-Units": "245.025", "Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"ok": true}'

    def test_request_fills_meta_headers_lowercased(self):
        with patch.object(urllib.request, "urlopen", return_value=self._Response()):
            meta = {}
            parsed, status, err = supercmo_env._request("GET", "https://queue.fal.test/r", meta=meta)
            self.assertEqual((parsed, status, err), ({"ok": True}, 200, None))
            self.assertEqual(meta["headers"]["x-fal-billable-units"], "245.025")

    def test_request_without_meta_is_unchanged(self):
        with patch.object(urllib.request, "urlopen", return_value=self._Response()):
            parsed, status, err = supercmo_env._request("GET", "https://queue.fal.test/r")
            self.assertEqual((parsed, status, err), ({"ok": True}, 200, None))

    def test_request_raw_fills_meta_headers(self):
        response = self._Response()
        response.headers = {"X-Fal-Billable-Units": "1.5", "Content-Type": "video/mp4"}
        with patch.object(urllib.request, "urlopen", return_value=response):
            meta = {}
            data, ctype, status, err = supercmo_env._request_raw(
                "GET", "https://cdn.test/v.mp4", meta=meta)
            self.assertEqual((data, status, err), (b'{"ok": true}', 200, None))
            self.assertEqual(meta["headers"]["x-fal-billable-units"], "1.5")


class FalBillableUnitsTests(unittest.TestCase):
    """The fal provider carries x-fal-billable-units through sync and queued completions."""

    def _stub(self, script):
        def stub(method, url, body=None, headers=None, timeout=120, retries=None, meta=None):
            item = script.pop(0)
            if meta is not None and len(item) > 3:
                meta["headers"] = item[3]
            return item[:3]
        return stub

    def test_sync_image_generate_attaches_units(self):
        route = {"provider": "fal", "id": "fal-ai/nano-banana", "edit_id": "fal-ai/nano-banana/edit",
                 "max_refs": 4, "size_style": "aspect_ratio", "sizes": {"1:1": "1:1"},
                 "defaults": {}, "supports": {"prompt", "aspect_ratio"}}
        script = [({"images": [{"url": "https://cdn/i.png"}], "seed": 7}, 200, None,
                   {"x-fal-billable-units": "3.9"})]
        with patch.object(supercmo_env, "_request", self._stub(script)):
            out = fal.image_generate(route, {"model": "nano-banana", "prompt": "x"}, "k")
        self.assertTrue(out["ok"])
        self.assertEqual(out["billable_units"], 3.9)

    def test_queue_result_fetch_attaches_units_to_done_status(self):
        script = [({"status": "COMPLETED"}, 200, None),
                  ({"video": {"url": "https://cdn/v.mp4", "duration": 6}}, 200, None,
                   {"x-fal-billable-units": "293.625"})]
        with patch.object(supercmo_env, "_request", self._stub(script)):
            st = fal.video_status("https://q/s", "https://q/r", "k")
        self.assertTrue(st["ok"] and st["done"])
        self.assertEqual(st["billable_units"], 293.625)

    def test_missing_or_garbage_header_is_simply_absent(self):
        for headers in ({}, {"x-fal-billable-units": "n/a"}):
            script = [({"status": "COMPLETED"}, 200, None),
                      ({"video": {"url": "https://cdn/v.mp4"}}, 200, None, headers)]
            with patch.object(supercmo_env, "_request", self._stub(script)):
                st = fal.video_status("https://q/s", "https://q/r", "k")
            self.assertTrue(st["ok"], st)
            self.assertNotIn("billable_units", st)


class AttachBillingTests(unittest.TestCase):
    def test_billing_block_shape_and_null_usd_when_unpriced(self):
        res = {"ok": True, "billable_units": 245.025}
        with patch.dict(catalog.FAL_UNIT_USD, {}, clear=True):
            client._attach_billing(res, "seedance-2.0")
        self.assertNotIn("billable_units", res)
        self.assertEqual(res["billing"], {"provider": "fal", "billable_units": 245.025,
                                          "usd_estimate": None, "price_basis": None})

    def test_usd_estimate_from_catalog_table_only(self):
        with patch.dict(catalog.FAL_UNIT_USD,
                        {"seedance-2.0": {"usd_per_unit": 0.01, "basis": "test-fixture"}}):
            res = {"ok": True, "billable_units": 293.625}
            client._attach_billing(res, "seedance-2.0")
            self.assertEqual(res["billing"]["usd_estimate"], 2.9363)
            self.assertEqual(res["billing"]["price_basis"], "test-fixture")

    def test_no_units_means_no_billing_block(self):
        res = {"ok": True}
        client._attach_billing(res, "seedance-2.0")
        self.assertNotIn("billing", res)


class LedgerAppendTests(unittest.TestCase):
    def test_persist_media_appends_ledger_line_with_project_attribution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_active_root(tmp)
            with chdir(root), patch.dict(os.environ, ENVIRONMENT_PATHS, clear=False):
                paths.set_active_project("riwayat-test-set")
                out = client._persist_media(billed_video_result(), None, "video",
                                            label="shot1-walkin", request_id="req-42")
                self.assertTrue(out["ok"])
                lines = read_ledger(root / "workspace")
                self.assertEqual(len(lines), 1)
                entry = lines[0]
                self.assertEqual(entry["capability"], "video")
                self.assertEqual(entry["model"], "seedance-2.0")
                self.assertEqual(entry["label"], "shot1-walkin")
                self.assertEqual(entry["project_slug"], "riwayat-test-set")
                self.assertEqual(entry["billable_units"], 293.625)
                self.assertIsNone(entry["usd_estimate"])
                self.assertEqual(entry["request_id"], "req-42")
                self.assertEqual(len(entry["files"]), 1)
                self.assertRegex(entry["files"][0],
                                 r"^shot1-walkin_video_seedance-2\.0_[0-9a-f]{8}$")
                # the stem must name the file that actually landed in the project bucket
                landed = root / "workspace" / "media" / "generated" / "riwayat-test-set"
                self.assertTrue((landed / (entry["files"][0] + ".mp4")).is_file())

    def test_attribution_follows_the_landing_bucket_not_the_pointer(self):
        """A handle pins its destination at submit time; if the pointer moves mid-flight, the
        ledger attributes the generation to where the files actually landed."""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_active_root(tmp)
            with chdir(root), patch.dict(os.environ, ENVIRONMENT_PATHS, clear=False):
                paths.set_active_project("project-a")
                pinned = paths.output_dir()
                paths.set_active_project("project-b")   # user switches while the job renders
                client._persist_media(billed_video_result(), pinned, "video")
                lines = read_ledger(root / "workspace")
                self.assertEqual(lines[0]["project_slug"], "project-a")

    def test_no_pointer_and_bare_inbox_logs_null_slug(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_active_root(tmp)
            with chdir(root), patch.dict(os.environ, ENVIRONMENT_PATHS, clear=False):
                client._persist_media(billed_video_result(), None, "video")
                lines = read_ledger(root / "workspace")
                self.assertEqual(len(lines), 1)
                self.assertIsNone(lines[0]["project_slug"])

    def test_generation_without_billing_still_logs_a_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_active_root(tmp)
            with chdir(root), patch.dict(os.environ, ENVIRONMENT_PATHS, clear=False):
                client._persist_media(billed_video_result(units=None), None, "video")
                lines = read_ledger(root / "workspace")
                self.assertEqual(len(lines), 1)
                self.assertIsNone(lines[0]["billable_units"])
                self.assertNotIn("request_id", lines[0])

    def test_ledger_fault_never_fails_the_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_active_root(tmp)
            with chdir(root), patch.dict(os.environ, ENVIRONMENT_PATHS, clear=False), \
                    patch.object(paths, "cost_ledger_path",
                                 side_effect=RuntimeError("ledger exploded")):
                out = client._persist_media(billed_video_result(), None, "video")
                self.assertTrue(out["ok"])
                self.assertTrue(Path(out["video"]["path"]).is_file())

    def test_no_workspace_means_no_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / "plain"
            plain.mkdir()
            env = dict(ENVIRONMENT_PATHS, SUPERCMO_OUTPUT_DIR=str(plain / "media"))
            with chdir(plain), patch.dict(os.environ, env, clear=False):
                out = client._persist_media(billed_video_result(), None, "video")
                self.assertTrue(out["ok"])
                self.assertEqual(list(plain.rglob(paths.COST_LEDGER_NAME)), [])


class CostSummaryTests(unittest.TestCase):
    def test_sums_per_project_and_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_active_root(tmp)
            ledger = root / "workspace" / "media" / "generated" / paths.COST_LEDGER_NAME
            ledger.write_text("\n".join([
                json.dumps({"capability": "video", "model": "seedance-2.0", "project_slug": "a",
                            "billable_units": 293.625, "usd_estimate": 2.94, "files": []}),
                json.dumps({"capability": "video", "model": "seedance-2.0", "project_slug": "a",
                            "billable_units": 245.025, "usd_estimate": None, "files": []}),
                json.dumps({"capability": "image", "model": "nano-banana", "project_slug": None,
                            "billable_units": None, "usd_estimate": None, "files": []}),
                "not json — skipped",
            ]) + "\n", encoding="utf-8")
            with chdir(root), patch.dict(os.environ, ENVIRONMENT_PATHS, clear=False), \
                    patch.dict(catalog.FAL_UNIT_USD,
                               {"seedance-2.0": {"usd_per_unit": 0.01, "basis": "test-fixture"}},
                               clear=True):
                summary = client.cost_summary()
                self.assertTrue(summary["ok"])
                by_project = {row["project"]: row for row in summary["projects"]}
                self.assertEqual(by_project["a"]["generations"], 2)
                self.assertEqual(by_project["a"]["billable_units"], 538.65)
                # first line keeps its recorded 2.94; the null-usd line is REPRICED from the
                # current table (245.025 x 0.01) instead of staying unpriced forever
                self.assertEqual(by_project["a"]["usd_estimate"], 5.3902)
                self.assertEqual(by_project["a"]["unpriced"], 0)
                # a model with no table entry (and no units) still counts as unpriced
                self.assertEqual(by_project["(inbox)"]["generations"], 1)
                self.assertEqual(by_project["(inbox)"]["unpriced"], 1)
                self.assertEqual(summary["totals"]["generations"], 3)

                filtered = client.cost_summary("a")
                self.assertEqual([row["project"] for row in filtered["projects"]], ["a"])
                self.assertEqual(filtered["totals"]["generations"], 2)

    def test_empty_ledger_is_ok_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_active_root(tmp)
            with chdir(root), patch.dict(os.environ, ENVIRONMENT_PATHS, clear=False):
                summary = client.cost_summary()
                self.assertTrue(summary["ok"])
                self.assertEqual(summary["projects"], [])
                self.assertEqual(summary["totals"]["generations"], 0)


class HygieneCheckerLedgerTests(unittest.TestCase):
    def test_ledger_file_is_expected_not_a_loose_file_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_active_root(tmp)
            ws = root / "workspace"
            for d in ("archive", "cache", "channels", "evaluations", "library",
                      "migrations", "projections", "secrets"):
                (ws / d).mkdir(exist_ok=True)
            (ws / "media" / "generated" / paths.COST_LEDGER_NAME).write_text(
                json.dumps({"capability": "video"}) + "\n", encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(CHECKER), "--workspace", str(ws), "--json"],
                capture_output=True, text=True, cwd=ws.parent,
            )
            report = json.loads(proc.stdout)
            self.assertEqual(proc.returncode, 0, report)
            self.assertEqual(report["errors"], [])
            self.assertEqual(report["warnings"], [])


if __name__ == "__main__":
    unittest.main()
