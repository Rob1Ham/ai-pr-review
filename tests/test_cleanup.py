import hashlib
from pathlib import Path
import tempfile
import unittest

from ai_pr_review.cleanup import cleanup_history
from ai_pr_review.common import CampaignError, atomic_json, digest, read_json, seal, verify_seal


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "batch"
        self.snapshots = Path(self.temp.name) / "inputs"
        self.root.mkdir(); self.snapshots.mkdir()
        self.head, self.base = "a" * 40, "b" * 40
        self.run = self.root / "prs" / f"1-{self.head[:12]}"
        self.run.mkdir(parents=True)
        content = b"tracked source\n"
        sha = hashlib.sha256(content).hexdigest()
        obj = {"storage_path": f"objects/{sha}.blob", "content_digest": f"sha256:{sha}"}
        paths = seal({"objects": []})
        index = seal({"objects": [obj], "shards": [], "path_index_digest": paths["digest"],
                      "options": {"snapshot_version": "v2"}})
        name = digest({"repository": "test/repo", "number": 1, "base": self.base,
                       "head": self.head, "config": "config", "snapshot_version": "v2"})
        self.target = self.snapshots / name
        (self.target / "objects").mkdir(parents=True)
        (self.target / obj["storage_path"]).write_bytes(content)
        atomic_json(self.target / "index.json", index)
        atomic_json(self.target / "path-index.json", paths)
        manifest = seal({"repository": "test/repo", "pr": 1, "head": self.head, "base": self.base,
                         "config": "config", "mode": "live", "prepared": {
                             "snapshot_root": f"/local/workspace/inputs/{name}", "snapshot_index": index}})
        self.receipt = seal({"publication": "github", "repository": "test/repo", "pr": 1,
                             "head": self.head, "reviews": [{"review_id": 123}]})
        atomic_json(self.run / "local-manifest.json", manifest)
        atomic_json(self.run / "github-receipt.json", self.receipt)
        atomic_json(self.run / "results/report.json", {"report": "retain"})
        batch = seal({"repository": "test/repo"})
        atomic_json(self.root / "batch-manifest.json", batch)
        self.state = {"manifest_digest": batch["digest"], "repository": "test/repo", "status": "paused_low_disk",
                      "prs": {"1": {"status": "partial", "head": self.head,
                                    "receipt_digest": self.receipt["digest"], "reviews": self.receipt["reviews"]},
                              "2": {"status": "failed", "head": "c" * 40}}}
        atomic_json(self.root / "batch-progress.json", self.state)
        self.unfinished = self.snapshots / ("d" * 64)
        self.unfinished.mkdir()
        (self.unfinished / "keep.txt").write_text("unfinished work")

    def test_dry_run_then_apply_preserves_results_and_unfinished_work(self):
        before = (self.root / "batch-progress.json").read_bytes()
        plan = cleanup_history(self.root, self.snapshots)
        self.assertEqual(plan["snapshot_count"], 1)
        self.assertTrue(self.target.exists())
        result = cleanup_history(self.root, self.snapshots, apply=True)
        self.assertFalse(self.target.exists())
        self.assertTrue((self.unfinished / "keep.txt").exists())
        self.assertTrue((self.run / "results/report.json").exists())
        self.assertEqual((self.root / "batch-progress.json").read_bytes(), before)
        self.assertEqual(read_json(self.run / "github-receipt.json"), self.receipt)
        verify_seal(read_json(result["cleanup_receipt"]))
        self.assertEqual(cleanup_history(self.root, self.snapshots)["snapshot_count"], 0)

    def test_missing_publication_proof_blocks_deletion(self):
        self.state["prs"]["1"]["receipt_digest"] = "wrong"
        atomic_json(self.root / "batch-progress.json", self.state)
        with self.assertRaisesRegex(CampaignError, "publication evidence"):
            cleanup_history(self.root, self.snapshots, apply=True)
        self.assertTrue(self.target.exists())

    def test_running_batch_blocks_deletion(self):
        self.state["status"] = "running"
        atomic_json(self.root / "batch-progress.json", self.state)
        with self.assertRaisesRegex(CampaignError, "stop the history"):
            cleanup_history(self.root, self.snapshots, apply=True)

    def test_unknown_file_blocks_deletion(self):
        (self.target / "user-notes.txt").write_text("user work")
        with self.assertRaisesRegex(CampaignError, "unexpected"):
            cleanup_history(self.root, self.snapshots, apply=True)
        self.assertTrue((self.target / "user-notes.txt").exists())

    def test_symlink_target_blocks_deletion(self):
        moved = self.snapshots / "saved"
        self.target.rename(moved)
        self.target.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(CampaignError, "escapes"):
            cleanup_history(self.root, self.snapshots, apply=True)
        self.assertTrue(moved.exists())
