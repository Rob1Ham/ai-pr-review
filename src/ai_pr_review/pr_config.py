"""Strict versioned configuration for persistent pull request review."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from .common import CampaignError, digest, read_json


CONFIG_VERSION = 1
_TOP = {"version", "repositories", "lanes", "tier_budgets", "risk_patterns",
        "review_checklist", "service"}
_REPOSITORY_KEYS = {"repository", "default_branch", "base_branch", "enabled", "include_drafts"}
_LANE_KEYS = {"key", "provider", "model", "effort"}
_BUDGET_KEYS = {"timeout_s", "no_progress_s", "steps", "output_tokens", "max_prompt_bytes",
                "max_output_bytes", "diff_bytes", "units"}
_SERVICE_KEYS = {"poll_interval", "max_parallel_prs", "max_inline_comments",
                  "blocking_severities", "review_name", "max_attempts_per_lane"}
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class PRConfig:
    """Validated PR service configuration and its deterministic digest."""

    path: Path
    data: dict
    digest: str

    @property
    def repositories(self):
        return tuple(self.data["repositories"])

    @property
    def lanes(self):
        return tuple(self.data["lanes"])

    @property
    def tiers(self):
        return self.data["tier_budgets"]

    @property
    def checklist(self):
        return tuple(self.data["review_checklist"])

    @property
    def service(self):
        return self.data["service"]

    def repository(self, name: str) -> dict:
        """Return an enabled repository declaration using canonical case."""
        matches = [item for item in self.repositories if item["repository"].casefold() == name.casefold()]
        if not matches or matches[0]["repository"] != name or not matches[0]["enabled"]:
            raise CampaignError(f"repository is not enabled with canonical identity: {name}")
        return matches[0]


def _object(value, keys, name, *, exact=True):
    if not isinstance(value, dict):
        raise CampaignError(f"{name} must be an object")
    unknown = set(value) - keys
    if unknown:
        raise CampaignError(f"unknown {name} keys: {', '.join(sorted(unknown))}")
    if exact and set(value) != keys:
        raise CampaignError(f"{name} requires every version 1 field")


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or "\0" in value or "\n" in value:
        raise CampaignError(f"{name} must be a nonempty single-line string")


def load_pr_config(path: str | Path) -> PRConfig:
    """Load and strictly validate a version 1 PR service JSON config."""
    source = Path(path).resolve()
    data = read_json(source)
    _object(data, _TOP, "config")
    if data["version"] != CONFIG_VERSION:
        raise CampaignError("unsupported PR config version")

    repositories = data["repositories"]
    if not isinstance(repositories, list) or not repositories:
        raise CampaignError("repositories must be a nonempty list")
    seen_repositories = set()
    for item in repositories:
        _object(item, _REPOSITORY_KEYS, "repository")
        if not isinstance(item["repository"], str) or not _REPOSITORY.fullmatch(item["repository"]):
            raise CampaignError("repository.repository must be owner/name")
        folded = item["repository"].casefold()
        if folded in seen_repositories:
            raise CampaignError("repository identities must be unique ignoring case")
        seen_repositories.add(folded)
        for key in ("default_branch", "base_branch"):
            _text(item[key], f"repository.{key}")
        for key in ("enabled", "include_drafts"):
            if type(item[key]) is not bool:
                raise CampaignError(f"repository.{key} must be boolean")

    lanes = data["lanes"]
    if not isinstance(lanes, list) or not 1 <= len(lanes) <= 4:
        raise CampaignError("config requires one to four lanes")
    lane_keys = set()
    for lane in lanes:
        _object(lane, _LANE_KEYS, "lane")
        for key in _LANE_KEYS:
            _text(lane[key], f"lane.{key}")
        if lane["key"] in lane_keys:
            raise CampaignError(f"duplicate lane: {lane['key']}")
        lane_keys.add(lane["key"])
        if lane["effort"] != "max" or not lane["model"].startswith(lane["provider"] + "/"):
            raise CampaignError("every lane must use its provider model at max effort")

    tiers = data["tier_budgets"]
    _object(tiers, {"light", "standard", "deep"}, "tier_budgets")
    for name, values in tiers.items():
        _object(values, _BUDGET_KEYS, f"tier {name}")
        if any(type(value) is not int or value <= 0 for value in values.values()):
            raise CampaignError(f"tier {name} requires positive integer budgets")

    risk = data["risk_patterns"]
    _object(risk, {"sensitive", "critical"}, "risk_patterns")
    for name, pattern in risk.items():
        _text(pattern, f"risk_patterns.{name}")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise CampaignError(f"invalid {name} regex") from exc

    checklist = data["review_checklist"]
    if not isinstance(checklist, list) or not checklist or any(
            not isinstance(item, str) or not item.strip() for item in checklist):
        raise CampaignError("review_checklist must be a nonempty string list")

    service = data["service"]
    _object(service, _SERVICE_KEYS, "service")
    for key in ("poll_interval", "max_parallel_prs", "max_inline_comments", "max_attempts_per_lane"):
        if type(service[key]) is not int or service[key] <= 0:
            raise CampaignError(f"service.{key} must be a positive integer")
    severities = service["blocking_severities"]
    if not isinstance(severities, list) or len(severities) != len(set(severities)) or any(
            not isinstance(value, str) or not value.strip() for value in severities):
        raise CampaignError("service.blocking_severities must be a unique string list")
    _text(service["review_name"], "service.review_name")
    return PRConfig(source, data, digest(data))


# A concise alias for callers that use this module as their config boundary.
load_config = load_pr_config
