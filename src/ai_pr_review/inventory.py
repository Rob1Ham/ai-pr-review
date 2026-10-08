"""Freeze a Git range into deterministic landing and diff scopes."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess

from .common import CampaignError, SCHEMA_VERSION, digest, seal, verify_seal


_PR_RE = re.compile(r"(?:pull request\s+#|\(#)(\d+)\)?", re.I)
_HUNK_RE = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.M)


def _git(repo: Path, *args: str, input_data: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(["git", "--literal-pathspecs", "-c", "core.quotePath=false", "-C",
                                 str(repo), *args], input=input_data, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CampaignError(f"git command failed or timed out ({args[0]})") from exc
    if result.returncode:
        raise CampaignError(f"git command failed ({args[0]}) with exit {result.returncode}")
    return result.stdout


def _sha(repo: Path, ref: str) -> str:
    if not isinstance(ref, str) or not ref or ref.startswith("-") or "\0" in ref or "\n" in ref:
        raise CampaignError("invalid Git ref")
    value = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", value):
        raise CampaignError(f"ref did not resolve to a commit: {ref}")
    return value


def _diff(repo: Path, before: str, after: str, paths: list[str] | None = None) -> bytes:
    args = ["diff", "--no-ext-diff", "--no-textconv", "--full-index", "--find-renames",
            "--find-copies", before, after]
    if paths is not None:
        args.extend(["--", *paths])
    return _git(repo, *args)


def _changed_paths(repo: Path, before: str, after: str):
    fields = _git(repo, "diff", "--no-ext-diff", "--no-textconv", "--find-renames",
                  "--name-status", "-z", before, after).split(b"\0")
    paths, kinds, index = set(), {}, 0
    while index < len(fields) and fields[index]:
        status = fields[index].decode("ascii", "replace"); index += 1
        count = 2 if status[:1] in {"R", "C"} else 1
        if index + count > len(fields):
            raise CampaignError("malformed Git name-status output")
        for raw in fields[index:index + count]:
            name = raw.decode("utf-8", "surrogateescape")
            if "\n" in name or "\0" in name:
                raise CampaignError("newline and NUL path names are unsupported")
            paths.add(name); kinds[name] = status[:1]
        index += count
    return sorted(paths), kinds


def _patch_path(value: bytes, prefix: bytes):
    if value.startswith(b'"'):
        try: value = json.loads(value.decode()).encode("utf-8", "surrogateescape")
        except (UnicodeError, json.JSONDecodeError): return None
    if not value.startswith(prefix): return None
    path = value[len(prefix):].decode("utf-8", "surrogateescape")
    if "\n" in path or "\0" in path: raise CampaignError("newline and NUL path names are unsupported")
    return path


def diff_units(repo: Path, before: str, after: str):
    paths, kinds = _changed_paths(repo, before, after)
    patch = _diff(repo, before, after)
    units = []
    for path in paths:
        unit = {"kind": "file", "path": path, "change": kinds[path], "before_start": 0,
                "before_count": 0, "after_start": 0, "after_count": 0}
        unit["id"] = "sha256:" + digest({"base": before, "head": after, **unit}); units.append(unit)
    starts = [m.start() for m in re.finditer(rb"(?m)^diff --git ", patch)] + [len(patch)]
    for index in range(max(0, len(starts) - 1)):
        old = current = None
        for line in patch[starts[index]:starts[index + 1]].splitlines(keepends=True):
            if line.startswith(b"--- "): old = _patch_path(line[4:].rstrip(b"\r\n"), b"a/")
            elif line.startswith(b"+++ "):
                value = line[4:].rstrip(b"\r\n"); current = old if value == b"/dev/null" else _patch_path(value, b"b/")
            match = _HUNK_RE.match(line)
            if match and current is not None:
                a, ac, b, bc = match.groups()
                unit = {"kind": "hunk", "path": current, "before_start": int(a),
                        "before_count": int(ac or b"1"), "after_start": int(b),
                        "after_count": int(bc or b"1")}
                unit["id"] = "sha256:" + digest({"base": before, "head": after, **unit}); units.append(unit)
    return paths, units, patch


def _gh_json(args):
    try:
        result = subprocess.run(["gh", "api", *args], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CampaignError("GitHub metadata request failed or timed out") from exc
    if result.returncode: raise CampaignError("GitHub metadata request failed")
    try: return json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError) as exc: raise CampaignError("GitHub returned invalid JSON") from exc


def _remote_ref_sha(repository, ref):
    if re.fullmatch(r"[0-9a-f]{40,64}", ref):
        record = _gh_json([f"repos/{repository}/git/commits/{ref}"])
        if record.get("sha") != ref: raise CampaignError(f"remote commit object not found: {ref}")
        return ref
    candidates = [ref.removeprefix("refs/")] if ref.startswith("refs/") else [f"heads/{ref}", f"tags/{ref}"]
    record = None
    for candidate in candidates:
        try: record = _gh_json([f"repos/{repository}/git/ref/{candidate}"]); break
        except CampaignError: pass
    if not isinstance(record, dict): raise CampaignError(f"remote ref not found: {ref}")
    obj = record.get("object") or {}
    for _ in range(16):
        if obj.get("type") == "commit" and isinstance(obj.get("sha"), str): return obj["sha"]
        if obj.get("type") != "tag": raise CampaignError("remote ref is not a commit or tag")
        obj = (_gh_json([f"repos/{repository}/git/tags/{obj.get('sha')}"]) or {}).get("object") or {}
    raise CampaignError("annotated tag nesting exceeds limit")


def _remote_metadata(repository, branch, prs):
    repo = _gh_json([f"repos/{repository}"])
    records = {}
    for number in sorted(set(prs)):
        item = _gh_json([f"repos/{repository}/pulls/{number}"])
        records[str(number)] = {"number": item.get("number"), "state": item.get("state"),
            "merged": item.get("merged"), "base": (item.get("base") or {}).get("ref"),
            "merge_sha": item.get("merge_commit_sha")}
    ref = _gh_json([f"repos/{repository}/git/ref/heads/{branch}"])
    return {"repository": repo.get("full_name"), "default_branch": repo.get("default_branch"),
            "issues": repo.get("has_issues"), "branch_sha": ((ref or {}).get("object") or {}).get("sha"),
            "prs": records}


def _lineage(scope, declarations, supplied):
    declared = tuple((declarations or {}).get(scope.get("pr"), ()))
    if not declared: return []
    raw = (supplied or {}).get(scope["pr"], (supplied or {}).get(str(scope["pr"]), []))
    if not isinstance(raw, list): raise CampaignError("lineage mapping must be a list")
    valid = {unit["id"] for unit in scope["units"]}; mapped = {}
    for entry in raw:
        if not isinstance(entry, dict) or entry.get("source_pr") not in declared:
            raise CampaignError(f"undeclared lineage source for PR #{scope['pr']}")
        source = entry["source_pr"]
        if source in mapped: raise CampaignError(f"duplicate lineage source PR #{source}")
        if entry.get("disposition") not in {"delivered", "rewritten", "excluded"}:
            raise CampaignError(f"invalid lineage disposition for source PR #{source}")
        units = entry.get("mapped_units")
        if not isinstance(units, list) or any(unit not in valid for unit in units):
            raise CampaignError(f"lineage references unknown delivered units for source PR #{source}")
        evidence = entry.get("evidence_refs")
        if not isinstance(evidence, list) or not evidence or not all(isinstance(x, str) and x for x in evidence):
            raise CampaignError(f"lineage evidence is required for source PR #{source}")
        if entry["disposition"] != "excluded" and not units:
            raise CampaignError(f"delivered lineage requires mapped units for source PR #{source}")
        mapped[source] = dict(entry)
    result = []
    for source in declared:
        entry = mapped.get(source, {"source_pr": source, "disposition": None, "mapped_units": [],
                                   "evidence_refs": [], "note": None, "reviewer": None})
        entry["status"] = "mapped" if source in mapped else "declared-review-required"
        unit = {"kind": "lineage", "path": ".", "source_pr": source, "before_start": 0,
                "before_count": 0, "after_start": 0, "after_count": 0}
        unit["id"] = "sha256:" + digest({"landing_pr": scope["pr"], **unit})
        entry["unit_id"] = unit["id"]; scope["units"].append(unit); result.append(entry)
    unit = {"kind": "integration", "path": ".", "landing_pr": scope["pr"], "before_start": 0,
            "before_count": 0, "after_start": 0, "after_count": 0}
    unit["id"] = "sha256:" + digest(unit); scope["units"].append(unit)
    return result


def build_inventory(repo: Path, base_ref: str, head_ref: str, *, repository: str,
                    branch: str, github: bool = False, lineage: dict | None = None,
                    lineage_declarations: dict | None = None, pinned_base: str | None = None,
                    pinned_head: str | None = None) -> dict:
    repo = Path(repo).resolve()
    if not (repo / ".git").exists(): raise CampaignError(f"not a Git repository: {repo}")
    if _git(repo, "rev-parse", "--is-shallow-repository").decode().strip() != "false":
        raise CampaignError("campaign inventory requires a non-shallow repository")
    base, head = _sha(repo, base_ref), _sha(repo, head_ref)
    if pinned_base and base != pinned_base: raise CampaignError("local base ref does not match pinned SHA")
    if pinned_head and head != pinned_head: raise CampaignError("local head ref does not match pinned SHA")
    ancestry = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", base, head])
    if ancestry.returncode: raise CampaignError("baseline is not an ancestor of campaign head")
    commits = _git(repo, "rev-list", "--first-parent", "--reverse", f"{base}..{head}").decode().splitlines()
    scopes, blockers, previous = [], [], base
    for ordinal, commit in enumerate(commits):
        parents = _git(repo, "rev-list", "--parents", "-n", "1", commit).decode().split()[1:]
        subject = _git(repo, "show", "-s", "--format=%s", commit).decode("utf-8", "replace").strip()
        match = _PR_RE.search(subject); pr = int(match.group(1)) if match else None
        if not parents or parents[0] != previous: blockers.append(f"first-parent transition is not contiguous at {commit}")
        if pr is None: blockers.append(f"unmapped first-parent change {commit}")
        paths, units, patch = diff_units(repo, previous, commit)
        introduced = _git(repo, "rev-list", commit, "--not", previous).decode().splitlines()
        scope = {"id": f"pr-{pr}" if pr else f"unmapped-{commit}", "stage": "pr", "ordinal": ordinal,
                 "pr": pr, "base_sha": previous, "head_sha": commit, "paths": paths, "units": units,
                 "diff_digest": "sha256:" + hashlib.sha256(patch).hexdigest(), "diff_bytes": len(patch),
                 "introduced_commits": sorted(introduced), "gaps": []}
        scope["lineage"] = _lineage(scope, lineage_declarations, lineage)
        scopes.append(scope); previous = commit
    expected = set(_git(repo, "rev-list", f"{base}..{head}").decode().splitlines())
    owned = [commit for scope in scopes for commit in scope["introduced_commits"]]
    if set(owned) != expected or len(owned) != len(set(owned)): blockers.append("reachable commit ownership is not exact")
    remote = None
    if github:
        try:
            remote = _remote_metadata(repository, branch, [s["pr"] for s in scopes if s["pr"]])
            if remote["repository"] != repository: blockers.append("GitHub repository identity mismatch")
            if remote["default_branch"] != branch: blockers.append("GitHub default branch mismatch")
            if not remote["issues"]: blockers.append("GitHub Issues are disabled")
            if remote["branch_sha"] != head: blockers.append("GitHub default branch cutoff drift")
            if _remote_ref_sha(repository, head_ref) != head: blockers.append("GitHub head cutoff drift")
            if _remote_ref_sha(repository, base_ref) != base: blockers.append("GitHub base cutoff drift")
            for scope in scopes:
                item = remote["prs"].get(str(scope["pr"]), {})
                if not item.get("merged") or item.get("state") not in {"closed", "MERGED"}: blockers.append(f"PR #{scope['pr']} is not merged")
                if item.get("base") != branch: blockers.append(f"PR #{scope['pr']} base branch drift")
                if item.get("merge_sha") != scope["head_sha"]: blockers.append(f"PR #{scope['pr']} exact merge SHA mismatch")
        except CampaignError as exc: blockers.append(str(exc))
    paths, units, patch = diff_units(repo, base, head)
    scopes.append({"id": "whole-range", "stage": "whole_range", "ordinal": len(scopes), "pr": None,
                   "base_sha": base, "head_sha": head, "paths": paths, "units": units,
                   "diff_digest": "sha256:" + hashlib.sha256(patch).hexdigest(), "diff_bytes": len(patch),
                   "introduced_commits": sorted(expected), "lineage": [], "gaps": []})
    manifest = {"schema_version": SCHEMA_VERSION, "repository": repository, "repository_path": str(repo),
                "branch": branch, "base_ref": base_ref, "head_ref": head_ref, "base_sha": base,
                "head_sha": head, "captured_at": datetime.now(timezone.utc).isoformat(),
                "gate": {"status": "blocked" if blockers else ("pass" if github else "unverified"),
                         "blockers": blockers}, "scopes": scopes,
                "counts": {"commits": len(expected), "prs": sum(s["pr"] is not None for s in scopes),
                           "net_paths": len(paths)}}
    if remote is not None: manifest["github_metadata"] = remote
    return seal(manifest)


def recheck_refs(manifest: dict) -> None:
    verify_seal(manifest)
    scopes = [scope for scope in manifest["scopes"] if scope["stage"] == "pr"]
    metadata = _remote_metadata(manifest["repository"], manifest["branch"], [s["pr"] for s in scopes])
    if metadata["repository"] != manifest["repository"] or metadata["default_branch"] != manifest["branch"]:
        raise CampaignError("GitHub repository identity or default branch drift")
    if metadata["branch_sha"] != manifest["head_sha"]: raise CampaignError("GitHub default branch cutoff drift")
    if _remote_ref_sha(manifest["repository"], manifest["head_ref"]) != manifest["head_sha"]:
        raise CampaignError("GitHub head cutoff drift")
    if _remote_ref_sha(manifest["repository"], manifest["base_ref"]) != manifest["base_sha"]:
        raise CampaignError("GitHub base cutoff drift")
    for scope in scopes:
        item = metadata["prs"].get(str(scope["pr"]), {})
        if not item.get("merged") or item.get("merge_sha") != scope["head_sha"] or item.get("base") != manifest["branch"]:
            raise CampaignError(f"PR #{scope['pr']} exact merge identity drift")
