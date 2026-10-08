import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_pr_review.common import digest, seal
from ai_pr_review.git_mirror import parse_changed_line_map
from ai_pr_review.pr_config import PRConfig, load_pr_config
from ai_pr_review.pr_service import PRService
from ai_pr_review.runner import MockRunner


ROOT = Path(__file__).resolve().parents[1]
A = "a" * 40
B = "b" * 40
C = "c" * 40


def pr(number, head=B, *, draft=False, repo="ExampleOrg/sample-wallet"):
    branch = "develop" if repo.endswith("sample-service") else "main"
    return {"number": number, "state": "open", "draft": draft, "head": {"sha": head},
            "base": {"ref": branch}, "title": "untrusted title", "body": "untrusted body",
            "user": {"login": "author"}, "html_url": "https://example.invalid/pr"}


class FakeGitHub:
    def __init__(self, prs):
        self.prs = {repo: {item["number"]: item for item in values} for repo, values in prs.items()}
        self.checks, self.updates, self.publications = [], [], []

    def list_open_prs(self, repository):
        return list(self.prs.get(repository, {}).values())

    def get_pr(self, repository, number):
        return self.prs[repository][number]

    def create_check(self, repository, head_sha, name, external_id):
        self.checks.append((repository, head_sha, name, external_id))
        return {"id": len(self.checks)}

    def update_check(self, repository, check_id, **values):
        self.updates.append((repository, check_id, values))
        return {"id": check_id}

    def publish(self, repository, pr_value, head_sha, results, line_map):
        self.publications.append((repository, pr_value, head_sha, results, line_map))
        return {"id": f"review-{len(self.publications)}"}


class FakeMirror:
    def __init__(self):
        self.calls = []

    def prepare_pr(self, repository, number, base_branch, expected_head, metadata, destination, config):
        self.calls.append((repository, number, base_branch, expected_head, metadata))
        unit = {"id": f"file-{number}", "kind": "file", "path": f"src/{number}.py"}
        scope = {"id": f"pr-{number}-{expected_head}", "stage": "pr", "ordinal": 0,
                 "pr": number, "base_sha": A, "head_sha": expected_head,
                 "paths": [unit["path"]], "units": [unit], "diff_bytes": 100,
                 "lineage": [], "gaps": []}
        index = seal({"shards": [{"id": f"shard-{number}", "unit_ids": [unit["id"]]}],
                      "gaps": []})
        return {"scope": scope, "snapshot_index": index, "merge_base": A,
                "line_map": {unit["path"]: {"LEFT": [], "RIGHT": [[1, 1]]}}}


class ServiceTests(unittest.TestCase):
    def make_service(self, root, github, mirror, runner, config=None):
        config = config or load_pr_config(ROOT / "configs" / "pr-service.example.json")
        return PRService(config, github, mirror, lambda lane, prepared: runner,
                         state_dir=root / "state", workspace=root / "workspace")

    def test_polling_two_repositories_new_heads_draft_and_exact_once(self):
        prs = {"ExampleOrg/sample-wallet": [pr(1), pr(2, draft=True)],
               "ExampleOrg/sample-service": [pr(3, C, repo="ExampleOrg/sample-service")]}
        github, mirror, runner = FakeGitHub(prs), FakeMirror(), MockRunner(start_barrier=threading.Barrier(3))
        with tempfile.TemporaryDirectory() as temporary:
            service = self.make_service(Path(temporary), github, mirror, runner)
            outcomes = service.run_once()
            self.assertEqual([item["status"] for item in outcomes], ["reviewed", "draft", "reviewed"])
            self.assertEqual(len(github.publications), 2)
            self.assertEqual(len(runner.started), 6)
            self.assertEqual(runner.max_concurrency, 3)
            completed = [item for item in github.updates if item[2]["status"] == "completed"]
            self.assertTrue(all(item[2]["conclusion"] == "success" for item in completed))
            service.run_once()
            self.assertEqual(len(github.publications), 2)
            self.assertEqual(len(runner.started), 6)
            github.prs["ExampleOrg/sample-wallet"][1] = pr(1, C)
            service.run_once()
            self.assertEqual(len(github.publications), 3)
            self.assertEqual(len(runner.started), 9)

    def test_partial_lane_retry_and_persistent_resume(self):
        github, mirror = FakeGitHub({"ExampleOrg/sample-wallet": [pr(7)]}), FakeMirror()
        script = {(f"pr-7-{B}", "model-a", 1): "permanent"}
        runner = MockRunner(script=script)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self.make_service(root, github, mirror, runner).review_pr("ExampleOrg/sample-wallet", 7)
            self.assertEqual(first["status"], "retry")
            self.assertEqual(len(runner.started), 3)
            second = self.make_service(root, github, mirror, runner).review_pr("ExampleOrg/sample-wallet", 7)
            self.assertEqual(second["status"], "reviewed")
            self.assertEqual(len(runner.started), 4)
            self.assertEqual([item[1] for item in runner.started].count("model-a"), 2)
            state = json.loads((root / "state" / "pr-service.json").read_text())
            record = next(iter(state["heads"].values()))
            self.assertEqual(record["status"], "reviewed")
            self.assertTrue(record["publication_receipt"])
            self.assertEqual(len(list((root / "state" / "results").glob("*.json"))), 3)

    def test_head_change_before_publish_is_superseded(self):
        github, mirror = FakeGitHub({"ExampleOrg/sample-wallet": [pr(9)]}), FakeMirror()

        class ChangingRunner(MockRunner):
            def run(inner, job, scope, cancel):
                result = super(ChangingRunner, inner).run(job, scope, cancel)
                if len(inner.completed) == 3:
                    github.prs["ExampleOrg/sample-wallet"][9] = pr(9, C)
                return result

        with tempfile.TemporaryDirectory() as temporary:
            result = self.make_service(Path(temporary), github, mirror, ChangingRunner(delay=.02)).review_pr(
                "ExampleOrg/sample-wallet", 9)
            self.assertEqual(result["status"], "superseded")
            self.assertFalse(github.publications)
            self.assertEqual(github.updates[-1][2]["conclusion"], "cancelled")

    def test_check_conclusions_and_publication_receipt_gate(self):
        base = load_pr_config(ROOT / "configs" / "pr-service.example.json")
        data = json.loads(json.dumps(base.data))
        data["service"]["blocking_severities"] = ["high"]
        config = PRConfig(base.path, data, digest(data))
        finding = {"severity": "high"}
        script = {(f"pr-11-{B}", "model-a", 1): {"findings": [finding]}}
        github = FakeGitHub({"ExampleOrg/sample-wallet": [pr(11)]})
        with tempfile.TemporaryDirectory() as temporary:
            result = self.make_service(Path(temporary), github, FakeMirror(), MockRunner(script=script), config).run_once()[0]
            self.assertEqual(result["conclusion"], "failure")
            self.assertEqual(github.updates[-1][2]["conclusion"], "failure")
            self.assertEqual(len(github.publications[0][3]), 3)

        neutral_script = {(f"pr-12-{B}", "model-a", 1): {"findings": [{"severity": "high"}]}}
        github = FakeGitHub({"ExampleOrg/sample-wallet": [pr(12)]})
        with tempfile.TemporaryDirectory() as temporary:
            result = self.make_service(Path(temporary), github, FakeMirror(),
                                       MockRunner(script=neutral_script)).run_once()[0]
            self.assertEqual(result["conclusion"], "neutral")

    def test_service_never_executes_target_and_jobs_assign_all_shards(self):
        class InspectRunner(MockRunner):
            def run(inner, job, scope, cancel):
                self.assertEqual(job["assigned_shards"], ["shard-13"])
                self.assertEqual(job["expected_units"], ["file-13"])
                return super(InspectRunner, inner).run(job, scope, cancel)

        with tempfile.TemporaryDirectory() as temporary:
            github = FakeGitHub({"ExampleOrg/sample-wallet": [pr(13)]})
            result = self.make_service(Path(temporary), github, FakeMirror(), InspectRunner()).run_once()[0]
            self.assertEqual(result["status"], "reviewed")

    def test_successful_review_removes_only_reproducible_snapshot(self):
        class MaterializingMirror(FakeMirror):
            def prepare_pr(inner, repository, number, base_branch, expected_head, metadata, destination, config):
                Path(destination).mkdir(parents=True)
                (Path(destination) / "temporary-source").write_text("review input")
                return super().prepare_pr(repository, number, base_branch, expected_head, metadata, destination, config)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); github = FakeGitHub({"ExampleOrg/sample-wallet": [pr(14)]})
            service = self.make_service(root, github, MaterializingMirror(), MockRunner())
            result = service.run_once()[0]
            self.assertEqual(result["status"], "reviewed")
            self.assertFalse(any((root / "workspace").iterdir()))


class LineMapTests(unittest.TestCase):
    def test_add_delete_ranges_and_quoted_paths(self):
        patch = (b'diff --git "a/old name.py" "b/new name.py"\n'
                 b'--- "a/old name.py"\n+++ "b/new name.py"\n'
                 b'@@ -2,3 +2,0 @@\n@@ -8,0 +5,2 @@\n')
        self.assertEqual(parse_changed_line_map(patch), {
            "old name.py": {"LEFT": [2, 3, 4], "RIGHT": []},
            "new name.py": {"LEFT": [], "RIGHT": [5, 6]}})

    def test_newline_path_rejected(self):
        patch = b'--- "a/bad\\nname"\n+++ b/good\n@@ -1 +1 @@\n'
        with self.assertRaisesRegex(Exception, "newline"):
            parse_changed_line_map(patch)


if __name__ == "__main__":
    unittest.main()
