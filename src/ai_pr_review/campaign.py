"""Four model queues with atomic resumable state and a PR barrier."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import random
import threading
import time

from .common import CampaignError, Cancelled, TransientError, atomic_json, digest, now_iso, read_json, seal, verify_seal
from .complexity import budget, budget_version, classify
from .prompts import ISSUE_PROMPT_DIGEST as PROMPT_DIGEST
from .runner import RUNNER_VERSION, OpenCodeRunner, validate_report
from .snapshot import SNAPSHOT_VERSION, prepare_scope, read_evidence, verify_snapshot


@contextmanager
def run_lock(directory: Path):
    try: import fcntl
    except ImportError as exc: raise CampaignError("run locking requires a platform with fcntl (Linux and macOS are supported)") from exc
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".run.lock").open("a+") as stream:
        try: fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise CampaignError("this run directory already has an active runner") from None
        try: yield
        finally: fcntl.flock(stream, fcntl.LOCK_UN)


class Campaign:
    def __init__(self, directory, manifest, config, runner=None, publisher=None, *, per_model=1,
                 retries=2, retry_failed=False, max_hours=12, max_attempts=1000,
                 max_cost_usd=None, emit=print):
        verify_seal(manifest)
        if type(per_model) is not int or not 1 <= per_model <= 4: raise CampaignError("use 1-4 jobs per model")
        self.directory, self.manifest, self.config = Path(directory), manifest, config
        self.repository, self.lanes = Path(manifest["repository_path"]), config.lanes
        self.scopes = {scope["id"]: scope for scope in manifest["scopes"]}
        if len(self.scopes) != len(manifest["scopes"]): raise CampaignError("duplicate scope identities")
        self.runner = runner or OpenCodeRunner(self.resolve_snapshot, None, budget("standard", config), config.checklist)
        self.mode = self.runner.mode
        if self.mode == "live" and manifest["gate"]["status"] != "pass": raise CampaignError("live review requires the GitHub gate")
        self.publisher, self.per_model, self.retries = publisher, per_model, retries
        self.retry_failed, self.max_hours, self.max_attempts = retry_failed, max_hours, max_attempts
        self.max_cost_usd, self.emit = max_cost_usd, emit
        self.cancel, self.lock = threading.Event(), threading.RLock()
        self.snapshot_locks = {key: threading.Lock() for key in self.scopes}; self.snapshots = {}
        self.state_path, self.state, self.deadline = self.directory / "progress.json", None, float("inf")

    def _signature(self):
        return digest({"manifest": self.manifest["digest"], "config": self.config.digest,
                       "prompt": PROMPT_DIGEST, "runner": RUNNER_VERSION, "snapshot": SNAPSHOT_VERSION,
                       "budgets": budget_version(self.config), "tiers": {key: classify(value, self.config) for key, value in self.scopes.items()}})

    def _save(self): self.state["updated_at"] = now_iso(); atomic_json(self.state_path, self.state)

    def _load(self):
        signature = self._signature()
        if self.state_path.exists():
            self.state = read_json(self.state_path)
            if self.state.get("signature") != signature: raise CampaignError("saved run signature differs; use a new run directory")
            for record in self.state["jobs"].values():
                if record["status"] in {"running", "cancelled"}: record["status"] = "pending"
                if record["status"] == "failed" and self.retry_failed:
                    record["status"] = "pending"; record["transient_failures"] = 0
                if record["status"] == "complete":
                    result = self._saved(record); self._validate(record["job"], result)
                    if self.publisher: self.publisher.submit(record["job"], result)
            completed = {(record["job"]["scope_id"], record["job"]["lane"]["key"])
                         for record in self.state["jobs"].values() if record["status"] == "complete"}
            for scope_id, lanes in self.state.get("scopes", {}).items():
                for lane_key, value in lanes.items():
                    if value.get("status") == "complete" and (scope_id, lane_key) not in completed:
                        value["status"] = "pending"
        else:
            self.state = {"schema_version": 1, "signature": signature, "config_digest": self.config.digest,
                          "run_id": f"{self.directory.name}-{signature[:12]}", "mode": self.mode,
                          "started_at": now_iso(), "status": "ready", "jobs": {}, "scopes": {}}
        self._save()

    def _saved(self, record):
        value = read_json(self.directory / "results" / f"{record['job']['id']}.json"); verify_seal(value)
        if value["digest"] != record.get("result_digest"): raise CampaignError("saved result receipt mismatch")
        return value["result"]

    def _validate(self, job, result):
        provenance = result.get("provenance", {})
        if provenance.get("mode") != self.mode or provenance.get("provider") != job["lane"]["provider"] or provenance.get("model_requested") != job["lane"]["model"]:
            raise CampaignError("result routing does not match its exact lane")
        if self.mode == "live" and provenance.get("routing_observed") is not True: raise CampaignError("exact transport route was not observed")
        validate_report(result.get("report", {}), job, mode=self.mode)
        if self.mode == "live":
            for unit in result["report"]["coverage"]["units"]:
                for citation in unit["evidence"]:
                    commit, path, lines = citation.split(":", 2); start, end = map(int, lines.split("-", 1))
                    read_evidence(self.repository, commit, path, start, end)

    def _prepared(self, scope):
        tier = classify(scope, self.config)["tier"]
        with self.snapshot_locks[scope["id"]]:
            if scope["id"] in self.snapshots: return self.snapshots[scope["id"]]
            root = self.directory / "inputs" / digest([SNAPSHOT_VERSION, budget_version(self.config), scope["id"], tier])
            if self.mode == "mock":
                ids = [unit["id"] for unit in scope["units"]]; size = self.config.tiers[tier]["units"]
                index = seal({"shards": [{"id": digest(ids[i:i + size]), "unit_ids": ids[i:i + size]} for i in range(0, len(ids), size)], "gaps": [], "mode": "mock"})
            else:
                root.parent.mkdir(parents=True, exist_ok=True)
                index = prepare_scope(self.repository, scope, root, campaign_head=self.manifest["head_sha"],
                                      max_diff_bytes=self.config.tiers[tier]["diff_bytes"],
                                      max_units_per_shard=self.config.tiers[tier]["units"])
                if index["gaps"]: raise CampaignError("changed evidence unavailable: " + "; ".join(index["gaps"][:5]))
            self.snapshots[scope["id"]] = root, index; return root, index

    def _check_stop(self):
        if time.monotonic() >= self.deadline: self.cancel.set()
        if self.cancel.is_set(): raise Cancelled("campaign paused or reached max_hours")

    def resolve_snapshot(self, job, scope):
        root, index = self._prepared(scope); verify_snapshot(root, index); return root, index

    def _job(self, scope, lane, index):
        tier = classify(scope, self.config)["tier"]
        units = [unit["id"] for unit in scope["units"] if unit.get("kind") in {"file", "lineage", "integration"}] or [unit["id"] for unit in scope["units"]]
        material = {"scope": scope["id"], "lane": lane, "index": index["digest"], "units": units,
                    "prompt": PROMPT_DIGEST, "runner": RUNNER_VERSION, "budget": budget_version(self.config)}
        identifier = digest(material)
        return {"id": identifier, "run_id": self.state["run_id"], "scope_id": scope["id"], "stage": scope["stage"],
                "ordinal": scope["ordinal"], "pr": scope.get("pr"), "base_sha": scope["base_sha"],
                "head_sha": scope["head_sha"], "campaign_head_sha": self.manifest["head_sha"], "lane": dict(lane),
                "input_digest": digest(material), "prompt_digest": PROMPT_DIGEST, "tier": tier,
                "budget": budget(tier, self.config), "expected_units": units,
                "expected_unit_paths": {unit["id"]: unit.get("path") for unit in scope["units"] if unit["id"] in units},
                "assigned_shards": [shard["id"] for shard in index["shards"]]}

    def _execute(self, job, scope):
        with self.lock:
            record = self.state["jobs"].setdefault(job["id"], {"job": job, "status": "pending", "attempts": 0, "transient_failures": 0})
            if record["status"] == "complete": return self._saved(record)
        while True:
            with self.lock:
                self._check_stop()
                known_cost = sum((item.get("usage") or {}).get("cost_usd") or 0 for item in self.state["jobs"].values())
                attempts = sum(item.get("attempts", 0) for item in self.state["jobs"].values())
                if self.max_cost_usd is not None and known_cost >= self.max_cost_usd: raise Cancelled("provider-reported cost ceiling reached")
                if attempts >= self.max_attempts: raise Cancelled("max_attempts reached")
                record["attempts"] += 1; record["status"] = "running"; job["attempt"] = record["attempts"]; self._save()
            try:
                result = self.runner.run(job, scope, self.cancel); self._validate(job, result)
                sealed = seal({"result": result}); atomic_json(self.directory / "results" / f"{job['id']}.json", sealed)
                with self.lock: record.update(status="complete", result_digest=sealed["digest"], usage=result.get("usage")); self._save()
                if self.publisher: self.publisher.submit(job, result)
                return result
            except TransientError as exc:
                with self.lock: record["transient_failures"] += 1; record["status"] = "pending"; record["error"] = str(exc); self._save()
                if record["transient_failures"] > self.retries: raise CampaignError("transient retries exhausted") from None
                if self.cancel.wait(min(60, 2 ** record["transient_failures"]) + random.random()): raise Cancelled("cancelled")
            except Cancelled:
                with self.lock: record.update(status="pending", error="interrupted"); self._save()
                raise
            except CampaignError as exc:
                with self.lock: record.update(status="failed", error=str(exc)); self._save()
                raise

    def _scope_lane(self, scope, lane):
        lane_state = self.state["scopes"].setdefault(scope["id"], {})
        if lane_state.get(lane["key"], {}).get("status") == "complete": return True
        try:
            _, index = self._prepared(scope); self._execute(self._job(scope, lane, index), scope)
            with self.lock: lane_state[lane["key"]] = {"status": "complete"}; self._save()
            return True
        except Cancelled:
            with self.lock: lane_state[lane["key"]] = {"status": "pending"}; self._save()
            return False
        except CampaignError as exc:
            with self.lock: lane_state[lane["key"]] = {"status": "failed", "error": str(exc)}; self._save()
            return False

    def _stage(self, stage):
        scopes = sorted((s for s in self.manifest["scopes"] if s["stage"] == stage), key=lambda x: x["ordinal"])
        assignments = [(lane, scopes[offset::self.per_model]) for lane in self.lanes for offset in range(self.per_model)]
        def worker(lane, values):
            for scope in values:
                if self.cancel.is_set(): return
                self._scope_lane(scope, lane)
        with ThreadPoolExecutor(max_workers=len(assignments)) as pool:
            for future in [pool.submit(worker, *item) for item in assignments]: future.result()
        return all(self.state["scopes"].get(scope["id"], {}).get(lane["key"], {}).get("status") == "complete" for scope in scopes for lane in self.lanes)

    def run(self):
        with run_lock(self.directory):
            self._load(); self.deadline = time.monotonic() + self.max_hours * 3600; self.state["status"] = "running"; self._save()
            prs = self._stage("pr"); whole = self._stage("whole_range") if prs and not self.cancel.is_set() else False
            publication = self.publisher.summary() if self.publisher else {"claims": {}}
            blocked = any(publication["claims"].get(key, 0) for key in ("error", "unknown", "pending"))
            self.state["status"] = "complete" if prs and whole and not blocked else "paused" if self.cancel.is_set() else "blocked"
            self._save(); return self.summary()

    def prepare(self):
        with run_lock(self.directory):
            self._load(); self.state["status"] = "preparing"; self._save()
            for scope in sorted(self.manifest["scopes"], key=lambda x: x["ordinal"]): self._prepared(scope)
            self.state["status"] = "prepared"; self._save(); return self.summary()

    def summary(self):
        jobs = {}
        for record in self.state["jobs"].values(): jobs[record["status"]] = jobs.get(record["status"], 0) + 1
        scopes = {stage: {"expected": len([s for s in self.manifest["scopes"] if s["stage"] == stage]) * len(self.lanes),
                          "complete": sum(self.state["scopes"].get(s["id"], {}).get(l["key"], {}).get("status") == "complete"
                                          for s in self.manifest["scopes"] if s["stage"] == stage for l in self.lanes)}
                  for stage in ("pr", "whole_range")}
        return {"run_id": self.state["run_id"], "status": self.state["status"], "mode": self.mode,
                "jobs": jobs, "scopes": scopes, "publication": self.publisher.summary() if self.publisher else None}
