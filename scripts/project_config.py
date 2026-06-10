#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
import subprocess
import sys
from pathlib import Path
from typing import Any

from project_paths import ROOT, build_paths, load_project_config, validate_project_name

WORKSPACE_ROOT = ROOT / "modules" / "taste"
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))
from auto_research.source_selection import normalize_source_selection, save_canonical_source_selection


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _enabled_sources_for(selection: dict[str, Any]) -> list[str]:
    enabled_sources = ["manual", "semantic_scholar"]
    if selection.get("include_arxiv"):
        enabled_sources.append("arxiv")
    if selection.get("include_biorxiv"):
        enabled_sources.append("biorxiv")
    if selection.get("include_nature"):
        enabled_sources.append("nature")
    if selection.get("include_science"):
        enabled_sources.append("science")
    if selection.get("include_github"):
        enabled_sources.append("github")
    if selection.get("include_huggingface"):
        enabled_sources.append("huggingface")
    return enabled_sources


def _sync_discovery_booleans(discovery: dict[str, Any], selection: dict[str, Any]) -> dict[str, Any]:
    """Keep legacy discovery sub-configs derived from canonical_source_selection.

    Older scripts still inspect discovery.<source>.enabled. Those fields are
    compatibility cache only; the source-selection checkboxes remain the
    single authority.
    """
    out = dict(discovery)
    for key, enabled in {
        "arxiv": bool(selection.get("include_arxiv")),
        "biorxiv": bool(selection.get("include_biorxiv")),
        "nature": bool(selection.get("include_nature")),
        "science": bool(selection.get("include_science")),
        "github": bool(selection.get("include_github")),
        "huggingface": bool(selection.get("include_huggingface")),
    }.items():
        child = dict(out.get(key) or {}) if isinstance(out.get(key), dict) else {}
        child["enabled"] = enabled
        out[key] = child
    out["enabled_sources"] = _enabled_sources_for(selection)
    return out


def _source_selection_from_patch(patch: dict[str, Any]) -> dict[str, Any] | None:
    raw = patch.get("canonical_source_selection") or patch.get("default_find_selection") or patch.get("source_selection")
    return normalize_source_selection(raw) if isinstance(raw, dict) else None


def _first_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        if key in payload:
            value = str(payload.get(key) or "").strip()
            if value:
                return value
    return ""


_ACRONYM_VENUE_PREFIXES = ("iclr", "icml", "kdd", "cikm", "acl", "emnlp", "aaai", "cvpr", "eccv", "neurips")


def _normalize_venue_label(venue: str) -> str:
    text = str(venue or "").strip()
    lowered = text.lower()
    return text.upper() if lowered.startswith(_ACRONYM_VENUE_PREFIXES) else text


def _venue_slug(venue: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(venue or "").strip().lower()).strip("-")


def _paper_template_fields(venue: str) -> dict[str, str]:
    lowered = str(venue or "").strip().lower()
    if lowered.startswith("iclr"):
        return {"template_family": "iclr", "template_source_url": "https://github.com/ICLR/Master-Template"}
    if "nature" in lowered:
        return {"template_family": "springer-nature", "template_source_url": "https://www.springernature.com/gp/authors/campaigns/latex-author-support"}
    return {}


def _sync_paper_target_venue(project: str, venue: str) -> None:
    normalized = _normalize_venue_label(venue)
    if not normalized:
        return
    from paper_common import ensure_paper_dirs, load_json, slugify, update_pipeline_state, write_json

    slug = slugify(normalized)
    template_fields = _paper_template_fields(normalized)
    paper_paths = ensure_paper_dirs(project)
    pipeline_update = {"target_venue": normalized, "venue": normalized, "venue_slug": slug, **template_fields}
    update_pipeline_state(project, pipeline_update, venue=normalized, promote_to_top=True)
    metadata_path = paper_paths["paper_metadata"]
    metadata = load_json(metadata_path, {})
    if not isinstance(metadata, dict):
        metadata = {}
    metadata.update(pipeline_update)
    write_json(metadata_path, metadata)
    _sync_control_state_target_venue(project, normalized, slug)


def _job_is_live(job: Any) -> bool:
    if not isinstance(job, dict):
        return False
    status = str(job.get("status") or "").strip().lower()
    return status in {"queued", "running", "cancelling"} and (job.get("process_alive") is True or job.get("alive") is True)


def _sync_stale_job_target_venue(job: Any, venue: str, slug: str) -> bool:
    if not isinstance(job, dict) or _job_is_live(job):
        return False
    changed = False
    launch_venue = str(job.get("venue") or job.get("target_venue") or "").strip()
    if launch_venue and launch_venue != venue and not job.get("launch_venue"):
        job["launch_venue"] = launch_venue
        changed = True
    for key, value in {"target_venue": venue, "venue_slug": slug}.items():
        if job.get(key) != value:
            job[key] = value
            changed = True
    if "venue" in job:
        job.pop("venue", None)
        changed = True
    # Stale job commands are audit details for logs, not current project state.
    # Keeping them in current JSON lets old --venue flags override the saved venue
    # in downstream UI/debug views.
    for key in ["cmd", "command"]:
        if key in job:
            job.pop(key, None)
            changed = True
    return changed


def _contains_other_venue_paper_artifact(value: Any, slug: str) -> bool:
    text = json.dumps(value, ensure_ascii=False).lower() if isinstance(value, (dict, list)) else str(value or "").lower()
    if not text or not slug:
        return False
    for match in re.finditer(r"paper/(?:output|writing|venues|orchestra)/([a-z0-9-]+)", text):
        found = match.group(1).strip("-")
        if found and found != slug:
            return True
    for match in re.finditer(r"--venue(?:=|\s+)([a-z0-9_.-]+)", text):
        found = _venue_slug(match.group(1))
        if found and found != slug:
            return True
    if slug not in {"nature", "springer-nature"} and any(marker in text for marker in ["springernature.com", "sn-jnl", "sn-nature", "nature article", "nature expects"]):
        return True
    if slug != "iclr" and any(marker in text for marker in ["github.com/iclr/master-template", "iclr2026_conference"]):
        return True
    return False


def _venue_refresh_blocker(venue: str) -> dict[str, Any]:
    return {
        "name": "venue_readiness_refresh_required",
        "id": "venue_readiness_refresh_required",
        "status": "blocked",
        "severity": "block",
        "detail": f"Target venue is now {venue}; paper evidence and submission-readiness audits must be rebuilt for this venue before the paper module can use those checks.",
        "evidence": ["project.json", "paper/metadata/paper_pipeline.json"],
    }


def _reset_venue_sensitive_audit_payload(payload: dict[str, Any], rel_name: str, venue: str, slug: str, before: str) -> bool:
    before_slug = _venue_slug(before)
    stale = bool(before_slug and before_slug != slug)
    stale = stale or _contains_other_venue_paper_artifact(
        {
            "checks": payload.get("checks"),
            "failed_checks": payload.get("failed_checks"),
            "blockers": payload.get("blockers"),
            "metrics": payload.get("metrics"),
            "issues": payload.get("issues"),
            "warnings": payload.get("warnings"),
            "paper_self_review_blockers": payload.get("paper_self_review_blockers"),
            "paper_self_review_evidence_blockers": payload.get("paper_self_review_evidence_blockers"),
        },
        slug,
    )
    if not stale:
        return False

    blocker = _venue_refresh_blocker(venue)
    payload["status"] = "blocked"
    payload["submission_ready"] = False
    payload["venue_change_requires_readiness_refresh"] = True
    payload["venue_refresh_required"] = True
    payload["checks"] = [blocker]
    payload["failed_checks"] = [blocker]
    payload["blockers"] = [blocker]
    payload["issues"] = [blocker["detail"]]
    payload["warnings"] = []
    payload["paper_self_review_blockers"] = []
    payload["paper_self_review_evidence_blockers"] = []
    payload["paper_self_review_evidence_blocker_count"] = 0
    payload["paper_self_review_submission_evidence_ready"] = False
    payload["paper_self_review_preview_only_ready"] = False
    payload["metrics"] = {
        "venue_policy_status": "requires_refresh",
        "venue_failed_hard_checks": 1,
        "venue_refresh_required": True,
    }
    if rel_name == "submission_readiness.json":
        payload["promotion_gate"] = "blocked_venue_readiness_refresh_required"
        payload["principle"] = "Submission readiness must be regenerated after target-venue changes; stale checks from another venue cannot support the current paper module."
    else:
        payload["promotion_gate"] = "hold-markdown-only"
        payload["principle"] = "Paper evidence audits must be regenerated after target-venue changes; stale checks from another venue cannot support paper claims."
    return True


def _reset_stale_venue_paper_process_payload(
    payload: dict[str, Any],
    rel_name: str,
    project: str,
    venue: str,
    slug: str,
    before: str,
    now: str,
) -> bool:
    before_slug = _venue_slug(before)
    stale = bool(before_slug and before_slug != slug)
    stale = stale or _contains_other_venue_paper_artifact(payload, slug)
    if not stale:
        return False

    blocker = _venue_refresh_blocker(venue)
    title = str(payload.get("title") or "").strip()
    payload.clear()
    payload.update(
        {
            "project": project,
            "venue": venue,
            "target_venue": venue,
            "venue_slug": slug,
            **_paper_template_fields(venue),
            "status": "blocked",
            "venue_refresh_required": True,
            "venue_change_requires_current_paper_refresh": True,
            "updated_at": now,
            "summary": f"Target venue is now {venue}; {rel_name} must be regenerated for the current venue before the paper module can use it.",
            "checks": [blocker],
            "failed_checks": [blocker],
            "blockers": [blocker],
            "issues": [blocker["detail"]],
            "warnings": [],
        }
    )
    if title:
        payload["title"] = title
    if rel_name == "paper_preview_self_review.json":
        payload["paper_self_review_ready"] = False
        payload["paper_self_review_preview_only_ready"] = False
        payload["paper_self_review_submission_evidence_ready"] = False
        payload["remaining_blockers"] = [blocker]
        payload["final_checks"] = []
    elif rel_name == "paper_preview_repair_loop.json":
        payload["paper_preview_repair_loop_status"] = "blocked"
        payload["rounds"] = []
        payload["final"] = {}
    elif rel_name == "paper_figure_quality_audit.json":
        payload["figure_quality_ready"] = False
        payload["blocked_count"] = 1
        payload["failed_items"] = [blocker]
    elif rel_name == "paper_orchestra_bridge.json":
        payload["paper_orchestra_bridge_status"] = "blocked"
        payload["phases"] = []
    elif rel_name == "paper_orchestra_audit.json":
        payload["paper_orchestra_status"] = "blocked"
    return True



_STALE_CURRENT_PAPER_FIELD_KEYS = {
    "blocked_pdf_path",
    "blocked_preview_pdf",
    "blocked_preview_tex",
    "blocked_tex_path",
    "compile_log",
    "conference_preview_blocker_summary",
    "conference_preview_blockers",
    "conference_preview_pdf",
    "conference_preview_tex",
    "latest_generated_pdf_info",
    "latest_generated_pdf_path",
    "latest_generated_tex_path",
    "latest_pdf",
    "latest_pdf_info",
    "latest_preview_pdf",
    "latest_preview_tex",
    "paper_citation_render_blockers",
    "paper_layout_footprint_warnings",
    "paper_log",
    "paper_self_review_blockers",
    "paper_self_review_evidence_blockers",
    "paper_stage",
    "paper_status",
    "pdf_after",
    "pdf_before",
    "pdf_path",
    "raw_pdf_path",
    "raw_tex_path",
    "rendered_tex",
    "submission_readiness",
    "tex_path",
    "venue_requirements",
    "venue_requirements_path",
    "venue_requirements_summary",
    "venue_submission_policy",
}

_STALE_CURRENT_PAPER_CONTAINER_KEYS = {
    "actions",
    "blocker_action_plan",
    "failed_checks",
    "latest_gate",
    "metrics",
    "paper_evidence_audit",
    "paper_preview",
    "paper_state",
    "recommended_commands",
    "submission_readiness",
    "top_actions",
}


def _drop_stale_venue_current_paper_fields(payload: Any, slug: str) -> bool:
    if not isinstance(payload, dict):
        return False
    changed = False
    for key in list(payload.keys()):
        value = payload.get(key)
        key_l = str(key).lower()
        if key_l in _STALE_CURRENT_PAPER_FIELD_KEYS and _contains_other_venue_paper_artifact(value, slug):
            payload.pop(key, None)
            changed = True
            continue
        if isinstance(value, dict) and key_l in _STALE_CURRENT_PAPER_CONTAINER_KEYS:
            if _drop_stale_venue_current_paper_fields(value, slug):
                changed = True
        elif isinstance(value, list) and key_l in _STALE_CURRENT_PAPER_CONTAINER_KEYS:
            kept = [item for item in value if not _contains_other_venue_paper_artifact(item, slug)]
            if len(kept) != len(value):
                payload[key] = kept
                changed = True
    return changed


def _invalidate_stale_venue_current_paper_snapshots(payload: dict[str, Any], venue: str, slug: str) -> bool:
    latest_gate = payload.get("latest_gate") if isinstance(payload.get("latest_gate"), dict) else None
    gate_changed = _drop_stale_venue_current_paper_fields(latest_gate, slug) if latest_gate is not None else False
    changed = _drop_stale_venue_current_paper_fields(payload, slug) or gate_changed
    if gate_changed and latest_gate is not None:
        latest_gate["accepted_preview"] = False
        latest_gate["submission_ready"] = False
        latest_gate["complete"] = False
        latest_gate["venue_refresh_required"] = True
        latest_gate["venue_change_invalidated_paper_snapshot"] = True
        latest_gate["paper_snapshot_invalidated_for_venue"] = venue
        changed = True
    if changed:
        payload["venue_refresh_required"] = True
        payload["venue_change_invalidated_paper_snapshot"] = True
    return changed


def _sync_control_state_target_venue(project: str, venue: str, slug: str) -> None:
    paths = build_paths(project)
    now = datetime.now(timezone.utc).isoformat()
    template_fields = _paper_template_fields(venue)
    for rel in [
        Path("state/full_research_cycle.json"),
        Path("state/blocker_action_plan.json"),
        Path("state/submission_readiness.json"),
        Path("state/paper_evidence_audit.json"),
        Path("state/paper_orchestra_state.json"),
        Path("state/paper_orchestra_bridge.json"),
        Path("state/paper_orchestra_audit.json"),
        Path("state/paper_normality_audit.json"),
        Path("state/paper_preview_repair_loop.json"),
        Path("state/paper_preview_self_review.json"),
        Path("state/paper_figure_quality_audit.json"),
        Path("state/paper_figure_repair_loop.json"),
        Path("state/paper_citation_quality_repair.json"),
        Path("state/paper_citation_coverage_revision.json"),
        Path("state/supervision_tick.json"),
        Path("state/full_cycle_job.json"),
    ]:
        path = paths.root / rel
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        before = str(payload.get("target_venue") or payload.get("venue") or "").strip()
        if rel.name == "full_cycle_job.json" and before and before != venue and not payload.get("launch_venue"):
            payload["launch_venue"] = before
        template_synced = all(payload.get(key) == value for key, value in template_fields.items())
        if not template_fields:
            template_synced = "template_family" not in payload and "template_source_url" not in payload
        payload["target_venue"] = venue
        payload["venue"] = venue
        payload["venue_slug"] = slug
        if template_fields:
            payload.update(template_fields)
        else:
            payload.pop("template_family", None)
            payload.pop("template_source_url", None)
        payload["venue_config_synced_at"] = now
        if rel.name in {"submission_readiness.json", "paper_evidence_audit.json"}:
            _reset_venue_sensitive_audit_payload(payload, rel.name, venue, slug, before)
        elif rel.name.startswith("paper_"):
            _reset_stale_venue_paper_process_payload(payload, rel.name, project, venue, slug, before, now)
        elif rel.name == "submission_readiness.json" and before and _venue_slug(before) != slug and payload.get("submission_ready"):
            payload["submission_ready"] = False
            payload["status"] = "blocked"
            payload["venue_change_requires_readiness_refresh"] = True
        for key in ["full_cycle_job", "paper_job", "job"]:
            _sync_stale_job_target_venue(payload.get(key), venue, slug)
        if rel.name == "full_cycle_job.json":
            _sync_stale_job_target_venue(payload, venue, slug)
        _invalidate_stale_venue_current_paper_snapshots(payload, venue, slug)
        _write_json(path, payload)


def _apply_project_patch(cfg: dict[str, Any], patch: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    venue_present = "target_venue" in patch or "venue" in patch
    venue = str(patch.get("target_venue") or patch.get("venue") or "").strip()
    if venue_present:
        paper = dict(cfg.get("paper") or {}) if isinstance(cfg.get("paper"), dict) else {}
        if venue:
            normalized_venue = _normalize_venue_label(venue)
            cfg["target_venue"] = normalized_venue
            cfg["venue"] = normalized_venue
            paper["target_venue"] = normalized_venue
            paper["venue_slug"] = _venue_slug(normalized_venue)
            template_fields = _paper_template_fields(normalized_venue)
            if template_fields:
                paper.update(template_fields)
            elif paper.get("template_family") in {"iclr", "springer-nature"}:
                paper.pop("template_family", None)
                paper.pop("template_source_url", None)
        else:
            cfg.pop("target_venue", None)
            cfg.pop("venue", None)
            for key in ["target_venue", "venue_slug", "template_family", "template_source_url"]:
                paper.pop(key, None)
        cfg["paper"] = paper
    if "topic" in patch:
        topic = str(patch.get("topic") or "").strip()
        if topic:
            cfg["topic"] = topic
    for key in ["user_prompt", "title", "research_interest", "researcher_profile"]:
        if key in patch:
            cfg[key] = str(patch.get(key) or "").strip()
    if "queries" in patch and isinstance(patch.get("queries"), list):
        cfg["queries"] = [str(item).strip() for item in patch.get("queries", []) if str(item).strip()]
    if "coding_backend" in patch:
        backend = str(patch.get("coding_backend") or "").strip()
        if backend:
            coding = dict(cfg.get("coding_agent") or {}) if isinstance(cfg.get("coding_agent"), dict) else {}
            coding["backend"] = backend
            cfg["coding_agent"] = coding
    selection = _source_selection_from_patch(patch)
    if selection is not None:
        discovery = dict(cfg.get("discovery") or {}) if isinstance(cfg.get("discovery"), dict) else {}
        discovery["canonical_source_selection"] = selection
        discovery = _sync_discovery_booleans(discovery, selection)
        cfg["discovery"] = discovery
        cfg["default_find_selection"] = selection
    return cfg, selection


def create_project_settings(payload: dict[str, Any]) -> dict[str, Any]:
    name = validate_project_name(payload.get("id") or payload.get("name") or "")
    paths = build_paths(name)
    if paths.config.exists():
        raise ValueError(f"project already exists: {name}")
    template_path = ROOT / "templates" / "project.json"
    cfg = json.loads(template_path.read_text(encoding="utf-8")) if template_path.exists() else {}
    topic = _first_text(payload, "topic") or name
    cfg["name"] = name
    cfg["topic"] = topic
    cfg["user_prompt"] = ""
    cfg["conda_env"] = ""
    cfg["research_interest"] = ""
    cfg["researcher_profile"] = ""
    cfg["queries"] = [topic]
    cfg.pop("target_venue", None)
    cfg.pop("venue", None)
    paper = dict(cfg.get("paper") or {}) if isinstance(cfg.get("paper"), dict) else {}
    for key in ["target_venue", "venue_slug", "template_family", "template_source_url"]:
        paper.pop(key, None)
    if paper:
        cfg["paper"] = paper
    startup = dict(cfg.get("startup") or {}) if isinstance(cfg.get("startup"), dict) else {}
    startup["last_bootstrap_request"] = ""
    cfg["startup"] = startup
    selection = None
    _write_json(paths.config, cfg)
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "init_project.py"), "--project", name], cwd=ROOT, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or "failed to initialize project structure")
    activate = paths.root / "activate_env.sh"
    if not activate.exists():
        activate.write_text(
            "\n".join([
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                "SCRIPT_DIR=\"$(cd \"$(dirname \"${BASH_SOURCE[0]}\")\" && pwd)\"",
                "ROOT=\"$(cd \"$SCRIPT_DIR/../..\" && pwd)\"",
                f"exec \"$ROOT/scripts/run_in_conda.sh\" \"{name}\" \"$@\"",
                "",
            ]),
            encoding="utf-8",
        )
        activate.chmod(0o755)
    if selection is not None:
        save_canonical_source_selection(selection, project_config_path=paths.config)
    return cfg

def project_source_selection(project: str) -> dict[str, Any]:
    cfg = load_project_config(project)
    discovery = cfg.get("discovery", {}) if isinstance(cfg.get("discovery"), dict) else {}
    raw = discovery.get("canonical_source_selection") or cfg.get("default_find_selection")
    return normalize_source_selection(raw)


def project_target_venue(project: str, default: str = "") -> str:
    try:
        cfg = load_project_config(project)
    except Exception:
        return default
    paper = cfg.get("paper", {}) if isinstance(cfg.get("paper"), dict) else {}
    value = cfg.get("target_venue") or cfg.get("venue") or paper.get("target_venue") or default
    return str(value or default).strip() or default


def update_project_settings(project: str, patch: dict[str, Any]) -> dict[str, Any]:
    paths = build_paths(project)
    cfg = load_project_config(project)
    venue_present = "target_venue" in patch or "venue" in patch
    cfg, selection = _apply_project_patch(cfg, patch)
    _write_json(paths.config, cfg)
    if venue_present:
        paper = cfg.get("paper") if isinstance(cfg.get("paper"), dict) else {}
        venue = str(cfg.get("target_venue") or cfg.get("venue") or paper.get("target_venue") or "").strip()
        if venue:
            _sync_paper_target_venue(project, venue)
    if selection is not None:
        save_canonical_source_selection(selection, project_config_path=paths.config)
    return cfg
