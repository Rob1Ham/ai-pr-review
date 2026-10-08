"""Generic GitHub pull-request discovery and review publication via ``gh api``."""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
from urllib.parse import quote

from .common import CampaignError, UnknownDelivery, atomic_json, digest, seal
from .snapshot import _secret_reason


_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?")
_SHA = re.compile(r"[0-9a-fA-F]{7,64}")
_SEVERITIES = {"critical", "high", "medium", "low", "info"}
_CONFIDENCES = {"high", "medium", "low"}
_BODY_LIMIT = 32_000
_SECRET_TEXT = re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})")


def _repository(value: str) -> str:
    if not isinstance(value, str) or value.count("/") != 1:
        raise CampaignError("repository must be strict owner/name")
    owner, name = value.split("/")
    if not _OWNER.fullmatch(owner) or not _NAME.fullmatch(name) or name in {".", ".."}:
        raise CampaignError("repository must be strict owner/name")
    return f"{owner}/{name}"


def _pages(value):
    if not isinstance(value, list):
        raise CampaignError("GitHub returned an unexpected response")
    if value and all(isinstance(page, list) for page in value):
        return [item for page in value for item in page]
    return value


class GitHubPRClient:
    """Small REST client which deliberately delegates authentication to ``gh``.

    ``GH_TOKEN`` is inherited from the environment at request time. Tokens are
    never accepted by this object, retained, or included in errors. Creating or
    updating Checks may require a GitHub App or appropriately scoped fine-grained
    token; this client does not broaden token permissions.
    """

    def __init__(self, *, timeout: float = 90):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise CampaignError("GitHub timeout must be positive")
        self.timeout = timeout

    def _api(self, args: list[str], payload=None, *, unknown_write=False):
        command = ["gh", "api", *args]
        try:
            completed = subprocess.run(
                command,
                input=json.dumps(payload, sort_keys=True, separators=(",", ":")) if payload is not None else None,
                text=True,
                capture_output=True,
                timeout=self.timeout,
                env=os.environ.copy(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            error = UnknownDelivery("GitHub write outcome is unknown; reconcile before retry") if unknown_write else CampaignError("GitHub request failed or timed out")
            raise error from exc
        if completed.returncode:
            # gh stderr can contain request bodies, credentials, and provider details.
            error = UnknownDelivery("GitHub write outcome is unknown; reconcile before retry") if unknown_write else CampaignError("GitHub request failed; verify gh and GH_TOKEN access")
            raise error
        if not completed.stdout.strip():
            return {}
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise CampaignError("GitHub returned invalid JSON") from exc

    def _list(self, endpoint: str):
        # Older supported gh releases do not provide `api --slurp`. These API
        # collections use per_page=100 and are bounded in normal operation.
        # `--slurp` wraps every page set in one JSON array, including the
        # single-page object responses (for example check-runs) that plain
        # `--paginate` would emit unwrapped.
        return _pages(self._api([endpoint, "--paginate", "--slurp"]))

    @staticmethod
    def _normalize_pr(item: dict) -> dict:
        try:
            head_repo = item.get("head", {}).get("repo") or {}
            return {
                "number": int(item["number"]),
                "url": str(item["html_url"]),
                "title": str(item.get("title") or "")[:512],
                "body": str(item.get("body") or "")[:_BODY_LIMIT],
                "base_ref": str(item["base"]["ref"]),
                "base_sha": str(item["base"]["sha"]),
                "head_sha": str(item["head"]["sha"]),
                "head_repo": head_repo.get("full_name"),
                "draft": bool(item.get("draft", False)),
                "state": str(item.get("state") or "").lower(),
                "merged": item.get("merged") is True,
                "merge_commit_sha": item.get("merge_commit_sha"),
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise CampaignError("GitHub returned malformed pull-request metadata") from exc

    def list_open_prs(self, repo: str, base_branches=None) -> list[dict]:
        repo = _repository(repo)
        if isinstance(base_branches, str):
            raise CampaignError("base branches must be a collection of branch names")
        branches = None if base_branches is None else set(base_branches)
        if branches is not None and any(not isinstance(branch, str) or not branch for branch in branches):
            raise CampaignError("base branches must be nonempty strings")
        items = self._list(f"repos/{repo}/pulls?state=open&per_page=100")
        result = [self._normalize_pr(item) for item in items]
        return [item for item in result if branches is None or item["base_ref"] in branches]

    def get_pr(self, repo: str, number: int) -> dict:
        return self._normalize_pr(self._api([f"repos/{_repository(repo)}/pulls/{self._number(number)}"]))

    def list_merged_prs(self, repo: str) -> list[dict]:
        """Enumerate all merged PRs; creation/update ordering is not merge ordering."""
        owner, name = _repository(repo).split("/")
        query = """query($owner:String!,$name:String!,$cursor:String) {
          repository(owner:$owner,name:$name) {
            pullRequests(first:100,after:$cursor,states:MERGED,orderBy:{field:CREATED_AT,direction:DESC}) {
              nodes { number title url mergedAt headRefOid baseRefName mergeCommit { oid } }
              pageInfo { hasNextPage endCursor }
            }
          }
        }"""
        cursor, seen, result = None, set(), []
        while True:
            value = self._api(["graphql", "--input", "-"], {
                "query": query, "variables": {"owner": owner, "name": name, "cursor": cursor}})
            if value.get("errors"):
                raise CampaignError("GitHub could not enumerate merged PRs")
            try:
                page = value["data"]["repository"]["pullRequests"]
                for item in page["nodes"]:
                    number, head = item["number"], item["headRefOid"]
                    if type(number) is not int or number <= 0 or not re.fullmatch(r"[0-9a-f]{40}", head):
                        raise CampaignError("GitHub returned an invalid merged PR identity")
                    result.append({"number": number, "title": item["title"], "url": item["url"],
                                   "merged_at": item["mergedAt"], "head_sha": head,
                                   "base_ref": item["baseRefName"],
                                   "merge_commit_sha": (item["mergeCommit"] or {}).get("oid")})
                info = page["pageInfo"]
                if not info["hasNextPage"]: return result
                cursor = info["endCursor"]
                if not cursor or cursor in seen:
                    raise CampaignError("GitHub merged PR pagination did not advance")
                seen.add(cursor)
            except (KeyError, TypeError) as exc:
                raise CampaignError("GitHub returned malformed merged PR metadata") from exc

    @staticmethod
    def _number(value):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise CampaignError("GitHub number must be a positive integer")
        return value

    def list_checks(self, repo: str, head: str) -> list[dict]:
        return self._list_checks_response(repo, head)

    def _list_checks_response(self, repo, head):
        value = self._api([f"repos/{_repository(repo)}/commits/{head}/check-runs?per_page=100", "--paginate", "--slurp"])
        pages = _pages(value)
        checks = []
        for page in pages:
            if isinstance(page, dict):
                checks.extend(page.get("check_runs", []))
        return checks

    @staticmethod
    def _matching_check(checks, head, name, external_id):
        matches = [item for item in checks if item.get("name") == name and item.get("external_id") == external_id
                   and (item.get("head_sha") == head or item.get("check_suite", {}).get("head_sha") == head)]
        if len(matches) > 1:
            raise CampaignError("multiple GitHub checks have the same identity")
        return matches[0] if matches else None

    def create_check(self, repo: str, head: str, name: str, external_id: str) -> dict:
        repo = _repository(repo)
        if not all(isinstance(value, str) and value for value in (head, name, external_id)):
            raise CampaignError("check head, name, and external_id are required")
        existing = self._matching_check(self.list_checks(repo, head), head, name, external_id)
        if existing:
            return existing
        payload = {"name": name, "head_sha": head, "external_id": external_id, "status": "queued"}
        try:
            return self._api([f"repos/{repo}/check-runs", "--method", "POST", "--input", "-"], payload, unknown_write=True)
        except UnknownDelivery:
            existing = self._matching_check(self.list_checks(repo, head), head, name, external_id)
            if existing:
                return existing
            raise

    def update_check(self, repo: str, check_id: int, *, status=None, conclusion=None,
                     summary=None, details_url=None) -> dict:
        repo = _repository(repo)
        check_id = self._number(check_id)
        payload = {}
        if status is not None:
            if status not in {"queued", "in_progress", "completed"}:
                raise CampaignError("invalid check status")
            payload["status"] = status
        if conclusion is not None:
            if conclusion not in {"action_required", "cancelled", "failure", "neutral", "success", "skipped", "stale", "timed_out"}:
                raise CampaignError("invalid check conclusion")
            payload["conclusion"] = conclusion
        if summary is not None:
            if not isinstance(summary, str) or not summary:
                raise CampaignError("check summary must be nonempty")
            payload["output"] = {"title": "AI PR review", "summary": summary[:65_535]}
        if details_url is not None:
            if not isinstance(details_url, str) or not details_url.startswith("https://"):
                raise CampaignError("check details_url must be HTTPS")
            payload["details_url"] = details_url
        if not payload:
            raise CampaignError("check update is empty")
        endpoint = f"repos/{repo}/check-runs/{check_id}"
        try:
            return self._api([endpoint, "--method", "PATCH", "--input", "-"], payload, unknown_write=True)
        except UnknownDelivery:
            current = self._api([endpoint])
            if current.get("status") == status and (conclusion is None or current.get("conclusion") == conclusion):
                return current
            raise

    def list_issue_comments(self, repo: str, number: int) -> list[dict]:
        return self._list(f"repos/{_repository(repo)}/issues/{self._number(number)}/comments?per_page=100")

    def create_comment(self, repo: str, number: int, body: str) -> dict:
        return self._api([f"repos/{_repository(repo)}/issues/{self._number(number)}/comments", "--method", "POST", "--input", "-"], {"body": body}, unknown_write=True)

    def update_comment(self, repo: str, comment_id: int, body: str) -> dict:
        return self._api([f"repos/{_repository(repo)}/issues/comments/{self._number(comment_id)}", "--method", "PATCH", "--input", "-"], {"body": body}, unknown_write=True)

    def list_reviews(self, repo: str, number: int) -> list[dict]:
        return self._list(f"repos/{_repository(repo)}/pulls/{self._number(number)}/reviews?per_page=100")

    def list_pr_commits(self, repo: str, number: int) -> list[dict]:
        return self._list(f"repos/{_repository(repo)}/pulls/{self._number(number)}/commits?per_page=100")

    def list_review_comments(self, repo: str, number: int) -> list[dict]:
        return self._list(f"repos/{_repository(repo)}/pulls/{self._number(number)}/comments?per_page=100")

    def create_review(self, repo: str, number: int, head: str, body: str,
                      comments: list[dict], *, event="COMMENT") -> dict:
        if event not in {"COMMENT", "REQUEST_CHANGES"}:
            raise CampaignError("invalid review event")
        if not isinstance(comments, list):
            raise CampaignError("review comments must be an array")
        payload = {"commit_id": head, "body": body, "event": event, "comments": comments}
        return self._api([f"repos/{_repository(repo)}/pulls/{self._number(number)}/reviews", "--method", "POST", "--input", "-"], payload, unknown_write=True)


def _text(value, name, limit=8_000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise CampaignError(f"finding {name} must be bounded nonempty text")
    return value.strip()


def _safe_path(value):
    if not isinstance(value, str) or not value or "\0" in value or "\n" in value:
        raise CampaignError("finding evidence path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise CampaignError("finding evidence path is unsafe")
    return value


def _attribution(result):
    lane = result.get("lane") or {}
    provenance = result.get("provenance") or {}
    model = result.get("model") or lane.get("model") or provenance.get("model")
    effort = result.get("effort") or lane.get("effort") or provenance.get("effort") or "unspecified"
    tier = result.get("tier") or lane.get("tier") or provenance.get("tier") or "unspecified"
    return {"model": _text(model, "model", 256), "effort": _text(effort, "effort", 64), "tier": _text(tier, "tier", 64)}


def _lines_for(changed_line_map, path, side):
    entry = changed_line_map.get(path, {}) if isinstance(changed_line_map, dict) else {}
    if isinstance(entry, dict):
        values = entry.get(side, entry.get(side.lower(), []))
        if isinstance(values, dict):
            values = values.keys()
        if isinstance(values, (list, tuple, set, range)):
            return {int(value) for value in values if isinstance(value, int) and not isinstance(value, bool) and value > 0}
        # Also accept the compact {line: "RIGHT"} representation.
        return {int(line) for line, value in entry.items() if str(value).upper() == side and str(line).isdigit()}
    return set()


class PRReviewPublisher:
    """Publish one unified body-plus-inline GitHub review per model and PR head."""

    def __init__(self, client: GitHubPRClient, policy: dict | None, state_path):
        self.client = client
        self.policy = dict(policy or {})
        self.state_path = Path(state_path)
        cap = self.policy.get("max_inline_comments", 50)
        if isinstance(cap, bool) or not isinstance(cap, int) or not 0 <= cap <= 50:
            raise CampaignError("max_inline_comments must be an integer from 0 through 50")
        self.cap = cap

    def _validate(self, repo, pr, head, result, finding):
        if not isinstance(finding, dict):
            raise CampaignError("finding must be an object")
        severity = str(finding.get("severity", "")).lower()
        confidence = str(finding.get("confidence", "")).lower()
        if severity not in _SEVERITIES or confidence not in _CONFIDENCES:
            raise CampaignError("finding severity or confidence is invalid")
        fields = {}
        for key in ("root_cause", "expected", "observed", "impact", "remediation_direction"):
            fields[key] = _text(finding.get(key), key)
        if finding.get("checked_head_sha") != head:
            raise CampaignError("finding was not checked against the exact PR head")
        if finding.get("persists_at_head") != "present":
            raise CampaignError("only findings present at the exact PR head may be published")
        evidence = finding.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise CampaignError("finding requires evidence")
        normalized = []
        for item in evidence:
            if not isinstance(item, dict):
                raise CampaignError("finding evidence must be an object")
            path = _safe_path(item.get("path"))
            start, end = item.get("line_start"), item.get("line_end", item.get("line_start"))
            if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in (start, end)) or end < start:
                raise CampaignError("finding evidence lines are invalid")
            commit = item.get("commit")
            if commit not in {head, pr.get("base_sha")}:
                raise CampaignError("finding cites evidence outside the reviewed commits")
            normalized.append({**item, "path": path, "line_start": start, "line_end": end, "commit": commit})
        if not any(item["commit"] == head for item in normalized):
            raise CampaignError("finding lacks exact-head evidence")
        attribution = _attribution(result)
        combined = "\n".join([*fields.values(), attribution["model"], attribution["effort"], attribution["tier"]])
        if _SECRET_TEXT.search(combined) or any(_secret_reason(item["path"], combined.encode()) for item in normalized):
            raise CampaignError("finding text or path matches credential material")
        primary = next((item for item in normalized if item["commit"] == head), normalized[0])
        root_key = " ".join(fields["root_cause"].lower().split())
        fingerprint = digest({"repository": repo.lower(), "path": primary["path"],
                              "symbol": primary.get("symbol"), "root_cause": root_key})
        return {**finding, **fields, "severity": severity, "confidence": confidence,
                "evidence": normalized, "attributions": [attribution], "fingerprint": fingerprint}

    @staticmethod
    def _permalink(repo, evidence):
        path = quote(evidence["path"], safe="/")
        start, end = evidence["line_start"], evidence["line_end"]
        fragment = f"#L{start}" if start == end else f"#L{start}-L{end}"
        return f"https://github.com/{repo}/blob/{evidence['commit']}/{path}{fragment}"

    def _location(self, finding, pr, head, changed):
        # A head location is normally clearer; use deleted/base evidence only
        # when no current-head changed location is available.
        ordered = sorted(finding["evidence"], key=lambda item: item["commit"] != head)
        for evidence in ordered:
            side = "RIGHT" if evidence["commit"] == head else "LEFT"
            available = _lines_for(changed, evidence["path"], side)
            if not available:
                continue
            start, end = evidence["line_start"], evidence["line_end"]
            candidates = [line for line in range(start, end + 1) if line in available]
            if not candidates:
                continue
            line = candidates[-1]
            location = {"path": evidence["path"], "side": side, "line": line}
            valid_start = next((candidate for candidate in candidates if candidate <= line), line)
            if valid_start < line and all(candidate in available for candidate in range(valid_start, line + 1)):
                location.update(start_line=valid_start, start_side=side)
            return location
        return None

    @staticmethod
    def _inline_body(finding):
        models = ", ".join(f"`{item['model']}` ({item['effort']}, {item['tier']})" for item in finding["attributions"])
        return "\n".join([
            f"<!-- ai-pr-review:finding:{finding['fingerprint']} -->",
            f"### [{finding['severity'].upper()}] {finding['root_cause']}",
            f"**{finding['severity'].upper()} / {finding['confidence']} confidence**",
            "", f"**Expected:** {finding['expected']}", f"**Observed:** {finding['observed']}",
            f"**Impact:** {finding['impact']}", f"**Remediation:** {finding['remediation_direction']}",
            f"**Models:** {models}",
        ])

    def _summary(self, repo, pr, head, findings, inline_ids, held_ids, results):
        models = sorted({_attribution(result)["model"] for result in results})
        lines = ["# AI pull request review", "", f"- Head: `{head}`",
                 f"- Models: {', '.join(f'`{model}`' for model in models)}",
                 f"- Findings: **{len(findings)}**", f"- Inline: **{len(inline_ids)}**",
                 f"- Summary-only: **{len(held_ids)}**", "",
                  "Findings are deduplicated within this model's review. Model reasoning is not published.", ""]
        withheld = sum(result.get("report", {}).get("withheld_count", 0) for result in results)
        if withheld:
            lines.extend([f"- Candidate findings withheld by evidence validation: **{withheld}**", ""])
        if not findings and withheld:
            lines.extend(["No candidate findings could be published after evidence validation; this is not a clean-review verdict.", ""])
        elif not findings:
            lines.extend(["No actionable findings identified within the reviewed scope.", ""])
        if pr.get("merged"):
            lines.extend(["**Retrospective review of a merged PR.** Findings refer to the exact head above; "
                          "subsequent fixes and today's branch tip were not reviewed.", ""])
        for result in results:
            if result.get("report", {}).get("summary"):
                assessment = _text(result["report"]["summary"], "summary")
                if _SECRET_TEXT.search(assessment) or _secret_reason("summary.txt", assessment.encode()):
                    raise CampaignError("summary text matches credential material")
                lines.extend(["## Overall assessment", "", assessment, ""])
        limitations = []
        for result in results:
            for note in result.get("report", {}).get("limitations", []):
                text = _text(note, "limitation")
                if _SECRET_TEXT.search(text) or _secret_reason("limitations.txt", text.encode()):
                    raise CampaignError("limitation text matches credential material")
                if text not in limitations: limitations.append(text)
        if limitations:
            lines.extend(["## Review limitations", "", *[f"- {note}" for note in limitations], ""])
        if findings:
            lines.extend(["## Finding index", ""])
            for finding in sorted(findings, key=lambda item: item["fingerprint"]):
                evidence = finding["evidence"][0]
                lines.append(f"- `{finding['fingerprint'][:12]}` **{finding['severity']}**: "
                             f"{finding['root_cause'][:180]} "
                             f"([{evidence['path']}:{evidence['line_start']}]({self._permalink(repo, evidence)}))")
            lines.append("")
        for severity in ("critical", "high", "medium", "low", "info"):
            group = [item for item in findings if item["severity"] == severity]
            if not group:
                continue
            lines.extend([f"## {severity.title()} severity", ""])
            for finding in sorted(group, key=lambda item: (-{"high": 3, "medium": 2, "low": 1}[item["confidence"]], item["evidence"][0]["path"], item["fingerprint"])):
                evidence = finding["evidence"][0]
                models_text = ", ".join(f"`{a['model']}` (effort {a['effort']}, tier {a['tier']})" for a in finding["attributions"])
                disposition = "inline" if finding["fingerprint"] in inline_ids else "summary only"
                lines.extend([f"### [{finding['severity'].upper()}] {finding['root_cause']}", "",
                              f"- Confidence: **{finding['confidence']}**", f"- Delivery: {disposition}",
                              f"- Models: {models_text}", f"- Evidence: [{evidence['path']}:{evidence['line_start']}]({self._permalink(repo, evidence)})",
                              "", f"**Expected:** {finding['expected']}", "", f"**Observed:** {finding['observed']}",
                              "", f"**Impact:** {finding['impact']}", "", f"**Remediation:** {finding['remediation_direction']}", ""])
        rendered = "\n".join(lines).rstrip() + "\n"
        if len(rendered) > 60_000:
            rendered = rendered[:59_000].rstrip() + "\n\n_Additional detail is retained in the durable review results._\n"
        return rendered

    @staticmethod
    def _url(item):
        return item.get("html_url") or item.get("url")

    def publish(self, repo: str, pr: dict, head: str, results: list[dict], changed_line_map: dict) -> dict:
        repo = _repository(repo)
        if not isinstance(pr, dict) or isinstance(pr.get("number"), bool) or not isinstance(pr.get("number"), int):
            raise CampaignError("normalized PR metadata is required")
        if not isinstance(head, str) or not _SHA.fullmatch(head) or pr.get("head_sha") not in {None, head}:
            raise CampaignError("publication head does not match the PR")
        if not isinstance(results, list) or not isinstance(changed_line_map, dict):
            raise CampaignError("results and changed_line_map must be collections")

        # Render and validate every lane before the first external write.
        plans = []
        models = set()
        for result in results:
            findings = result.get("findings", result.get("report", {}).get("findings")) if isinstance(result, dict) else None
            if not isinstance(findings, list):
                raise CampaignError("result findings must be an array")
            model = _attribution(result)["model"]
            if model in models:
                raise CampaignError("each model must have exactly one review result")
            models.add(model)
            unique = {}
            for raw in findings:
                finding = self._validate(repo, pr, head, result, raw)
                unique.setdefault(finding["fingerprint"], finding)
            findings = list(unique.values())
            severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
            confidence_rank = {"high": 0, "medium": 1, "low": 2}
            candidates, held = [], []
            for finding in findings:
                location = self._location(finding, pr, head, changed_line_map)
                (candidates if location else held).append((finding, location))
            candidates.sort(key=lambda pair: (severity_rank[pair[0]["severity"]], confidence_rank[pair[0]["confidence"]], pair[1]["path"], pair[1]["line"]))
            selected, overflow = candidates[:self.cap], candidates[self.cap:]
            held.extend(overflow)
            inline_ids = {item["fingerprint"] for item, _ in selected}
            held_ids = {item["fingerprint"] for item, _ in held}
            marker = f"<!-- ai-pr-review:model-review:{repo}:{pr['number']}:{head}:{digest(model)} -->"
            body = marker + "\n" + self._summary(repo, pr, head, findings, inline_ids, held_ids, [result])
            plans.append({"model": model, "marker": marker, "body": body,
                          "comments": [{**location, "body": self._inline_body(item)} for item, location in selected],
                          "held_count": len(held), "findings": findings})

        receipts = []
        for plan in plans:
            matches = [item for item in self.client.list_reviews(repo, pr["number"])
                       if plan["marker"] in (item.get("body") or "")]
            if len(matches) > 1: raise CampaignError("multiple marked reviews exist for this model and head")
            review = matches[0] if matches else None
            if review is None:
                try:
                    review = self.client.create_review(repo, pr["number"], head, plan["body"], plan["comments"], event="COMMENT")
                except UnknownDelivery:
                    matches = [item for item in self.client.list_reviews(repo, pr["number"])
                               if plan["marker"] in (item.get("body") or "")]
                    if len(matches) != 1: raise
                    review = matches[0]
            receipts.append({"model": plan["model"], "review_id": review.get("id"), "review_url": self._url(review),
                             "inline_count": len(plan["comments"]), "held_count": plan["held_count"],
                             "finding_fingerprints": sorted(item["fingerprint"] for item in plan["findings"])})
        findings = [item for plan in plans for item in plan["findings"]]
        blocking = set(self.policy.get("blocking_severities", []))
        withheld = sum(result.get("report", {}).get("withheld_count", 0) for result in results)
        conclusion = "failure" if any(item["severity"] in blocking for item in findings) else "neutral" if findings or withheld else "success"
        receipt = seal({
            "version": "github-model-reviews-v2", "repository": repo, "pr": pr["number"], "head": head,
            "reviews": receipts, "inline_count": sum(item["inline_count"] for item in receipts),
            "held_count": sum(item["held_count"] for item in receipts), "conclusion_suggestion": conclusion,
        })
        atomic_json(self.state_path, receipt)
        return receipt

    # A descriptive alias is convenient for callers which treat publishers as callables.
    submit = publish


__all__ = ["GitHubPRClient", "PRReviewPublisher"]
