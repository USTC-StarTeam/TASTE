#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

from project_paths import ROOT, build_paths


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)




def rel_from_root(path: Path) -> str:
    return str(path.resolve().relative_to(ROOT))


def in_project(paths, path: Path) -> bool:
    try:
        resolved = path.resolve()
        project_root = paths.root.resolve()
        return resolved == project_root or str(resolved).startswith(str(project_root) + os.sep)
    except Exception:
        return False
def norm_path(value: Any) -> str:
    return str(value or "").rstrip("/")


def route_identity(payload: Any) -> dict[str, str]:
    row = payload if isinstance(payload, dict) else {}
    selected = row.get("selected") if isinstance(row.get("selected"), dict) else {}
    route = row.get("candidate_route") if isinstance(row.get("candidate_route"), dict) else {}
    new_route = row.get("new_route") if isinstance(row.get("new_route"), dict) else {}
    merged = {**row, **selected, **route, **new_route}
    return {
        "name": str(merged.get("name") or merged.get("repo") or merged.get("repo_name") or "").strip(),
        "title": str(merged.get("title") or merged.get("paper_title") or merged.get("base_title") or merged.get("literature_base_title") or "").strip(),
        "repo_path": norm_path(merged.get("repo_path") or merged.get("local_path") or merged.get("path")),
        "dataset": str(merged.get("dataset") or merged.get("claim_ready_dataset") or "").strip(),
    }


def current_route(paths) -> dict[str, str]:
    for name in ["evidence_ready_repo_selection.json", "active_repo.json", "fresh_base_implementation_plan.json"]:
        payload = load_json(paths.state / name, {})
        identity = route_identity(payload)
        if identity.get("repo_path") or identity.get("name") or identity.get("title"):
            identity["source"] = f"state/{name}"
            return identity
    return {"name": "", "title": "", "repo_path": "", "dataset": "", "source": ""}


def deterministic_switch_authorized(paths) -> bool:
    gate = load_json(paths.state / "base_switch_gate.json", {})
    execution = load_json(paths.state / "base_switch_execution.json", {})
    return bool(
        isinstance(gate, dict)
        and gate.get("status") == "pass"
        and gate.get("decision") == "authorize_base_switch"
        and gate.get("switch_authorized") is True
        and isinstance(execution, dict)
        and str(execution.get("status") or "").startswith("authorized_by_deterministic_base_switch_gate")
    )


def project_agent_cleanup_authorization(paths) -> tuple[bool, str, dict[str, Any]]:
    """Return project-agent cleanup authorization, if the project wrote one.

    framework code may enumerate obsolete-route candidates and enforce path
    protections. It must not authorize project-file cleanup merely because a
    current route exists; a project-scoped agent review must explicitly approve
    each candidate path, or a deterministic base-switch gate must authorize the
    route change.
    """
    auth = load_json(paths.state / "obsolete_baseline_cleanup_authorization.json", {})
    if not isinstance(auth, dict) or not auth:
        return False, "missing_project_agent_cleanup_authorization", {}
    status = str(auth.get("status") or "").strip()
    allowed_status = {
        "authorized_by_project_agent_review",
        "authorized_by_project_claude_review",
        "project_agent_cleanup_authorized",
    }
    if status not in allowed_status:
        return False, f"authorization_status_not_allowed:{status or 'empty'}", auth
    if auth.get("cleanup_authorized") is not True:
        return False, "cleanup_authorized_not_true", auth
    if auth.get("current_route_reviewed") is not True or auth.get("protected_current_route") is not True:
        return False, "current_route_or_protection_not_reviewed", auth
    approved = auth.get("approved_candidate_paths")
    if not isinstance(approved, list) or not any(str(item).strip() for item in approved):
        return False, "no_approved_candidate_paths", auth
    reviewer = " ".join(str(auth.get(key) or "") for key in ["authorized_by", "agent_id", "backend", "reviewer"]).lower()
    if reviewer and not any(marker in reviewer for marker in ["project_agent", "claude", "claude_code", "agent"]):
        return False, "authorization_reviewer_not_project_agent", auth
    return True, "project_agent_cleanup_authorization", auth


def filter_authorized_candidates(candidates: list[dict[str, Any]], auth: dict[str, Any]) -> list[dict[str, Any]]:
    approved = {
        str(item.get("path") if isinstance(item, dict) else item).strip()
        for item in (auth.get("approved_candidate_paths") or [])
        if str(item.get("path") if isinstance(item, dict) else item).strip()
    }
    protected = {
        str(item.get("path") if isinstance(item, dict) else item).strip()
        for item in (auth.get("protected_paths") or [])
        if str(item.get("path") if isinstance(item, dict) else item).strip()
    }
    return [row for row in candidates if str(row.get("path") or "") in approved and str(row.get("path") or "") not in protected]




def review_candidate_fingerprint(candidates: list[dict[str, Any]]) -> str:
    rows = sorted(str(row.get("path") or "") for row in candidates if isinstance(row, dict) and row.get("path"))
    return "\n".join(rows)
def project_agent_no_cleanup_review(paths, candidates: list[dict[str, Any]]) -> tuple[bool, str, dict[str, Any]]:
    """Return a project-agent review that explicitly keeps cleanup as a no-op."""
    review = load_json(paths.state / "obsolete_baseline_cleanup_review.json", {})
    if not isinstance(review, dict) or not review:
        return False, "missing_project_agent_cleanup_review", {}
    if review.get("cleanup_authorized") is not False:
        return False, "review_does_not_decline_cleanup", review
    if review.get("current_route_reviewed") is not True or review.get("protected_current_route") is not True:
        return False, "review_did_not_confirm_current_route_protection", review
    reviewer = " ".join(str(review.get(key) or "") for key in ["review_type", "reviewer", "agent_id", "backend"]).lower()
    if reviewer and not any(marker in reviewer for marker in ["project_agent", "claude", "claude_code", "agent"]):
        return False, "reviewer_not_project_agent", review
    current_fingerprint = review_candidate_fingerprint(candidates)
    reviewed_fingerprint = str(review.get("candidate_fingerprint") or "")
    reviewed_count = review.get("reviewed_candidate_count")
    if candidates:
        if not reviewed_fingerprint or reviewed_fingerprint != current_fingerprint:
            return False, "review_stale_candidate_set_changed", review
        if reviewed_count is not None:
            try:
                if int(reviewed_count) != len(candidates):
                    return False, "review_stale_candidate_count_changed", review
            except Exception:
                return False, "review_stale_candidate_count_invalid", review
    rationale = str(review.get("rationale") or review.get("reason") or "").strip()
    if review.get("blocked_no_candidates") is True and not candidates:
        return True, "project_agent_review_no_cleanup_required", review
    if review.get("decision") in {"project_review_keeps_files", "keep_project_evidence"} and reviewed_fingerprint == current_fingerprint:
        return True, "project_agent_review_keeps_current_candidate_set", review
    if candidates and reviewed_fingerprint == current_fingerprint and rationale:
        return True, "project_agent_review_keeps_current_candidate_set", review
    return False, "review_has_unhandled_candidate_paths", review


def project_agent_cleanup_execution(paths, candidates: list[dict[str, Any]]) -> tuple[bool, str, dict[str, Any]]:
    """Return a project-agent cleanup execution receipt, if the project wrote one.

    TASTE may enumerate candidates and verify protections, but project-file cleanup
    itself must be performed by the project-scoped Claude Code session.
    """
    receipt = load_json(paths.state / "obsolete_baseline_cleanup_execution.json", {})
    if not isinstance(receipt, dict) or not receipt:
        return False, "missing_project_agent_cleanup_execution_receipt", {}
    status = str(receipt.get("status") or "").strip()
    allowed_status = {
        "completed_by_project_claude",
        "project_claude_cleanup_completed",
        "completed_by_project_agent",
        "project_agent_cleanup_completed",
    }
    if status not in allowed_status:
        return False, f"execution_status_not_allowed:{status or empty}", receipt
    if receipt.get("cleanup_executed") is not True:
        return False, "cleanup_executed_not_true", receipt
    if receipt.get("cleanup_authorized") is not True:
        return False, "cleanup_authorized_not_true", receipt
    if receipt.get("current_route_reviewed") is not True or receipt.get("protected_current_route") is not True:
        return False, "current_route_or_protection_not_reviewed", receipt
    executor = " ".join(str(receipt.get(key) or "") for key in ["executed_by", "agent_id", "backend", "reviewer"]).lower()
    if executor and not any(marker in executor for marker in ["project_agent", "claude", "claude_code", "agent"]):
        return False, "execution_reviewer_not_project_agent", receipt
    remaining = receipt.get("remaining_candidate_paths")
    if isinstance(remaining, list) and any(str(item.get("path") if isinstance(item, dict) else item).strip() for item in remaining):
        return False, "execution_receipt_reports_remaining_candidates", receipt
    if candidates:
        return False, "cleanup_execution_incomplete_current_candidates_remain", receipt
    applied = receipt.get("applied_paths") or receipt.get("approved_candidate_paths") or []
    if not isinstance(applied, list) or not applied:
        return False, "execution_receipt_has_no_applied_or_approved_paths", receipt
    rationale = str(receipt.get("rationale") or receipt.get("reason") or "").strip()
    if not rationale:
        return False, "execution_receipt_missing_rationale", receipt
    return True, "project_agent_cleanup_execution_completed", receipt


def cleanup_authorized(paths, current: dict[str, str], candidates: list[dict[str, Any]]) -> tuple[bool, str, dict[str, Any], list[dict[str, Any]]]:
    # Project-route cleanup is a project-context decision. A base-switch gate can
    # explain why stale paths need review, but it must not authorize cleanup by
    # itself. The project Claude Code review must approve exact candidate paths.
    ok, reason, auth = project_agent_cleanup_authorization(paths)
    if ok:
        filtered = filter_authorized_candidates(candidates, auth)
        if filtered:
            return True, reason, auth, filtered
        return False, "project_agent_authorization_has_no_matching_candidates", auth, []
    return False, reason, auth, []


def _path_row(path: Path, reason: str) -> dict[str, Any]:
    return {"path": str(path.relative_to(ROOT)), "is_dir": path.is_dir(), "reason": reason}


def _add_candidate(candidates: list[dict[str, Any]], seen: set[str], path: Path, reason: str) -> None:
    if not path.exists():
        return
    rel = str(path.resolve().relative_to(ROOT))
    if rel in seen:
        return
    seen.add(rel)
    candidates.append({"path": rel, "is_dir": path.is_dir(), "reason": reason})




def _registry_rows(paths) -> list[dict[str, Any]]:
    payload = load_json(paths.state / "experiment_registry.json", [])
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ["experiments", "records", "rows"]:
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    return []


def _row_route_path(row: dict[str, Any]) -> str:
    for key in ["repo_path", "active_repo_path", "selected_repo_path", "local_path"]:
        value = norm_path(row.get(key))
        if value:
            return value
    return ""


def _row_artifact_paths(row: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ["artifact_path", "artifact_dir", "output_dir", "run_dir", "log_path", "audit_path", "bad_case_path", "config_path"]:
        value = norm_path(row.get(key))
        if value:
            values.append(value)
    return values


def add_registry_provenance_candidates(paths, current: dict[str, str], candidates: list[dict[str, Any]], seen: set[str], keep: list[dict[str, Any]]) -> None:
    current_repo = norm_path(current.get("repo_path"))
    current_paths: set[str] = set()
    non_current_paths: set[str] = set()
    for row in _registry_rows(paths):
        route_path = _row_route_path(row)
        artifact_values = _row_artifact_paths(row)
        is_current = bool(current_repo and route_path and (route_path == current_repo or route_path.startswith(current_repo + "/")))
        target = current_paths if is_current else non_current_paths if route_path else set()
        for value in artifact_values:
            target.add(value)
    for value in sorted(current_paths):
        path = Path(value).expanduser()
        if not path.exists() or not in_project(paths, path):
            continue
        keep.append({"path": rel_from_root(path), "is_dir": path.is_dir(), "reason": "current_route_registry_evidence"})
    for value in sorted(non_current_paths):
        path = Path(value).expanduser()
        if not path.exists() or not in_project(paths, path):
            continue
        _add_candidate(candidates, seen, path, "non_current_route_registry_evidence")


def prune_nested_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(candidates, key=lambda row: (len(str(row.get("path") or "").split("/")), str(row.get("path") or "")))
    kept: list[dict[str, Any]] = []
    kept_paths: list[str] = []
    for row in ordered:
        rel = str(row.get("path") or "")
        if not rel:
            continue
        if any(rel == parent or rel.startswith(parent + "/") for parent in kept_paths):
            continue
        kept.append(row)
        kept_paths.append(rel)
    return kept
def collect_route_artifacts(paths, current: dict[str, str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    current_repo = norm_path(current.get("repo_path"))
    keep: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    seen_candidates: set[str] = set()
    protected_state = {
        "active_repo.json",
        "evidence_ready_repo_selection.json",
        "reference_reproduction_gate.json",
        "scientific_progress_gate.json",
        "experiment_registry.json",
        "experiment_record_table.json",
        "blocker_action_plan.json",
        "base_switch_gate.json",
        "base_switch_execution.json",
        "obsolete_baseline_cleanup_plan.json",
        "obsolete_baseline_cleanup_execution.json",
        "full_research_cycle.json",
    }
    protected_reports = {
        "reference_reproduction_gate.md",
        "experiment_iteration_audit.md",
        "paper_evidence_audit.md",
        "submission_readiness.md",
        "blocker_action_plan.md",
    }
    current_repo_name = Path(current_repo).name if current_repo else ""
    selected_roots = [paths.repos_selected, paths.root / "obsidian" / "repos" / "selected"]
    for root in selected_roots:
        if not root.exists():
            continue
        for path in root.iterdir():
            resolved = norm_path(path.resolve())
            same_current_path = bool(current_repo and (resolved == current_repo or resolved.startswith(current_repo + "/")))
            same_current_mirror = bool(current_repo_name and path.name == current_repo_name)
            if same_current_path or same_current_mirror:
                keep.append(_path_row(path, "current_selected_route"))
            else:
                _add_candidate(candidates, seen_candidates, path, "non_current_selected_repo")
    for root, reason in [
        (paths.artifacts, "non_current_route_artifact"),
        (paths.state, "non_current_route_state"),
        (paths.reports, "non_current_route_report"),
        (paths.root / "obsidian" / "reports", "non_current_route_obsidian_report"),
    ]:
        if not root.exists():
            continue
        for path in root.iterdir():
            if path.parent == paths.state and path.name in protected_state:
                keep.append(_path_row(path, "shared_audit_state"))
                continue
            if path.parent in {paths.reports, paths.root / "obsidian" / "reports"} and path.name in protected_reports:
                keep.append(_path_row(path, "shared_audit_report"))
                continue
            name = path.name.lower()
            legacy_state_signal = any(token in name for token in ("reference_reproduction", "loader_contract", "reference_protocol", "route_switch_proposal", "failure_analysis")) and not name.startswith("fresh_base_reference")
            if legacy_state_signal:
                _add_candidate(candidates, seen_candidates, path, reason)
                continue
            if root == paths.artifacts and any((path / marker).exists() for marker in ["legacy_route.json", "obsolete_route.json"]):
                _add_candidate(candidates, seen_candidates, path, reason)
    scripts_root = paths.root / "scripts"
    if scripts_root.exists():
        for path in scripts_root.iterdir():
            if path.is_file() and any(token in path.name.lower() for token in ("parse_", "run_", "probe_", "badcase", "popularity_analysis")):
                _add_candidate(candidates, seen_candidates, path, "project_local_legacy_route_script")
        legacy_dir = scripts_root / "legacy"
        if legacy_dir.exists():
            _add_candidate(candidates, seen_candidates, legacy_dir, "project_local_legacy_script_dir")
        pycache = scripts_root / "__pycache__"
        if pycache.exists():
            _add_candidate(candidates, seen_candidates, pycache, "project_local_generated_cache")
    add_registry_provenance_candidates(paths, current, candidates, seen_candidates, keep)
    return keep, prune_nested_candidates(candidates)

def write_report(paths, payload: dict[str, Any]) -> Path:
    out = paths.reports / "obsolete_baseline_cleanup_plan.md"
    lines = [
        "# Obsolete Baseline Cleanup Plan\n\n",
        f"- status: {payload.get('status')}\n",
        f"- cleanup_authorized: {payload.get('cleanup_authorized')}\n",
        f"- decision: {payload.get('decision')}\n",
        "- policy: framework only enumerates cleanup candidates and enforces protections. Cleanup requires project Claude Code authorization that approves exact candidate paths. Project evidence is never deleted by name matching alone.\n",
        f"- project_review_status: {payload.get('project_review_status', '')}\n",
        f"- protected_current_route: {payload.get('protected_current_route')}\n",
        f"- project_reviewed_candidate_count: {payload.get('project_reviewed_candidate_count', '')}\n",
        f"- project_review_rationale: {payload.get('project_review_rationale', '')}\n\n",
        "## Current Route\n\n",
        json.dumps(payload.get("current_route", {}), ensure_ascii=False, indent=2),
        "\n\n## Candidate Cleanup Paths\n\n",
    ]
    display_rows = payload.get("candidate_cleanup_paths", []) or payload.get("blocked_candidate_paths", [])
    for row in display_rows[:120]:
        lines.append(f"- {row.get('path')} ({row.get('reason')})\n")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(lines), encoding="utf-8")
    return out


def build(project: str) -> dict[str, Any]:
    paths = build_paths(project)
    gate = load_json(paths.state / "base_switch_gate.json", {})
    execution = load_json(paths.state / "base_switch_execution.json", {})
    current = current_route(paths)
    keep, candidates = collect_route_artifacts(paths, current)
    authorized, authorization_reason, project_auth, authorized_candidates = cleanup_authorized(paths, current, candidates)
    execution_ok, execution_reason, project_execution = project_agent_cleanup_execution(paths, candidates)
    review_ok, review_reason, project_review = project_agent_no_cleanup_review(paths, candidates)
    reviewed_no_cleanup = bool(not authorized and not execution_ok and review_ok)
    if execution_ok:
        status = "project_cleanup_completed"
        decision = "project_claude_cleanup_completed"
    elif authorized:
        status = "pending_project_claude_cleanup_execution"
        decision = "project_claude_cleanup_execution_required"
    elif reviewed_no_cleanup:
        status = "reviewed_no_cleanup_required"
        decision = "project_review_keeps_files"
    else:
        status = "blocked_pending_project_review" if candidates else "blocked_not_authorized"
        decision = "project_claude_review_required" if candidates else "do_not_delete_or_archive_project_files"
    if execution_ok:
        review_source = project_execution
    elif reviewed_no_cleanup:
        review_source = project_review
    elif authorized:
        review_source = project_auth
    else:
        review_source = {}
    payload = {
        "project": project,
        "generated_at": now_iso(),
        "status": status,
        "decision": decision,
        "cleanup_authorized": bool(authorized or execution_ok),
        "authorization_reason": authorization_reason if authorized else execution_reason if execution_ok else authorization_reason,
        "project_agent_cleanup_authorization": project_auth,
        "project_agent_cleanup_execution": project_execution if execution_ok else {},
        "project_agent_cleanup_execution_reason": execution_reason,
        "project_agent_cleanup_review": project_review,
        "project_agent_cleanup_review_reason": review_reason,
        "project_review_status": "executed_cleanup" if execution_ok else "reviewed_keep" if reviewed_no_cleanup else "authorized_cleanup_pending_project_execution" if authorized else "missing_or_stale_review",
        "project_review_rationale": str(review_source.get("rationale") or review_source.get("reason") or ""),
        "project_reviewed_candidate_count": (project_review or {}).get("reviewed_candidate_count"),
        "protected_current_route": bool(review_source.get("protected_current_route") is True),
        "current_route_reviewed": bool(review_source.get("current_route_reviewed") is True),
        "candidate_fingerprint": review_candidate_fingerprint(candidates),
        "candidate_count": len(candidates),
        "current_route": current,
        "base_switch_gate": {
            "status": gate.get("status") if isinstance(gate, dict) else "",
            "decision": gate.get("decision") if isinstance(gate, dict) else "",
            "switch_authorized": gate.get("switch_authorized") if isinstance(gate, dict) else False,
            "authorization_status": gate.get("authorization_status") if isinstance(gate, dict) else "",
        },
        "base_switch_execution": {
            "status": execution.get("status") if isinstance(execution, dict) else "",
            "decision": execution.get("decision") if isinstance(execution, dict) else "",
            "switch_authorized": execution.get("switch_authorized") if isinstance(execution, dict) else False,
            "authorization_status": execution.get("authorization_status") if isinstance(execution, dict) else "",
        },
        "kept_paths": keep[:200],
        "candidate_cleanup_paths": authorized_candidates[:300] if authorized else [],
        "blocked_candidate_paths": [] if execution_ok or authorized else candidates[:300],
        "policy": "framework only enumerates cleanup candidates and audits project Claude Code decisions. If cleanup is required, project Claude Code must execute it and write an execution receipt; the framework must not delete or archive project files by script or by name matching.",
        "next_action": (
            "Project Claude Code cleanup execution receipt is audited; continue normal gates."
            if execution_ok else
            "No cleanup is required after project Claude Code review; keep current-route files and shared evidence intact."
            if reviewed_no_cleanup else
            "Project Claude Code must review blocked_candidate_paths in project context. If files must be kept, write state/obsolete_baseline_cleanup_review.json with reviewed_candidate_count and candidate_fingerprint copied from this plan; if cleanup is required, write state/obsolete_baseline_cleanup_authorization.json before any cleanup action. Do not clean project files by name matching."
            if not authorized else
            "Project Claude Code must execute cleanup itself for only the exact candidate_cleanup_paths, protect current-route/shared evidence, and write state/obsolete_baseline_cleanup_execution.json with cleanup_executed=true, applied_paths, remaining_candidate_paths, and rationale. The workflow will only audit the receipt; it must not move or delete project files."
        ),
    }
    save_json(paths.state / "obsolete_baseline_cleanup_plan.json", payload)
    report = write_report(paths, payload)
    payload["report_path"] = str(report)
    save_json(paths.state / "obsolete_baseline_cleanup_plan.json", payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a state-driven obsolete baseline cleanup plan without deleting files.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--venue", default="", help="Accepted for orchestrator compatibility; cleanup planning is venue-agnostic.")
    args = parser.parse_args()
    payload = build(args.project)
    print(build_paths(args.project).state / "obsolete_baseline_cleanup_plan.json")
    ok_statuses = {"project_cleanup_completed", "reviewed_no_cleanup_required"}
    return 0 if payload.get("status") in ok_statuses else 2


if __name__ == "__main__":
    raise SystemExit(main())
