from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from policies.venue import venue_slug as _venue_slug
from runtime.framework_io import as_list as _as_list


def _read_json(path: Path) -> dict[str, Any]:
    try:
        if not path.exists():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _paper_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    paper = cfg.get("paper") if isinstance(cfg.get("paper"), dict) else {}
    return paper if isinstance(paper, dict) else {}


def _configured_venue(cfg: dict[str, Any], fallback: Any = "") -> str:
    paper = _paper_cfg(cfg)
    return str(
        fallback
        or cfg.get("target_venue")
        or cfg.get("venue")
        or paper.get("target_venue")
        or paper.get("venue")
        or paper.get("venue_slug")
        or ""
    ).strip()


def _configured_title(cfg: dict[str, Any]) -> str:
    paper = _paper_cfg(cfg)
    return str(paper.get("title") or "").strip()


def _status_from_audit(payload: dict[str, Any]) -> str:
    text = str(payload.get("status") or payload.get("final_verdict") or "").strip().lower()
    return "pass" if text == "pass" else "blocked" if text else ""


def _project_writing_projection(root: Path, state: dict[str, Any]) -> dict[str, Any]:
    writing_root = root / "paper" / "writing"
    if not writing_root.is_dir():
        return {}
    loop = _read_json(writing_root / "audit_repair_loop.json")
    audit = _read_json(writing_root / "workspace" / "audits" / "claude_quality_audit.json")
    page_audit = _read_json(writing_root / "workspace" / "audits" / "page_audit.json")
    venue_requirements = writing_root / "venue" / "venue_requirements.json"
    template_source = writing_root / "venue" / "template_source.json"
    tex_path = writing_root / "workspace" / "final" / "paper.tex"
    pdf_path = writing_root / "workspace" / "final" / "paper.pdf"
    refs_path = writing_root / "workspace" / "refs.bib"
    summary_status = str(state.get("writing_status") or "").strip()
    audit_status = str(loop.get("final_audit_status") or _status_from_audit(audit) or "").strip()
    blockers = _as_list(audit.get("blockers")) or _as_list(state.get("blockers"))
    pdf_ready = pdf_path.is_file()
    tex_ready = tex_path.is_file()
    audit_pass = audit_status == "pass"
    generated = summary_status == "generated"
    conference_preview_ready = bool(generated and audit_pass and tex_ready and pdf_ready)
    blocked_summary = "; ".join(str(item) for item in blockers[:3])
    if not blocked_summary and summary_status and summary_status != "generated":
        blocked_summary = f"Writing status={summary_status}; final audit={audit_status or 'missing'}"
    public_status = (
        "conference_preview_ready"
        if conference_preview_ready
        else "preview_available"
        if pdf_ready or tex_ready
        else "blocked"
        if summary_status == "blocked" or blockers
        else summary_status
        or "not_started"
    )
    repair_rounds = loop.get("repair_history") if isinstance(loop.get("repair_history"), list) else []
    return {
        "writing_module": "modules/writing",
        "writing_status": summary_status or public_status,
        "writing_workspace": str(writing_root),
        "writing_message_id": str(state.get("message_id") or ""),
        "status": public_status,
        "paper_stage_status": public_status,
        "summary": state.get("summary") or ("论文预览已由 Writing 生成并通过独立审计。" if conference_preview_ready else blocked_summary),
        "summary_zh": state.get("summary_zh") or ("论文预览已由 Writing 生成并通过独立审计。" if conference_preview_ready else blocked_summary),
        "conference_preview_ready": conference_preview_ready,
        "pdf_ready": pdf_ready,
        "pdf_path": str(pdf_path) if pdf_ready else "",
        "raw_pdf_path": str(pdf_path) if pdf_ready else "",
        "blocked_pdf_path": str(pdf_path) if pdf_ready else "",
        "tex_path": str(tex_path) if tex_ready else "",
        "blocked_tex_path": str(tex_path) if tex_ready else "",
        "latest_generated_pdf_path": str(pdf_path) if pdf_ready else "",
        "latest_generated_tex_path": str(tex_path) if tex_ready else "",
        "refs_bib_path": str(refs_path) if refs_path.is_file() else "",
        "paper_normality_status": "pass" if tex_ready and audit_pass else "blocked" if tex_ready else "missing",
        "paper_venue_format_status": "pass" if venue_requirements.is_file() and audit_pass else "blocked" if tex_ready else "missing",
        "paper_figure_quality_status": "pass" if audit_pass else "blocked" if tex_ready else "missing",
        "paper_citation_render_status": "pass" if refs_path.is_file() and audit_pass else "blocked" if tex_ready else "missing",
        "paper_citation_render_ready": bool(refs_path.is_file() and audit_pass),
        "paper_self_review_status": "pass" if audit_pass else "blocked" if tex_ready else "missing",
        "paper_self_review_ready": audit_pass,
        "paper_quality_audit_status": audit_status or ("missing" if tex_ready else ""),
        "paper_quality_audit_path": str(writing_root / "workspace" / "audits" / "claude_quality_audit.json") if audit else "",
        "paper_preview_repair_loop_status": "pass" if audit_pass else "blocked" if tex_ready else "",
        "paper_preview_repair_rounds": len(repair_rounds),
        "conference_preview_blocker_summary": blocked_summary,
        "conference_preview_blockers": blockers,
        "venue_requirements_status": "pass" if venue_requirements.is_file() else "missing",
        "venue_requirements_path": str(venue_requirements) if venue_requirements.is_file() else "",
        "template_fetched": template_source.is_file() or (writing_root / "venue" / "template_source").exists(),
        "template_source_path": str(template_source) if template_source.is_file() else "",
        "conference_preview_pages": page_audit.get("total_pages", ""),
        "conference_preview_body_pages": page_audit.get("body_pages", ""),
        "conference_preview_reference_pages": page_audit.get("reference_pages", ""),
    }


def active_paper_state(root: Path, project: str, cfg: dict[str, Any] | None = None, venue: str = "") -> dict[str, Any]:
    """Read public paper state without importing Writing private scripts."""
    cfg = cfg if isinstance(cfg, dict) else {}
    target_venue = _configured_venue(cfg, venue)
    state: dict[str, Any] = {}
    for path in [
        root / "paper" / "metadata" / "paper_pipeline.json",
        root / "state" / "paper_pipeline.json",
    ]:
        state = _read_json(path)
        if state:
            break
    if not state:
        state = {}
    state.setdefault("project", project)
    if target_venue:
        state.setdefault("venue", target_venue)
        state.setdefault("target_venue", target_venue)
        state.setdefault("venue_slug", _venue_slug(target_venue))
    title = _configured_title(cfg)
    if title:
        state.setdefault("title", title)
    writing_projection = _project_writing_projection(root, state)
    if writing_projection:
        state = {**state, **{key: value for key, value in writing_projection.items() if value not in ("", None, [])}}
    return state
