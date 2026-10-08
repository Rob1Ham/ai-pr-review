import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ai_pr_review.common import CampaignError
from ai_pr_review.prompts import PROMPT_DIGEST, ISSUE_PROMPT_DIGEST, build_prompt
from ai_pr_review.publish import Publisher
from ai_pr_review.severity import estimate_severity


HEAD = "a" * 40


def finding():
    return {"local_id": "one", "severity": "high", "confidence": "high", "confidence_rationale": "Direct code evidence",
            "root_cause": "Unchecked state", "broken_invariant": "State must be validated", "expected": "Error returned",
            "observed": "Unchecked access", "impact": "Bounded service outage", "remediation_direction": "Validate state",
            "persists_at_head": "present", "checked_head_sha": HEAD,
            "evidence": [{"commit": HEAD, "path": "src/app.py", "line_start": 1, "line_end": 1}],
            "severity_assessment": {"impact": "high", "likelihood": "low", "confidence": "medium",
                "rationale": "Major impact requires an uncommon deployment configuration.",
                "assumptions": ["That deployment configuration is enabled."]}}


class SeverityTests(unittest.TestCase):
    def test_matrix_assigns_severity_and_preserves_model_proposal(self):
        estimate = estimate_severity(finding(), required=True)
        self.assertEqual(estimate["severity"], "medium")
        self.assertEqual(estimate["model_proposed_severity"], "high")
        value = finding()
        value["severity_assessment"].update(impact="critical", likelihood="high")
        self.assertEqual(estimate_severity(value)["severity"], "critical")
        value["severity_assessment"]["likelihood"] = "low"
        self.assertEqual(estimate_severity(value)["severity"], "high")

    def test_missing_and_malformed_assessments_are_not_guessed(self):
        value = finding()
        del value["severity_assessment"]
        self.assertFalse(estimate_severity(value)["complete"])
        self.assertIsNone(estimate_severity(value)["severity"])
        with self.assertRaisesRegex(CampaignError, "structured"):
            estimate_severity(value, required=True)
        for bad in ("certain", [], None):
            value = finding()
            value["severity_assessment"]["likelihood"] = bad
            with self.assertRaises(CampaignError): estimate_severity(value)

    def test_issue_prompt_requests_rubric_without_changing_frozen_pr_prompt(self):
        self.assertEqual(PROMPT_DIGEST, "7d472f65bb939682e55372d5535b48a36bf295655f3a84720f0a57f5bb929083")
        job = {"tier": "standard", "expected_units": [], "prompt_digest": PROMPT_DIGEST}
        self.assertNotIn("SEVERITY_RUBRIC=", build_prompt(job, {}))
        job["prompt_digest"] = ISSUE_PROMPT_DIGEST
        prompt = build_prompt(job, {})
        self.assertIn("SEVERITY_RUBRIC=", prompt)
        self.assertIn("severity_assessment", prompt)

    @patch("ai_pr_review.publish.read_evidence", return_value="value = 1")
    def test_issue_filing_requires_assessment_and_renders_estimated_severity(self, _read):
        with tempfile.TemporaryDirectory() as temporary:
            remote = Mock(mode="github")
            remote.issues.return_value = []
            remote.create.return_value = {"number": 1, "url": "https://example.invalid/issues/1"}
            manifest = {"repository": "test/repo", "repository_path": temporary, "head_sha": HEAD}
            job = {"id": "job", "run_id": "run", "pr": 1, "head_sha": HEAD, "base_sha": HEAD,
                   "lane": {"model": "openai/test"}}
            value = finding()
            legacy = copy.deepcopy(value)
            del legacy["severity_assessment"]
            publisher = Publisher(Path(temporary), manifest, remote)
            publisher.submit(job, {"provenance": {"mode": "live"}, "report": {"findings": [legacy]}})
            remote.create.assert_not_called()
            self.assertEqual(publisher.summary()["claims"], {"needs-severity-assessment": 1})
            publisher.submit(job, {"provenance": {"mode": "live"}, "report": {"findings": [value]}})
            title, body = remote.create.call_args.args
            self.assertIn("[medium]", title)
            self.assertIn("Assigned severity: **medium**", body)
            self.assertIn("Likelihood: **low**", body)
            self.assertIn("Assessment confidence: **medium**", body)
            self.assertIn("uncommon deployment configuration", body)
            saved = next(iter(publisher.data["claims"].values()))
            self.assertEqual(saved["severity_estimate"]["severity"], "medium")
