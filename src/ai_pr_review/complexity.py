"""Deterministic, campaign-configured review tier selection."""

from collections import Counter
from pathlib import PurePosixPath
import re

from .common import CampaignError, digest


def budget_version(config) -> str:
    return digest(config.tiers)


def classify(scope: dict, config) -> dict:
    paths, size = scope.get("paths", []), scope.get("diff_bytes", 0)
    sensitive = re.compile(config.data["risk_patterns"]["sensitive"], re.I)
    critical = re.compile(config.data["risk_patterns"]["critical"], re.I)
    production_roots = {path.split("/")[0] for path in paths if "/src/" in path and "/test" not in path}
    docs_only = bool(paths) and all(PurePosixPath(path).suffix.lower() in {".md", ".txt", ".rst"} for path in paths)
    normative = any(path.startswith(("docs/specs/", "docs/routes/")) for path in paths)
    if scope.get("stage") == "whole_range": tier, reason = "deep", "whole-range interaction review"
    elif scope.get("lineage"): tier, reason = "deep", "consolidated PR with source and integration changes"
    elif docs_only and not normative and len(paths) <= 5 and size <= 80000: tier, reason = "light", "bounded documentation change"
    elif any(critical.search(path) for path in paths): tier, reason = "deep", "critical enforcement boundary"
    elif len(paths) > 18 or size > 160000 or len(production_roots) >= 3: tier, reason = "deep", "large or cross-component change"
    elif len(paths) <= 4 and size <= 16000 and not any(sensitive.search(path) for path in paths) and not normative:
        tier, reason = "light", "small localized low-risk change"
    else: tier, reason = "standard", "bounded change requiring caller context"
    return {"tier": tier, "reason": reason, "files": len(paths), "diff_bytes": size}


def budget(tier: str, config) -> dict:
    if tier not in config.tiers: raise CampaignError(f"unknown review tier: {tier}")
    return {key: value for key, value in config.tiers[tier].items() if key not in {"diff_bytes", "units"}}


def tier_summary(manifest: dict, config) -> dict:
    return dict(Counter(classify(scope, config)["tier"] for scope in manifest["scopes"] if scope["stage"] == "pr"))
