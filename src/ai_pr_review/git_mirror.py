"""Safe non-checkout Git mirrors and immutable PR scope preparation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess

from .common import CampaignError, digest
from .complexity import classify
from .inventory import diff_units
from .snapshot import prepare_scope


_SHA = re.compile(r"[0-9a-f]{40,64}")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_HUNK = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _bounded_metadata(metadata: dict) -> dict:
    limits = {"title": 500, "body": 20000, "author": 200, "url": 2000}
    result = {}
    for key, limit in limits.items():
        value = metadata.get(key, "")
        if value is None:
            value = ""
        if not isinstance(value, str) or "\0" in value:
            raise CampaignError(f"PR metadata {key} must be inert text")
        result[key] = value[:limit]
    return result


def _patch_path(raw: bytes, prefix: bytes):
    if raw == b"/dev/null":
        return None
    if raw.startswith(b'"'):
        try:
            raw = json.loads(raw.decode("utf-8")).encode("utf-8", "surrogateescape")
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise CampaignError("malformed quoted diff path") from exc
    if not raw.startswith(prefix):
        raise CampaignError("malformed diff path prefix")
    value = raw[len(prefix):].decode("utf-8", "surrogateescape")
    if "\n" in value or "\0" in value:
        raise CampaignError("newline and NUL path names are unsupported")
    return value


def parse_changed_line_map(patch: bytes) -> dict:
    """Parse a zero-context Git patch into GitHub LEFT/RIGHT changed lines."""
    result, old_path, new_path = {}, None, None
    for line in patch.splitlines():
        if line.startswith(b"--- "):
            old_path = _patch_path(line[4:], b"a/")
        elif line.startswith(b"+++ "):
            new_path = _patch_path(line[4:], b"b/")
        else:
            match = _HUNK.match(line)
            if not match:
                continue
            old_start, old_count, new_start, new_count = (int(value or b"1") for value in match.groups())
            if old_count:
                if old_path is None:
                    raise CampaignError("deleted lines have no old path")
                result.setdefault(old_path, {"LEFT": [], "RIGHT": []})["LEFT"].extend(
                    range(old_start, old_start + old_count))
            if new_count:
                if new_path is None:
                    raise CampaignError("added lines have no new path")
                result.setdefault(new_path, {"LEFT": [], "RIGHT": []})["RIGHT"].extend(
                    range(new_start, new_start + new_count))
    return result


def build_pr_scope(repo: Path, merge_base: str, head: str, *, repository: str, number: int,
                   metadata: dict, destination: Path, config) -> dict:
    """Build and snapshot a generic PR scope from immutable Git objects."""
    paths, units, patch = diff_units(Path(repo), merge_base, head)
    scope = {"id": f"pr-{number}-{head}", "stage": "pr", "ordinal": 0, "pr": number,
             "base_sha": merge_base, "head_sha": head, "paths": paths, "units": units,
             "diff_digest": "sha256:" + hashlib.sha256(patch).hexdigest(),
             "diff_bytes": len(patch), "introduced_commits": [], "lineage": [], "gaps": [],
             "repository": repository, "pr_metadata": _bounded_metadata(metadata)}
    tier = classify(scope, config)["tier"]
    order = ("light", "standard", "deep")
    while len(patch) > config.tiers[tier]["diff_bytes"] and tier != "deep":
        tier = order[order.index(tier) + 1]
    scope["tier_override"] = tier
    limits = config.tiers[tier]
    context_limit = {"light": 0, "standard": 24, "deep": 80}[tier]
    context = _nearby_context(repo, head, paths, context_limit)
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    index = prepare_scope(Path(repo), scope, Path(destination), campaign_head=head,
                          context_paths=context,
                          full_context=tier in {"standard", "deep"},
                          max_diff_bytes=limits["diff_bytes"], max_units_per_shard=limits["units"])
    zero_patch = _git(repo, "diff", "--no-ext-diff", "--no-textconv", "--unified=0",
                      "--find-renames", merge_base, head)
    return {"repository_path": str(Path(repo).resolve()), "merge_base": merge_base, "head_sha": head,
            "scope": scope, "snapshot_root": str(Path(destination).resolve()), "snapshot_index": index,
             "line_map": parse_changed_line_map(zero_patch)}


def _nearby_context(repo: Path, head: str, changed: list[str], limit: int) -> list[str]:
    if not limit:
        return []
    parents = {str(Path(path).parent) for path in changed}
    raw = _git(repo, "ls-tree", "-r", "--name-only", "-z", head)
    candidates = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        path = item.decode("utf-8", "surrogateescape")
        if path not in changed and str(Path(path).parent) in parents and Path(path).suffix in {".rs", ".toml", ".md", ".ts", ".py"}:
            candidates.append(path)
    return sorted(candidates)[:limit]


def _git(repo: Path, *args: str, timeout=120) -> bytes:
    try:
        result = subprocess.run(["git", "--literal-pathspecs", "-c", "core.quotePath=false",
                                 "-c", "core.hooksPath=/dev/null",
                                 "-c", "protocol.file.allow=never", "-C", str(repo), *args],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CampaignError("Git command failed or timed out") from exc
    if result.returncode:
        raise CampaignError(f"Git command failed ({args[0]})")
    return result.stdout


class GitMirror:
    """Manage one validated, non-checkout clone per configured repository."""

    def __init__(self, workspace: str | Path, *, timeout: int = 120):
        self.workspace = Path(workspace).resolve()
        self.timeout = timeout

    def _path(self, repository):
        if not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository):
            raise CampaignError("invalid mirror repository")
        return self.workspace / (repository.replace("/", "--") + ".git")

    def _run(self, args):
        try:
            result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CampaignError("mirror command failed or timed out") from exc
        if result.returncode:
            raise CampaignError("mirror command failed")
        return result.stdout

    def _ensure(self, repository):
        self.workspace.mkdir(parents=True, exist_ok=True)
        path = self._path(repository)
        if not path.exists():
            self._run(["gh", "repo", "clone", repository, str(path), "--", "--no-checkout"])
        if not (path / ".git").is_dir():
            raise CampaignError("mirror is not a non-bare Git clone")
        origin = _git(path, "remote", "get-url", "origin", timeout=self.timeout).decode().strip()
        allowed = {f"https://github.com/{repository}.git", f"https://github.com/{repository}",
                   f"git@github.com:{repository}.git", f"ssh://git@github.com/{repository}.git"}
        if origin not in allowed:
            raise CampaignError("mirror origin does not match configured repository")
        if _git(path, "rev-parse", "--is-shallow-repository", timeout=self.timeout).decode().strip() != "false":
            raise CampaignError("shallow mirrors are unsupported")
        return path

    def prepare_pr(self, repository: str, number: int, base_branch: str, expected_head: str,
                   metadata: dict, destination: str | Path, config) -> dict:
        """Fetch exact base/PR refs, verify identity, and prepare an immutable scope."""
        if (not isinstance(expected_head, str) or not _SHA.fullmatch(expected_head)
                or type(number) is not int or number <= 0
                or not isinstance(base_branch, str) or not base_branch
                or "\0" in base_branch or "\n" in base_branch):
            raise CampaignError("invalid PR identity")
        path = self._ensure(repository)
        base_ref = "refs/ai-pr-review/base"
        head_ref = f"refs/ai-pr-review/pr/{number}"
        _git(path, "fetch", "--no-tags", "origin",
             f"+refs/heads/{base_branch}:{base_ref}", f"+refs/pull/{number}/head:{head_ref}",
             timeout=self.timeout)
        resolved = _git(path, "rev-parse", "--verify", f"{head_ref}^{{commit}}",
                        timeout=self.timeout).decode().strip()
        if resolved != expected_head:
            raise CampaignError("fetched PR head does not match expected SHA")
        base = _git(path, "rev-parse", "--verify", f"{base_ref}^{{commit}}",
                    timeout=self.timeout).decode().strip()
        merge_base = _git(path, "merge-base", base, resolved, timeout=self.timeout).decode().strip()
        if not _SHA.fullmatch(base) or not _SHA.fullmatch(merge_base):
            raise CampaignError("base or merge-base commit is missing")
        return build_pr_scope(path, merge_base, resolved, repository=repository, number=number,
                              metadata=metadata, destination=Path(destination), config=config)
