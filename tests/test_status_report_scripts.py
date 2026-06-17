from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from path_helpers import load_script


def _write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str = "ok") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_paths(tmp_path: Path) -> SimpleNamespace:
    root = tmp_path / "projects" / "demo_project"
    paths = SimpleNamespace(
        root=root,
        state=root / "state",
        reports=root / "reports",
        planning=root / "planning",
        discover=root / "discover",
        raw_papers=root / "raw_papers",
        wiki_papers=root / "wiki" / "papers",
        wiki_concepts=root / "wiki" / "concepts",
        wiki_entities=root / "wiki" / "entities",
        wiki_comparisons=root / "wiki" / "comparisons",
        wiki_synthesis=root / "wiki" / "synthesis",
        wiki_gaps=root / "wiki" / "gaps",
        wiki_overview=root / "wiki" / "overview.md",
        agents_file=root / "AGENTS.md",
        work_status=root / "工作状态.txt",
    )
    for directory in [
        paths.state,
        paths.reports,
        paths.planning / "finding",
        paths.discover,
        paths.raw_papers,
        paths.wiki_papers,
        paths.wiki_concepts,
        paths.wiki_entities,
        paths.wiki_comparisons,
        paths.wiki_synthesis,
        paths.wiki_gaps,
    ]:
        directory.mkdir(parents=True, exist_ok=True)
    return paths


def _seed_common_project(paths: SimpleNamespace) -> None:
    for path in [
        paths.agents_file,
        paths.wiki_overview,
        paths.wiki_synthesis / "field-map.md",
        paths.wiki_synthesis / "shared-assumptions.md",
        paths.wiki_gaps / "confirmed-gaps.md",
        paths.wiki_gaps / "questions.md",
        paths.planning / "init_brief.md",
        paths.planning / "paper_quality.md",
        paths.planning / "workflow_blueprint.md",
        paths.reports / "shared_research.md",
        paths.reports / "workflow_connectivity.md",
        paths.reports / "machine_profile.md",
        paths.root / "paper" / "drafts" / "paper_draft.md",
        paths.root / "paper" / "reviews" / "paper_review_packet.md",
    ]:
        _write_text(path)
    _write_text(paths.wiki_gaps / "hypotheses.md", "status: draft\n")
    _write_json(paths.reports / "machine_profile.json", {"dependencies": {"ready_for_core_loop": True, "ready_for_latex": True}})
    _write_json(paths.state / "repo_candidates.json", [])
    _write_json(paths.state / "dataset_registry.json", [])
    _write_json(paths.state / "experiment_registry.json", [])
    _write_json(paths.state / "natural_language_requests.json", [])
    _write_json(paths.state / "idea_candidates.json", {"summary": {"idea_count": 2, "pursue_count": 2}})
    _write_json(paths.state / "paper_quality.json", {"summary": {}})
    _write_json(paths.state / "loop_history.json", [])
    _write_json(paths.state / "ingested_ids.json", [])
    _write_json(paths.state / "compiled_ids.json", [])
    _write_json(paths.state / "repo_env_bootstrap.json", {"env_name": "demo_env", "status": "completed"})
    _write_json(paths.state / "repo_data_requirements.json", {"ready_datasets": ["amazon-beauty"], "blocked_datasets": []})
    _write_json(paths.state / "active_repo.json", {"name": "old/repo", "repo_path": "/tmp/old"})


def _seed_current_selected_experiment_block(paths: SimpleNamespace) -> None:
    run_id = "find_current"
    _write_json(paths.state / "finding_frontend.json", {"status": "claude_current_find_read_idea_plan_ready_waiting_for_environment_base_selection", "taste_run_id": run_id})
    _write_json(paths.state / "current_find_research_plan.json", {"status": "claude_current_find_read_idea_plan_ready_waiting_for_environment_base_selection", "run_id": run_id, "base_selection_status": "waiting_for_environment_claude_code"})
    _write_json(
        paths.state / "evidence_ready_repo_selection.json",
        {
            "fresh_find_run_id": run_id,
            "selection_gate": "accepted_by_claude_topic_fit",
            "selection_stage": "environment_claude_code",
            "current_action": "complete",
            "selected": {
                "name": "new/repo",
                "repo_path": "/tmp/new",
                "dataset": "amazon-beauty",
                "fresh_find_run_id": run_id,
                "selection_stage": "environment_claude_code",
            },
        },
    )
    _write_json(paths.state / "repo_selection_blocker.json", {"status": "blocked", "fresh_find_run_id": "find_old", "reason": "old environment-stage blocker"})
    _write_json(paths.state / "full_research_cycle.json", {"status": "blocked_after_max_cycles", "summary_zh": "实验门控阻塞", "current_find_run_id": run_id})
    _write_json(paths.state / "scientific_progress_gate.json", {"status": "blocked", "summary": "candidate evidence missing"})
    _write_json(paths.state / "taste_sync.json", {"counts": {"ideas_synced": 2}})


def test_report_status_separates_current_find_environment_and_experiment_state(tmp_path, monkeypatch):
    report_status = load_script("report_status")
    paths = _make_paths(tmp_path)
    _seed_common_project(paths)
    _seed_current_selected_experiment_block(paths)

    monkeypatch.setattr(report_status, "build_paths", lambda _project: paths)
    monkeypatch.setattr(report_status, "load_project_config", lambda _project: {"name": "demo_project", "topic": "Demo topic", "coding_agent": {}})
    monkeypatch.setattr(report_status, "get_active_paper_state", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(report_status, "llm_available", lambda _cfg: True)
    monkeypatch.setattr(report_status, "find_claude", lambda _cfg: "/bin/claude")
    monkeypatch.setattr(sys, "argv", ["report_status.py", "--project", "demo_project", "--venue", "ICLR"])

    report_status.main()

    text = (paths.reports / "status.md").read_text(encoding="utf-8")
    assert "- current_find_downstream_status: claude_current_find_read_idea_plan_ready_waiting_for_environment_base_selection" in text
    assert "- environment_base_selection_status: selected" in text
    assert "- full_cycle_status: blocked_after_max_cycles" in text
    assert "- scientific_progress_gate_status: blocked" in text
    assert "- repo_selection_status: selected" in text
    assert "- repo_selection_block_reason: none" in text
    assert "- repo_selection_blocker_stale_ignored: True" in text
    assert "old environment-stage blocker" not in text


def test_research_healthcheck_reports_overall_full_cycle_not_find_only_status(tmp_path, monkeypatch):
    healthcheck = load_script("research_healthcheck")
    paths = _make_paths(tmp_path)
    _seed_common_project(paths)
    _seed_current_selected_experiment_block(paths)

    monkeypatch.setattr(healthcheck, "build_paths", lambda _project: paths)
    monkeypatch.setattr(healthcheck, "load_project_config", lambda _project: {"name": "demo_project", "topic": "Demo topic", "coding_agent": {}})
    monkeypatch.setattr(healthcheck, "get_active_paper_state", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(healthcheck, "llm_available", lambda _cfg: True)
    monkeypatch.setattr(healthcheck, "find_claude", lambda _cfg: "/bin/claude")
    monkeypatch.setattr(sys, "argv", ["research_healthcheck.py", "--project", "demo_project", "--venue", "ICLR"])

    healthcheck.main()

    text = (paths.reports / "healthcheck.md").read_text(encoding="utf-8")
    assert "Current-Find downstream status: claude_current_find_read_idea_plan_ready_waiting_for_environment_base_selection (run_id=find_current)" in text
    assert "Environment base selection: selected" in text
    assert "Full-cycle status: blocked_after_max_cycles" in text
    assert "Full-cycle summary: 实验门控阻塞" in text
    assert "Experiment evidence gate: blocked" in text
    assert "TASTE status: claude_current_find_read_idea_plan_ready_waiting_for_environment_base_selection" not in text


def test_status_and_healthcheck_mark_stale_environment_selection(tmp_path, monkeypatch):
    report_status = load_script("report_status")
    healthcheck = load_script("research_healthcheck")
    paths = _make_paths(tmp_path)
    _seed_common_project(paths)
    _seed_current_selected_experiment_block(paths)
    run_id = "find_current"
    _write_json(
        paths.state / "current_find_research_plan.json",
        {
            "status": "claude_current_find_read_idea_plan_ready_waiting_for_environment_base_selection",
            "run_id": run_id,
            "selected_plan_id": "plan-current",
            "selected_idea_id": "idea-current",
        },
    )
    _write_json(
        paths.state / "evidence_ready_repo_selection.json",
        {
            "fresh_find_run_id": run_id,
            "selected_plan_id": "plan-current",
            "selected_idea_id": "idea-current",
            "selection_gate": "accepted_by_claude_topic_fit",
            "current_action": "complete",
            "claude_topic_decision": {"decision": "accept", "rationale": "stale selected route"},
            "selected": {
                "name": "new/repo",
                "repo_path": "/tmp/new",
                "dataset": "amazon-beauty",
                "fresh_find_run_id": "find_old",
                "selected_plan_id": "plan-old",
                "selected_idea_id": "idea-old",
            },
        },
    )

    for module in [report_status, healthcheck]:
        monkeypatch.setattr(module, "build_paths", lambda _project: paths)
        monkeypatch.setattr(module, "load_project_config", lambda _project: {"name": "demo_project", "topic": "Demo topic", "coding_agent": {}})
        monkeypatch.setattr(module, "get_active_paper_state", lambda *_args, **_kwargs: {})
        monkeypatch.setattr(module, "llm_available", lambda _cfg: True)
        monkeypatch.setattr(module, "find_claude", lambda _cfg: "/bin/claude")

    monkeypatch.setattr(sys, "argv", ["report_status.py", "--project", "demo_project", "--venue", "ICLR"])
    report_status.main()
    status_text = (paths.reports / "status.md").read_text(encoding="utf-8")
    assert "- environment_base_selection_status: environment_selection_find_run_missing_or_stale" in status_text
    assert "- repo_selection_status: environment_selection_find_run_missing_or_stale" in status_text
    assert "- claude_repo_decision_scope: stale_environment_selection" in status_text
    assert "- claude_accepted_transformable_repo: False" in status_text
    assert "- claude_repo_decision_scope: accepted_environment_selection" not in status_text

    monkeypatch.setattr(sys, "argv", ["research_healthcheck.py", "--project", "demo_project", "--venue", "ICLR"])
    healthcheck.main()
    health_text = (paths.reports / "healthcheck.md").read_text(encoding="utf-8")
    assert "Environment base selection: environment_selection_find_run_missing_or_stale" in health_text
    assert "Environment base selection: selected" not in health_text


def _seed_selected_base_semantic_provenance_block(paths: SimpleNamespace) -> None:
    _seed_current_selected_experiment_block(paths)
    _write_json(
        paths.state / "full_research_cycle.json",
        {
            "status": "blocked_after_max_cycles",
            "summary": "stale full-cycle summary says continue behavior experiments",
            "current_find_run_id": "find_current",
        },
    )
    _write_json(
        paths.state / "selected_base_viability_gate.json",
        {
            "status": "blocked",
            "decision": "base_switch_gate_required",
            "issue": (
                "selected_base_viability_gate: 参考复现已通过，但当前 selected-base 主线缺少 "
                "LLM/text-semantic 实验所需的可审计文本/元数据 provenance；继续运行纯行为或损失级候选实验无法清除此门控。"
            ),
            "semantic_data_provenance_review": {
                "status": "blocked",
                "deterministic_gate_required": True,
                "project_requires_llm_semantics": True,
                "llm_semantic_guard_status": "blocked",
                "has_real_llm_embedding_evidence": False,
                "text_metadata_provenance": {
                    "status": "blocked",
                    "has_text_metadata_evidence": False,
                    "dataset": "demo-data",
                    "repo_path": "/tmp/new",
                },
            },
        },
    )


def test_report_status_surfaces_selected_base_semantic_provenance_gate(tmp_path, monkeypatch):
    report_status = load_script("report_status")
    paths = _make_paths(tmp_path)
    _seed_common_project(paths)
    _seed_selected_base_semantic_provenance_block(paths)

    monkeypatch.setattr(report_status, "build_paths", lambda _project: paths)
    monkeypatch.setattr(report_status, "load_project_config", lambda _project: {"name": "demo_project", "topic": "Demo topic", "coding_agent": {}})
    monkeypatch.setattr(report_status, "get_active_paper_state", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(report_status, "llm_available", lambda _cfg: True)
    monkeypatch.setattr(report_status, "find_claude", lambda _cfg: "/bin/claude")
    monkeypatch.setattr(sys, "argv", ["report_status.py", "--project", "demo_project", "--venue", "ICLR"])

    report_status.main()

    text = (paths.reports / "status.md").read_text(encoding="utf-8")
    assert "- current_blocker_category: semantic_data_provenance_required" in text
    assert "- selected_base_viability_gate_status: blocked" in text
    assert "- selected_base_viability_gate_decision: base_switch_gate_required" in text
    assert "- semantic_data_provenance_status: blocked" in text
    assert "- semantic_data_provenance_dataset: demo-data" in text
    assert "- semantic_data_provenance_has_text_metadata_evidence: False" in text
    assert "- semantic_data_provenance_has_real_llm_embedding_evidence: False" in text
    assert "LLM/text-semantic 实验所需的可审计文本/元数据 provenance" in text
    assert "stale full-cycle summary says continue behavior experiments" not in text


def test_research_healthcheck_surfaces_selected_base_semantic_provenance_gate(tmp_path, monkeypatch):
    healthcheck = load_script("research_healthcheck")
    paths = _make_paths(tmp_path)
    _seed_common_project(paths)
    _seed_selected_base_semantic_provenance_block(paths)

    monkeypatch.setattr(healthcheck, "build_paths", lambda _project: paths)
    monkeypatch.setattr(healthcheck, "load_project_config", lambda _project: {"name": "demo_project", "topic": "Demo topic", "coding_agent": {}})
    monkeypatch.setattr(healthcheck, "get_active_paper_state", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(healthcheck, "llm_available", lambda _cfg: True)
    monkeypatch.setattr(healthcheck, "find_claude", lambda _cfg: "/bin/claude")
    monkeypatch.setattr(sys, "argv", ["research_healthcheck.py", "--project", "demo_project", "--venue", "ICLR"])

    healthcheck.main()

    text = (paths.reports / "healthcheck.md").read_text(encoding="utf-8")
    assert "Selected-base viability gate: semantic_data_provenance_required (blocked/base_switch_gate_required)" in text
    assert "Semantic data provenance: blocked; dataset=demo-data; text_metadata_evidence=False; real_llm_embedding_evidence=False" in text
    assert "Current blocker summary: selected_base_viability_gate" in text
    assert "LLM/text-semantic 实验所需的可审计文本/元数据 provenance" in text
    assert "stale full-cycle summary says continue behavior experiments" not in text



def _seed_failed_base_switch_gate(paths: SimpleNamespace) -> None:
    _write_json(
        paths.state / "base_switch_gate.json",
        {
            "status": "blocked",
            "decision": "base_switch_not_authorized",
            "switch_authorized": False,
            "candidate_route": {},
            "failed_checks": [
                {"id": "candidate_route_proposal_exists", "status": "blocked"},
                {"id": "candidate_find_run_provenance_clear", "status": "blocked"},
            ],
        },
    )


def test_report_status_uses_failed_base_switch_gate_result(tmp_path, monkeypatch):
    report_status = load_script("report_status")
    paths = _make_paths(tmp_path)
    _seed_common_project(paths)
    _seed_selected_base_semantic_provenance_block(paths)
    _seed_failed_base_switch_gate(paths)

    monkeypatch.setattr(report_status, "build_paths", lambda _project: paths)
    monkeypatch.setattr(report_status, "load_project_config", lambda _project: {"name": "demo_project", "topic": "Demo topic", "coding_agent": {}})
    monkeypatch.setattr(report_status, "get_active_paper_state", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(report_status, "llm_available", lambda _cfg: True)
    monkeypatch.setattr(report_status, "find_claude", lambda _cfg: "/bin/claude")
    monkeypatch.setattr(sys, "argv", ["report_status.py", "--project", "demo_project", "--venue", "ICLR"])

    report_status.main()

    text = (paths.reports / "status.md").read_text(encoding="utf-8")
    assert "- base_switch_gate_status: blocked" in text
    assert "- base_switch_gate_decision: base_switch_not_authorized" in text
    assert "- base_switch_candidate_route_present: False" in text
    assert "candidate_route_proposal_exists" in text
    assert "确定性 base-switch gate 已执行但未授权" in text
    assert "candidate base-switch proposal" in text
    assert "运行 deterministic base-switch / semantic-provenance gate" not in text


def test_report_status_keeps_current_route_repair_when_candidate_gate_failed(tmp_path, monkeypatch):
    report_status = load_script("report_status")
    paths = _make_paths(tmp_path)
    _seed_common_project(paths)
    _seed_selected_base_semantic_provenance_block(paths)
    _write_json(
        paths.state / "base_switch_gate.json",
        {
            "status": "blocked",
            "decision": "base_switch_not_authorized",
            "switch_authorized": False,
            "candidate_route": {"repo": "owner/candidate", "repo_path": "/tmp/candidate"},
            "failed_checks": [
                {"id": "candidate_loader_import_probe_passed", "status": "blocked"},
                {"id": "candidate_reference_protocol_passed", "status": "blocked"},
            ],
        },
    )

    monkeypatch.setattr(report_status, "build_paths", lambda _project: paths)
    monkeypatch.setattr(report_status, "load_project_config", lambda _project: {"name": "demo_project", "topic": "Demo topic", "coding_agent": {}})
    monkeypatch.setattr(report_status, "get_active_paper_state", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(report_status, "llm_available", lambda _cfg: True)
    monkeypatch.setattr(report_status, "find_claude", lambda _cfg: "/bin/claude")
    monkeypatch.setattr(sys, "argv", ["report_status.py", "--project", "demo_project", "--venue", "ICLR"])

    report_status.main()

    text = (paths.reports / "status.md").read_text(encoding="utf-8")
    assert "- base_switch_candidate_route_present: True" in text
    assert "candidate_loader_import_probe_passed, candidate_reference_protocol_passed" in text
    assert "补齐当前路线保存 ID 映射的原始文本/元数据 provenance" in text
    assert "artifact-local LLM/text embedding probe" in text
    assert "补齐上列候选路线未通过检查后刷新 deterministic base-switch gate" in text



def test_status_and_healthcheck_block_environment_when_pending_candidate_lacks_loader(tmp_path, monkeypatch):
    report_status = load_script("report_status")
    healthcheck = load_script("research_healthcheck")
    paths = _make_paths(tmp_path)
    _seed_common_project(paths)
    _seed_selected_base_semantic_provenance_block(paths)
    _write_json(paths.state / "repo_data_requirements.json", {"ready_datasets": ["demo-data"], "blocked_datasets": []})
    _write_json(paths.state / "active_repo.json", {"name": "old/repo", "repo_path": "/tmp/new", "claim_ready_dataset": "demo-data"})
    _write_json(
        paths.state / "evidence_ready_repo_selection.json",
        {
            "fresh_find_run_id": "find_current",
            "current_action": "complete",
            "selection_gate": "blocked_pending_data_loader_for_claude_best_candidate",
            "blocker": "candidate proposal lacks claim-ready loader evidence; active_repo is unchanged.",
            "claude_topic_decision": {"decision": "accept-with-modifications", "confidence": 0.61, "rationale": "candidate rationale"},
            "selected": {},
            "pending_environment_candidate": {
                "name": "owner/pending",
                "repo_path": "/tmp/pending",
                "fresh_find_run_id": "find_current",
                "pending_loader_bootstrap": True,
                "probe_summary": {"claim_ready_datasets": []},
            },
        },
    )
    _write_json(paths.state / "repo_selection_blocker.json", {"status": "blocked", "fresh_find_run_id": "find_current", "reason": "old active_repo remains legacy/control only"})

    for module in [report_status, healthcheck]:
        monkeypatch.setattr(module, "build_paths", lambda _project: paths)
        monkeypatch.setattr(module, "load_project_config", lambda _project: {"name": "demo_project", "topic": "Demo topic", "coding_agent": {}})
        monkeypatch.setattr(module, "get_active_paper_state", lambda *_args, **_kwargs: {})
        monkeypatch.setattr(module, "llm_available", lambda _cfg: True)
        monkeypatch.setattr(module, "find_claude", lambda _cfg: "/bin/claude")

    monkeypatch.setattr(sys, "argv", ["report_status.py", "--project", "demo_project", "--venue", "ICLR"])
    report_status.main()
    status_text = (paths.reports / "status.md").read_text(encoding="utf-8")
    assert "- environment_base_selection_status: environment_repo_selection_blocked_pending_loader_candidate" in status_text
    assert "- repo_selection_status: pending_candidate_blocked" in status_text
    assert "candidate proposal lacks claim-ready loader evidence" in status_text
    assert "old active_repo remains legacy/control only" not in status_text
    assert "- claude_repo_decision_scope: pending_candidate_not_authoritative" in status_text
    assert "- claude_repo_decision_subject: owner/pending" in status_text
    assert "- claude_accepted_transformable_repo: False" in status_text
    assert "pending candidate only; active_repo unchanged: candidate rationale" in status_text

    monkeypatch.setattr(sys, "argv", ["research_healthcheck.py", "--project", "demo_project", "--venue", "ICLR"])
    healthcheck.main()
    health_text = (paths.reports / "healthcheck.md").read_text(encoding="utf-8")
    assert "Environment base selection: environment_repo_selection_blocked_pending_loader_candidate" in health_text
    assert "selected_current_route_pending_candidate_blocked" not in health_text


def test_research_healthcheck_uses_failed_base_switch_gate_result(tmp_path, monkeypatch):
    healthcheck = load_script("research_healthcheck")
    paths = _make_paths(tmp_path)
    _seed_common_project(paths)
    _seed_selected_base_semantic_provenance_block(paths)
    _seed_failed_base_switch_gate(paths)

    monkeypatch.setattr(healthcheck, "build_paths", lambda _project: paths)
    monkeypatch.setattr(healthcheck, "load_project_config", lambda _project: {"name": "demo_project", "topic": "Demo topic", "coding_agent": {}})
    monkeypatch.setattr(healthcheck, "get_active_paper_state", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(healthcheck, "llm_available", lambda _cfg: True)
    monkeypatch.setattr(healthcheck, "find_claude", lambda _cfg: "/bin/claude")
    monkeypatch.setattr(sys, "argv", ["research_healthcheck.py", "--project", "demo_project", "--venue", "ICLR"])

    healthcheck.main()

    text = (paths.reports / "healthcheck.md").read_text(encoding="utf-8")
    assert "Base-switch gate: blocked/base_switch_not_authorized; candidate_route_present=False" in text
    assert "failed_checks=candidate_route_proposal_exists,candidate_find_run_provenance_clear" in text
    assert "确定性 base-switch gate 已执行但未授权" in text
    assert "运行 deterministic base-switch / semantic-provenance gate" not in text



def test_refresh_project_reports_runs_reflection_after_status_inputs():
    refresh_reports = load_script("refresh_project_reports")

    steps = refresh_reports.build_steps("demo_project", "ICLR")
    names = [name for name, _cmd in steps]
    rendered = [" ".join(cmd) for _name, cmd in steps]

    assert names == ["healthcheck", "status", "next_actions", "trajectory", "reflection", "shared_research"]
    assert rendered[0].endswith("research_healthcheck.py --project demo_project --venue ICLR")
    assert rendered[1].endswith("report_status.py --project demo_project --venue ICLR")
    assert rendered[2].endswith("run_module.py planning --action next_actions --project demo_project")
    assert rendered[3].endswith("build_research_trajectory_system.py --project demo_project --skip-helpers --venue ICLR")
    assert rendered[4].endswith("run_module.py planning --action reflect --project demo_project")
    assert rendered[5].endswith("compile_prompt.py --project demo_project")
    assert rendered.index(rendered[3]) > rendered.index(rendered[2])
    assert rendered.index(rendered[4]) > rendered.index(rendered[0])
    assert rendered.index(rendered[4]) > rendered.index(rendered[3])
    assert rendered.index(rendered[5]) > rendered.index(rendered[4])


def test_refresh_project_reports_normalizes_legacy_taste_root_metadata(tmp_path):
    refresh_reports = load_script("refresh_project_reports")
    paths = _make_paths(tmp_path)
    _write_json(
        paths.state / "finding_frontend.json",
        {"taste_root": "/home/fmh/workspace/TASTE/modules/taste", "taste_run_id": "find_demo"},
    )
    _write_json(
        paths.state / "taste_sync.json",
        {
            "taste_state": {"taste_root": "/home/fmh/workspace/TASTE/modules/taste"},
            "nested": [{"taste_root": "/home/fmh/workspace/TASTE/modules/taste"}],
        },
    )

    result = refresh_reports.normalize_project_metadata(paths, canonical_root=Path("/workspace/TASTE"))

    assert len(result["changed_files"]) == 2
    assert json.loads((paths.state / "finding_frontend.json").read_text(encoding="utf-8"))["taste_root"] == "/workspace/TASTE"
    sync_payload = json.loads((paths.state / "taste_sync.json").read_text(encoding="utf-8"))
    assert sync_payload["taste_state"]["taste_root"] == "/workspace/TASTE"
    assert sync_payload["nested"][0]["taste_root"] == "/workspace/TASTE"


def test_propose_next_actions_prioritizes_semantic_provenance_gate(tmp_path, monkeypatch):
    propose = load_script("propose_next_actions")
    paths = _make_paths(tmp_path)
    _seed_common_project(paths)
    _seed_selected_base_semantic_provenance_block(paths)
    _write_json(paths.state / "ingest_ranking.json", {"no_qualified_papers": True, "no_qualified_reason": "No qualified papers."})
    _write_json(paths.state / "repo_candidates.json", [{"name": "old/top-repo", "url": "https://example.test/old"}])
    _write_json(paths.state / "repo_data_requirements.json", {"ready_datasets": ["demo-data"], "blocked_datasets": []})
    _write_json(paths.state / "real_dataset_probe.json", {"probes": []})
    _write_json(paths.state / "experiment_registry.json", [])

    monkeypatch.setattr(propose, "build_paths", lambda _project: paths)
    monkeypatch.setattr(propose, "load_project_config", lambda _project: {"name": "demo_project", "topic": "Demo topic"})
    monkeypatch.setattr(sys, "argv", ["propose_next_actions.py", "--project", "demo_project"])

    propose.main()

    payload = json.loads((paths.state / "next_actions.json").read_text(encoding="utf-8"))
    titles = [row["title"] for row in payload["actions"]]
    assert titles == ["Run deterministic semantic-provenance/base-switch gate"]
    assert payload["actions"][0]["gate_category"] == "semantic_data_provenance_required"
    assert "demo-data" in payload["actions"][0]["evidence"]
    assert "text_metadata_evidence=False" in payload["actions"][0]["evidence"]
    assert "Run repo-first literature backtracking" not in titles
    assert "Probe real repo dataset loaders before real experiments" not in titles

    text = (paths.planning / "next_actions.md").read_text(encoding="utf-8")
    assert "current_blocker_category: semantic_data_provenance_required" in text
    assert "selected_base_viability_gate_decision: base_switch_gate_required" in text
    assert "Run deterministic semantic-provenance/base-switch gate" in text
    assert "Run repo-first literature backtracking" not in text
    assert "Probe real repo dataset loaders before real experiments" not in text


def test_propose_next_actions_reports_loader_probe_success_field(tmp_path, monkeypatch):
    propose = load_script("propose_next_actions")
    paths = _make_paths(tmp_path)
    _seed_common_project(paths)
    _write_json(paths.state / "dataset_registry.json", [{"name": "amazon-beauty", "available": True}])
    _write_json(
        paths.state / "real_dataset_probe.json",
        {
            "status": "passed",
            "decision": "claim_ready_datasets_available",
            "ready_datasets": ["amazon-beauty"],
            "probes": [
                {
                    "dataset": "amazon-beauty",
                    "claim_ready": True,
                    "loader_probe_success": True,
                    "loader_probe": {"success": True},
                }
            ],
        },
    )
    _write_json(paths.state / "experiment_registry.json", [])

    monkeypatch.setattr(propose, "build_paths", lambda _project: paths)
    monkeypatch.setattr(propose, "load_project_config", lambda _project: {"name": "demo_project", "topic": "Demo topic"})
    monkeypatch.setattr(sys, "argv", ["propose_next_actions.py", "--project", "demo_project"])

    propose.main()

    text = (paths.planning / "next_actions.md").read_text(encoding="utf-8")
    assert "real_loader_probe_passed: True" in text
    assert "Probe real repo dataset loaders before real experiments" not in text
    assert "Launch a real-dataset repo reproduction smoke run" in text


def test_deterministic_base_switch_gate_blocks_empty_candidate_route(tmp_path, monkeypatch):
    gate_script = load_script("audit_deterministic_base_switch_gate")
    paths = _make_paths(tmp_path)
    _seed_common_project(paths)
    _seed_selected_base_semantic_provenance_block(paths)
    _write_json(paths.state / "reference_reproduction_gate.json", {"status": "pass", "decision": "continue_base"})

    monkeypatch.setattr(gate_script, "build_paths", lambda _project: paths)

    payload = gate_script.build_gate("demo_project", "ICLR")

    checks = {row["id"]: row for row in payload["checks"]}
    assert payload["status"] == "blocked"
    assert payload["decision"] == "base_switch_not_authorized"
    assert payload["candidate_route"] == {}
    assert checks["candidate_route_proposal_exists"]["status"] == "blocked"
    assert "non-empty candidate route proposal" in checks["candidate_route_proposal_exists"]["detail"]



def test_deterministic_base_switch_gate_reads_pending_environment_candidate(tmp_path, monkeypatch):
    gate_script = load_script("audit_deterministic_base_switch_gate")
    paths = _make_paths(tmp_path)
    _seed_common_project(paths)
    _seed_selected_base_semantic_provenance_block(paths)
    pending_repo = paths.root / "repos" / "selected" / "pending_repo"
    pending_repo.mkdir(parents=True)
    _write_json(paths.state / "reference_reproduction_gate.json", {"status": "pass", "decision": "continue_base"})
    _write_json(paths.state / "active_repo.json", {"name": "old/repo", "repo_path": "/tmp/new", "claim_ready_dataset": "demo-data"})
    _write_json(
        paths.state / "evidence_ready_repo_selection.json",
        {
            "fresh_find_run_id": "find_current",
            "selection_gate": "blocked_pending_data_loader_for_claude_best_candidate",
            "selected": {},
            "pending_environment_candidate": {
                "name": "owner/pending",
                "repo_path": str(pending_repo),
                "fresh_find_run_id": "find_current",
                "pending_loader_bootstrap": True,
                "probe_summary": {"claim_ready_datasets": []},
            },
        },
    )

    monkeypatch.setattr(gate_script, "build_paths", lambda _project: paths)

    payload = gate_script.build_gate("demo_project", "ICLR")

    checks = {row["id"]: row for row in payload["checks"]}
    assert payload["status"] == "blocked"
    assert payload["decision"] == "base_switch_not_authorized"
    assert payload["candidate_route"]["repo"] == "owner/pending"
    assert payload["candidate_route"]["type"] == "pending_environment_candidate_proposal"
    assert checks["candidate_route_proposal_exists"]["status"] == "pass"
    assert checks["candidate_route_is_non_authoritative"]["status"] == "pass"
    assert checks["candidate_route_distinct_from_selected_base"]["status"] == "pass"
    assert checks["candidate_find_run_provenance_clear"]["status"] == "pass"
    assert checks["candidate_loader_import_probe_passed"]["status"] == "blocked"
    assert checks["candidate_data_contract_passed"]["status"] == "blocked"


def test_build_blocker_action_plan_bootstraps_pythonpath_for_direct_help():
    import os
    import subprocess

    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    proc = subprocess.run(
        [sys.executable, str(root / "modules" / "planning" / "scripts" / "build_blocker_action_plan.py"), "--help"],
        cwd=str(root),
        env=env,
        text=True,
        capture_output=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert "Build a deterministic blocker-to-action routing plan" in proc.stdout


def test_blocker_action_plan_uses_failed_base_switch_gate_result(tmp_path, monkeypatch):
    blocker_plan = load_script("build_blocker_action_plan")
    paths = _make_paths(tmp_path)
    _seed_common_project(paths)
    _seed_selected_base_semantic_provenance_block(paths)
    _seed_failed_base_switch_gate(paths)
    full_cycle = json.loads((paths.state / "full_research_cycle.json").read_text(encoding="utf-8"))
    full_cycle["stage_failures"] = [
        {"stage": "selected-base-viability-initial", "return_code": 2, "tail": "state/selected_base_viability_gate.json"},
        {
            "stage": "selected-base-viability-precheck",
            "return_code": 2,
            "tail": "state/selected_base_viability_gate.json still blocked",
        },
    ]
    _write_json(paths.state / "full_research_cycle.json", full_cycle)

    monkeypatch.setattr(blocker_plan, "build_paths", lambda _project: paths)

    payload = blocker_plan.build("demo_project", "ICLR")

    assert payload["status"] == "blocked"
    assert payload["summary"]["top_route"] == "selected_base_viability_gate"
    assert "failed deterministic base-switch gate" in payload["summary"]["top_action"]
    selected_or_switch = [row for row in payload["actions"] if row.get("route") in {"selected_base_viability_gate", "base_switch_gate"}]
    selected_actions = [row for row in selected_or_switch if row.get("route") == "selected_base_viability_gate"]
    switch_actions = [row for row in selected_or_switch if row.get("route") == "base_switch_gate"]
    assert len(selected_actions) == 1
    assert len(switch_actions) == 1
    assert payload["summary"]["p0_action_count"] == 2
    assert {"selected-base-viability-initial", "selected-base-viability-precheck"}.issubset(set(selected_actions[0].get("merged_source_check_ids", [])))
    assert selected_or_switch
    combined = "\n".join(" ".join(str(row.get(key) or "") for key in ["issue", "repair_strategy"]) for row in selected_or_switch)
    assert "Re-running it unchanged will not clear the blocker" in combined
    assert "proposal-only candidate base-switch route" in combined
    assert "run the deterministic base-switch gate" not in combined.lower()
    assert all(row.get("base_switch_gate_status") == "blocked/base_switch_not_authorized" for row in selected_or_switch)
    downstream_actions = [
        {
            "route": "experiment_evidence_repair",
            "issue": "参考复现已通过；下一步由 project agent 继续真实实验迭代，论文/claim 暂停。",
            "priority": "P0",
        }
    ]
    blocker_plan.apply_failed_base_switch_gate_guidance(downstream_actions, paths, "demo_project", "ICLR")
    deferred = downstream_actions[0]
    deferred_public = " ".join(str(deferred.get(key) or "") for key in ["issue", "human_summary"])
    assert deferred["priority"] == "P2"
    assert deferred.get("blocked_by_selected_base_viability_gate") is True
    assert "blocked_by_failed_base_switch_gate" in deferred_public
    assert "继续真实实验迭代" not in deferred_public
    assert "继续真实实验迭代" in deferred.get("deferred_original_issue", "")

    duplicate_downstream = [
        {"id": "a1", "route": "experiment_evidence_repair", "issue": "继续真实实验迭代", "source": "state/a.json", "evidence": ["state/a.json"]},
        {"id": "a2", "route": "paper_production_repair", "issue": "写论文", "source": "state/b.json", "evidence": ["state/b.json"]},
        {"id": "a3", "route": "section_state_repair", "issue": "修章节", "source": "state/c.json", "evidence": ["state/c.json"]},
    ]
    blocker_plan.apply_failed_base_switch_gate_guidance(duplicate_downstream, paths, "demo_project", "ICLR")
    compacted = blocker_plan.compact_failed_base_switch_gate_actions(duplicate_downstream)

    assert len(compacted) == 1
    merged = compacted[0]
    assert merged["issue"].startswith("blocked_by_failed_base_switch_gate:")
    assert set(merged.get("merged_routes", [])) == {"experiment_evidence_repair", "paper_production_repair", "section_state_repair"}
    assert set(merged.get("merged_sources", [])) == {"state/a.json", "state/b.json", "state/c.json"}
    assert set(merged.get("evidence", [])) == {"state/a.json", "state/b.json", "state/c.json"}


def test_research_trajectory_gate_context_reads_failed_base_switch_gate(tmp_path):
    trajectory = load_script("build_research_trajectory_system")
    paths = _make_paths(tmp_path)
    _seed_common_project(paths)
    _seed_selected_base_semantic_provenance_block(paths)
    _seed_failed_base_switch_gate(paths)

    context = trajectory.trajectory_gate_context(paths)

    assert context["selected_base_gate_required"] is True
    assert context["base_switch_failed"] is True
    assert context["blocks_downstream"] is True
    assert context["base_switch_candidate_route_present"] is False
    assert context["base_switch_gate_status"] == "blocked"
    assert context["base_switch_gate_decision"] == "base_switch_not_authorized"
    assert "candidate_route_proposal_exists" in context["base_switch_failed_checks"]


def test_research_trajectory_controller_defers_exploration_after_failed_base_switch_gate():
    trajectory = load_script("build_research_trajectory_system")

    controller = trajectory.trajectory_controller(
        {"status": "blocked"},
        {"nodes": [{"id": "failed_behavior_probe", "method": "behavior-only candidate"}]},
        {"nodes": [{"id": "ready_repo_data_cross_product", "title": "ready candidate"}]},
        {"repair_queue": [], "elite_pool": [{"id": "elite_method"}]},
        {},
        {"phase_count": 1},
        [{"name": "experiment-loop", "path": "framework/resources/claude/skills/experiment-loop/SKILL.md"}],
        {
            "blocks_downstream": True,
            "base_switch_failed": True,
            "base_switch_gate_status": "blocked",
            "base_switch_gate_decision": "base_switch_not_authorized",
        },
    )

    public_objectives = "\n".join(controller["next_objectives"])
    assert "Resolve current-route provenance/embedding evidence or a proposal-only candidate base-switch route" in public_objectives
    assert "Use Claude Code only on gate evidence repair nodes" in public_objectives
    assert "Keep unexplored-niche experiments deferred" in public_objectives
    assert "Keep method deepening deferred" in public_objectives
    assert "Select one unexplored niche" not in public_objectives
    assert "next bounded experiment" not in public_objectives


def test_propose_next_actions_uses_failed_base_switch_gate_result(tmp_path, monkeypatch):
    propose = load_script("propose_next_actions")
    paths = _make_paths(tmp_path)
    _seed_common_project(paths)
    _seed_selected_base_semantic_provenance_block(paths)
    _write_json(
        paths.state / "base_switch_gate.json",
        {
            "status": "blocked",
            "decision": "base_switch_not_authorized",
            "switch_authorized": False,
            "candidate_route": {},
            "failed_checks": [
                {"id": "candidate_route_proposal_exists", "status": "blocked"},
                {"id": "candidate_find_run_provenance_clear", "status": "blocked"},
            ],
        },
    )
    _write_json(paths.state / "ingest_ranking.json", {"no_qualified_papers": True, "no_qualified_reason": "No qualified papers."})
    _write_json(paths.state / "repo_candidates.json", [{"name": "old/top-repo", "url": "https://example.test/old"}])
    _write_json(paths.state / "repo_data_requirements.json", {"ready_datasets": ["demo-data"], "blocked_datasets": []})
    _write_json(paths.state / "real_dataset_probe.json", {"probes": []})
    _write_json(paths.state / "experiment_registry.json", [])
    selected_gate = json.loads((paths.state / "selected_base_viability_gate.json").read_text(encoding="utf-8"))
    selected_gate["issue"] += " 下一步只能进入 deterministic base-switch / semantic-provenance gate。"
    _write_json(paths.state / "selected_base_viability_gate.json", selected_gate)

    monkeypatch.setattr(propose, "build_paths", lambda _project: paths)
    monkeypatch.setattr(propose, "load_project_config", lambda _project: {"name": "demo_project", "topic": "Demo topic"})
    monkeypatch.setattr(sys, "argv", ["propose_next_actions.py", "--project", "demo_project"])

    propose.main()

    payload = json.loads((paths.state / "next_actions.json").read_text(encoding="utf-8"))
    titles = [row["title"] for row in payload["actions"]]
    assert titles == ["Provide semantic provenance evidence or a candidate base-switch proposal"]
    action = payload["actions"][0]
    assert "base_switch_gate=blocked/base_switch_not_authorized" in action["evidence"]
    assert "failed_checks=candidate_route_proposal_exists,candidate_find_run_provenance_clear" in action["evidence"]
    assert "Run deterministic semantic-provenance/base-switch gate" not in titles
    assert "下一步只能进入 deterministic base-switch / semantic-provenance gate" not in action["evidence"]
    assert "Run repo-first literature backtracking" not in titles
    assert "Probe real repo dataset loaders before real experiments" not in titles

    text = (paths.planning / "next_actions.md").read_text(encoding="utf-8")
    assert "base_switch_gate_status: blocked" in text
    assert "base_switch_gate_decision: base_switch_not_authorized" in text
    assert "Provide semantic provenance evidence or a candidate base-switch proposal" in text
    assert "下一步只能进入 deterministic base-switch / semantic-provenance gate" not in text
