from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from ai_pr_review.common import CampaignError, digest, read_json
from ai_pr_review.history import _validate_manifest
from ai_pr_review.pr_config import load_pr_config
from ai_pr_review.range_history import plan_range, range_commits


ROOT = Path(__file__).resolve().parents[1]
REPO = "ExampleOrg/sample-wallet"
BASE, HEAD = "a" * 40, "b" * 40


def comparison(commits, total=None):
    return {"status": "ahead" if commits else "identical", "behind_by": 0, "base_commit": {"sha": BASE},
            "merge_base_commit": {"sha": BASE}, "commits": commits,
            "total_commits": len(commits) if total is None else total}


def commit(sha):
    return {"sha": sha, "parents": [{"sha": BASE}, {"sha": "c" * 40}]}


def pr(number, merge):
    return {"number": number, "title": "Change", "head_sha": "c" * 40, "merge_commit_sha": merge,
            "base_ref": "main", "merged_at": f"2026-09-0{number}T12:00:00Z", "url": f"https://github.com/{REPO}/pull/{number}"}


class RangeHistoryTests(unittest.TestCase):
    def setUp(self):
        self.config = load_pr_config(ROOT / "configs/pr-service.example.json")
        self.client = Mock(spec=["_api", "list_merged_prs", "list_reviews"])
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_comparison_paginates_beyond_250_using_frozen_shas(self):
        commits = [commit(f"{number:040x}") for number in range(268)] + [commit(HEAD)]
        self.client._api.side_effect = [comparison(commits[offset:offset + 100], 269) for offset in range(0, 269, 100)]
        result = range_commits(self.client, REPO, BASE, HEAD)
        self.assertEqual(len(result), 269)
        self.assertEqual(self.client._api.call_count, 3)
        for page, call in enumerate(self.client._api.call_args_list, 1):
            self.assertIn(f"{BASE}...{HEAD}?per_page=100&page={page}", call.args[0][0])

    def test_diverged_incomplete_duplicate_and_wrong_endpoint_ranges_rejected(self):
        cases = [
            [{**comparison([commit(HEAD)]), "behind_by": 1}],
            [comparison([commit("d" * 40)], 2), comparison([], 2)],
            [comparison([commit(HEAD), commit(HEAD)], 2)],
            [comparison([commit("d" * 40)])],
        ]
        for pages in cases:
            self.client._api.side_effect = pages
            with self.assertRaises(CampaignError): range_commits(self.client, REPO, BASE, HEAD)

    def setup_plan(self):
        self.client._api.side_effect = [{"sha": BASE}, {"sha": HEAD},
                                      comparison([commit("d" * 40), commit(HEAD)])]
        self.client.list_merged_prs.return_value = [pr(1, "d" * 40), pr(2, HEAD), pr(3, BASE)]
        model = self.config.lanes[0]["model"]
        partial = {"state": "COMMENTED", "commit_id": "c" * 40, "id": 42,
                   "body": f"<!-- ai-pr-review:model-review:{REPO}:1:{'c' * 40}:{digest(model)} -->"}
        self.client.list_reviews.side_effect = lambda repo, number: [partial] if number == 1 else []

    def test_only_uncovered_range_prs_scheduled_and_resume_is_frozen(self):
        self.setup_plan()
        result = plan_range(self.config, REPO, self.root, "v0.1.1", "main", client=self.client)
        self.assertEqual(result["missing_prs"], [2])
        self.assertEqual(result["existing_partial_prs"], [1])
        self.assertEqual(result["covered_prs"], 1)
        self.assertEqual(result["unmapped_merge_commits"], [])
        self.assertEqual({call.args[1] for call in self.client.list_reviews.call_args_list}, {1, 2})
        manifest = read_json(self.root / "batch-manifest.json")
        _validate_manifest(manifest, self.config)
        self.assertEqual([p["number"] for p in manifest["prs"]], [2])
        self.client.reset_mock()
        self.assertEqual(plan_range(self.config, REPO, self.root, "v0.1.1", "main", client=self.client), result)
        self.client._api.assert_not_called()
        with self.assertRaisesRegex(CampaignError, "different selection"):
            plan_range(self.config, REPO, self.root, "v0.1.0", "main", client=self.client)

    def test_wrong_head_review_does_not_count_as_coverage(self):
        self.setup_plan()
        model = self.config.lanes[0]["model"]
        self.client.list_reviews.side_effect = lambda repo, number: [{"state": "COMMENTED", "commit_id": BASE,
            "id": 42, "body": f"<!-- ai-pr-review:model-review:{REPO}:{number}:{'c' * 40}:{digest(model)} -->"}]
        result = plan_range(self.config, REPO, self.root, "v0.1.1", "main", client=self.client)
        self.assertEqual(result["missing_prs"], [2, 1])

    def test_empty_range_writes_audit_without_invalid_zero_job_manifest(self):
        self.client._api.side_effect = [{"sha": BASE}, {"sha": BASE}, comparison([])]
        self.client.list_merged_prs.return_value = []
        result = plan_range(self.config, REPO, self.root, "v0.1.1", "main", client=self.client)
        self.assertEqual(result["missing_prs"], [])
        self.assertTrue((self.root / "range-audit.json").exists())
        self.assertFalse((self.root / "batch-manifest.json").exists())
