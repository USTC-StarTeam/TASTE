import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

from path_helpers import ensure_script_paths


def _load_paper_common():
    ensure_script_paths()
    import paper_common
    import project_paths

    return paper_common, project_paths


def _load_project_config():
    ensure_script_paths()
    import project_config
    import project_paths

    return project_config, project_paths


def _load_project_bridge():
    repo_root = Path(__file__).resolve().parents[1]
    ensure_script_paths()
    from taste_pythonpath import ensure_taste_pythonpath
    ensure_taste_pythonpath(repo_root)
    from auto_research.web import project_bridge

    return project_bridge


def _load_audit_paper_normality():
    ensure_script_paths()
    import audit_paper_normality

    return audit_paper_normality


def _load_audit_paper_figures():
    ensure_script_paths()
    import audit_paper_figures

    return audit_paper_figures


def _load_paper_self_review():
    ensure_script_paths()
    import paper_self_review

    return paper_self_review


def _load_repair_paper_preview_loop():
    ensure_script_paths()
    import repair_paper_preview_loop

    return repair_paper_preview_loop


def _load_vendor_bibtex_format():
    repo_root = Path(__file__).resolve().parents[1]
    path = repo_root / "modules" / "writing" / "vendor" / "PaperOrchestra" / "skills" / "literature-review-agent" / "scripts" / "bibtex_format.py"
    spec = importlib.util.spec_from_file_location("paper_orchestra_bibtex_format", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _load_resolve_venue_requirements():
    ensure_script_paths()
    import resolve_venue_requirements

    return resolve_venue_requirements


def _load_fetch_latex_template():
    ensure_script_paths()
    import fetch_latex_template

    return fetch_latex_template


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")



def test_writing_entrypoints_direct_help_bootstrap_pythonpath():
    repo_root = Path(__file__).resolve().parents[1]
    scripts = [
        repo_root / "modules" / "writing" / "scripts" / "resolve_venue_requirements.py",
        repo_root / "modules" / "writing" / "scripts" / "fetch_latex_template.py",
        repo_root / "modules" / "writing" / "scripts" / "run_paper_pipeline.py",
    ]
    for script in scripts:
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        proc = subprocess.run(
            [sys.executable, str(script), "--help"],
            cwd=repo_root,
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
        )
        assert proc.returncode == 0, f"{script.name}: {proc.stderr}"
        assert "--venue" in proc.stdout


def test_iclr_requirements_resolve_deterministically_without_claude(monkeypatch):
    resolve = _load_resolve_venue_requirements()
    raw_template = (
        "There will be a strict upper limit of 9 pages for the main text of the initial submission, "
        "with unlimited additional pages for citations. "
        "Under review as a conference paper at ICLR 2026 Anonymous authors Paper under double-blind review"
    )

    def fake_fetch(url, timeout=30):
        if "api.github.com" in url:
            return '[{"name":"iclr2026"}]'
        if "AuthorGuide" in url:
            return "<html>Main text should be 9 pages or fewer. The bibliography/references do not count toward the page limit.</html>"
        if "iclr2026_conference.tex" in url:
            return raw_template
        return ""

    monkeypatch.setattr(resolve, "fetch_official_text", fake_fetch)

    payload = resolve.build_iclr_requirements("demo_project", "ICLR")
    payload = resolve.normalize_requirement_payload(payload, "ICLR", "demo_project")

    assert resolve.validate_payload(payload) == []
    assert payload["status"] == "ok"
    assert payload["official_sources"]
    assert any("AuthorGuide" in item["url"] for item in payload["official_sources"])
    assert payload["page_policy"]["body_page_max"] == 9
    assert payload["page_policy"]["reference_page_max"] == 0
    assert payload["template"]["family"] == "iclr"
    assert payload["template"]["archive_url"].endswith("iclr2026.zip")
    assert "iclr2026_conference.tex" in payload["template"]["required_files"]
    assert payload["venue_submission_policy"]["reference_target_source"] == "quality_target"
    assert payload["venue_submission_policy"]["official_min_references"] == 0
    assert payload["venue_submission_policy"]["reference_quality_target"] > 0

def test_repository_clone_timeout_does_not_block_complete_official_venue_contract():
    resolve = _load_resolve_venue_requirements()
    payload = {
        "status": "blocked",
        "venue": "ICLR",
        "track": "conference",
        "official_sources": [
            {
                "url": "https://github.com/ICLR/Master-Template/blob/master/iclr2026/iclr2026_conference.tex",
                "label": "official template tex",
                "evidence": "Official template states the main-text page limit.",
            }
        ],
        "page_policy": {
            "body_page_max": 9,
            "reference_page_max": 0,
            "total_page_max": 0,
            "source_type": "official_template_instruction",
        },
        "citation_policy": {
            "min_verified_references": 40,
            "source_type": "quality_target",
            "estimated_references_per_page": 30,
        },
        "template": {
            "family": "iclr",
            "repository_url": "https://github.com/ICLR/Master-Template.git",
            "archive_url": "https://github.com/ICLR/Master-Template/raw/master/iclr2026.zip",
            "directory_hint": "iclr2026",
            "main_tex": "iclr2026_conference.tex",
            "documentclass": "article",
            "required_files": ["iclr2026_conference.sty", "iclr2026_conference.bst"],
        },
        "venue_submission_policy": {
            "status": "known",
            "venue": "ICLR",
            "body_page_max": 9,
            "template_family": "iclr",
            "reference_quality_target": 40,
            "reference_target_source": "quality_target",
        },
        "venue_template_profile": {"family": "iclr", "documentclass": "article"},
        "blockers": [
            "official repository verification failed: git command timed out after 60s",
            "official repository refresh failed",
        ],
    }

    healed = resolve.heal_verified_venue_payload(payload)

    assert healed["status"] == "ok"
    assert not healed.get("blockers")
    assert resolve.validate_payload(healed) == []


def test_template_fetch_prefers_official_archive_when_requirements_record_archive(tmp_path, monkeypatch):
    fetch = _load_fetch_latex_template()

    def fail_repository(template, source_dir):
        raise AssertionError("repository clone should not run before an explicit official archive")

    def fake_archive(archive_url, archive_dir, source_dir, directory_hint=""):
        source_dir.mkdir(parents=True, exist_ok=True)
        (source_dir / "iclr2026_conference.tex").write_text("template", encoding="utf-8")
        return {
            "source_kind": "archive",
            "source_url": archive_url,
            "archive_path": str(archive_dir / "iclr2026.zip"),
            "source_subdir": directory_hint,
        }

    monkeypatch.setattr(fetch, "sync_from_repository", fail_repository)
    monkeypatch.setattr(fetch, "sync_from_archive", fake_archive)

    metadata = fetch.sync_from_repository_with_archive_fallback(
        {
            "repository_url": "https://github.com/ICLR/Master-Template.git",
            "archive_url": "https://github.com/ICLR/Master-Template/raw/master/iclr2026.zip",
            "directory_hint": "iclr2026",
            "main_tex": "iclr2026_conference.tex",
            "required_files": ["iclr2026_conference.sty"],
        },
        tmp_path / "downloads",
        tmp_path / "source",
    )

    assert metadata["source_kind"] == "archive"
    assert metadata["archive_preferred"] is True
    assert metadata["repository_fallback_used"] is False
    assert (tmp_path / "source" / "iclr2026_conference.tex").exists()


def test_template_fetch_falls_back_to_official_archive_when_repository_clone_times_out(tmp_path, monkeypatch):
    fetch = _load_fetch_latex_template()
    monkeypatch.setenv("VENUE_TEMPLATE_REPOSITORY_FIRST", "1")

    def fail_repository(template, source_dir):
        raise RuntimeError("git command timed out after 60s")

    def fake_archive(archive_url, archive_dir, source_dir, directory_hint=""):
        source_dir.mkdir(parents=True, exist_ok=True)
        (source_dir / "iclr2026_conference.tex").write_text("template", encoding="utf-8")
        return {
            "source_kind": "archive",
            "source_url": archive_url,
            "archive_path": str(archive_dir / "iclr2026.zip"),
            "source_subdir": directory_hint,
        }

    monkeypatch.setattr(fetch, "sync_from_repository", fail_repository)
    monkeypatch.setattr(fetch, "sync_from_archive", fake_archive)

    metadata = fetch.sync_from_repository_with_archive_fallback(
        {
            "repository_url": "https://github.com/ICLR/Master-Template.git",
            "archive_url": "https://github.com/ICLR/Master-Template/raw/master/iclr2026.zip",
            "directory_hint": "iclr2026",
        },
        tmp_path / "downloads",
        tmp_path / "source",
    )

    assert metadata["source_kind"] == "archive"
    assert metadata["repository_fallback_used"] is True
    assert "timed out" in metadata["repository_fetch_error"]
    assert (tmp_path / "source" / "iclr2026_conference.tex").exists()


def test_template_fetched_requires_actual_template_source_or_format_pass():
    project_bridge = _load_project_bridge()

    assert not project_bridge._paper_template_fetched({
        "venue_requirements_status": "ok",
        "venue_requirements_ready": True,
        "template_source": {"status": "failed-official-template-fetch"},
    })
    assert project_bridge._paper_template_fetched({
        "venue_requirements_status": "ok",
        "template_source": {"status": "ok", "official_template": True},
    })
    assert project_bridge._paper_template_fetched({"paper_venue_format_status": "pass"})


def test_workspace_tool_path_uses_configurable_texlive_root(monkeypatch, tmp_path):
    paper_common, _ = _load_paper_common()
    texlive_root = tmp_path / "texlive"
    latexmk = texlive_root / "2027" / "bin" / "x86_64-linux" / "latexmk"
    latexmk.parent.mkdir(parents=True)
    latexmk.write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.setenv("TEXLIVE_ROOT", str(texlive_root))
    candidates = [str(candidate) for candidate in paper_common.texlive_tool_candidates("latexmk")]

    assert str(latexmk) in candidates
    assert paper_common.workspace_tool_path("latexmk") == str(latexmk)


def test_project_venue_update_promotes_current_paper_slot_and_template(tmp_path, monkeypatch):
    project_config, project_paths = _load_project_config()
    monkeypatch.setattr(project_paths, "ROOT", tmp_path)
    monkeypatch.setattr(project_config, "ROOT", tmp_path)
    project = "demo_project"
    root = tmp_path / "projects" / project
    _write_json(
        root / "project.json",
        {
            "name": project,
            "target_venue": "Nature",
            "venue": "Nature",
            "paper": {
                "target_venue": "Nature",
                "template_family": "springer-nature",
                "template_source_url": "https://www.springernature.com/gp/authors/campaigns/latex-author-support",
            },
        },
    )
    _write_json(
        root / "paper" / "metadata" / "paper_pipeline.json",
        {
            "active_venue": "nature",
            "venue": "Nature",
            "target_venue": "Nature",
            "pdf_path": "/tmp/nature.pdf",
            "venues": {
                "nature": {"venue": "Nature", "venue_slug": "nature", "pdf_path": "/tmp/nature.pdf"},
                "iclr": {"venue": "ICLR", "venue_slug": "iclr", "blocked_preview_pdf": "/tmp/iclr.pdf", "paper_venue_format_status": "pass"},
            },
        },
    )
    stale_job = {
        "status": "stale",
        "venue": "Nature",
        "cmd": "run_full_research_cycle.py --project demo_project --venue Nature",
        "command": "run_full_research_cycle.py --project demo_project --venue Nature",
        "process_alive": False,
        "alive": False,
    }
    _write_json(
        root / "state" / "full_research_cycle.json",
        {
            "status": "stale_full_research_cycle_snapshot",
            "venue": "Nature",
            "target_venue": "Nature",
            "full_cycle_job": dict(stale_job),
            "latest_gate": {
                "accepted_preview": True,
                "submission_ready": True,
                "complete": True,
                "latest_pdf": "/tmp/project/paper/output/nature/paper.pdf",
                "latest_pdf_info": {"path": "/tmp/project/paper/output/nature/paper.pdf", "exists": True},
                "paper_status": {
                    "conference_preview_ready": True,
                    "venue_submission_policy": {
                        "venue": "Nature",
                        "source_url": "https://www.springernature.com/gp/authors/campaigns/latex-author-support",
                        "required_documentclass_options": ["pdflatex", "sn-nature"],
                    },
                },
                "submission_readiness": {
                    "metrics": {"venue_policy_source": "https://www.springernature.com/gp/authors/campaigns/latex-author-support"}
                },
                "blocker_action_plan": {
                    "top_actions": [
                        {"recommended_commands": ["framework/scripts/run_supervision_tick.py --project demo_project --venue nature --supervise"]}
                    ]
                },
            },
        },
    )
    _write_json(
        root / "state" / "submission_readiness.json",
        {
            "status": "submission_ready",
            "venue": "Nature",
            "submission_ready": True,
            "checks": [
                {
                    "name": "pdf_compiled",
                    "severity": "pass",
                    "detail": "pdf_path=/tmp/project/paper/output/nature/paper.pdf pdf_ready=True",
                }
            ],
            "metrics": {
                "venue_policy_source": "https://www.springernature.com/gp/authors/campaigns/latex-author-support",
                "venue_body_pages": 11,
            },
            "paper_self_review_evidence_blockers": [
                {
                    "category": "results_contains_untested_design_space",
                    "detail": "In a Nature article this old issue should not be reused after an ICLR venue change.",
                    "evidence": ["/tmp/project/paper/writing/nature/workspace/final/paper.tex"],
                }
            ],
        },
    )
    _write_json(root / "state" / "supervision_tick.json", {"status": "stale_full_research_cycle_snapshot", "target_venue": "NATURE", "full_cycle_job": dict(stale_job)})
    _write_json(
        root / "state" / "paper_evidence_audit.json",
        {
            "status": "blocked",
            "venue": "Nature",
            "issues": ["In a Nature article this old issue should not be shown after switching to ICLR."],
            "paper_self_review_evidence_blockers": [
                {"detail": "Nature expects statements from the old venue.", "evidence": ["/tmp/project/paper/venues/nature/venue_requirements.json"]}
            ],
        },
    )
    _write_json(root / "state" / "full_cycle_job.json", dict(stale_job))
    _write_json(
        root / "state" / "paper_preview_repair_loop.json",
        {
            "project": project,
            "venue": "Nature",
            "title": "Demo Paper",
            "status": "pass",
            "pdf_after": "/tmp/project/paper/output/nature/paper.pdf",
            "final": {"pdf_path": "/tmp/project/paper/output/nature/paper.pdf"},
            "venue_contract": {"source_url": "https://www.springernature.com/gp/authors/campaigns/latex-author-support"},
        },
    )
    _write_json(
        root / "state" / "paper_preview_self_review.json",
        {
            "project": project,
            "venue": "Nature",
            "status": "passed",
            "artifact_fingerprints": {"pdf": "/tmp/project/paper/output/nature/paper.pdf"},
            "remaining_blockers": [],
            "final_checks": [{"name": "pdf", "status": "pass"}],
        },
    )
    _write_json(
        root / "state" / "paper_orchestra_bridge.json",
        {
            "project": project,
            "venue": "Nature",
            "status": "blocked",
            "source": {"template": "https://www.springernature.com/gp/authors/campaigns/latex-author-support"},
            "phases": [{"workspace": "/tmp/project/paper/writing/nature/workspace"}],
        },
    )
    _write_json(
        root / "state" / "paper_figure_quality_audit.json",
        {
            "project": project,
            "venue": "Nature",
            "status": "pass",
            "figure_quality_ready": True,
            "source_path": "/tmp/project/paper/output/nature/paper.tex",
            "figures": [{"path": "/tmp/project/paper/output/nature/figure.pdf", "status": "pass"}],
        },
    )
    _write_json(
        root / "state" / "paper_orchestra_audit.json",
        {
            "project": project,
            "venue": "Nature",
            "status": "hold",
            "issues": ["Nature expects old front matter."],
            "report": "/tmp/project/paper/writing/nature/workspace/report.md",
        },
    )

    cfg = project_config.update_project_settings(project, {"target_venue": "ICLR", "venue": "ICLR"})

    assert cfg["target_venue"] == "ICLR"
    assert cfg["paper"]["template_family"] == "iclr"
    assert cfg["paper"]["template_source_url"] == "https://github.com/ICLR/Master-Template"
    state = json.loads((root / "paper" / "metadata" / "paper_pipeline.json").read_text(encoding="utf-8"))
    assert state["active_venue"] == "iclr"
    assert state["venue"] == "ICLR"
    assert state["target_venue"] == "ICLR"
    assert state["template_family"] == "iclr"
    assert state["template_source_url"] == "https://github.com/ICLR/Master-Template"
    assert state["blocked_preview_pdf"] == "/tmp/iclr.pdf"
    assert state.get("pdf_path") != "/tmp/nature.pdf"
    metadata = json.loads((root / "paper" / "metadata" / "paper_metadata.json").read_text(encoding="utf-8"))
    assert metadata["target_venue"] == "ICLR"
    assert metadata["venue_slug"] == "iclr"
    assert metadata["template_family"] == "iclr"
    assert metadata["template_source_url"] == "https://github.com/ICLR/Master-Template"
    full_cycle = json.loads((root / "state" / "full_research_cycle.json").read_text(encoding="utf-8"))
    assert full_cycle["target_venue"] == "ICLR"
    assert full_cycle["venue"] == "ICLR"
    assert full_cycle["full_cycle_job"]["target_venue"] == "ICLR"
    assert full_cycle["full_cycle_job"]["launch_venue"] == "Nature"
    assert "cmd" not in full_cycle["full_cycle_job"]
    assert "command" not in full_cycle["full_cycle_job"]
    assert "venue" not in full_cycle["full_cycle_job"]
    latest_gate = full_cycle["latest_gate"]
    latest_gate_payload = json.dumps(latest_gate, ensure_ascii=False).lower()
    assert latest_gate["accepted_preview"] is False
    assert latest_gate["submission_ready"] is False
    assert latest_gate["complete"] is False
    assert latest_gate["venue_change_invalidated_paper_snapshot"] is True
    assert latest_gate["paper_snapshot_invalidated_for_venue"] == "ICLR"
    assert "latest_pdf" not in latest_gate
    assert "paper_status" not in latest_gate
    assert "submission_readiness" not in latest_gate
    assert "paper/output/nature" not in latest_gate_payload
    assert "springernature.com" not in latest_gate_payload
    assert "--venue nature" not in latest_gate_payload
    readiness = json.loads((root / "state" / "submission_readiness.json").read_text(encoding="utf-8"))
    assert readiness["venue"] == "ICLR"
    assert readiness["submission_ready"] is False
    assert readiness["status"] == "blocked"
    assert readiness["venue_refresh_required"] is True
    readiness_payload = json.dumps(readiness, ensure_ascii=False).lower()
    assert "paper/output/nature" not in readiness_payload
    assert "paper/writing/nature" not in readiness_payload
    assert "springernature.com" not in readiness_payload
    assert readiness["failed_checks"][0]["id"] == "venue_readiness_refresh_required"
    paper_audit = json.loads((root / "state" / "paper_evidence_audit.json").read_text(encoding="utf-8"))
    paper_audit_payload = json.dumps(paper_audit, ensure_ascii=False).lower()
    assert paper_audit["venue_refresh_required"] is True
    assert "nature article" not in paper_audit_payload
    assert "nature expects" not in paper_audit_payload
    assert "paper/venues/nature" not in paper_audit_payload
    tick = json.loads((root / "state" / "supervision_tick.json").read_text(encoding="utf-8"))
    assert tick["target_venue"] == "ICLR"
    assert tick["venue"] == "ICLR"
    assert tick["venue_slug"] == "iclr"
    assert tick["full_cycle_job"]["target_venue"] == "ICLR"
    assert tick["full_cycle_job"]["launch_venue"] == "Nature"
    assert "cmd" not in tick["full_cycle_job"]
    assert "command" not in tick["full_cycle_job"]
    assert "venue" not in tick["full_cycle_job"]
    stored_job = json.loads((root / "state" / "full_cycle_job.json").read_text(encoding="utf-8"))
    assert stored_job["target_venue"] == "ICLR"
    assert stored_job["launch_venue"] == "Nature"
    assert "cmd" not in stored_job
    assert "command" not in stored_job
    for filename in [
        "paper_preview_repair_loop.json",
        "paper_preview_self_review.json",
        "paper_orchestra_bridge.json",
        "paper_figure_quality_audit.json",
        "paper_orchestra_audit.json",
    ]:
        aux = json.loads((root / "state" / filename).read_text(encoding="utf-8"))
        aux_payload = json.dumps(aux, ensure_ascii=False).lower()
        assert aux["venue"] == "ICLR"
        assert aux["target_venue"] == "ICLR"
        assert aux["venue_slug"] == "iclr"
        assert aux["status"] == "blocked"
        assert aux["venue_refresh_required"] is True
        assert "nature" not in aux_payload
        assert "springernature" not in aux_payload


def test_project_venue_update_cleans_half_synced_paper_evidence_issues(tmp_path, monkeypatch):
    project_config, project_paths = _load_project_config()
    monkeypatch.setattr(project_paths, "ROOT", tmp_path)
    monkeypatch.setattr(project_config, "ROOT", tmp_path)
    project = "demo_project"
    root = tmp_path / "projects" / project
    _write_json(
        root / "project.json",
        {
            "name": project,
            "target_venue": "ICLR",
            "venue": "ICLR",
            "paper": {"target_venue": "ICLR", "venue_slug": "iclr", "template_family": "iclr"},
        },
    )
    _write_json(root / "paper" / "metadata" / "paper_pipeline.json", {"active_venue": "iclr", "venue": "ICLR", "venues": {"iclr": {"venue": "ICLR"}}})
    _write_json(
        root / "state" / "paper_evidence_audit.json",
        {
            "status": "blocked",
            "venue": "ICLR",
            "target_venue": "ICLR",
            "venue_slug": "iclr",
            "issues": ["In a Nature article this stale issue must not survive a no-op ICLR save."],
        },
    )

    project_config.update_project_settings(project, {"target_venue": "ICLR", "venue": "ICLR"})

    paper_audit = json.loads((root / "state" / "paper_evidence_audit.json").read_text(encoding="utf-8"))
    payload = json.dumps(paper_audit, ensure_ascii=False).lower()
    assert paper_audit["venue_refresh_required"] is True
    assert "nature article" not in payload
    assert paper_audit["issues"][0].startswith("Target venue is now ICLR")


def test_paper_action_uses_saved_project_venue_over_stale_payload(tmp_path, monkeypatch):
    project_bridge = _load_project_bridge()
    project = "demo_project"
    root = tmp_path / "projects" / project
    root.mkdir(parents=True)
    _write_json(root / "project.json", {"name": project, "target_venue": "ICLR", "venue": "ICLR"})
    monkeypatch.setattr(project_bridge, "PROJECTS", tmp_path / "projects")
    monkeypatch.setattr(project_bridge, "project_target_venue", lambda _project, default="": "ICLR")

    returned_project, cmd = project_bridge.build_command({"action": "paper", "project": project, "venue": "Nature"})

    assert returned_project == project
    assert cmd[cmd.index("--venue") + 1] == "ICLR"
    assert "Nature" not in cmd


def test_paper_state_path_rejects_other_venue_output(tmp_path):
    project_bridge = _load_project_bridge()
    root = tmp_path / "project"
    nature_pdf = root / "paper" / "output" / "nature" / "paper.pdf"
    iclr_pdf = root / "paper" / "output" / "iclr" / "paper.pdf"
    nature_pdf.parent.mkdir(parents=True)
    iclr_pdf.parent.mkdir(parents=True)
    nature_pdf.write_bytes(b"nature pdf")
    iclr_pdf.write_bytes(b"iclr pdf")

    state = {"venue_slug": "iclr", "blocked_preview_pdf": str(nature_pdf), "latest_preview_pdf": str(iclr_pdf)}

    assert project_bridge._paper_state_path(root, state, "blocked_preview_pdf") is None
    assert project_bridge._paper_state_path(root, state, "blocked_preview_pdf", "latest_preview_pdf") == iclr_pdf


def test_full_cycle_summary_rejects_stale_latest_gate_pdf_from_other_venue(tmp_path, monkeypatch):
    project_bridge = _load_project_bridge()
    project = "demo_project"
    root = tmp_path / "projects" / project
    nature_pdf = root / "paper" / "output" / "nature" / "paper.pdf"
    iclr_pdf = root / "paper" / "output" / "iclr" / "paper.pdf"
    nature_pdf.parent.mkdir(parents=True)
    iclr_pdf.parent.mkdir(parents=True)
    nature_pdf.write_bytes(b"nature pdf")
    iclr_pdf.write_bytes(b"iclr pdf")
    _write_json(root / "project.json", {"name": project, "target_venue": "ICLR", "venue": "ICLR"})
    _write_json(
        root / "state" / "full_research_cycle.json",
        {
            "status": "blocked_after_max_cycles",
            "target_venue": "ICLR",
            "venue": "ICLR",
            "latest_gate": {
                "accepted_preview": True,
                "latest_pdf": str(nature_pdf),
                "latest_pdf_info": {"path": str(nature_pdf), "exists": True},
            },
        },
    )
    monkeypatch.setattr(project_bridge, "_live_full_cycle_process", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        project_bridge,
        "_active_paper_state",
        lambda _root, _project, _cfg=None, venue="": {
            "venue": "ICLR",
            "target_venue": "ICLR",
            "venue_slug": "iclr",
            "active_venue": "iclr",
            "blocked_preview_pdf": str(iclr_pdf),
            "latest_preview_pdf": str(iclr_pdf),
            "pdf_ready": True,
            "conference_preview_ready": True,
            "normal_preview_ready": True,
            "venue_template_format_ready": True,
            "paper_figure_quality_ready": True,
        },
    )

    summary = project_bridge._full_cycle_summary(root)
    payload = json.dumps(summary, ensure_ascii=False)

    assert str(nature_pdf) not in payload
    assert summary["latest_pdf_path"] == str(iclr_pdf)
    assert summary["latest_pdf_info"]["path"] == str(iclr_pdf)


def test_submission_blockers_hide_stale_other_venue_checks(tmp_path, monkeypatch):
    project_bridge = _load_project_bridge()
    project = "demo_project"
    root = tmp_path / "projects" / project
    _write_json(root / "project.json", {"name": project, "target_venue": "ICLR", "venue": "ICLR"})
    _write_json(
        root / "state" / "submission_readiness.json",
        {
            "status": "blocked",
            "venue": "ICLR",
            "target_venue": "ICLR",
            "submission_ready": False,
            "failed_checks": [
                {
                    "name": "pdf_compiled",
                    "severity": "block",
                    "detail": "pdf_path=/tmp/project/paper/output/nature/paper.pdf pdf_ready=False",
                }
            ],
            "metrics": {"venue_policy_source": "https://www.springernature.com/gp/authors/campaigns/latex-author-support"},
        },
    )

    blockers = project_bridge._current_submission_blockers(root)
    payload = json.dumps(blockers, ensure_ascii=False)

    assert len(blockers) == 1
    assert "Target venue is now ICLR" in payload
    assert "paper/output/nature" not in payload
    assert "springernature.com" not in payload


def test_fast_project_summary_hides_stale_full_cycle_venue_and_command(tmp_path, monkeypatch):
    from auto_research.web import project_bridge

    project = "demo_project"
    root = tmp_path / "projects" / project
    state_dir = root / "state"
    state_dir.mkdir(parents=True)
    old_command = "/env/bin/python run_full_research_cycle.py --project demo_project --venue Nature"
    stale_job = {
        "project": project,
        "venue": "Nature",
        "status": "stale",
        "pid": "123",
        "cmd": old_command,
        "command": old_command,
        "process_alive": False,
        "alive": False,
        "log_path": str(root / "logs" / "old.log"),
    }
    _write_json(
        state_dir / "full_research_cycle.json",
        {
            "status": "stale_full_research_cycle_snapshot",
            "target_venue": "ICLR",
            "venue": "ICLR",
            "summary": "old stopped cycle",
            "full_cycle_job": stale_job,
        },
    )
    _write_json(state_dir / "supervision_tick.json", {"status": "running", "full_cycle_job": stale_job})
    monkeypatch.setattr(project_bridge, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(project_bridge, "_remote_process_rows", lambda: [])
    monkeypatch.setattr(project_bridge, "project_source_selection", lambda _project: {})
    monkeypatch.setattr(
        project_bridge,
        "_cached_runtime_diagnostics",
        lambda _project, _cfg: {"project": _project, "runtime": {}, "checks": {}, "status": "ready"},
    )
    monkeypatch.setattr(
        project_bridge,
        "_active_paper_state",
        lambda _root, _project, _cfg, venue="": {"venue": venue, "status": "blocked"},
    )

    summary = project_bridge._fast_project_summary(
        project,
        root,
        {"name": project, "topic": "demo", "target_venue": "ICLR", "venue": "ICLR", "paper": {"target_venue": "ICLR"}},
    )

    payload = json.dumps(summary, ensure_ascii=False)
    assert summary["target_venue"] == "ICLR"
    assert summary["venue"] == "ICLR"
    assert summary["run_preferences"]["target_venue"] == "ICLR"
    assert summary["run_preferences"]["venue"] == "ICLR"
    assert summary["stages"]["paper"]["venue"] == "ICLR"
    assert summary["stages"]["paper"]["target_venue"] == "ICLR"
    assert summary["stages"]["paper"]["venue_slug"] == "iclr"
    assert summary["full_research_cycle"]["full_cycle_job"]["target_venue"] == "ICLR"
    assert summary["full_research_cycle"]["full_cycle_job"]["process_alive"] is False
    assert "--venue Nature" not in payload
    assert '"venue": "Nature"' not in payload
    assert "cmd" not in summary["full_research_cycle"]["full_cycle_job"]
    assert "command" not in summary["full_research_cycle"]["full_cycle_job"]


def test_active_paper_state_follows_project_target_venue(tmp_path, monkeypatch):
    paper_common, project_paths = _load_paper_common()
    monkeypatch.setattr(project_paths, "ROOT", tmp_path)
    project = "demo_project"
    root = tmp_path / "projects" / project
    _write_json(root / "project.json", {"name": project, "target_venue": "ICLR", "venue": "ICLR"})
    _write_json(
        root / "paper" / "metadata" / "paper_pipeline.json",
        {
            "active_venue": "nature",
            "venue": "Nature",
            "paper_normality_status": "pass",
            "conference_preview_ready": True,
            "venues": {
                "nature": {"venue": "Nature", "venue_slug": "nature", "paper_normality_status": "pass", "conference_preview_ready": True},
                "iclr": {"venue": "ICLR", "venue_slug": "iclr", "paper_normality_status": "blocked", "conference_preview_ready": False},
            },
        },
    )

    state = paper_common.get_active_paper_state(project)

    assert state["venue"] == "ICLR"
    assert state["venue_slug"] == "iclr"
    assert state["paper_normality_status"] == "blocked"
    assert state["conference_preview_ready"] is False


def test_explicit_paper_venue_state_does_not_inherit_other_venue_scoped_fields(tmp_path, monkeypatch):
    paper_common, project_paths = _load_paper_common()
    monkeypatch.setattr(project_paths, "ROOT", tmp_path)
    project = "demo_project"
    root = tmp_path / "projects" / project
    _write_json(root / "project.json", {"name": project, "target_venue": "Nature", "venue": "Nature"})
    _write_json(
        root / "paper" / "metadata" / "paper_pipeline.json",
        {
            "active_venue": "nature",
            "venue": "Nature",
            "venue_slug": "nature",
            "paper_normality_status": "pass",
            "conference_preview_ready": True,
            "pdf_path": "/tmp/nature.pdf",
            "venues": {
                "nature": {"venue": "Nature", "venue_slug": "nature", "paper_normality_status": "pass", "conference_preview_ready": True, "pdf_path": "/tmp/nature.pdf"},
                "iclr": {"venue": "ICLR", "venue_slug": "iclr", "paper_normality_status": "blocked", "conference_preview_ready": False, "pdf_path": "/tmp/iclr.pdf"},
            },
        },
    )

    state = paper_common.get_active_paper_state(project, venue="ICLR")

    assert state["venue"] == "ICLR"
    assert state["venue_slug"] == "iclr"
    assert state["paper_normality_status"] == "blocked"
    assert state["conference_preview_ready"] is False
    assert state["pdf_path"] == "/tmp/iclr.pdf"


def test_missing_paper_venue_slot_is_not_promoted_from_previous_active_venue(tmp_path, monkeypatch):
    paper_common, project_paths = _load_paper_common()
    monkeypatch.setattr(project_paths, "ROOT", tmp_path)
    project = "demo_project"
    root = tmp_path / "projects" / project
    _write_json(root / "project.json", {"name": project, "target_venue": "New Journal", "venue": "New Journal"})
    _write_json(
        root / "paper" / "metadata" / "paper_pipeline.json",
        {
            "active_venue": "nature",
            "venue": "Nature",
            "paper_normality_status": "pass",
            "conference_preview_ready": True,
            "pdf_path": "/tmp/nature.pdf",
            "venues": {
                "nature": {"venue": "Nature", "venue_slug": "nature", "paper_normality_status": "pass", "conference_preview_ready": True, "pdf_path": "/tmp/nature.pdf"}
            },
        },
    )

    state = paper_common.get_active_paper_state(project)

    assert state["venue"] == "New Journal"
    assert state["venue_slug"] == "new-journal"
    assert "paper_normality_status" not in state
    assert "conference_preview_ready" not in state
    assert "pdf_path" not in state



def test_springer_nature_rejects_abstract_environment_front_matter(tmp_path, monkeypatch):
    paper_common, project_paths = _load_paper_common()
    monkeypatch.setattr(project_paths, "ROOT", tmp_path)
    project = "demo_project"
    root = tmp_path / "projects" / project
    _write_json(root / "project.json", {"name": project, "target_venue": "Nature", "venue": "Nature"})
    _write_json(
        root / "paper" / "venues" / "nature" / "venue_requirements.json",
        {
            "venue": "Nature",
            "status": "pass",
            "template": {"family": "springer-nature"},
            "venue_template_profile": {
                "family": "springer-nature",
                "documentclass": "sn-jnl",
                "required_options": ["pdflatex", "sn-nature"],
                "forbidden_documentclasses": ["article", "acmart", "llncs", "IEEEtran"],
            },
            "venue_submission_policy": {"template_family": "springer-nature", "status": "known"},
        },
    )
    tex = r"""
\documentclass[pdflatex,sn-nature]{sn-jnl}
\begin{document}
\title[Short]{Example Candidate Method}
\author*{\fnm{Anonymous} \sur{Authors}}
\begin{abstract}
Diffusion models have emerged as a powerful paradigm.
\end{abstract}
\maketitle
\section{Introduction} Text.
\end{document}
"""

    validation = paper_common.validate_venue_template_format(tex, "Nature", project=project)

    assert validation["status"] == "block"
    assert any("abstract environment" in item for item in validation["failures"])


def test_springer_nature_front_matter_normalization_produces_macro(tmp_path, monkeypatch):
    paper_common, project_paths = _load_paper_common()
    monkeypatch.setattr(project_paths, "ROOT", tmp_path)
    project = "demo_project"
    root = tmp_path / "projects" / project
    _write_json(root / "project.json", {"name": project, "target_venue": "Nature", "venue": "Nature"})
    _write_json(
        root / "paper" / "venues" / "nature" / "venue_requirements.json",
        {
            "venue": "Nature",
            "status": "pass",
            "template": {"family": "springer-nature"},
            "venue_template_profile": {
                "family": "springer-nature",
                "documentclass": "sn-jnl",
                "required_options": ["pdflatex", "sn-nature"],
                "forbidden_documentclasses": ["article", "acmart", "llncs", "IEEEtran"],
            },
            "venue_submission_policy": {"template_family": "springer-nature", "status": "known"},
        },
    )
    tex = r"""
\documentclass[pdflatex,sn-nature]{sn-jnl}
\begin{document}
\title[Short]{Example Candidate Method}
\author*{\fnm{Anonymous} \sur{Authors}}
\begin{abstract}
Diffusion models have emerged as a powerful paradigm.
\end{abstract}
\maketitle
\section{Introduction} Text.
\end{document}
"""

    fixed, changes = paper_common.normalize_venue_front_matter(tex, "Nature", project=project)
    validation = paper_common.validate_venue_template_format(fixed, "Nature", project=project)

    assert "springer_nature_abstract_environment_converted_to_macro" in changes
    assert r"\begin{abstract}" not in fixed
    assert r"\abstract{" in fixed
    assert fixed.index(r"\abstract{") < fixed.index(r"\maketitle")
    assert validation["status"] == "pass"


def test_springer_nature_blocks_placeholder_affiliation_front_matter(tmp_path, monkeypatch):
    paper_common, project_paths = _load_paper_common()
    monkeypatch.setattr(project_paths, "ROOT", tmp_path)
    project = "demo_project"
    root = tmp_path / "projects" / project
    _write_json(root / "project.json", {"name": project, "target_venue": "Nature", "venue": "Nature"})
    _write_json(
        root / "paper" / "venues" / "nature" / "venue_requirements.json",
        {
            "venue": "Nature",
            "status": "pass",
            "template": {"family": "springer-nature"},
            "venue_template_profile": {
                "family": "springer-nature",
                "documentclass": "sn-jnl",
                "required_options": ["pdflatex", "sn-nature"],
                "forbidden_documentclasses": ["article", "acmart", "llncs", "IEEEtran"],
            },
            "venue_submission_policy": {"template_family": "springer-nature", "status": "known"},
        },
    )
    tex = r"""
\documentclass[pdflatex,sn-nature]{sn-jnl}
\begin{document}
\title[Short]{Example Candidate Method}
\author*[1]{\fnm{Anonymous} \sur{Authors}}
\affil*[1]{\orgdiv{Department}, \orgname{Institution}, \orgaddress{\city{City}, \country{Country}}}
\abstract{Diffusion models have emerged as a powerful paradigm.}
\maketitle
\section{Introduction} Text.
\end{document}
"""

    validation = paper_common.validate_venue_template_format(tex, "Nature", project=project)
    fixed, changes = paper_common.normalize_venue_front_matter(tex, "Nature", project=project)
    fixed_validation = paper_common.validate_venue_template_format(fixed, "Nature", project=project)

    assert validation["status"] == "block"
    assert any("placeholder affiliation" in item or "corresponding-author" in item for item in validation["failures"])
    assert "springer_nature_anonymous_author_unlinked_from_placeholder_affiliation" in changes
    assert "springer_nature_placeholder_affiliation_removed" in changes
    assert r"\author{\fnm{Anonymous} \sur{Authors}}" in fixed
    assert r"\affil" not in fixed
    assert fixed_validation["status"] == "pass"


def test_springer_nature_pdf_front_matter_blocks_cropped_abstract_and_placeholder(monkeypatch, tmp_path):
    paper_common, _ = _load_paper_common()
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    tex = r"""
\documentclass[pdflatex,sn-nature]{sn-jnl}
\begin{document}
\title[Short]{Example Candidate Method}
\author*[1]{\fnm{Anonymous} \sur{Authors}}
\affil*[1]{\orgdiv{Department}, \orgname{Institution}, \orgaddress{\city{City}, \country{Country}}}
\abstract{Diffusion models have emerged as a powerful paradigm for sequential recommendation.}
\maketitle
\section{Introduction} Text.
\end{document}
"""

    monkeypatch.setattr(
        paper_common,
        "pdf_first_page_text",
        lambda _: {
            "available": True,
            "text": "iffusion models have emerged as a powerful paradigm Anonymous Authors1* Department Institution City Country",
            "lines": [
                "iffusion models have emerged as a powerful paradigm",
                "Anonymous Authors1*",
                "Department, Institution, City, Country.",
            ],
            "error": "",
        },
    )

    failures, first_page = paper_common.springer_nature_pdf_front_matter_failures(pdf, tex, "Nature", project="demo_project")

    assert first_page["available"] is True
    assert any("cropped abstract word" in item for item in failures)
    assert any("corresponding-author footnote" in item for item in failures)
    assert any("placeholder affiliation" in item for item in failures)

def _write_nature_requirements(root: Path, project: str) -> None:
    _write_json(
        root / "paper" / "venues" / "nature" / "venue_requirements.json",
        {
            "venue": "Nature",
            "status": "pass",
            "template": {"family": "springer-nature"},
            "paper_shape": {
                "canonical_sections": ["Introduction", "Results", "Discussion", "Methods"],
                "required_back_matter": ["Data availability", "Code availability"],
                "nature_family_article_mode": True,
            },
            "venue_template_profile": {
                "family": "springer-nature",
                "documentclass": "sn-jnl",
                "required_options": ["pdflatex", "sn-nature"],
                "forbidden_documentclasses": ["article", "acmart", "llncs", "IEEEtran"],
            },
            "venue_submission_policy": {
                "template_family": "springer-nature",
                "status": "known",
                "canonical_sections": ["Introduction", "Results", "Discussion", "Methods"],
                "required_back_matter": ["Data availability", "Code availability"],
                "data_availability_expected": True,
                "code_availability_expected": True,
            },
        },
    )


def test_springer_nature_article_shape_blocks_conference_sections_and_keywords(tmp_path, monkeypatch):
    paper_common, project_paths = _load_paper_common()
    monkeypatch.setattr(project_paths, "ROOT", tmp_path)
    project = "demo_project"
    root = tmp_path / "projects" / project
    _write_json(root / "project.json", {"name": project, "target_venue": "Nature", "venue": "Nature"})
    _write_nature_requirements(root, project)
    tex = r'''
\documentclass[pdflatex,sn-nature]{sn-jnl}
\begin{document}
\title[Short]{Example Candidate Method}
\author{\fnm{Anonymous} \sur{Authors}}
\abstract{A compact unstructured abstract.}
\keywords{Diffusion Models, Recommendation}
\maketitle
\section{Introduction} Text.
\section{Related Work} Text.
\section{Methods} Text.
\section{Experiments} Text.
\section{Discussion} Text.
\section{Conclusion} Text.
\bmhead{Data availability} Data.
\bmhead{Code availability} Code.
\bibliography{refs}
\end{document}
'''

    failures = paper_common.springer_nature_article_shape_failures(tex, "Nature", project=project)

    assert any("Keywords" in item for item in failures)
    assert any("Related Work" in item for item in failures)
    assert any("Experiments" in item for item in failures)
    assert any("Conclusion" in item for item in failures)
    assert any("missing Nature-family article sections: Results" in item for item in failures)


def test_springer_nature_article_shape_accepts_article_sections_and_back_matter(tmp_path, monkeypatch):
    paper_common, project_paths = _load_paper_common()
    monkeypatch.setattr(project_paths, "ROOT", tmp_path)
    project = "demo_project"
    root = tmp_path / "projects" / project
    _write_json(root / "project.json", {"name": project, "target_venue": "Nature", "venue": "Nature"})
    _write_nature_requirements(root, project)
    tex = r'''
\documentclass[pdflatex,sn-nature]{sn-jnl}
\begin{document}
\title[Short]{Example Candidate Method}
\author{\fnm{Anonymous} \sur{Authors}}
\abstract{A compact unstructured abstract.}
\maketitle
\section{Introduction} Text.
\section{Results} Text.
\section{Discussion} Text.
\section{Methods} Text.
\bmhead{Data availability} Data.
\bmhead{Code availability} Code.
\bibliography{refs}
\end{document}
'''

    failures = paper_common.springer_nature_article_shape_failures(tex, "Nature", project=project)

    assert failures == []




def test_figure_audit_toy_term_uses_word_boundary_for_dataset_names():
    audit = _load_audit_paper_figures()

    real_dataset_table = r"""
\begin{table}[t]
\caption{Statistics for verified Amazon Toys and Yelp benchmark datasets.}
\label{tab:dataset_stats}
\centering
\small
\begin{tabular}{lrrr}
Dataset & Users & Items & Interactions \\
Amazon Toys & 19412 & 11924 & 167597 \\
Yelp & 30431 & 20033 & 316354 \\
\end{tabular}
\end{table}
"""
    toy_probe_table = r"""
\begin{table}[t]
\caption{Toy smoke test statistics.}
\label{tab:toy}
\centering
\begin{tabular}{lr}
Dataset & Value \\
Toy & 1 \\
\end{tabular}
\end{table}
"""

    real_rows = audit.table_rows(real_dataset_table)
    toy_rows = audit.table_rows(toy_probe_table)

    assert real_rows[0]["status"] in {"pass", "warn"}
    assert not any("toy" in issue.lower() for issue in real_rows[0]["issues"])
    assert toy_rows[0]["status"] == "block"
    assert any("toy" in issue.lower() for issue in toy_rows[0]["issues"])


def test_citation_render_helpers_detect_author_style_failures():
    audit = _load_audit_paper_normality()
    log = """
Package natbib Warning: Author undefined for citation`refmodel' on input line 56.
LaTeX Warning: Citation `missing-key' on page 1 undefined on input line 57.
There were undefined citations.
"""

    warnings = audit.latex_citation_warning_findings(log)
    bibtex = audit.bibtex_error_findings("""
You can't pop an empty literal stack for entry refmodel
while executing---line 3431 of file sn-nature.bst
(There were 1 error messages)
Bibtex errors: See file 'paper.blg'
""")
    latex_errors = audit.latex_compile_error_findings(
        "! Missing $ inserted.\n"
        "! Missing } inserted.\n"
        "! Extra }, or forgotten \\endgroup.\n"
    )
    markers = audit.pdf_unresolved_citation_markers("DiffRec renders as (author?) [10].\nGood numeric citation [3].\nAnother unresolved ?? marker.")
    commands = audit.textual_citation_commands(r"Prior work \citet{refmodel,diffrec} contrasts with \citeauthor{p5}.")

    assert warnings["author_undefined_keys"] == ["refmodel"]
    assert "missing-key" in warnings["undefined_citation_keys"]
    assert warnings["undefined_citation_summary_count"] == 1
    assert bibtex["empty_literal_stack_entries"] == ["refmodel"]
    assert bibtex["error_count"] >= 1
    assert any("Bibtex errors" in line for line in bibtex["fatal_lines"])
    assert latex_errors["error_count"] == 3
    assert any("Missing $ inserted" in line for line in latex_errors["fatal_lines"])
    assert any("author?" in item for item in markers)
    assert any("??" in item for item in markers)
    assert commands[0]["command"] == "citet"
    assert commands[0]["keys"] == ["refmodel", "diffrec"]
    assert commands[1]["command"] == "citeauthor"
    assert commands[1]["keys"] == ["p5"]


def test_citation_render_diagnostics_blocks_nature_numeric_author_style(tmp_path, monkeypatch):
    audit = _load_audit_paper_normality()
    output_dir = tmp_path / "nature"
    output_dir.mkdir()
    tex_path = output_dir / "paper.tex"
    pdf_path = output_dir / "paper.pdf"
    source = r"""
\documentclass[pdflatex,sn-nature]{sn-jnl}
\begin{document}
\section{Introduction}
\citet{refmodel} introduce a recommendation model.
\end{document}
"""
    tex_path.write_text(source, encoding="utf-8")
    pdf_path.write_bytes(b"%PDF-1.4\n")
    (output_dir / "compile.log").write_text(
        "Package natbib Warning: Author undefined for citation`refmodel' on input line 8.\n"
        "You can't pop an empty literal stack for entry refmodel\n"
        "while executing---line 3431 of file sn-nature.bst\n"
        "(There were 1 error messages)\n"
        "Bibtex errors: See file 'paper.blg'\n"
        "! Missing $ inserted.\n"
        "! Undefined control sequence.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(audit, "read_pdf_text", lambda path, max_chars=240000: "ReferenceRec appears as (author?) [10] in the compiled PDF.")

    diagnostics = audit.citation_render_diagnostics(
        source,
        pdf_path=pdf_path,
        tex_path=tex_path,
        output_dir=output_dir,
        state={},
        venue_profile={"family": "springer-nature", "documentclass": {"options": ["pdflatex", "sn-nature"]}},
    )

    blocker_ids = {item["id"] for item in diagnostics["blockers"]}
    assert diagnostics["status"] == "block"
    assert "natbib_author_undefined" in blocker_ids
    assert "pdf_unresolved_citation_markers" in blocker_ids
    assert "nature_numeric_style_textual_citations" in blocker_ids
    assert "bibtex_compile_errors" in blocker_ids
    assert "latex_compile_errors" in blocker_ids
    assert diagnostics["latex_warnings"]["author_undefined_keys"] == ["refmodel"]
    assert diagnostics["bibtex_errors"]["empty_literal_stack_entries"] == ["refmodel"]
    assert diagnostics["latex_compile_errors"]["error_count"] == 2
    assert diagnostics["numeric_nature_style"] is True

    profile_with_required_options = audit.citation_render_diagnostics(
        source,
        pdf_path=pdf_path,
        tex_path=tex_path,
        output_dir=output_dir,
        state={},
        venue_profile={"family": "springer-nature", "documentclass": "sn-jnl", "required_options": ["pdflatex", "sn-nature"]},
    )
    assert profile_with_required_options["numeric_nature_style"] is True
    assert any(item["id"] == "nature_numeric_style_textual_citations" for item in profile_with_required_options["blockers"])


def test_vendor_bibtex_format_escapes_latex_special_title_chars():
    formatter = _load_vendor_bibtex_format()

    escaped = formatter.escape_bibtex("R^2ec: 100% LLM_rec & C# recommender")
    math_preserved = formatter.escape_bibtex("R$^2$ec: math-safe title")
    unbalanced_dollar = formatter.escape_bibtex("Cost $5 baseline")

    assert escaped == r"R\^{}2ec: 100\% LLM\_rec \& C\# recommender"
    assert math_preserved == r"R$^2$ec: math-safe title"
    assert unbalanced_dollar == r"Cost \$5 baseline"



def test_citation_render_diagnostics_ignores_stale_workspace_root_log(tmp_path, monkeypatch):
    audit = _load_audit_paper_normality()
    output_dir = tmp_path / "paper" / "output" / "iclr"
    workspace = tmp_path / "paper" / "writing" / "iclr" / "workspace"
    final_dir = workspace / "final"
    output_dir.mkdir(parents=True)
    final_dir.mkdir(parents=True)
    tex_path = output_dir / "paper.tex"
    pdf_path = output_dir / "paper.pdf"
    tex_path.write_text(r"""
\documentclass{article}
\begin{document}
Prior work is cited with normal numeric syntax~\citep{validkey}.
\bibliography{refs}
\end{document}
""", encoding="utf-8")
    pdf_path.write_bytes(b"%PDF-1.4\n")
    (workspace / "paper.log").write_text(
        "Package natbib Warning: Citation `oldmissing' on page 1 undefined on input line 9.\n"
        "There were undefined citations.\n",
        encoding="utf-8",
    )
    (final_dir / "paper.log").write_text("Output written on paper.pdf (9 pages).\n", encoding="utf-8")
    monkeypatch.setattr(audit, "read_pdf_text", lambda path, max_chars=240000: "All citations render normally [1].")

    diagnostics = audit.citation_render_diagnostics(
        tex_path.read_text(encoding="utf-8"),
        pdf_path=pdf_path,
        tex_path=tex_path,
        output_dir=output_dir,
        state={"paper_orchestra_workspace": str(workspace)},
        venue_profile={"family": "iclr"},
    )

    assert diagnostics["status"] == "pass"
    assert diagnostics["blockers"] == []
    assert str(final_dir / "paper.log") in diagnostics["log_paths"]
    assert str(workspace / "paper.log") not in diagnostics["log_paths"]



def test_missing_paper_self_review_receipt_blocks(tmp_path):
    review = _load_paper_self_review()

    result = review.validate_paper_self_review_receipt(tmp_path, "Nature")

    assert result["ready"] is False
    assert result["status"] == "block"
    assert {item["id"] for item in result["blockers"]} == {"missing_claude_self_review_receipt"}


def _write_self_review_fixture(root: Path) -> dict[str, Path]:
    paths = {
        "pdf": root / "paper" / "output" / "nature" / "paper.pdf",
        "pdf_text": root / "paper" / "output" / "nature" / "paper.txt",
        "tex": root / "paper" / "output" / "nature" / "paper.tex",
        "refs_bib": root / "paper" / "output" / "nature" / "refs.bib",
        "compile_log": root / "paper" / "output" / "nature" / "compile.log",
        "paper_log": root / "paper" / "output" / "nature" / "paper.log",
        "venue_requirements": root / "paper" / "venues" / "nature" / "venue_requirements.json",
    }
    for key, path in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if key == "pdf":
            path.write_bytes(b"%PDF-1.4\ncurrent preview\n")
        else:
            path.write_text(f"{key} reviewed\n", encoding="utf-8")
    payload = {
        "status": "pass",
        "reviewed_by": "project_claude_code",
        "venue": "Nature",
        "artifact_fingerprints": {
            key: {"path": str(path.relative_to(root)), "sha256": _sha256(path)}
            for key, path in paths.items()
        },
        "review_protocol": {
            "open_ended_review": True,
            "mode": "open-ended independent manuscript issue discovery from current artifacts",
            "scope": "Project Claude Code read the current compiled preview, TeX, bibliography, logs, and venue contract before repair.",
            "gate_crosscheck_after_independent_review": True,
            "discovery_order": ["independent_artifact_review", "manuscript_repairs", "gate_crosscheck"],
            "artifact_reading_log": [
                {
                    "artifact": key,
                    "path": str(path.relative_to(root)),
                    "method": "project Claude Code fixture read current artifact",
                    "sha256": _sha256(path),
                    "observations": f"{key} reviewed from the current preview bundle",
                }
                for key, path in paths.items()
            ],
        },
        "independent_findings": [
            {
                "id": "citation_render_issue",
                "category": "citation_render",
                "issue": "Current PDF/TeX/log review found unresolved citation-rendering risk that required explicit post-repair verification.",
                "review_source": "project Claude Code independent pdftotext/tex/bib/log/venue review",
                "discovery_phase": "independent_artifact_review",
                "source_artifacts": ["paper/output/nature/paper.txt", "paper/output/nature/paper.tex", "paper/output/nature/refs.bib", "paper/output/nature/paper.log"],
                "pdf_text_excerpt": "current preview",
                "tex_location": "paper/output/nature/paper.tex",
                "log_excerpt": "paper_log reviewed",
            }
        ],
        "repairs_applied": [
            {
                "file": "paper/output/nature/paper.tex",
                "action": "Recorded manuscript-level repair receipt for the current preview after checking PDF text, TeX, bibliography, logs, and venue requirements.",
                "verification": {"command": "build_conference_preview_paper.py --project demo --venue Nature", "result": "fixture verification passed"},
            }
        ],
        "remaining_blockers": [],
        "final_checks": {"compiled": True, "pdf_text_rechecked": True, "venue_shape_rechecked": True, "citation_render_rechecked": True, "bibliography_rechecked": True},
    }
    _write_json(root / "state" / "paper_preview_self_review.json", payload)
    return paths


def test_valid_paper_self_review_receipt_passes(tmp_path):
    review = _load_paper_self_review()
    paths = _write_self_review_fixture(tmp_path)

    result = review.validate_paper_self_review_receipt(
        tmp_path,
        "Nature",
        current_pdf=paths["pdf"],
        current_tex=paths["tex"],
        current_refs=paths["refs_bib"],
    )

    assert result["ready"] is True
    assert result["status"] == "pass"
    assert result["independent_findings_count"] == 1
    assert result["repairs_count"] == 1
    assert result["artifact_reading_log_count"] == 7
    assert result["open_review_protocol_ready"] is True
    assert result["blockers"] == []
    assert result["evidence_blockers"] == []
    assert result["submission_evidence_ready"] is True


def test_paper_self_review_distinguishes_preview_from_evidence_blockers(tmp_path):
    review = _load_paper_self_review()
    paths = _write_self_review_fixture(tmp_path)
    receipt = tmp_path / "state" / "paper_preview_self_review.json"
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["independent_findings"].append(
        {
            "category": "missing_empirical_validation",
            "issue": "The manuscript reports no empirical results for the proposed semantic-conditioned method; only backbone calibration is present.",
            "review_source": "project Claude Code independent PDF/TeX review",
            "discovery_phase": "independent_artifact_review",
            "source_artifacts": ["paper/output/nature/paper.tex", "paper/output/nature/paper.txt"],
            "tex_location": "Results section",
            "pdf_text_excerpt": "Reference calibration results only.",
            "repair": "No repair applied because fabricating results would violate experimental integrity.",
        }
    )
    _write_json(receipt, payload)

    result = review.validate_paper_self_review_receipt(
        tmp_path,
        "Nature",
        current_pdf=paths["pdf"],
        current_tex=paths["tex"],
        current_refs=paths["refs_bib"],
    )

    assert result["ready"] is True
    assert result["status"] == "pass"
    assert result["blockers"] == []
    assert result["preview_only_ready"] is True
    assert result["submission_evidence_ready"] is False
    assert result["evidence_blocker_count"] == 1
    assert result["evidence_blockers"][0]["category"] == "missing_empirical_validation"


def test_paper_self_review_resolved_evidence_finding_does_not_block_submission(tmp_path):
    review = _load_paper_self_review()
    paths = _write_self_review_fixture(tmp_path)
    receipt = tmp_path / "state" / "paper_preview_self_review.json"
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["independent_findings"].append(
        {
            "category": "evaluation_scope_mismatch",
            "issue": "A contribution sentence overstated evaluation scope.",
            "review_source": "project Claude Code independent TeX review",
            "discovery_phase": "independent_artifact_review",
            "source_artifacts": ["paper/output/nature/paper.tex"],
            "tex_location": "contribution paragraph",
            "resolved": True,
            "repair": "Resolved by rewording the contribution and verified by re-reading PDF text.",
            "verification": "PDF text rechecked and contribution no longer overstates scope.",
        }
    )
    _write_json(receipt, payload)

    result = review.validate_paper_self_review_receipt(
        tmp_path,
        "Nature",
        current_pdf=paths["pdf"],
        current_tex=paths["tex"],
        current_refs=paths["refs_bib"],
    )

    assert result["ready"] is True
    assert result["evidence_blockers"] == []
    assert result["submission_evidence_ready"] is True


def test_paper_self_review_receipt_must_match_current_pdf(tmp_path):
    review = _load_paper_self_review()
    paths = _write_self_review_fixture(tmp_path)
    current_pdf = tmp_path / "paper" / "output" / "nature" / "paper-current.pdf"
    current_pdf.write_bytes(b"%PDF-1.4\nnew current preview\n")

    result = review.validate_paper_self_review_receipt(
        tmp_path,
        "Nature",
        current_pdf=current_pdf,
        current_tex=paths["tex"],
        current_refs=paths["refs_bib"],
    )

    assert result["ready"] is False
    assert "self_review_pdf_not_current" in {item["id"] for item in result["blockers"]}



def test_paper_self_review_receipt_requires_independent_evidence_and_verification(tmp_path):
    review = _load_paper_self_review()
    paths = _write_self_review_fixture(tmp_path)
    receipt = tmp_path / "state" / "paper_preview_self_review.json"
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["independent_findings"] = [
        {"detail": "Claude checked the TASTE deterministic blockers."}
    ]
    payload["repairs_applied"] = [
        {"file": "paper/output/nature/paper.tex", "action": "Checked manuscript."}
    ]
    _write_json(receipt, payload)

    result = review.validate_paper_self_review_receipt(
        tmp_path,
        "Nature",
        current_pdf=paths["pdf"],
        current_tex=paths["tex"],
        current_refs=paths["refs_bib"],
    )

    blocker_ids = {item["id"] for item in result["blockers"]}
    assert result["ready"] is False
    assert "self_review_finding_missing_artifact_evidence" in blocker_ids
    assert "self_review_finding_missing_independence_provenance" in blocker_ids
    assert "self_review_finding_generic_review_note" in blocker_ids
    assert "self_review_repair_missing_verification" in blocker_ids


def test_paper_self_review_receipt_requires_open_artifact_reading_log(tmp_path):
    review = _load_paper_self_review()
    paths = _write_self_review_fixture(tmp_path)
    receipt = tmp_path / "state" / "paper_preview_self_review.json"
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload.pop("review_protocol", None)
    _write_json(receipt, payload)

    result = review.validate_paper_self_review_receipt(
        tmp_path,
        "Nature",
        current_pdf=paths["pdf"],
        current_tex=paths["tex"],
        current_refs=paths["refs_bib"],
    )

    blocker_ids = {item["id"] for item in result["blockers"]}
    assert result["ready"] is False
    assert "self_review_missing_open_review_protocol" in blocker_ids
    assert "self_review_missing_artifact_reading_log" in blocker_ids


def test_paper_self_review_receipt_requires_independent_first_order(tmp_path):
    review = _load_paper_self_review()
    paths = _write_self_review_fixture(tmp_path)
    receipt = tmp_path / "state" / "paper_preview_self_review.json"
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["review_protocol"].pop("gate_crosscheck_after_independent_review", None)
    payload["review_protocol"].pop("discovery_order", None)
    payload["independent_findings"][0].pop("discovery_phase", None)
    _write_json(receipt, payload)

    result = review.validate_paper_self_review_receipt(
        tmp_path,
        "Nature",
        current_pdf=paths["pdf"],
        current_tex=paths["tex"],
        current_refs=paths["refs_bib"],
    )

    blocker_ids = {item["id"] for item in result["blockers"]}
    assert result["ready"] is False
    assert "self_review_protocol_not_independent_first" in blocker_ids
    assert "self_review_finding_not_independent_first" in blocker_ids



def test_repair_prompt_preserves_literal_citep_example(monkeypatch):
    repair = _load_repair_paper_preview_loop()
    monkeypatch.setattr(repair, "venue_submission_policy", lambda venue, project="": {"template_family": "springer-nature"})

    prompt = repair.claude_repair_prompt(
        "demo_project",
        "Nature",
        "Demo Title",
        {"conference_preview": "", "normality": "", "figures": ""},
        {"paper_citation_render_status": "block"},
    )

    assert r"\citep{...}" in prompt
    assert r"\citepEllipsis" not in prompt
    assert "TASTE gate diagnostics are a later cross-check" in prompt
    assert "Do not merely copy TASTE-listed deterministic blockers" in prompt
    assert "discovery_order" in prompt
    assert "gate_crosscheck_after_independent_review=true" in prompt
    assert 'discovery_phase="independent_artifact_review"' in prompt
    assert "non-empty `independent_findings`" in prompt
    assert "review_protocol" in prompt
    assert "artifact_reading_log" in prompt
    assert "citation_render_rechecked=true" in prompt
    assert "The workflow will reject findings that only restate TASTE gate names" in prompt


def test_refresh_request_without_pdf_hash_change_does_not_block_ready_preview():
    repair = _load_repair_paper_preview_loop()

    decision = repair.preview_repair_status(
        True,
        True,
        force_refresh=True,
        pdf_exists=True,
        pdf_changed=False,
        passed_status="pass",
    )

    assert decision["status"] == "pass"
    assert decision["refresh_pdf_note"] == "refresh_requested_but_pdf_content_unchanged"


def test_repair_compile_preserves_newer_final_refs_and_fresh_pdf(tmp_path, monkeypatch):
    repair = _load_repair_paper_preview_loop()
    workspace = tmp_path / "workspace"
    final_dir = workspace / "final"
    final_dir.mkdir(parents=True)
    final_tex = final_dir / "paper.tex"
    workspace_refs = workspace / "refs.bib"
    final_refs = final_dir / "refs.bib"
    final_pdf = final_dir / "paper.pdf"
    final_tex.write_text(
        "\\documentclass{article}\n"
        "\\begin{document}x\\end{document}\n",
        encoding="utf-8",
    )
    workspace_refs.write_text("@article{old,title={Old}}\n", encoding="utf-8")
    final_refs.write_text("@article{new,title={New}}\n", encoding="utf-8")
    final_pdf.write_bytes(b"%PDF-1.4\ncurrent pdf\n")
    base = 1_700_000_000
    os.utime(workspace_refs, (base, base))
    os.utime(final_tex, (base + 10, base + 10))
    os.utime(final_refs, (base + 20, base + 20))
    os.utime(final_pdf, (base + 30, base + 30))
    monkeypatch.setattr(repair.shutil, "which", lambda _name: None)
    monkeypatch.setattr(repair, "workspace_tool_path", lambda _name: "")

    result = repair.compile_workspace_pdf(workspace)

    assert result["return_code"] == 0
    assert result["skipped_compile"] is True
    assert result["refs_sync"] == "kept_existing_final_refs_bib"
    assert final_refs.read_text(encoding="utf-8") == "@article{new,title={New}}\n"
    assert result["commands"] == []


def test_backend_off_prompt_update_does_not_overwrite_self_review_gate(tmp_path):
    repair = _load_repair_paper_preview_loop()

    update = repair.prompt_only_pipeline_update(
        tmp_path / "reports" / "paper_preview_repair_loop.md",
        tmp_path / "state" / "paper_preview_repair_loop.json",
        tmp_path / "paper" / "metadata" / "writing_revision_prompt_round_1.md",
    )

    assert update["paper_preview_repair_loop_status"] == "prompt_ready"
    assert update["paper_preview_repair_prompt_only"] is True
    assert "paper_self_review_status" not in update
    assert "paper_self_review_ready" not in update


def test_noncompact_project_summary_keeps_configured_venue_when_public_config_is_identity_only(tmp_path, monkeypatch):
    project_bridge = _load_project_bridge()
    project = "demo_project"
    root = tmp_path / "projects" / project
    _write_json(
        root / "project.json",
        {
            "name": project,
            "topic": "demo topic",
            "target_venue": "ICLR",
            "venue": "ICLR",
            "paper": {"target_venue": "ICLR", "venue_slug": "iclr", "template_family": "iclr"},
        },
    )
    _write_json(root / "state" / "experiment_registry.json", [])
    _write_json(root / "state" / "full_research_cycle.json", {"status": "blocked"})
    _write_json(root / "paper" / "metadata" / "paper_pipeline.json", {"venue": "ICLR", "target_venue": "ICLR", "venue_slug": "iclr"})

    monkeypatch.setattr(project_bridge, "PROJECTS", tmp_path / "projects")
    monkeypatch.setattr(project_bridge, "_PROJECT_SUMMARY_CACHE", {})
    monkeypatch.setattr(project_bridge, "_stage_status", lambda _root, _cfg: {"environment": {}, "experiment": {}, "paper": {}})
    monkeypatch.setattr(project_bridge, "_fast_project_summary", lambda _project, _root, _cfg: {
        "project": project,
        "topic": "demo topic",
        "config": {"name": project, "topic": "demo topic"},
        "run_preferences": {"target_venue": "ICLR", "venue": "ICLR"},
        "stages": {"environment": {}, "experiment": {}, "paper": {"venue": "ICLR", "target_venue": "ICLR"}},
        "full_research_cycle": {"status": "blocked"},
    })
    monkeypatch.setattr(project_bridge, "runtime_diagnostics", lambda _project: {})
    monkeypatch.setattr(project_bridge, "_claude_session_status", lambda _root: {})
    monkeypatch.setattr(project_bridge, "_agent_state", lambda _project: {})
    monkeypatch.setattr(project_bridge, "_taste_literature_summary", lambda _root: {})

    summary = project_bridge.project_summary(project, compact=False)

    assert summary["config"] == {"name": project, "topic": "demo topic"}
    assert summary["run_preferences"]["target_venue"] == "ICLR"
    assert summary["run_preferences"]["venue"] == "ICLR"
    assert summary["stages"]["paper"]["target_venue"] == "ICLR"



def test_manuscript_route_policy_allows_cited_prior_work_but_blocks_current_route_story(tmp_path, monkeypatch):
    ensure_script_paths()
    import build_conference_preview_paper as preview

    project = "demo_project"
    state = tmp_path / "projects" / project / "state"
    _write_json(
        state / "base_switch_gate.json",
        {
            "status": "blocked",
            "candidate_route": {"repo": "owner/CandidateRepo", "repo_path": str(tmp_path / "repos" / "candidate_repo")},
        },
    )
    monkeypatch.setattr(preview, "ROOT", tmp_path)

    prior_work_text = r"""
\section{Introduction}
CandidateRepo~\citep{candidate2026} proposes a prior method for the same problem.
\section{Related Work}
CandidateRepo~\citep{candidate2026} constructs a useful comparison point.
\section{Method}
Our method uses a separate current implementation and does not use that repository.
"""
    route_story_text = r"""
\section{Method}
We use CandidateRepo as the current repository and implementation backbone for the selected route.
"""

    assert preview.legacy_route_story_violations(project, prior_work_text, active_name="CurrentBase") == []
    assert preview.legacy_route_story_violations(project, route_story_text, active_name="CurrentBase") == [
        "legacy_route_story_in_manuscript:CandidateRepo"
    ]




def test_manuscript_integrity_blocks_completed_experiment_claims_without_supported_result(tmp_path, monkeypatch):
    ensure_script_paths()
    import build_conference_preview_paper as preview

    project = "demo_project"
    state = tmp_path / "projects" / project / "state"
    _write_json(state / "scientific_progress_gate.json", {"status": "blocked", "best_candidate": {}})
    _write_json(state / "reference_reproduction_gate.json", {"status": "pass", "decision": "continue_base"})
    monkeypatch.setattr(preview, "ROOT", tmp_path)

    manuscript = (
        "\\section{Introduction}\n"
        "PriorMethod~\\citep{prior2026} demonstrates strong results in prior work.\n"
        "\\section{Experiments}\n"
        "We report reference calibration for the selected baseline after reproducing the official protocol.\n"
        "We evaluate the proposed method on four benchmark datasets with loaders from RSIR~\\citep{rsir2026}.\n"
        "All experiments use fixed random seeds and are repeated three times with mean and standard deviation reported.\n"
        "Metrics are reported with mean and standard deviation across three runs.\n"
        "Results averaged over 3 random seeds are shown in Table~\\ref{tab:main}.\n"
        "The reference baseline is trained on a single NVIDIA A100 GPU for approximately 2 hours per run.\n"
        "Full training configuration, random seeds, and implementation details are documented.\n"
        "NDCG@10 & 0.0508 $\\pm$ 0.0021.\n"
        "NDCG@10 & 0.0508 $\\pm$ 0.0021 \\\\ Recall@10 & 0.0862 $\\pm$ 0.0035.\n"
    )

    violations = preview.unsupported_completed_experiment_claim_violations(project, manuscript)

    assert not any("reference calibration" in item for item in violations)
    assert any(item.startswith("unsupported_completed_evaluation_claim:") for item in violations)
    assert any(item.startswith("unsupported_experiment_protocol_claim:") for item in violations)
    assert any(item.startswith("unsupported_repeated_results_claim:") for item in violations)

    tex_path = tmp_path / "projects" / project / "paper" / "paper.tex"
    tex_path.parent.mkdir(parents=True, exist_ok=True)
    tex_path.write_text(manuscript, encoding="utf-8")
    policy_violations = preview.manuscript_policy_violations(project, tex_path, venue="ICLR")
    assert any(item.startswith("unsupported_completed_evaluation_claim:") for item in policy_violations)


def test_manuscript_integrity_blocks_hardware_claim_without_explicit_evidence(tmp_path, monkeypatch):
    ensure_script_paths()
    import build_conference_preview_paper as preview

    project = "demo_project"
    state = tmp_path / "projects" / project / "state"
    _write_json(state / "scientific_progress_gate.json", {"status": "blocked", "best_candidate": {}})
    monkeypatch.setattr(preview, "ROOT", tmp_path)

    manuscript = (
        "\\section{Experiments}\n"
        "The reference baseline is trained on a single NVIDIA A100 GPU for approximately 2 hours per run.\n"
    )

    violations = preview.unsupported_completed_experiment_claim_violations(project, manuscript)

    assert any(item.startswith("unsupported_hardware_runtime_claim:") for item in violations)


def test_manuscript_integrity_blocks_seed_configuration_claim_without_explicit_evidence(tmp_path, monkeypatch):
    ensure_script_paths()
    import build_conference_preview_paper as preview

    project = "demo_project"
    state = tmp_path / "projects" / project / "state"
    _write_json(state / "scientific_progress_gate.json", {"status": "blocked", "best_candidate": {}})
    monkeypatch.setattr(preview, "ROOT", tmp_path)

    manuscript = (
        "\\section{Experiments}\n"
        "Full training configuration, random seeds, and implementation details are documented.\n"
        "NDCG@10 & $0.0508 \\pm 0.0021$ \\\\ Recall@10 & $0.0862 \\pm 0.0035$.\n"
    )

    violations = preview.unsupported_completed_experiment_claim_violations(project, manuscript)

    assert any(item.startswith("unsupported_seed_configuration_claim:") for item in violations)
    assert any(item.startswith("unsupported_metric_uncertainty_claim:") for item in violations)


def test_manuscript_integrity_still_blocks_repeated_claims_without_explicit_evidence(tmp_path, monkeypatch):
    ensure_script_paths()
    import build_conference_preview_paper as preview

    project = "demo_project"
    state = tmp_path / "projects" / project / "state"
    _write_json(
        state / "scientific_progress_gate.json",
        {"status": "pass", "best_candidate": {"experiment_id": "run_1", "metric_name": "ndcg_at_10", "metric_value": 0.12}},
    )
    monkeypatch.setattr(preview, "ROOT", tmp_path)

    manuscript = (
        "\\section{Experiments}\n"
        "We evaluate the proposed method on four benchmark datasets and report audited metrics.\n"
        "All experiments use fixed random seeds and are repeated three times with mean and standard deviation reported.\n"
    )

    violations = preview.unsupported_completed_experiment_claim_violations(project, manuscript)
    assert not any(item.startswith("unsupported_completed_evaluation_claim:") for item in violations)
    assert any(item.startswith("unsupported_repeated_results_claim:") for item in violations)


def test_manuscript_integrity_allows_repeated_and_hardware_claims_with_explicit_evidence(tmp_path, monkeypatch):
    ensure_script_paths()
    import build_conference_preview_paper as preview

    project = "demo_project"
    state = tmp_path / "projects" / project / "state"
    _write_json(
        state / "scientific_progress_gate.json",
        {
            "status": "pass",
            "best_candidate": {
                "experiment_id": "run_1",
                "metric_name": "ndcg_at_10",
                "metric_value": 0.12,
                "seed_count": 3,
                "hardware": "NVIDIA A100",
            },
        },
    )
    monkeypatch.setattr(preview, "ROOT", tmp_path)

    manuscript = (
        "\\section{Experiments}\n"
        "We evaluate the proposed method on four benchmark datasets and report audited metrics.\n"
        "All experiments use fixed random seeds and are repeated three times with mean and standard deviation reported.\n"
        "The reference baseline is trained on a single NVIDIA A100 GPU.\n"
        "Full training configuration, random seeds, and implementation details are documented.\n"
    )

    assert preview.unsupported_completed_experiment_claim_violations(project, manuscript) == []


def test_writer_route_boundary_marks_candidate_routes_as_prior_work_only(tmp_path):
    ensure_script_paths()
    import run_paper_orchestra_bridge as bridge

    state = tmp_path / "state"
    _write_json(
        state / "base_switch_gate.json",
        {
            "status": "blocked",
            "candidate_route": {"repo": "owner/CandidateRepo", "repo_path": str(tmp_path / "repos" / "candidate_repo")},
        },
    )
    paths = type("Paths", (), {"state": state})()

    boundary = bridge.candidate_route_boundary_for_writer(paths, {"name": "owner/CurrentBase"})

    assert "CandidateRepo" in boundary["non_authoritative_or_legacy_route_terms"]
    assert "prior work" in boundary["policy"]
    assert "current selected route" in boundary["forbidden_use"].lower()


def test_preview_gate_blockers_summarize_current_preview_failures():
    ensure_script_paths()
    import build_conference_preview_paper as preview

    blockers = preview._preview_gate_blockers(
        [
            {
                "id": "reference_quality_target",
                "public_detail": "参考文献覆盖不足：当前 33/45，需要补充真实且相关的已验证引用。",
            }
        ],
        "blocked",
        False,
        ["wide 1.77:1 graphic is squeezed into a single-column figure"],
        [{"id": "missing_claude_self_review_receipt", "detail": "missing receipt"}],
    )

    assert [item["id"] for item in blockers] == [
        "reference_quality_target",
        "figure_quality",
        "missing_claude_self_review_receipt",
    ]
    summary = preview._preview_blocker_summary(blockers)
    assert "33/45" in summary
    assert "图表质量审计未通过" in summary
    assert "SIREN" not in summary


def test_conference_preview_rejection_helpers_preserve_candidate_blocker(tmp_path):
    ensure_script_paths()
    import build_conference_preview_paper as preview

    pdf = tmp_path / "paper.pdf"
    tex = tmp_path / "paper.tex"
    pdf.write_bytes(b"%PDF-1.4\n% rejected candidate\n")
    tex.write_text("\\section{Method} SIREN route story", encoding="utf-8")
    manifest = [
        {
            "label": "workspace_final",
            "pdf": str(pdf),
            "tex": str(tex),
            "pdf_exists": True,
            "tex_exists": True,
            "pages": 11,
            "violations": ["legacy_route_story_in_manuscript:SIREN"],
            "selected": False,
        }
    ]

    assert preview._first_existing_candidate_path(manifest, "pdf") == pdf
    assert preview._first_existing_candidate_path(manifest, "tex") == tex
    blockers = preview._candidate_rejection_blockers(manifest)

    assert blockers == [
        {
            "id": "manuscript_candidate_rejected",
            "status": "block",
            "detail": "workspace_final: legacy_route_story_in_manuscript:SIREN",
            "source": "paper_content_candidate_audit",
            "preview_blocker": True,
            "submission_blocker": True,
        }
    ]
