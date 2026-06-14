#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from experiment_contracts import row_promotion_blockers
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


def norm_path(value: Any) -> str:
    text = str(value or "").strip()
    return str(Path(text).resolve()) if text else ""


def rows_from_registry(payload: Any) -> list[dict[str, Any]]:
    rows = payload.get("experiments") if isinstance(payload, dict) else payload
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def row_id(row: dict[str, Any]) -> str:
    return str(row.get("experiment_id") or row.get("id") or row.get("name") or "").strip()


def text_blob(row: dict[str, Any]) -> str:
    values = [
        row_id(row),
        row.get("method"),
        row.get("method_label"),
        row.get("name"),
        row.get("embedding_source"),
        row.get("claim_verdict"),
        row.get("comparison_status"),
        row.get("conclusion"),
    ]
    return " ".join(str(value or "") for value in values).lower()


def row_dataset(row: dict[str, Any]) -> str:
    return str(row.get("dataset") or row.get("data") or row.get("claim_ready_dataset") or "").strip().lower()


def current_repo_rows(rows: list[dict[str, Any]], repo_path: str, dataset: str = "") -> list[dict[str, Any]]:
    if not repo_path:
        return []
    dataset_key = str(dataset or "").strip().lower()
    out: list[dict[str, Any]] = []
    for row in rows:
        if norm_path(row.get("repo_path") or row.get("active_repo_path") or row.get("local_path")) != repo_path:
            continue
        row_ds = row_dataset(row)
        if dataset_key and row_ds and row_ds != dataset_key:
            continue
        out.append(row)
    return out


def candidate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        blob = text_blob(row)
        if any(marker in blob for marker in ["baseline", "reference", "control", "pretrain"]):
            continue
        if any(marker in blob for marker in ["llm", "semantic", "text", "embedding", "finetune"]):
            out.append(row)
    return out


def live_candidate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if str(row.get("status") or "").strip().lower() in {"running", "queued", "pending"}]


def live_training_processes(repo_path: str, dataset: str = "") -> list[dict[str, Any]]:
    dataset_key = str(dataset or "").strip().lower()
    try:
        proc = subprocess.run(["ps", "-eo", "pid=,etimes=,cmd="], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid, etimes, cmd = parts
        lowered = cmd.lower()
        if "finetune" not in lowered or "python" not in lowered:
            continue
        if dataset_key and f"--data {dataset_key}" not in lowered and f"--data={dataset_key}" not in lowered:
            continue
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except Exception:
            cwd = ""
        if repo_path and norm_path(cwd) != repo_path:
            continue
        rows.append({"pid": pid, "elapsed_sec": etimes, "cmd": cmd, "cwd": cwd, "dataset": dataset})
    return rows


def has_real_text_signal(row: dict[str, Any]) -> bool:
    blob = text_blob(row)
    return any(marker in blob for marker in ["realtext", "real text", "sentence-transformers", "minilm", "title", "description", "movies_info", "text embeddings"])


def has_proxy_or_cluster_signal(row: dict[str, Any]) -> bool:
    blob = text_blob(row)
    return any(marker in blob for marker in ["cluster", "centroid", "cooccurrence", "co-occurrence", "id embeddings", "pseudo"])


def route_switch_proposals(paths) -> list[str]:
    planning = paths.root / "planning"
    if not planning.exists():
        return []
    return [str(path) for path in sorted(planning.glob("route_switch_proposal*.md"))]


def lower_json_blob(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False).lower()
    except Exception:
        return str(value or "").lower()


def nested_dict(payload: Any, *keys: str) -> dict[str, Any]:
    row = payload if isinstance(payload, dict) else {}
    for key in keys:
        child = row.get(key)
        if not isinstance(child, dict):
            return {}
        row = child
    return row


def nested_bool(payload: Any, *keys: str) -> bool:
    row = payload if isinstance(payload, dict) else {}
    for key in keys[:-1]:
        child = row.get(key)
        if not isinstance(child, dict):
            return False
        row = child
    return row.get(keys[-1]) is True if keys else False


def int_or_none(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(float(str(value).strip()))
    except Exception:
        return None


def project_terminal_route_review(paths) -> dict[str, Any]:
    """Read project-authored route-exhaustion evidence without knowing project content.

    The framework does not decide that a named method/dataset is obsolete. It
    only recognizes a project-scoped Claude/research state that explicitly says
    the current selected route is terminal/exhausted and asks for a dedicated
    deterministic base-switch gate. That still is not switch authorization.
    """
    state_names = [
        "ideation_cycle2_plan.json",
        "structural_blocker_terminal_summary.json",
        "next_cycle_route_proposal.json",
        "repo_viability_assessment.json",
        "selected_route_terminal_review.json",
    ]
    evidence: list[str] = []
    terminal_sources: list[str] = []
    gate_sources: list[str] = []
    no_promotable_sources: list[str] = []
    proposal_sources: list[str] = []
    details: list[dict[str, Any]] = []
    for name in state_names:
        path = paths.state / name
        payload = load_json(path, {})
        if not isinstance(payload, dict) or not payload:
            continue
        blob = lower_json_blob(payload)
        evidence.append(str(path))
        current_route = nested_dict(payload, "current_route_status")
        current_cycle = nested_dict(payload, "current_cycle_status")
        blocker_evidence = nested_dict(payload, "blocker_evidence", "experiment_evidence")
        route_proposal = nested_dict(payload, "route_proposal")
        blockers = nested_dict(payload, "blockers")
        terminal = any(
            marker in str(value or "").lower()
            for value in [
                payload.get("ideation_decision"),
                payload.get("structural_blocker_status"),
                current_route.get("route_status"),
                current_cycle.get("selected_base_status"),
                payload.get("conclusion"),
            ]
            for marker in ["terminal", "exhausted", "structural_blocker", "structurally_blocked", "no further"]
        ) or ("terminal" in blob and any(marker in blob for marker in ["current route", "selected base", "selected-base", "current selected"]))
        gate_required = (
            nested_bool(payload, "route_proposal", "requires_deterministic_base_switch_gate")
            or "deterministic_base_switch_gate" in blob
            or ("deterministic" in blob and ("base-switch" in blob or "base_switch" in blob))
            or ("base-switch gate" in blob or "base_switch_gate" in blob)
        )
        proposal_only = (
            "proposal_only" in blob
            or "proposal only" in blob
            or route_proposal.get("status") == "proposal_only_not_authorized"
            or "not_authorized" in blob
        )
        promotable_values = [
            int_or_none(nested_dict(payload, "current_route_status", "experiments").get("promotable")),
            int_or_none(blocker_evidence.get("promotable_candidates")),
            int_or_none(blockers.get("promotable")),
        ]
        no_promotable = any(value == 0 for value in promotable_values if value is not None)
        if terminal:
            terminal_sources.append(str(path))
        if gate_required:
            gate_sources.append(str(path))
        if no_promotable:
            no_promotable_sources.append(str(path))
        if proposal_only:
            proposal_sources.append(str(path))
        details.append(
            {
                "path": str(path),
                "terminal_current_route": terminal,
                "deterministic_gate_required": gate_required,
                "proposal_only_not_authorized": proposal_only,
                "no_promotable_project_candidates": no_promotable,
            }
        )
    return {
        "terminal_current_route": bool(terminal_sources),
        "deterministic_gate_required": bool(gate_sources),
        "proposal_only_not_authorized": bool(proposal_sources),
        "no_promotable_project_candidates": bool(no_promotable_sources),
        "sources": evidence,
        "terminal_sources": terminal_sources,
        "gate_sources": gate_sources,
        "no_promotable_sources": no_promotable_sources,
        "proposal_sources": proposal_sources,
        "details": details,
    }


def deterministic_base_switch_active(paths) -> dict[str, Any]:
    gate = load_json(paths.state / "base_switch_gate.json", {})
    execution = load_json(paths.state / "base_switch_execution.json", {})
    active = load_json(paths.state / "active_repo.json", {})
    if not (
        isinstance(gate, dict)
        and gate.get("status") == "pass"
        and gate.get("decision") == "authorize_base_switch"
        and gate.get("switch_authorized") is True
        and isinstance(execution, dict)
        and str(execution.get("status") or "").startswith("authorized_by_deterministic_base_switch_gate")
        and isinstance(active, dict)
    ):
        return {}
    candidate = gate.get("candidate_route") if isinstance(gate.get("candidate_route"), dict) else {}
    repo_path = str(candidate.get("repo_path") or "").strip()
    active_path = str(active.get("repo_path") or active.get("local_path") or "").strip()
    if repo_path and active_path == repo_path:
        return {"gate": gate, "execution": execution, "active_repo": active, "candidate": candidate}
    return {}


def build_gate(project: str, venue: str = "") -> dict[str, Any]:
    paths = build_paths(project)
    deterministic_switch = deterministic_base_switch_active(paths)
    if deterministic_switch:
        active = deterministic_switch.get("active_repo", {})
        candidate = deterministic_switch.get("candidate", {})
        return {
            "project": project,
            "venue": venue,
            "updated_at": now_iso(),
            "status": "pass",
            "decision": "continue_after_authorized_base_switch",
            "severity": "pass",
            "issue": "deterministic base-switch gate has executed; continue with reference/scientific/submission gates on the authorized active route.",
            "current_selected_repo": active.get("name") or candidate.get("repo") or "",
            "current_selected_repo_path": active.get("repo_path") or candidate.get("repo_path") or "",
            "selected_base_title": active.get("selected_base_title") or candidate.get("title") or "",
            "authorized_current_repo_unchanged": False,
            "switch_authorized": True,
            "authorization_status": "authorized_by_deterministic_base_switch_gate",
            "base_switch_gate": {
                "status": deterministic_switch.get("gate", {}).get("status"),
                "decision": deterministic_switch.get("gate", {}).get("decision"),
                "switch_authorized": deterministic_switch.get("gate", {}).get("switch_authorized"),
            },
            "guardrail": "Authorized route switch only clears the selected-base viability hard stop; it does not promote claims or make the paper submission-ready.",
            "evidence": [
                str(paths.state / "base_switch_gate.json"),
                str(paths.state / "base_switch_execution.json"),
                str(paths.state / "active_repo.json"),
                str(paths.state / "evidence_ready_repo_selection.json"),
            ],
        }
    active_repo = load_json(paths.state / "active_repo.json", {})
    reference_gate = load_json(paths.state / "reference_reproduction_gate.json", {})
    progress_gate = load_json(paths.state / "scientific_progress_gate.json", {})
    registry = load_json(paths.state / "experiment_registry.json", {})
    audit = load_json(paths.state / "fresh_base_reference_full_reproduction_audit.json", {})
    if not isinstance(audit, dict) or not audit:
        audit = load_json(paths.state / "fresh_base_reference_reproduction_audit.json", {})

    current_dataset = str(
        (reference_gate.get("best_reproduction", {}) if isinstance(reference_gate.get("best_reproduction"), dict) else {}).get("dataset")
        or audit.get("dataset")
        or ""
    ).strip()
    repo_path = norm_path(active_repo.get("repo_path") if isinstance(active_repo, dict) else "")
    repo_name = str((active_repo.get("name") if isinstance(active_repo, dict) else "") or audit.get("repo_name") or "selected-base repo").strip()
    base_title = str(
        (active_repo.get("selected_base_title") if isinstance(active_repo, dict) else "")
        or audit.get("paper_title")
        or audit.get("base_title")
        or "selected-base paper"
    ).strip()
    rows = current_repo_rows(rows_from_registry(registry), repo_path, current_dataset)
    candidates = candidate_rows(rows)
    live_candidates = live_candidate_rows(candidates)
    real_text_candidates = [row for row in candidates if has_real_text_signal(row)]
    proxy_candidates = [row for row in candidates if has_proxy_or_cluster_signal(row)]
    candidate_ids = [row_id(row) for row in candidates if row_id(row)]
    live_candidate_ids = [row_id(row) for row in live_candidates if row_id(row)]
    live_processes = live_training_processes(repo_path, current_dataset)
    live_candidate_ids.extend([f"live_training_pid_{row.get('pid')}" for row in live_processes if row.get("pid")])
    real_text_ids = [row_id(row) for row in real_text_candidates if row_id(row)]
    proxy_ids = [row_id(row) for row in proxy_candidates if row_id(row)]
    promotable_count = int(progress_gate.get("promotable_candidate_audit_ready_runs") or 0) if isinstance(progress_gate, dict) else 0
    non_promotable = progress_gate.get("non_promotable_candidate_runs", []) if isinstance(progress_gate, dict) and isinstance(progress_gate.get("non_promotable_candidate_runs"), list) else []
    progress_status = str(progress_gate.get("status") or "") if isinstance(progress_gate, dict) else ""
    reference_passed = bool(isinstance(reference_gate, dict) and reference_gate.get("status") == "pass" and reference_gate.get("decision") == "continue_base")
    proposal_paths = route_switch_proposals(paths)
    terminal_review = project_terminal_route_review(paths)
    project_review_requests_gate = bool(
        terminal_review.get("terminal_current_route")
        and terminal_review.get("deterministic_gate_required")
        and terminal_review.get("no_promotable_project_candidates")
    )

    status = "not_applicable"
    decision = "not_applicable"
    issue = "selected-base viability gate is not applicable before the selected-base reference gate passes."
    severity = "info"
    if reference_passed and progress_status == "pass" and promotable_count > 0:
        status = "pass"
        decision = "continue_base"
        issue = "selected-base viability gate passed: at least one promotable current selected-base candidate is audit-ready."
        severity = "pass"
    elif reference_passed and progress_status == "blocked":
        enough_attempts = len(set(candidate_ids)) >= 3 or bool(real_text_ids and proxy_ids)
        no_promotable = promotable_count == 0
        if live_candidate_ids:
            status = "blocked"
            decision = "continue_experiment_evidence_repair"
            severity = "block"
            issue = (
                f"selected_base_viability_gate: 当前 selected-base 主线（{repo_name}）仍有候选训练在运行，不能判定路线耗尽或进入自动切基底。"
                "等待训练完成、写入 artifact-local audit 并刷新 scientific_progress/paper_evidence/submission_readiness 后再判断。"
            )
        elif no_promotable and project_review_requests_gate:
            status = "blocked"
            decision = "base_switch_gate_required"
            severity = "block"
            issue = (
                f"selected_base_viability_gate: 参考复现已通过，且项目内路线审计已确认当前 selected-base 主线（{repo_name}）"
                "没有可提升、可写入论文的项目目标候选证据，并要求进入专门 deterministic base-switch gate。"
                f"已审计候选尝试数={len(set(candidate_ids))}；该门控不授权切换基底、不修改 active_repo/evidence_ready_repo_selection、"
                "不提升论文/claim。下一步只能运行确定性 base-switch gate，把候选路线保持为 proposal，直到候选路线的 provenance、loader/data/protocol/smoke/full reproduction 全部通过。"
            )
        elif enough_attempts and no_promotable:
            status = "blocked"
            decision = "continue_experiment_evidence_repair"
            severity = "block"
            issue = (
                f"selected_base_viability_gate: 参考复现已通过，但当前 selected-base 主线（{repo_name}）还没有可提升、可写入论文的 项目目标候选实验证据。"
                f"已审计候选尝试数={len(set(candidate_ids))}；该门控不授权切换基底，也不把旧路线提升为当前参考工作。"
                "确定性门控只确认当前主线仍缺少可提升候选证据；具体实验设计、审计和剪枝动作由项目代理读取证据后决定。只有项目审计明确当前路线耗尽并要求 deterministic base-switch gate 时，才进入切基底门控评估。"
            )
        else:
            status = "blocked"
            decision = "continue_experiment_evidence_repair"
            severity = "block"
            issue = "selected_base_viability_gate: scientific progress is blocked, but the current selected-base experiment search is not yet exhausted enough for base-switch gating."

    return {
        "project": project,
        "venue": venue,
        "updated_at": now_iso(),
        "status": status,
        "decision": decision,
        "severity": severity,
        "issue": issue,
        "current_selected_repo": repo_name,
        "current_selected_repo_path": repo_path,
        "selected_base_title": base_title,
        "current_dataset": current_dataset,
        "reference_gate": {"status": reference_gate.get("status", "") if isinstance(reference_gate, dict) else "", "decision": reference_gate.get("decision", "") if isinstance(reference_gate, dict) else ""},
        "scientific_progress_gate": {"status": progress_status, "promotable_candidate_audit_ready_runs": promotable_count, "non_promotable_candidate_runs": non_promotable},
        "current_repo_candidate_runs": candidate_ids,
        "live_candidate_runs": live_candidate_ids,
        "live_candidate_processes": live_processes,
        "non_promotable_candidate_reasons": {row_id(row): row_promotion_blockers(row) for row in candidates if row_id(row) and row_promotion_blockers(row)},
        "real_text_candidate_runs": real_text_ids,
        "proxy_or_cluster_candidate_runs": proxy_ids,
        "route_switch_proposals": proposal_paths,
        "project_terminal_route_review": terminal_review,
        "authorized_current_repo_unchanged": True,
        "switch_authorized": False,
        "authorization_status": "not_authorized_by_this_gate",
        "guardrail": (
            "This gate does not switch active_repo/evidence_ready_repo_selection. decision=continue_experiment_evidence_repair means The workflow must keep repairing or pruning evidence on the current selected base; "
            "it is not authorization to switch, write base_switch_execution as approved, or run alternative-route experiments as the main route. "
            "decision=base_switch_gate_required only means the framework must run deterministic base-switch gate checks while keeping the current selected route authoritative. "
            "Any alternative route must pass loader/import probe, data contract, reference protocol, smoke, and full reference reproduction before becoming current."
        ),
        "evidence": [
            str(paths.state / "active_repo.json"),
            str(paths.state / "evidence_ready_repo_selection.json"),
            str(paths.state / "reference_reproduction_gate.json"),
            str(paths.state / "scientific_progress_gate.json"),
            str(paths.state / "experiment_registry.json"),
            str(paths.state / "fresh_base_reference_full_reproduction_audit.json"),
        ] + terminal_review.get("sources", [])[:5] + proposal_paths[:5],
    }


def write_report(paths, payload: dict[str, Any]) -> Path:
    out = paths.reports / "selected_base_viability_gate.md"
    lines = [
        "# Selected-Base Viability Gate\n\n",
        f"- status: {payload.get('status', '')}\n",
        f"- decision: {payload.get('decision', '')}\n",
        f"- selected_repo: {payload.get('current_selected_repo', '')}\n",
        f"- selected_base_title: {payload.get('selected_base_title', '')}\n",
        f"- issue: {payload.get('issue', '')}\n",
        "\n## Evidence\n",
    ]
    for item in payload.get("evidence", [])[:20]:
        lines.append(f"- {item}\n")
    out.write_text("".join(lines), encoding="utf-8")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit whether the current selected base should keep receiving experiment repair or enter deterministic base-switch gating.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--venue", default="")
    args = parser.parse_args()
    paths = build_paths(args.project)
    payload = build_gate(args.project, args.venue)
    save_json(paths.state / "selected_base_viability_gate.json", payload)
    report = write_report(paths, payload)
    print(report)
    return 0 if payload.get("status") in {"pass", "not_applicable"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
