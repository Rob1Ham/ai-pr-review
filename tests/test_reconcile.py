from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from ai_pr_review.common import CampaignError, atomic_json, digest, read_json, seal
from ai_pr_review.history import plan_history, run_history
from ai_pr_review.pr_config import load_pr_config
from ai_pr_review.pr_service import service_lock
from ai_pr_review.reconcile import reconcile_history


REPO = "ExampleOrg/sample-wallet"
HEAD, MERGE = "a" * 40, "b" * 40
ROOT = Path(__file__).resolve().parents[1]


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = load_pr_config(ROOT / "configs/pr-service.example.json")
        self.client = Mock(spec=["list_merged_prs", "get_pr", "list_reviews"])
        self.client.list_merged_prs.return_value = [{"number": 1, "title": "Repair", "url": "url",
            "head_sha": HEAD, "merge_commit_sha": MERGE, "base_ref": "master", "merged_at": "2026-09-01T12:00:00Z"}]
        self.client.get_pr.return_value = {"merged": True, "head_sha": HEAD, "merge_commit_sha": MERGE}
        self.client.list_reviews.return_value = []
        plan_history(self.config, REPO, self.root, count=1, client=self.client)
        run_history(self.config, self.root, self.root / "workspace", publish="github", client=self.client,
                    reviewer=Mock(side_effect=CampaignError("old validation failure")))
        self.run = self.root / "prs" / f"1-{HEAD[:12]}"
        self.run.mkdir(parents=True)
        manifest = seal({"repository": REPO, "pr": 1, "head": HEAD,
                         "config": self.config.digest, "mode": "live"})
        atomic_json(self.run / "local-manifest.json", manifest)
        audit = seal({"manifest_digest": manifest["digest"], "lanes": [{"withheld": [{"local_id": "bad"}]}]})
        atomic_json(self.run / "publication-validation.json", audit)
        reviews = [{"model": lane["model"], "review_id": index, "review_url": "https://example.invalid/review"}
                   for index, lane in enumerate(self.config.lanes)]
        self.receipt = seal({"repository": REPO, "pr": 1, "head": HEAD, "publication": "github",
                            "reviews": reviews, "omitted_models": [], "withheld_count": 1,
                            "validation_digest": audit["digest"]})
        atomic_json(self.run / "github-receipt.json", self.receipt)
        self.client.list_reviews.return_value = [{"id": r["review_id"], "state": "COMMENTED", "commit_id": HEAD,
            "html_url": r["review_url"], "body": f"<!-- ai-pr-review:model-review:{REPO}:1:{HEAD}:{digest(r['model'])} -->"}
            for r in reviews]

    def reconcile(self):
        return reconcile_history(self.config, self.root, [1], client=self.client)

    def test_verified_receipts_repair_status_preserve_evidence_and_disclose_withheld(self):
        before = {p: p.read_bytes() for p in self.run.glob("*.json")}
        outcome = self.reconcile()
        self.assertEqual(outcome["status"], "complete")
        self.assertEqual(outcome["counts"], {"published": 1})
        record = read_json(self.root / "batch-progress.json")["prs"]["1"]
        self.assertEqual(record["receipt_digest"], self.receipt["digest"])
        self.assertEqual(record["previous_error"], "old validation failure")
        self.assertNotIn("error", record)
        self.assertEqual(record["withheld_count"], 1)
        self.assertIn("Withheld by evidence validation: 1", (self.root / "REPORT.md").read_text())
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertEqual(self.reconcile()["counts"], {"published": 1})

    def test_batch_and_run_locks_prevent_state_changes(self):
        before = (self.root / "batch-progress.json").read_bytes()
        for directory in (self.root, self.run):
            with service_lock(directory):
                with self.assertRaisesRegex(CampaignError, "active process"):
                    self.reconcile()
        self.assertEqual(before, (self.root / "batch-progress.json").read_bytes())

    def test_missing_or_duplicate_remote_reviews_fail_without_writes(self):
        before = (self.root / "batch-progress.json").read_bytes()
        reviews = self.client.list_reviews.return_value
        for invalid in (reviews[:-1], reviews + [reviews[0]], [{**r, "state": "DISMISSED"} for r in reviews]):
            self.client.list_reviews.return_value = invalid
            with self.assertRaises(CampaignError): self.reconcile()
            self.assertEqual(before, (self.root / "batch-progress.json").read_bytes())

    def test_wrong_receipt_identity_and_audit_fail_without_writes(self):
        before = (self.root / "batch-progress.json").read_bytes()
        for change in ({"head": "c" * 40}, {"withheld_count": 0}, {"omitted_models": ["unexpected"]}):
            atomic_json(self.run / "github-receipt.json", seal({**self.receipt, **change}))
            with self.assertRaises(CampaignError): self.reconcile()
            self.assertEqual(before, (self.root / "batch-progress.json").read_bytes())

    def test_running_status_and_unselected_pr_are_rejected(self):
        with self.assertRaisesRegex(CampaignError, "cohort"):
            reconcile_history(self.config, self.root, [2], client=self.client)
        state = read_json(self.root / "batch-progress.json")
        state["status"] = "running"
        atomic_json(self.root / "batch-progress.json", state)
        with self.assertRaisesRegex(CampaignError, "stop"):
            self.reconcile()

    def test_partial_publication_requires_policy_and_remains_partial(self):
        omitted = self.receipt["reviews"][-1]["model"]
        receipt = seal({**self.receipt, "reviews": self.receipt["reviews"][:-1], "omitted_models": [omitted]})
        atomic_json(self.run / "github-receipt.json", receipt)
        with self.assertRaisesRegex(CampaignError, "policy"):
            self.reconcile()
        state = read_json(self.root / "batch-progress.json")
        state["policy"]["allow_partial"] = True
        atomic_json(self.root / "batch-progress.json", state)
        outcome = self.reconcile()
        self.assertEqual(outcome["status"], "complete_with_failures")
        self.assertEqual(outcome["counts"], {"partial": 1})
        self.assertIn(omitted, (self.root / "REPORT.md").read_text())
