"""Strict versioned campaign configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from .common import CampaignError, digest, read_json


CONFIG_VERSION = 1
_TOP = {"version", "name", "target", "lanes", "tier_budgets", "risk_patterns",
        "review_checklist", "source_lineage", "expected"}
_TARGET = {"repository", "branch", "base_ref", "head_ref", "base_sha", "head_sha"}
_LANE = {"key", "provider", "model", "effort"}
_BUDGET = {"timeout_s", "no_progress_s", "steps", "output_tokens", "max_prompt_bytes",
           "max_output_bytes", "diff_bytes", "units"}
_RISK = {"sensitive", "critical"}
_LINEAGE = {"landing_pr", "source_prs"}
_EXPECTED = {"prs", "commits", "baseline_calls", "tier_counts"}
_SHA = re.compile(r"[0-9a-f]{40,64}")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class CampaignConfig:
    path: Path
    data: dict
    digest: str

    @property
    def target(self): return self.data["target"]
    @property
    def lanes(self): return tuple(self.data["lanes"])
    @property
    def tiers(self): return self.data["tier_budgets"]
    @property
    def checklist(self): return tuple(self.data["review_checklist"])
    @property
    def lineage(self): return {item["landing_pr"]: tuple(item["source_prs"])
                               for item in self.data["source_lineage"]}


def _keys(value, allowed, name):
    if not isinstance(value, dict):
        raise CampaignError(f"{name} must be an object")
    unknown = set(value) - allowed
    if unknown:
        raise CampaignError(f"unknown {name} keys: {', '.join(sorted(unknown))}")


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or "\0" in value or "\n" in value:
        raise CampaignError(f"{name} must be a nonempty single-line string")


def load_config(path: str | Path) -> CampaignConfig:
    source = Path(path).resolve()
    data = read_json(source)
    _keys(data, _TOP, "config")
    if set(data) != _TOP or data.get("version") != CONFIG_VERSION:
        raise CampaignError("config requires every version 1 field")
    _text(data["name"], "name")
    target = data["target"]
    _keys(target, _TARGET, "target")
    if set(target) != _TARGET:
        raise CampaignError("target requires repository, branch, refs, and pinned SHA fields")
    if not isinstance(target["repository"], str) or not _REPOSITORY.fullmatch(target["repository"]):
        raise CampaignError("target.repository must be owner/name")
    for key in ("branch", "base_ref", "head_ref"):
        _text(target[key], f"target.{key}")
    for key in ("base_sha", "head_sha"):
        if target[key] is not None and (not isinstance(target[key], str) or not _SHA.fullmatch(target[key])):
            raise CampaignError(f"target.{key} must be null or a full commit SHA")
    lanes = data["lanes"]
    if not isinstance(lanes, list) or not 1 <= len(lanes) <= 4:
        raise CampaignError("config requires one to four lane objects")
    seen = set()
    for lane in lanes:
        _keys(lane, _LANE, "lane")
        if set(lane) != _LANE:
            raise CampaignError("each lane requires key, provider, model, and effort")
        for key in ("key", "provider", "model", "effort"):
            _text(lane[key], f"lane.{key}")
        if lane["key"] in seen:
            raise CampaignError(f"duplicate lane: {lane['key']}")
        seen.add(lane["key"])
        if lane["effort"] != "max" or not lane["model"].startswith(lane["provider"] + "/"):
            raise CampaignError("every lane must use its provider's exact model at max effort")
    tiers = data["tier_budgets"]
    _keys(tiers, {"light", "standard", "deep"}, "tier_budgets")
    if set(tiers) != {"light", "standard", "deep"}:
        raise CampaignError("tier_budgets must define light, standard, and deep")
    for tier, values in tiers.items():
        _keys(values, _BUDGET, f"tier {tier}")
        if set(values) != _BUDGET or any(type(value) is not int or value <= 0 for value in values.values()):
            raise CampaignError(f"tier {tier} requires positive integer budget fields")
    risk = data["risk_patterns"]
    _keys(risk, _RISK, "risk_patterns")
    if set(risk) != _RISK:
        raise CampaignError("risk_patterns requires sensitive and critical")
    for key, pattern in risk.items():
        _text(pattern, f"risk_patterns.{key}")
        try: re.compile(pattern)
        except re.error as exc: raise CampaignError(f"invalid {key} regex") from exc
    checklist = data["review_checklist"]
    if not isinstance(checklist, list) or not checklist or any(not isinstance(x, str) or not x.strip() for x in checklist):
        raise CampaignError("review_checklist must be a nonempty string list")
    lineage = data["source_lineage"]
    if not isinstance(lineage, list):
        raise CampaignError("source_lineage must be a list")
    landings = set()
    for item in lineage:
        _keys(item, _LINEAGE, "source_lineage entry")
        if set(item) != _LINEAGE or type(item["landing_pr"]) is not int or item["landing_pr"] <= 0:
            raise CampaignError("lineage landing_pr must be a positive integer")
        sources = item["source_prs"]
        if (item["landing_pr"] in landings or not isinstance(sources, list) or not sources
                or any(type(x) is not int or x <= 0 for x in sources) or len(sources) != len(set(sources))):
            raise CampaignError("lineage declarations must have unique landings and source PRs")
        landings.add(item["landing_pr"])
    expected = data["expected"]
    _keys(expected, _EXPECTED, "expected")
    if set(expected) != _EXPECTED or any(type(expected[k]) is not int or expected[k] <= 0
                                         for k in ("prs", "commits", "baseline_calls")):
        raise CampaignError("expected counts must be positive integers")
    counts = expected["tier_counts"]
    _keys(counts, {"light", "standard", "deep"}, "expected.tier_counts")
    if set(counts) != {"light", "standard", "deep"} or any(type(x) is not int or x < 0 for x in counts.values()):
        raise CampaignError("expected tier counts must be non-negative integers")
    if sum(counts.values()) != expected["prs"]:
        raise CampaignError("expected tier counts must exactly cover expected PRs")
    if expected["baseline_calls"] != (expected["prs"] + 1) * len(lanes):
        raise CampaignError("expected baseline_calls must exactly equal (PRs + range) x lanes")
    return CampaignConfig(source, data, digest(data))
