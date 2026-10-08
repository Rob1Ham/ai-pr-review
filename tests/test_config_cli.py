import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_pr_review.cli import parser
from ai_pr_review.common import CampaignError
from ai_pr_review.config import load_config

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "campaigns" / "example-release.json"


class ConfigTests(unittest.TestCase):
    def test_example_campaign_contract(self):
        config = load_config(CONFIG)
        self.assertEqual(config.target["repository"], "ExampleOrg/sample-wallet")
        self.assertEqual(config.data["expected"], {"prs": 2, "commits": 4, "baseline_calls": 9,
                                               "tier_counts": {"light": 1, "standard": 1, "deep": 0}})
        self.assertEqual([lane["key"] for lane in config.lanes], ["model-a", "model-b", "model-c"])
        self.assertTrue(all(lane["effort"] == "max" for lane in config.lanes))
        self.assertEqual(set(config.lineage), {10})
        self.assertNotIn("path", config.target)

    def test_unknown_key_and_duplicate_lane_rejected(self):
        value = json.loads(CONFIG.read_text()); value["surprise"] = True
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bad.json"; path.write_text(json.dumps(value))
            with self.assertRaisesRegex(CampaignError, "unknown"): load_config(path)
        value = json.loads(CONFIG.read_text()); value["lanes"][1]["key"] = value["lanes"][0]["key"]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bad.json"; path.write_text(json.dumps(value))
            with self.assertRaisesRegex(CampaignError, "duplicate lane"): load_config(path)

    def test_required_cli_arguments(self):
        with self.assertRaises(SystemExit): parser().parse_args(["plan", "--config", str(CONFIG)])
        args = parser().parse_args(["plan", "--config", str(CONFIG), "--target-repo", "/tmp/repo"])
        self.assertEqual(args.command, "plan")
        args = parser().parse_args(["doctor", "--config", str(CONFIG)])
        self.assertEqual(args.command, "doctor")


if __name__ == "__main__": unittest.main()
