from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_pr_review.campaign import Campaign
from ai_pr_review.common import seal
from ai_pr_review.config import load_config
from ai_pr_review.runner import MockRunner
from ai_pr_review.publish import Publisher

ROOT = Path(__file__).resolve().parents[1]


def scope(name, stage, ordinal):
    return {"id": name, "stage": stage, "ordinal": ordinal, "pr": ordinal + 1 if stage == "pr" else None,
            "base_sha": "a" * 40, "head_sha": "b" * 40, "paths": [f"src/{name}.rs"],
            "units": [{"id": name, "kind": "file", "path": f"src/{name}.rs"}],
            "diff_bytes": 1000, "lineage": [], "gaps": []}


class CampaignTests(unittest.TestCase):
    def test_configured_workers_and_pr_barrier(self):
        config = load_config(ROOT / "campaigns" / "example-release.json")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); scopes = [scope("pr-1", "pr", 0), scope("range", "whole_range", 1)]
            manifest = seal({"repository": "example/repo", "repository_path": str(root),
                             "base_sha": "a" * 40, "head_sha": "b" * 40,
                             "gate": {"status": "unverified", "blockers": []}, "scopes": scopes})
            runner = MockRunner(start_barrier=threading.Barrier(len(config.lanes)))
            result = Campaign(root / "run", manifest, config, runner=runner).run()
            self.assertEqual(result["status"], "complete")
            self.assertEqual(runner.max_concurrency, 3)
            first_range = next(index for index, item in enumerate(runner.started) if item[0] == "range")
            self.assertEqual(first_range, 3)
            self.assertEqual(result["scopes"]["pr"], {"expected": 3, "complete": 3})

    def test_publisher_holds_non_high_confidence_finding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); repo = root / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
            (repo / "sample.py").write_text("value = False\n")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
            sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
            run = root / "run"; run.mkdir()
            manifest = {"repository": "example/repo", "repository_path": str(repo), "head_sha": sha}
            publisher = Publisher(run, manifest)
            lane = load_config(ROOT / "campaigns" / "example-release.json").lanes[0]
            job = {"id": "job", "run_id": "run", "stage": "pr", "pr": 1,
                   "base_sha": sha, "head_sha": sha, "lane": lane}
            finding = {"local_id": "F1", "root_cause": "value is false", "broken_invariant": "value must be true",
                       "expected": "true", "observed": "false", "impact": "incorrect state",
                       "remediation_direction": "return true", "confidence_rationale": "single static signal",
                       "severity": "medium", "confidence": "medium", "persists_at_head": "present",
                       "checked_head_sha": sha, "evidence": [{"commit": sha, "path": "sample.py",
                                                               "line_start": 1, "line_end": 1}]}
            result = {"report": {"findings": [finding]}, "provenance": {"mode": "live"}}
            publisher.submit(job, result)
            self.assertEqual(publisher.summary()["claims"], {"needs-validation": 1})


if __name__ == "__main__": unittest.main()
