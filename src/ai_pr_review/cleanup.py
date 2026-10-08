"""Receipt-gated removal of reproducible snapshots from a stopped history batch."""

from contextlib import ExitStack
import hashlib
from pathlib import Path
import re
import shutil
import stat

from .common import CampaignError, atomic_json, digest, now_iso, read_json, seal, verify_seal
from .pr_service import service_lock


def _hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _snapshot_stats(target, expected):
    if target.is_symlink() or not target.is_dir():
        raise CampaignError("cleanup target must be a regular snapshot directory")
    expected_files = {"index.json": None, "path-index.json": None}
    for item in expected["objects"]:
        if not re.fullmatch(r"objects/[0-9a-f]{64}\.blob", item["storage_path"]):
            raise CampaignError("invalid snapshot object path")
        expected_files[item["storage_path"]] = item["content_digest"]
    for item in expected["shards"]:
        if not re.fullmatch(r"shards/[0-9a-f]{64}\.diff", item["diff_path"]):
            raise CampaignError("invalid snapshot shard path")
        expected_files[item["diff_path"]] = item["diff_digest"]
    actual, size, allocated = set(), 0, 0
    for path in target.rglob("*"):
        info = path.lstat()
        relative = path.relative_to(target).as_posix()
        if stat.S_ISDIR(info.st_mode) and relative in {"objects", "shards"}: continue
        if not stat.S_ISREG(info.st_mode) or relative not in expected_files:
            raise CampaignError("snapshot contains an unexpected file, directory, or symlink")
        actual.add(relative)
        if expected_files[relative] and "sha256:" + _hash(path) != expected_files[relative]:
            raise CampaignError("snapshot content was changed; refusing cleanup")
        size += info.st_size
        allocated += info.st_blocks * 512
    if actual != set(expected_files): raise CampaignError("snapshot file set is incomplete")
    stored = read_json(target / "index.json")
    verify_seal(stored)
    if stored != expected: raise CampaignError("snapshot index differs from retained manifest")
    paths = read_json(target / "path-index.json")
    verify_seal(paths)
    if paths["digest"] != expected["path_index_digest"]:
        raise CampaignError("snapshot path index mismatch")
    return {"files": len(actual), "bytes": size, "allocated_bytes": allocated}


def cleanup_history(root, snapshots, *, apply=False):
    """Dry-run by default; only published/terminal-partial PR snapshots qualify.

    The caller explicitly supplies the host snapshot directory. Container paths
    are mapped only by the content-addressed basename, which is recomputed from
    the sealed run manifest. No arbitrary saved absolute path is deleted.
    """
    root, snapshots = Path(root).absolute(), Path(snapshots).absolute()
    if root.is_symlink() or snapshots.is_symlink():
        raise CampaignError("cleanup roots cannot be symlinks")
    root, snapshots = root.resolve(), snapshots.resolve()
    if not root.is_dir() or not snapshots.is_dir(): raise CampaignError("cleanup roots must already exist")
    with ExitStack() as locks:
        locks.enter_context(service_lock(root))
        state = read_json(root / "batch-progress.json")
        batch = read_json(root / "batch-manifest.json")
        verify_seal(batch)
        if state["manifest_digest"] != batch["digest"]:
            raise CampaignError("batch state/manifest mismatch")
        if state["status"] not in {"paused_low_disk", "paused", "stopped", "complete", "complete_with_failures"}:
            raise CampaignError("stop the history batch before cleaning snapshots")
        plan, protected, skipped = [], {}, []
        for name in ("batch-manifest.json", "batch-progress.json", "COHORT.md", "REPORT.md"):
            path = root / name
            if path.is_symlink(): raise CampaignError("batch artifacts cannot be symlinks")
            if path.is_file(): protected[str(path)] = _hash(path)
        for number, record in state["prs"].items():
            if record["status"] not in {"published", "partial"}:
                skipped.append({"pr": int(number), "reason": record["status"]})
                continue
            run = root / "prs" / f"{int(number)}-{record['head'][:12]}"
            if run.is_symlink() or not run.is_dir() or run.resolve().parent != root / "prs":
                raise CampaignError("invalid retained run directory")
            locks.enter_context(service_lock(run))
            manifest = read_json(run / "local-manifest.json")
            receipt = read_json(run / "github-receipt.json")
            verify_seal(manifest)
            verify_seal(receipt)
            if (manifest["pr"] != int(number) or manifest["head"] != record["head"]
                    or manifest["repository"] != state["repository"] or manifest["mode"] != "live"
                    or receipt.get("publication") != "github" or not receipt.get("reviews")
                    or receipt["head"] != manifest["head"] or receipt["pr"] != manifest["pr"]
                    or receipt["repository"] != manifest["repository"]
                    or receipt["digest"] != record.get("receipt_digest")
                    or receipt["reviews"] != record.get("reviews")):
                raise CampaignError(f"PR #{number} lacks matching publication evidence")
            index = manifest["prepared"]["snapshot_index"]
            verify_seal(index)
            name = digest({"repository": manifest["repository"], "number": manifest["pr"],
                           "base": manifest["base"], "head": manifest["head"], "config": manifest["config"],
                           "snapshot_version": index["options"]["snapshot_version"]})
            if Path(manifest["prepared"]["snapshot_root"]).name != name:
                raise CampaignError("snapshot directory identity does not match the manifest")
            target = snapshots / name
            if not target.exists() and not target.is_symlink():
                skipped.append({"pr": int(number), "reason": "snapshot already absent"})
                continue
            if target.is_symlink() or target.resolve().parent != snapshots:
                raise CampaignError("snapshot target escapes the designated directory")
            stats = _snapshot_stats(target, index)
            for path in run.rglob("*"):
                if path.is_symlink(): raise CampaignError("retained artifacts contain a symlink")
                if path.is_file() and path.name != ".service.lock":
                    protected[str(path)] = _hash(path)
            plan.append({"pr": int(number), "path": str(target), "snapshot_digest": index["digest"], **stats})
        outcome = {"version": 1, "created_at": now_iso(), "mode": "apply" if apply else "dry-run",
                   "batch_manifest_digest": batch["digest"], "snapshots": plan, "skipped": skipped,
                   "snapshot_count": len(plan), "bytes": sum(item["bytes"] for item in plan),
                   "allocated_bytes": sum(item["allocated_bytes"] for item in plan),
                   "free_bytes_before": shutil.disk_usage(snapshots).free, "deleted": []}
        if apply:
            receipt_path = root / ("snapshot-cleanup-" + now_iso().replace(":", "-") + ".json")
            outcome["protected_artifacts"] = protected
            atomic_json(receipt_path, seal(outcome))
            for item in plan:
                # All candidate roots and their complete file sets were checked
                # under the batch/run locks before any deletion began.
                shutil.rmtree(item["path"])
                outcome["deleted"].append(item["path"])
                atomic_json(receipt_path, seal(outcome))
            for path, expected_hash in protected.items():
                if _hash(Path(path)) != expected_hash:
                    raise CampaignError("a retained artifact changed during snapshot cleanup")
            outcome["retained_artifacts_verified"] = len(protected)
            outcome["free_bytes_after"] = shutil.disk_usage(snapshots).free
            atomic_json(receipt_path, seal(outcome))
            outcome["cleanup_receipt"] = str(receipt_path)
            outcome.pop("protected_artifacts")
        return outcome
