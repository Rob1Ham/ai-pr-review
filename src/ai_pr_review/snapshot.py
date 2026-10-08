"""Immutable content-addressed evidence snapshots from Git objects."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile

from .common import CampaignError, atomic_json, canonical, digest, seal, verify_seal
from .inventory import _diff, _git

SNAPSHOT_VERSION = "ai-pr-review-snapshot-v2"
_SECRET_PATH = re.compile(r"(^|/)(\.env(?:\..*)?|id_(?:rsa|dsa|ecdsa|ed25519)|credentials(?:\.[^/]*)?|secrets?(?:\.[^/]*)?|[^/]+\.(?:pem|p12|pfx|key|keystore))$", re.I)
_SECRET_CONTENT = (re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
                    re.compile(rb"(?im)^\s*(?:aws_secret_access_key|client_secret|private_key)\s*[:=]"),
                    re.compile(rb"AKIA[0-9A-Z]{16}"),
                    re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
                    re.compile(rb"sk-[A-Za-z0-9_-]{24,}"),
                    re.compile(rb"eyJ[A-Za-z0-9_-]{16,}\.eyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"),
                    re.compile(rb"(?im)^\s*(?:password|passwd|token|api_key|secret_key|bearer)\s*[:=]\s*['\"]?[^\s'\"]{16,}"))


def _safe_path(path):
    if not isinstance(path, str) or not path or "\0" in path or "\n" in path or path.startswith("-"):
        raise CampaignError("invalid evidence path")
    value = PurePosixPath(path)
    if value.is_absolute() or ".." in value.parts: raise CampaignError(f"unsafe evidence path: {path!r}")
    return path


def _secret_reason(path, content):
    example = PurePosixPath(path).name in {".env.example", ".env.sample", ".env.template"}
    source = PurePosixPath(path).suffix in {".c", ".cc", ".cpp", ".go", ".js", ".jsx", ".py", ".rs", ".ts", ".tsx"}
    if not example and not source and _SECRET_PATH.search(path): return "path matches an excluded credential artifact"
    for index, pattern in enumerate(_SECRET_CONTENT):
        for match in pattern.finditer(content):
            # A Rust field initialized from an existing byte variable contains
            # executable syntax, not a literal credential. Keep this exception
            # narrow and keep scanning the entire file for other signatures.
            if index == len(_SECRET_CONTENT) - 1 and source:
                line = content[match.start():].lstrip().split(b"\n", 1)[0].strip()
                if re.fullmatch(rb"(?:password|passwd|token|api_key|secret_key|bearer)\s*[:=]\s*"
                                rb"[A-Za-z_$][A-Za-z0-9_$.:]*(?:\([^\"']*\))?,?", line):
                    continue
            return "content matches a credential signature"
    return None


def _sha(data): return "sha256:" + hashlib.sha256(data).hexdigest()


def _blob(repo, commit, path):
    raw = _git(repo, "ls-tree", "-z", commit, "--", _safe_path(path))
    for record in raw.split(b"\0"):
        if not record: continue
        metadata, name = record.split(b"\t", 1)
        if name.decode("utf-8", "surrogateescape") == path:
            mode, kind, object_id = metadata.decode().split()
            return mode, _git(repo, "cat-file", "blob", object_id) if kind == "blob" else b""
    return None


def _write(root, relative, data):
    target = root / relative; target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as stream: stream.write(data); stream.flush(); os.fsync(stream.fileno())
    target.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)


def prepare_scope(repo: Path, scope: dict, destination: Path, *, max_diff_bytes=400000,
                  max_blob_bytes=2000000, context_paths=None, full_context=False,
                  campaign_head=None, max_units_per_shard=80):
    repo, destination = Path(repo).resolve(), Path(destination)
    if destination.is_symlink(): raise CampaignError("snapshot destination cannot be a symlink")
    options = {"snapshot_version": SNAPSHOT_VERSION, "max_diff_bytes": max_diff_bytes,
               "max_blob_bytes": max_blob_bytes, "max_units_per_shard": max_units_per_shard,
               "context_paths": sorted(context_paths or []), "full_context": bool(full_context),
               "campaign_head": campaign_head}
    if destination.exists():
        index = json.loads((destination / "index.json").read_text()); verify_snapshot(destination, index)
        if index.get("options") != options: raise CampaignError("existing snapshot was prepared with different options")
        return index
    commits = list(dict.fromkeys([scope["base_sha"], scope["head_sha"], *([campaign_head] if campaign_head else [])]))
    paths = set(scope.get("paths", [])) | set(context_paths or [])
    if full_context:
        raw = _git(repo, "ls-tree", "-r", "--name-only", "-z", scope["head_sha"])
        paths |= {item.decode("utf-8", "surrogateescape") for item in raw.split(b"\0") if item}
    objects, data, gaps, exclusions = [], {}, [], []
    for commit in commits:
        for path in sorted(paths):
            found = _blob(repo, commit, path)
            if found is None: continue
            mode, content = found; reason = None
            if mode == "160000": reason = "gitlink recorded in diff metadata; submodule source is not executed or embedded"
            elif mode not in {"100644", "100755"}: reason = "tracked symlink or unsupported mode excluded"
            elif len(content) > max_blob_bytes: reason = f"blob exceeds {max_blob_bytes} bytes"
            else: reason = _secret_reason(path, content)
            if reason is None and (b"\0" in content or _is_binary(content)): reason = "binary content excluded"
            if reason:
                exclusions.append({"commit": commit, "path": path, "reason": reason})
                if path in scope.get("paths", []) and mode != "160000": gaps.append(f"{commit}:{path}: {reason}")
                continue
            content_digest = _sha(content); relative = f"objects/{content_digest[7:]}.blob"
            data.setdefault(relative, content)
            objects.append({"commit": commit, "path": path, "content_digest": content_digest,
                            "storage_path": relative, "lines": len(content.splitlines())})
    if gaps:
        detail = "; ".join(gaps[:8])
        raise CampaignError(f"changed source contains excluded secret, binary, symlink, or oversized evidence: {detail}")
    patch = _diff(repo, scope["base_sha"], scope["head_sha"])
    units = [unit["id"] for unit in scope.get("units", [])]
    if len(patch) > max_diff_bytes and len(units) <= 1: raise CampaignError("atomic diff unit exceeds max_diff_bytes")
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.staging-", dir=destination.parent))
    try:
        for relative, content in data.items(): _write(staging, relative, content)
        shards = []
        if units:
            chunk = patch
            if len(chunk) > max_diff_bytes: raise CampaignError("PR diff exceeds the configured tier limit")
            dd = _sha(chunk); sid = "sha256:" + digest({"unit_ids": units, "diff_digest": dd})
            relative = f"shards/{sid[7:]}.diff"; _write(staging, relative, chunk)
            shards.append({"id": sid, "unit_ids": units, "diff_path": relative,
                            "diff_digest": dd, "diff_bytes": len(chunk)})
        path_index = seal({"objects": [{key: item[key] for key in ("commit", "path", "storage_path")} for item in objects]})
        # One object per line lets bounded read/grep tools locate a path and its
        # blob without truncating a full-repository index into an unusable line.
        path_text = '{"digest":' + canonical(path_index["digest"]) + ',"objects":[\n'
        path_text += ",\n".join(canonical(item) for item in path_index["objects"]) + "\n]}\n"
        _write(staging, "path-index.json", path_text.encode())
        index = seal({"scope_id": scope["id"], "base_sha": scope["base_sha"], "head_sha": scope["head_sha"],
                      "objects": objects, "gaps": gaps, "shards": shards, "unit_ids": units,
                      "path_index_digest": path_index["digest"], "options": options,
                      "context_complete": bool(full_context), "context_mode": "full" if full_context else ("targeted" if context_paths else "changed"),
                      "context_notes": [] if full_context else ["Context is limited to selected immutable paths."],
                      "context_exclusions": exclusions, "exported_commits": commits})
        atomic_json(staging / "index.json", index); (staging / "index.json").chmod(0o444)
        verify_snapshot(staging, index); staging.rename(destination); return index
    finally:
        if staging.exists(): shutil.rmtree(staging)


def _is_binary(content):
    try: content.decode("utf-8"); return False
    except UnicodeDecodeError: return True


def verify_snapshot(destination: Path, index: dict):
    root = Path(destination)
    if root.is_symlink() or not root.is_dir(): raise CampaignError("invalid snapshot destination")
    root = root.resolve(); verify_seal(index); allowed = {"index.json", "path-index.json"}; seen = []
    path_index = json.loads(_resolved(root, "path-index.json").read_text())
    verify_seal(path_index)
    if path_index["digest"] != index.get("path_index_digest"):
        raise CampaignError("snapshot path index digest mismatch")
    for item in index.get("objects", []):
        target = _resolved(root, item["storage_path"]); data = target.read_bytes()
        if _sha(data) != item["content_digest"]: raise CampaignError("snapshot object digest mismatch")
        if target.stat().st_mode & 0o222: raise CampaignError("snapshot object is writable")
        allowed.add(item["storage_path"])
    for shard in index.get("shards", []):
        target = _resolved(root, shard["diff_path"]); data = target.read_bytes()
        if _sha(data) != shard["diff_digest"] or len(data) != shard["diff_bytes"]: raise CampaignError("snapshot shard digest mismatch")
        seen.extend(shard["unit_ids"]); allowed.add(shard["diff_path"])
    if seen != index.get("unit_ids") or len(seen) != len(set(seen)): raise CampaignError("snapshot shard union does not exactly cover expected units")
    stored = json.loads((root / "index.json").read_text())
    if stored != index: raise CampaignError("stored snapshot index does not match")
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    if actual != allowed: raise CampaignError("snapshot file set mismatch")
    if any(path.is_symlink() for path in root.rglob("*")): raise CampaignError("snapshot contains a symlink")


def _resolved(root, relative):
    safe = _safe_path(relative); target = root / safe
    if target.is_symlink() or not target.is_file() or root not in target.resolve().parents:
        raise CampaignError("snapshot path escapes or is missing")
    return target


def read_evidence(repo: Path, commit: str, path: str, start: int, end: int) -> str:
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit) or type(start) is not int or type(end) is not int or start < 1 or end < start:
        raise CampaignError("invalid evidence coordinates")
    found = _blob(Path(repo).resolve(), commit, path)
    if found is None or found[0] not in {"100644", "100755"}: raise CampaignError("evidence is not a regular tracked file")
    content = found[1]
    if _secret_reason(path, content): raise CampaignError("evidence path is excluded by secret policy")
    try: lines = content.decode().splitlines(keepends=True)
    except UnicodeDecodeError as exc: raise CampaignError("non-UTF-8 evidence cannot be cited") from exc
    if end > len(lines): raise CampaignError("evidence line range exceeds the tracked blob")
    return "".join(lines[start - 1:end])
