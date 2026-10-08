"""Frozen, resumable merged-PR batches executed locally and published remotely."""

from collections import Counter
from datetime import datetime
from pathlib import Path
import re
import shutil
import threading

from .common import CampaignError, atomic_json, digest, now_iso, read_json, seal, verify_seal
from .github_pr import GitHubPRClient
from .local_job import review_local, publish_saved
from .pr_service import service_lock
from .prompts import PROMPT_DIGEST
from .runner import RUNNER_VERSION
from .snapshot import SNAPSHOT_VERSION
from .retirement import active_lanes, load_retirement


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None: raise ValueError("missing timezone")
        return parsed
    except (AttributeError, TypeError, ValueError) as exc:
        raise CampaignError("invalid merged PR timestamp") from exc


def select_latest(prs, count, cutoff, *, since=None, base_branch=None):
    if count is not None and (type(count) is not int or not 1 <= count <= 1000):
        raise CampaignError("history count must be between 1 and 1000")
    if since is not None:
        _timestamp(since)
    if base_branch is not None and (not isinstance(base_branch, str) or not base_branch):
        raise CampaignError("history base branch must be a nonempty string")
    seen, eligible = set(), []
    for pr in prs:
        number = pr.get("number")
        if type(number) is not int or number <= 0 or number in seen:
            raise CampaignError("duplicate or invalid merged PR number")
        seen.add(number)
        if not re.fullmatch(r"[0-9a-f]{40}", pr.get("head_sha", "")):
            raise CampaignError("invalid merged PR head SHA")
        merged_at = _timestamp(pr["merged_at"])
        if (merged_at <= _timestamp(cutoff)
                and (since is None or merged_at >= _timestamp(since))
                and (base_branch is None or pr.get("base_ref") == base_branch)):
            eligible.append(pr)
    ordered = sorted(eligible, key=lambda p: (_timestamp(p["merged_at"]), p["number"]), reverse=True)
    selected = ordered if count is None else ordered[:count]
    if count is not None and len(selected) != count:
        raise CampaignError(f"only {len(selected)} merged PRs are available before the cutoff")
    if not selected:
        raise CampaignError("no merged PRs match the requested history selection")
    return selected


def _validate_manifest(manifest, config):
    verify_seal(manifest)
    config.repository(manifest["repository"])
    if (manifest.get("version") != 1 or manifest["config_digest"] != config.digest
            or manifest["prompt_digest"] != PROMPT_DIGEST or manifest["runner_version"] != RUNNER_VERSION
            or manifest["snapshot_version"] != SNAPSHOT_VERSION):
        raise CampaignError("history manifest is incompatible with this config/runtime")
    if select_latest(manifest["prs"], manifest["count"], manifest["cutoff"],
                     since=manifest.get("since"), base_branch=manifest.get("base_branch")) != manifest["prs"]:
        raise CampaignError("history manifest selection order is invalid")


def plan_history(config, repository, root, *, count=50, since=None, base_branch=None, client=None):
    """Enumerate all merged PRs once, then freeze the requested merge-time cohort."""
    config.repository(repository)
    root = Path(root).resolve()
    with service_lock(root):
        path = root / "batch-manifest.json"
        if path.exists():
            manifest = read_json(path)
            _validate_manifest(manifest, config)
            if (manifest["repository"] != repository or manifest["count"] != count
                    or manifest.get("since") != since or manifest.get("base_branch") != base_branch):
                raise CampaignError("history directory belongs to a different selection")
            return manifest
        cutoff = now_iso()
        all_prs = (client or GitHubPRClient()).list_merged_prs(repository)
        prs = select_latest(all_prs, count, cutoff, since=since, base_branch=base_branch)
        manifest = seal({"version": 1, "repository": repository, "count": count, "cutoff": cutoff,
                         "since": since, "base_branch": base_branch,
                         "selection": "merged_at descending; full merged-PR pagination; optional inclusive since/base filters",
                         "enumerated_merged_prs": len(all_prs), "prs": prs,
                         "config_digest": config.digest, "prompt_digest": PROMPT_DIGEST,
                         "runner_version": RUNNER_VERSION, "snapshot_version": SNAPSHOT_VERSION})
        atomic_json(path, manifest)
        lines = ["# Historical review cohort", "", f"Repository: {repository}", f"Frozen at: {cutoff}",
                 f"Merged since: {since or 'unbounded'}", f"Base branch: {base_branch or 'any'}", "",
                 "| PR | Merged (UTC) | Base branch | Title |", "| --- | --- | --- | --- |"]
        for item in prs:
            title = item["title"].replace("|", "\\|").replace("\n", " ")
            lines.append(f"| [#{item['number']}]({item['url']}) | {item['merged_at']} | {item['base_ref']} | {title} |")
        (root / "COHORT.md").write_text("\n".join(lines) + "\n")
        return manifest


def _existing_complete_reviews(client, repository, item, lanes):
    """Avoid inference if all configured model/head reviews already exist."""
    reviews = client.list_reviews(repository, item["number"])
    receipts = []
    for lane in lanes:
        marker = f"<!-- ai-pr-review:model-review:{repository}:{item['number']}:{item['head_sha']}:{digest(lane['model'])} -->"
        matches = [review for review in reviews if marker in (review.get("body") or "")
                   and review.get("commit_id") == item["head_sha"]
                   and review.get("state") == "COMMENTED"]
        if len(matches) > 1: raise CampaignError("duplicate remote model/head reviews require reconciliation")
        if not matches: return None
        receipts.append({"model": lane["model"], "review_id": matches[0]["id"],
                         "review_url": matches[0].get("html_url")})
    return receipts


def _save_progress(root, state):
    state["updated_at"] = now_iso()
    state["counts"] = dict(Counter(item["status"] for item in state["prs"].values()))
    atomic_json(root / "batch-progress.json", state)
    lines = ["# Historical review progress", "", f"Status: **{state['status']}**",
             f"Updated: {state['updated_at']}", "", "| PR | Status | Reviews | Detail |",
             "| --- | --- | --- | --- |"]
    for number, item in state["prs"].items():
        links = ", ".join(f"[{review['model'].split('/')[-1]}]({review['review_url']})"
                          for review in item.get("reviews", []) if review.get("review_url"))
        detail = item.get("error", "").replace("|", "\\|").replace("\n", " ")[:500]
        if item.get("omitted_models"):
            detail += " Omitted: " + ", ".join(item["omitted_models"])
        if item.get("withheld_count"):
            detail += f" Withheld by evidence validation: {item['withheld_count']}."
        lines.append(f"| #{number} | {item['status']} | {links} | {detail} |")
    (root / "REPORT.md").write_text("\n".join(lines) + "\n")


def history_status(root):
    state = read_json(Path(root) / "batch-progress.json")
    return {key: state[key] for key in ("repository", "status", "updated_at", "counts", "pause_reason",
                                      "active_lanes", "retired_lanes") if key in state}


def run_history(config, root, workspace, *, publish="none", mock=False, allow_partial=False,
                 max_prs=None, client=None, reviewer=None, publisher=None, stop_event=None,
                 emit=lambda text: None, hold_invalid_findings=False):
    """Run a frozen cohort serially, preserving per-head lane and publication state."""
    if publish not in {"none", "github"} or (mock and publish == "github"):
        raise CampaignError("mock history runs cannot publish to GitHub")
    if hold_invalid_findings and publish != "github":
        raise CampaignError("invalid-finding withholding requires GitHub publication")
    if max_prs is not None and (type(max_prs) is not int or max_prs <= 0):
        raise CampaignError("max-prs must be positive")
    root, workspace = Path(root).resolve(), Path(workspace).resolve()
    client = client or GitHubPRClient()
    reviewer, publisher = reviewer or review_local, publisher or publish_saved
    stop = stop_event or threading.Event()
    policy = {"publish": publish, "mock": mock, "allow_partial": allow_partial}
    if hold_invalid_findings: policy["hold_invalid_findings"] = True
    with service_lock(root):
        manifest = read_json(root / "batch-manifest.json")
        _validate_manifest(manifest, config)
        repository = manifest["repository"]
        retired = load_retirement(config, manifest, root)
        lanes = active_lanes(config, retired)
        state_path = root / "batch-progress.json"
        if state_path.exists():
            state = read_json(state_path)
            if state.get("manifest_digest") != manifest["digest"] or state.get("policy") != policy:
                raise CampaignError("saved history state uses a different manifest or execution policy")
        else:
            state = {"manifest_digest": manifest["digest"], "repository": repository, "policy": policy,
                     "created_at": now_iso(), "status": "running", "prs": {
                         str(item["number"]): {"status": "pending", "head": item["head_sha"]}
                         for item in manifest["prs"]}}
        state["status"] = "running"
        state["active_lanes"] = [lane["key"] for lane in lanes]
        state["retired_lanes"] = list(retired)
        emit("Active review lanes: " + ", ".join(state["active_lanes"]))
        _save_progress(root, state)
        processed, low_disk = 0, False
        state.pop("pause_reason", None)
        terminal = {"published", "partial", "already_published", "reviewed", "review_failed"}
        for item in manifest["prs"]:
            number = item["number"]
            record = state["prs"][str(number)]
            if record["status"] in terminal: continue
            if stop.is_set() or (max_prs is not None and processed >= max_prs): break
            if shutil.disk_usage(root).free < 2 * 1024 ** 3:
                low_disk = True
                state["pause_reason"] = "Less than 2 GiB free on the output filesystem; free space before resuming"
                emit(state["pause_reason"])
                break
            processed += 1
            record.update(status="running", started_at=now_iso())
            record.pop("error", None)
            _save_progress(root, state)
            emit(f"History PR #{number}: starting ({processed} this invocation)")
            try:
                current = client.get_pr(repository, number)
                if (not current.get("merged") or current["head_sha"] != item["head_sha"]
                        or current.get("merge_commit_sha") != item["merge_commit_sha"]):
                    raise CampaignError("merged PR identity differs from the frozen cohort")
                existing = _existing_complete_reviews(client, repository, item, lanes) if publish == "github" else None
                if existing:
                    record.update(status="already_published", reviews=existing, finished_at=now_iso())
                    _save_progress(root, state)
                    emit(f"History PR #{number}: all model reviews already published; skipped inference")
                    continue
                run_dir = root / "prs" / f"{number}-{item['head_sha'][:12]}"
                record["run_dir"] = str(run_dir)
                saved_path = run_dir / "local-manifest.json"
                prepared = None
                if saved_path.exists():
                    saved = read_json(saved_path)
                    verify_seal(saved)
                    prepared = saved["prepared"]
                result = reviewer(config, repository, number, run_dir, workspace, mock=mock,
                                   expected_head=item["head_sha"], client=client, prepared=prepared,
                                   stop_event=stop, emit=emit, retired_lanes=retired,
                                   **({"render_draft": False} if hold_invalid_findings else {}))
                record["lanes"] = result["lanes"]
                if stop.is_set():
                    record["status"] = "interrupted"
                    _save_progress(root, state)
                    break
                successful = [lane for lane in lanes if result["lanes"][lane["key"]]["status"] == "complete"]
                record["omitted_models"] = [lane["model"] for lane in lanes
                                             if result["lanes"][lane["key"]]["status"] != "complete"]
                if not successful or (result["status"] != "complete" and not allow_partial):
                    record.update(status="review_failed", error="One or more required model reviews failed")
                elif publish == "github":
                    # Validate and render locally before any remote write.
                    publisher(config, run_dir, client=client, allow_partial=allow_partial,
                               allow_merged=True, dry_run=True, retired_lanes=retired,
                               **({"hold_invalid_findings": True} if hold_invalid_findings else {}))
                    receipt = publisher(config, run_dir, client=client, allow_partial=allow_partial,
                                         allow_merged=True, retired_lanes=retired,
                                         **({"hold_invalid_findings": True} if hold_invalid_findings else {}))
                    record.update(status="partial" if receipt["omitted_models"] else "published",
                                   reviews=receipt["reviews"], omitted_models=receipt["omitted_models"],
                                   receipt_digest=receipt["digest"], withheld_count=receipt.get("withheld_count", 0))
                else:
                    record["status"] = "reviewed" if result["status"] == "complete" else "review_failed"
                record["finished_at"] = now_iso()
            except Exception as exc:
                record.update(status="failed", error=str(exc), finished_at=now_iso())
            _save_progress(root, state)
            emit(f"History PR #{number}: {record['status']}" + (f" — {record['error']}" if record.get("error") else ""))
        pending = any(item["status"] in {"pending", "running", "interrupted"} for item in state["prs"].values())
        failures = any(item["status"] in {"failed", "partial", "review_failed"} for item in state["prs"].values())
        state["status"] = "paused_low_disk" if low_disk else "stopped" if stop.is_set() else "paused" if pending else "complete_with_failures" if failures else "complete"
        _save_progress(root, state)
        return history_status(root)
