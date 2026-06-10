import importlib.util
import json
import sys
from pathlib import Path


def load_script(name: str):
    root = Path(__file__).resolve().parents[3]
    scripts_dir = root / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    spec = importlib.util.spec_from_file_location(name, root / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_literature_packet_keeps_screened_and_read_as_audit_only():
    packet = load_script("build_literature_tool_packet")
    candidates = packet.extract_candidates({
        "screened_ranking": [{"title": "Screened Only", "score": 9, "reason": "ranked but not promoted"}],
        "read_candidates": [{"title": "Read Only", "score": 8, "reason": "triage only"}],
        "evaluated_candidates": [{"title": "Evaluated Only", "score": 7}],
    })

    assert packet.claim_ready_candidates(candidates) == []
    audit_titles = {row["title"] for row in packet.audit_only_candidates(candidates)}
    assert audit_titles == {"Screened Only", "Read Only", "Evaluated Only"}
    assert all(row["positive_claim_evidence"] is False for row in candidates)


def test_literature_packet_counts_articles_as_claim_ready_anchor():
    packet = load_script("build_literature_tool_packet")
    candidates = packet.extract_candidates({
        "articles": [{"title": "Promoted Strong", "score": 8, "reason": "passed strict gate", "abstract": "This paper presents a reproducible method with real evaluation evidence, detailed experimental protocol, and enough methodological context for the TASTE literature gate to treat the row as a true scored article rather than a title-only placeholder."}],
        "screened_ranking": [{"title": "Screened Only", "score": 9}],
    })

    strong = packet.claim_ready_candidates(candidates)
    assert [row["title"] for row in strong] == ["Promoted Strong"]
    assert strong[0]["claim_ready_anchor"] is True


def test_full_cycle_literature_gate_uses_articles_not_screened_fallback(tmp_path, monkeypatch):
    full = load_script("run_full_research_cycle")
    project = "tmp_lit_gate_test"
    root = full.ROOT / "projects" / project
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    try:
        (root / "project.json").write_text(json.dumps({"name": project, "topic": "x"}), encoding="utf-8")
        (state / "taste_literature_intermediates.json").write_text(json.dumps({
            "candidate_pool_counts": {
                "articles": 0,
                "screened_ranking": 7,
                "read_candidates": 3,
                "evaluated_candidates": 11,
            }
        }), encoding="utf-8")
        (state / "paper_quality.json").write_text(json.dumps({"summary": {"recent_high_priority_count": 1}}), encoding="utf-8")
        (state / "idea_candidates.json").write_text(json.dumps({"summary": {"pursue_count": 1}}), encoding="utf-8")

        args = type("Args", (), {
            "project": project, "topic": "x", "venue": "CIKM", "title": "x",
            "max_cycles": 3, "iterations_per_cycle": 1, "trajectory_rounds": 1,
        })()
        cycle = full.FullCycle(args)
        gate = cycle.literature_gate_status()

        assert gate["strong_recommendations"] == 0
        assert gate["candidate_count"] == 21
        assert gate["status"] == "candidates_but_no_positive_anchor"
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)


def test_full_cycle_write_json_serializes_project_paths(tmp_path):
    full = load_script("run_full_research_cycle")
    project_paths = full.build_paths("sample_project")
    out = tmp_path / "state" / "payload.json"

    full.write_json(out, {"paths": project_paths, "state_dir": project_paths.state})

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert isinstance(payload["paths"], dict)
    assert payload["paths"]["name"] == "sample_project"
    assert payload["paths"]["state"].endswith("/projects/sample_project/state")
    assert payload["state_dir"].endswith("/projects/sample_project/state")


def test_taste_sync_drops_recoverable_fallback_ideas_and_plans(tmp_path):
    sync = load_script("sync_outputs")
    taste_dir = tmp_path / "finding"
    taste_dir.mkdir()
    (taste_dir / "ideas.json").write_text(json.dumps({
        "run_id": "taste_downstream_recoverable_fallback",
        "llm": {"enabled": False},
        "ideas": [{"id": "idea-001", "title": "Fallback idea", "score": 9}],
    }), encoding="utf-8")
    (taste_dir / "plans.json").write_text(json.dumps({
        "run_id": "taste_downstream_recoverable_fallback",
        "plans": [{"plan_id": "plan-001", "title": "Fallback plan"}],
    }), encoding="utf-8")
    (taste_dir / "plan.md").write_text("# fallback plan", encoding="utf-8")

    assert sync.extract_ideas(taste_dir) == []
    plans = sync.extract_plans(taste_dir)
    assert plans["plans_json"] == {}
    assert plans["plan_markdown_excerpt"] == ""
    assert "not synced" in plans["guardrail"]

def test_taste_sync_report_uses_usage_boundary_not_guardrail():
    sync = load_script("sync_outputs")

    report = sync.render_report({
        "generated_at": "2026-06-05T00:00:00+00:00",
        "project": "demo_project",
        "status": "completed",
        "taste_output_dir": "/tmp/finding",
        "counts": {"papers_synced": 1, "audit_candidates_retained": 0, "repos_synced": 0, "ideas_synced": 0, "plans_synced": 0},
        "top_papers": [{"selection_bucket": "recommended", "discovery_priority_score": 8.5, "title": "Demo Paper"}],
        "top_repos": [],
    })

    assert "## 使用边界" in report
    forbidden = ["## Guardrail", "PDF promotion", "paper claims", "paper claim", "claim promotion", "论文 claim", "对实现的直接含义"]
    assert not any(item in report for item in forbidden)
    assert "manuscript conclusions" in report
