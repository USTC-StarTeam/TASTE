#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
from pathlib import Path
from typing import Any

from project_paths import build_paths


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def key(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def current_find_run_id(paths) -> str:
    for path in [paths.planning / "finding" / "find_results.json", paths.state / "current_find_research_plan.json"]:
        payload = load_json(path, {})
        if isinstance(payload, dict):
            run_id = str(payload.get("run_id") or payload.get("find_run_id") or "").strip()
            if run_id:
                return run_id
    return ""


def title_in_current_find(paths, title: Any) -> bool:
    title_key = key(title)
    if not title_key:
        return False
    payload = load_json(paths.planning / "finding" / "find_results.json", {})
    if not isinstance(payload, dict):
        return False
    for pool in ["articles", "strong_recommendations"]:
        rows = payload.get(pool)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and key(row.get("title") or row.get("paper_title")) == title_key:
                return True
    return False


def trusted_reference_audit(paths) -> dict[str, Any]:
    audit = load_json(paths.state / "fresh_base_reference_reproduction_audit.json", {})
    if not isinstance(audit, dict):
        return {}
    if not (audit.get("mode") == "full" and audit.get("return_code") == 0 and audit.get("audit_ready") and audit.get("paper_level_reproduction_passed")):
        return {}
    repo_path = str(audit.get("repo_path") or audit.get("active_repo_path") or "").strip()
    if not repo_path:
        return {}
    return audit


def selected_from_audit(paths, audit: dict[str, Any], selection: dict[str, Any]) -> dict[str, Any]:
    repo_path = str(audit.get("repo_path") or audit.get("active_repo_path") or "").strip()
    repo_name = str(audit.get("repo_name") or "").strip() or "selected-base repo"
    audit_selected = audit.get("selected_base") if isinstance(audit.get("selected_base"), dict) else {}
    if audit_selected and str(audit_selected.get("repo_path") or audit_selected.get("local_path") or "").strip() == repo_path:
        restored = dict(audit_selected)
        restored.setdefault("name", repo_name)
        restored.setdefault("repo_path", repo_path)
        return restored
    candidates = selection.get("audited_candidates") if isinstance(selection.get("audited_candidates"), list) else []
    for row in candidates:
        if not isinstance(row, dict):
            continue
        if str(row.get("repo_path") or row.get("local_path") or "").strip() == repo_path:
            restored = dict(row)
            restored.setdefault("name", repo_name)
            restored.setdefault("repo_path", repo_path)
            return restored
    impl = load_json(paths.state / "fresh_base_implementation_plan.json", {})
    if isinstance(impl, dict):
        for source in [impl.get("selected_base"), impl.get("repo")]:
            if isinstance(source, dict) and str(source.get("repo_path") or source.get("local_path") or "").strip() == repo_path:
                restored = dict(source)
                restored.setdefault("name", repo_name)
                restored.setdefault("repo_path", repo_path)
                return restored
    gate = load_json(paths.state / "reference_reproduction_gate.json", {})
    base_switch = gate.get("base_switch") if isinstance(gate, dict) and isinstance(gate.get("base_switch"), dict) else {}
    fresh_base = base_switch.get("fresh_paper_base") if isinstance(base_switch.get("fresh_paper_base"), dict) else {}
    title = str(fresh_base.get("title") or "").strip()
    restored = {
        "name": repo_name,
        "url": str(audit.get("repo_url") or "").strip(),
        "repo_path": repo_path,
        "literature_base_title": title or str(audit.get("paper_title") or audit.get("base_title") or "").strip(),
        "fresh_find_run_id": current_find_run_id(paths),
        "source": "trusted_full_reference_reproduction_audit",
        "decision": "selected_by_trusted_reference_audit",
        "claim_ready_dataset": audit.get("dataset") or "",
        "claim_ready_datasets": [audit.get("dataset")] if audit.get("dataset") else [],
    }
    return restored


def selection_valid(paths, selected: dict[str, Any], audit: dict[str, Any]) -> bool:
    repo_path = str(selected.get("repo_path") or selected.get("local_path") or "").strip()
    audit_repo = str(audit.get("repo_path") or audit.get("active_repo_path") or "").strip()
    title = selected.get("literature_base_title") or selected.get("title") or selected.get("selected_base_title") or ""
    if not (repo_path and audit_repo and repo_path == audit_repo):
        return False
    audit_selected = audit.get("selected_base") if isinstance(audit.get("selected_base"), dict) else {}
    selected_run = str(selected.get("fresh_find_run_id") or "").strip()
    audit_run = str(audit_selected.get("fresh_find_run_id") or "").strip()
    if audit_run and selected_run == audit_run:
        return True
    audit_title = audit_selected.get("literature_base_title") or audit_selected.get("title") or audit.get("paper_title") or audit.get("base_title") or ""
    if key(title) and key(audit_title) and key(title) == key(audit_title):
        return True
    return bool(title_in_current_find(paths, title))


def selected_base_find_run_id(audit: dict[str, Any], restored_selected: dict[str, Any], fallback: str = "") -> str:
    audit_selected = audit.get("selected_base") if isinstance(audit.get("selected_base"), dict) else {}
    return str(
        restored_selected.get("fresh_find_run_id")
        or audit_selected.get("fresh_find_run_id")
        or fallback
        or ""
    ).strip()


def authorized_route_reason(repo_name: str, title: str, dataset: Any) -> str:
    dataset_text = str(dataset or "selected dataset").strip() or "selected dataset"
    return (
        f"Current authorized selected-base route remains {repo_name} for `{title}`. "
        f"Wrapper-managed full reference reproduction passed on dataset {dataset_text}; "
        "any legacy/control route switch must remain a non-authoritative proposal until deterministic TASTE base-switch gates approve it."
    )


def _route_text_markers(paths, audit: dict[str, Any]) -> tuple[list[str], list[str]]:
    allowed: list[str] = []
    selected_repo = str(audit.get("repo_name") or "").strip()
    if selected_repo:
        allowed.append(selected_repo)
        allowed.append(selected_repo.split("/")[-1])
    blocked: list[str] = []
    for state_name in ["base_switch_gate.json", "base_switch_execution.json"]:
        payload = load_json(paths.state / state_name, {})
        if not isinstance(payload, dict):
            continue
        routes = [payload.get("candidate_route"), payload.get("new_route"), payload.get("proposed_route"), payload.get("invalidated_previous_new_route")]
        for route in routes:
            if not isinstance(route, dict):
                continue
            for value in [route.get("repo"), route.get("name"), route.get("repo_name"), route.get("title"), route.get("paper_title")]:
                text = str(value or "").strip()
                if text:
                    blocked.append(text)
                    blocked.append(text.split("/")[-1])
    allowed_keys = {key(item) for item in allowed if key(item)}
    blocked_keys = []
    for item in blocked:
        marker = key(item)
        if marker and marker not in allowed_keys and marker not in blocked_keys:
            blocked_keys.append(marker)
    return list(allowed_keys), blocked_keys


def contains_unauthorized_route_text(value: Any, audit: dict[str, Any], paths=None) -> bool:
    text = key(value)
    if not text:
        return False
    allowed, markers = _route_text_markers(paths, audit) if paths is not None else ([key(audit.get("repo_name") or "")], [])
    if any(marker and marker in text for marker in allowed) and not any(marker and marker in text for marker in markers):
        return False
    generic_markers = [
        "unauthorized route switch",
        "unapproved switch",
        "invalid_unapproved_switch",
        "switch to candidate",
        "switched to candidate",
        "route switch",
        "dead end",
    ]
    return any(marker in text for marker in markers + generic_markers if marker)


def scrub_authoritative_route_text(
    payload: dict[str, Any],
    *,
    audit: dict[str, Any],
    restored_selected: dict[str, Any],
    report: dict[str, Any],
    prefix: str,
) -> bool:
    changed = False
    repo_name = str(restored_selected.get("name") or audit.get("repo_name") or "selected-base repo")
    title = str(restored_selected.get("literature_base_title") or restored_selected.get("title") or audit.get("paper_title") or "selected-base paper")
    reason = authorized_route_reason(repo_name, title, audit.get("dataset") or restored_selected.get("claim_ready_dataset"))
    for field in [
        "reason",
        "selection_reason",
        "selection_reason_en",
        "switch_reason",
        "route_reason",
        "current_route_reason",
    ]:
        if contains_unauthorized_route_text(payload.get(field), audit, report.get("_paths")):
            payload[field] = reason
            report["violations"].append(f"{prefix}.{field} contained unauthorized legacy/control route-switch wording.")
            changed = True
    zh_reason = f"当前授权 selected-base 路线仍为 {repo_name} / {title}；wrapper 管理的 full reference reproduction 已通过。任何 legacy/control route switch 只能作为非权威 proposal，直到 TASTE 确定性 base-switch 门控批准。"
    for field in ["selection_reason_zh", "route_reason_zh", "current_route_reason_zh"]:
        if contains_unauthorized_route_text(payload.get(field), audit, report.get("_paths")):
            payload[field] = zh_reason
            report["violations"].append(f"{prefix}.{field} contained unauthorized legacy/control route-switch wording.")
            changed = True
    return changed


def backup(path: Path) -> None:
    if path.exists():
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        shutil.copy2(path, path.with_name(path.name + f".bak_route_guard_{stamp}"))


def deterministic_base_switch_authorized(paths) -> dict[str, Any]:
    gate = load_json(paths.state / "base_switch_gate.json", {})
    execution = load_json(paths.state / "base_switch_execution.json", {})
    if not (
        isinstance(gate, dict)
        and gate.get("status") == "pass"
        and gate.get("decision") in {"authorize_base_switch", "approve_switch", "switch_base"}
        and gate.get("switch_authorized") is True
        and isinstance(execution, dict)
        and str(execution.get("status") or "").startswith("authorized_by_deterministic_base_switch_gate")
    ):
        return {}
    candidate = gate.get("candidate_route") if isinstance(gate.get("candidate_route"), dict) else {}
    executed = execution.get("new_route") if isinstance(execution.get("new_route"), dict) else {}
    repo_path = str(executed.get("repo_path") or candidate.get("repo_path") or "").strip()
    if not repo_path:
        return {}
    return {"gate": gate, "execution": execution, "repo_path": repo_path, "candidate": candidate, "executed": executed}


def base_switch_gate_passed(paths) -> bool:
    gate = load_json(paths.state / "base_switch_gate.json", {})
    return bool(
        isinstance(gate, dict)
        and gate.get("status") == "pass"
        and gate.get("decision") in {"authorize_base_switch", "approve_switch", "switch_base"}
        and gate.get("switch_authorized") is True
    )


def authoritative_route_from_current_state(paths) -> dict[str, Any]:
    selection = load_json(paths.state / "evidence_ready_repo_selection.json", {})
    active = load_json(paths.state / "active_repo.json", {})
    selected = selection.get("selected") if isinstance(selection, dict) and isinstance(selection.get("selected"), dict) else {}
    active = active if isinstance(active, dict) else {}
    return {
        "name": selected.get("name") or active.get("name") or "",
        "repo_path": selected.get("repo_path") or selected.get("local_path") or active.get("repo_path") or active.get("local_path") or "",
        "title": selected.get("literature_base_title") or selected.get("title") or active.get("selected_base_title") or active.get("title") or "",
        "dataset": selected.get("claim_ready_dataset") or active.get("claim_ready_dataset") or "",
        "source": "evidence_ready_repo_selection/active_repo",
    }


def repair_stale_base_switch_execution_without_full_audit(paths, report: dict[str, Any], *, dry_run: bool = False) -> bool:
    execution_path = paths.state / "base_switch_execution.json"
    execution = load_json(execution_path, {})
    if not (isinstance(execution, dict) and str(execution.get("status") or "").startswith("authorized")):
        return False
    if base_switch_gate_passed(paths):
        return False
    report["violations"].append("base_switch_execution.json claimed an authorized route switch, but state/base_switch_gate.json is not pass/authorize_base_switch/switch_authorized=true.")
    if dry_run:
        return True

    backup(execution_path)
    previous = dict(execution)
    previous_new_route = previous.get("new_route") if isinstance(previous.get("new_route"), dict) else None
    repaired = dict(execution)
    repaired.pop("new_route", None)
    repaired.update({
        "status": "invalid_unapproved_switch",
        "decision": "rejected_by_selected_base_route_guard",
        "invalidated_at": report["generated_at"],
        "invalidated_reason": "陈旧 base_switch_execution 曾声称已切换候选路线，但当前 state/base_switch_gate.json 未通过授权；候选路线只能保留为历史/proposal，不能作为当前参考工作。",
        "invalidated_previous_status": previous.get("status", ""),
        "invalidated_previous_decision": previous.get("decision", ""),
        "invalidated_previous_new_route": previous_new_route or {},
        "authoritative_route_after_guard": authoritative_route_from_current_state(paths),
        "base_switch_gate_snapshot": load_json(paths.state / "base_switch_gate.json", {}),
    })
    save_json(execution_path, repaired)

    for state_name in ["evidence_ready_repo_selection.json", "active_repo.json"]:
        path = paths.state / state_name
        payload = load_json(path, {})
        if not isinstance(payload, dict):
            continue
        if str(payload.get("selection_gate") or "").startswith("accepted_by_deterministic_base_switch_gate"):
            backup(path)
            payload["selection_gate"] = "accepted_by_claude_topic_fit"
            payload["base_switch_execution_guard"] = {
                "status": "stale_authorization_invalidated",
                "updated_at": report["generated_at"],
                "reason": "deterministic base-switch gate is not authorized; the current selected-base route remains authoritative.",
                "base_switch_execution_path": str(execution_path),
            }
            save_json(path, payload)
    return True


def repair_project(project: str, *, source_stage: str = "", dry_run: bool = False) -> dict[str, Any]:
    paths = build_paths(project)
    audit = trusted_reference_audit(paths)
    report: dict[str, Any] = {
        "_paths": paths,
        "project": project,
        "generated_at": now_iso(),
        "source_stage": source_stage,
        "status": "ok",
        "repaired": False,
        "violations": [],
        "trusted_audit": {},
        "current_find_run_id": current_find_run_id(paths),
        "guardrail": "Current selected-base identity is anchored to wrapper-managed full reference reproduction audit; legacy/control routes cannot overwrite state/evidence_ready_repo_selection.json or state/active_repo.json.",
    }
    stale_execution_repaired = repair_stale_base_switch_execution_without_full_audit(paths, report, dry_run=dry_run)
    if not audit:
        report["trusted_audit_status"] = "no_trusted_full_reference_audit"
        if stale_execution_repaired:
            report["status"] = "would_repair" if dry_run else "repaired"
            report["repaired"] = not dry_run
            report["notes"] = ["No trusted full reference reproduction audit is currently available, but stale base-switch execution authorization was still invalidated because that invariant does not depend on a full audit."]
        else:
            report["status"] = "no_trusted_full_reference_audit"
            report["violations"].append("No trusted selected-base full reference reproduction audit is available; guard did not modify selected route identity.")
        if not dry_run:
            save_json(paths.state / "selected_base_route_guard.json", {k: v for k, v in report.items() if k != "_paths"})
        return report
    report["trusted_audit"] = {
        "repo_name": audit.get("repo_name") or "",
        "repo_path": audit.get("repo_path") or audit.get("active_repo_path") or "",
        "dataset": audit.get("dataset") or "",
        "artifact_dir": audit.get("artifact_dir") or audit.get("artifact_path") or "",
        "audit_path": str(paths.state / "fresh_base_reference_reproduction_audit.json"),
    }
    selection_path = paths.state / "evidence_ready_repo_selection.json"
    active_path = paths.state / "active_repo.json"
    base_switch_execution_path = paths.state / "base_switch_execution.json"
    authorized_switch = deterministic_base_switch_authorized(paths)
    if authorized_switch:
        selection = load_json(selection_path, {})
        active = load_json(active_path, {})
        selected = selection.get("selected") if isinstance(selection, dict) and isinstance(selection.get("selected"), dict) else {}
        active_repo_path = str(active.get("repo_path") or active.get("local_path") or "").strip() if isinstance(active, dict) else ""
        selected_repo_path = str(selected.get("repo_path") or selected.get("local_path") or "").strip()
        if active_repo_path == authorized_switch["repo_path"] and selected_repo_path == authorized_switch["repo_path"]:
            report["status"] = "ok_authorized_base_switch_active"
            report["authorized_base_switch"] = {
                "repo_path": authorized_switch["repo_path"],
                "gate_path": str(paths.state / "base_switch_gate.json"),
                "execution_path": str(paths.state / "base_switch_execution.json"),
            }
            if not dry_run:
                save_json(paths.state / "selected_base_route_guard.json", {k: v for k, v in report.items() if k != "_paths"})
            return report

    selection = load_json(selection_path, {})
    if not isinstance(selection, dict):
        selection = {}
    active = load_json(active_path, {})
    if not isinstance(active, dict):
        active = {}
    selected = selection.get("selected") if isinstance(selection.get("selected"), dict) else {}
    restored_selected = selected_from_audit(paths, audit, selection)
    current_run = report["current_find_run_id"]
    selected_ok = selection_valid(paths, selected, audit)
    active_ok = str(active.get("repo_path") or active.get("local_path") or "").strip() == str(audit.get("repo_path") or audit.get("active_repo_path") or "").strip()
    selected_anchor_run = selected_base_find_run_id(audit, restored_selected, current_run)
    report["selected_base_find_run_id"] = selected_anchor_run
    authoritative_text_repair = False
    if not selected_ok:
        report["violations"].append("evidence_ready_repo_selection.selected does not match the trusted selected-base full reproduction audit.")
    if not active_ok:
        report["violations"].append("active_repo does not match the trusted selected-base full reproduction audit.")
    decision = selection.get("claude_topic_decision") if isinstance(selection.get("claude_topic_decision"), dict) else {}
    raw_selection_gate = str(selection.get("selection_gate") or selected.get("selection_gate") or "").strip()
    accepted_by_claude = bool(selection.get("accepted_by_claude") or raw_selection_gate.startswith("accepted_by_claude") or decision.get("accept_as_current_best"))
    if selected_ok and active_ok and accepted_by_claude and not raw_selection_gate.startswith("accepted_by_claude"):
        report["violations"].append("evidence_ready_repo_selection.selection_gate was stale/non-accepted even though the environment-stage Claude decision accepted the current selected base.")
    authoritative_text_repair = scrub_authoritative_route_text(selection, audit=audit, restored_selected=restored_selected, report=report, prefix="evidence_ready_repo_selection") or authoritative_text_repair
    if isinstance(selection.get("selected"), dict):
        authoritative_text_repair = scrub_authoritative_route_text(selection["selected"], audit=audit, restored_selected=restored_selected, report=report, prefix="evidence_ready_repo_selection.selected") or authoritative_text_repair
    authoritative_text_repair = scrub_authoritative_route_text(active, audit=audit, restored_selected=restored_selected, report=report, prefix="active_repo") or authoritative_text_repair
    base_switch_execution = load_json(base_switch_execution_path, {})
    invalid_base_switch_execution = False
    deterministic_switch_active = bool(authorized_switch)
    if isinstance(base_switch_execution, dict) and str(base_switch_execution.get("status") or "").startswith("authorized"):
        dedicated_gate = load_json(paths.state / "base_switch_gate.json", {})
        dedicated_gate_passed = bool(
            isinstance(dedicated_gate, dict)
            and dedicated_gate.get("status") == "pass"
            and dedicated_gate.get("decision") in {"authorize_base_switch", "approve_switch", "switch_base"}
            and dedicated_gate.get("switch_authorized") is True
        )
        if not (dedicated_gate_passed and deterministic_switch_active):
            invalid_base_switch_execution = True
            report["violations"].append("base_switch_execution.json claimed authorization, but the dedicated deterministic base-switch gate is not passed/authorized for the active route.")
    if report["violations"]:
        report["status"] = "repaired" if not dry_run else "would_repair"
        report["repaired"] = not dry_run
        if not dry_run:
            backup(selection_path)
            backup(active_path)
            if invalid_base_switch_execution and base_switch_execution_path.exists():
                backup(base_switch_execution_path)
                base_switch_execution = dict(base_switch_execution) if isinstance(base_switch_execution, dict) else {}
                base_switch_execution.update({
                    "status": "invalid_unapproved_switch",
                    "decision": "rejected_by_selected_base_route_guard",
                    "invalidated_at": report["generated_at"],
                    "invalidated_reason": "base_switch_execution is stale/non-authoritative because state/base_switch_gate.json is not pass/authorize_base_switch/switch_authorized=true for the active route.",
                    "authoritative_route_after_guard": {
                        "name": restored_selected.get("name") or audit.get("repo_name") or "",
                        "repo_path": restored_selected.get("repo_path") or audit.get("repo_path") or audit.get("active_repo_path") or "",
                        "title": restored_selected.get("literature_base_title") or restored_selected.get("title") or "",
                        "dataset": audit.get("dataset") or restored_selected.get("claim_ready_dataset") or "",
                    },
                })
                save_json(base_switch_execution_path, base_switch_execution)
            if not selected_ok:
                selection["selected"] = restored_selected
            else:
                selection["selected"] = selected
            selection["fresh_find_run_id"] = selected_anchor_run or restored_selected.get("fresh_find_run_id") or selection.get("fresh_find_run_id")
            selection["selection_stage"] = "environment_claude_code"
            if str(selection.get("selection_gate") or "").startswith("accepted_by_deterministic_base_switch_gate") and not authorized_switch:
                report["violations"].append("evidence_ready_repo_selection.selection_gate incorrectly implied deterministic base-switch acceptance without an authorized gate.")
            selection["selection_gate"] = "accepted_by_claude_topic_fit"
            selection["accepted_by_claude"] = True
            selection["selected_base_route_guard"] = {
                "status": "restored_from_trusted_full_reference_audit",
                "updated_at": report["generated_at"],
                "source_stage": source_stage,
                "audit_path": str(paths.state / "fresh_base_reference_reproduction_audit.json"),
            }
            if isinstance(selection.get("repo_env_strategy"), dict):
                selection["repo_env_strategy"]["selected_repo"] = {
                    "name": restored_selected.get("name") or audit.get("repo_name") or "",
                    "repo_path": restored_selected.get("repo_path") or audit.get("repo_path") or "",
                    "dataset": audit.get("dataset") or restored_selected.get("claim_ready_dataset") or "",
                }
            save_json(selection_path, selection)
            active_restored = dict(active)
            if authoritative_text_repair:
                scrub_authoritative_route_text(selection, audit=audit, restored_selected=restored_selected, report={"violations": []}, prefix="evidence_ready_repo_selection")
                if isinstance(selection.get("selected"), dict):
                    scrub_authoritative_route_text(selection["selected"], audit=audit, restored_selected=restored_selected, report={"violations": []}, prefix="evidence_ready_repo_selection.selected")
                scrub_authoritative_route_text(active_restored, audit=audit, restored_selected=restored_selected, report={"violations": []}, prefix="active_repo")
            active_restored.update({
                "project": project,
                "updated_at": report["generated_at"],
                "name": restored_selected.get("name") or audit.get("repo_name") or "",
                "url": restored_selected.get("url") or restored_selected.get("repo_url") or "",
                "repo_path": restored_selected.get("repo_path") or audit.get("repo_path") or "",
                "local_path": restored_selected.get("local_path") or restored_selected.get("repo_path") or audit.get("repo_path") or "",
                "role": "main_fresh_base",
                "selection_stage": "environment_claude_code",
                "selected_by": selected_anchor_run,
                "fresh_find_run_id": selected_anchor_run,
                "selected_base_title": restored_selected.get("literature_base_title") or restored_selected.get("title") or "",
                "claim_ready_dataset": restored_selected.get("claim_ready_dataset") or audit.get("dataset") or "",
                "ready_datasets": restored_selected.get("claim_ready_datasets") or ([audit.get("dataset")] if audit.get("dataset") else []),
                "reference_reproduction": {
                    "status": "pass",
                    "decision": "continue_base",
                    "audit_path": str(paths.state / "fresh_base_reference_reproduction_audit.json"),
                    "artifact_dir": audit.get("artifact_dir") or audit.get("artifact_path") or "",
                },
                "selected_base_route_guard": selection["selected_base_route_guard"],
            })
            save_json(active_path, active_restored)
    if not dry_run:
        save_json(paths.state / "selected_base_route_guard.json", {k: v for k, v in report.items() if k != "_paths"})
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Guard current selected-base route identity against legacy/control route overwrite.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--source-stage", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    report = repair_project(args.project, source_stage=args.source_stage, dry_run=args.dry_run)
    public_report = {k: v for k, v in report.items() if k != "_paths"}
    print(json.dumps(public_report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") in {"ok", "repaired", "would_repair"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
