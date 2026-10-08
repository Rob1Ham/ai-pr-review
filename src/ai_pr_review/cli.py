"""Command-line interface."""

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading

from .campaign import Campaign
from .common import CampaignError, atomic_json, read_json, verify_seal
from .complexity import classify, tier_summary
from .config import load_config
from .inventory import build_inventory, recheck_refs
from .publish import GitHubRemote, MockRemote, Publisher
from .runner import MockRunner, credential_status
from .runner import OpenCodeRunner
from .github_pr import GitHubPRClient, PRReviewPublisher
from .git_mirror import GitMirror
from .pr_config import load_pr_config
from .pr_service import PRService
from .complexity import budget


def _repo(value):
    path = Path(value).resolve()
    if not (path / ".git").exists(): raise CampaignError(f"not a Git repository: {path}")
    return path


def _inventory(args, config, github):
    target = config.target
    return build_inventory(_repo(args.target_repo), target["base_ref"], target["head_ref"],
                           repository=target["repository"], branch=target["branch"], github=github,
                           lineage_declarations=config.lineage, pinned_base=target["base_sha"],
                           pinned_head=target["head_sha"])


def doctor(args):
    config = load_config(args.config); executable = shutil.which("opencode")
    models = {}
    for provider in sorted({lane["provider"] for lane in config.lanes}):
        listed = set()
        if executable:
            try:
                result = subprocess.run([executable, "models", provider], text=True, capture_output=True, timeout=30)
                if result.returncode == 0: listed = set(result.stdout.splitlines())
            except (OSError, subprocess.TimeoutExpired): pass
        models[provider] = {lane["model"]: lane["model"] in listed for lane in config.lanes if lane["provider"] == provider}
    status = {"config": {"name": config.data["name"], "digest": config.digest},
              "opencode": {"available": bool(executable), "models": models},
              "credentials": credential_status(config.lanes),
              "github_auth": "runtime gh credential or GH_TOKEN (not inspected)",
              "note": "Presence does not prove entitlement or max-effort acceptance."}
    print(json.dumps(status, indent=2))
    ready = bool(executable) and all(value for group in models.values() for value in group.values())
    return 0 if ready and all(item["available"] for item in status["credentials"]) else 1


def plan(args):
    config = load_config(args.config); manifest = _inventory(args, config, args.github)
    scopes = [scope for scope in manifest["scopes"] if scope["stage"] == "pr"]
    blockers = list(manifest["gate"]["blockers"])
    if manifest["counts"]["prs"] != config.data["expected"]["prs"]: blockers.append("expected PR count mismatch")
    if manifest["counts"]["commits"] != config.data["expected"]["commits"]: blockers.append("expected commit count mismatch")
    tiers = tier_summary(manifest, config)
    if tiers != config.data["expected"]["tier_counts"]: blockers.append("expected tier count mismatch")
    result = {"gate": {"status": "blocked" if blockers else manifest["gate"]["status"], "blockers": blockers},
              "counts": manifest["counts"], "tiers": tiers, "baseline_calls": (len(scopes) + 1) * len(config.lanes),
              "expected": config.data["expected"], "scopes": [{"pr": scope["pr"], **classify(scope, config)} for scope in scopes]}
    print(json.dumps(result, indent=2)); return 1 if blockers else 0


def _manifest(args, config, run_dir, live):
    path = run_dir / "manifest.json"
    if path.exists():
        value = read_json(path); verify_seal(value)
        if value.get("config_digest") != config.digest: raise CampaignError("run manifest uses a different config")
        if Path(value["repository_path"]).resolve() != _repo(args.target_repo): raise CampaignError("run manifest uses a different target repo")
        if live: recheck_refs(value)
        return value
    value = _inventory(args, config, live)
    if live and value["gate"]["status"] != "pass": raise CampaignError("live operation requires the exact GitHub PR gate")
    value = {key: item for key, item in value.items() if key != "digest"}; value["config_digest"] = config.digest
    from .common import seal
    value = seal(value); atomic_json(path, value); return value


def prepare(args):
    config = load_config(args.config); run_dir = Path(args.run_dir).resolve(); run_dir.mkdir(parents=True, exist_ok=True)
    manifest = _manifest(args, config, run_dir, True)
    summary = Campaign(run_dir, manifest, config, max_hours=args.max_hours).prepare()
    print(json.dumps(summary, indent=2)); return 0


def run(args):
    if args.max_cost_usd is None: raise CampaignError("live runs require --max-cost-usd")
    config = load_config(args.config); run_dir = Path(args.run_dir).resolve(); run_dir.mkdir(parents=True, exist_ok=True)
    manifest = _manifest(args, config, run_dir, True)
    remote = GitHubRemote(manifest["repository"]) if args.publish == "github" else None
    publisher = Publisher(run_dir, manifest, remote) if args.publish in {"github", "draft"} else None
    campaign = Campaign(run_dir, manifest, config, publisher=publisher, per_model=args.per_model,
                       retries=args.retries, retry_failed=args.retry_failed, max_hours=args.max_hours,
                       max_attempts=args.max_attempts, max_cost_usd=args.max_cost_usd,
                       emit=lambda text: print(text, flush=True))
    previous = {kind: signal.getsignal(kind) for kind in (signal.SIGINT, signal.SIGTERM)}
    for kind in previous: signal.signal(kind, lambda _number, _frame: campaign.cancel.set())
    try: summary = campaign.run()
    finally:
        for kind, handler in previous.items(): signal.signal(kind, handler)
    print(json.dumps(summary, indent=2)); return 0 if summary["status"] == "complete" else 2


def mock(args):
    config = load_config(args.config); run_dir = Path(args.run_dir).resolve(); run_dir.mkdir(parents=True, exist_ok=True)
    manifest = _manifest(args, config, run_dir, False)
    publisher = Publisher(run_dir, manifest, MockRemote(run_dir / "mock-github.json"))
    summary = Campaign(run_dir, manifest, config, runner=MockRunner(delay=args.mock_delay),
                       publisher=publisher, per_model=args.per_model,
                       max_hours=args.max_hours, emit=lambda text: print(text, flush=True)).run()
    print(json.dumps(summary, indent=2)); return 0 if summary["status"] == "complete" else 2


def status(args):
    state = read_json(Path(args.run_dir).resolve() / "progress.json"); counts = {}
    for record in state["jobs"].values(): counts[record["status"]] = counts.get(record["status"], 0) + 1
    print(json.dumps({"run_id": state["run_id"], "status": state["status"],
                      "updated_at": state["updated_at"], "jobs": counts}, indent=2)); return 0


def _pr_service(args):
    config = load_pr_config(args.config)
    state_dir, workspace = Path(args.state_dir).resolve(), Path(args.workspace_dir).resolve()
    client, mirror = GitHubPRClient(), GitMirror(workspace / "mirrors")
    publisher = PRReviewPublisher(client, config.service, state_dir / "publication-latest.json")
    def runner_factory(_lane, prepared):
        root, index = Path(prepared["snapshot_root"]), prepared["snapshot_index"]
        return OpenCodeRunner(lambda _job, _scope: (root, index), None,
                              budget("standard", config), config.checklist)
    return PRService(config, client, mirror, runner_factory, state_dir=state_dir,
                     workspace=workspace / "inputs", publisher=publisher)


def doctor_prs(args):
    config = load_pr_config(args.config)
    statuses = credential_status(config.lanes)
    result = {"repositories": [item["repository"] for item in config.repositories if item["enabled"]],
              "credentials": statuses,
              "github": "GH_TOKEN must allow read access plus Checks, Pull requests, and Issues write access"}
    print(json.dumps(result, indent=2))
    return 0 if all(item["available"] for item in statuses) and bool(os.environ.get("GH_TOKEN")) else 1


def serve_prs(args):
    service = _pr_service(args)
    previous = {kind: signal.getsignal(kind) for kind in (signal.SIGINT, signal.SIGTERM)}
    for kind in previous: signal.signal(kind, lambda _number, _frame: service.stop.set())
    try: service.serve()
    finally:
        for kind, handler in previous.items(): signal.signal(kind, handler)
    return 0


def run_prs_once(args):
    outcomes = _pr_service(args).run_once(); print(json.dumps(outcomes, indent=2)); return 0


def review_pr(args):
    outcome = _pr_service(args).review_pr(args.repository, args.number, args.head)
    print(json.dumps(outcome, indent=2)); return 0 if outcome["status"] in {"reviewed", "draft"} else 2


def review_local(args):
    from .local_job import review_local as execute
    outcome = execute(load_pr_config(args.config), args.repository, args.number,
                      args.run_dir, args.workspace_dir, mock=args.mock,
                      base=args.base, expected_head=args.head,
                      retired_lanes=args.retired_lane,
                      emit=lambda text: print(text, file=sys.stderr, flush=True))
    print(json.dumps(outcome, indent=2))
    return 0 if outcome["status"] == "complete" else 2


def publish_local(args):
    from .local_job import publish_saved
    receipt = publish_saved(load_pr_config(args.config), args.run_dir,
                            allow_partial=args.allow_partial, dry_run=args.dry_run, allow_merged=args.allow_merged,
                            retired_lanes=args.retired_lane, hold_invalid_findings=args.hold_invalid_findings)
    print(json.dumps(receipt, indent=2))
    return 0


def plan_history(args):
    from .history import plan_history as execute
    count = args.count if args.count is not None or args.since else 50
    manifest = execute(load_pr_config(args.config), args.repository, args.run_dir, count=count,
                       since=args.since, base_branch=args.base_branch)
    print(json.dumps(manifest, indent=2))
    return 0


def run_history(args):
    from .history import run_history as execute
    stop = threading.Event()
    previous = {kind: signal.getsignal(kind) for kind in (signal.SIGINT, signal.SIGTERM)}
    for kind in previous: signal.signal(kind, lambda *_: stop.set())
    try:
        outcome = execute(load_pr_config(args.config), args.run_dir, args.workspace_dir,
                           publish=args.publish, mock=args.mock, allow_partial=args.allow_partial,
                           max_prs=args.max_prs, stop_event=stop, hold_invalid_findings=args.hold_invalid_findings,
                          emit=lambda text: print(text, file=sys.stderr, flush=True))
    finally:
        for kind, handler in previous.items(): signal.signal(kind, handler)
    print(json.dumps(outcome, indent=2))
    return 0 if outcome["status"] in {"complete", "paused"} else 2


def plan_history_range(args):
    from .range_history import plan_range
    print(json.dumps(plan_range(load_pr_config(args.config), args.repository, args.run_dir,
                               args.base_ref, args.head_ref), indent=2))
    return 0


def history_status(args):
    from .history import history_status as execute
    print(json.dumps(execute(args.run_dir), indent=2))
    return 0


def publish_history_issues(args):
    from .history_issues import publish_history_issues as execute
    print(json.dumps(execute(load_pr_config(args.config), args.run_dir, dry_run=args.dry_run), indent=2))
    return 0


def publish_local_issues(args):
    from .history_issues import publish_local_issues as execute
    print(json.dumps(execute(load_pr_config(args.config), args.run_dir, dry_run=args.dry_run), indent=2))
    return 0


def cleanup_history(args):
    from .cleanup import cleanup_history as execute
    print(json.dumps(execute(args.run_dir, args.snapshot_dir, apply=args.apply), indent=2))
    return 0


def reconcile_history(args):
    from .reconcile import reconcile_history as execute
    print(json.dumps(execute(load_pr_config(args.config), args.run_dir, args.number), indent=2))
    return 0


def retire_history(args):
    from .retirement import retire_history as execute
    print(json.dumps(execute(load_pr_config(args.config), args.run_dir, args.lane), indent=2))
    return 0


def parser():
    root = argparse.ArgumentParser(description="Parallel static pull request review campaign")
    commands = root.add_subparsers(dest="command", required=True)
    doctor_parser = commands.add_parser("doctor"); doctor_parser.add_argument("--config", required=True); doctor_parser.set_defaults(func=doctor)
    common = argparse.ArgumentParser(add_help=False); common.add_argument("--config", required=True); common.add_argument("--target-repo", required=True)
    planned = commands.add_parser("plan", parents=[common]); planned.add_argument("--github", action="store_true"); planned.set_defaults(func=plan)
    prepared = commands.add_parser("prepare", parents=[common]); prepared.add_argument("--run-dir", required=True); prepared.add_argument("--max-hours", type=float, default=2); prepared.set_defaults(func=prepare)
    execute = commands.add_parser("run", parents=[common]); execute.add_argument("--run-dir", required=True)
    execute.add_argument("--publish", choices=("github", "draft", "none"), default="github")
    execute.add_argument("--max-cost-usd", type=float, required=True); execute.add_argument("--per-model", type=int, default=1)
    execute.add_argument("--retries", type=int, default=2); execute.add_argument("--retry-failed", action="store_true")
    execute.add_argument("--max-hours", type=float, default=12); execute.add_argument("--max-attempts", type=int, default=1000); execute.set_defaults(func=run)
    mocked = commands.add_parser("mock", parents=[common]); mocked.add_argument("--run-dir", required=True)
    mocked.add_argument("--per-model", type=int, default=1); mocked.add_argument("--max-hours", type=float, default=1)
    mocked.add_argument("--mock-delay", type=float, default=.001); mocked.set_defaults(func=mock)
    inspect = commands.add_parser("status"); inspect.add_argument("--run-dir", required=True); inspect.set_defaults(func=status)
    pr_common = argparse.ArgumentParser(add_help=False); pr_common.add_argument("--config", required=True)
    pr_common.add_argument("--state-dir", required=True); pr_common.add_argument("--workspace-dir", required=True)
    prs_doctor = commands.add_parser("doctor-prs"); prs_doctor.add_argument("--config", required=True); prs_doctor.set_defaults(func=doctor_prs)
    serve = commands.add_parser("serve-prs", parents=[pr_common]); serve.set_defaults(func=serve_prs)
    once = commands.add_parser("run-prs-once", parents=[pr_common]); once.set_defaults(func=run_prs_once)
    one = commands.add_parser("review-pr", parents=[pr_common]); one.add_argument("--repository", required=True)
    one.add_argument("--number", required=True, type=int); one.add_argument("--head"); one.set_defaults(func=review_pr)
    local = commands.add_parser("review-local", help="Review an open/merged PR with local-only publication")
    local.add_argument("--config", required=True); local.add_argument("--repository", required=True)
    local.add_argument("--number", required=True, type=int); local.add_argument("--head"); local.add_argument("--base")
    local.add_argument("--run-dir", required=True); local.add_argument("--workspace-dir", required=True)
    local.add_argument("--mock", action="store_true"); local.set_defaults(func=review_local)
    local.add_argument("--retired-lane", action="append", default=[])
    publish = commands.add_parser("publish-local", help="Submit completed live local results as unified model reviews")
    publish.add_argument("--config", required=True); publish.add_argument("--run-dir", required=True)
    publish.add_argument("--allow-partial", action="store_true", help="Explicitly publish only completed lanes after failures")
    publish.add_argument("--dry-run", action="store_true", help="Validate and render locally without GitHub writes")
    publish.add_argument("--allow-merged", action="store_true", help="Allow retrospective reviews of verified merged PR heads")
    publish.add_argument("--retired-lane", action="append", default=[])
    publish.add_argument("--hold-invalid-findings", action="store_true", help="Withhold findings with invalid coordinates or unconfirmed head presence and record the audit")
    publish.set_defaults(func=publish_local)
    history_plan = commands.add_parser("plan-history", help="Freeze the most recently merged PRs by merge timestamp")
    history_plan.add_argument("--config", required=True); history_plan.add_argument("--repository", required=True)
    history_plan.add_argument("--run-dir", required=True); history_plan.add_argument("--count", type=int)
    history_plan.add_argument("--since", help="Inclusive ISO-8601 merged-at boundary; selects all matches unless --count is set")
    history_plan.add_argument("--base-branch", help="Include only PRs merged into this base branch")
    history_plan.set_defaults(func=plan_history)
    range_plan = commands.add_parser("plan-history-range", help="Freeze uncovered merged PRs in a release-to-branch range")
    range_plan.add_argument("--config", required=True); range_plan.add_argument("--repository", required=True)
    range_plan.add_argument("--run-dir", required=True); range_plan.add_argument("--base-ref", required=True)
    range_plan.add_argument("--head-ref", required=True); range_plan.set_defaults(func=plan_history_range)
    history_run = commands.add_parser("run-history", help="Resume local historical reviews with optional GitHub publication")
    history_run.add_argument("--config", required=True); history_run.add_argument("--run-dir", required=True)
    history_run.add_argument("--workspace-dir", required=True)
    history_run.add_argument("--publish", choices=("none", "github"), required=True)
    history_run.add_argument("--mock", action="store_true"); history_run.add_argument("--allow-partial", action="store_true")
    history_run.add_argument("--hold-invalid-findings", action="store_true", help="Withhold invalid-coordinate or unconfirmed-head candidates and publish the validated remainder")
    history_run.add_argument("--max-prs", type=int); history_run.set_defaults(func=run_history)
    history_inspect = commands.add_parser("history-status")
    history_inspect.add_argument("--run-dir", required=True); history_inspect.set_defaults(func=history_status)
    issue_publish = commands.add_parser("publish-history-issues", help="Create one comprehensive issue per validated historical finding")
    issue_publish.add_argument("--config", required=True); issue_publish.add_argument("--run-dir", required=True)
    issue_publish.add_argument("--dry-run", action="store_true"); issue_publish.set_defaults(func=publish_history_issues)
    local_issue_publish = commands.add_parser("publish-local-issues", help="Create one comprehensive issue per validated local-review finding")
    local_issue_publish.add_argument("--config", required=True); local_issue_publish.add_argument("--run-dir", required=True)
    local_issue_publish.add_argument("--dry-run", action="store_true"); local_issue_publish.set_defaults(func=publish_local_issues)
    reconcile = commands.add_parser("reconcile-history", help="Verify saved GitHub reviews and reconcile a stopped batch")
    reconcile.add_argument("--config", required=True); reconcile.add_argument("--run-dir", required=True)
    reconcile.add_argument("--number", type=int, action="append", required=True)
    reconcile.set_defaults(func=reconcile_history)
    cleanup = commands.add_parser("cleanup-history", help="Remove only published-work snapshots from a stopped batch")
    cleanup.add_argument("--run-dir", required=True); cleanup.add_argument("--snapshot-dir", required=True)
    cleanup.add_argument("--apply", action="store_true", help="Delete the verified snapshots; default is dry-run")
    cleanup.set_defaults(func=cleanup_history)
    retire = commands.add_parser("retire-history", help="Retire model lanes from a stopped frozen batch without replacing evidence")
    retire.add_argument("--config", required=True); retire.add_argument("--run-dir", required=True)
    retire.add_argument("--lane", action="append", required=True); retire.set_defaults(func=retire_history)
    return root


def main(argv=None):
    try:
        args = parser().parse_args(argv); return args.func(args)
    except CampaignError as exc:
        print(f"error: {exc}", file=sys.stderr); return 2


if __name__ == "__main__": raise SystemExit(main())
