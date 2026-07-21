from __future__ import annotations

import json
from pathlib import Path

from automation import run_supervision_tick
from bridges import project_bridge, reading_bridge
from orchestration import run_module
from project import project_config, project_paths


ROOT = Path(__file__).resolve().parents[1]


def _project(tmp_path: Path, *, max_read_papers: int = 50, source: str = "module_default") -> tuple[Path, Path]:
    projects = tmp_path / "projects"
    root = projects / "demo"
    (root / "planning" / "finding").mkdir(parents=True)
    (root / "state").mkdir(parents=True)
    (root / "project.json").write_text(
        json.dumps(
            {
                "name": "demo",
                "topic": "ranked reading topic",
                "research_interest": "specific research interest",
                "researcher_profile": "researcher profile",
                "max_read_papers": max_read_papers,
                "reading": {"max_read_papers_source": source},
            }
        ),
        encoding="utf-8",
    )
    return projects, root


def test_project_read_count_defaults_and_user_patch_are_project_scoped():
    template = json.loads((ROOT / "framework" / "resources" / "templates" / "project.json").read_text(encoding="utf-8"))
    assert template["max_read_papers"] == 50
    assert template["reading"]["max_read_papers_source"] == "module_default"
    assert project_paths.configured_max_read_papers(cfg={}) == 50

    updated, _selection = project_config._apply_project_patch(template, {"max_read_papers": 73})
    assert updated["max_read_papers"] == 73
    assert updated["reading"]["max_read_papers_source"] == "user"


def test_find_refreshes_automatic_read_default_but_preserves_user_override(tmp_path):
    projects, root = _project(tmp_path)
    find_results = {
        "run_id": "find_new",
        "strong_recommendations": [{"id": f"r{index}", "title": f"R{index}"} for index in range(3)],
    }
    result = reading_bridge.update_project_read_default_after_find("demo", find_results, projects_root=projects)
    saved = json.loads((root / "project.json").read_text(encoding="utf-8"))
    assert result["status"] == "updated_framework_find_default"
    assert saved["max_read_papers"] == 6
    assert saved["reading"]["max_read_papers_source"] == "framework_find_default"
    assert saved["reading"]["max_read_papers_find_run_id"] == "find_new"

    reset = reading_bridge.update_project_read_default_after_find(
        "demo",
        {
            "run_id": "find_empty",
            "strong_recommendations": [],
            "read_candidates": [{"id": f"candidate-{index}", "title": f"Candidate {index}"} for index in range(12)],
        },
        projects_root=projects,
    )
    assert reset["status"] == "reset_module_default_no_find_recommendations"
    saved = json.loads((root / "project.json").read_text(encoding="utf-8"))
    assert saved["max_read_papers"] == 50

    saved["max_read_papers"] = 9
    saved["reading"]["max_read_papers_source"] = "user"
    (root / "project.json").write_text(json.dumps(saved), encoding="utf-8")
    preserved = reading_bridge.update_project_read_default_after_find(
        "demo",
        {"run_id": "find_later", "strong_recommendations": [{"title": "Only"}]},
        projects_root=projects,
    )
    assert preserved["status"] == "preserved_user_override"
    assert json.loads((root / "project.json").read_text(encoding="utf-8"))["max_read_papers"] == 9


def test_framework_read_input_uses_final_ranking_first_n_and_research_context(tmp_path):
    projects, root = _project(tmp_path, max_read_papers=3)
    finding = root / "planning" / "finding"
    (finding / "find_results.json").write_text(
        json.dumps(
            {
                "run_id": "find_ranked",
                "strong_recommendations": [{"id": "p0", "title": "Rank 0"}, {"id": "p1", "title": "Rank 1"}],
                "screened_ranking": [
                    {"id": f"p{index}", "title": f"Rank {index}", "fit_score": 10 - index}
                    for index in range(5)
                ],
            }
        ),
        encoding="utf-8",
    )
    prepared = reading_bridge.prepare_current_find_read_input(
        "demo",
        projects_root=projects,
        reading_root=tmp_path / "reading",
    )
    payload = json.loads(Path(prepared["input_json"]).read_text(encoding="utf-8"))
    assert prepared["ranking_source"] == "screened_ranking"
    assert prepared["ranked_count"] == 5
    assert prepared["recommendation_count"] == 2
    assert prepared["input_article_count"] == 3
    assert [row["title"] for row in payload["articles"]] == ["Rank 0", "Rank 1", "Rank 2"]
    assert payload["research_topic"] == "ranked reading topic"
    assert payload["research_interest"] == "specific research interest"
    assert payload["researcher_profile"] == "researcher profile"


def test_framework_sync_uses_selected_count_and_preserves_scored_output_order(tmp_path):
    projects, root = _project(tmp_path, max_read_papers=2)
    finding = root / "planning" / "finding"
    (finding / "find_results.json").write_text(
        json.dumps(
            {
                "run_id": "find_scored",
                "strong_recommendations": [{"id": "a", "title": "Paper A"}],
                "screened_ranking": [
                    {"id": "a", "title": "Paper A", "url": "https://example.test/a"},
                    {"id": "b", "title": "Paper B", "url": "https://example.test/b"},
                    {"id": "c", "title": "Paper C", "url": "https://example.test/c"},
                ],
            }
        ),
        encoding="utf-8",
    )
    reading_root = tmp_path / "reading"
    prepared = reading_bridge.prepare_current_find_read_input(
        "demo", projects_root=projects, reading_root=reading_root
    )
    run_dir = Path(prepared["reading_run_dir"])
    input_payload = json.loads(Path(prepared["input_json"]).read_text(encoding="utf-8"))
    papers = {row["id"]: row for row in input_payload["articles"]}
    (run_dir / "read.md").write_text("# 论文精读\n\n## Paper B\n\n## Paper A\n", encoding="utf-8")
    (run_dir / "read_results.json").write_text(
        json.dumps(
            {
                "read_markdown_aggregation": {"valid": True, "mode": "claude_scored"},
                "reading_scoring": {"status": "complete", "expected_article_count": 2, "scored_article_count": 2},
                "items": [
                    {
                        "paper_index": 2,
                        "paper": {"id": "b", "title": "Paper B"},
                        "reading": {"match_score": 9, "transferability_score": 8, "average_score": 8.5, "final_read_rank": 1},
                        "full_text_packet": {"full_text_available": True, "full_text_chars": 2000},
                        "validation": {"full_text_ready": True, "deep_read_complete": True},
                    },
                    {
                        "paper_index": 1,
                        "paper": {"id": "a", "title": "Paper A"},
                        "reading": {"match_score": 7, "transferability_score": 6, "average_score": 6.5, "final_read_rank": 2},
                        "full_text_packet": {"full_text_available": True, "full_text_chars": 2000},
                        "validation": {"full_text_ready": True, "deep_read_complete": True},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    result = reading_bridge.sync_current_find_read_outputs(
        "demo",
        result_payload={"run_dir": str(run_dir)},
        projects_root=projects,
        reading_root=reading_root,
    )
    projected = json.loads((finding / "read_results.json").read_text(encoding="utf-8"))
    rows = projected["readings"]
    assert result["valid"] is True
    assert projected["reading_validation"]["expected_reading_count"] == 2
    assert projected["recommendation_count"] == 1
    assert projected["ranked_paper_count"] == 3
    assert [row["title"] for row in rows] == ["Paper B", "Paper A"]
    assert [row["url"] for row in rows] == [papers["b"]["url"], papers["a"]["url"]]
    assert rows[0]["match_score"] == 9
    assert rows[0]["transferability_score"] == 8
    assert rows[0]["average_score"] == 8.5
    assert rows[0]["final_read_rank"] == 1
    assert [row["input_index"] for row in rows] == [1, 2]
    assert [row["paper_index"] for row in rows] == [2, 1]
    packets = json.loads((finding / "full_text_reading" / "full_text_packet.json").read_text(encoding="utf-8"))["papers"]
    assert [row["paper_index"] for row in packets] == [2, 1]
    assert projected["reading_validation"]["scoring_complete"] is True


def test_framework_validation_downgrades_incomplete_final_scoring():
    items = [
        {
            "paper": {"id": "a", "title": "A"},
            "full_text_packet": {"full_text_available": True, "full_text_chars": 2000},
            "validation": {"full_text_ready": True, "deep_read_complete": True},
        },
        {
            "paper": {"id": "b", "title": "B"},
            "full_text_packet": {"full_text_available": True, "full_text_chars": 2000},
            "validation": {"full_text_ready": True, "deep_read_complete": True},
        },
    ]
    readings = [
        {"title": "A", "full_text_available": True, "deep_read_complete": True},
        {"title": "B", "full_text_available": True, "deep_read_complete": True},
    ]
    validation = reading_bridge._build_validation(
        find_run_id="find_scoring",
        expected_count=2,
        find_recommendation_count=1,
        source_rows=[item["paper"] for item in items],
        items=items,
        readings=readings,
        claude_mode="run",
        limited=False,
        public_final_artifact_present=True,
        reading_scoring={"status": "complete_with_warnings", "expected_article_count": 2, "scored_article_count": 1},
        scoring_required=True,
    )
    assert validation["valid"] is True
    assert validation["read_quality_complete"] is False
    assert validation["status"] == "current_find_deep_read_complete_with_warnings"
    assert validation["scoring_complete"] is False
    assert validation["scoring_expected_count"] == 2
    assert validation["scoring_scored_count"] == 1
    assert any("统一评分仅覆盖 1/2" in item for item in validation["warnings"])
    assert reading_bridge._project_read_status(validation)[1:] == ("reading_scoring_incomplete", "rerun_current_find_read")

    missing = reading_bridge._build_validation(
        find_run_id="find_scoring",
        expected_count=2,
        find_recommendation_count=1,
        source_rows=[item["paper"] for item in items],
        items=items,
        readings=readings,
        claude_mode="run",
        limited=False,
        public_final_artifact_present=True,
        reading_scoring={},
        scoring_required=True,
    )
    assert missing["scoring_complete"] is False
    assert missing["read_quality_complete"] is False


def test_web_validation_accepts_nonrecommended_rows_selected_from_final_ranking():
    selected = [
        {"id": "recommended", "title": "Recommended", "find_recommendation": True},
        {"id": "ranked_only", "title": "Ranked only", "fit_score": 5.5},
    ]
    validation = project_bridge._current_find_reading_validation(
        {
            "original_strong_recommendations": [selected[0]],
            "selected_reading_rows": selected,
            "screened_ranking": selected,
        },
        [
            {"id": "recommended", "title": "Recommended", "verdict": "core_reading"},
            {"id": "ranked_only", "title": "Ranked only", "verdict": "core_reading"},
        ],
        read_limit=2,
    )
    assert validation["expected_reading_count"] == 2
    assert validation["expected_recommendation_count"] == 1
    assert validation["extra_reading_titles"] == []
    assert validation["invalid_positive_titles"] == []
    assert validation["unlabeled_non_positive_titles"] == []


def test_run_module_passes_resolved_project_count_to_reading(monkeypatch, tmp_path):
    input_json = tmp_path / "input.json"
    input_json.write_text(json.dumps({"articles": [{"title": "A"}]}), encoding="utf-8")
    captured: dict[str, object] = {}
    monkeypatch.setattr(run_module, "configured_max_read_papers", lambda *_args, **_kwargs: 88)
    monkeypatch.setattr(
        run_module,
        "prepare_current_find_read_input",
        lambda *_args, **_kwargs: {"input_json": input_json},
    )
    monkeypatch.setattr(run_module, "module_entry", lambda _stage: tmp_path / "reading_main.py")

    def fake_stream(cmd, *, env, input_text=""):
        captured["cmd"] = cmd
        return 0, "{}"

    monkeypatch.setattr(run_module, "_run_streaming", fake_stream)
    monkeypatch.setattr(
        run_module,
        "sync_current_find_read_outputs",
        lambda *_args, **_kwargs: {
            "status": "current_find_deep_read_complete",
            "public_final_artifact_present": True,
        },
    )
    assert run_module._run_current_find_read_bridge("current_find_research_plan", ["--project", "demo"]) == 0
    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert cmd[cmd.index("--max-papers") + 1] == "88"


def test_run_module_keeps_read_complete_with_warnings_nonzero(monkeypatch, tmp_path):
    input_json = tmp_path / "input.json"
    input_json.write_text(json.dumps({"articles": [{"title": "A"}]}), encoding="utf-8")
    monkeypatch.setattr(run_module, "configured_max_read_papers", lambda *_args, **_kwargs: 50)
    monkeypatch.setattr(run_module, "prepare_current_find_read_input", lambda *_args, **_kwargs: {"input_json": input_json})
    monkeypatch.setattr(run_module, "module_entry", lambda _stage: tmp_path / "reading_main.py")
    monkeypatch.setattr(run_module, "_run_streaming", lambda *_args, **_kwargs: (0, "{}"))
    monkeypatch.setattr(
        run_module,
        "sync_current_find_read_outputs",
        lambda *_args, **_kwargs: {
            "status": "current_find_deep_read_complete_with_warnings",
            "public_final_artifact_present": True,
        },
    )

    assert run_module._run_current_find_read_bridge("current_find_research_plan", ["--project", "demo"]) == 2


def test_project_summary_exposes_read_count_in_config_and_preferences(tmp_path):
    _projects, root = _project(tmp_path, max_read_papers=61)
    config = json.loads((root / "project.json").read_text(encoding="utf-8"))
    assert project_bridge._public_project_identity_config("demo", config)["max_read_papers"] == 61
    preferences = project_bridge._public_run_preferences("demo", root, config)
    assert preferences["max_read_papers"] == 61


def test_project_read_count_save_round_trips_through_compact_summary(monkeypatch, tmp_path):
    projects, root = _project(tmp_path)
    monkeypatch.setattr(project_config, "ROOT", tmp_path)
    monkeypatch.setattr(project_paths, "ROOT", tmp_path)
    monkeypatch.setattr(project_bridge, "PROJECTS", projects)
    project_bridge._cleruntime_caches("demo")

    saved_summary = project_bridge.update_project_config("demo", {"max_read_papers": 100})
    refreshed_summary = project_bridge.project_summary("demo", compact=True)
    saved_config = json.loads((root / "project.json").read_text(encoding="utf-8"))

    assert saved_config["max_read_papers"] == 100
    assert saved_config["reading"]["max_read_papers_source"] == "user"
    assert saved_summary["config"]["max_read_papers"] == 100
    assert saved_summary["run_preferences"]["max_read_papers"] == 100
    assert refreshed_summary["config"]["max_read_papers"] == 100
    assert refreshed_summary["run_preferences"]["max_read_papers"] == 100


def test_supervision_uses_selected_read_count_not_recommendation_count():
    assert run_supervision_tick.reading_validation_is_ready(
        {
            "run_id": "find_n_gt_recommendations",
            "valid": True,
            "policy_version": reading_bridge.READING_POLICY_VERSION,
            "expected_reading_count": 4,
            "selected_reading_count": 4,
            "expected_recommendation_count": 2,
            "actual_reading_count": 4,
            "full_text_reading_count": 4,
            "pending_full_text_reading_count": 0,
            "deep_read_complete_count": 4,
            "pending_deep_read_synthesis_count": 0,
            "read_quality_complete": True,
            "scoring_complete": True,
            "blockers": [],
        },
        "find_n_gt_recommendations",
    ) is True


def test_find_frontend_updates_read_default_after_adopting_find():
    source = (ROOT / "framework" / "scripts" / "orchestration" / "run_frontend.py").read_text(encoding="utf-8")
    assert "update_project_read_default_after_find(project, result, projects_root=paths.root.parent)" in source
