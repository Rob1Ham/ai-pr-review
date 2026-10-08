from pathlib import Path
import copy
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from ai_pr_review.common import CampaignError, read_json, atomic_json, digest
from ai_pr_review.git_mirror import build_pr_scope, _git
from ai_pr_review.local_job import historical_base, review_local, publish_saved, _resolve_historical_base
from ai_pr_review.pr_config import PRConfig, load_pr_config
from ai_pr_review.runner import MockRunner
from ai_pr_review.snapshot import verify_snapshot


ROOT = Path(__file__).resolve().parents[1]
REPO = "ExampleOrg/sample-wallet"


def git(repo, *args):
    return subprocess.check_output(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                                    "-C", str(repo), *args], text=True, stderr=subprocess.DEVNULL).strip()


class LocalJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "master")
        (self.repo / "value.py").write_text("value = 1\n")
        git(self.repo, "add", "."); git(self.repo, "commit", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "switch", "-c", "feature")
        (self.repo / "value.py").write_text("value = 2\n")
        git(self.repo, "commit", "-am", "change")
        self.head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "switch", "master"); git(self.repo, "merge", "--no-ff", "feature", "-m", "merge")
        self.merge = git(self.repo, "rev-parse", "HEAD")
        self.pr = {"number": 1, "state": "closed", "merged": True, "head_sha": self.head,
                   "base_sha": self.merge, "merge_commit_sha": self.merge}
        self.config = load_pr_config(ROOT / "configs/pr-service.example.json")
        self.prepared = build_pr_scope(self.repo, self.base, self.head, repository=REPO,
                                      number=1, metadata={}, destination=self.root / "snapshot", config=self.config)
        self.client = Mock(spec=["get_pr"])
        self.client.get_pr.return_value = self.pr

    def run_job(self, **kwargs):
        return review_local(self.config, REPO, 1, self.root / "run", self.root / "workspace",
                            client=self.client, prepared=self.prepared, **kwargs)

    def test_merged_base_uses_verified_parent_not_current_tip(self):
        base, provenance = historical_base(self.repo, self.pr)
        self.assertEqual(base, self.base)
        self.assertEqual(provenance["source"], "verified-merge-first-parent")
        with self.assertRaisesRegex(CampaignError, "already contains"):
            historical_base(self.repo, self.pr, self.merge)

    def test_ambiguous_merge_requires_explicit_base(self):
        with self.assertRaisesRegex(CampaignError, "ambiguous"):
            historical_base(self.repo, {**self.pr, "merge_commit_sha": self.head})
        self.assertEqual(historical_base(self.repo, self.pr, self.base)[0], self.base)

    def test_open_pr_does_not_fetch_ephemeral_test_merge(self):
        self.client.get_pr.return_value = {**self.pr, "state": "open", "merged": False,
                                           "base_sha": self.base, "merge_commit_sha": "f" * 40}

        def checked_git(repo, *args):
            if args[0] == "fetch":
                self.assertNotIn("f" * 40, args)
                return b""
            return _git(repo, *args)

        with patch("ai_pr_review.local_job.GitMirror._ensure", return_value=self.repo), \
                patch("ai_pr_review.local_job._git", side_effect=checked_git):
            result = review_local(self.config, REPO, 1, self.root / "open", self.root / "workspace",
                                  mock=True, client=self.client)
        self.assertEqual(result["status"], "complete")

    def test_real_snapshot_mock_end_to_end_and_resume(self):
        runner = MockRunner(script={(self.prepared["scope"]["id"], "model-a", 1): "transient"})
        result = self.run_job(mock=True, runner_factory=lambda *_: runner)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(runner.started), len(self.config.lanes) + 1)
        self.run_job(mock=True, runner_factory=lambda *_: runner)
        self.assertEqual(len(runner.started), len(self.config.lanes) + 1)
        draft = read_json(self.root / "run/publication-draft.json")
        self.assertEqual(len(draft["reviews"]), len(self.config.lanes))
        self.assertNotIn("comments", draft)
        self.assertEqual(result["publication"], "local-only")

    def test_default_mock_verifies_snapshot_and_prompt(self):
        self.assertEqual(self.run_job(mock=True)["status"], "complete")

    def test_snapshot_index_is_searchable_and_tamper_checked(self):
        index_path = self.root / "snapshot/path-index.json"
        lines = index_path.read_text().splitlines()
        self.assertEqual(sum('"storage_path"' in line for line in lines), 2)
        index_path.chmod(0o600)
        index_path.write_text(index_path.read_text().replace("value.py", "other.py"))
        with self.assertRaises(CampaignError):
            verify_snapshot(self.root / "snapshot", self.prepared["snapshot_index"])

    def test_mock_results_cannot_be_reused_for_live(self):
        self.run_job(mock=True)
        with self.assertRaisesRegex(CampaignError, "requires live"):
            publish_saved(self.config, self.root / "run", client=self.client)
        with self.assertRaisesRegex(CampaignError, "different review"):
            self.run_job(mock=False, runner_factory=lambda *_: MockRunner())

    def test_closed_unmerged_and_head_mismatch_rejected(self):
        self.client.get_pr.return_value = {**self.pr, "merged": False}
        with self.assertRaisesRegex(CampaignError, "closed-unmerged"):
            self.run_job(mock=True)
        self.client.get_pr.return_value = self.pr
        with self.assertRaisesRegex(CampaignError, "head does not match"):
            self.run_job(mock=True, expected_head="a" * 40)

    def test_failure_retries_are_bounded(self):
        script = {(self.prepared["scope"]["id"], "model-a", attempt): "permanent" for attempt in (1, 2, 3)}
        runner = MockRunner(script=script)
        self.assertEqual(self.run_job(mock=True, runner_factory=lambda *_: runner)["status"], "failed")
        self.assertEqual(len(runner.started), len(self.config.lanes) + 2)
        self.run_job(mock=True, runner_factory=lambda *_: runner)
        self.assertEqual(len(runner.started), len(self.config.lanes) + 2)
        self.assertFalse((self.root / "run/publication-draft.json").exists())

    def make_live_results(self, findings=None, **kwargs):
        class LiveFixtureRunner(MockRunner):
            mode = "live"

            def run(inner, job, scope, cancel):
                result = super().run(job, scope, cancel)
                result["provenance"]["mode"] = "live"
                for unit in result["report"]["coverage"]["units"]:
                    unit.update(method="static", evidence=[f"{self.head}:value.py:1-1"])
                if findings is not None and job["lane"]["key"] == "model-b":
                    result["report"]["findings"] = copy.deepcopy(findings)
                return result

        runner = LiveFixtureRunner()
        self.run_job(mock=False, runner_factory=lambda *_: runner, **kwargs)
        return runner

    def test_publish_saved_reuses_live_results_and_rechecks_head(self):
        runner = self.make_live_results()
        client = Mock(spec=["get_pr", "list_reviews", "create_review"])
        client.get_pr.return_value = {**self.pr, "state": "open", "draft": False, "base_ref": "main"}
        client.list_reviews.return_value = []
        client.create_review.return_value = {"id": 42, "html_url": "https://example.invalid/review"}
        receipt = publish_saved(self.config, self.root / "run", client=client)
        self.assertEqual(len(receipt["reviews"]), 3)
        self.assertEqual(client.create_review.call_count, 3)
        self.assertEqual(len(runner.started), 3)
        state = read_json(self.root / "run/pr-service.json")
        record = next(iter(state["heads"].values()))
        record["status"] = "failed"
        record["lanes"]["model-c"]["status"] = "failed"
        atomic_json(self.root / "run/pr-service.json", state)
        with self.assertRaisesRegex(CampaignError, "all lanes"):
            publish_saved(self.config, self.root / "run", client=client)
        receipt = publish_saved(self.config, self.root / "run", client=client, allow_partial=True, dry_run=True)
        self.assertEqual(len(receipt["reviews"]), 2)
        self.assertEqual(receipt["omitted_models"], [self.config.lanes[2]["model"]])
        self.assertEqual(client.create_review.call_count, 3)
        client.get_pr.return_value["head_sha"] = "f" * 40
        with self.assertRaisesRegex(CampaignError, "reviewed head"):
            publish_saved(self.config, self.root / "run", client=client, allow_partial=True)
        self.assertEqual(client.create_review.call_count, 3)

    def test_merged_publication_requires_opt_in_and_verified_history(self):
        self.make_live_results()
        client = Mock(spec=["get_pr", "list_reviews", "create_review", "list_pr_commits"])
        client.list_pr_commits.return_value = []
        client.get_pr.return_value = {**self.pr, "draft": False, "base_ref": "feature-branch"}
        client.list_reviews.return_value = []
        client.create_review.return_value = {"id": 42}
        with self.assertRaisesRegex(CampaignError, "allow-merged"):
            publish_saved(self.config, self.root / "run", client=client)
        receipt = publish_saved(self.config, self.root / "run", client=client, allow_merged=True)
        self.assertEqual(len(receipt["reviews"]), 3)
        self.assertIn("Retrospective review", client.create_review.call_args.args[3])
        client.get_pr.return_value["merged"] = False
        with self.assertRaisesRegex(CampaignError, "not eligible"):
            publish_saved(self.config, self.root / "run", client=client, allow_merged=True)
        client.get_pr.return_value.update(merged=True, merge_commit_sha=self.head)
        with self.assertRaisesRegex(CampaignError, "ambiguous"):
            publish_saved(self.config, self.root / "run", client=client, allow_merged=True)

    def test_retirement_reuses_successes_without_changing_sealed_evidence(self):
        data = copy.deepcopy(self.config.data)
        data["lanes"].append({"key": "glm", "provider": "fireworks-ai", "effort": "max",
                              "model": "fireworks-ai/accounts/fireworks/models/glm-5p3"})
        self.config = PRConfig(self.config.path, data, digest(data))
        runner = MockRunner(script={(self.prepared["scope"]["id"], "glm", attempt): "permanent"
                                    for attempt in (1, 2, 3)})
        self.assertEqual(self.run_job(mock=True, runner_factory=lambda *_: runner)["status"], "failed")
        evidence = {path: path.read_bytes() for path in (self.root / "run/results").glob("*.json")}
        manifest_before = (self.root / "run/local-manifest.json").read_bytes()
        calls = len(runner.started)
        result = self.run_job(mock=True, runner_factory=lambda *_: runner, retired_lanes=("glm",))
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["active_lanes"], ["model-a", "model-b", "model-c"])
        self.assertEqual(result["lanes"]["glm"]["attempts"], 3)
        self.assertEqual(len(runner.started), calls)
        self.assertEqual((self.root / "run/local-manifest.json").read_bytes(), manifest_before)
        self.assertTrue(all(path.read_bytes() == content for path, content in evidence.items()))
        self.assertEqual(len(read_json(self.root / "run/publication-draft.json")["reviews"]), 3)
        with self.assertRaisesRegex(CampaignError, "re-enabled"):
            self.run_job(mock=True, runner_factory=lambda *_: runner)
        fresh = MockRunner()
        review_local(self.config, REPO, 1, self.root / "fresh", self.root / "workspace",
                     client=self.client, prepared=self.prepared, mock=True, retired_lanes=("glm",),
                     runner_factory=lambda *_: fresh)
        self.assertEqual({call[1] for call in fresh.started}, {"model-a", "model-b", "model-c"})
        self.assertEqual(len(fresh.started), 3)

    def test_invalid_candidate_can_be_withheld_without_rewriting_reports(self):
        valid = {"local_id": "good", "root_cause": "Valid candidate", "severity": "low", "confidence": "high",
                 "expected": "expected", "observed": "observed", "impact": "impact",
                 "remediation_direction": "fix", "checked_head_sha": self.head, "persists_at_head": "present",
                 "evidence": [{"commit": self.head, "path": "value.py", "line_start": 1, "line_end": 1}]}
        invalid = copy.deepcopy(valid)
        invalid.update(local_id="bad", root_cause="Unvalidated candidate")
        invalid["evidence"][0]["line_end"] = 999
        self.make_live_results([valid, invalid])
        before = {path: path.read_bytes() for path in (self.root / "run/results").glob("*.json")}
        client = Mock(spec=["get_pr", "list_reviews", "create_review"])
        client.get_pr.return_value = {**self.pr, "state": "open", "draft": False, "base_ref": "main"}
        with self.assertRaisesRegex(CampaignError, "outside the immutable snapshot"):
            publish_saved(self.config, self.root / "run", client=client, dry_run=True)
        receipt = publish_saved(self.config, self.root / "run", client=client, dry_run=True, hold_invalid_findings=True)
        self.assertEqual(receipt["withheld_count"], 1)
        draft = read_json(self.root / "run/publication-draft.json")
        second_model = draft["reviews"][1]
        self.assertEqual(len(second_model["comments"]), 1)
        self.assertIn("Valid candidate", second_model["body"])
        self.assertNotIn("Unvalidated candidate", second_model["body"])
        self.assertIn("withheld", second_model["body"])
        self.assertTrue(all(path.read_bytes() == data for path, data in before.items()))
        audit = read_json(self.root / "run/publication-validation.json")
        self.assertEqual(audit["lanes"][1]["withheld"][0]["local_id"], "bad")
        client.create_review.assert_not_called()

    def test_saved_complete_lanes_recover_after_initial_render_failure(self):
        invalid = {"local_id": "bad", "root_cause": "Invalid coordinate", "severity": "low", "confidence": "high",
                   "expected": "expected", "observed": "observed", "impact": "impact",
                   "remediation_direction": "fix", "checked_head_sha": self.head, "persists_at_head": "present",
                   "evidence": [{"commit": self.head, "path": "value.py", "line_start": 0, "line_end": 1}]}
        with self.assertRaisesRegex(CampaignError, "evidence lines are invalid"):
            self.make_live_results([invalid])
        state = read_json(self.root / "run/pr-service.json")
        record = next(iter(state["heads"].values()))
        self.assertEqual(record["status"], "pending")
        self.assertTrue(all(lane["status"] == "complete" for lane in record["lanes"].values()))
        before = {path: path.read_bytes() for path in (self.root / "run/results").glob("*.json")}
        client = Mock(spec=["get_pr", "list_reviews", "create_review"])
        client.get_pr.return_value = {**self.pr, "draft": False, "base_ref": "main"}
        receipt = publish_saved(self.config, self.root / "run", client=client, allow_merged=True,
                                hold_invalid_findings=True, dry_run=True)
        self.assertEqual(len(receipt["reviews"]), 3)
        self.assertEqual(receipt["withheld_count"], 1)
        self.assertEqual(receipt["conclusion_suggestion"], "neutral")
        self.assertTrue(all(path.read_bytes() == data for path, data in before.items()))
        record["lanes"]["model-c"]["status"] = "pending"
        atomic_json(self.root / "run/pr-service.json", state)
        with self.assertRaisesRegex(CampaignError, "all lanes"):
            publish_saved(self.config, self.root / "run", client=client, allow_partial=True,
                          allow_merged=True, hold_invalid_findings=True)
        client.create_review.assert_not_called()

    def test_deferred_rendering_seals_results_for_withholding_publication(self):
        finding = {"local_id": "bad", "root_cause": "Invalid coordinate", "severity": "low", "confidence": "high",
                   "expected": "expected", "observed": "observed", "impact": "impact",
                   "remediation_direction": "fix", "checked_head_sha": self.head, "persists_at_head": "present",
                   "evidence": [{"commit": self.head, "path": "value.py", "line_start": "1", "line_end": "1"}]}
        runner = self.make_live_results([finding], render_draft=False)
        self.assertEqual(read_json(self.root / "run/summary.json")["status"], "complete")
        self.assertFalse((self.root / "run/publication-draft.json").exists())
        receipt = publish_saved(self.config, self.root / "run", client=self.client, allow_merged=True,
                                hold_invalid_findings=True, dry_run=True)
        self.assertEqual(receipt["withheld_count"], 1)
        self.assertEqual(len(receipt["reviews"]), 3)
        self.assertEqual(len(runner.started), 3)

    def test_stacked_pr_base_requires_complete_chain_and_merge_containment(self):
        git(self.repo, "switch", "feature")
        (self.repo / "value.py").write_text("value = 3\n")
        git(self.repo, "commit", "-am", "stack tip")
        tip = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "switch", "master")
        git(self.repo, "merge", "--no-ff", "feature", "-m", "merge stack")
        merge = git(self.repo, "rev-parse", "HEAD")
        stacked = {**self.pr, "merge_commit_sha": merge}
        with self.assertRaisesRegex(CampaignError, "ambiguous"):
            historical_base(self.repo, stacked)
        base, provenance = historical_base(self.repo, stacked, commits=[{"sha": self.head}])
        self.assertEqual(base, self.base)
        self.assertEqual(provenance["source"], "verified-stacked-pr-commits")
        self.assertEqual(provenance["commits"], [self.head])
        with self.assertRaisesRegex(CampaignError, "original head"):
            historical_base(self.repo, stacked, commits=[{"sha": tip}])
        with self.assertRaisesRegex(CampaignError, "contained"):
            historical_base(self.repo, {**stacked, "head_sha": tip, "merge_commit_sha": self.merge},
                            commits=[{"sha": tip}])
        with self.assertRaisesRegex(CampaignError, "linear chain"):
            historical_base(self.repo, {**stacked, "merge_commit_sha": self.base, "head_sha": tip},
                            commits=[{"sha": self.base}, {"sha": tip}])

    def test_unknown_or_absent_head_findings_are_withheld_only_by_opt_in(self):
        valid = {"local_id": "good", "root_cause": "Valid candidate", "severity": "low", "confidence": "high",
                 "expected": "expected", "observed": "observed", "impact": "impact",
                 "remediation_direction": "fix", "checked_head_sha": self.head, "persists_at_head": "present",
                 "evidence": [{"commit": self.head, "path": "value.py", "line_start": 1, "line_end": 1}]}
        findings = [valid, {**valid, "local_id": "unknown", "root_cause": "Unconfirmed candidate", "persists_at_head": "unknown"},
                    {**valid, "local_id": "absent", "root_cause": "Historical candidate", "persists_at_head": "absent"}]
        runner = self.make_live_results(findings, render_draft=False)
        before = {path: path.read_bytes() for path in (self.root / "run/results").glob("*.json")}
        with self.assertRaisesRegex(CampaignError, "present at the exact PR head"):
            publish_saved(self.config, self.root / "run", client=self.client, allow_merged=True, dry_run=True)
        receipt = publish_saved(self.config, self.root / "run", client=self.client, allow_merged=True,
                                hold_invalid_findings=True, dry_run=True)
        self.assertEqual(receipt["withheld_count"], 2)
        self.assertEqual(receipt["inline_count"], 1)
        self.assertEqual(len(receipt["reviews"]), 3)
        second_model = read_json(self.root / "run/publication-draft.json")["reviews"][1]
        self.assertIn("Valid candidate", second_model["body"])
        self.assertNotIn("Unconfirmed candidate", second_model["body"])
        self.assertNotIn("Historical candidate", second_model["body"])
        self.assertIn("head-presence", second_model["body"])
        audit = read_json(self.root / "run/publication-validation.json")
        self.assertEqual({item["persists_at_head"] for item in audit["lanes"][1]["withheld"]}, {"unknown", "absent"})
        self.assertTrue(all(path.read_bytes() == data for path, data in before.items()))
        self.assertEqual(len(runner.started), 3)

    def test_stack_tip_merge_uses_pr_base_not_entire_stack(self):
        git(self.repo, "switch", "feature")
        (self.repo / "value.py").write_text("value = 3\n")
        git(self.repo, "commit", "-am", "stack tip")
        tip = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "switch", "-c", "clean-base", self.base)
        git(self.repo, "merge", "--no-ff", "feature", "-m", "merge entire stack")
        merge = git(self.repo, "rev-parse", "HEAD")
        pr = {**self.pr, "head_sha": tip, "base_sha": self.head, "merge_commit_sha": merge}
        client = Mock(spec=["list_pr_commits"])
        client.list_pr_commits.return_value = [{"sha": tip}]
        self.assertEqual(historical_base(self.repo, pr)[0], self.base)
        base, provenance = _resolve_historical_base(self.repo, pr, client, REPO)
        self.assertEqual(base, self.head)
        self.assertEqual(provenance["commits"], [tip])
        client.list_pr_commits.return_value = [{"sha": self.head}, {"sha": tip}]
        with self.assertRaisesRegex(CampaignError, "does not match the GitHub PR base"):
            _resolve_historical_base(self.repo, pr, client, REPO)
        client.list_pr_commits.return_value = []
        with self.assertRaisesRegex(CampaignError, "complete PR commit list"):
            _resolve_historical_base(self.repo, pr, client, REPO)
