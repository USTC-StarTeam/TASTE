from __future__ import annotations

import re
from typing import Any

from scripts.reproduction.decision import success_criteria_issues

OPERATIONAL_CRITERIA_MARKERS = {
    "environment", "env", "setup", "conda", "cuda", "blackwell", "data", "dataset",
    "download", "smoke", "import", "verify", "checkpoint", "training", "train",
}
METRIC_TARGET_KEYS = ("value", "target", "paper_value")


def _criterion_name(row: dict[str, Any]) -> str:
    return str(row.get("name") or row.get("metric") or "").strip()


def _criterion_value(row: dict[str, Any]) -> Any:
    for key in METRIC_TARGET_KEYS:
        if key in row:
            return row.get(key)
    return None


def _is_operational_criterion(row: dict[str, Any]) -> bool:
    name = _criterion_name(row).lower()
    source = " ".join(str(row.get(key) or "") for key in ["source", "paper_source", "evidence_source", "description"]).lower()
    text = f"{name} {source}"
    if name in {"designability_target", "designability_improvement", "motif_scaffolding_success_improvement"}:
        return False
    return any(marker in text for marker in OPERATIONAL_CRITERIA_MARKERS)


def _paper_target_to_criterion(row: dict[str, Any]) -> dict[str, Any] | None:
    name = str(row.get("name") or row.get("metric") or row.get("metric_name") or "").strip()
    if not name:
        return None
    value_found = False
    value: Any = None
    for key in METRIC_TARGET_KEYS:
        if key in row and row.get(key) not in {None, ""}:
            value = row.get(key)
            value_found = True
            break
    if not value_found:
        return None
    criterion = {
        "name": name,
        "operator": str(row.get("operator") or row.get("op") or ">=").strip() or ">=",
        "value": value,
        "source": str(row.get("source") or row.get("paper_source") or row.get("evidence_source") or "paper_evidence.target_metrics").strip() or "paper_evidence.target_metrics",
    }
    description = str(row.get("description") or "").strip()
    if description:
        criterion["description"] = description
    return criterion


def normalize_success_criteria(plan: dict[str, Any], paper_evidence: dict[str, Any] | None = None, policy_version: str = "") -> dict[str, Any]:
    if not isinstance(plan, dict):
        return plan
    rows = plan.get("success_criteria") if isinstance(plan.get("success_criteria"), list) else []
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            dropped.append({"index": index, "reason": "success_criteria row is not an object", "row": row})
            continue
        schema_issues = success_criteria_issues([row])
        if schema_issues:
            dropped.append({"index": index, "name": _criterion_name(row), "reason": "non_numeric_or_invalid_success_criterion", "issues": schema_issues, "row": row})
            continue
        if _is_operational_criterion(row):
            dropped.append({"index": index, "name": _criterion_name(row), "reason": "operational_gate_moved_to_backend_approval_gate", "row": row})
            continue
        kept.append(dict(row))

    fallback_used = False
    if not kept and isinstance(paper_evidence, dict):
        for target in paper_evidence.get("target_metrics") if isinstance(paper_evidence.get("target_metrics"), list) else []:
            if not isinstance(target, dict):
                continue
            criterion = _paper_target_to_criterion(target)
            if criterion and not success_criteria_issues([criterion]):
                kept.append(criterion)
        fallback_used = bool(kept)

    if kept == rows and not dropped:
        return plan
    normalized = dict(plan)
    normalized["success_criteria"] = kept
    prior = normalized.get("success_criteria_policy_rewrites") if isinstance(normalized.get("success_criteria_policy_rewrites"), list) else []
    normalized["success_criteria_policy_rewrites"] = [
        *prior,
        {
            "policy_version": policy_version,
            "dropped_count": len(dropped),
            "kept_count": len(kept),
            "fallback_from_paper_evidence": fallback_used,
            "policy": "success_criteria only carries numeric paper/result metrics; environment setup, CUDA, data download, smoke/import checks are enforced by dedicated backend approval gates.",
            "dropped": dropped[:20],
        },
    ]
    return normalized
