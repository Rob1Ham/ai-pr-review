"""Publish one comprehensive, deduplicated GitHub issue per validated historical finding."""

from pathlib import Path
from urllib.parse import quote

from .common import CampaignError, atomic_json, digest, read_json, verify_seal
from .publish import GitHubRemote, MockRemote
from .snapshot import _secret_reason, read_evidence, verify_snapshot


_LEVELS = {"critical", "high", "medium", "low", "info"}
_CONFIDENCE = {"high", "medium", "low"}


def _language(path):
    return {".rs": "rust", ".ts": "typescript", ".tsx": "tsx", ".js": "javascript",
            ".py": "python", ".json": "json", ".toml": "toml", ".yml": "yaml",
            ".yaml": "yaml", ".sql": "sql", ".sh": "bash"}.get(Path(path).suffix, "text")


def _text(finding, key):
    value = finding.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > 12_000:
        raise CampaignError(f"history issue finding requires bounded {key}")
    return value.strip()


def render_history_issue(repository, pr, base, head, model_key, model, finding, repository_path):
    severity, confidence = finding.get("severity"), finding.get("confidence")
    if severity not in _LEVELS or confidence not in _CONFIDENCE:
        raise CampaignError("history issue finding has invalid severity or confidence")
    if finding.get("persists_at_head") != "present" or finding.get("checked_head_sha") != head:
        raise CampaignError("history issue finding is not established at the exact reviewed head")
    expected, observed, impact = (_text(finding, key) for key in ("expected", "observed", "impact"))
    root_cause = _text(finding, "root_cause")
    eli5 = finding.get("eli5")
    if not isinstance(eli5, str) or not eli5.strip():
        eli5 = (f"A safety rule in the software can be broken because {root_cause.rstrip('.').lower()}. "
                f"If that happens, {impact.rstrip('.').lower()}.")
    evidence_blocks, has_head = [], False
    for item in finding.get("evidence", []):
        try:
            commit, path = item["commit"], item["path"]
            start, end = item["line_start"], item.get("line_end", item["line_start"])
        except (KeyError, TypeError) as exc:
            raise CampaignError("history issue finding has malformed evidence") from exc
        if commit not in {base, head}:
            # read_evidence below still verifies immutable repository membership;
            # this branch rejects arbitrary model-supplied commits.
            raise CampaignError("history issue finding cites an unreviewed commit")
        snippet = read_evidence(Path(repository_path), commit, path, start, end).rstrip()
        if len(snippet) > 6_000:
            snippet = snippet[:6_000].rstrip() + "\n// Evidence excerpt truncated; use the immutable source link for the full range."
        has_head = has_head or commit == head
        url = f"https://github.com/{repository}/blob/{commit}/{quote(path, safe='/')}#L{start}-L{end}"
        evidence_blocks.extend([f"### [`{path}:{start}-{end}`]({url})", "",
                                f"```{_language(path)}", snippet, "```", ""])
    if not evidence_blocks or not has_head:
        raise CampaignError("history issue finding requires exact-head code evidence")
    fingerprint = digest({"repository": repository, "pr": pr, "head": head, "model": model,
                          "path": finding["evidence"][0]["path"], "root_cause": " ".join(root_cause.lower().split())})
    title = f"[{severity.upper()}] [{confidence.upper()}-confidence] [{model_key.upper()}] {root_cause}"[:256]
    marker = f"<!-- ai-pr-review:history-issue:{fingerprint} -->"
    body = "\n".join([marker, "## Explain like I'm 5", "", eli5.strip(), "",
                       "## Triage", "", f"- Severity: **{severity.upper()}**",
                       f"- Confidence: **{confidence.upper()}**", f"- Model: `{model}`",
                       f"- Source PR: [#{pr}](https://github.com/{repository}/pull/{pr})",
                       f"- Reviewed head: `{head}`", "", "## Confidence rationale", "",
                       _text(finding, "confidence_rationale"), "", "## Root cause", "", root_cause,
                       "", "## Broken invariant", "", _text(finding, "broken_invariant"),
                       "", "## Expected behavior", "", expected, "", "## Observed behavior", "",
                       observed, "", "## Impact", "", impact, "", "## Preconditions", "",
                       *([f"- {item}" for item in finding.get("preconditions", [])] or ["- No additional preconditions reported."]),
                       "", "## Recommended remediation", "", _text(finding, "remediation_direction"),
                       "", "## Validation gap", "", _text(finding, "validation_gap"),
                       "", "## Code evidence", "", *evidence_blocks]).rstrip() + "\n"
    if _secret_reason("history-issue.md", body.encode()):
        raise CampaignError("history issue text matches credential material")
    if len(body) > 60_000:
        body = body[:59_000].rstrip() + "\n\n_Issue detail was truncated to GitHub's size limit; immutable evidence links above remain authoritative._\n"
    return fingerprint, title, body


def publish_history_issues(config, root, *, dry_run=False):
    root = Path(root).resolve()
    batch, progress = read_json(root / "batch-manifest.json"), read_json(root / "batch-progress.json")
    verify_seal(batch)
    if batch["config_digest"] != config.digest or progress.get("manifest_digest") != batch["digest"]:
        raise CampaignError("history issue publication does not match the frozen batch")
    remote = MockRemote(root / "history-issues-draft.json") if dry_run else GitHubRemote(batch["repository"])
    receipt_path = root / ("history-issues-dry-run.json" if dry_run else "history-issues.json")
    receipt = read_json(receipt_path) if receipt_path.exists() else {"version": 1, "repository": batch["repository"], "claims": {}}
    existing = remote.issues()
    allowed_statuses = {"reviewed", "published", "partial", "already_published"} if dry_run else {"published", "partial", "already_published"}
    for item in batch["prs"]:
        if progress["prs"][str(item["number"])]["status"] not in allowed_statuses:
            continue
        run_dir = root / "prs" / f"{item['number']}-{item['head_sha'][:12]}"
        manifest = read_json(run_dir / "local-manifest.json"); verify_seal(manifest)
        if not dry_run and manifest["mode"] != "live":
            raise CampaignError("GitHub issues require live model results")
        verify_snapshot(Path(manifest["prepared"]["snapshot_root"]), manifest["prepared"]["snapshot_index"])
        for result_path in sorted((run_dir / "results").glob("*.json")):
            saved = read_json(result_path); verify_seal(saved)
            lane, result = saved["job"]["lane"], saved["result"]
            for finding in result["report"]["findings"]:
                try:
                    key, title, body = render_history_issue(batch["repository"], item["number"], manifest["base"], item["head_sha"],
                                                            lane["key"], lane["model"], finding,
                                                            manifest["prepared"]["repository_path"])
                except CampaignError as exc:
                    claim = digest([item["number"], lane["model"], finding.get("local_id")])
                    receipt["claims"][claim] = {"status": "withheld", "reason": str(exc)}
                    atomic_json(receipt_path, receipt)
                    continue
                marker = f"<!-- ai-pr-review:history-issue:{key} -->"
                matches = [issue for issue in existing if marker in (issue.get("body") or "")]
                if len(matches) > 1:
                    raise CampaignError("duplicate history issue markers require reconciliation")
                issue = matches[0] if matches else remote.create(title, body)
                if not matches:
                    existing.append(issue)
                receipt["claims"][key] = {"status": "draft" if dry_run else "delivered", "number": issue["number"],
                                           "url": issue["url"], "title": title}
                atomic_json(receipt_path, receipt)
    counts = {}
    for claim in receipt["claims"].values(): counts[claim["status"]] = counts.get(claim["status"], 0) + 1
    return {"mode": "dry-run" if dry_run else "github", "counts": counts,
            "issue_urls": sorted(claim["url"] for claim in receipt["claims"].values() if claim.get("url"))}


def publish_local_issues(config, run_dir, *, dry_run=False):
    """Publish findings from one sealed local PR review, including an open release PR."""
    root = Path(run_dir).resolve()
    manifest = read_json(root / "local-manifest.json"); verify_seal(manifest)
    if manifest["config"] != config.digest or not dry_run and manifest["mode"] != "live":
        raise CampaignError("local issue publication requires matching live review results")
    prepared = manifest["prepared"]
    verify_snapshot(Path(prepared["snapshot_root"]), prepared["snapshot_index"])
    remote = MockRemote(root / "local-issues-draft.json") if dry_run else GitHubRemote(manifest["repository"])
    receipt_path = root / ("local-issues-dry-run.json" if dry_run else "local-issues.json")
    receipt = read_json(receipt_path) if receipt_path.exists() else {"version": 1, "repository": manifest["repository"], "claims": {}}
    existing = remote.issues()
    for result_path in sorted((root / "results").glob("*.json")):
        saved = read_json(result_path); verify_seal(saved)
        lane, result = saved["job"]["lane"], saved["result"]
        if result.get("provenance", {}).get("mode") != manifest["mode"]:
            raise CampaignError("local issue result provenance does not match the review")
        for finding in result["report"]["findings"]:
            try:
                key, title, body = render_history_issue(manifest["repository"], manifest["pr"], manifest["base"],
                                                        manifest["head"], lane["key"], lane["model"], finding,
                                                        prepared["repository_path"])
            except CampaignError as exc:
                claim = digest([manifest["pr"], lane["model"], finding.get("local_id")])
                receipt["claims"][claim] = {"status": "withheld", "reason": str(exc)}
                atomic_json(receipt_path, receipt)
                continue
            marker = f"<!-- ai-pr-review:history-issue:{key} -->"
            matches = [issue for issue in existing if marker in (issue.get("body") or "")]
            if len(matches) > 1: raise CampaignError("duplicate local issue markers require reconciliation")
            issue = matches[0] if matches else remote.create(title, body)
            if not matches: existing.append(issue)
            receipt["claims"][key] = {"status": "draft" if dry_run else "delivered", "number": issue["number"],
                                       "url": issue["url"], "title": title}
            atomic_json(receipt_path, receipt)
    counts = {}
    for claim in receipt["claims"].values(): counts[claim["status"]] = counts.get(claim["status"], 0) + 1
    return {"mode": "dry-run" if dry_run else "github", "counts": counts,
            "issue_urls": sorted(claim["url"] for claim in receipt["claims"].values() if claim.get("url"))}
