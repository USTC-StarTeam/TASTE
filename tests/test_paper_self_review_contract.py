from pathlib import Path

from path_helpers import load_script, script_path


ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    return load_script(name)


def test_paper_pipeline_always_runs_preview_repair_loop_after_preview_build():
    source = script_path("run_paper_pipeline.py").read_text(encoding="utf-8")
    preview_idx = source.index("preview_cmd = [sys.executable, str(SCRIPTS / 'build_conference_preview_paper.py')")
    repair_idx = source.index("preview_repair_cmd = [sys.executable, str(SCRIPTS / 'repair_paper_preview_loop.py')")
    readiness_idx = source.index("audit_submission_readiness.py", repair_idx)
    legacy_render_idx = source.index("render_paper_tex.py", repair_idx)

    assert preview_idx < repair_idx < readiness_idx < legacy_render_idx
    repair_block = source[repair_idx:legacy_render_idx]
    assert "run(preview_repair_cmd, required=False)" in repair_block
    assert "--refresh-current-paper" in repair_block
    assert "regenerate_current_preview" in repair_block


def test_repair_prompt_requires_open_ended_independent_claude_review():
    repair = _load_script("repair_paper_preview_loop")
    prompt = repair.claude_repair_prompt(
        "demo_project",
        "Nature",
        "Demo Title",
        {"conference_preview": "", "normality": "", "figures": ""},
        {},
    )

    assert "open-ended manuscript review" in prompt
    assert "before using TASTE deterministic gate details as a checklist" in prompt
    assert "Phase 1: independently read the compiled PDF text" in prompt
    assert "discovery_order" in prompt
    assert "independent_artifact_review" in prompt
    assert "gate_crosscheck" in prompt
    assert "paper_preview_self_review.json" in prompt
    assert "artifact_reading_log" in prompt
    assert "Do not merely copy TASTE-listed deterministic blockers" in prompt
    assert "Findings discovered only from TASTE gate names do not count" in prompt


def _write(path: Path, text: str = "content") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_self_review_receipt_rejects_gate_only_checklist_and_accepts_independent_artifact_review(tmp_path):
    paper_self_review = _load_script("paper_self_review")
    project_root = tmp_path / "project"
    state_dir = project_root / "state"
    paper_dir = project_root / "paper" / "output" / "nature"
    venue_dir = project_root / "paper" / "venues" / "nature"
    state_dir.mkdir(parents=True)
    paper_dir.mkdir(parents=True)
    venue_dir.mkdir(parents=True)

    pdf = paper_dir / "paper.pdf"
    pdf_text = paper_dir / "paper_text.txt"
    tex = paper_dir / "paper.tex"
    refs = paper_dir / "refs.bib"
    compile_log = paper_dir / "compile.log"
    paper_log = paper_dir / "paper.log"
    venue_req = venue_dir / "venue_requirements.json"
    for file_path, content in [
        (pdf, "%PDF demo"),
        (pdf_text, "No unresolved citations; references render cleanly."),
        (tex, "\\documentclass{sn-jnl}\n\\begin{document}Demo\\citep{key}\\bibliography{refs}\\end{document}"),
        (refs, "@article{key,title={Demo},author={A},journal={J},year={2026}}"),
        (compile_log, "Output written on paper.pdf; no undefined citations."),
        (paper_log, "No Author undefined warnings."),
        (venue_req, '{"venue":"Nature","template_family":"springer-nature"}'),
    ]:
        _write(file_path, content)

    def artifact(path: Path, extra: dict | None = None):
        row = {"path": str(path), "sha256": paper_self_review.sha256_file(path)}
        if extra:
            row.update(extra)
        return row

    base_receipt = {
        "status": "passed",
        "reviewed_by": "project_claude",
        "venue": "Nature",
        "artifact_fingerprints": {
            "pdf": artifact(pdf),
            "pdf_text": artifact(pdf_text, {"excerpt": "No unresolved citations"}),
            "tex": artifact(tex),
            "refs_bib": artifact(refs),
            "compile_log": artifact(compile_log),
            "paper_log": artifact(paper_log),
            "venue_requirements": artifact(venue_req),
        },
        "repairs_applied": [
            {
                "file": str(tex),
                "action": "Checked citation commands and template shape after repair.",
                "verification": "Recompiled and rechecked PDF text, refs.bib, compile.log, and paper.log.",
            }
        ],
        "remaining_blockers": [],
        "final_checks": {
            "compiled": True,
            "pdf_text_rechecked": True,
            "venue_shape_rechecked": True,
            "citation_render_rechecked": True,
            "bibliography_rechecked": True,
        },
    }

    gate_only = {
        **base_receipt,
        "review_protocol": {
            "open_ended_review": False,
            "scope": "TASTE gate checklist",
            "artifact_reading_log": [],
        },
        "independent_findings": [
            {
                "category": "paper_citation_render_status",
                "issue": "TASTE-listed deterministic blocker from conference_preview_report.",
                "discovery_phase": "gate_crosscheck",
                "source_artifacts": ["conference_preview_report.md"],
            }
        ],
    }
    receipt = state_dir / "paper_preview_self_review.json"
    receipt.write_text(__import__("json").dumps(gate_only), encoding="utf-8")
    rejected = paper_self_review.validate_paper_self_review_receipt(
        project_root,
        "Nature",
        current_pdf=pdf,
        current_tex=tex,
        current_refs=refs,
    )
    rejected_ids = {row["id"] for row in rejected["blockers"]}
    assert rejected["ready"] is False
    assert "self_review_protocol_not_open_ended" in rejected_ids or "self_review_protocol_not_independent_first" in rejected_ids
    assert "self_review_finding_not_independent_first" in rejected_ids

    independent = {
        **base_receipt,
        "review_protocol": {
            "open_ended_review": True,
            "scope": "Open-ended independent manuscript issue discovery from current PDF/TeX/BibTeX/logs/venue artifacts.",
            "independent_artifact_review_before_gate_crosscheck": True,
            "gate_crosscheck_after_independent_review": True,
            "discovery_order": "phase 1 independent_artifact_review before phase 3 gate_crosscheck",
            "artifact_reading_log": [
                {"artifact": "pdf", "path": str(pdf), "method": "sha256sum and pdfinfo", "evidence": paper_self_review.sha256_file(pdf)},
                {"artifact": "pdf_text", "path": str(pdf_text), "method": "pdftotext output read", "evidence": "No unresolved citations"},
                {"artifact": "tex", "path": str(tex), "method": "direct TeX read", "evidence": "\\citep{key}"},
                {"artifact": "refs_bib", "path": str(refs), "method": "direct BibTeX read", "evidence": "@article{key"},
                {"artifact": "compile_log", "path": str(compile_log), "method": "compile log read", "evidence": "Output written on paper.pdf"},
                {"artifact": "paper_log", "path": str(paper_log), "method": "paper.log grep", "evidence": "No Author undefined warnings"},
                {"artifact": "venue_requirements", "path": str(venue_req), "method": "venue contract read", "evidence": "springer-nature"},
            ],
        },
        "independent_findings": [
            {
                "category": "citation_render_style",
                "issue": "The manuscript previously needed numeric-compatible citation command review for Springer Nature rendering.",
                "discovery_phase": "independent_artifact_review",
                "discovery_method": "project Claude independently read TeX citation commands, refs.bib, paper.log, compile.log, and PDF text before TASTE gate cross-check.",
                "source_artifacts": [str(tex), str(refs), str(paper_log), str(compile_log), str(pdf_text)],
                "tex_location": "paper.tex citation command scan",
                "pdf_text_excerpt": "No unresolved citations",
                "log_excerpt": "No Author undefined warnings",
                "severity": "repaired_preview_issue",
                "status": "fixed",
                "repair_verification": "compiled=true; citation_render_rechecked=true; bibliography_rechecked=true",
            }
        ],
    }
    receipt.write_text(__import__("json").dumps(independent), encoding="utf-8")
    accepted = paper_self_review.validate_paper_self_review_receipt(
        project_root,
        "Nature",
        current_pdf=pdf,
        current_tex=tex,
        current_refs=refs,
    )
    assert accepted["ready"] is True
    assert accepted["open_review_protocol_ready"] is True
    assert accepted["artifact_reading_log_count"] == 7
    assert accepted["independent_findings_count"] == 1



def test_self_review_wrong_venue_does_not_import_old_evidence_findings(tmp_path):
    paper_self_review = _load_script("paper_self_review")
    project_root = tmp_path / "project"
    state_dir = project_root / "state"
    nature_dir = project_root / "paper" / "output" / "nature"
    iclr_dir = project_root / "paper" / "output" / "iclr"
    nature_venue_dir = project_root / "paper" / "venues" / "nature"
    for directory in [state_dir, nature_dir, iclr_dir, nature_venue_dir]:
        directory.mkdir(parents=True, exist_ok=True)

    nature_pdf = nature_dir / "paper.pdf"
    nature_tex = nature_dir / "paper.tex"
    nature_refs = nature_dir / "refs.bib"
    nature_log = nature_dir / "paper.log"
    nature_compile = nature_dir / "compile.log"
    nature_venue = nature_venue_dir / "venue_requirements.json"
    iclr_pdf = iclr_dir / "paper.pdf"
    iclr_tex = iclr_dir / "paper.tex"
    iclr_refs = iclr_dir / "refs.bib"
    for file_path, content in [
        (nature_pdf, "%PDF nature"),
        (nature_tex, "Nature old tex"),
        (nature_refs, "@article{old,title={Old}}"),
        (nature_log, "old nature log"),
        (nature_compile, "old compile log"),
        (nature_venue, "{\"venue\":\"Nature\"}"),
        (iclr_pdf, "%PDF iclr"),
        (iclr_tex, "ICLR current tex"),
        (iclr_refs, "@article{new,title={New}}"),
    ]:
        _write(file_path, content)

    def artifact(path: Path):
        return {"path": str(path), "sha256": paper_self_review.sha256_file(path)}

    receipt = {
        "status": "passed",
        "reviewed_by": "project_claude",
        "venue": "Nature",
        "artifact_fingerprints": {
            "pdf": artifact(nature_pdf),
            "pdf_text": {"path": str(nature_pdf), "sha256": paper_self_review.sha256_file(nature_pdf), "excerpt": "Nature text"},
            "tex": artifact(nature_tex),
            "refs_bib": artifact(nature_refs),
            "compile_log": artifact(nature_compile),
            "paper_log": artifact(nature_log),
            "venue_requirements": artifact(nature_venue),
        },
        "review_protocol": {
            "open_ended_review": True,
            "independent_artifact_review_before_gate_crosscheck": True,
            "artifact_reading_log": [
                {"artifact": "pdf", "path": str(nature_pdf), "method": "read", "evidence": "Nature"},
                {"artifact": "pdf_text", "path": str(nature_pdf), "method": "read", "evidence": "Nature"},
                {"artifact": "tex", "path": str(nature_tex), "method": "read", "evidence": "Nature"},
                {"artifact": "refs_bib", "path": str(nature_refs), "method": "read", "evidence": "Nature"},
                {"artifact": "compile_log", "path": str(nature_compile), "method": "read", "evidence": "Nature"},
                {"artifact": "paper_log", "path": str(nature_log), "method": "read", "evidence": "Nature"},
                {"artifact": "venue_requirements", "path": str(nature_venue), "method": "read", "evidence": "Nature"},
            ],
        },
        "repairs_applied": [{"file": str(nature_tex), "action": "checked", "verification": "compiled"}],
        "final_checks": {"compiled": True, "pdf_text_rechecked": True, "venue_shape_rechecked": True, "citation_render_rechecked": True, "bibliography_rechecked": True},
        "independent_findings": [
            {
                "category": "results_contains_untested_design_space",
                "issue": "In a Nature article this old issue must not be imported for ICLR.",
                "discovery_phase": "independent_artifact_review",
                "discovery_method": "read old Nature artifacts",
                "source_artifacts": [str(nature_tex)],
                "severity": "submission_blocker",
            }
        ],
    }
    (state_dir / "paper_preview_self_review.json").write_text(__import__("json").dumps(receipt), encoding="utf-8")

    result = paper_self_review.validate_paper_self_review_receipt(
        project_root,
        "ICLR",
        current_pdf=iclr_pdf,
        current_tex=iclr_tex,
        current_refs=iclr_refs,
    )
    blocker_ids = {row["id"] for row in result["blockers"]}

    assert "self_review_wrong_venue" in blocker_ids
    assert "self_review_pdf_not_current" in blocker_ids
    assert result["ready"] is False
    assert result["evidence_blockers"] == []
    assert result["evidence_blocker_count"] == 0
