"""job_status must surface a COMPLETED-but-rejected fal job as a terminal structured error.

Regression: a veo-3.1 request whose /status said COMPLETED but whose response URL answered
HTTP 422 {"detail":[{"type":"content_policy_violation", ...}]} was reported as {status: "pending"}
forever — the 4xx was classified as a transient fetch hiccup. The job is over at that point, so the
caller needs ok:false + error type + message + hint (and must never resubmit).
"""
import json
import os
import sys
import unittest
from unittest.mock import patch

import supercmo_env
import supercmo_skills
from supercmo_skills.providers import fal

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "mcp-server"))
from tools import jobs  # noqa: E402  (MCP binding over supercmo_skills.job_status)

POLICY_BODY = json.dumps({"detail": [{"type": "content_policy_violation",
                                      "msg": "Your request was flagged by a content checker."}]})
HANDLE = {"status": "pending", "provider": "fal", "capability": "video", "model": "veo-3.1",
          "request_id": "req-1", "status_url": "https://queue.fal.run/x/requests/req-1/status",
          "response_url": "https://queue.fal.run/x/requests/req-1", "output_dir": None}


class _Script:
    """Scripted stand-in for supercmo_env._request: pops (parsed, status, err) per call and records
    every call so the test can prove nothing was resubmitted."""

    def __init__(self, *responses):
        self.queue, self.calls = list(responses), []

    def __call__(self, method, url, body=None, headers=None, timeout=120, retries=None, meta=None):
        self.calls.append((method, url))
        return self.queue.pop(0)


def _completed_then(code, body):
    return _Script(({"status": "COMPLETED"}, 200, None), (None, code, body))


class QueueStatusRejectionTest(unittest.TestCase):
    def test_4xx_on_result_fetch_is_terminal_structured_error(self):
        with patch.object(supercmo_env, "_request", _completed_then(422, POLICY_BODY)):
            st = fal.video_status(HANDLE["status_url"], HANDLE["response_url"], "k")
        self.assertFalse(st["ok"])
        self.assertTrue(st["terminal"])
        self.assertNotIn("transient", st)
        self.assertEqual(st["error"], "content_policy_violation")
        self.assertEqual(st["status"], 422)
        self.assertIn("content checker", st["message"])
        self.assertIn("NEW request", st["hint"])

    def test_string_detail_and_unknown_type_still_terminal(self):
        with patch.object(supercmo_env, "_request", _completed_then(400, json.dumps({"detail": "bad"}))):
            st = fal.queue_status(HANDLE["status_url"], HANDLE["response_url"], "k")
        self.assertTrue(st["terminal"])
        self.assertEqual(st["error"], "result rejected (400)")
        self.assertEqual(st["message"], "bad")
        with patch.object(supercmo_env, "_request", _completed_then(403, "<html>forbidden</html>")):
            st = fal.queue_status(HANDLE["status_url"], HANDLE["response_url"], "k")
        self.assertTrue(st["terminal"])
        self.assertEqual(st["detail"], "<html>forbidden</html>")

    def test_retryable_result_fetch_stays_transient(self):
        for code in (408, 429, 500, 503, None):
            with patch.object(supercmo_env, "_request", _completed_then(code, "later")):
                st = fal.queue_status(HANDLE["status_url"], HANDLE["response_url"], "k")
            self.assertTrue(st.get("transient"), (code, st))
            self.assertFalse(st.get("terminal"), (code, st))


class JobStatusRejectionTest(unittest.TestCase):
    def setUp(self):
        self._env = patch.dict(os.environ, {"FAL_KEY": "k"})
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_single_check_returns_error_not_pending_handle(self):
        script = _completed_then(422, POLICY_BODY)
        with patch.object(supercmo_env, "_request", script):
            res = supercmo_skills.job_status(dict(HANDLE), wait=False)
        self.assertFalse(supercmo_skills.is_pending(res))
        self.assertFalse(supercmo_skills.job_ok(res))
        self.assertEqual(res["error"], "content_policy_violation")
        self.assertEqual([m for m, _ in script.calls], ["GET", "GET"])   # status + result; no POST

    def test_wait_loop_stops_on_rejection_without_resubmit(self):
        script = _completed_then(422, POLICY_BODY)
        with patch.object(supercmo_env, "_request", script), patch("time.sleep") as sleep:
            res = supercmo_skills.job_status(dict(HANDLE), wait=True, deadline_s=30)
        self.assertFalse(res["ok"])
        self.assertTrue(res["terminal"])
        self.assertEqual(len(script.calls), 2)              # stopped on first poll, queue drained
        sleep.assert_not_called()
        self.assertTrue(all(m == "GET" for m, _ in script.calls))

    def test_mcp_tool_surfaces_structured_error(self):
        with patch.object(supercmo_env, "_request", _completed_then(422, POLICY_BODY)):
            out = jobs.job_status({"jobs": [dict(HANDLE)]})
        self.assertFalse(out["ok"])
        self.assertNotIn("pending", out)                      # not reported as still generating
        r = out["results"][0]
        self.assertEqual(r["error"], "content_policy_violation")
        self.assertIn("content checker", r["message"])
        self.assertTrue(r["hint"])


if __name__ == "__main__":
    unittest.main()
