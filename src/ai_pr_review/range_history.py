"""Plan missing PR reviews from a pinned release-to-branch commit range."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

from .common import CampaignError, atomic_json, digest, now_iso, read_json, seal, verify_seal
from .github_pr import GitHubPRClient
from .history import _validate_manifest, select_latest
from .pr_service import service_lock
from .prompts import PROMPT_DIGEST
from .runner import RUNNER_VERSION
from .snapshot import SNAPSHOT_VERSION


def range_commits(client, repository, base, head):
    """Paginate by immutable SHAs; the unpaginated compare API truncates at 250."""
    commits, seen, page = [], set(), 1
    total = None
    while True:
        value = client._api([f"repos/{repository}/compare/{base}...{head}?per_page=100&page={page}"])
        if (value.get("status") not in {"ahead", "identical"} or value.get("behind_by") != 0
                or value.get("base_commit", {}).get("sha") != base
                or value.get("merge_base_commit", {}).get("sha") != base):
            raise CampaignError("release ref must be an ancestor of the pinned branch head")
        count = value.get("total_commits")
        if type(count) is not int or count < 0 or (total is not None and count != total):
            raise CampaignError("inconsistent comparison size")
        total = count
        batch = value.get("commits", [])
        for commit in batch:
            sha = commit["sha"]
            if sha in seen: raise CampaignError("duplicate commit in paginated comparison")
            seen.add(sha)
            commits.append({"sha": sha, "parents": [parent["sha"] for parent in commit["parents"]]})
        if len(commits) == total: break
        if not batch or len(commits) > total or page >= 100:
            raise CampaignError("incomplete or oversized comparison")
        page += 1
    if base in seen or (total and head not in seen):
        raise CampaignError("comparison endpoints do not match the pinned range")
    return commits


def covered_reviews(client, repository, pr, lanes):
    """A previously published partial review counts as covered, per batch policy."""
    remote, matches = client.list_reviews(repository, pr["number"]), []
    for lane in lanes:
        marker = f"<!-- ai-pr-review:model-review:{repository}:{pr['number']}:{pr['head_sha']}:{digest(lane['model'])} -->"
        found = [review for review in remote if review.get("state") == "COMMENTED"
                 and review.get("commit_id") == pr["head_sha"] and marker in (review.get("body") or "")]
        if len(found) > 1: raise CampaignError("duplicate remote model/head reviews require reconciliation")
        if found:
            matches.append({"model": lane["model"], "review_id": found[0]["id"], "review_url": found[0].get("html_url")})
    return matches


def plan_range(config, repository, root, base_ref, head_ref, *, client=None):
    config.repository(repository)
    if not base_ref or not head_ref: raise CampaignError("both range refs are required")
    root = Path(root).resolve()
    # Large release ranges can require several compare pages, complete merged-PR
    # pagination, and review coverage checks before any frozen state is written.
    client = client or GitHubPRClient(timeout=300)
    with service_lock(root):
        audit_path, manifest_path = root / "range-audit.json", root / "batch-manifest.json"
        if audit_path.exists():
            audit = read_json(audit_path)
            verify_seal(audit)
            if (audit["repository"] != repository or audit["base_ref"] != base_ref or audit["head_ref"] != head_ref
                    or audit["config_digest"] != config.digest):
                raise CampaignError("range directory belongs to a different selection")
            base, head, cutoff = audit["base_sha"], audit["head_sha"], audit["cutoff"]
        else:
            cutoff = now_iso()
            base = client._api([f"repos/{repository}/commits/{quote(base_ref, safe='')}"])["sha"]
            head = client._api([f"repos/{repository}/commits/{quote(head_ref, safe='')}"])["sha"]
            commits = range_commits(client, repository, base, head)
            commit_ids = {commit["sha"] for commit in commits}
            all_prs = client.list_merged_prs(repository)
            candidates = [pr for pr in all_prs if pr["merge_commit_sha"] in commit_ids]
            candidates = select_latest(candidates, len(candidates), cutoff) if candidates else []
            with ThreadPoolExecutor(max_workers=4) as pool:
                coverage = list(pool.map(lambda pr: covered_reviews(client, repository, pr, config.lanes), candidates))
            entries = [{"pr": pr, "reviews": reviews,
                        "coverage": "full" if len(reviews) == len(config.lanes) else "partial" if reviews else "missing"}
                       for pr, reviews in zip(candidates, coverage)]
            matched_merges = {pr["merge_commit_sha"] for pr in candidates}
            audit = seal({"version": 1, "repository": repository, "base_ref": base_ref, "head_ref": head_ref,
                          "base_sha": base, "head_sha": head, "cutoff": cutoff, "config_digest": config.digest,
                          "selection": "PR merge commit belongs to base..head; any exact-head active-model review counts as covered",
                          "enumerated_merged_prs": len(all_prs), "commits": commits, "prs": entries,
                          "unmapped_merge_commits": [c["sha"] for c in commits if len(c["parents"]) > 1 and c["sha"] not in matched_merges]})
            atomic_json(audit_path, audit)
        missing = [entry["pr"] for entry in audit["prs"] if entry["coverage"] == "missing"]
        if manifest_path.exists():
            manifest = read_json(manifest_path)
            _validate_manifest(manifest, config)
            if manifest.get("range_audit_digest") != audit["digest"] or manifest["prs"] != missing:
                raise CampaignError("range audit and batch manifest differ")
        elif missing:
            manifest = seal({"version": 1, "repository": repository, "count": len(missing), "cutoff": cutoff,
                             "selection": audit["selection"], "enumerated_merged_prs": audit["enumerated_merged_prs"],
                             "prs": missing, "config_digest": config.digest, "prompt_digest": PROMPT_DIGEST,
                             "runner_version": RUNNER_VERSION, "snapshot_version": SNAPSHOT_VERSION,
                             "range_audit_digest": audit["digest"], "base_sha": base, "head_sha": head})
            _validate_manifest(manifest, config)
            atomic_json(manifest_path, manifest)
        atomic_json(root / "original-config.json", config.data)
        lines = ["# Release-to-branch PR review coverage", "", f"Repository: {repository}",
                 f"Range: `{base_ref}` (`{base}`) → `{head_ref}` (`{head}`)", f"Frozen: {cutoff}", "",
                 "Existing partial reviews count as covered. Only uncovered PRs are scheduled.",
                 "This is per-PR coverage, not an aggregate review of every commit in the range.", "",
                 "| PR | Coverage | Base branch | Title |", "| --- | --- | --- | --- |"]
        for entry in audit["prs"]:
            pr = entry["pr"]
            title = pr["title"].replace("|", "\\|").replace("\n", " ")
            lines.append(f"| [#{pr['number']}]({pr['url']}) | {entry['coverage']} | {pr['base_ref']} | {title} |")
        (root / "COHORT.md").write_text("\n".join(lines) + "\n")
        parents = {commit["sha"]: commit["parents"] for commit in audit["commits"]}
        return {"repository": repository, "base_sha": base, "head_sha": head, "commits": len(audit["commits"]),
                "range_prs": len(audit["prs"]), "covered_prs": len(audit["prs"]) - len(missing),
                "existing_partial_prs": [entry["pr"]["number"] for entry in audit["prs"] if entry["coverage"] == "partial"],
                "missing_prs": [pr["number"] for pr in missing], "unmapped_merge_commits": audit["unmapped_merge_commits"],
                "ambiguous_merge_prs": [pr["number"] for pr in missing
                    if len(parents[pr["merge_commit_sha"]]) != 2 or parents[pr["merge_commit_sha"]][1] != pr["head_sha"]],
                "run_dir": str(root), "audit_digest": audit["digest"]}
