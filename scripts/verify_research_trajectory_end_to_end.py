#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

from project_paths import ROOT, build_paths

SCRIPTS = ROOT / "scripts"


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


def read_text(path: Path, limit: int = 500000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit] if path.exists() else ""
    except Exception:
        return ""


def list_count(payload: Any, key: str) -> int:
    value = payload.get(key, []) if isinstance(payload, dict) else []
    return len(value) if isinstance(value, list) else 0


def history_count(payload: Any, key: str = "history") -> int:
    value = payload.get(key, []) if isinstance(payload, dict) else []
    return len(value) if isinstance(value, list) else 0


def nested_get(payload: Any, *keys: str, default: Any = None) -> Any:
    current = payload
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return current if current is not None else default


def count_landscape_nodes(payload: Any) -> int:
    nodes = payload.get("nodes", {}) if isinstance(payload, dict) else {}
    if not isinstance(nodes, dict):
        return 0
    return sum(len(rows) for rows in nodes.values() if isinstance(rows, list))


def source_contains(path: Path, patterns: list[str]) -> bool:
    text = read_text(path)
    return bool(text) and all(pattern in text for pattern in patterns)


def source_omits(path: Path, patterns: list[str]) -> bool:
    text = read_text(path)
    return bool(text) and all(pattern not in text for pattern in patterns)


def check(name: str, ok: bool, *, severity: str = "block", evidence: Any = None, detail: str = "") -> dict[str, Any]:
    return {
        "id": name,
        "name": name,
        "status": "pass" if ok else severity,
        "severity": "pass" if ok else severity,
        "detail": detail,
        "evidence": evidence or [],
    }


def module_status(checks: list[dict[str, Any]]) -> str:
    if any(row.get("status") in {"block", "blocked"} or row.get("severity") in {"block", "blocked"} for row in checks):
        return "blocked"
    if any(row.get("status") in {"warn", "warning"} or row.get("severity") in {"warn", "warning"} for row in checks):
        return "warn"
    return "pass"


def module_payload(module_id: str, checks: list[dict[str, Any]], metrics: dict[str, Any] | None = None, evidence_files: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": module_id,
        "module": module_id,
        "status": module_status(checks),
        "checks": checks,
        "metrics": metrics or {},
        "evidence_files": evidence_files or [],
    }


def verify_required_state(paths) -> dict[str, Any]:
    required = [
        "research_landscape.json",
        "novelty_map.json",
        "failed_hypothesis_graph.json",
        "unexplored_niche_graph.json",
        "research_assurance_layer.json",
        "research_memory.json",
        "research_direction_memory.json",
        "research_evidence_integrity.json",
        "research_evidence_manifest.json",
        "research_graph_history.json",
        "research_landscape_assessment.json",
        "evolutionary_memory_ledger.json",
        "trajectory_optimization_plan.json",
        "trajectory_execution_protocol.json",
        "trajectory_checkpoints.json",
        "evolutionary_memory_index.json",
        "recoverable_cycle_summary.json",
        "research_skill_contracts.json",
    ]
    missing = [name for name in required if not (paths.state / name).exists()]
    landscape = load_json(paths.state / "research_landscape.json", {})
    novelty = load_json(paths.state / "novelty_map.json", {})
    failed = load_json(paths.state / "failed_hypothesis_graph.json", {})
    niches = load_json(paths.state / "unexplored_niche_graph.json", {})
    checks = [
        check("required_state_files_exist", not missing, evidence=[str(paths.state / name) for name in required], detail="missing=" + ", ".join(missing)),
        check("landscape_nodes_available", count_landscape_nodes(landscape) > 0, severity="warn", evidence=[str(paths.state / "research_landscape.json")], detail=f"nodes={count_landscape_nodes(landscape)}"),
        check("novelty_map_list", isinstance(novelty.get("nodes", []), list), evidence=[str(paths.state / "novelty_map.json")], detail=f"nodes={list_count(novelty, 'nodes')}"),
        check("failed_hypothesis_list", isinstance(failed.get("nodes", []), list), evidence=[str(paths.state / "failed_hypothesis_graph.json")], detail=f"nodes={list_count(failed, 'nodes')}"),
        check("unexplored_niche_list", isinstance(niches.get("nodes", []), list), evidence=[str(paths.state / "unexplored_niche_graph.json")], detail=f"nodes={list_count(niches, 'nodes')}"),
    ]
    return module_payload("persistent_state_files", checks, {"required_files": len(required), "missing_files": len(missing), "landscape_nodes": count_landscape_nodes(landscape)})


def verify_direction(paths) -> dict[str, Any]:
    graph_history = load_json(paths.state / "research_graph_history.json", {})
    assessment = load_json(paths.state / "research_landscape_assessment.json", {})
    direction = load_json(paths.state / "research_direction_memory.json", {})
    novelty = load_json(paths.state / "novelty_map.json", {})
    failed = load_json(paths.state / "failed_hypothesis_graph.json", {})
    niches = load_json(paths.state / "unexplored_niche_graph.json", {})
    checks = [
        check("graph_history_has_entries", history_count(graph_history) > 0 or int(graph_history.get("history_count", 0) or 0) > 0, evidence=[str(paths.state / "research_graph_history.json")], detail=f"entries={graph_history.get('history_count', history_count(graph_history))}"),
        check("graph_history_has_hash", bool(nested_get(graph_history, "latest", "snapshot_hash", default="")), evidence=[str(paths.state / "research_graph_history.json")]),
        check("landscape_assessment_recorded", bool(assessment.get("status")), evidence=[str(paths.state / "research_landscape_assessment.json")], detail=f"status={assessment.get('status', '')}"),
        check("direction_memory_persisted", history_count(direction) > 0, evidence=[str(paths.state / "research_direction_memory.json")], detail=f"entries={history_count(direction)}"),
        check("novelty_failed_niche_graphs_maintained", all(isinstance(payload.get("nodes", []), list) for payload in [novelty, failed, niches]), evidence=[str(paths.state / "novelty_map.json"), str(paths.state / "failed_hypothesis_graph.json"), str(paths.state / "unexplored_niche_graph.json")]),
    ]
    return module_payload("research_direction_management_e2e", checks, {
        "direction_memory_entries": history_count(direction),
        "graph_history_entries": int(graph_history.get("history_count", 0) or history_count(graph_history)),
        "landscape_assessment_status": assessment.get("status", ""),
        "novelty_nodes": list_count(novelty, "nodes"),
        "failed_hypotheses": list_count(failed, "nodes"),
        "unexplored_niches": list_count(niches, "nodes"),
    })


def verify_memory(paths) -> dict[str, Any]:
    memory = load_json(paths.state / "research_memory.json", {})
    ledger = load_json(paths.state / "evolutionary_memory_ledger.json", {})
    index = load_json(paths.state / "evolutionary_memory_index.json", {})
    evo_cycle = load_json(paths.state / "recoverable_cycle_summary.json", load_json(paths.state / "evoscientist_cycle_summary.json", {}))
    counts = nested_get(ledger, "latest", "counts", default={})
    family_keys = ["ideation_memory", "experimentation_memory", "assurance_memory", "trajectory_memory"]
    checks = [
        check("research_memory_file", (paths.state / "research_memory.json").exists(), evidence=[str(paths.state / "research_memory.json")]),
        *[check(f"{key}_persisted", history_count(memory, key) > 0, evidence=[str(paths.state / "research_memory.json")], detail=f"entries={history_count(memory, key)}") for key in family_keys],
        check("memory_ledger_has_entries", history_count(ledger) > 0 or int(ledger.get("history_count", 0) or 0) > 0, evidence=[str(paths.state / "evolutionary_memory_ledger.json")], detail=f"entries={ledger.get('history_count', history_count(ledger))}"),
        check("memory_ledger_counts_memory_families", all(int(counts.get(name.replace('_memory', '_entries'), 0) or 0) > 0 for name in family_keys), evidence=[str(paths.state / "evolutionary_memory_ledger.json")], detail=json.dumps(counts, ensure_ascii=False)),
        check("evolutionary_index_has_items", int(index.get("indexed_item_count", 0) or 0) > 0, evidence=[str(paths.state / "evolutionary_memory_index.json")], detail=f"items={index.get('indexed_item_count', 0)}"),
        check("recoverable_cycle_has_phases", int(evo_cycle.get("phase_count", 0) or 0) > 0, severity="warn", evidence=[str(paths.state / "recoverable_cycle_summary.json")], detail=f"phases={evo_cycle.get('phase_count', 0)}"),
    ]
    return module_payload("evolutionary_memory_e2e", checks, {
        "ideation_entries": history_count(memory, "ideation_memory"),
        "experimentation_entries": history_count(memory, "experimentation_memory"),
        "assurance_entries": history_count(memory, "assurance_memory"),
        "trajectory_entries": history_count(memory, "trajectory_memory"),
        "evolutionary_memory_ledger_entries": int(ledger.get("history_count", 0) or history_count(ledger)),
        "evolutionary_index_items": int(index.get("indexed_item_count", 0) or 0),
        "evo_phase_count": int(evo_cycle.get("phase_count", 0) or 0),
    })


def verify_assurance(paths) -> dict[str, Any]:
    assurance = load_json(paths.state / "research_assurance_layer.json", {})
    integrity = load_json(paths.state / "research_evidence_integrity.json", {})
    manifest = load_json(paths.state / "research_evidence_manifest.json", {})
    claim_ledger = load_json(paths.state / "claim_ledger.json", {})
    weak_claims = manifest.get("weak_or_unsupported_claims", []) if isinstance(manifest.get("weak_or_unsupported_claims", []), list) else []
    ledger_weak = [row for row in claim_ledger.get("claims", []) if isinstance(row, dict) and str(row.get("status", "")).lower() in {"weak", "unsupported"}] if isinstance(claim_ledger, dict) else []
    missing_local = manifest.get("missing_local_refs", []) if isinstance(manifest.get("missing_local_refs", []), list) else []
    checks = [
        check("assurance_layer_file", (paths.state / "research_assurance_layer.json").exists(), evidence=[str(paths.state / "research_assurance_layer.json")]),
        check("evidence_integrity_checked_nodes", int(integrity.get("checked_nodes", 0) or 0) > 0, evidence=[str(paths.state / "research_evidence_integrity.json")], detail=f"checked={integrity.get('checked_nodes', 0)}"),
        check("evidence_manifest_has_refs", int(manifest.get("ref_count", 0) or 0) > 0, evidence=[str(paths.state / "research_evidence_manifest.json")], detail=f"refs={manifest.get('ref_count', 0)}"),
        check("weak_claims_block_assurance", not weak_claims or assurance.get("status") == "blocked", evidence=[str(paths.state / "research_assurance_layer.json"), str(paths.state / "research_evidence_manifest.json")], detail=f"weak_claims={len(weak_claims)} assurance={assurance.get('status', '')}"),
        check("missing_local_refs_not_promoted", not missing_local or integrity.get("status") in {"blocked", "warn"}, evidence=[str(paths.state / "research_evidence_integrity.json"), str(paths.state / "research_evidence_manifest.json")], detail=f"missing={len(missing_local)} integrity={integrity.get('status', '')}"),
        check("claim_ledger_weak_claims_blocked", not ledger_weak or assurance.get("status") == "blocked", evidence=[str(paths.state / "claim_ledger.json"), str(paths.state / "research_assurance_layer.json")], detail=f"ledger_weak={len(ledger_weak)} assurance={assurance.get('status', '')}"),
    ]
    return module_payload("research_assurance_layer_e2e", checks, {
        "assurance_status": assurance.get("status", ""),
        "evidence_integrity_status": integrity.get("status", ""),
        "evidence_manifest_ref_count": int(manifest.get("ref_count", 0) or 0),
        "weak_or_unsupported_claim_count": len(weak_claims),
        "missing_local_refs": len(missing_local),
    })


def verify_trajectory(paths) -> dict[str, Any]:
    protocol = load_json(paths.state / "trajectory_execution_protocol.json", {})
    plan = load_json(paths.state / "trajectory_optimization_plan.json", {})
    checkpoints = load_json(paths.state / "trajectory_checkpoints.json", {})
    supervisor = load_json(paths.state / "trajectory_supervisor_state.json", {})
    loop_steps = protocol.get("loop_steps", []) if isinstance(protocol.get("loop_steps", []), list) else []
    worker_reads = nested_get(protocol, "worker_contract", "required_context_reads", default=[])
    queue = plan.get("queue", []) if isinstance(plan.get("queue", []), list) else []
    rounds = supervisor.get("rounds", []) if isinstance(supervisor.get("rounds", []), list) else []
    latest = supervisor.get("latest", {}) if isinstance(supervisor.get("latest", {}), dict) else {}
    latest_id = nested_get(latest, "queue_item", "id", default="")
    queue_ids = {str(row.get("id")) for row in queue if isinstance(row, dict)}
    dry_run_preserved = latest.get("status") != "dry_run_recorded" or (bool(latest_id) and str(latest_id) in queue_ids)
    supervisor_source = SCRIPTS / "run_research_trajectory_supervisor.py"
    claude_source = SCRIPTS / "claude_project_session.py"
    checks = [
        check("execution_protocol_loop", len(loop_steps) >= 5, evidence=[str(paths.state / "trajectory_execution_protocol.json")], detail=f"steps={len(loop_steps)}"),
        check("worker_contract_reads_long_horizon_state", all(name in "\n".join(str(x) for x in worker_reads) for name in ["research_graph_history", "research_evidence_manifest", "evolutionary_memory_ledger", "trajectory_checkpoints"]), evidence=[str(paths.state / "trajectory_execution_protocol.json")]),
        check("optimization_queue_available", len(queue) > 0, severity="warn", evidence=[str(paths.state / "trajectory_optimization_plan.json")], detail=f"queue={len(queue)}"),
        check("checkpoint_history_available", int(checkpoints.get("checkpoint_count", 0) or 0) > 0, evidence=[str(paths.state / "trajectory_checkpoints.json")], detail=f"checkpoints={checkpoints.get('checkpoint_count', 0)}"),
        check("supervisor_round_history", len(rounds) > 0, severity="warn", evidence=[str(paths.state / "trajectory_supervisor_state.json")], detail=f"rounds={len(rounds)}"),
        check("dry_run_does_not_complete_queue", dry_run_preserved, evidence=[str(paths.state / "trajectory_supervisor_state.json"), str(paths.state / "trajectory_optimization_plan.json")], detail=f"latest={latest.get('status', '')} queue_item={latest_id}"),
        check("supervisor_multi_round_entrypoint", source_contains(supervisor_source, ["--rounds", "for local_round", "claude_project_session.py", "post_rebuild_return_code"]), evidence=[str(supervisor_source)]),
        check("long_running_claude_timeout_supported", source_contains(claude_source, ["CLAUDE_SESSION_TIMEOUT_SEC", "14400", "effective_timeout <= 0", "deadline =", "else None"]), evidence=[str(claude_source)], detail="default 14400s and timeout<=0 disables deadline"),
    ]
    return module_payload("trajectory_system_e2e", checks, {
        "loop_steps": len(loop_steps),
        "optimization_queue_size": len(queue),
        "checkpoint_count": int(checkpoints.get("checkpoint_count", 0) or 0),
        "supervisor_rounds": len(rounds),
        "latest_supervisor_status": latest.get("status", ""),
    })


def verify_skills_and_prompts(paths) -> dict[str, Any]:
    skill_root = ROOT / ".claude" / "skills"
    required = ["experiment-loop", "evidence-gate", "writing"]
    skill_paths = [skill_root / name / "SKILL.md" for name in required]
    skill_texts = {path.parent.name: read_text(path) for path in skill_paths}
    contracts = load_json(paths.state / "research_skill_contracts.json", [])
    rich_terms = ["native", "trajectory", "evidence", "memory", "prune", "repair"]
    rich = all(len(text) >= 1200 and all(term.lower() in text.lower() for term in rich_terms) for text in skill_texts.values())
    source_name_leaks = ["source-style", "source-style", "TASTE-writing-style", "Use external source names in public Workflow prompts"]
    checks = [
        check("skill_contracts_exist", all(path.exists() for path in skill_paths), evidence=[str(path) for path in skill_paths]),
        check("skill_contracts_rich", rich, evidence=[str(path) for path in skill_paths], detail="requires rich native/evidence/memory/trajectory/prune/repair contracts"),
        check("runtime_prompts_use_native_names", source_contains(SCRIPTS / "claude_project_session.py", ["native method capability contracts", "research-direction", "evolutionary-memory", "evidence-assurance", "trajectory-optimization", "paper-production"]) and source_omits(SCRIPTS / "claude_project_session.py", source_name_leaks), evidence=[str(SCRIPTS / "claude_project_session.py")]),
        check("core_skills_do_not_expose_source_agents", all(all(term not in text for term in source_name_leaks) for text in skill_texts.values()), evidence=[str(path) for path in skill_paths]),
        check("skill_contracts_exported", isinstance(contracts, list) and len(contracts) >= len(required), evidence=[str(paths.state / "research_skill_contracts.json")], detail=f"contracts={len(contracts) if isinstance(contracts, list) else 'n/a'}"),
        check("claude_prompt_reads_long_horizon_assets", source_contains(SCRIPTS / "claude_project_session.py", ["research_graph_history", "research_evidence_manifest", "evolutionary_memory_ledger", "Optimize the whole trajectory", ".claude/skills"]), evidence=[str(SCRIPTS / "claude_project_session.py")]),
        check("coding_agent_reads_trajectory_assets", source_contains(SCRIPTS / "run_coding_agent.py", ["research_evidence_manifest", "research_graph_history", "evolutionary_memory_ledger", "research_trajectory_capability_audit"]), evidence=[str(SCRIPTS / "run_coding_agent.py")]),
    ]
    return module_payload("skills_and_prompt_context_e2e", checks, {
        "skill_contract_count": len([path for path in skill_paths if path.exists()]),
        "exported_skill_contracts": len(contracts) if isinstance(contracts, list) else 0,
        "skill_contract_chars": sum(len(text) for text in skill_texts.values()),
    })


def verify_third_party_stack(paths) -> dict[str, Any]:
    stack_path = paths.state / "third_party_research_stack.json"
    report_path = paths.reports / "third_party_research_stack.md"
    stack = load_json(stack_path, {})
    summary = stack.get("summary", {}) if isinstance(stack.get("summary", {}), dict) else {}
    sources = stack.get("sources", []) if isinstance(stack.get("sources", []), list) else []
    contracts = load_json(paths.state / "research_skill_contracts.json", [])
    contract_names = {str(row.get("name") or "") for row in contracts if isinstance(row, dict)}
    source_names = {str(row.get("name") or "") for row in sources if isinstance(row, dict) and row.get("available")}
    bridge = ROOT / "modules" / "taste" / "auto_research" / "web" / "project_bridge.py"
    app = ROOT / "modules" / "taste" / "auto_research" / "web" / "client" / "src" / "App.tsx"
    checks = [
        check("third_party_stack_file", stack_path.exists(), evidence=[str(stack_path)]),
        check("third_party_stack_report", report_path.exists(), evidence=[str(report_path)]),
        check("third_party_stack_ready", stack.get("status") == "ready", evidence=[str(stack_path)], detail=f"status={stack.get('status', '')}"),
        check("third_party_sources_cover_required_repos", {"ARIS", "EvoScientist", "academic-research-skills", "PaperOrchestra"} <= source_names, evidence=[str(stack_path)], detail=f"sources={sorted(source_names)}"),
        check("third_party_modules_selected", int(summary.get("selected_module_count", 0) or 0) >= 40, evidence=[str(stack_path)], detail=f"modules={summary.get('selected_module_count', 0)}"),
        check("third_party_skill_adapters_synced", int(summary.get("synced_skill_count", 0) or 0) >= 25, evidence=[str(stack_path)], detail=f"skills={summary.get('synced_skill_count', 0)}"),
        check("runtime_prompt_uses_native_method_context", source_contains(SCRIPTS / "claude_project_session.py", ["third_party_research_stack", "native method capability contracts"]), evidence=[str(SCRIPTS / "claude_project_session.py")]),
        check("trajectory_builder_uses_native_method_context", source_contains(SCRIPTS / "build_research_trajectory_system.py", ["sync_third_party_research_stack.py", "third_party_research_stack", "ResearchDirectionManagement", "EvidenceAssurance", "TrajectoryOptimization", "PaperProduction"]), evidence=[str(SCRIPTS / "build_research_trajectory_system.py")]),
        check("third_party_web_api_bound", source_contains(bridge, ["third_party_research_stack", "third_party_stack_status", "third_party_synced_skill_count"]), evidence=[str(bridge)]),
        check("third_party_web_ui_bound", source_contains(app, ["thirdPartyStack", "thirdPartyResearchStack", "thirdPartySources"]), evidence=[str(app)]),
    ]
    return module_payload("third_party_stack_e2e", checks, {
        "source_count": int(summary.get("source_count", 0) or 0),
        "selected_module_count": int(summary.get("selected_module_count", 0) or 0),
        "synced_skill_count": int(summary.get("synced_skill_count", 0) or 0),
        "exported_skill_contracts": len(contract_names),
    }, [str(stack_path), str(report_path)])


def verify_web_visibility(paths) -> dict[str, Any]:
    bridge = ROOT / "modules" / "taste" / "auto_research" / "web" / "project_bridge.py"
    app = ROOT / "modules" / "taste" / "auto_research" / "web" / "client" / "src" / "App.tsx"
    checks = [
        check("api_exposes_verification", source_contains(bridge, ["research_trajectory_end_to_end_verification", "end_to_end_verification_status"]), evidence=[str(bridge)]),
        check("ui_renders_verification", source_contains(app, ["endToEndVerification", "research_trajectory_end_to_end_verification"]), evidence=[str(app)]),
    ]
    return module_payload("web_visibility_e2e", checks, {})


def verify_paper_orchestra_writing(paths) -> dict[str, Any]:
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
        check("real_paper_orchestra_bridge_script", (SCRIPTS / "run_paper_orchestra_bridge.py").exists(), evidence=[str(SCRIPTS / "run_paper_orchestra_bridge.py")]),
        check("paper_normality_audit_script", (SCRIPTS / "audit_paper_normality.py").exists(), evidence=[str(SCRIPTS / "audit_paper_normality.py")]),
        check("bridge_clones_real_paper_orchestra", source_contains(SCRIPTS / "run_paper_orchestra_bridge.py", ["Ar9av/PaperOrchestra", "paper-orchestra", "claude_project_session.py", "workspace/inputs"]), evidence=[str(SCRIPTS / "run_paper_orchestra_bridge.py")]),
        check("preview_requires_normality_gate", source_contains(SCRIPTS / "build_conference_preview_paper.py", ["audit_paper_normality.py", "does not", "normal_preview_ready"]), evidence=[str(SCRIPTS / "build_conference_preview_paper.py")]),
        check("paper_orchestra_state_file", state_path.exists(), severity="warn", evidence=[str(state_path)]),
        check("paper_orchestra_section_ledger", len(sections) >= 8, severity="warn", evidence=[str(state_path)], detail=f"sections={len(sections)}"),
        check("paper_orchestra_tracks_structured_inputs", all(key in state for key in ["structured_inputs", "claims", "citations", "artifacts", "revision_queue"]), severity="warn", evidence=[str(state_path)]),
        check("paper_orchestra_bridge_state_file", bridge_path.exists(), severity="warn", evidence=[str(bridge_path)]),
        check("paper_orchestra_bridge_records_source_repo", bridge_state.get("source", {}).get("repository") == "https://github.com/Ar9av/PaperOrchestra.git" if isinstance(bridge_state.get("source", {}), dict) else False, severity="warn", evidence=[str(bridge_path)]),
        check("paper_normality_state_file", normality_path.exists(), severity="warn", evidence=[str(normality_path)]),
        check("submission_readiness_file", readiness_path.exists(), severity="warn", evidence=[str(readiness_path)]),
        check("submission_readiness_checks_exist", isinstance(readiness.get("checks", []), list) and len(readiness.get("checks", [])) >= 10, severity="warn", evidence=[str(readiness_path)], detail=f"checks={len(readiness.get('checks', [])) if isinstance(readiness.get('checks', []), list) else 0}"),
        check("readiness_blocks_unsupported_papers", readiness.get("status") != "submission_ready" or not readiness.get("blockers"), evidence=[str(readiness_path)], detail=f"status={readiness.get('status', '')} blockers={len(readiness.get('blockers', [])) if isinstance(readiness.get('blockers', []), list) else 0}"),
        check("pipeline_integrates_orchestra_state", source_contains(SCRIPTS / "run_paper_pipeline.py", ["run_paper_orchestra_bridge.py", "build_paper_orchestra_state.py", "audit_submission_readiness.py", "submission_ready"]), evidence=[str(SCRIPTS / "run_paper_pipeline.py")]),
        check("trajectory_exports_paper_production", source_contains(SCRIPTS / "build_research_trajectory_system.py", ["paper_orchestra_state", "submission_readiness", "TASTE Paper Production System"]), evidence=[str(SCRIPTS / "build_research_trajectory_system.py")]),
    ]
    return module_payload("paper_orchestra_writing_e2e", checks, {
        "section_count": len(sections),
        "paper_orchestra_state_status": state.get("status", "") if isinstance(state, dict) else "",
        "submission_readiness_status": readiness.get("status", "") if isinstance(readiness, dict) else "",
        "submission_ready": bool(readiness.get("submission_ready")) if isinstance(readiness, dict) else False,
    }, [str(state_path), str(readiness_path), str(audit_path), str(bridge_path), str(normality_path), str(pipeline_path)])


def write_report(paths, payload: dict[str, Any]) -> Path:
    out = paths.reports / "research_trajectory_end_to_end_verification.md"
    lines = ["# Research Trajectory End-to-End Verification\n\n"]
    for key in ["updated_at", "capability_status", "research_gate_status", "evidence_integrity_status", "overall_status", "total_checks", "passed_checks", "warning_checks", "failed_checks"]:
        lines.append(f"- {key}: {payload.get(key, '')}\n")
    lines.append("\n## Modules\n")
    for module in payload.get("modules", []):
        lines.append(f"\n### {module.get('module')}\n\n")
        lines.append(f"- status: {module.get('status')}\n")
        metrics = module.get("metrics", {}) if isinstance(module.get("metrics", {}), dict) else {}
        for key, value in metrics.items():
            lines.append(f"- {key}: {value}\n")
        lines.append("\nChecks:\n")
        for row in module.get("checks", []):
            lines.append(f"- [{row.get('severity')}] {row.get('name')} | {row.get('detail', '')} | evidence={row.get('evidence', [])}\n")
    out.write_text("".join(lines), encoding="utf-8")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify TASTE long-horizon research trajectory capabilities end to end.")
    parser.add_argument("--project", required=True)
    args = parser.parse_args()
    paths = build_paths(args.project)
    modules = [
        verify_required_state(paths),
        verify_direction(paths),
        verify_memory(paths),
        verify_assurance(paths),
        verify_trajectory(paths),
        verify_skills_and_prompts(paths),
        verify_third_party_stack(paths),
        verify_web_visibility(paths),
        verify_paper_orchestra_writing(paths),
    ]
    checks = [row for module in modules for row in module.get("checks", [])]
    failed = [row for row in checks if row.get("severity") in {"block", "blocked"} or row.get("status") in {"block", "blocked"}]
    warnings = [row for row in checks if row.get("severity") in {"warn", "warning"} or row.get("status") in {"warn", "warning"}]
    passed = [row for row in checks if row.get("status") == "pass"]
    capability_status = module_status([{"status": module.get("status", "pass")} for module in modules])
    assurance = load_json(paths.state / "research_assurance_layer.json", {})
    integrity = load_json(paths.state / "research_evidence_integrity.json", {})
    research_gate_status = assurance.get("status", "unknown") if isinstance(assurance, dict) else "unknown"
    evidence_integrity_status = integrity.get("status", "unknown") if isinstance(integrity, dict) else "unknown"
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
        "total_checks": len(checks),
        "passed_checks": len(passed),
        "warning_checks": len(warnings),
        "failed_checks": len(failed),
        "modules_checked": len(modules),
        "modules": modules,
        "failed_check_ids": [row.get("id") for row in failed],
        "warning_check_ids": [row.get("id") for row in warnings],
        "principle": "This verifier checks that long-horizon TASTE capabilities are wired end to end. A blocked research gate remains correct when evidence is weak or missing.",
    }
    save_json(paths.state / "research_trajectory_end_to_end_verification.json", payload)
    report = write_report(paths, payload)
    print(json.dumps({"project": args.project, "overall_status": overall, "capability_status": capability_status, "report": str(report)}, ensure_ascii=False, indent=2))
    return 0 if capability_status != "blocked" else 1


if __name__ == "__main__":
    raise SystemExit(main())
