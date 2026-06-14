#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

from project_paths import ROOT, build_paths

from taste_pythonpath import script_resolver

SCRIPTS = script_resolver(ROOT)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

def read_text(path: Path, limit: int = 300000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit] if path.exists() else ""
    except Exception:
        return ""


def source_contains(script: str, patterns: list[str]) -> bool:
    source = read_text(SCRIPTS / script)
    return bool(source) and all(pattern in source for pattern in patterns)


def source_omits(script: str, patterns: list[str]) -> bool:
    source = read_text(SCRIPTS / script)
    return bool(source) and all(pattern not in source for pattern in patterns)


def count_landscape_nodes(payload: dict[str, Any]) -> int:
    nodes = payload.get("nodes", {}) if isinstance(payload, dict) else {}
    if not isinstance(nodes, dict):
        return 0
    return sum(len(rows) for rows in nodes.values() if isinstance(rows, list))


def list_count(payload: Any, key: str) -> int:
    values = payload.get(key, []) if isinstance(payload, dict) else []
    return len(values) if isinstance(values, list) else 0


def history_count(payload: Any, key: str = "history") -> int:
    values = payload.get(key, []) if isinstance(payload, dict) else []
    return len(values) if isinstance(values, list) else 0


def script_exists(name: str) -> bool:
    return (SCRIPTS / name).exists()


def skill_files() -> list[Path]:
    root = ROOT / ".claude" / "skills"
    return sorted(root.glob("*/SKILL.md")) if root.exists() else []


def module_status(checks: list[dict[str, Any]]) -> str:
    if any(row.get("severity") in {"block", "blocked"} or row.get("status") in {"block", "blocked"} for row in checks):
        return "blocked"
    if any(row.get("severity") == "warn" or row.get("status") == "warn" for row in checks):
        return "warn"
    return "pass"


def check(name: str, ok: bool, *, severity: str = "block", evidence: Any = None, detail: str = "") -> dict[str, Any]:
    return {
        "id": name,
        "name": name,
        "status": "pass" if ok else severity,
        "severity": "pass" if ok else severity,
        "detail": detail,
        "evidence": evidence or [],
    }


def audit_direction_management(paths) -> dict[str, Any]:
    landscape_path = paths.state / "research_landscape.json"
    novelty_path = paths.state / "novelty_map.json"
    failed_path = paths.state / "failed_hypothesis_graph.json"
    niche_path = paths.state / "unexplored_niche_graph.json"
    direction_path = paths.state / "research_direction_memory.json"
    graph_history_path = paths.state / "research_graph_history.json"
    landscape_assessment_path = paths.state / "research_landscape_assessment.json"
    landscape = load_json(landscape_path, {})
    novelty = load_json(novelty_path, {})
    failed = load_json(failed_path, {})
    niches = load_json(niche_path, {})
    direction = load_json(direction_path, {})
    graph_history = load_json(graph_history_path, {})
    landscape_assessment = load_json(landscape_assessment_path, {})
    checks = [
        check("research_landscape_file", landscape_path.exists(), evidence=[str(landscape_path)]),
        check("research_landscape_has_nodes", count_landscape_nodes(landscape) > 0, severity="warn", evidence=[str(landscape_path)], detail=f"nodes={count_landscape_nodes(landscape)}"),
        check("novelty_map_file", novelty_path.exists(), evidence=[str(novelty_path)]),
        check("novelty_map_maintained", isinstance(novelty.get("nodes", []), list), evidence=[str(novelty_path)], detail=f"nodes={list_count(novelty, 'nodes')}"),
        check("failed_hypothesis_graph_file", failed_path.exists(), evidence=[str(failed_path)]),
        check("failed_hypothesis_graph_maintained", isinstance(failed.get("nodes", []), list), evidence=[str(failed_path)], detail=f"nodes={list_count(failed, 'nodes')}"),
        check("unexplored_niche_graph_file", niche_path.exists(), evidence=[str(niche_path)]),
        check("unexplored_niche_graph_maintained", isinstance(niches.get("nodes", []), list), evidence=[str(niche_path)], detail=f"nodes={list_count(niches, 'nodes')}"),
        check("direction_memory_history", history_count(direction) > 0, severity="warn", evidence=[str(direction_path)], detail=f"entries={history_count(direction)}"),
        check("research_graph_history_file", graph_history_path.exists(), evidence=[str(graph_history_path)]),
        check("research_graph_history_append", history_count(graph_history) > 0, severity="warn", evidence=[str(graph_history_path)], detail=f"entries={history_count(graph_history)}"),
        check("landscape_assessment_file", landscape_assessment_path.exists(), evidence=[str(landscape_assessment_path)]),
        check("landscape_assessment_status", bool(landscape_assessment.get("status")), severity="warn", evidence=[str(landscape_assessment_path)], detail=f"status={landscape_assessment.get('status', '')}"),
    ]
    return {
        "id": "research_direction_management",
        "module": "research_direction_management",
        "status": module_status(checks),
        "checks": checks,
        "metrics": {
            "landscape_nodes": count_landscape_nodes(landscape),
            "novelty_nodes": list_count(novelty, "nodes"),
            "failed_hypotheses": list_count(failed, "nodes"),
            "unexplored_niches": list_count(niches, "nodes"),
            "direction_memory_entries": history_count(direction),
            "graph_history_entries": history_count(graph_history),
            "landscape_assessment_status": landscape_assessment.get("status", ""),
        },
        "evidence_files": [str(landscape_path), str(novelty_path), str(failed_path), str(niche_path), str(direction_path), str(graph_history_path), str(landscape_assessment_path)],
    }


def audit_evolutionary_memory(paths) -> dict[str, Any]:
    memory_path = paths.state / "research_memory.json"
    index_path = paths.state / "evolutionary_memory_index.json"
    evo_memory_path = paths.state / "evolution_memory.json"
    recoverable_path = paths.state / "evo_recoverable_memory.json"
    cycle_path = paths.state / "recoverable_cycle_summary.json"
    ledger_path = paths.state / "evolutionary_memory_ledger.json"
    memory = load_json(memory_path, {})
    index = load_json(index_path, {})
    evo = load_json(evo_memory_path, {})
    recoverable = load_json(recoverable_path, {})
    cycle = load_json(cycle_path, {})
    ledger = load_json(ledger_path, {})
    checks = [
        check("research_memory_file", memory_path.exists(), evidence=[str(memory_path)]),
        check("ideation_memory_persisted", history_count(memory, "ideation_memory") > 0, severity="warn", evidence=[str(memory_path)], detail=f"entries={history_count(memory, 'ideation_memory')}"),
        check("experimentation_memory_persisted", history_count(memory, "experimentation_memory") > 0, severity="warn", evidence=[str(memory_path)], detail=f"entries={history_count(memory, 'experimentation_memory')}"),
        check("assurance_memory_persisted", history_count(memory, "assurance_memory") > 0, severity="warn", evidence=[str(memory_path)], detail=f"entries={history_count(memory, 'assurance_memory')}"),
        check("trajectory_memory_persisted", history_count(memory, "trajectory_memory") > 0, severity="warn", evidence=[str(memory_path)], detail=f"entries={history_count(memory, 'trajectory_memory')}"),
        check("evolutionary_index_file", index_path.exists(), evidence=[str(index_path)]),
        check("evolutionary_index_has_items", int(index.get("indexed_item_count", 0) or 0) > 0, severity="warn", evidence=[str(index_path)], detail=f"items={index.get('indexed_item_count', 0)}"),
        check("recoverable_cycle_summary", cycle_path.exists(), severity="warn", evidence=[str(cycle_path)], detail=f"status={cycle.get('status', '')}"),
        check("recoverable_memory_available", recoverable_path.exists() or bool(recoverable) or bool(evo), severity="warn", evidence=[str(recoverable_path), str(evo_memory_path)]),
        check("evolutionary_memory_ledger_file", ledger_path.exists(), evidence=[str(ledger_path)]),
        check("evolutionary_memory_ledger_append", history_count(ledger) > 0, severity="warn", evidence=[str(ledger_path)], detail=f"entries={history_count(ledger)}"),
    ]
    return {
        "id": "evolutionary_memory",
        "module": "evolutionary_memory",
        "status": module_status(checks),
        "checks": checks,
        "metrics": {
            "ideation_entries": history_count(memory, "ideation_memory"),
            "experimentation_entries": history_count(memory, "experimentation_memory"),
            "assurance_entries": history_count(memory, "assurance_memory"),
            "trajectory_entries": history_count(memory, "trajectory_memory"),
            "evolutionary_index_items": int(index.get("indexed_item_count", 0) or 0),
            "recoverable_exception_entries": int(cycle.get("exception_memory_entries", 0) or 0),
            "evolutionary_memory_ledger_entries": history_count(ledger),
        },
        "evidence_files": [str(memory_path), str(index_path), str(evo_memory_path), str(recoverable_path), str(cycle_path), str(ledger_path)],
    }


def audit_assurance_layer(paths) -> dict[str, Any]:
    assurance_path = paths.state / "research_assurance_layer.json"
    integrity_path = paths.state / "research_evidence_integrity.json"
    audit_path = paths.reports / "paper_evidence_audit.md"
    aris_path = paths.state / "evidence_review_board.json"
    manifest_path = paths.state / "research_evidence_manifest.json"
    assurance = load_json(assurance_path, {})
    integrity = load_json(integrity_path, {})
    manifest = load_json(manifest_path, {})
    checks = [
        check("assurance_layer_file", assurance_path.exists(), evidence=[str(assurance_path)]),
        check("assurance_layer_has_principles", bool(assurance.get("principles")), severity="warn", evidence=[str(assurance_path)]),
        check("assurance_issues_are_explicit", isinstance(assurance.get("issues", []), list), evidence=[str(assurance_path)], detail=f"issues={len(assurance.get('issues', [])) if isinstance(assurance.get('issues', []), list) else 'n/a'}"),
        check("evidence_integrity_file", integrity_path.exists(), evidence=[str(integrity_path)]),
        check("evidence_integrity_checked_nodes", int(integrity.get("checked_nodes", 0) or 0) > 0, severity="warn", evidence=[str(integrity_path)], detail=f"checked={integrity.get('checked_nodes', 0)}"),
        check("paper_evidence_audit_present", audit_path.exists(), severity="warn", evidence=[str(audit_path)]),
        check("evidence_review_board_present", aris_path.exists(), severity="warn", evidence=[str(aris_path)]),
        check("evidence_manifest_file", manifest_path.exists(), evidence=[str(manifest_path)]),
        check("evidence_manifest_has_refs", int(manifest.get("ref_count", 0) or 0) > 0, severity="warn", evidence=[str(manifest_path)], detail=f"refs={manifest.get('ref_count', 0)}"),
        check("weak_claims_are_blocked", assurance.get("status") == "blocked" or not manifest.get("weak_or_unsupported_claims"), evidence=[str(assurance_path), str(manifest_path)]),
    ]
    return {
        "id": "research_assurance_layer",
        "module": "research_assurance_layer",
        "status": module_status(checks),
        "research_gate_status": assurance.get("status", "unknown"),
        "evidence_integrity_status": integrity.get("status", "unknown"),
        "research_gate_issues": assurance.get("issues", [])[:20] if isinstance(assurance.get("issues", []), list) else [],
        "checks": checks,
        "metrics": {
            "assurance_issues": len(assurance.get("issues", [])) if isinstance(assurance.get("issues", []), list) else 0,
            "integrity_issues": len(integrity.get("issues", [])) if isinstance(integrity.get("issues", []), list) else 0,
            "checked_nodes": int(integrity.get("checked_nodes", 0) or 0),
            "evidence_manifest_refs": int(manifest.get("ref_count", 0) or 0),
            "weak_or_unsupported_claims": len(manifest.get("weak_or_unsupported_claims", [])) if isinstance(manifest.get("weak_or_unsupported_claims", []), list) else 0,
        },
        "evidence_files": [str(assurance_path), str(integrity_path), str(audit_path), str(aris_path), str(manifest_path)],
        "note": "A blocked research gate means The workflow is refusing unsupported claims; it is not a capability failure if the gate and evidence are explicit.",
    }



def audit_end_to_end_verification(paths) -> dict[str, Any]:
    verification_path = paths.state / "research_trajectory_end_to_end_verification.json"
    verification = load_json(verification_path, {})
    modules = verification.get("modules", []) if isinstance(verification.get("modules", []), list) else []
    failed_checks = int(verification.get("failed_checks", 0) or 0) if isinstance(verification, dict) else 0
    warning_checks = int(verification.get("warning_checks", 0) or 0) if isinstance(verification, dict) else 0
    checks = [
        check("end_to_end_verification_file", verification_path.exists(), evidence=[str(verification_path)]),
        check("end_to_end_verification_script", script_exists("verify_research_trajectory_end_to_end.py"), evidence=[str(SCRIPTS / "verify_research_trajectory_end_to_end.py")]),
        check("end_to_end_verification_has_modules", len(modules) >= 6, severity="warn", evidence=[str(verification_path)], detail=f"modules={len(modules)}"),
        check("end_to_end_verification_no_failed_checks", failed_checks == 0, evidence=[str(verification_path)], detail=f"failed={failed_checks}"),
        check("trajectory_builder_runs_end_to_end_verifier", source_contains("build_research_trajectory_system.py", ["run_end_to_end_verification", "verify_research_trajectory_end_to_end.py"]), evidence=[str(SCRIPTS / "build_research_trajectory_system.py")]),
        check("web_exposes_end_to_end_verification", source_contains("../web/backend/auto_research/web/project_bridge.py", ["research_trajectory_end_to_end_verification", "end_to_end_verification_status"]), evidence=[str(ROOT / "web" / "backend" / "auto_research" / "web" / "project_bridge.py")]),
    ]
    return {
        "id": "end_to_end_verification",
        "module": "end_to_end_verification",
        "status": module_status(checks),
        "checks": checks,
        "metrics": {
            "verification_modules": len(modules),
            "verification_total_checks": int(verification.get("total_checks", 0) or 0) if isinstance(verification, dict) else 0,
            "verification_failed_checks": failed_checks,
            "verification_warning_checks": warning_checks,
            "verification_overall_status": verification.get("overall_status", "") if isinstance(verification, dict) else "",
        },
        "evidence_files": [str(verification_path), str(SCRIPTS / "verify_research_trajectory_end_to_end.py")],
    }


def audit_paper_orchestra_capability(paths) -> dict[str, Any]:
    state_path = paths.state / "paper_orchestra_state.json"
    readiness_path = paths.state / "submission_readiness.json"
    audit_path = paths.state / "paper_orchestra_audit.json"
    bridge_path = paths.state / "paper_orchestra_bridge.json"
    normality_path = paths.state / "paper_normality_audit.json"
    pipeline_path = paths.root / "paper" / "metadata" / "paper_pipeline.json"
    state = load_json(state_path, {})
    readiness = load_json(readiness_path, {})
    bridge_state = load_json(bridge_path, {})
    sections = state.get("sections", []) if isinstance(state.get("sections", []), list) else []
    checks = [
        check("paper_orchestra_state_script", script_exists("build_paper_orchestra_state.py"), evidence=[str(SCRIPTS / "build_paper_orchestra_state.py")]),
        check("real_paper_orchestra_bridge_script", script_exists("run_paper_orchestra_bridge.py"), evidence=[str(SCRIPTS / "run_paper_orchestra_bridge.py")]),
        check("paper_normality_audit_script", script_exists("audit_paper_normality.py"), evidence=[str(SCRIPTS / "audit_paper_normality.py")]),
        check("submission_readiness_script", script_exists("audit_submission_readiness.py"), evidence=[str(SCRIPTS / "audit_submission_readiness.py")]),
        check("paper_bridge_invokes_project_session", source_contains("run_paper_orchestra_bridge.py", ["claude_project_session.py", "workspace/inputs"]), evidence=[str(SCRIPTS / "run_paper_orchestra_bridge.py")]),
        check("preview_blocks_non_normal_outputs", source_contains("build_conference_preview_paper.py", ["audit_paper_normality.py", "normal_preview_ready", "does not"]), evidence=[str(SCRIPTS / "build_conference_preview_paper.py")]),
        check("paper_orchestra_state_file", state_path.exists(), severity="warn", evidence=[str(state_path)]),
        check("paper_orchestra_sections_present", len(sections) >= 8, severity="warn", evidence=[str(state_path)], detail=f"sections={len(sections)}"),
        check("paper_orchestra_tracks_claims_evidence_citations_artifacts", all(key in state for key in ["claims", "citations", "artifacts", "structured_inputs"]), severity="warn", evidence=[str(state_path)]),
        check("paper_orchestra_bridge_state_file", bridge_path.exists(), severity="warn", evidence=[str(bridge_path)]),
        check("paper_bridge_records_source_repo", bridge_state.get("source", {}).get("repository") == "https://github.com/Ar9av/PaperOrchestra.git" if isinstance(bridge_state.get("source", {}), dict) else False, severity="warn", evidence=[str(bridge_path)]),
        check("paper_normality_state_file", normality_path.exists(), severity="warn", evidence=[str(normality_path)]),
        check("submission_readiness_file", readiness_path.exists(), severity="warn", evidence=[str(readiness_path)]),
        check("submission_readiness_has_checks", isinstance(readiness.get("checks", []), list) and bool(readiness.get("checks", [])), severity="warn", evidence=[str(readiness_path)], detail=f"checks={len(readiness.get('checks', [])) if isinstance(readiness.get('checks', []), list) else 0}"),
        check("paper_pipeline_runs_orchestra_and_readiness", source_contains("run_paper_pipeline.py", ["run_paper_orchestra_bridge.py", "build_paper_orchestra_state.py", "audit_submission_readiness.py", "submission_ready"]), evidence=[str(SCRIPTS / "run_paper_pipeline.py")]),
        check("trajectory_builder_reads_paper_production", source_contains("build_research_trajectory_system.py", ["paper_orchestra_state", "submission_readiness", "TASTE Paper Production System"]), evidence=[str(SCRIPTS / "build_research_trajectory_system.py")]),
        check("writing_skill_contract_mentions_submission", "submission" in read_text(ROOT / ".claude" / "skills" / "writing" / "SKILL.md").lower(), severity="warn", evidence=[str(ROOT / ".claude" / "skills" / "writing" / "SKILL.md")]),
    ]
    return {
        "id": "paper_production_system",
        "module": "paper_production_system",
        "status": module_status(checks),
        "checks": checks,
        "metrics": {
            "section_count": len(sections),
            "paper_orchestra_state_status": state.get("status", "") if isinstance(state, dict) else "",
            "paper_orchestra_bridge_status": bridge_state.get("status", "") if isinstance(bridge_state, dict) else "",
            "submission_readiness_status": readiness.get("status", "") if isinstance(readiness, dict) else "",
            "submission_ready": bool(readiness.get("submission_ready")) if isinstance(readiness, dict) else False,
        },
        "evidence_files": [str(state_path), str(readiness_path), str(audit_path), str(bridge_path), str(normality_path), str(pipeline_path)],
    }

def audit_trajectory_system(paths) -> dict[str, Any]:
    protocol_path = paths.state / "trajectory_execution_protocol.json"
    plan_path = paths.state / "trajectory_optimization_plan.json"
    checkpoints_path = paths.state / "trajectory_checkpoints.json"
    supervisor_state_path = paths.state / "trajectory_supervisor_state.json"
    protocol = load_json(protocol_path, {})
    plan = load_json(plan_path, {})
    checkpoints = load_json(checkpoints_path, {})
    supervisor = load_json(supervisor_state_path, {})
    loop_steps = protocol.get("loop_steps", []) if isinstance(protocol.get("loop_steps", []), list) else []
    queue = plan.get("queue", []) if isinstance(plan.get("queue", []), list) else []
    rounds = supervisor.get("rounds", []) if isinstance(supervisor.get("rounds", []), list) else []
    checks = [
        check("execution_protocol_file", protocol_path.exists(), evidence=[str(protocol_path)]),
        check("execution_protocol_has_loop_steps", len(loop_steps) >= 5, severity="warn", evidence=[str(protocol_path)], detail=f"steps={len(loop_steps)}"),
        check("trajectory_supervisor_entrypoint", script_exists("run_research_trajectory_supervisor.py"), evidence=[str(SCRIPTS / "run_research_trajectory_supervisor.py")]),
        check("optimization_plan_file", plan_path.exists(), evidence=[str(plan_path)]),
        check("optimization_queue_available", len(queue) > 0, severity="warn", evidence=[str(plan_path)], detail=f"queue={len(queue)}"),
        check("trajectory_checkpoints_file", checkpoints_path.exists(), evidence=[str(checkpoints_path)]),
        check("trajectory_checkpoints_append", int(checkpoints.get("checkpoint_count", 0) or 0) > 0, severity="warn", evidence=[str(checkpoints_path)], detail=f"checkpoints={checkpoints.get('checkpoint_count', 0)}"),
        check("supervisor_state_file", supervisor_state_path.exists(), severity="warn", evidence=[str(supervisor_state_path)]),
        check("supervisor_has_round_history", len(rounds) > 0, severity="warn", evidence=[str(supervisor_state_path)], detail=f"rounds={len(rounds)}"),
        check("run_loop_integrates_trajectory_supervisor", source_contains("run_loop.py", ["run_research_trajectory_supervisor.py", "trajectory-rounds"]), evidence=[str(SCRIPTS / "run_loop.py")]),
        check("supervisor_delegates_persistent_claude_trajectory_stage", source_contains("run_research_trajectory_supervisor.py", ["claude_project_session.py", "--stage", "trajectory"]), evidence=[str(SCRIPTS / "run_research_trajectory_supervisor.py")]),
        check("supervisor_rebuilds_after_worker", source_contains("run_research_trajectory_supervisor.py", ["build_research_trajectory_system.py", "post_rebuild_return_code"]), evidence=[str(SCRIPTS / "run_research_trajectory_supervisor.py")]),
        check("protocol_declares_worker_contract", isinstance(protocol.get("worker_contract", {}), dict) and bool(protocol.get("worker_contract", {})), evidence=[str(protocol_path)]),
        check("protocol_requires_graph_history_and_manifest", "state/research_graph_history.json" in json.dumps(protocol) and "state/research_evidence_manifest.json" in json.dumps(protocol), evidence=[str(protocol_path)]),
    ]
    return {
        "id": "trajectory_system",
        "module": "trajectory_system",
        "status": module_status(checks),
        "checks": checks,
        "metrics": {
            "loop_steps": len(loop_steps),
            "optimization_queue_size": len(queue),
            "checkpoint_count": int(checkpoints.get("checkpoint_count", 0) or 0),
            "supervisor_rounds": len(rounds),
            "latest_supervisor_status": supervisor.get("latest", {}).get("status", "") if isinstance(supervisor.get("latest", {}), dict) else "",
        },
        "evidence_files": [str(protocol_path), str(plan_path), str(checkpoints_path), str(supervisor_state_path), str(SCRIPTS / "run_research_trajectory_supervisor.py")],
    }


def audit_skill_and_code_bindings(paths) -> dict[str, Any]:
    required_skills = {"experiment-loop", "evidence-gate", "writing"}
    skills = skill_files()
    skill_names = {path.parent.name for path in skills}
    contracts_path = paths.state / "research_skill_contracts.json"
    contracts = load_json(contracts_path, [])
    required_scripts = {
        "EvidenceAssurance": ["build_aris_review_board.py", "audit_paper_evidence.py"],
        "TrajectoryOptimization": ["update_evolution_memory.py", "run_evoscientist_style_cycle.py", "run_autoscientist_supervisor.py", "run_research_trajectory_supervisor.py"],
        "PaperProduction": ["run_paper_pipeline.py", "build_paper_md.py", "revise_paper_md.py", "build_paper_orchestra_state.py", "audit_paper_orchestra.py", "audit_submission_readiness.py", "audit_paper_evidence.py"],
    }
    checks = [
        check(f"skill_{name}", name in skill_names, evidence=[str(ROOT / ".claude" / "skills" / name / "SKILL.md")])
        for name in sorted(required_skills)
    ]
    checks.append(check("skill_contracts_exported", contracts_path.exists() and isinstance(contracts, list) and len(contracts) >= len(required_skills), severity="warn", evidence=[str(contracts_path)], detail=f"contracts={len(contracts) if isinstance(contracts, list) else 'n/a'}"))
    checks.extend([
        check("trajectory_builder_invokes_assurance_and_memory_helpers", source_contains("build_research_trajectory_system.py", ["build_aris_review_board.py", "audit_paper_evidence.py", "update_evolution_memory.py"]), evidence=[str(SCRIPTS / "build_research_trajectory_system.py")]),
        check("trajectory_builder_exports_local_skill_contracts", source_contains("build_research_trajectory_system.py", ["load_skill_contracts", "research_skill_contracts"]), evidence=[str(SCRIPTS / "build_research_trajectory_system.py")]),
        check("trajectory_builder_maintains_graph_history_and_manifest", source_contains("build_research_trajectory_system.py", ["update_research_graph_history", "research_evidence_manifest", "update_evolutionary_memory_ledger"]), evidence=[str(SCRIPTS / "build_research_trajectory_system.py")]),
        check("claude_project_session_requires_trajectory_and_skills", source_contains("claude_project_session.py", ["research_trajectory_capability_audit", ".claude/skills", "Optimize the whole trajectory"]), evidence=[str(SCRIPTS / "claude_project_session.py")]),
        check("coding_agent_reads_capability_audit", source_contains("run_coding_agent.py", ["research_trajectory_capability_audit", "trajectory_context"]), evidence=[str(SCRIPTS / "run_coding_agent.py")]),
    ])
    for family, scripts in required_scripts.items():
        for script in scripts:
            checks.append(check(f"{family}:{script}", script_exists(script), evidence=[str(SCRIPTS / script)]))
    return {
        "id": "skill_and_code_bindings",
        "module": "skill_and_code_bindings",
        "status": module_status(checks),
        "checks": checks,
        "metrics": {
            "required_skills": len(required_skills),
            "available_required_skills": len(required_skills & skill_names),
            "exported_skill_contracts": len(contracts) if isinstance(contracts, list) else 0,
        },
        "directly_used_patterns": required_scripts,
        "evidence_files": [str(path) for path in skills] + [str(contracts_path)],
    }


def audit_third_party_research_stack(paths) -> dict[str, Any]:
    stack_path = paths.state / "third_party_research_stack.json"
    report_path = paths.reports / "third_party_research_stack.md"
    stack = load_json(stack_path, {})
    summary = stack.get("summary", {}) if isinstance(stack.get("summary", {}), dict) else {}
    sources = stack.get("sources", []) if isinstance(stack.get("sources", []), list) else []
    adapters = stack.get("synced_skill_adapters", []) if isinstance(stack.get("synced_skill_adapters", []), list) else []
    families = stack.get("families", []) if isinstance(stack.get("families", []), list) else []
    modules = [module for family in families if isinstance(family, dict) for module in family.get("modules", []) if isinstance(module, dict)]
    adapter_names = {str(row.get("name") or "") for row in adapters if isinstance(row, dict)}
    required_sources = {"ARIS", "EvoScientist", "academic-research-skills", "PaperOrchestra"}
    available_sources = {str(row.get("name") or "") for row in sources if isinstance(row, dict) and row.get("available")}
    checks = [
        check("third_party_stack_sync_script", script_exists("sync_third_party_research_stack.py"), evidence=[str(SCRIPTS / "sync_third_party_research_stack.py")]),
        check("third_party_stack_state_file", stack_path.exists(), evidence=[str(stack_path)]),
        check("third_party_stack_report_file", report_path.exists(), evidence=[str(report_path)]),
        check("third_party_stack_status_ready", stack.get("status") == "ready", evidence=[str(stack_path)], detail=f"status={stack.get('status', '')}"),
        check("third_party_sources_available", required_sources <= available_sources, evidence=[str(ROOT / "third_party")], detail=f"available={sorted(available_sources)}"),
        check("third_party_commits_recorded", all(row.get("commit") for row in sources if isinstance(row, dict)), evidence=[str(stack_path)]),
        check("third_party_licenses_recorded", all(row.get("license") and row.get("license_path") for row in sources if isinstance(row, dict)), evidence=[str(stack_path)]),
        check("third_party_selected_modules_available", int(summary.get("missing_module_count", 0) or 0) == 0, evidence=[str(stack_path)], detail=f"missing={summary.get('missing_module_count', 0)}"),
        check("third_party_builder_invokes_sync", source_contains("build_research_trajectory_system.py", ["sync_third_party_research_stack.py", "third_party_research_stack"]), evidence=[str(SCRIPTS / "build_research_trajectory_system.py")]),
        check("claude_prompt_uses_native_method_context", source_contains("claude_project_session.py", ["third_party_research_stack", "native method capability contracts"]) and source_omits("claude_project_session.py", ["Use ARIS/EvoScientist/academic-research-skills/PaperOrchestra", "Third-party research stack that you must use as external method contracts"]), evidence=[str(SCRIPTS / "claude_project_session.py")]),
        check("third_party_web_exposed", source_contains("../web/backend/auto_research/web/project_bridge.py", ["third_party_research_stack", "third_party_stack_status"]), evidence=[str(ROOT / "web" / "backend" / "auto_research" / "web" / "project_bridge.py")]),
    ]
    return {
        "id": "native_method_contract_stack",
        "module": "native_method_contract_stack",
        "status": module_status(checks),
        "checks": checks,
        "metrics": {
            "source_count": int(summary.get("source_count", 0) or 0),
            "available_source_count": int(summary.get("available_source_count", 0) or 0),
            "selected_module_count": int(summary.get("selected_module_count", 0) or 0),
            "missing_module_count": int(summary.get("missing_module_count", 0) or 0),
            "synced_skill_count": int(summary.get("synced_skill_count", 0) or 0),
            "adapter_count": len(adapters),
            "external_module_count": len(modules),
        },
        "directly_used_patterns": {
            "ResearchDirectionManagement": ["research-pipeline", "novelty-check", "deep-research"],
            "EvolutionaryMemory": ["staged research roles", "memory", "recoverable exception handling"],
            "EvidenceAssurance": ["claim audit", "citation audit", "uncited assertion detection"],
            "TrajectoryOptimization": ["tool-error handling", "context-overflow handling", "async watching", "experiment queue"],
            "PaperProduction": ["section writing", "literature review", "plotting", "review/rating", "content refinement"],
        },
        "evidence_files": [str(stack_path), str(report_path), str(SCRIPTS / "sync_third_party_research_stack.py")],
        "note": "This module confirms optional method references are summarized while runtime prompts and UI expose only native capabilities.",
    }


def write_report(paths, payload: dict[str, Any]) -> Path:
    out = paths.reports / "research_trajectory_capability_audit.md"
    lines = ["# Research Trajectory Capability Audit\n\n"]
    lines.append(f"- updated_at: {payload.get('updated_at', '')}\n")
    lines.append(f"- capability_status: {payload.get('capability_status', '')}\n")
    lines.append(f"- research_gate_status: {payload.get('research_gate_status', '')}\n")
    lines.append(f"- evidence_integrity_status: {payload.get('evidence_integrity_status', '')}\n")
    lines.append(f"- overall_status: {payload.get('overall_status', '')}\n\n")
    for module in payload.get("modules", []):
        lines.append(f"## {module.get('module')}\n\n")
        lines.append(f"- status: {module.get('status')}\n")
        for key, value in (module.get("metrics", {}) if isinstance(module.get("metrics", {}), dict) else {}).items():
            lines.append(f"- {key}: {value}\n")
        lines.append("\nChecks:\n")
        for row in module.get("checks", []):
            lines.append(f"- [{row.get('severity')}] {row.get('name')} | {row.get('detail', '')} | evidence={row.get('evidence', [])}\n")
        lines.append("\n")
    out.write_text("".join(lines), encoding="utf-8")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit whether The workflow has the required long-horizon research trajectory capabilities.")
    parser.add_argument("--project", required=True)
    args = parser.parse_args()
    paths = build_paths(args.project)
    modules = [
        audit_direction_management(paths),
        audit_evolutionary_memory(paths),
        audit_assurance_layer(paths),
        audit_trajectory_system(paths),
        audit_skill_and_code_bindings(paths),
        audit_third_party_research_stack(paths),
        audit_end_to_end_verification(paths),
        audit_paper_orchestra_capability(paths),
    ]
    capability_status = module_status([{"status": module.get("status", "pass")} for module in modules])
    assurance = modules[2]
    research_gate_status = assurance.get("research_gate_status", "unknown")
    evidence_integrity_status = assurance.get("evidence_integrity_status", "unknown")
    if capability_status == "blocked":
        overall = "capability_blocked"
    elif research_gate_status == "blocked":
        overall = "operational_but_research_gate_blocked"
    elif capability_status == "warn" or evidence_integrity_status in {"warn", "blocked"}:
        overall = "operational_with_warnings"
    else:
        overall = "operational"
    payload = {
        "project": args.project,
        "updated_at": now_iso(),
        "capability_status": capability_status,
        "research_gate_status": research_gate_status,
        "evidence_integrity_status": evidence_integrity_status,
        "overall_status": overall,
        "modules": modules,
        "required_capabilities": [
            "research_direction_management",
            "evolutionary_memory",
            "research_assurance_layer",
            "trajectory_system",
            "skill_and_code_bindings",
            "third_party_research_stack",
            "end_to_end_verification",
            "paper_orchestra_writing_system",
        ],
        "principle": "This audit checks infrastructure and evidence discipline. A blocked research gate is a correct refusal to overclaim, not a reason to weaken evidence standards.",
    }
    save_json(paths.state / "research_trajectory_capability_audit.json", payload)
    report = write_report(paths, payload)
    print(json.dumps({"project": args.project, "overall_status": overall, "capability_status": capability_status, "report": str(report)}, ensure_ascii=False, indent=2))
    return 0 if capability_status != "blocked" else 1


if __name__ == "__main__":
    raise SystemExit(main())
