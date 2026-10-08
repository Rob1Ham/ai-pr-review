import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from ai_pr_review.common import CampaignError, atomic_json, read_json, seal
from ai_pr_review.history import plan_history, run_history
from ai_pr_review.pr_config import load_pr_config
from ai_pr_review.retirement import active_lanes, load_retirement, retire_history


ROOT = Path(__file__).resolve().parents[1]


class RetirementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        data = copy.deepcopy(load_pr_config(ROOT / "configs/pr-service.example.json").data)
        data["lanes"].append({"key": "glm", "provider": "fireworks-ai", "effort": "max",
                              "model": "fireworks-ai/example-retired-model"})
        atomic_json(self.root / "legacy-config.json", data)
        self.config = load_pr_config(self.root / "legacy-config.json")
        self.client = Mock(spec=["list_merged_prs", "get_pr", "list_reviews"])
        self.client.list_merged_prs.return_value = [{"number": 1, "title": "Example", "url": "url",
            "head_sha": "a" * 40, "merge_commit_sha": "b" * 40, "base_ref": "master",
            "merged_at": "2026-09-01T00:00:00Z"}]
        self.manifest = plan_history(self.config, "ExampleOrg/sample-wallet", self.root, count=1, client=self.client)
        self.state = {"manifest_digest": self.manifest["digest"], "repository": "ExampleOrg/sample-wallet",
                      "policy": {"publish": "none", "mock": True, "allow_partial": False},
                      "status": "stopped", "prs": {"1": {"head": "a" * 40, "status": "pending"}}}
        atomic_json(self.root / "batch-progress.json", self.state)
        self.client.get_pr.return_value = {"merged": True, "head_sha": "a" * 40, "merge_commit_sha": "b" * 40}

    def test_retirement_is_durable_idempotent_and_keeps_original_manifest(self):
        before = (self.root / "batch-manifest.json").read_bytes()
        receipt = retire_history(self.config, self.root, ["glm"])
        self.assertEqual(receipt["active_lanes"], ["model-a", "model-b", "model-c"])
        self.assertEqual(load_retirement(self.config, self.manifest, self.root), ("glm",))
        self.assertEqual(retire_history(self.config, self.root, ["glm"]), receipt)
        self.assertEqual((self.root / "batch-manifest.json").read_bytes(), before)
        self.assertEqual(read_json(self.root / "original-config.json"), self.config.data)

    def test_retirement_requires_stopped_batch_and_known_remaining_lanes(self):
        with self.assertRaisesRegex(CampaignError, "configured lane"):
            retire_history(self.config, self.root, ["unknown"])
        with self.assertRaisesRegex(CampaignError, "at least one active"):
            retire_history(self.config, self.root, [lane["key"] for lane in self.config.lanes])
        self.state["status"] = "running"
        atomic_json(self.root / "batch-progress.json", self.state)
        with self.assertRaisesRegex(CampaignError, "stop the batch"):
            retire_history(self.config, self.root, ["glm"])

    def test_mismatched_retirement_cannot_be_applied(self):
        receipt = retire_history(self.config, self.root, ["glm"])
        atomic_json(self.root / "lane-retirement.json", seal({**receipt, "config_digest": "wrong"}))
        with self.assertRaisesRegex(CampaignError, "frozen batch"):
            load_retirement(self.config, self.manifest, self.root)

    def test_resumed_batch_forwards_retirement_and_ignores_retired_failure(self):
        retire_history(self.config, self.root, ["glm"])
        reviewer = Mock(return_value={"status": "complete", "lanes": {
            lane["key"]: {"status": "failed" if lane["key"] == "glm" else "complete"} for lane in self.config.lanes}})
        outcome = run_history(self.config, self.root, self.root / "workspace", mock=True,
                              client=self.client, reviewer=reviewer)
        self.assertEqual(outcome["status"], "complete")
        self.assertEqual(outcome["active_lanes"], ["model-a", "model-b", "model-c"])
        self.assertEqual(reviewer.call_args.kwargs["retired_lanes"], ("glm",))

    def test_new_defaults_have_no_glm(self):
        config = load_pr_config(ROOT / "configs/pr-service.example.json")
        self.assertEqual([lane["key"] for lane in active_lanes(config)], ["model-a", "model-b", "model-c"])
