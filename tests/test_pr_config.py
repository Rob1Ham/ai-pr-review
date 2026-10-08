import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_pr_review.common import CampaignError
from ai_pr_review.pr_config import load_pr_config


ROOT = Path(__file__).resolve().parents[1]


class PRConfigTests(unittest.TestCase):
    def test_defaults_and_campaign_review_policy(self):
        config = load_pr_config(ROOT / "configs" / "pr-service.example.json")
        self.assertEqual([(item["repository"], item["default_branch"], item["base_branch"])
                          for item in config.repositories],
                          [("ExampleOrg/sample-wallet", "main", "main"),
                           ("ExampleOrg/sample-service", "develop", "develop")])
        self.assertTrue(all(item["enabled"] and not item["include_drafts"]
                            for item in config.repositories))
        self.assertEqual([lane["key"] for lane in config.lanes], ["model-a", "model-b", "model-c"])
        self.assertEqual(config.service, {"poll_interval": 60, "max_parallel_prs": 1,
                                      "max_inline_comments": 50, "max_attempts_per_lane": 3,
                                      "blocking_severities": [],
                                          "review_name": "AI PR Review"})
        campaign = json.loads((ROOT / "campaigns" / "example-release.json").read_text())
        for key in ("lanes", "risk_patterns", "review_checklist"):
            self.assertEqual(config.data[key], campaign[key])
        # Max-effort reasoning consumed the old light output cap before a final
        # report could be emitted in the live four-lane smoke test.
        self.assertTrue(all(tier["output_tokens"] >= 32768 for tier in config.tiers.values()))

    def test_strict_unknowns_and_canonical_allowlist(self):
        source = json.loads((ROOT / "configs" / "pr-service.example.json").read_text())
        source["credentials"] = {}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(source))
            with self.assertRaisesRegex(CampaignError, "unknown config keys"):
                load_pr_config(path)
        config = load_pr_config(ROOT / "configs" / "pr-service.example.json")
        self.assertEqual(config.repository("ExampleOrg/sample-service")["base_branch"], "develop")
        with self.assertRaisesRegex(CampaignError, "canonical"):
            config.repository("exampleorg/sample-service")
        with self.assertRaisesRegex(CampaignError, "enabled"):
            config.repository("Other/repo")


if __name__ == "__main__":
    unittest.main()
