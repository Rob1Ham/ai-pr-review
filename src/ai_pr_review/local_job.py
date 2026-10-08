"""Local historical replay using real snapshots and locally captured publication."""

from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
from pathlib import Path
import re

from .common import CampaignError, atomic_json, digest, read_json, seal, verify_seal
from .git_mirror import GitMirror, _git, build_pr_scope
from .github_pr import GitHubPRClient, PRReviewPublisher
from .pr_service import PRService, service_lock
from .prompts import PROMPT_DIGEST, build_prompt
from .runner import MockRunner, OpenCodeRunner, RUNNER_VERSION, credential_status, validate_report
from .snapshot import SNAPSHOT_VERSION, verify_snapshot
from .retirement import active_lanes


class LocalPublication:
    """Capture the real publisher's API payloads without a GitHub write client."""

    def __init__(self, root, *, fresh=False):
        self.path = Path(root) / "publication-draft.json"
        self.data = read_json(self.path) if self.path.exists() and not fresh else {"reviews": []}

    def list_reviews(self, *_): return self.data["reviews"]

    def create_review(self, repo, number, head, body, comments, *, event):
        item = {"id": len(self.data["reviews"]) + 1, "commit_id": head,
                "body": body, "comments": comments, "event": event}
        self.data["reviews"].append(item)
        atomic_json(self.path, self.data)
        return item


def historical_base(repo, pr, explicit=None, *, commits=None):
    """Verify either a direct merge base or an exact linear stacked-PR range."""
    if explicit:
        if not re.fullmatch(r"[0-9a-f]{40}", explicit):
            raise CampaignError("base must be a full commit SHA")
        candidate, source = explicit, "explicit-base"
    elif pr.get("merged"):
        merge = pr.get("merge_commit_sha")
        if not isinstance(merge, str) or not re.fullmatch(r"[0-9a-f]{40}", merge):
            raise CampaignError("merged PR has no verifiable merge commit; supply --base")
        parents = _git(repo, "rev-list", "--parents", "-n", "1", merge).decode().split()
        if len(parents) != 3 or parents[2] != pr["head_sha"] or commits is not None:
            # GitHub can mark a stacked PR merged by a later stack-tip merge.
            # Verify its entire linear PR commit list against immutable Git
            # parents and prove the original head is contained in the merge.
            if not commits or len(commits) >= 250:
                raise CampaignError("squash/rebase or ambiguous merge history; supply a verified --base")
            shas = [item.get("sha") for item in commits]
            if (any(not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha) for sha in shas)
                    or len(set(shas)) != len(shas) or shas[-1] != pr["head_sha"]):
                raise CampaignError("stacked PR commit list does not match the original head")
            candidate, previous = None, None
            for sha in shas:
                row = _git(repo, "rev-list", "--parents", "-n", "1", sha).decode().split()
                if len(row) != 2 or (previous is not None and row[1] != previous):
                    raise CampaignError("stacked PR commits must form a complete linear chain")
                if candidate is None: candidate = row[1]
                previous = sha
            if _git(repo, "merge-base", pr["head_sha"], merge).decode().strip() != pr["head_sha"]:
                raise CampaignError("original PR head is not contained in its recorded merge")
            if set(_git(repo, "rev-list", f"{candidate}..{pr['head_sha']}").decode().split()) != set(shas):
                raise CampaignError("stacked PR range differs from the GitHub commit list")
            return candidate, {"source": "verified-stacked-pr-commits", "candidate": candidate,
                               "merge_base": candidate, "merge_commit": merge, "commits": shas}
        candidate, source = parents[1], "verified-merge-first-parent"
    else:
        candidate, source = pr["base_sha"], "github-base-sha"
    base = _git(repo, "merge-base", candidate, pr["head_sha"]).decode().strip()
    if base == pr["head_sha"]:
        raise CampaignError("base already contains the PR head; refusing an empty historical review")
    return base, {"source": source, "candidate": candidate, "merge_base": base}


def _resolve_historical_base(repo, pr, client, repository, explicit=None):
    commits = None
    github_base = None
    if not explicit and pr.get("merged") and isinstance(pr.get("merge_commit_sha"), str) and re.fullmatch(r"[0-9a-f]{40}", pr["merge_commit_sha"]):
        parents = _git(repo, "rev-list", "--parents", "-n", "1", pr["merge_commit_sha"]).decode().split()
        direct_merge = len(parents) == 3 and parents[2] == pr["head_sha"]
        if direct_merge and isinstance(pr.get("base_sha"), str) and re.fullmatch(r"[0-9a-f]{40}", pr["base_sha"]):
            # A stack-tip merge can include its ancestors on master even though
            # the PR itself targets a narrower stacked base. Do not attribute
            # the entire stack's diff to that PR.
            github_base = _git(repo, "merge-base", pr["base_sha"], pr["head_sha"]).decode().strip()
            merge_base = _git(repo, "merge-base", parents[1], pr["head_sha"]).decode().strip()
            if github_base in {merge_base, pr["head_sha"]}: github_base = None
        if not direct_merge or github_base is not None:
            commits = client.list_pr_commits(repository, pr["number"])
            if not commits:
                raise CampaignError("ambiguous merge history without a complete PR commit list")
    base, provenance = historical_base(repo, pr, explicit, commits=commits)
    if github_base is not None and base != github_base:
        raise CampaignError("verified PR commit range does not match the GitHub PR base")
    return base, provenance


class SnapshotMockRunner(MockRunner):
    """Exercise snapshot verification and prompt construction before fake inference."""

    def __init__(self, prepared, checklist):
        super().__init__()
        self.prepared, self.checklist = prepared, checklist

    def run(self, job, scope, cancel):
        verify_snapshot(Path(self.prepared["snapshot_root"]), self.prepared["snapshot_index"])
        prompt = build_prompt(job, scope, self.prepared["snapshot_index"], self.checklist)
        if len(prompt.encode()) > job["budget"]["max_prompt_bytes"]:
            raise CampaignError("prompt exceeds limit")
        return super().run(job, scope, cancel)


def review_local(config, repository, number, run_dir, workspace, *, mock=False,
                 base=None, expected_head=None, client=None, prepared=None, runner_factory=None,
                  emit=lambda text: None, stop_event=None, retired_lanes=(), render_draft=True):
    """Run active lanes, resume saved successes, and render publication locally.

    The client is used only for get_pr. All publication calls go to LocalPublication.
    Mock and live executions have separate identities and cannot reuse each other's results.
    """
    config.repository(repository)
    lanes = active_lanes(config, retired_lanes)
    client = client or GitHubPRClient()
    pr = client.get_pr(repository, number)
    if pr["state"] != "open" and not pr.get("merged"):
        raise CampaignError("closed-unmerged PRs are not eligible for local replay")
    if expected_head and expected_head != pr["head_sha"]:
        raise CampaignError("PR head does not match --head")
    if not mock and runner_factory is None and not all(x["available"] for x in credential_status(lanes)):
        raise CampaignError("configure provider credentials or AI_PR_REVIEW_OPENCODE_AUTH_FILE")
    run_dir, workspace = Path(run_dir).resolve(), Path(workspace).resolve()
    with service_lock(run_dir):
        if prepared is None:
            emit(f"PR #{number}: fetching immutable Git objects and preparing snapshot")
            mirror = GitMirror(workspace / "mirrors")
            repo = mirror._ensure(repository)
            refs = [pr["head_sha"], pr["base_sha"]]
            if pr.get("merged") and pr.get("merge_commit_sha"): refs.append(pr["merge_commit_sha"])
            if base: refs.append(base)
            if any(not re.fullmatch(r"[0-9a-f]{40}", ref) for ref in refs):
                raise CampaignError("invalid GitHub commit identity")
            _git(repo, "fetch", "--no-tags", "origin", *dict.fromkeys(refs))
            resolved_base, provenance = _resolve_historical_base(repo, pr, client, repository, base)
            snapshot_id = digest({"repository": repository, "number": number,
                                  "base": resolved_base, "head": pr["head_sha"], "config": config.digest,
                                  "snapshot_version": SNAPSHOT_VERSION})
            prepared = build_pr_scope(repo, resolved_base, pr["head_sha"], repository=repository,
                                      number=number, metadata=pr, config=config,
                                      destination=workspace / "inputs" / snapshot_id)
            prepared["base_provenance"] = provenance
        emit(f"PR #{number}: snapshot ready ({len(prepared['scope']['paths'])} changed files)")
        material = {"repository": repository, "pr": number, "head": pr["head_sha"],
                    "base": prepared["merge_base"], "config": config.digest, "prompt": PROMPT_DIGEST,
                    "runner": RUNNER_VERSION, "mode": "mock" if mock else "live",
                    "snapshot": prepared["snapshot_index"]["digest"]}
        manifest = seal({**material, "prepared": prepared})
        manifest_path = run_dir / "local-manifest.json"
        if manifest_path.exists():
            saved = read_json(manifest_path)
            verify_seal(saved)
            if saved != manifest:
                raise CampaignError("run directory belongs to a different review; use a new --run-dir")
        else:
            atomic_json(manifest_path, manifest)
        identity = digest(material)
        if runner_factory is None:
            def runner_factory(lane, inputs):
                if mock: return SnapshotMockRunner(inputs, config.checklist)
                return OpenCodeRunner(lambda *_: (Path(inputs["snapshot_root"]), inputs["snapshot_index"]),
                                      None, config.tiers["deep"], config.checklist)
        service = PRService(config, client, None, runner_factory, state_dir=run_dir, stop_event=stop_event)
        service._load()
        record = service.state["heads"].setdefault(identity, {
            "number": number, "status": "pending", "lanes": {
                lane["key"]: {"status": "pending", "attempts": 0} for lane in config.lanes}})
        if set(record.get("retired_lanes", [])) - set(retired_lanes):
            raise CampaignError("retired lanes cannot be re-enabled for a saved run")
        record["retired_lanes"] = list(retired_lanes)
        limit = config.service["max_attempts_per_lane"]
        while not service.stop.is_set():
            pending = [lane for lane in lanes if record["lanes"][lane["key"]]["status"] != "complete"
                       and record["lanes"][lane["key"]]["attempts"] < limit]
            if not pending: break
            emit(f"PR #{number}: running lanes {', '.join(lane['key'] for lane in pending)}")
            with ThreadPoolExecutor(max_workers=len(pending)) as pool:
                futures = {pool.submit(service._run_lane, identity, record, lane, prepared): lane for lane in pending}
                for future in as_completed(futures):
                    key = futures[future]["key"]
                    try:
                        future.result()
                        emit(f"PR #{number}: {key} complete")
                    except Exception as exc:
                        emit(f"PR #{number}: {key} failed: {exc}")
        complete = all(record["lanes"][lane["key"]]["status"] == "complete" for lane in lanes)
        if complete and render_draft:
            results = [service._saved_result(identity, lane["key"], record["lanes"][lane["key"]]) for lane in lanes]
            publisher = PRReviewPublisher(LocalPublication(run_dir, fresh=True), config.service, run_dir / "draft-receipt.json")
            receipt = publisher.publish(repository, {**pr, "base_sha": prepared["merge_base"]},
                                        pr["head_sha"], results, prepared["line_map"])
            # Also regenerate the human-readable view when publication was cached.
            draft = read_json(run_dir / "publication-draft.json")
            (run_dir / "review.md").write_text("\n\n---\n\n".join(item["body"] for item in draft["reviews"]))
            record.update(status="complete", publication_draft=receipt)
        elif complete:
            # Batch publication will validate and render the sealed reports,
            # including any explicitly enabled invalid-finding withholding.
            record["status"] = "complete"
        else:
            record["status"] = "interrupted" if service.stop.is_set() else "failed"
        service._save()
        summary = {**material, "status": record["status"], "publication": "local-only",
                   "active_lanes": [lane["key"] for lane in lanes], "retired_lanes": list(retired_lanes),
                   "run_dir": str(run_dir), "lanes": record["lanes"],
                   "tier": prepared["scope"].get("tier_override"),
                   "changed_files": len(prepared["scope"]["paths"])}
        atomic_json(run_dir / "summary.json", summary)
        return summary


def publish_saved(config, run_dir, *, client=None, allow_partial=False, dry_run=False, allow_merged=False,
                  retired_lanes=(), hold_invalid_findings=False):
    """Explicitly submit previously validated live results without rerunning models."""
    root = Path(run_dir).resolve()
    lanes = active_lanes(config, retired_lanes)
    with service_lock(root):
        manifest = read_json(root / "local-manifest.json")
        verify_seal(manifest)
        if manifest["mode"] != "live" or manifest["config"] != config.digest:
            raise CampaignError("publication requires live results with the original config")
        declaration = config.repository(manifest["repository"])
        prepared = manifest["prepared"]
        verify_snapshot(Path(prepared["snapshot_root"]), prepared["snapshot_index"])
        state = read_json(root / "pr-service.json")
        if state.get("config_digest") != config.digest or len(state["heads"]) != 1:
            raise CampaignError("saved review state does not match the manifest")
        identity, record = next(iter(state["heads"].items()))
        if set(record.get("retired_lanes", [])) - set(retired_lanes):
            raise CampaignError("publication must honor saved lane retirement")
        # Initial local rendering can fail after every lane result was sealed,
        # leaving only the aggregate status pending. Recover from those saved
        # results under the run lock; pending individual lanes remain ineligible.
        completed_before_render = record["status"] == "pending" and all(
            record["lanes"].get(lane["key"], {}).get("status") == "complete" for lane in lanes)
        if record["status"] != "complete" and not completed_before_render and not (allow_partial and record["status"] == "failed"):
            raise CampaignError("all lanes must complete before publication (or explicitly use --allow-partial)")
        objects = {(item["commit"], item["path"]): item for item in prepared["snapshot_index"]["objects"]}
        results, omitted, validation = [], [], []
        for lane in lanes:
            if record["lanes"][lane["key"]]["status"] != "complete":
                if not allow_partial or record["lanes"][lane["key"]]["status"] != "failed":
                    raise CampaignError("publication cannot skip a pending or running lane")
                omitted.append(lane["model"])
                continue
            saved = read_json(root / "results" / f"{digest(identity)}-{lane['key']}.json")
            verify_seal(saved)
            if saved["digest"] != record["lanes"][lane["key"]]["result_digest"] or saved["identity"] != identity:
                raise CampaignError("saved lane result identity mismatch")
            job, result = saved["job"], saved["result"]
            if (job["lane"] != lane or job["head_sha"] != manifest["head"] or job["base_sha"] != manifest["base"]
                    or result.get("provenance", {}).get("mode") != "live"):
                raise CampaignError("saved lane provenance mismatch")
            validate_report(result["report"], job)
            accepted, withheld = [], []
            for finding in result["report"]["findings"]:
                invalid = []
                for evidence in finding.get("evidence", []):
                    source = objects.get((evidence.get("commit"), evidence.get("path")))
                    start, end = evidence.get("line_start"), evidence.get("line_end", evidence.get("line_start"))
                    if (not source or type(start) is not int or type(end) is not int
                            or not 1 <= start <= end <= source["lines"]):
                        invalid.append({"evidence": evidence, "source_lines": source["lines"] if source else None})
                reason = ("finding evidence is outside the immutable snapshot" if invalid else
                          "only findings present at the exact PR head may be published"
                          if finding.get("persists_at_head") != "present" else None)
                if reason:
                    if not hold_invalid_findings:
                        raise CampaignError(reason)
                    withheld.append({"local_id": finding.get("local_id"), "finding_digest": digest(finding),
                                      "reason": reason, "invalid": invalid,
                                      "persists_at_head": finding.get("persists_at_head")})
                else:
                    accepted.append(finding)
            validation.append({"lane": lane["key"], "model": lane["model"], "source_result_digest": saved["digest"],
                               "accepted_count": len(accepted), "withheld": withheld})
            if withheld:
                # Preserve the sealed model output. Publish only a derived view;
                # its old overall assessment may depend on a withheld claim.
                result = copy.deepcopy(result)
                report = result["report"]
                report["findings"] = accepted
                report["withheld_count"] = len(withheld)
                report["summary"] = (f"Publication validation: {len(accepted)} candidate finding(s) passed source-coordinate "
                                     f"and exact-head checks. {len(withheld)} candidate finding(s) were withheld because their "
                                     "evidence or head-presence assessment did not validate. The original model assessment is retained locally rather "
                                     "than published because it may refer to withheld claims.")
                report["limitations"] = [*report.get("limitations", []),
                    f"{len(withheld)} candidate finding(s) withheld by publication validation; no replacement coordinates or head-presence claims were inferred."]
            results.append(result)
        if not results: raise CampaignError("no completed model reviews to publish")
        client = client or GitHubPRClient()
        current = client.get_pr(manifest["repository"], manifest["pr"])
        if current["head_sha"] != manifest["head"] or current.get("draft"):
            raise CampaignError("PR is no longer an eligible open PR at the reviewed head")
        if allow_merged and current.get("merged") and current["state"] == "closed":
            # Reverify the recorded head against the actual merge's parents.
            # Historical reviews may target a feature branch instead of master.
            verified_base, _ = _resolve_historical_base(Path(prepared["repository_path"]), current, client, manifest["repository"])
            if verified_base != manifest["base"]:
                raise CampaignError("merged PR history no longer matches the reviewed base")
        elif current["state"] != "open" or current["base_ref"] != declaration["base_branch"]:
            raise CampaignError("PR is not eligible; merged PR publication requires --allow-merged")
        receipt_path = root / ("draft-receipt.json" if dry_run else "github-receipt.json")
        audit = seal({"version": 1, "manifest_digest": manifest["digest"], "lanes": validation})
        if hold_invalid_findings:
            atomic_json(root / "publication-validation.json", audit)
        publisher = PRReviewPublisher(LocalPublication(root, fresh=True) if dry_run else client, config.service, receipt_path)
        receipt = publisher.publish(manifest["repository"], {**current, "base_sha": manifest["base"]},
                                    manifest["head"], results, prepared["line_map"])
        receipt = seal({**receipt, "omitted_models": omitted, "publication": "local-only" if dry_run else "github",
                        "retired_models": [lane["model"] for lane in config.lanes if lane["key"] in retired_lanes],
                        "withheld_count": sum(len(item["withheld"]) for item in validation),
                        "validation_digest": audit["digest"]})
        atomic_json(receipt_path, receipt)
        if dry_run:
            draft = read_json(root / "publication-draft.json")
            (root / "review.md").write_text("\n\n---\n\n".join(item["body"] for item in draft["reviews"]))
        return receipt
