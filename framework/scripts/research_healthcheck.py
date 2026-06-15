#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from llm_client import llm_available, llm_disabled_reason
from project_paths import build_paths, load_project_config
from pipeline_guard import current_environment_selection as guard_current_environment_selection
from paper_common import get_active_paper_state


def find_cli_binary(cfg: dict, name: str) -> str:
    import glob
    import os
    import subprocess
    hints = [os.environ.get(f"{name.upper()}_BIN", "")]
    agent_cfg = cfg.get("coding_agent", {}) if isinstance(cfg, dict) else {}
    hints.append(str(agent_cfg.get(f"{name}_path_hint", "") or ""))
    found = shutil.which(name)
    if found:
        hints.append(found)
    try:
        proc = subprocess.run(["bash", "-ic", "printf %s \"$PATH\""], text=True, capture_output=True, timeout=10)
        if proc.returncode == 0:
            for part in proc.stdout.split(":"):
                if part:
                    hints.append(str(Path(part) / name))
    except Exception:
        pass
    root = Path(__file__).resolve().parents[1]
    hints.extend([
        str(root.parent / ".nvm" / "versions" / "node" / "*" / "bin" / name),
        str(Path.home() / ".nvm" / "versions" / "node" / "*" / "bin" / name),
    ])
    expanded = []
    for item in hints:
        if item and "*" in item:
            expanded.extend(glob.glob(item))
        elif item:
            expanded.append(item)
    seen = set()
    for item in expanded:
        if item in seen or not Path(item).exists():
            continue
        seen.add(item)
        env = os.environ.copy()
        env["PATH"] = str(Path(item).parent) + os.pathsep + env.get("PATH", "")
        proc = subprocess.run([item, "--version"], text=True, capture_output=True, env=env)
        if proc.returncode == 0:
            return item
    return ""


def find_claude(cfg: dict) -> str:
    return find_cli_binary(cfg, "claude")


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _state_run_id(row: dict) -> str:
    row = _as_dict(row)
    for key in ("fresh_find_run_id", "current_find_run_id", "run_id", "taste_run_id"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _accepted_repo_selection(row: dict) -> bool:
    row = _as_dict(row)
    selected = _as_dict(row.get("selected"))
    gate = str(row.get("selection_gate") or "").strip()
    action = str(row.get("current_action") or row.get("status") or "").strip().lower()
    return bool(
        selected
        and (
            gate.startswith(("accepted_by_claude", "accepted_by_deterministic_base_switch_gate"))
            or action in {"complete", "completed", "selected", "done"}
        )
    )


def _pending_candidate_blocked(selection: dict) -> bool:
    selection = _as_dict(selection)
    return str(selection.get("selection_gate") or "").strip() == "blocked_pending_data_loader_for_claude_best_candidate"


def _environment_selection_status(selection: dict, current_find_plan: dict, current_env: dict | None = None) -> str:
    env = _as_dict(current_env)
    if env.get("valid"):
        return "selected"
    if env:
        reason = str(env.get("reason") or "").strip()
        if reason:
            return reason
    plan = _as_dict(current_find_plan)
    return str(plan.get("base_selection_status") or plan.get("next_required_action") or "not-run").strip() or "not-run"


def _public_summary(row: dict) -> str:
    row = _as_dict(row)
    return str(row.get("summary_zh") or row.get("summary") or row.get("human_summary") or "").strip()


def _failed_check_ids(gate: dict) -> list[str]:
    gate = _as_dict(gate)
    failed = gate.get("failed_checks") if isinstance(gate.get("failed_checks"), list) else []
    if not failed and isinstance(gate.get("checks"), list):
        failed = [row for row in gate.get("checks", []) if isinstance(row, dict) and row.get("status") != "pass"]
    return [str(row.get("id") or "").strip() for row in failed if isinstance(row, dict) and str(row.get("id") or "").strip()]


def _route_has_identity(route: dict) -> bool:
    route = _as_dict(route)
    return any(str(route.get(key) or "").strip() for key in ["repo", "title", "repo_path", "proposed_path_hint"])


def _selected_base_viability_public_status(gate: dict, base_switch_gate: dict | None = None) -> dict:
    gate = _as_dict(gate)
    base_switch = _as_dict(base_switch_gate)
    status = str(gate.get("status") or "").strip().lower()
    decision = str(gate.get("decision") or "").strip().lower()
    if status != "blocked" or decision not in {"base_switch_gate_required", "continue_experiment_evidence_repair"}:
        return {}
    semantic = _as_dict(gate.get("semantic_data_provenance_review"))
    text_meta = _as_dict(semantic.get("text_metadata_provenance"))
    dataset = str(text_meta.get("dataset") or gate.get("dataset") or gate.get("selected_dataset") or "").strip()
    text_evidence_value = text_meta.get("has_text_metadata_evidence")
    semantic_required = bool(
        semantic.get("deterministic_gate_required")
        or (
            str(semantic.get("status") or "").strip().lower() == "blocked"
            and bool(semantic.get("project_requires_llm_semantics"))
            and not bool(semantic.get("has_real_llm_embedding_evidence"))
            and text_evidence_value is False
        )
    )
    issue = str(gate.get("issue") or "").strip()
    base_switch_status = str(base_switch.get("status") or "").strip().lower()
    base_switch_decision = str(base_switch.get("decision") or "").strip().lower()
    failed_ids = _failed_check_ids(base_switch)
    candidate_present = _route_has_identity(_as_dict(base_switch.get("candidate_route")))
    base_switch_not_authorized = base_switch_status == "blocked" and base_switch_decision == "base_switch_not_authorized"
    base_switch_fields = {
        "base_switch_gate_status": base_switch_status,
        "base_switch_gate_decision": base_switch_decision,
        "base_switch_candidate_route_present": candidate_present,
        "base_switch_failed_checks": failed_ids[:10],
    }
    if semantic_required:
        summary = issue or (
            "selected_base_viability_gate: 参考复现已通过，但当前 selected-base 数据路线缺少 LLM/text-semantic "
            "实验所需的可审计文本/元数据 provenance；继续运行纯行为或损失级候选实验无法清除此门控。"
        )
        next_action = (
            "运行 deterministic base-switch / semantic-provenance gate；候选路线保持 proposal-only，或补齐当前路线保存 ID "
            "映射的原始文本/元数据 provenance 与 artifact-local LLM/text embedding probe。通过前不继续纯行为级候选实验、不写论文、不提升结论。"
        )
        if base_switch_not_authorized:
            missing_candidate = "candidate_route_proposal_exists" in failed_ids or not candidate_present
            if missing_candidate:
                summary = (
                    "selected_base_viability_gate: 参考复现已通过，但当前 selected-base 数据路线缺少 LLM/text-semantic "
                    "文本/元数据 provenance；确定性 base-switch gate 已执行但未授权，因为还没有独立、可审计、可追溯到当前 "
                    "Find/read 的候选路线 proposal。继续运行纯行为或损失级候选实验无法清除此门控。"
                )
                next_action = (
                    "补齐当前路线保存 ID 映射的原始文本/元数据 provenance 与 artifact-local LLM/text embedding probe，"
                    "或生成可追溯到当前 Find/read 的 candidate base-switch proposal，并完成 loader/data/protocol/smoke/"
                    "full-reference/artifact-local audits。通过前不继续纯行为级候选实验、不写论文、不提升结论。"
                )
            else:
                failed_text = "、".join(failed_ids[:5]) or "候选路线证据"
                summary = (
                    "selected_base_viability_gate: 参考复现已通过，但当前 selected-base 数据路线仍缺少 LLM/text-semantic "
                    "文本/元数据 provenance；确定性 base-switch gate 已执行且未授权；"
                    f"候选路线仍有未通过检查：{failed_text}。"
                )
                next_action = (
                    "补齐当前路线保存 ID 映射的原始文本/元数据 provenance 与 artifact-local LLM/text "
                    "embedding probe；或补齐上列候选路线未通过检查后刷新 deterministic base-switch gate。"
                    "gate 通过前不切换基底、不写论文、不提升结论。"
                )
        return {
            "category": "semantic_data_provenance_required",
            "status": status,
            "decision": decision,
            "summary": summary,
            "next_action": next_action,
            "semantic_status": str(semantic.get("status") or "").strip(),
            "semantic_dataset": dataset,
            "semantic_has_text_metadata_evidence": text_evidence_value,
            "semantic_has_real_llm_embedding_evidence": bool(semantic.get("has_real_llm_embedding_evidence")),
            **base_switch_fields,
        }
    summary = issue or (
        "selected_base_viability_gate: 参考复现已通过；当前主线还缺少可审计、可写入论文的候选实验结果。"
    )
    return {
        "category": "experiment_evidence_audit",
        "status": status,
        "decision": decision,
        "summary": summary,
        "next_action": "等待项目代理读取当前缺口证据，并给出下一轮实验或修复动作。",
        "semantic_status": str(semantic.get("status") or "").strip(),
        "semantic_dataset": dataset,
        "semantic_has_text_metadata_evidence": text_evidence_value,
        "semantic_has_real_llm_embedding_evidence": bool(semantic.get("has_real_llm_embedding_evidence")),
        **base_switch_fields,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--venue", default="")
    args = parser.parse_args()

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    issues: list[str] = []
    notes: list[str] = []

    required = [
        paths.agents_file,
        paths.wiki_overview,
        paths.wiki_synthesis / "field-map.md",
        paths.wiki_synthesis / "shared-assumptions.md",
        paths.wiki_gaps / "confirmed-gaps.md",
        paths.wiki_gaps / "hypotheses.md",
        paths.wiki_gaps / "questions.md",
        paths.planning / "init_brief.md",
        paths.planning / "paper_quality.md",
        paths.planning / "workflow_blueprint.md",
        paths.reports / "shared_research.md",
        paths.reports / "workflow_connectivity.md",
        paths.reports / "machine_profile.md",
        paths.root / "paper" / "drafts" / "paper_draft.md",
        paths.root / "paper" / "reviews" / "paper_review_packet.md",
    ]
    for path in required:
        if not path.exists():
            issues.append(f"Missing {path.relative_to(paths.root)}")

    machine_profile = paths.reports / "machine_profile.json"
    if machine_profile.exists():
        profile = json.loads(machine_profile.read_text(encoding="utf-8"))
        deps = profile.get("dependencies", {}) if isinstance(profile, dict) else {}
        required_missing = deps.get("required_missing", []) if isinstance(deps, dict) else []
        recommended_missing = deps.get("recommended_missing", []) if isinstance(deps, dict) else []
        optional_missing = deps.get("optional_missing", []) if isinstance(deps, dict) else []
        if required_missing:
            issues.append(f"Missing required runtime dependencies: {', '.join(required_missing)}")
        if recommended_missing:
            notes.append(f"Recommended dependencies missing: {', '.join(recommended_missing)}")
        if optional_missing:
            notes.append(f"Optional dependencies missing: {', '.join(optional_missing)}")
        install_plan = paths.reports / "dependency_install_plan.md"
        if install_plan.exists():
            notes.append(f"Install guidance available at {install_plan.relative_to(paths.root)}")
    else:
        issues.append("Missing machine profile JSON; run detect_machine_profile.py")

    llm_ready = llm_available(cfg)
    if not llm_ready:
        notes.append(f"Generic LLM backend is not configured: {llm_disabled_reason(cfg)}")
        notes.append("Run framework/scripts/check_llm_ready.py --project <project> after setting LLM API configuration.")
    claude_path = find_claude(cfg)
    notes.append("Configured downstream coding backend: claude")
    notes.append(f"Claude backend available: {bool(claude_path)}{f' ({claude_path})' if claude_path else ''}")

    project_takeover_required = [paths.root / "AGENTS.md"]
    for path in project_takeover_required:
        if not path.exists():
            try:
                label = path.relative_to(paths.root)
            except ValueError:
                label = path
            issues.append(f"Missing project takeover file {label}")
    if paths.work_status.exists():
        notes.append("Framework-maintainer 工作状态.txt exists at workspace root; it is not project scientific memory.")

    if not load_json(paths.state / "repo_candidates.json") and cfg.get("repo_selection", {}).get("enabled", False):
        issues.append("No repo candidates registered")
    if not load_json(paths.state / "dataset_registry.json") and cfg.get("dataset_check", {}).get("enabled", False):
        issues.append("No datasets registered")
    if not load_json(paths.state / "experiment_registry.json"):
        notes.append("No experiments logged yet")
    if not (paths.state / "parallel_plan.json").exists():
        notes.append("No parallel experiment plan yet")
    if not (paths.state / "natural_language_requests.json").exists():
        notes.append("No natural-language run requests logged yet")
    taste_state = load_json(paths.state / "finding_frontend.json") if (paths.state / "finding_frontend.json").exists() else {}
    current_find_plan = load_json(paths.state / "current_find_research_plan.json") if (paths.state / "current_find_research_plan.json").exists() else {}
    repo_selection = load_json(paths.state / "evidence_ready_repo_selection.json") if (paths.state / "evidence_ready_repo_selection.json").exists() else {}
    full_cycle = load_json(paths.state / "full_research_cycle.json") if (paths.state / "full_research_cycle.json").exists() else {}
    scientific_progress_gate = load_json(paths.state / "scientific_progress_gate.json") if (paths.state / "scientific_progress_gate.json").exists() else {}
    selected_base_viability_gate = load_json(paths.state / "selected_base_viability_gate.json") if (paths.state / "selected_base_viability_gate.json").exists() else {}
    base_switch_gate = load_json(paths.state / "base_switch_gate.json") if (paths.state / "base_switch_gate.json").exists() else {}
    current_environment = guard_current_environment_selection(paths)
    taste_sync = load_json(paths.state / "taste_sync.json") if (paths.state / "taste_sync.json").exists() else {}
    taste_counts = taste_sync.get("counts", {}) if isinstance(taste_sync, dict) else {}
    if not taste_state and not current_find_plan:
        issues.append("The workflow has not been run for this project; run framework/scripts/run_frontend.py then sync_outputs.py")
    else:
        current_find_status = str(_as_dict(current_find_plan).get("status") or _as_dict(taste_state).get("status") or "unknown").strip()
        current_find_run_id = _state_run_id(current_find_plan) or _state_run_id(taste_state) or _state_run_id(full_cycle)
        notes.append(f"Current-Find downstream status: {current_find_status}{f' (run_id={current_find_run_id})' if current_find_run_id else ''}")
        environment_status = _environment_selection_status(repo_selection, current_find_plan, current_environment)
        if _pending_candidate_blocked(repo_selection) and _as_dict(selected_base_viability_gate):
            environment_status = "selected_current_route_pending_candidate_blocked"
        notes.append(f"Environment base selection: {environment_status}")
        full_cycle_status = str(_as_dict(full_cycle).get("status") or "").strip()
        selected_base_viability_status = _selected_base_viability_public_status(selected_base_viability_gate, base_switch_gate)
        if full_cycle_status:
            notes.append(f"Full-cycle status: {full_cycle_status}")
            full_cycle_summary = (selected_base_viability_status.get("summary") if selected_base_viability_status else "") or _public_summary(full_cycle)
            if full_cycle_summary:
                notes.append(f"Full-cycle summary: {full_cycle_summary}")
        scientific_progress_status = str(_as_dict(scientific_progress_gate).get("status") or "").strip()
        if scientific_progress_status:
            notes.append(f"Experiment evidence gate: {scientific_progress_status}")
        if selected_base_viability_status:
            notes.append(
                "Selected-base viability gate: "
                f"{selected_base_viability_status.get('category')} "
                f"({selected_base_viability_status.get('status')}/{selected_base_viability_status.get('decision')})"
            )
            if selected_base_viability_status.get("category") == "semantic_data_provenance_required":
                notes.append(
                    "Semantic data provenance: "
                    f"{selected_base_viability_status.get('semantic_status') or 'blocked'}; "
                    f"dataset={selected_base_viability_status.get('semantic_dataset') or 'unknown'}; "
                    f"text_metadata_evidence={selected_base_viability_status.get('semantic_has_text_metadata_evidence')}; "
                    f"real_llm_embedding_evidence={selected_base_viability_status.get('semantic_has_real_llm_embedding_evidence')}"
                )
                base_switch_status = selected_base_viability_status.get("base_switch_gate_status")
                if base_switch_status:
                    failed_text = ",".join(selected_base_viability_status.get("base_switch_failed_checks", [])[:8]) or "none"
                    notes.append(
                        "Base-switch gate: "
                        f"{base_switch_status}/{selected_base_viability_status.get('base_switch_gate_decision') or 'unknown'}; "
                        f"candidate_route_present={selected_base_viability_status.get('base_switch_candidate_route_present')}; "
                        f"failed_checks={failed_text}"
                    )
                notes.append(f"Current blocker summary: {selected_base_viability_status.get('summary')}")
        if isinstance(taste_state, dict) and taste_state.get("status") in {"timeout", "failed", "error"}:
            notes.append("The Find workflow is in a recoverable failure state; rerun after API/network/source repair and do not treat fallback as scientific evidence.")
    if not taste_sync:
        issues.append("Outputs have not been synchronized into research state")
    elif isinstance(taste_counts, dict):
        fallback_only = False
        taste_dir = paths.planning / "finding"
        find_results = load_json(taste_dir / "find_results.json") if (taste_dir / "find_results.json").exists() else {}
        if isinstance(find_results, dict):
            articles = find_results.get("articles", []) or []
            fallback_only = bool(articles) and all(str(row.get("source", "")) == "taste_recoverable_fallback" for row in articles if isinstance(row, dict))
        if fallback_only:
            notes.append("TASTE sync contains only recoverable fallback outputs; this keeps workflow connectivity but is not scientific literature evidence.")
        if int(taste_counts.get("ideas_synced", 0) or 0) == 0:
            notes.append("The workflow has not produced synced ideas yet; rerun TASTE before idea selection.")

    paper_state = get_active_paper_state(args.project, venue=args.venue)
    if not paper_state:
        notes.append("No paper pipeline state yet")
    else:
        if not paper_state.get("draft_ready"):
            notes.append("Paper draft not prepared yet")
        if not paper_state.get("paper_reviews_ready"):
            notes.append("Internal paper review aggregation not prepared yet")
        if not paper_state.get("author_response_ready"):
            notes.append("Author response packet not prepared yet")
        if not paper_state.get("re_review_ready"):
            notes.append("Re-review summary not prepared yet")
        if not paper_state.get("paper_revision_ready"):
            notes.append("Revised Markdown paper draft not prepared yet")
        if paper_state.get("paper_review_verdict") == "blocked":
            notes.append("Paper is currently blocked by internal review; keep improving Markdown before template promotion.")
        if paper_state.get("template_fetched") and not paper_state.get("pdf_ready"):
            notes.append("Venue template fetched but PDF not compiled yet")
        if paper_state.get("template_fetch_error"):
            notes.append(f"Template fetch is currently blocked: {paper_state.get('template_fetch_error')}")

    report = paths.reports / "healthcheck.md"
    lines = ["# Research Healthcheck\n\n"]
    if issues:
        lines.append("## Issues\n")
        for issue in issues:
            lines.append(f"- {issue}\n")
    else:
        lines.append("All key project components are present.\n")
    lines.append("\n## Notes\n")
    if notes:
        for note in notes:
            lines.append(f"- {note}\n")
    else:
        lines.append("- No additional notes.\n")
    report.write_text("".join(lines), encoding="utf-8")
    print(report)
    print(f"issues={len(issues)}")


if __name__ == "__main__":
    main()
