from __future__ import annotations

import re
from typing import Any

from scripts.reproduction.decision import success_criteria_issues

OPERATIONAL_CRITERIA_MARKERS = {
    "environment", "env", "setup", "conda", "cuda", "blackwell", "data", "dataset",
    "download", "smoke", "import", "verify", "checkpoint", "training", "train",
}
METRIC_TARGET_KEYS = ("value", "target", "paper_value")
ENVIRONMENT_GATE_NOTE = "Environment/data/import/smoke success criteria support the environment handoff only; they cannot approve paper claims or full reproduction metrics."


def _command_phase(row: Any) -> str:
    if not isinstance(row, dict) or row.get("required") is False:
        return ""
    return str(row.get("phase") or "").strip().lower()


def _required_command_phases(plan: dict[str, Any]) -> list[str]:
    rows = plan.get("commands") if isinstance(plan.get("commands"), list) else []
    return [phase for phase in (_command_phase(row) for row in rows) if phase]


def _phase_has_any(phases: list[str], markers: set[str]) -> bool:
    return any(any(marker in phase for marker in markers) for phase in phases)


def _environment_gate_criterion(name: str, source: str, description: str) -> dict[str, Any]:
    return {
        "name": name,
        "operator": ">=",
        "value": 1,
        "source": source,
        "description": description,
        "approval_scope": "environment_gate",
        "paper_metric": False,
        "non_paper_approval_note": ENVIRONMENT_GATE_NOTE,
    }


def _fallback_environment_gate_criteria(plan: dict[str, Any]) -> list[dict[str, Any]]:
    phases = _required_command_phases(plan)
    if not phases:
        return []
    criteria: list[dict[str, Any]] = []
    if _phase_has_any(phases, {"conda", "install", "dependency", "deps", "env"}) and _phase_has_any(phases, {"verify", "import", "smoke", "test", "eval", "benchmark", "reproduce"}):
        criteria.append(_environment_gate_criterion(
            "conda_environment_ready",
            "environment handoff: required Conda setup and verification commands",
            "Run-local Conda setup plus import/runtime verification must succeed before experimenting can consume this environment.",
        ))
    if _phase_has_any(phases, {"verify", "import", "smoke", "model", "loader", "test"}):
        criteria.append(_environment_gate_criterion(
            "runtime_smoke_ready",
            "environment handoff: required import/model/loader smoke commands",
            "Required import, model, loader, or smoke-test commands must succeed; this does not validate paper metrics.",
        ))
    if _phase_has_any(phases, {"data", "dataset", "download", "preprocess", "clone"}):
        criteria.append(_environment_gate_criterion(
            "data_runtime_ready",
            "environment handoff: required data/repository preparation commands",
            "Required data, benchmark, or auxiliary repository preparation commands must succeed with run-local artifacts where applicable.",
        ))
    criteria.append(_environment_gate_criterion(
        "required_environment_commands_ready",
        "environment handoff: all non-full-reproduction required commands",
        "All required environment commands except downstream full reproduction must succeed before the handoff can be marked ready.",
    ))
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in criteria:
        name = str(row.get("name") or "")
        if name in seen:
            continue
        seen.add(name)
        deduped.append(row)
    return deduped




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
        criterion = dict(row)
        if _is_operational_criterion(row):
            criterion.setdefault("approval_scope", "environment_gate")
            criterion.setdefault("paper_metric", False)
            criterion.setdefault("non_paper_approval_note", ENVIRONMENT_GATE_NOTE)
        else:
            criterion.setdefault("approval_scope", "paper_metric")
            criterion.setdefault("paper_metric", True)
        kept.append(criterion)

    fallback_used = False
    environment_fallback_used = False
    if not kept and isinstance(paper_evidence, dict):
        for target in paper_evidence.get("target_metrics") if isinstance(paper_evidence.get("target_metrics"), list) else []:
            if not isinstance(target, dict):
                continue
            criterion = _paper_target_to_criterion(target)
            if criterion and not success_criteria_issues([criterion]):
                kept.append(criterion)
        fallback_used = bool(kept)
    if not kept:
        kept = _fallback_environment_gate_criteria(plan)
        environment_fallback_used = bool(kept)

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
            "fallback_from_environment_handoff_commands": environment_fallback_used,
            "policy": "success_criteria preserves numeric environment/data/import/smoke gates as approval_scope=environment_gate while reserving paper_metric criteria for paper/full-reproduction approval.",
            "dropped": dropped[:20],
        },
    ]
    return normalized
