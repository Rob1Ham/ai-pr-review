"""Reconcile out-of-band historical publications without inference or remote writes."""

from contextlib import ExitStack
from pathlib import Path

from .common import CampaignError, now_iso, read_json, verify_seal
from .github_pr import GitHubPRClient
from .history import _existing_complete_reviews, _save_progress, _validate_manifest, history_status
from .pr_service import service_lock
from .retirement import active_lanes, load_retirement


def reconcile_history(config, root, numbers, *, client=None):
    root = Path(root).resolve()
    client = client or GitHubPRClient()
    if not numbers or any(type(number) is not int or number <= 0 for number in numbers):
        raise CampaignError("reconciliation requires positive PR numbers")
    with ExitStack() as locks:
        locks.enter_context(service_lock(root))
        batch = read_json(root / "batch-manifest.json")
        _validate_manifest(batch, config)
        state = read_json(root / "batch-progress.json")
        if (state.get("manifest_digest") != batch["digest"]
                or state.get("repository") != batch["repository"]
                or state.get("policy", {}).get("publish") != "github"
                or state["policy"].get("mock") is not False):
            raise CampaignError("reconciliation requires matching live GitHub batch state")
        if state["status"] == "running":
            raise CampaignError("stop the history batch before reconciling publications")
        selected = {item["number"]: item for item in batch["prs"]}
        if set(numbers) - selected.keys():
            raise CampaignError("requested PR is not in the frozen cohort")
        lanes = active_lanes(config, load_retirement(config, batch, root))
        models = {lane["model"] for lane in lanes}
        updates = {}
        for number in dict.fromkeys(numbers):
            item = selected[number]
            record = state["prs"][str(number)]
            run = root / "prs" / f"{number}-{item['head_sha'][:12]}"
            if run.is_symlink() or not run.is_dir() or run.resolve().parent != root / "prs":
                raise CampaignError("invalid retained run directory")
            locks.enter_context(service_lock(run))
            manifest = read_json(run / "local-manifest.json")
            receipt = read_json(run / "github-receipt.json")
            verify_seal(manifest)
            verify_seal(receipt)
            if (record["head"] != item["head_sha"] or manifest.get("config") != config.digest
                    or manifest.get("mode") != "live" or receipt.get("publication") != "github"
                    or any(value.get("repository") != batch["repository"] or value.get("pr") != number
                           or value.get("head") != item["head_sha"] for value in (manifest, receipt))):
                raise CampaignError(f"PR #{number} publication identity mismatch")
            reviews = receipt.get("reviews", [])
            published = {review["model"] for review in reviews}
            omitted = receipt.get("omitted_models", [])
            if (not published or len(published) != len(reviews) or not published <= models
                    or set(omitted) != models - published or len(omitted) != len(set(omitted))):
                raise CampaignError("receipt does not account for the active model lanes")
            if omitted and not state["policy"].get("allow_partial"):
                raise CampaignError("batch policy does not allow partial publications")
            withheld = receipt.get("withheld_count", 0)
            if type(withheld) is not int or withheld < 0:
                raise CampaignError("invalid withheld finding count")
            audit_path = run / "publication-validation.json"
            if withheld or audit_path.exists():
                audit = read_json(audit_path)
                verify_seal(audit)
                if (audit["digest"] != receipt.get("validation_digest")
                        or audit.get("manifest_digest") != manifest["digest"]
                        or sum(len(lane["withheld"]) for lane in audit["lanes"]) != withheld):
                    raise CampaignError("publication validation audit mismatch")
            current = client.get_pr(batch["repository"], number)
            if (not current.get("merged") or current["head_sha"] != item["head_sha"]
                    or current.get("merge_commit_sha") != item["merge_commit_sha"]):
                raise CampaignError("merged PR identity differs from the frozen cohort")
            remote = _existing_complete_reviews(client, batch["repository"], item,
                                                [lane for lane in lanes if lane["model"] in published])
            if remote is None or {r["model"]: r["review_id"] for r in remote} != {
                    r["model"]: r["review_id"] for r in reviews}:
                raise CampaignError("remote model reviews do not match the saved receipt")
            updates[str(number)] = {"status": "partial" if omitted else "published", "reviews": reviews,
                                    "omitted_models": omitted, "receipt_digest": receipt["digest"],
                                    "withheld_count": withheld, "reconciled_at": now_iso()}
        # Validate every requested repair before changing any batch state.
        for number, update in updates.items():
            record = state["prs"][number]
            if record.get("error"):
                record["previous_error"] = record.pop("error")
            record.update(update)
        pending = any(record["status"] in {"pending", "running", "interrupted"} for record in state["prs"].values())
        failures = any(record["status"] in {"failed", "partial", "review_failed"} for record in state["prs"].values())
        if not pending:
            state["status"] = "complete_with_failures" if failures else "complete"
            state.pop("pause_reason", None)
        _save_progress(root, state)
        return {**history_status(root), "reconciled_prs": [int(number) for number in updates]}
