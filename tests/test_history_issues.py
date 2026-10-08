from pathlib import Path
import unittest
from unittest.mock import patch

from ai_pr_review.history_issues import render_history_issue


class HistoryIssueTests(unittest.TestCase):
    @patch("ai_pr_review.history_issues.read_evidence", return_value="if unsafe { fail(); }\n")
    def test_requested_title_and_comprehensive_body(self, _read):
        head = "a" * 40
        finding = {"severity": "high", "confidence": "medium", "eli5": "A safety gate can open at the wrong time.",
                   "confidence_rationale": "The branch is directly visible, but runtime state is unknown.",
                   "root_cause": "Safety gate checks stale state", "broken_invariant": "Only current state may authorize.",
                   "expected": "Read current state.", "observed": "A cached state is used.", "impact": "An invalid action can proceed.",
                   "preconditions": ["The cache is stale."], "remediation_direction": "Reload before authorizing.",
                   "validation_gap": "No runtime test was executed.", "persists_at_head": "present", "checked_head_sha": head,
                   "evidence": [{"commit": head, "path": "src/gate.rs", "line_start": 10, "line_end": 12}]}
        _, title, body = render_history_issue("ExampleOrg/sample-service", 42, "b" * 40, head, "model-a", "openai/example-model",
                                               finding, Path("/tmp/repo"))
        self.assertEqual(title, "[HIGH] [MEDIUM-confidence] [MODEL-A] Safety gate checks stale state")
        self.assertIn("## Explain like I'm 5\n\nA safety gate", body)
        self.assertIn("```rust\nif unsafe { fail(); }\n```", body)
        self.assertIn("## Confidence rationale", body)


if __name__ == "__main__": unittest.main()
