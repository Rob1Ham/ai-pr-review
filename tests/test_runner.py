import json
import os
from pathlib import Path
import sys
import subprocess
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_pr_review.common import CampaignError
from ai_pr_review.config import load_config
from ai_pr_review.runner import OpenCodeRunner, credential_status, isolated_environment, make_config, validate_report, _parse_events, _session_routing, _routing_from_db

ROOT = Path(__file__).resolve().parents[1]


class RunnerTests(unittest.TestCase):
    def test_routing_reads_only_metadata_for_large_sessions(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "transport.db"
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE session (id TEXT)")
                db.execute("CREATE TABLE message (id TEXT, session_id TEXT, data TEXT)")
                db.execute("INSERT INTO session VALUES ('ses_1')")
                db.execute("INSERT INTO message VALUES (?, ?, ?)", ("msg1", "ses_1", json.dumps({
                    "role": "assistant", "providerID": "openai", "modelID": "test", "unused": "x" * 100000})))
            routing = _routing_from_db(path, [{"type": "step_start", "part": {"messageID": "msg1"}}], "ses_1")
            self.assertEqual(routing[0]["modelID"], "test")
            self.assertLess(len(json.dumps(routing)), 200)

    def test_single_fenced_report_is_accepted_but_ambiguous_reports_are_not(self):
        events = [{"type": "text", "sessionID": "session", "providerID": "p", "modelID": "m",
                   "part": {"text": 'Review complete.\n```json\n{"schema_version":1}\n```'}},
                  {"type": "step_finish", "part": {"reason": "stop"}}]
        self.assertEqual(_parse_events("\n".join(map(json.dumps, events)).encode())[0], {"schema_version": 1})
        events[0]["part"]["text"] *= 2
        with self.assertRaisesRegex(CampaignError, "exactly one"):
            _parse_events("\n".join(map(json.dumps, events)).encode())
        events[0]["part"]["text"] = '{"schema_version":1}'
        events[1]["part"]["reason"] = "length"
        with self.assertRaisesRegex(CampaignError, "incompletely"):
            _parse_events("\n".join(map(json.dumps, events)).encode())

    def test_export_routing_must_cover_exact_transport_messages(self):
        events = [{"type": "step_start", "part": {"messageID": "msg1"}}]
        exported = {"info": {"id": "ses_1"}, "messages": [{"info": {
            "id": "msg1", "role": "assistant", "sessionID": "ses_1",
            "modelID": "gpt-test", "providerID": "openai"}}]}
        self.assertEqual(_session_routing(exported, events, "ses_1")[0]["modelID"], "gpt-test")
        with self.assertRaisesRegex(CampaignError, "does not match"):
            _session_routing(exported, events, "ses_other")
        exported["messages"] = []
        with self.assertRaisesRegex(CampaignError, "missing assistant"):
            _session_routing(exported, events, "ses_1")

    def test_search_permissions_keep_external_boundary_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = make_config({"provider": "openai", "model": "openai/test", "effort": "max"}, root)
            rules = config["permission"]
            self.assertEqual(rules["read"][str(root / "**").lstrip("/")], "allow")
            self.assertEqual(rules["external_directory"], "deny")
            self.assertEqual(rules["bash"], "deny")
            self.assertEqual(rules["*"], "deny")

    def test_tool_steps_are_not_terminal_failures(self):
        events = [{"type": "step_finish", "sessionID": "session", "providerID": "openai", "modelID": "model",
                   "part": {"reason": "tool-calls"}},
                  {"type": "text", "part": {"text": '{"schema_version": 1}'}},
                  {"type": "step_finish", "part": {"reason": "stop"}}]
        self.assertEqual(_parse_events("\n".join(map(json.dumps, events)).encode())[1:4],
                         ("openai", "model", "session"))
        events[-1]["part"]["reason"] = "tool-calls"
        with self.assertRaisesRegex(CampaignError, "final JSON report and stop"):
            _parse_events("\n".join(map(json.dumps, events)).encode())

    def test_auth_file_is_explicit_only_and_provider_filtered(self):
        config = load_config(ROOT / "campaigns" / "example-release.json"); lane = config.lanes[0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); auth = root / "auth.json"
            auth.write_text(json.dumps({"openai": {"type": "oauth", "refresh": "test"}, "fireworks-ai": {"type": "api", "key": "other"}}))
            with patch.dict(os.environ, {"AI_PR_REVIEW_OPENCODE_AUTH_FILE": str(auth)}, clear=True):
                env = isolated_environment(root / "home", make_config(lane, root), lane)
                status = credential_status([lane])
            copied = json.loads((Path(env["XDG_DATA_HOME"]) / "opencode" / "auth.json").read_text())
            self.assertEqual(set(copied), {"openai"})
            self.assertTrue(status[0]["available"])
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(credential_status([lane])[0]["available"])

    def test_relative_auth_path_rejected(self):
        config = load_config(ROOT / "campaigns" / "example-release.json")
        with patch.dict(os.environ, {"AI_PR_REVIEW_OPENCODE_AUTH_FILE": "relative.json"}, clear=True):
            with self.assertRaisesRegex(CampaignError, "absolute"): credential_status(config.lanes)

    def test_coverage_coordinate_must_match_assigned_path(self):
        job = {"id": "job", "run_id": "run", "stage": "pr", "pr": 1,
               "base_sha": "a" * 40, "head_sha": "b" * 40, "campaign_head_sha": "b" * 40,
               "input_digest": "input", "prompt_digest": "prompt", "expected_units": ["unit"],
               "expected_unit_paths": {"unit": "src/right.py"}}
        report = {"schema_version": 1, "job_id": "job", "run_id": "run", "stage": "pr", "pr": 1,
                  "base_sha": "a" * 40, "head_sha": "b" * 40, "input_digest": "input",
                  "prompt_digest": "prompt", "status": "complete", "complete": True,
                  "coverage": {"units": [{"id": "unit", "method": "static",
                                             "evidence": ["b" * 40 + ":src/wrong.py:1-1"]}], "gaps": []},
                  "findings": [], "errors": []}
        with self.assertRaisesRegex(CampaignError, "assigned file"):
            validate_report(report, job)
        report["coverage"]["units"][0]["evidence"].append("b" * 40 + ":src/right.py:1-1")
        # Context evidence must not disqualify coverage when the assigned source
        # is also cited. Context-only evidence is still rejected above.
        validate_report(report, job)

    def test_no_progress_timeout_kills_process(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
        limits = {"max_output_bytes": 1000, "timeout_s": 2, "no_progress_s": .1}
        with self.assertRaisesRegex(CampaignError, "no progress"):
            OpenCodeRunner._communicate(process, b"prompt", limits, threading.Event())
        self.assertIsNotNone(process.poll())


if __name__ == "__main__": unittest.main()
