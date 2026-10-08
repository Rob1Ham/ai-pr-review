"""Deduplicated high-confidence current-head GitHub issue delivery."""

import json
from pathlib import Path
import re
import subprocess
import threading
from urllib.parse import quote

from .common import CampaignError, UnknownDelivery, atomic_json, canonical, digest, read_json
from .snapshot import read_evidence, _secret_reason
from .severity import estimate_severity, render_estimate

PUBLISH_VERSION = "github-publisher-v4"


def _gh(args, payload=None):
    try:
        result = subprocess.run(["gh", "api", *args], input=canonical(payload) if payload is not None else None,
                                text=True, capture_output=True, timeout=90)
    except (OSError, subprocess.TimeoutExpired) as exc: raise CampaignError("GitHub request failed or timed out") from exc
    if result.returncode: raise CampaignError("GitHub request failed; verify gh or GH_TOKEN access")
    try: return json.loads(result.stdout)
    except json.JSONDecodeError as exc: raise CampaignError("GitHub returned invalid JSON") from exc


def _gh_collection(endpoint):
    values = []
    for page in range(1, 101):
        separator = "&" if "?" in endpoint else "?"
        batch = _gh([f"{endpoint}{separator}page={page}"])
        if not isinstance(batch, list): raise CampaignError("GitHub returned an invalid collection")
        values.extend(batch)
        if len(batch) < 100: return values
    raise CampaignError("GitHub collection exceeds pagination limit")


class GitHubRemote:
    mode = "github"
    def __init__(self, repository):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository): raise CampaignError("invalid GitHub repository")
        self.repository = repository
    def issues(self):
        return _gh_collection(f"repos/{self.repository}/issues?state=all&per_page=100")
    def comments(self, number):
        return _gh_collection(f"repos/{self.repository}/issues/{number}/comments?per_page=100")
    def create(self, title, body):
        try:
            item = _gh([f"repos/{self.repository}/issues", "--method", "POST", "--input", "-"], {"title": title, "body": body})
            return {**item, "url": item["html_url"]}
        except CampaignError: raise UnknownDelivery("issue creation outcome unknown; reconcile before retry") from None
    def comment(self, number, body):
        try: return _gh([f"repos/{self.repository}/issues/{number}/comments", "--method", "POST", "--input", "-"], {"body": body})
        except CampaignError: raise UnknownDelivery("comment outcome unknown; reconcile before retry") from None


class MockRemote:
    mode = "mock"
    def __init__(self, path): self.path = Path(path); self.data = read_json(path) if self.path.exists() else {"issues": [], "comments": {}}
    def issues(self): return list(self.data["issues"])
    def comments(self, number): return self.data["comments"].get(str(number), [])
    def create(self, title, body):
        item = {"number": len(self.data["issues"]) + 1, "title": title, "body": body,
                "url": f"https://example.invalid/issues/{len(self.data['issues']) + 1}"}
        self.data["issues"].append(item); atomic_json(self.path, self.data); return item
    def comment(self, number, body):
        self.data["comments"].setdefault(str(number), []).append({"body": body}); atomic_json(self.path, self.data)


def render(manifest, job, result, finding):
    required = ("local_id", "root_cause", "broken_invariant", "expected", "observed", "impact",
                "remediation_direction", "confidence_rationale")
    if any(not isinstance(finding.get(key), str) or not finding[key].strip() for key in required):
        raise CampaignError("finding requires bounded nonempty explanatory fields")
    links = []
    for item in finding.get("evidence", []):
        if item.get("commit") not in {job["base_sha"], job["head_sha"], manifest["head_sha"]}: raise CampaignError("finding cites an unreviewed commit")
        read_evidence(Path(manifest["repository_path"]), item["commit"], item["path"], item["line_start"], item["line_end"])
        url = f"https://github.com/{manifest['repository']}/blob/{item['commit']}/{quote(item['path'], safe='/')}#L{item['line_start']}-L{item['line_end']}"
        links.append(f"- [{item['path']}:{item['line_start']}-{item['line_end']}]({url})")
    if not links: raise CampaignError("finding requires source evidence")
    first = finding["evidence"][0]; fingerprint = digest({"repository": manifest["repository"], "path": first["path"],
                                                         "symbol": first.get("symbol"), "root_cause": " ".join(finding["root_cause"].lower().split())})
    estimate = estimate_severity(finding)
    title = f"[Audit][PR #{job['pr']}][{estimate['severity'] or 'unassessed'}] Possible: {finding['root_cause'][:130]}"
    body = "\n".join([f"<!-- ai-pr-review:finding:{fingerprint} -->", "## Possible finding - needs triage", "",
                      f"- Campaign: `{job['run_id']}`", f"- Model: `{job['lane']['model']}` at `max` effort",
                      f"- Frozen head: `{manifest['head_sha']}`", f"- Confidence: **{finding['confidence']}** - {finding['confidence_rationale']}",
                      "", *render_estimate(estimate), "## Invariant", finding["broken_invariant"], "", "## Expected", finding["expected"],
                      "", "## Observed", finding["observed"], "", "## Impact", finding["impact"],
                      "", "## Remediation", finding["remediation_direction"], "", "## Evidence", *links])
    if _secret_reason("report.txt", body.encode()): raise CampaignError("finding text matches credential material")
    return fingerprint, title, body


class Publisher:
    def __init__(self, directory, manifest, remote=None):
        self.path, self.manifest, self.remote, self.lock = Path(directory) / "issues.json", manifest, remote, threading.Lock()
        mode = remote.mode if remote else "draft"
        self.data = read_json(self.path) if self.path.exists() else {"version": PUBLISH_VERSION, "mode": mode, "claims": {}, "fingerprints": {}}
        if self.data.get("version") != PUBLISH_VERSION or self.data.get("mode") != mode: raise CampaignError("publication receipt mode differs")
    def _save(self): atomic_json(self.path, self.data)
    def submit(self, job, result):
        if self.remote and self.remote.mode == "github" and result["provenance"].get("mode") != "live": raise CampaignError("synthetic findings cannot be published")
        with self.lock:
            for finding in result["report"]["findings"]:
                claim = digest([job["id"], finding.get("local_id"), finding.get("root_cause")])
                if self.data["claims"].get(claim, {}).get("status") in {"delivered", "draft", "historical", "needs-validation"}: continue
                saved = self.data["claims"].setdefault(claim, {})
                try:
                    key, title, body = render(self.manifest, job, result, finding)
                    estimate = estimate_severity(finding)
                    saved.update(fingerprint=key, title=title, body=body, severity_estimate=estimate)
                    current = any(item.get("commit") == self.manifest["head_sha"] for item in finding["evidence"])
                    if finding.get("persists_at_head") == "absent": saved["status"] = "historical"
                    elif finding.get("confidence") != "high" or finding.get("persists_at_head") != "present" or not current or finding.get("checked_head_sha") != self.manifest["head_sha"]: saved["status"] = "needs-validation"
                    elif not estimate["complete"]: saved["status"] = "needs-severity-assessment"
                    elif self.remote is None: saved["status"] = "draft"
                    else: self._deliver(claim, saved)
                except CampaignError as exc: saved.update(status="error", error=str(exc))
                self._save()
    def _deliver(self, claim, saved):
        marker = f"<!-- ai-pr-review:finding:{saved['fingerprint']} -->"; claim_marker = f"<!-- ai-pr-review:claim:{claim} -->"
        issues = self.remote.issues(); matches = [item for item in issues if marker in (item.get("body") or "")]
        if len(matches) > 1: raise CampaignError("duplicate remote finding markers")
        if matches:
            issue = matches[0]
            if not any(claim_marker in (item.get("body") or "") for item in self.remote.comments(issue["number"])):
                self.remote.comment(issue["number"], saved["body"] + "\n" + claim_marker)
        else:
            saved["status"] = "unknown"; self._save(); issue = self.remote.create(saved["title"], saved["body"] + "\n" + claim_marker)
        saved.update(status="delivered", url=issue["url"], number=issue["number"])
        self.data["fingerprints"][saved["fingerprint"]] = {"number": issue["number"], "url": issue["url"]}
    def summary(self):
        counts = {}
        for item in self.data["claims"].values(): counts[item["status"]] = counts.get(item["status"], 0) + 1
        return {"mode": self.data["mode"], "claims": counts,
                "issue_urls": sorted(item["url"] for item in self.data["fingerprints"].values())}
