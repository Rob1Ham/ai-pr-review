from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from ai_pr_review.common import CampaignError, digest, read_json
from ai_pr_review.github_pr import GitHubPRClient
from ai_pr_review.history import plan_history, run_history, select_latest
from ai_pr_review.pr_config import load_pr_config


ROOT = Path(__file__).resolve().parents[1]
REPO = "ExampleOrg/sample-wallet"
HEAD, MERGE = "a" * 40, "b" * 40


def item(number, day):
    return {"number": number, "title": f"PR {number}", "url": f"https://github.com/{REPO}/pull/{number}",
            "head_sha": HEAD, "merge_commit_sha": MERGE, "base_ref": "master",
            "merged_at": f"2026-09-{day:02d}T12:00:00Z"}


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = load_pr_config(ROOT / "configs/pr-service.example.json")
        self.client = Mock(spec=["list_merged_prs", "get_pr", "list_reviews"])
        self.client.list_merged_prs.return_value = [item(1, 1), item(2, 2), item(3, 3)]
        self.client.get_pr.return_value = {"merged": True, "head_sha": HEAD, "merge_commit_sha": MERGE}
        self.client.list_reviews.return_value = []
        self.reviewer = Mock(return_value={"status": "complete", "lanes": {
            lane["key"]: {"status": "complete"} for lane in self.config.lanes}})
        self.publisher = Mock(return_value={"reviews": [], "omitted_models": [], "digest": "receipt"})

    def plan(self):
        return plan_history(self.config, REPO, self.root, count=3, client=self.client)

    def run_batch(self, **kwargs):
        return run_history(self.config, self.root, self.root / "workspace", client=self.client,
                           reviewer=self.reviewer, publisher=self.publisher, **kwargs)

    def test_selection_uses_merge_time_including_old_numbers(self):
        selected = select_latest([item(100, 1), item(1, 3), item(101, 2), item(102, 5)],
                                 2, "2026-09-04T00:00:00Z")
        self.assertEqual([entry["number"] for entry in selected], [1, 101])
        with self.assertRaisesRegex(CampaignError, "duplicate"):
            select_latest([item(1, 1), item(1, 2)], 2, "2026-09-04T00:00:00Z")

    def test_selection_can_freeze_all_since_date_for_one_base(self):
        values = [item(1, 1), item(2, 2), {**item(3, 3), "base_ref": "demo"}]
        selected = select_latest(values, None, "2026-09-04T00:00:00Z",
                                 since="2026-09-02T00:00:00Z", base_branch="master")
        self.assertEqual([entry["number"] for entry in selected], [2])

    def test_plan_is_frozen_on_resume(self):
        original = self.plan()
        self.client.list_merged_prs.return_value.append(item(4, 4))
        self.assertEqual(self.plan(), original)
        self.client.list_merged_prs.assert_called_once()

    def test_pause_and_resume_do_not_repeat_completed_reviews(self):
        self.plan()
        first = self.run_batch(publish="github", max_prs=1)
        self.assertEqual(first["status"], "paused")
        self.assertEqual(first["counts"], {"published": 1, "pending": 2})
        final = self.run_batch(publish="github")
        self.assertEqual(final["counts"], {"published": 3})
        self.assertEqual(self.reviewer.call_count, 3)
        self.assertTrue(all(call.kwargs["allow_merged"] for call in self.publisher.call_args_list))
        self.assertEqual(self.publisher.call_count, 6)  # Dry rendering followed by publication.
        self.run_batch(publish="github")
        self.assertEqual(self.reviewer.call_count, 3)

    def test_failures_do_not_stop_other_prs_and_partial_is_recorded(self):
        self.plan()
        def review(config, repo, number, *args, **kwargs):
            if number == 3: raise CampaignError("ambiguous merge")
            lanes = {lane["key"]: {"status": "complete"} for lane in config.lanes}
            if number == 2: lanes["model-c"]["status"] = "failed"
            return {"status": "failed" if number == 2 else "complete", "lanes": lanes}
        def publish(config, run_dir, **kwargs):
            return {"reviews": [], "omitted_models": ["model-c"] if run_dir.name.startswith("2-") else [], "digest": "receipt"}
        self.reviewer.side_effect = review
        self.publisher.side_effect = publish
        result = self.run_batch(publish="github", allow_partial=True)
        self.assertEqual(result["status"], "complete_with_failures")
        self.assertEqual(result["counts"], {"failed": 1, "partial": 1, "published": 1})
        self.run_batch(publish="github", allow_partial=True)
        self.assertEqual(self.reviewer.call_count, 4)  # Only the preparation failure retried.

    def test_existing_remote_reviews_skip_inference(self):
        self.plan()
        self.client.list_reviews.side_effect = lambda repo, number: [{
            "id": index, "state": "COMMENTED", "commit_id": HEAD, "html_url": "https://example.invalid/review",
            "body": f"<!-- ai-pr-review:model-review:{repo}:{number}:{HEAD}:{digest(lane['model'])} -->"}
            for index, lane in enumerate(self.config.lanes)]
        result = self.run_batch(publish="github")
        self.assertEqual(result["counts"], {"already_published": 3})
        self.reviewer.assert_not_called()
        self.publisher.assert_not_called()

    def test_mock_cannot_publish_and_execution_policy_cannot_change(self):
        self.plan()
        with self.assertRaisesRegex(CampaignError, "mock"):
            self.run_batch(publish="github", mock=True)
        self.run_batch(publish="none", mock=True)
        with self.assertRaisesRegex(CampaignError, "execution policy"):
            self.run_batch(publish="github")
        self.publisher.assert_not_called()

    def test_cancellation_stops_before_publication(self):
        self.plan()
        stop = threading.Event()
        def review(*args, **kwargs):
            stop.set()
            return {"status": "interrupted", "lanes": {}}
        self.reviewer.side_effect = review
        result = self.run_batch(publish="github", stop_event=stop)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["counts"], {"interrupted": 1, "pending": 2})
        self.publisher.assert_not_called()

    def test_changed_frozen_identity_never_calls_models(self):
        self.plan()
        self.client.get_pr.return_value["head_sha"] = "c" * 40
        result = self.run_batch(publish="github")
        self.assertEqual(result["counts"], {"failed": 3})
        self.reviewer.assert_not_called()

    def test_low_disk_pauses_without_starting_models(self):
        self.plan()
        with patch("ai_pr_review.history.shutil.disk_usage", return_value=Mock(free=100)):
            result = self.run_batch(publish="github")
        self.assertEqual(result["status"], "paused_low_disk")
        self.assertEqual(result["counts"], {"pending": 3})
        self.reviewer.assert_not_called()

    def test_withholding_policy_defers_rendering_and_is_frozen_on_resume(self):
        self.plan()
        self.publisher.return_value = {"reviews": [], "omitted_models": [], "digest": "receipt", "withheld_count": 2}
        self.run_batch(publish="github", allow_partial=True, hold_invalid_findings=True)
        self.assertTrue(all(call.kwargs["render_draft"] is False for call in self.reviewer.call_args_list))
        self.assertTrue(all(call.kwargs["hold_invalid_findings"] for call in self.publisher.call_args_list))
        self.assertIn("Withheld by evidence validation: 2", (self.root / "REPORT.md").read_text())
        with self.assertRaisesRegex(CampaignError, "execution policy"):
            self.run_batch(publish="github", allow_partial=True)


class MergedPaginationTests(unittest.TestCase):
    def test_all_pages_are_enumerated_before_selection(self):
        def page(number, next_page):
            return {"data": {"repository": {"pullRequests": {
                "nodes": [{"number": number, "title": "Title", "url": "url", "headRefOid": HEAD,
                           "mergedAt": "2026-09-01T00:00:00Z", "baseRefName": "master", "mergeCommit": {"oid": MERGE}}],
                "pageInfo": {"hasNextPage": next_page, "endCursor": "cursor1" if next_page else None}}}}}
        client = GitHubPRClient()
        with patch.object(client, "_api", side_effect=[page(99, True), page(1, False)]) as api:
            result = client.list_merged_prs(REPO)
        self.assertEqual([pr["number"] for pr in result], [99, 1])
        self.assertEqual(api.call_args_list[1].args[1]["variables"]["cursor"], "cursor1")
