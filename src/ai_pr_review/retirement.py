"""Explicit, monotonic lane retirement without rewriting historical evidence."""

from pathlib import Path

from .common import CampaignError, atomic_json, digest, now_iso, read_json, seal, verify_seal
from .pr_service import service_lock


def active_lanes(config, retired_lanes=()):
    keys = {lane["key"] for lane in config.lanes}
    retired = set(retired_lanes)
    if retired - keys or len(retired) != len(retired_lanes):
        raise CampaignError("retired lanes must be unique configured lane keys")
    active = tuple(lane for lane in config.lanes if lane["key"] not in retired)
    if not active: raise CampaignError("at least one active review lane is required")
    return active


def load_retirement(config, manifest, root):
    path = Path(root) / "lane-retirement.json"
    if not path.exists(): return ()
    value = read_json(path)
    verify_seal(value)
    if (value.get("version") != 1 or value["manifest_digest"] != manifest["digest"]
            or value["config_digest"] != config.digest):
        raise CampaignError("lane retirement does not match the frozen batch")
    retired = tuple(value["retired_lanes"])
    active_lanes(config, retired)
    if value["retired_models"] != {lane["key"]: lane["model"] for lane in config.lanes if lane["key"] in retired}:
        raise CampaignError("retired model identity mismatch")
    return retired


def retire_history(config, root, lane_keys):
    """Record approved removals and retain the original config for frozen jobs."""
    root = Path(root).resolve()
    with service_lock(root):
        manifest = read_json(root / "batch-manifest.json")
        verify_seal(manifest)
        state = read_json(root / "batch-progress.json")
        if manifest["config_digest"] != config.digest or state["manifest_digest"] != manifest["digest"]:
            raise CampaignError("retirement requires the original batch configuration")
        if state["status"] == "running": raise CampaignError("stop the batch before retiring a lane")
        previous = load_retirement(config, manifest, root)
        retired = tuple(sorted(set(previous) | set(lane_keys)))
        if not lane_keys: raise CampaignError("specify at least one lane to retire")
        active = active_lanes(config, retired)
        original = root / "original-config.json"
        if original.exists():
            if digest(read_json(original)) != config.digest:
                raise CampaignError("archived configuration does not match the batch")
        else:
            atomic_json(original, config.data)
        path = root / "lane-retirement.json"
        if retired == previous: return read_json(path)
        record = seal({"version": 1, "created_at": now_iso(), "manifest_digest": manifest["digest"],
                       "config_digest": config.digest, "retired_lanes": list(retired),
                       "retired_models": {lane["key"]: lane["model"] for lane in config.lanes if lane["key"] in retired},
                       "active_lanes": [lane["key"] for lane in active]})
        atomic_json(path, record)
        return record
