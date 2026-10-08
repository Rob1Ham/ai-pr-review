"""Versioned qualitative severity estimates for standalone issue filing."""

from .common import CampaignError


RUBRIC_VERSION = "impact-likelihood-v1"
MATRIX = {
    "critical": {"high": "critical", "medium": "high", "low": "high"},
    "high": {"high": "high", "medium": "high", "low": "medium"},
    "medium": {"high": "medium", "medium": "medium", "low": "low"},
    "low": {"high": "low", "medium": "low", "low": "low"},
    "info": {"high": "info", "medium": "info", "low": "info"},
}
RUBRIC = {
    "version": RUBRIC_VERSION,
    "impact": {
        "critical": "Plausible irreversible loss of customer funds, broad signing compromise, or catastrophic systemic loss.",
        "high": "Major authorization/integrity failure, sensitive-data exposure, or prolonged critical-service outage.",
        "medium": "Material but bounded correctness, reliability, or availability failure with a viable recovery path.",
        "low": "Limited edge-case, development, testing, or maintenance impact.",
        "info": "Observation with no established operational or security harm.",
    },
    "likelihood": {
        "high": "Reachable through ordinary use or common conditions demonstrated by the supplied code.",
        "medium": "Credible supported workflow with additional, explicit preconditions.",
        "low": "Uncommon or strongly constrained prerequisites; state the assumptions and uncertainty.",
    },
    "confidence": "Confidence in the severity assessment, separate from confidence that the defect exists.",
    "matrix": MATRIX,
    "limits": "Qualitative triage estimate, not CVSS or proof of exploitation. Do not execute or reproduce vulnerabilities.",
}
ASSESSMENT_SCHEMA = {"impact": "critical|high|medium|low|info", "likelihood": "high|medium|low",
                     "confidence": "high|medium|low", "rationale": "evidence-based impact and reachability rationale",
                     "assumptions": ["explicit assumption or missing deployment information"]}


def estimate_severity(finding, *, required=False):
    if not isinstance(finding, dict): raise CampaignError("severity assessment requires a finding object")
    proposed = finding.get("severity")
    if not isinstance(proposed, str) or proposed not in MATRIX:
        raise CampaignError("finding severity must be critical, high, medium, low, or info")
    assessment = finding.get("severity_assessment")
    if assessment is None:
        if required: raise CampaignError("finding requires a structured severity assessment")
        return {"rubric_version": RUBRIC_VERSION, "complete": False, "severity": None,
                "model_proposed_severity": proposed, "impact": "unassessed", "likelihood": "unassessed",
                "confidence": "unassessed", "rationale": "Legacy finding requires impact/likelihood assessment before issue filing.",
                "assumptions": []}
    if not isinstance(assessment, dict) or set(assessment) != set(ASSESSMENT_SCHEMA):
        raise CampaignError("severity assessment requires impact, likelihood, confidence, rationale, and assumptions")
    if (not isinstance(assessment["impact"], str) or assessment["impact"] not in MATRIX
            or not isinstance(assessment["likelihood"], str) or assessment["likelihood"] not in {"high", "medium", "low"}):
        raise CampaignError("invalid severity impact or likelihood")
    if not isinstance(assessment["confidence"], str) or assessment["confidence"] not in {"high", "medium", "low"}:
        raise CampaignError("invalid severity assessment confidence")
    rationale = assessment["rationale"]
    assumptions = assessment["assumptions"]
    if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 4000:
        raise CampaignError("severity rationale must be bounded nonempty text")
    if (not isinstance(assumptions, list) or len(assumptions) > 16
            or any(not isinstance(item, str) or not item.strip() or len(item) > 1000 for item in assumptions)):
        raise CampaignError("severity assumptions must be a bounded string list")
    return {"rubric_version": RUBRIC_VERSION, "complete": True,
            "severity": MATRIX[assessment["impact"]][assessment["likelihood"]],
            "model_proposed_severity": proposed, **assessment}


def render_estimate(estimate):
    return ["## Estimated severity", "",
            f"- Assigned severity: **{estimate['severity'] or 'unassessed'}**",
            f"- Model-proposed severity: **{estimate['model_proposed_severity']}**",
            f"- Impact: **{estimate['impact']}**", f"- Likelihood: **{estimate['likelihood']}**",
            f"- Assessment confidence: **{estimate['confidence']}**",
            f"- Rubric: `{estimate['rubric_version']}`", "",
            estimate["rationale"], "", "### Assessment assumptions",
            *([f"- {item}" for item in estimate["assumptions"]] or ["- No additional assumptions recorded."]), ""]
