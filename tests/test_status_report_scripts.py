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
    _write_json(paths.state / "evidence_ready_repo_selection.json", {"fresh_find_run_id": run_id, "selection_gate": "accepted_by_claude_topic_fit", "current_action": "complete", "selected": {"name": "new/repo", "repo_path": "/tmp/new", "dataset": "amazon-beauty"}})
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
    assert "Run repo-first literature backtracking" not in titles
    assert "Probe real repo dataset loaders before real experiments" not in titles

    text = (paths.planning / "next_actions.md").read_text(encoding="utf-8")
    assert "base_switch_gate_status: blocked" in text
    assert "base_switch_gate_decision: base_switch_not_authorized" in text
    assert "Provide semantic provenance evidence or a candidate base-switch proposal" in text
