"""Persistent polling and configured-lane review orchestration for pull requests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
import re
import shutil
import threading

from .common import CampaignError, atomic_json, digest, now_iso, read_json, seal, verify_seal
from .complexity import budget, classify
from .prompts import PROMPT_DIGEST
from .runner import validate_report


_SHA = re.compile(r"[0-9a-f]{40,64}")


@contextmanager
def service_lock(state_dir: Path):
    """Hold the process-wide nonblocking lock for one service state directory."""
    try:
        import fcntl
    except ImportError as exc:
        raise CampaignError("PR service locking requires fcntl") from exc
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / ".service.lock").open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CampaignError("this PR service already has an active process") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _pr_value(pr, *paths, default=None):
    for path in paths:
        value = pr
        for key in path.split("."):
            if not isinstance(value, dict) or key not in value:
                break
            value = value[key]
        else:
            return value
    return default


def _normalize_pr(repository, value):
    number = _pr_value(value, "number")
    head = _pr_value(value, "head_sha", "head.sha")
    state = str(_pr_value(value, "state", default="open")).lower()
    draft = _pr_value(value, "draft", "is_draft", default=False)
    base = _pr_value(value, "base_branch", "base_ref", "base.ref")
    base_sha = _pr_value(value, "base_sha", "base.sha")
    if type(number) is not int or number <= 0 or not isinstance(head, str) or not _SHA.fullmatch(head):
        raise CampaignError(f"GitHub returned an invalid PR identity for {repository}")
    if type(draft) is not bool:
        raise CampaignError("GitHub returned an invalid draft flag")
    metadata = {"title": _pr_value(value, "title", default=""),
                "body": _pr_value(value, "body", default=""),
                "author": _pr_value(value, "author.login", "user.login", default=""),
                "url": _pr_value(value, "url", "html_url", default="")}
    return {"repository": repository, "number": number, "head_sha": head, "state": state,
            "draft": draft, "base_branch": base, "base_ref": base, "base_sha": base_sha,
            "metadata": metadata, **metadata}


class PRService:
    """Poll configured repositories and durably review each distinct PR head.

    ``github`` supplies ``list_open_prs``, ``get_pr``, ``create_check``, and
    ``update_check``. The optional ``publisher`` supplies ``publish`` and
    defaults to ``github`` for combined test adapters. ``mirror`` supplies
    ``prepare_pr`` and ``runner_factory(lane)`` returns an isolated runner with
    ``run(job, scope, stop_event)``. No target content is executed by this class.
    """

    def __init__(self, config, github, mirror, runner_factory, *, state_dir, workspace=None,
                 publisher=None, stop_event=None):
        self.config, self.github, self.mirror, self.runner_factory = config, github, mirror, runner_factory
        self.publisher = publisher or github
        self.state_dir = Path(state_dir).resolve()
        self.workspace = Path(workspace).resolve() if workspace else self.state_dir / "inputs"
        self.stop = stop_event or threading.Event()
        self.path = self.state_dir / "pr-service.json"
        self._state_lock = threading.RLock()
        self.state = None

    def _load(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            state = read_json(self.path)
            if state.get("schema_version") != 1 or state.get("config_digest") != self.config.digest:
                raise CampaignError("saved PR service state is incompatible with the config")
        else:
            state = {"schema_version": 1, "config_digest": self.config.digest,
                     "created_at": now_iso(), "updated_at": now_iso(), "heads": {}}
        if not isinstance(state.get("heads"), dict):
            raise CampaignError("saved PR service heads are invalid")
        for identity, head in state["heads"].items():
            for lane_key, lane in head.get("lanes", {}).items():
                if lane.get("status") == "running":
                    lane["status"] = "pending"
                result_path = self._result_path(identity, lane_key)
                if lane.get("status") != "complete" and result_path.exists():
                    saved = read_json(result_path)
                    verify_seal(saved)
                    if saved.get("identity") == identity and saved.get("lane") == lane_key:
                        lane.update(status="complete", result_digest=saved["digest"])
        self.state = state
        self._save()

    def _save(self):
        self.state["updated_at"] = now_iso()
        atomic_json(self.path, self.state)

    @staticmethod
    def _identity(repository, number, head):
        return f"{repository}#{number}@{head}"

    def _record(self, pr):
        identity = self._identity(pr["repository"], pr["number"], pr["head_sha"])
        record = self.state["heads"].setdefault(identity, {
            "repository": pr["repository"], "number": pr["number"], "head_sha": pr["head_sha"],
            "status": "pending", "created_at": now_iso(), "check_id": None, "lanes": {
                lane["key"]: {"status": "pending", "attempts": 0} for lane in self.config.lanes}})
        return identity, record

    def _cleanup_snapshot(self, identity):
        target = self.workspace / digest(identity)
        workspace = self.workspace.resolve()
        if target.is_symlink():
            raise CampaignError("refusing to remove a symlinked PR snapshot")
        if target.exists():
            resolved = target.resolve()
            if resolved.parent != workspace or resolved.name != digest(identity):
                raise CampaignError("refusing to remove a snapshot outside the PR workspace")
            shutil.rmtree(resolved)

    def _check_start(self, pr, record):
        name = self.config.service["review_name"]
        if record.get("check_id") is None:
            external_id = "ai-pr-review:" + digest(self._identity(
                pr["repository"], pr["number"], pr["head_sha"]))
            receipt = self.github.create_check(pr["repository"], pr["head_sha"], name, external_id)
            check_id = receipt.get("id") if isinstance(receipt, dict) else receipt
            if check_id is None:
                raise CampaignError("GitHub did not return a check id")
            record["check_id"] = check_id
            self._save()
        self.github.update_check(pr["repository"], record["check_id"], status="in_progress",
                                 conclusion=None, summary="Review in progress")

    def _check_finish(self, pr, record, conclusion, summary):
        self.github.update_check(pr["repository"], record["check_id"], status="completed",
                                 conclusion=conclusion, summary=summary)

    def _result_path(self, identity, lane_key):
        return self.state_dir / "results" / f"{digest(identity)}-{lane_key}.json"

    def _saved_result(self, identity, lane_key, lane_state):
        value = read_json(self._result_path(identity, lane_key))
        verify_seal(value)
        if value["digest"] != lane_state.get("result_digest"):
            raise CampaignError("saved lane result receipt mismatch")
        return value["result"]

    def _job(self, identity, record, lane, prepared, attempt):
        scope, index = prepared["scope"], prepared["snapshot_index"]
        tier = scope.get("tier_override") or classify(scope, self.config)["tier"]
        units = [unit for unit in scope["units"] if unit.get("kind") == "file"]
        if not units:
            units = list(scope["units"])
        material = {"identity": identity, "lane": lane, "scope": scope["id"],
                    "snapshot": index["digest"], "units": [unit["id"] for unit in units]}
        identifier = digest(material)
        return {"id": identifier, "run_id": f"pr-{digest(identity)[:16]}", "scope_id": scope["id"],
                "stage": "pr", "ordinal": 0, "pr": record["number"],
                "base_sha": scope["base_sha"], "head_sha": scope["head_sha"],
                "campaign_head_sha": scope["head_sha"], "lane": dict(lane),
                "input_digest": digest(material), "prompt_digest": PROMPT_DIGEST,
                "tier": tier, "budget": budget(tier, self.config), "attempt": attempt,
                "expected_units": [unit["id"] for unit in units],
                "expected_unit_paths": {unit["id"]: unit.get("path") for unit in units},
                "assigned_shards": [shard["id"] for shard in index["shards"]]}

    def _run_lane(self, identity, record, lane, prepared):
        key = lane["key"]
        with self._state_lock:
            lane_state = record["lanes"][key]
            if lane_state["status"] == "complete":
                return self._saved_result(identity, key, lane_state)
            if lane_state["attempts"] >= self.config.service.get("max_attempts_per_lane", 3):
                raise CampaignError(f"lane {key} reached its per-head attempt limit")
            lane_state["attempts"] += 1
            lane_state["status"] = "running"
            lane_state.pop("error", None)
            self._save()
            job = self._job(identity, record, lane, prepared, lane_state["attempts"])
        try:
            runner = self.runner_factory(dict(lane), prepared)
            result = runner.run(job, prepared["scope"], self.stop)
            validate_report(result.get("report", {}), job, mode=getattr(runner, "mode", "live"))
            result = {**result, "lane": dict(lane), "model": lane["model"],
                      "effort": lane["effort"], "tier": job["tier"]}
            sealed = seal({"identity": identity, "lane": key, "job": job, "result": result})
            atomic_json(self._result_path(identity, key), sealed)
            with self._state_lock:
                lane_state.update(status="complete", result_digest=sealed["digest"])
                self._save()
            return result
        except Exception as exc:
            with self._state_lock:
                lane_state.update(status="failed", error=str(exc))
                self._save()
            raise

    def _results(self, identity, record):
        return [self._saved_result(identity, lane["key"], record["lanes"][lane["key"]])
                for lane in self.config.lanes]

    def _conclusion(self, results):
        findings = [finding for result in results for finding in result["report"]["findings"]]
        blocking = set(self.config.service["blocking_severities"])
        if any(finding.get("severity") in blocking for finding in findings):
            return "failure", f"Review found {len(findings)} finding(s), including blocking severity."
        if findings:
            return "neutral", f"Review found {len(findings)} nonblocking finding(s)."
        return "success", "Review completed with no findings."

    def _review(self, repository, number, expected_head=None):
        declaration = self.config.repository(repository)
        pr = _normalize_pr(repository, self.github.get_pr(repository, number))
        if pr["state"] != "open":
            return {"status": "closed", "repository": repository, "number": number}
        if expected_head is not None and pr["head_sha"] != expected_head:
            return {"status": "head_changed", "head_sha": pr["head_sha"]}
        if pr["draft"] and not declaration["include_drafts"]:
            return {"status": "draft", "head_sha": pr["head_sha"]}
        if pr["base_branch"] not in {None, declaration["base_branch"]}:
            return {"status": "base_skipped", "head_sha": pr["head_sha"]}
        identity, record = self._record(pr)
        if record["status"] == "reviewed":
            self._cleanup_snapshot(identity)
            return {"status": "reviewed", "identity": identity}
        if record["status"] == "exhausted":
            return {"status": "exhausted", "identity": identity}
        try:
            self._check_start(pr, record)
            prepared = self.mirror.prepare_pr(repository, number, declaration["base_branch"], pr["head_sha"],
                                              pr["metadata"], self.workspace / digest(identity), self.config)
            pending = [lane for lane in self.config.lanes
                       if record["lanes"][lane["key"]]["status"] != "complete"]
            errors = []
            with ThreadPoolExecutor(max_workers=len(self.config.lanes)) as pool:
                futures = {pool.submit(self._run_lane, identity, record, lane, prepared): lane for lane in pending}
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as exc:
                        errors.append(f"{futures[future]['key']}: {exc}")
            if errors:
                exhausted = any(value["status"] == "failed" and
                                value["attempts"] >= self.config.service.get("max_attempts_per_lane", 3)
                                for value in record["lanes"].values())
                record.update(status="exhausted" if exhausted else "retry", error="; ".join(errors))
                self._save()
                self._check_finish(pr, record, "action_required", "One or more review lanes require retry.")
                return {"status": record["status"], "identity": identity, "errors": errors}

            current = _normalize_pr(repository, self.github.get_pr(repository, number))
            if current["state"] != "open" or current["head_sha"] != pr["head_sha"]:
                record.update(status="superseded", superseded_by=current["head_sha"])
                self._save()
                try:
                    self._check_finish(pr, record, "cancelled", "PR head changed before publication.")
                except Exception as exc:
                    record["check_error"] = str(exc)
                    self._save()
                self._cleanup_snapshot(identity)
                return {"status": "superseded", "identity": identity}

            results = self._results(identity, record)
            if not record.get("publication_receipt"):
                publication_pr = {**pr, "base_sha": prepared["merge_base"]}
                receipt = self.publisher.publish(repository, publication_pr, pr["head_sha"], results,
                                                  prepared["line_map"])
                if not receipt:
                    raise CampaignError("GitHub publication returned no receipt")
                record["publication_receipt"] = receipt
                self._save()
            conclusion, summary = self._conclusion(results)
            self._check_finish(pr, record, conclusion, summary)
            record.update(status="reviewed", conclusion=conclusion, reviewed_at=now_iso())
            record.pop("error", None)
            self._save()
            self._cleanup_snapshot(identity)
            return {"status": "reviewed", "identity": identity, "conclusion": conclusion}
        except Exception as exc:
            record.update(status="retry", error=str(exc))
            self._save()
            if record.get("check_id") is not None:
                try:
                    self._check_finish(pr, record, "failure", "Review pipeline failed and will retry.")
                except Exception:
                    pass
            return {"status": "retry", "identity": identity, "errors": [str(exc)]}

    def review_pr(self, repository: str, number: int, expected_head: str | None = None):
        """Review one allowed PR head, resuming durable partial progress."""
        with service_lock(self.state_dir):
            self._load()
            return self._review(repository, number, expected_head)

    def _run_once(self):
        outcomes = []
        for declaration in self.config.repositories:
            if not declaration["enabled"]:
                continue
            repository = declaration["repository"]
            for raw in self.github.list_open_prs(repository):
                pr = _normalize_pr(repository, raw)
                if pr["draft"] and not declaration["include_drafts"]:
                    outcomes.append({"status": "draft", "repository": repository, "number": pr["number"]})
                    continue
                outcomes.append(self._review(repository, pr["number"], pr["head_sha"]))
                if self.stop.is_set():
                    return outcomes
        return outcomes

    def run_once(self):
        """Poll all enabled repositories once and process unseen heads."""
        with service_lock(self.state_dir):
            self._load()
            return self._run_once()

    def serve(self):
        """Poll until the injected stop event is set, holding one service lock."""
        with service_lock(self.state_dir):
            self._load()
            while not self.stop.is_set():
                self._run_once()
                self.stop.wait(self.config.service["poll_interval"])
