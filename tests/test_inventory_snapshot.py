from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_pr_review.inventory import build_inventory
from ai_pr_review.common import CampaignError
from ai_pr_review.snapshot import prepare_scope, read_evidence, verify_snapshot, _secret_reason


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


class InventorySnapshotTests(unittest.TestCase):
    def test_rust_computed_password_is_not_a_literal_credential(self):
        expression = b"    password: hex::encode(password_bytes),\n"
        fake_key = b"sk-" + b"abcdefghijklmnopqrstuvwxyz123456"
        self.assertIsNone(_secret_reason("node.rs", expression))
        self.assertIsNotNone(_secret_reason("config.yaml", expression))
        self.assertIsNotNone(_secret_reason("node.rs", expression + b'password: "long-literal-password-123456",\n'))
        self.assertIsNotNone(_secret_reason("node.rs", b'password: hex::encode("' + fake_key + b'"),\n'))
        self.assertIsNotNone(_secret_reason("secrets.key", expression))

    def test_secret_bearing_changed_patch_is_rejected_before_snapshot(self):
        fake_key = "sk-" + "abcdefghijklmnopqrstuvwxyz123456"
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"; repo.mkdir(); git(repo, "init", "-b", "master")
            git(repo, "config", "user.name", "Test"); git(repo, "config", "user.email", "test@example.invalid")
            (repo / "safe.txt").write_text("safe\n"); git(repo, "add", "."); git(repo, "commit", "-m", "base")
            base = git(repo, "rev-parse", "HEAD")
            (repo / ".env").write_text("OPENAI_API_KEY=" + fake_key + "\n")
            git(repo, "add", ".env"); git(repo, "commit", "-m", "secret")
            head = git(repo, "rev-parse", "HEAD")
            manifest = build_inventory(repo, base, head, repository="test/repo", branch="master")
            with self.assertRaisesRegex(CampaignError, "excluded secret"):
                prepare_scope(repo, manifest["scopes"][0], Path(temporary) / "snapshot")

    def test_local_landing_and_immutable_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); repo = root / "repo"; repo.mkdir()
            git(repo, "init", "-b", "master"); git(repo, "config", "user.name", "Test")
            git(repo, "config", "user.email", "test@example.invalid")
            (repo / "value.py").write_text("value = 1\n"); git(repo, "add", "."); git(repo, "commit", "-m", "base")
            base = git(repo, "rev-parse", "HEAD")
            git(repo, "switch", "-c", "feature"); (repo / "value.py").write_text("value = 2\n")
            git(repo, "add", "."); git(repo, "commit", "-m", "change"); git(repo, "switch", "master")
            git(repo, "merge", "--no-ff", "feature", "-m", "Merge pull request #12 from test")
            head = git(repo, "rev-parse", "HEAD")
            manifest = build_inventory(repo, base, head, repository="example/repo", branch="master")
            self.assertEqual(manifest["gate"]["status"], "unverified")
            self.assertEqual(manifest["counts"]["prs"], 1)
            destination = root / "snapshot"
            index = prepare_scope(repo, manifest["scopes"][0], destination)
            verify_snapshot(destination, index)
            self.assertEqual({unit for shard in index["shards"] for unit in shard["unit_ids"]},
                             set(index["unit_ids"]))
            self.assertEqual(read_evidence(repo, head, "value.py", 1, 1), "value = 2\n")
            self.assertFalse(any(path.stat().st_mode & 0o222 for path in destination.rglob("*") if path.is_file()))


if __name__ == "__main__": unittest.main()
