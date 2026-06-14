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
