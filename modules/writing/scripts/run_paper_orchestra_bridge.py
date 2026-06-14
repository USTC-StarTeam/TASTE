#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from difflib import SequenceMatcher

from paper_common import (
    ensure_paper_dirs,
    find_main_tex,
    get_active_paper_state,
    load_json,
    load_venue_requirements,
    normalize_venue_front_matter,
    read_text,
    slugify,
    update_pipeline_state,
    validate_venue_template_format,
    venue_fallback_template,
    venue_slug_aliases,
    venue_template_profile,
    venue_reference_target,
    venue_submission_policy,
    workspace_tool_path,
    write_json,
    write_text,
)
from project_paths import ROOT, build_paths, load_project_config
from pipeline_guard import guard_fresh_base_blocker_entry
from experiment_contracts import SUPPORTIVE_CLAIM_VERDICTS, row_promotion_blockers


PAPER_ORCHESTRA_URL = "https://github.com/Ar9av/PaperOrchestra.git"
WRITING_MODULE_ROOT = ROOT / "modules" / "writing"
WRITING_CONTRACT = WRITING_MODULE_ROOT / "SKILL.md"
WRITING_VENDOR_ROOT = WRITING_MODULE_ROOT / "vendor"
DEFAULT_PAPER_ORCHESTRA_DIR = WRITING_VENDOR_ROOT / "PaperOrchestra"
WRITING_ACADEMIC_SKILLS_DIR = WRITING_VENDOR_ROOT / "academic-research-skills"
WRITING_NATURE_REFERENCE_DIR = WRITING_VENDOR_ROOT / "nature_family_writing_reference"
WRITING_VENDOR_PROVENANCE = WRITING_VENDOR_ROOT / "WRITING_VENDOR_PROVENANCE.json"
CITE_RE = re.compile(r"\\\\cite\\w*\\{([^{}]+)\\}")


PAPER_ORCHESTRA_SKILLS = [
    "paper-orchestra",
    "outline-agent",
    "plotting-agent",
    "literature-review-agent",
    "section-writing-agent",
    "content-refinement-agent",
    "paper-writing-bench",
    "paper-autoraters",
    "agent-research-aggregator",
]

PHASE_ORDER = [
    "prepare",
    "outline",
    "plotting",
    "literature",
    "section-writing",
    "refinement",
    "compile",
    "audit",
    "sync-preview",
]

PHASE_REQUIRED_FILES = {
    "outline": ["outline"],
    "plotting": ["captions"],
    "literature": ["refs_bib", "citation_pool", "intro_relwork"],
    "section-writing": ["draft_tex"],
    "refinement": ["final_tex"],
    "compile": ["final_pdf"],
}

SECTION_TITLES = ["Introduction", "Related Work", "Method", "Experiments", "Conclusion"]


def section_titles_for_venue(venue: str, project: str = "") -> list[str]:
    policy = venue_submission_policy(venue, project=project) if venue else {}
    family = str(policy.get("template_family") or "").lower() if isinstance(policy, dict) else ""
    sections = [str(item).strip() for item in policy.get("canonical_sections", []) if str(item).strip()] if isinstance(policy, dict) else []
    if family == "springer-nature" or (venue and "nature" in slugify(venue)) or bool(policy.get("nature_family_article_mode") if isinstance(policy, dict) else False):
        return sections or ["Introduction", "Results", "Discussion", "Methods"]
    return sections or SECTION_TITLES


def mandatory_sections_markdown(venue: str, project: str = "") -> str:
    policy = venue_submission_policy(venue, project=project) if venue else {}
    family = str(policy.get("template_family") or "").lower() if isinstance(policy, dict) else ""
    is_nature = family == "springer-nature" or (venue and "nature" in slugify(venue)) or bool(policy.get("nature_family_article_mode") if isinstance(policy, dict) else False)
    body_sections = section_titles_for_venue(venue, project=project)
    rows = ["Abstract", *body_sections]
    back_matter = [str(item).strip() for item in policy.get("required_back_matter", []) if str(item).strip()] if isinstance(policy, dict) else []
    if is_nature:
        if policy.get("data_availability_expected") and "Data availability" not in back_matter:
            back_matter.append("Data availability")
        if policy.get("code_availability_expected") and "Code availability" not in back_matter:
            back_matter.append("Code availability")
        if not back_matter:
            back_matter = ["Data availability", "Code availability"]
    rows.extend(back_matter)
    rows.append("References")
    return "\n".join(f"{idx}. {item}" for idx, item in enumerate(rows, start=1))


def manuscript_shape_requirement(venue: str, project: str = "") -> str:
    policy = venue_submission_policy(venue, project=project) if venue else {}
    family = str(policy.get("template_family") or "").lower() if isinstance(policy, dict) else ""
    is_nature = family == "springer-nature" or (venue and "nature" in slugify(venue)) or bool(policy.get("nature_family_article_mode") if isinstance(policy, dict) else False)
    if is_nature:
        return (
            "Use Nature-family article shape from the resolved venue contract: Abstract, "
            "Introduction, Results, Discussion, Methods, Data availability, Code availability, and References. "
            "Fold related-work synthesis into Introduction/Results where appropriate; do not use top-level Related Work, Experiments, or Conclusion unless the resolved journal contract explicitly asks for them. Do not include a Keywords block unless the contract explicitly requires one."
        )
    return "Use the normal conference manuscript shape: Abstract, Introduction, Related Work, Method, Experiments, Conclusion, and References."


BASELINE_QUERIES: list[str] = []


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def decode_output(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def run(cmd: list[str], cwd: Path = ROOT, timeout: int | None = None, required: bool = False) -> dict[str, Any]:
    started = now_iso()
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        result = {
            "cmd": cmd,
            "cwd": str(cwd),
            "started_at": started,
            "finished_at": now_iso(),
            "return_code": 124,
            "stdout_tail": decode_output(exc.stdout)[-12000:],
            "stderr_tail": (decode_output(exc.stderr) + f"\nTimed out after {timeout}s")[-12000:],
            "timed_out": True,
        }
        if required:
            raise RuntimeError(f"command timed out after {timeout}s: {' '.join(cmd)}")
        return result
    stdout = decode_output(proc.stdout)
    stderr = decode_output(proc.stderr)
    result = {
        "cmd": cmd,
        "cwd": str(cwd),
        "started_at": started,
        "finished_at": now_iso(),
        "return_code": proc.returncode,
        "stdout_tail": stdout[-12000:],
        "stderr_tail": stderr[-12000:],
    }
    if required and proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{stderr[-2000:]}")
    return result


def sha256_file(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {"path": str(path), "exists": False, "sha256": "", "bytes": 0}
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"path": str(path), "exists": True, "sha256": digest.hexdigest(), "bytes": path.stat().st_size}


def active_pdf_fingerprint(project: str, venue: str) -> dict[str, Any]:
    state = get_active_paper_state(project, venue=venue)
    candidates = [
        state.get("conference_preview_pdf"),
        state.get("pdf_path"),
        state.get("blocked_preview_pdf"),
        state.get("latest_preview_pdf"),
        state.get("paper_orchestra_final_pdf"),
    ]
    for candidate in candidates:
        path = Path(str(candidate or ""))
        if path.exists() and path.is_file():
            return sha256_file(path)
    return {"path": "", "exists": False, "sha256": "", "bytes": 0}


def existing_file_path(value: Any) -> str:
    path_text = str(value or "")
    if not path_text:
        return ""
    path = Path(path_text)
    return str(path) if path.exists() and path.is_file() else ""


def workspace_final_artifacts(workspace: Path) -> dict[str, Any]:
    final_tex = workspace / "final" / "paper.tex"
    final_pdf = workspace / "final" / "paper.pdf"
    tex_info = sha256_file(final_tex)
    pdf_info = sha256_file(final_pdf)
    return {
        "ready": bool(tex_info.get("exists") and pdf_info.get("exists")),
        "final_tex": tex_info,
        "final_pdf": pdf_info,
        "missing": [
            name
            for name, info in {"workspace_final_tex": tex_info, "workspace_final_pdf": pdf_info}.items()
            if not info.get("exists")
        ],
    }


def count_bib_entries(path: Path) -> int:
    if not path.exists():
        return 0
    return len(re.findall(r"@\w+\s*\{", read_text(path)))


def count_tex_citations(path: Path) -> int:
    if not path.exists():
        return 0
    text = latex_text_with_inputs(path)
    keys: set[str] = set()
    for match in CITE_RE.finditer(text):
        keys.update(key.strip() for key in match.group(1).split(",") if key.strip())
    return len(keys)


def latex_text_with_inputs(path: Path, *, seen: set[Path] | None = None) -> str:
    if not path.exists():
        return ""
    seen = seen or set()
    try:
        resolved = path.resolve()
    except Exception:
        resolved = path
    if resolved in seen:
        return ""
    seen.add(resolved)
    text = read_text(path)

    def repl(match: re.Match[str]) -> str:
        raw = match.group(1).strip()
        if not raw:
            return ""
        candidate = Path(raw)
        if not candidate.suffix:
            candidate = candidate.with_suffix(".tex")
        if not candidate.is_absolute():
            candidate = path.parent / candidate
        return "\n" + latex_text_with_inputs(candidate, seen=seen) + "\n"

    return re.sub(r"\\(?:input|include)\{([^{}]+)\}", repl, text)


def latex_section_titles(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [item.strip() for item in re.findall(r"\\section\*?\{([^{}]+)\}", latex_text_with_inputs(path))]


def read_json_loose(path: Path, default: Any) -> Any:
    try:
        return load_json(path, default)
    except Exception:
        return default


def json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    return value


def write_phase_log(workspace: Path, phase: str, payload: dict[str, Any]) -> None:
    logs = workspace / "project_bridge_phases"
    logs.mkdir(parents=True, exist_ok=True)
    write_json(logs / f"{phase}.json", json_safe(payload))


def paper_orchestra_scripts(repo_dir: Path) -> dict[str, Path]:
    return {
        "validate_inputs": repo_dir / "skills" / "paper-orchestra" / "scripts" / "validate_inputs.py",
        "check_idea_density": repo_dir / "skills" / "paper-orchestra" / "scripts" / "check_idea_density.py",
        "validate_consistency": repo_dir / "skills" / "paper-orchestra" / "scripts" / "validate_consistency.py",
        "check_tex_packages": repo_dir / "skills" / "paper-orchestra" / "scripts" / "check_tex_packages.py",
        "anti_leakage_check": repo_dir / "skills" / "paper-orchestra" / "scripts" / "anti_leakage_check.py",
        "snapshot": repo_dir / "skills" / "content-refinement-agent" / "scripts" / "snapshot.py",
        "validate_outline": repo_dir / "skills" / "outline-agent" / "scripts" / "validate_outline.py",
        "render_matplotlib": repo_dir / "skills" / "plotting-agent" / "scripts" / "render_matplotlib.py",
        "render_diagram": repo_dir / "skills" / "plotting-agent" / "scripts" / "render_diagram.py",
        "s2_search": repo_dir / "skills" / "literature-review-agent" / "scripts" / "s2_search.py",
        "validate_pool": repo_dir / "skills" / "literature-review-agent" / "scripts" / "validate_pool.py",
        "bibtex_format": repo_dir / "skills" / "literature-review-agent" / "scripts" / "bibtex_format.py",
        "sync_keys": repo_dir / "skills" / "literature-review-agent" / "scripts" / "sync_keys.py",
        "extract_metrics": repo_dir / "skills" / "section-writing-agent" / "scripts" / "extract_metrics.py",
        "orphan_cite_gate": repo_dir / "skills" / "section-writing-agent" / "scripts" / "orphan_cite_gate.py",
        "latex_sanity": repo_dir / "skills" / "section-writing-agent" / "scripts" / "latex_sanity.py",
    }


def citation_pool_size(pool: Any) -> int:
    if isinstance(pool, dict):
        for key in ("papers", "citations", "items", "entries", "references"):
            value = pool.get(key)
            if isinstance(value, dict):
                return len(value)
            if isinstance(value, list):
                return len(value)
        return 0
    if isinstance(pool, list):
        return len(pool)
    return 0


def workspace_status(workspace: Path) -> dict[str, Any]:
    files = {
        "outline": workspace / "outline.json",
        "captions": workspace / "figures" / "captions.json",
        "refs_bib": workspace / "refs.bib",
        "citation_pool": workspace / "citation_pool.json",
        "intro_relwork": workspace / "drafts" / "intro_relwork.tex",
        "draft_tex": workspace / "drafts" / "paper.tex",
        "final_tex": workspace / "final" / "paper.tex",
        "final_pdf": workspace / "final" / "paper.pdf",
        "provenance": workspace / "provenance.json",
        "tex_profile": workspace / "tex_profile.json",
        "metrics": workspace / "metrics.json",
        "raw_candidates": workspace / "raw_candidates.json",
        "deduped_candidates": workspace / "deduped_candidates.json",
        "raw_pool": workspace / "raw_pool.json",
    }
    figures = sorted((workspace / "figures").glob("*.png")) if (workspace / "figures").exists() else []
    outline = read_json_loose(files["outline"], {})
    expected_figures = []
    if isinstance(outline, dict):
        expected_figures = [str(row.get("figure_id")) for row in outline.get("plotting_plan", []) if isinstance(row, dict) and row.get("figure_id")]
    figure_ids = {path.stem for path in figures}
    missing_figures = [fig for fig in expected_figures if fig not in figure_ids]
    pool = read_json_loose(files["citation_pool"], {})
    raw_pool = read_json_loose(files["raw_pool"], {})
    return {
        "files": {key: sha256_file(path) for key, path in files.items()},
        "figure_count": len(figures),
        "expected_figure_count": len(expected_figures),
        "missing_figures": missing_figures,
        "figures": [str(path) for path in figures[:80]],
        "bib_entry_count": count_bib_entries(files["refs_bib"]),
        "citation_pool_count": citation_pool_size(pool),
        "raw_pool_count": citation_pool_size(raw_pool),
        "draft_citation_count": count_tex_citations(files["draft_tex"]),
        "final_citation_count": count_tex_citations(files["final_tex"]),
        "draft_sections": latex_section_titles(files["draft_tex"]),
        "final_sections": latex_section_titles(files["final_tex"]),
        "missing_required_outputs": [
            key
            for key, path in files.items()
            if key in {"refs_bib", "citation_pool", "intro_relwork", "draft_tex", "final_tex", "final_pdf"} and not path.exists()
        ],
    }


def phase_ready(workspace: Path, phase: str, *, min_references: int = 0, venue: str = "", project: str = "") -> tuple[bool, list[str]]:
    status = workspace_status(workspace)
    files = status["files"]
    missing: list[str] = []
    for key in PHASE_REQUIRED_FILES.get(phase, []):
        if not files.get(key, {}).get("exists"):
            missing.append(key)
    if phase == "plotting":
        if status.get("missing_figures"):
            missing.append("figures:" + ",".join(status["missing_figures"]))
    if phase == "literature":
        if min_references and status.get("bib_entry_count", 0) < min_references:
            missing.append(f"refs_bib_entries<{min_references}")
        if min_references and status.get("citation_pool_count", 0) < min_references:
            missing.append(f"citation_pool_papers<{min_references}")
    if phase == "section-writing":
        for title in section_titles_for_venue(venue, project=project):
            if not any(title.lower() in item.lower() for item in status.get("draft_sections", [])):
                missing.append(f"draft_section:{title}")
        if venue and files.get("draft_tex", {}).get("exists"):
            validation = validate_venue_template_format(read_text(workspace / "drafts" / "paper.tex"), venue, project=project)
            if validation.get("status") != "pass":
                missing.append("draft_venue_template_format")
    if phase == "refinement":
        for title in section_titles_for_venue(venue, project=project):
            if not any(title.lower() in item.lower() for item in status.get("final_sections", [])):
                missing.append(f"final_section:{title}")
        if venue and files.get("final_tex", {}).get("exists"):
            validation = validate_venue_template_format(read_text(workspace / "final" / "paper.tex"), venue, project=project)
            if validation.get("status") != "pass":
                missing.append("final_venue_template_format")
    return not missing, missing



def ensure_writing_vendor(venue: str, repo_dir: Path, *, skip_clone: bool = False) -> dict[str, Any]:
    script = ROOT / "modules" / "writing" / "scripts" / "sync_writing_vendor.py"
    payload: dict[str, Any] = {"status": "missing_script", "required_ready": False, "warnings": []}
    if not script.exists():
        payload["warnings"].append("modules/writing/scripts/sync_writing_vendor.py is missing; writing vendor references cannot be checked.")
        return payload
    cmd = [sys.executable, str(script), "--venue", venue, "--paper-orchestra-dir", str(repo_dir), "--compact"]
    if skip_clone:
        cmd.append("--check")
    result = run(cmd, cwd=ROOT, timeout=900, required=False)
    payload["command"] = result
    parsed: dict[str, Any] = {}
    try:
        parsed = json.loads(result.get("stdout_tail") or "{}")
    except Exception as exc:
        payload["warnings"].append(f"writing vendor check returned unparsable JSON: {exc}")
    if parsed:
        payload.update(parsed)
    if result.get("return_code") != 0 and not payload.get("required_ready"):
        payload.setdefault("warnings", [])
        payload["warnings"].append(
            "writing vendor dependencies are missing. Run `python modules/writing/scripts/sync_writing_vendor.py --venue "
            + str(venue or "<venue>")
            + "` before paper generation, or allow the paper bridge to run without --skip-clone."
        )
    payload["status"] = "ready" if payload.get("required_ready") else "blocked"
    return payload


def clone_or_update(repo_dir: Path, *, skip_clone: bool = False) -> tuple[dict[str, Any], list[str]]:
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    commands: list[dict[str, Any]] = []
    has_module_snapshot = repo_dir.exists() and (repo_dir / "skills" / "paper-orchestra").exists()
    if repo_dir.exists() and (repo_dir / ".git").exists():
        commands.append(run(["git", "rev-parse", "--short", "HEAD"], cwd=repo_dir, required=False))
        commands.append(run(["git", "remote", "get-url", "origin"], cwd=repo_dir, required=False))
    elif has_module_snapshot:
        warnings.append(f"Using writing module vendor snapshot: {repo_dir}")
    elif skip_clone:
        warnings.append(f"writing source-method reference is missing and --skip-clone was set: {repo_dir}")
    else:
        if repo_dir.exists() and any(repo_dir.iterdir()):
            raise RuntimeError(f"{repo_dir} exists but is not a git checkout or writing module snapshot; refusing to overwrite it.")
        commands.append(run(["git", "clone", "--depth", "1", PAPER_ORCHESTRA_URL, str(repo_dir)], cwd=ROOT, required=True))
    commit = ""
    origin = PAPER_ORCHESTRA_URL
    if (repo_dir / ".git").exists():
        commit_result = run(["git", "rev-parse", "HEAD"], cwd=repo_dir, required=False)
        origin_result = run(["git", "remote", "get-url", "origin"], cwd=repo_dir, required=False)
        commands.extend([commit_result, origin_result])
        commit = commit_result.get("stdout_tail", "").strip()
        origin = origin_result.get("stdout_tail", "").strip() or PAPER_ORCHESTRA_URL
    elif has_module_snapshot:
        provenance = read_json_loose(WRITING_VENDOR_PROVENANCE, {})
        source_meta = {}
        if isinstance(provenance, dict):
            source_meta = (provenance.get("sources") or {}).get("PaperOrchestra") or {}
        if isinstance(source_meta, dict):
            commit = str(source_meta.get("commit") or "")
            origin = str(source_meta.get("origin") or PAPER_ORCHESTRA_URL)
    return {
        "repo_path": str(repo_dir),
        "repo_exists": repo_dir.exists(),
        "repo_is_git_checkout": (repo_dir / ".git").exists(),
        "repo_is_module_snapshot": has_module_snapshot,
        "origin": origin,
        "commit": commit,
        "commands": commands,
    }, warnings


def link_skills(repo_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    skill_root = ROOT / ".claude" / "skills"
    skill_root.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    for name in PAPER_ORCHESTRA_SKILLS:
        source = repo_dir / "skills" / name
        target = skill_root / name
        row = {"name": name, "source": str(source), "target": str(target), "status": "missing_source"}
        if not source.exists():
            warnings.append(f"missing writing source-method file: {source}")
            rows.append(row)
            continue
        if target.exists() or target.is_symlink():
            try:
                if target.is_symlink() and Path(os.readlink(target)) == source:
                    row["status"] = "linked"
                elif target.is_symlink():
                    target.unlink()
                    target.symlink_to(source, target_is_directory=True)
                    row["status"] = "relinked"
                else:
                    row["status"] = "conflict_existing_directory"
                    warnings.append(f"skill target exists and is not a symlink; left untouched: {target}")
            except OSError as exc:
                row["status"] = "link_failed"
                row["error"] = str(exc)
                warnings.append(f"failed to link {name}: {exc}")
        else:
            target.symlink_to(source, target_is_directory=True)
            row["status"] = "linked"
        rows.append(row)
    return rows, warnings


def first_existing(paths: list[Path]) -> Path | None:
    for path in paths:
        if path.exists() and path.is_file() and read_text(path).strip():
            return path
    return None




def norm_path(value: Any) -> str:
    return str(value or "").rstrip("/")


def current_route_row(row: dict[str, Any], active_repo: dict[str, Any]) -> bool:
    active_path = norm_path(active_repo.get("repo_path") or active_repo.get("local_path"))
    active_name = str(active_repo.get("name") or active_repo.get("repo_name") or "").strip().lower()
    row_path = norm_path(row.get("repo_path") or row.get("active_repo_path") or row.get("local_path"))
    row_name = str(row.get("repo_name") or row.get("repo") or row.get("base_repo") or "").strip().lower()
    if active_path and row_path:
        return row_path == active_path
    if active_name and row_name:
        return row_name == active_name
    return False


def current_claim_ledger_for_writer(paths: Any) -> dict[str, Any]:
    raw = load_json(paths.state / "claim_ledger.json", {"claims": []})
    active_repo = load_json(paths.state / "active_repo.json", {})
    experiments = load_json(paths.state / "experiment_registry.json", [])
    if not isinstance(active_repo, dict):
        active_repo = {}
    rows = [row for row in experiments if isinstance(row, dict) and current_route_row(row, active_repo)] if isinstance(experiments, list) else []
    eligible_run_ids = {
        str(row.get("experiment_id") or row.get("name") or "")
        for row in rows
        if (
            str(row.get("status") or "").lower() in {"completed", "success"}
            and row.get("audit_ready")
            and not row_promotion_blockers(row)
            and str(row.get("claim_verdict") or "").strip().lower() in SUPPORTIVE_CLAIM_VERDICTS
        )
    }
    claims = raw.get("claims", []) if isinstance(raw, dict) and isinstance(raw.get("claims"), list) else []
    filtered: list[dict[str, Any]] = []
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        text = str(claim.get("text") or "").strip()
        if not text or text.lower() == "missing":
            continue
        supporting = [str(item) for item in claim.get("supporting_runs", []) or [] if str(item) in eligible_run_ids]
        if not supporting:
            continue
        filtered.append({**claim, "supporting_runs": supporting, "support_count": len(supporting), "status": claim.get("status") or "supported"})
    return {
        "claims": filtered,
        "policy": "Only these filtered current selected-route claims may be used in manuscript contribution/results prose. If empty, write no positive result claim.",
        "active_repo": {"name": active_repo.get("name", ""), "repo_path": active_repo.get("repo_path") or active_repo.get("local_path") or ""},
        "eligible_current_route_supporting_runs": sorted(eligible_run_ids),
    }

def compact_json(path: Path, limit: int = 12000) -> str:
    payload = load_json(path, {})
    try:
        return json.dumps(payload, ensure_ascii=False, indent=2)[:limit]
    except Exception:
        return str(payload)[:limit]


def render_idea(project: str, title: str) -> str:
    cfg = load_project_config(project)
    paths = build_paths(project)
    topic = str(cfg.get("topic") or title or project).strip()
    init_brief = read_text(paths.planning / "init_brief.md")
    research_plan = read_text(paths.planning / "research_plan.md")
    active_repo_raw = load_json(paths.state / "active_repo.json", {})
    active_repo = active_repo_raw if isinstance(active_repo_raw, dict) else {}
    novelty_map = compact_json(paths.state / "novelty_map.json", 6000)
    claim_payload = current_claim_ledger_for_writer(paths)
    claim_ledger = json.dumps(claim_payload, ensure_ascii=False, indent=2)[:6000]
    base_title = str(active_repo.get("selected_base_title") or active_repo.get("literature_base_title") or active_repo.get("paper_title") or "current selected base recorded in research state")
    repo_name = str(active_repo.get("name") or active_repo.get("repo_name") or "current selected repository recorded in research state")
    repo_path = str(active_repo.get("repo_path") or active_repo.get("local_path") or "")
    planning_text = init_brief or research_plan or "Use current research state files and selected-base evidence conservatively."
    writing_contract = read_text(WRITING_CONTRACT)[:18000] or "Read modules/writing/SKILL.md before writing."
    return f"""# Writing Input Packet

## Writing Module Contract

```markdown
{writing_contract}
```

## Project Context

Project: `{project}`
Topic: {topic}
Current selected base: `{base_title}`
Current repository: `{repo_name}` at `{repo_path}`

The manuscript must be about the current selected route only. Legacy routes, internal gate reports, unsupported runs, and negative project history are not manuscript content.

## Local Planning Evidence

{planning_text}

## Current Claim Boundary

Use the filtered current-route claim payload below only to decide which result claims are allowed. If it is empty or has no supported current-route run, write no positive empirical superiority claim. Still write a polished method/theory/protocol paper draft.

```json
{claim_ledger}
```

## Novelty And Direction Evidence

```json
{novelty_map}
```
"""


def metric_value(row: dict[str, Any]) -> str:
    value = row.get("metric_value", row.get("result", ""))
    if isinstance(value, float):
        return f"{value:.8g}"
    return str(value)


def render_experimental_log(project: str) -> str:
    paths = build_paths(project)
    experiments = load_json(paths.state / "experiment_registry.json", [])
    datasets = load_json(paths.state / "dataset_registry.json", [])
    rows = experiments if isinstance(experiments, list) else []
    active_repo = load_json(paths.state / "active_repo.json", {})
    if not isinstance(active_repo, dict):
        active_repo = {}
    current_rows = [row for row in rows if isinstance(row, dict) and current_route_row(row, active_repo)]
    claim_ready_rows = [
        row
        for row in current_rows
        if (
            str(row.get("status") or "").lower() in {"completed", "success"}
            and row.get("audit_ready")
            and not row_promotion_blockers(row)
            and str(row.get("claim_verdict") or "").strip().lower() in SUPPORTIVE_CLAIM_VERDICTS
        )
    ]
    reference_rows = [
        row for row in current_rows
        if str(row.get("method") or "").strip() == "selected_base_reference"
        and str(row.get("status") or "").lower() in {"completed", "success"}
        and row.get("audit_ready")
    ]
    dataset_names = sorted({str(row.get("dataset") or "") for row in current_rows if row.get("dataset")})
    metric_names = sorted({str(row.get("metric_name") or row.get("metric") or "") for row in current_rows if (row.get("metric_name") or row.get("metric"))})
    table = [
        "| Experiment | Role | Method | Dataset | Metric | Value | Manuscript Use | Artifact |",
        "|---|---|---|---:|---:|---:|---|---|",
    ]
    source_rows = claim_ready_rows or reference_rows[-3:]
    for row in source_rows:
        role = "claim_supporting_result" if row in claim_ready_rows else "reference_calibration"
        use = "may support a result claim" if row in claim_ready_rows else "may calibrate protocol/reference reproduction only; not a superiority claim"
        table.append(
            "| "
            + " | ".join(
                str(item).replace("|", "/")
                for item in [
                    row.get("experiment_id") or row.get("name") or "",
                    role,
                    row.get("method") or "",
                    row.get("dataset") or "",
                    row.get("metric_name") or row.get("metric") or "",
                    metric_value(row),
                    use,
                    row.get("artifact_path") or row.get("audit_path") or "",
                ]
            )
            + " |"
        )
    if len(table) == 2:
        table.append("| no_current_route_numeric_result | protocol_only | none | none | none |  | write method/theory/protocol only | none |")
    return f"""# Experimental Log

## 1. Experimental Setup

* **Datasets recorded for current selected route:** {", ".join(dataset_names) if dataset_names else "No completed current-route dataset is recorded yet."}
* **Evaluation metrics recorded for current selected route:** {", ".join(metric_names) if metric_names else "No metric is recorded yet."}
* **Baselines compared:** Use only rows in the numeric table below. Do not add unrun baselines to result tables.
* **Implementation details:** All implementation and environment claims must trace to research state files, the selected repository, or explicit experiment artifacts.
* **Dataset registry snapshot:** `{paths.state / "dataset_registry.json"}` contains {len(datasets) if isinstance(datasets, list) else 0} dataset records.

## 2. Manuscript-Safe Numeric Data

{chr(10).join(table)}

## 3. Manuscript-Safe Qualitative Guidance

* If the table contains only `reference_calibration` rows, the paper may report that the selected base/protocol is available for calibration, but it must not claim that the proposed method improves over it.
* If no claim-supporting result exists, do not write a proposal or planned-study section. Write Experiments around verified reference calibration, dataset/protocol facts, implementation details, and completed protocol comparisons without future-tense or TODO language.
* Do not mention failed hypotheses, negative outcomes, internal blocker names, gate names, or legacy-route narratives in the manuscript body.
* The draft should still be written as a high-quality conference paper: strong motivation, precise method, equations, algorithmic description, and reproducibility details.
"""


def render_method_contract(project: str, venue: str) -> str:
    paths = build_paths(project)
    trajectory = load_json(paths.state / "research_trajectory_system.json", {})
    third_party = load_json(paths.state / "third_party_research_stack.json", {})
    optimization = load_json(paths.state / "trajectory_optimization_plan.json", {})
    assurance = load_json(paths.state / "research_assurance_layer.json", {})
    evidence_integrity = load_json(paths.state / "research_evidence_integrity.json", {})
    summary = trajectory.get("summary", {}) if isinstance(trajectory.get("summary", {}), dict) else {}
    bindings = third_party.get("capability_bindings", []) if isinstance(third_party.get("capability_bindings", []), list) else []
    queue = optimization.get("queue", []) if isinstance(optimization.get("queue", []), list) else []
    compact = {
        "venue": venue,
        "trajectory_summary": summary,
        "assurance_status": assurance.get("status") if isinstance(assurance, dict) else "",
        "evidence_integrity_status": evidence_integrity.get("status") if isinstance(evidence_integrity, dict) else "",
        "method_bindings": bindings[:20],
        "active_queue_head": queue[:8],
        "source_state_files": {
            "trajectory": str(paths.state / "research_trajectory_system.json"),
            "third_party_contracts": str(paths.state / "third_party_research_stack.json"),
            "optimization_plan": str(paths.state / "trajectory_optimization_plan.json"),
            "assurance_layer": str(paths.state / "research_assurance_layer.json"),
            "evidence_integrity": str(paths.state / "research_evidence_integrity.json"),
        },
    }
    writing_contract = read_text(WRITING_CONTRACT)[:12000]
    return f"""# Writing Method Contract

This file is an internal research workflow contract, not paper prose. It binds the writing module to the same research trajectory used by environment selection and experiment iteration.

The writer must treat source-method provenance as part of TASTE's normal process, not as a separate appendix or a main-paper section. Use it only to decide how to plan, verify, prune, repair, cite, and gate the manuscript. Do not describe this contract as a contribution unless local evidence explicitly supports it.

## Writing Module

```markdown
{writing_contract}
```

## Runtime State

```json
{json.dumps(compact, ensure_ascii=False, indent=2)}
```
"""


def render_guidelines(venue: str, project: str = "") -> str:
    venue_text = venue or "target venue"
    profile = venue_template_profile(venue_text, project=project)
    policy = venue_submission_policy(venue_text, project=project)
    requirements = load_venue_requirements(venue_text, project=project)
    sources = requirements.get("official_sources", []) if isinstance(requirements, dict) else []
    source_lines = []
    for item in sources if isinstance(sources, list) else []:
        if isinstance(item, dict):
            source_lines.append(f"* {item.get('label', 'official source')}: {item.get('url', '')}")
    source_summary = "\n".join(source_lines) or "* No resolved official source is cached yet; The workflow must resolve venue_requirements.json before drafting."
    profile_json = json.dumps(profile, ensure_ascii=False, indent=2)
    format_notes = "\n".join(f"* {item}" for item in profile.get("submission_notes", []))
    page_limit_note = str(profile.get("page_limit_note") or "").strip()
    body_min = int(policy.get("body_page_min") or 0) if isinstance(policy, dict) else 0
    body_max = int(policy.get("body_page_max") or 0) if isinstance(policy, dict) else 0
    ref_max = int(policy.get("reference_page_max") or 0) if isinstance(policy, dict) else 0
    total_max = int(policy.get("total_page_max") or 0) if isinstance(policy, dict) else 0
    official_min_refs = int(policy.get("official_min_references") or policy.get("min_references") or 0) if isinstance(policy, dict) else 0
    quality_ref_target = int(policy.get("reference_quality_target") or policy.get("reference_quality_target") or 0) if isinstance(policy, dict) else 0
    active_ref_target = max(official_min_refs, quality_ref_target)
    body_rule = f"{body_min}-{body_max} pages" if body_min and body_max else f"up to {body_max} pages" if body_max else "not locally capped"
    ref_rule = f"up to {ref_max} pages" if ref_max else "not locally capped"
    total_rule = f"up to {total_max} pages" if total_max else "not locally capped"
    if official_min_refs:
        ref_count_rule = f"official minimum at least {official_min_refs} verified citation keys"
    elif quality_ref_target:
        ref_count_rule = f"writing quality target at least {quality_ref_target} verified citation keys; this is not an official venue minimum"
    else:
        ref_count_rule = "verified citation keys from the local pool"
    policy_summary = (
        f"Current TASTE venue policy for {venue_text}: main/body pages are {body_rule}; "
        f"reference pages are {ref_rule}; total pages are {total_rule}; bibliography target is {ref_count_rule}."
    )
    return f"""# Venue Guidelines for Writing Run

## Submission deadline

Use the current date and venue-specific local state when available. If no official deadline is present in research state, do not invent one.

## Official source cache

{source_summary}

## Page limit

The normality gate for this framework expects a venue-formatted manuscript PDF. Main text should be normal paper prose rather than a workflow process report. The resolved target venue policy below wins; do not apply one venue's rules to another venue.

{policy_summary}

{page_limit_note or "No venue-specific page limit is recorded in research state yet; fetch and preserve the official author kit when available."}

## Mandatory sections

The generated main paper for {venue_text} must contain, in this order:

{mandatory_sections_markdown(venue_text, project=project)}

Appendices are allowed only after the normal venue-specific main sections. Do not use top-level sections such as Research Workflow, Readiness Matrix, Claim Ledger, Autonomous Research Trajectory, or Reviewer-Facing Framing in the main paper.

## Citation requirements

Use verified bibliographic metadata. Follow the current venue normality target above; if the citation count already passes the active audit, do not add references merely to satisfy a stale generic number. Never fabricate citations.

## Venue LaTeX format

Target format profile:

```json
{profile_json}
```

{format_notes}

Follow the resolved official venue package exactly: document class, options, style files, bibliography style, fonts, margins, and anonymity mode must come from the current `venue_requirements.json`. Do not infer one venue's template or page rule from another venue.

Official sources and TASTE quality targets are recorded in `paper/venues/<venue>/venue_requirements.json`; if that file is missing or blocked, writing must stop before drafting rather than guessing venue rules.

## Layout and reference-fit diagnosis

If the PDF violates page policy or looks cramped, diagnose the cause before editing: figure/table footprint first, then bibliography/reference-page footprint, then prose length. If body pages are already within the official venue limit, do not treat the job as prose shortening. Do not cut scientific content merely because total pages include references; resize/redraw/move figures or repair bibliography density when those are the actual source of overflow.

## Evidence and claim rules

All result claims must trace to current selected-route local experiment artifacts and supported claim verdicts. If evidence is insufficient for a positive result claim, the manuscript should still present the strongest method/theory/protocol story, but empirical superiority claims must be omitted from the abstract, introduction, and results; do not turn internal audit diagnostics into the paper main story.

## Formatting rules

Use `workspace/inputs/template.tex` as the venue format contract. Preserve its `\\documentclass`, required packages, margins, fonts, and bibliography style. Never replace a venue template with `\\documentclass{{article}}`. Keep figures and tables before the Conclusion unless an appendix is explicitly used.
"""


def fallback_template(title: str, venue: str, project: str = "") -> str:
    return venue_fallback_template(title, venue, project=project)


def copy_template_sidecars(template_source: Path, workspace: Path) -> list[str]:
    copied: list[str] = []
    source_dir = template_source.parent
    suffixes = {'.sty', '.bst', '.cls', '.bbx', '.cbx'}
    for source in sorted(source_dir.rglob('*')):
        if source.is_file() and (source.suffix.lower() in suffixes or source.name == 'math_commands.tex'):
            rel = source.relative_to(source_dir)
            for target_dir in [workspace, workspace / 'inputs']:
                target = target_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            copied.append(str(source))
    return copied


def copy_workspace_template_sidecars_to_final(workspace: Path, final_dir: Path) -> list[str]:
    copied: list[str] = []
    suffixes = {'.sty', '.bst', '.cls', '.bbx', '.cbx'}
    for source_dir in [workspace, workspace / 'inputs']:
        if not source_dir.exists():
            continue
        for source in sorted(source_dir.rglob('*')):
            if source.is_file() and (source.suffix.lower() in suffixes or source.name == 'math_commands.tex'):
                rel = source.relative_to(source_dir)
                target = final_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                copied.append(str(source))
    return sorted(set(copied))


def template_candidate_paths(project: str, venue: str, state: dict[str, Any]) -> list[Path]:
    paper = ensure_paper_dirs(project)
    candidates: list[Path] = []
    template_source = state.get("template_source")
    if isinstance(template_source, dict):
        source_dir = template_source.get("source_dir")
        if source_dir:
            main_tex = find_main_tex(Path(str(source_dir)))
            if main_tex:
                candidates.append(main_tex)
    for key in ["template_main", "venue_template_main", "template_path"]:
        value = state.get(key)
        if value:
            candidates.append(Path(str(value)))
    for alias in venue_slug_aliases(venue):
        source_dir = paper["venue_dir"] / alias / "source"
        if not source_dir.exists():
            continue
        main_tex = find_main_tex(source_dir)
        if main_tex:
            candidates.append(main_tex)
        candidates.extend(sorted(source_dir.rglob("*.tex")))
    deduped: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def choose_template_source(project: str, venue: str, state: dict[str, Any]) -> tuple[Path | None, list[dict[str, Any]]]:
    rejected: list[dict[str, Any]] = []
    for candidate in template_candidate_paths(project, venue, state):
        if not candidate.exists() or not candidate.is_file():
            rejected.append({"path": str(candidate), "reason": "missing_or_not_file"})
            continue
        text = read_text(candidate)
        if not text.strip():
            rejected.append({"path": str(candidate), "reason": "empty"})
            continue
        validation = validate_venue_template_format(text, venue, project=project)
        if validation.get("status") == "pass":
            return candidate, rejected
        rejected.append({"path": str(candidate), "reason": "venue_template_format_blocked", "validation": validation})
    return None, rejected


def prepare_workspace(project: str, venue: str, title: str, repo_dir: Path, force: bool = True) -> tuple[Path, dict[str, Any]]:
    paths = build_paths(project)
    venue_slug = slugify(venue)
    workspace = paths.root / "paper" / "writing" / venue_slug / "workspace"
    init_script = repo_dir / "skills" / "paper-orchestra" / "framework" / "scripts" / "init_workspace.py"
    commands: list[dict[str, Any]] = []
    if init_script.exists():
        cmd = [sys.executable, str(init_script), "--out", str(workspace)]
        # writing scaffold is overlay-only; --force permits a
        # non-empty workspace but does not delete existing phase outputs.
        cmd.append("--force")
        commands.append(run(cmd, cwd=ROOT, required=True))
    else:
        for sub in ["inputs", "inputs/figures", "figures", "drafts", "refinement", "final", "cache"]:
            (workspace / sub).mkdir(parents=True, exist_ok=True)
    inputs = workspace / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    write_text(inputs / "idea.md", render_idea(project, title))
    write_text(inputs / "experimental_log.md", render_experimental_log(project))
    write_text(inputs / "conference_guidelines.md", render_guidelines(venue, project=project))
    write_text(inputs / "method_contract.md", render_method_contract(project, venue))

    state = get_active_paper_state(project, venue=venue)
    template_source, rejected_templates = choose_template_source(project, venue, state)
    copied_template_sidecars: list[str] = []
    if template_source:
        shutil.copy2(template_source, inputs / "template.tex")
        copied_template_sidecars = copy_template_sidecars(template_source, workspace)
    else:
        raise RuntimeError("official venue template is unavailable; run resolve_venue_requirements.py and fetch_latex_template.py before writing")

    template_validation = validate_venue_template_format(read_text(inputs / "template.tex"), venue, project=project)
    input_hashes = {name: sha256_file(inputs / name) for name in ["idea.md", "experimental_log.md", "template.tex", "conference_guidelines.md", "method_contract.md"]}
    return workspace, {
        "workspace": str(workspace),
        "commands": commands,
        "inputs": input_hashes,
        "template_source": str(template_source or ""),
        "template_sidecars": copied_template_sidecars,
        "rejected_templates": rejected_templates,
        "template_profile": venue_template_profile(venue, project=project),
        "template_validation": template_validation,
    }


def maybe_run_prepare_phase(project: str, venue: str, title: str, repo_dir: Path, *, force: bool) -> tuple[Path, dict[str, Any]]:
    workspace, info = prepare_workspace(project, venue, title, repo_dir, force=force)
    ready, missing = phase_ready(workspace, "prepare", project=project)
    phase = {
        "phase": "prepare",
        "status": "pass" if ready else "blocked",
        "missing": missing,
        "workspace_status": workspace_status(workspace),
        "finished_at": now_iso(),
    }
    write_phase_log(workspace, "prepare", phase)
    info["phase"] = phase
    return workspace, info


def _valid_template_source_exists(project: str, venue: str) -> tuple[bool, str, list[dict[str, Any]]]:
    state = get_active_paper_state(project, venue=venue)
    template_source, rejected = choose_template_source(project, venue, state)
    return bool(template_source), str(template_source or ""), rejected


def ensure_venue_contract(project: str, venue: str, *, refresh_current_venue: bool = False) -> dict[str, Any]:
    """Ensure writing has current venue rules and an official template.

    run_paper_pipeline.py normally performs this preflight. The bridge enforces
    it too because TASTE can call the writing module directly from trajectory
    actions or web paper actions; direct calls must not silently reuse stale
    templates or guessed page rules.
    """
    paths = build_paths(project)
    venue_slug = slugify(venue)
    req_path = paths.root / "paper" / "venues" / venue_slug / "venue_requirements.json"
    commands: list[dict[str, Any]] = []
    requirements = load_venue_requirements(venue, project=project)
    if refresh_current_venue or not requirements:
        cmd = [sys.executable, str(ROOT / "modules" / "writing" / "scripts" / "resolve_venue_requirements.py"), "--project", project, "--venue", venue]
        if refresh_current_venue:
            cmd.append("--refresh-current-venue")
        result = run(cmd, cwd=ROOT, required=False)
        commands.append(result)
        if result.get("return_code") != 0:
            raise RuntimeError(
                "writing venue-intelligence did not resolve current official venue requirements; "
                "refusing to draft from stale or guessed rules."
            )
        requirements = load_venue_requirements(venue, project=project)
    if not requirements:
        raise RuntimeError("writing venue requirements are missing or blocked; refusing to draft from stale or guessed rules.")

    template_ok, template_path, rejected = _valid_template_source_exists(project, venue)
    if not template_ok:
        result = run([sys.executable, str(ROOT / "modules" / "writing" / "scripts" / "fetch_latex_template.py"), "--project", project, "--venue", venue], cwd=ROOT, required=False)
        commands.append(result)
        if result.get("return_code") != 0:
            raise RuntimeError(
                "writing official LaTeX template fetch/validation failed; "
                "refusing to expose a fallback template as a paper."
            )
        template_ok, template_path, rejected = _valid_template_source_exists(project, venue)
    if not template_ok:
        raise RuntimeError("writing could not find a validated official venue template after fetch.")

    template = requirements.get("template") if isinstance(requirements.get("template"), dict) else {}
    policy = requirements.get("venue_submission_policy") if isinstance(requirements.get("venue_submission_policy"), dict) else {}
    return {
        "status": "pass",
        "venue": venue,
        "venue_requirements_path": str(req_path),
        "venue_requirements_status": requirements.get("status"),
        "official_source_count": len(requirements.get("official_sources", [])) if isinstance(requirements.get("official_sources"), list) else 0,
        "template_path": template_path,
        "template_repository": template.get("verified_repository_url") or template.get("repository_url") or template.get("official_source_url") or "",
        "template_commit": template.get("verified_repository_commit") or requirements.get("official_repository_commit") or "",
        "template_directory": template.get("verified_directory_hint") or template.get("directory_hint") or "",
        "template_main_tex": template.get("main_tex") or "",
        "body_page_max": policy.get("body_page_max", ""),
        "official_min_references": policy.get("official_min_references") or policy.get("min_references") or 0,
        "reference_quality_target": policy.get("reference_quality_target") or policy.get("reference_quality_target") or 0,
        "reference_target_source": policy.get("reference_target_source") or "",
        "refresh_current_venue": refresh_current_venue,
        "commands": commands,
        "rejected_template_candidates": rejected[:8],
    }


def refresh_current_paper_workspace_outputs(workspace: Path, *, reason: str) -> dict[str, Any]:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = workspace / "current_paper_regeneration_backups" / stamp
    removed: list[str] = []
    backed_up: list[str] = []
    targets = [
        workspace / "drafts" / "paper.tex",
        workspace / "final" / "paper.tex",
        workspace / "final" / "paper.pdf",
        workspace / "provenance.json",
        workspace / "project_bridge_provenance.json",
    ]
    for path in targets:
        if not path.exists():
            continue
        backup_path = backup_dir / path.relative_to(workspace)
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup_path)
        backed_up.append(str(backup_path))
        path.unlink()
        removed.append(str(path))
    for directory in [workspace / "refinement"]:
        if not directory.exists():
            continue
        backup_path = backup_dir / directory.relative_to(workspace)
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(directory, backup_path, dirs_exist_ok=True)
        backed_up.append(str(backup_path))
        shutil.rmtree(directory)
        removed.append(str(directory))
    return {
        "current_paper_regeneration_requested": True,
        "reason": reason,
        "backup_dir": str(backup_dir),
        "removed": removed,
        "backed_up": backed_up,
        "updated_at": now_iso(),
    }


def _append_query_value(queries: list[str], value: Any) -> None:
    if isinstance(value, str):
        text = re.sub(r"\s+", " ", value).strip()
        if text:
            queries.append(text)
    elif isinstance(value, dict):
        for key in ["title", "paper_title", "query", "search_query", "name"]:
            _append_query_value(queries, value.get(key))
    elif isinstance(value, list):
        for item in value[:80]:
            _append_query_value(queries, item)


def _project_literature_queries(workspace: Path) -> list[str]:
    project_root = workspace.parents[2] if len(workspace.parents) >= 3 else Path("")
    queries: list[str] = []
    for rel in [
        "state/literature_tool_packet.json",
        "state/current_find_research_plan.json",
        "planning/finding/read_results.json",
        "planning/finding/ideas.json",
        "planning/finding/find_results.json",
    ]:
        payload = read_json_loose(project_root / rel, {}) if project_root else {}
        if isinstance(payload, dict):
            for key in ["articles", "strong_recommendations", "readings", "ideas", "plans", "candidate_pool", "selected", "papers"]:
                _append_query_value(queries, payload.get(key))
            _append_query_value(queries, payload.get("selected_base_title"))
            _append_query_value(queries, payload.get("base_title"))
        elif isinstance(payload, list):
            _append_query_value(queries, payload)
    active_paper = read_json_loose(project_root / "state/active_paper.json", {}) if project_root else {}
    if isinstance(active_paper, dict):
        _append_query_value(queries, active_paper.get("title"))
        _append_query_value(queries, active_paper.get("verified_references"))
    return queries


def candidate_queries_from_outline(workspace: Path) -> list[str]:
    queries = list(BASELINE_QUERIES)
    queries.extend(_project_literature_queries(workspace))
    outline = read_json_loose(workspace / "outline.json", {})
    if isinstance(outline, dict):
        intro = outline.get("intro_related_work_plan", {})
        if isinstance(intro, dict):
            introduction = intro.get("introduction_strategy", {})
            related = intro.get("related_work_strategy", {})
            if isinstance(introduction, dict):
                for item in introduction.get("search_directions", []):
                    if isinstance(item, str):
                        queries.append(item)
                    elif isinstance(item, dict):
                        queries.extend(str(value) for value in item.values() if isinstance(value, str))
            if isinstance(related, dict):
                for cluster in related.get("subsections", []):
                    if not isinstance(cluster, dict):
                        continue
                    for key in ["methodology_cluster", "sota_investigation_mission", "limitation_hypothesis", "bridge_to_our_method"]:
                        if isinstance(cluster.get(key), str):
                            queries.append(str(cluster[key]))
                    raw_queries = cluster.get("limitation_search_queries", [])
                    if isinstance(raw_queries, list):
                        queries.extend(str(item) for item in raw_queries if str(item).strip())
    existing = read_json_loose(workspace / "deduped_candidates.json", {})
    candidates = existing.get("candidates", []) if isinstance(existing, dict) else []
    for row in candidates:
        if isinstance(row, dict) and row.get("title"):
            queries.append(str(row["title"]))
    seen: set[str] = set()
    out: list[str] = []
    for query in queries:
        query = re.sub(r"\s+", " ", query).strip()
        if len(query) < 6:
            continue
        key = query.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(query)
    return out


def normalize_title(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.lower())).strip()


def title_ratio(a: str, b: str) -> int:
    return int(round(100 * SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio()))


def search_s2(query: str, scripts: dict[str, Path], *, limit: int = 5) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    helper = scripts["s2_search"]
    result = run(
        [
            sys.executable,
            str(helper),
            "--query",
            query,
            "--limit",
            str(limit),
            "--fields",
            "title,abstract,year,authors,venue,externalIds,paperId,citationCount",
        ],
        cwd=ROOT,
        timeout=80,
        required=False,
    )
    if result["return_code"] != 0:
        return [], result
    try:
        data = json.loads(result.get("stdout_tail", "") or "{}")
    except Exception as exc:
        result["parse_error"] = str(exc)
        return [], result
    rows = data.get("data", []) if isinstance(data, dict) else []
    return rows if isinstance(rows, list) else [], result


def paper_identity(paper: dict[str, Any]) -> str:
    ext = paper.get("externalIds") if isinstance(paper.get("externalIds"), dict) else {}
    return str(paper.get("paperId") or ext.get("DOI") or ext.get("ArXiv") or normalize_title(str(paper.get("title") or "")))


def build_verified_literature_pool(workspace: Path, repo_dir: Path, *, min_references: int, max_s2_queries: int) -> dict[str, Any]:
    scripts = paper_orchestra_scripts(repo_dir)
    phase: dict[str, Any] = {
        "phase": "literature-deterministic-verification",
        "started_at": now_iso(),
        "commands": [],
        "queries_attempted": [],
        "warnings": [],
    }
    existing_pool = read_json_loose(workspace / "raw_pool.json", {})
    verified: dict[str, dict[str, Any]] = {}
    if isinstance(existing_pool, dict):
        for paper in existing_pool.get("papers", []):
            if not isinstance(paper, dict):
                continue
            if paper.get("cutoff_violation"):
                continue
            if not paper.get("title") or not paper.get("year"):
                continue
            if not str(paper.get("abstract") or "").strip():
                continue
            verified[paper_identity(paper)] = paper

    queries = candidate_queries_from_outline(workspace)
    attempted = 0
    rate_limited = False
    for query in queries:
        if len(verified) >= min_references or attempted >= max_s2_queries:
            break
        rows, result = search_s2(query, scripts)
        attempted += 1
        phase["commands"].append(result)
        phase["queries_attempted"].append(query)
        if result["return_code"] != 0:
            if "rate-limited" in (result.get("stderr_tail", "") + result.get("stdout_tail", "")).lower() or "429" in (result.get("stderr_tail", "") + result.get("stdout_tail", "")):
                rate_limited = True
                phase["warnings"].append("Semantic Scholar rate limit hit; stopping live verification and leaving literature phase blocked.")
                break
            continue
        best: dict[str, Any] | None = None
        best_ratio = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            ratio = title_ratio(query, str(row.get("title") or ""))
            if ratio > best_ratio:
                best_ratio = ratio
                best = row
        if not best or best_ratio < 70:
            continue
        year = best.get("year")
        if not isinstance(year, int) or year > 2026:
            continue
        if not str(best.get("abstract") or "").strip():
            continue
        best["orig_title"] = query
        best["levenshtein_ratio"] = best_ratio
        best["verified"] = True
        best["cutoff_violation"] = False
        verified[paper_identity(best)] = best

    papers = sorted(verified.values(), key=lambda row: (-(int(row.get("citationCount") or 0)), int(row.get("year") or 9999), str(row.get("title") or "")))
    pool = {
        "papers": papers,
        "min_cite_paper_count": max(1, int(len(papers) * 0.9)),
        "n_total": len(papers),
        "cutoff_date": "2026-04-17",
        "verification_mode": "writing deterministic verification with research state-machine control",
        "rate_limited": rate_limited,
        "generated_at": now_iso(),
    }
    write_json(workspace / "citation_pool.json", pool)
    write_json(workspace / "raw_pool.json", pool)
    validate = run([sys.executable, str(scripts["validate_pool"]), "--pool", str(workspace / "citation_pool.json"), "--fix"], cwd=ROOT, required=False)
    bib = run([sys.executable, str(scripts["bibtex_format"]), "--pool", str(workspace / "citation_pool.json"), "--out", str(workspace / "refs.bib")], cwd=ROOT, required=False)
    phase["commands"].extend([validate, bib])
    phase["verified_count"] = len(papers)
    phase["bib_entry_count"] = count_bib_entries(workspace / "refs.bib")
    phase["status"] = "pass" if phase["bib_entry_count"] >= min_references else "blocked"
    if phase["status"] != "pass":
        phase["warnings"].append(f"Only {phase['bib_entry_count']} verified references available; reference_target={min_references}.")
    phase["finished_at"] = now_iso()
    write_phase_log(workspace, "literature_verification", phase)
    return phase


def preflight(repo_dir: Path, workspace: Path) -> tuple[list[dict[str, Any]], list[str]]:
    scripts = repo_dir / "skills" / "paper-orchestra" / "scripts"
    commands: list[dict[str, Any]] = []
    warnings: list[str] = []
    checks = [
        [sys.executable, str(scripts / "validate_inputs.py"), "--workspace", str(workspace)],
        [sys.executable, str(scripts / "check_idea_density.py"), "--idea", str(workspace / "inputs" / "idea.md"), "--log", str(workspace / "inputs" / "experimental_log.md")],
        [sys.executable, str(scripts / "validate_consistency.py"), "--idea", str(workspace / "inputs" / "idea.md"), "--log", str(workspace / "inputs" / "experimental_log.md")],
    ]
    for cmd in checks:
        if not Path(cmd[1]).exists():
            warnings.append(f"missing preflight helper: {cmd[1]}")
            continue
        result = run(cmd, cwd=ROOT, required=False)
        commands.append(result)
        if result["return_code"] not in {0, 1}:
            warnings.append(f"preflight command failed: {' '.join(cmd)}")
    tex_check = scripts / "check_tex_packages.py"
    if tex_check.exists():
        result = run([sys.executable, str(tex_check), "--out", str(workspace / "tex_profile.json")], cwd=ROOT, required=False)
        commands.append(result)
        if result["return_code"] != 0:
            warnings.append("TeX package probe failed; section writing must use conservative LaTeX patterns.")
    return commands, warnings


def claude_prompt(project: str, venue: str, title: str, workspace: Path, repo_dir: Path, phase: str = "all", force_refresh: bool = False) -> str:
    status = workspace_status(workspace)
    template_profile = venue_template_profile(venue, project=project)
    venue_requirements = load_venue_requirements(venue, project=project)
    venue_policy = venue_requirements.get("venue_submission_policy", {}) if isinstance(venue_requirements, dict) else {}
    citation_policy = venue_requirements.get("citation_policy", {}) if isinstance(venue_requirements, dict) else {}
    template_validation = validate_venue_template_format(read_text(workspace / "inputs" / "template.tex"), venue, project=project)
    phase_instruction = {
        "outline": "Execute only writing Step 1: produce and validate workspace/outline.json.",
        "plotting": "Execute only writing Step 2: produce necessary figure PNGs and workspace/figures/captions.json. Use only figures backed by manuscript-safe evidence; do not create blocker, failed-run, or future-work visuals as main paper figures.",
        "literature": "Execute only writing Step 3: produce citation_pool.json, refs.bib, and drafts/intro_relwork.tex from verified citations only.",
        "section-writing": "Execute only writing Step 4: use existing intro_relwork.tex, refs.bib, citation_pool.json, figures, captions, and manuscript-safe metrics to produce a full target-venue-style drafts/paper.tex, not a short method memo. Follow the resolved venue-specific manuscript shape, including Nature-family article shape when applicable, while avoiding fabricated superiority claims and avoiding planned/future/status language.",
        "refinement": "Execute only writing Step 5: refine drafts/paper.tex into a polished final/paper.tex that reads like a serious venue submission preview. Strengthen narrative, math, algorithm box if useful, venue-appropriate results/protocol prose, tables, captions, and citation coverage while preserving the target venue template. Do not replace the manuscript with a revision-status, future-work, limitations, success-criteria, or gate report.",
        "compile": "Compile final/paper.tex into final/paper.pdf and write provenance.json.",
        "all": "Run the missing writing steps until final/paper.tex and final/paper.pdf exist.",
    }.get(phase, "Run the missing writing steps until final/paper.tex and final/paper.pdf exist.")
    writing_contract = read_text(WRITING_CONTRACT)[:16000] or "Read modules/writing/SKILL.md before writing."
    return f"""
Run the writing module on this TASTE-prepared workspace.

Workspace: {workspace}
Read-only source-method reference directory: {repo_dir}
Read-only TASTE academic writing skills reference directory: {WRITING_ACADEMIC_SKILLS_DIR}
Read-only TASTE Nature-family writing reference directory: {WRITING_NATURE_REFERENCE_DIR}
Project: {project}
Target venue: {venue}
Title hint: {title}
TASTE-controlled writing phase for this call: {phase}
Focused objective: {phase_instruction}
Current paper-preview regeneration requested by TASTE user/UI (this means rebuild the venue-formatted manuscript preview from current TASTE evidence; it is not a claim-change directive and not an instruction to intervene in the underlying research project): {force_refresh}

writing module contract:
```markdown
{writing_contract}
```

Current deterministic workspace status:
```json
{json.dumps(status, ensure_ascii=False, indent=2)}
```

Venue template profile:
```json
{json.dumps(template_profile, ensure_ascii=False, indent=2)}
```

Resolved current venue requirements from venue-intelligence:
```json
{json.dumps({
    "status": venue_requirements.get("status") if isinstance(venue_requirements, dict) else "missing",
    "venue": venue_requirements.get("venue") if isinstance(venue_requirements, dict) else venue,
    "source_checked_at": venue_requirements.get("source_checked_at") if isinstance(venue_requirements, dict) else "",
    "official_source_count": len(venue_requirements.get("official_sources", [])) if isinstance(venue_requirements, dict) and isinstance(venue_requirements.get("official_sources"), list) else 0,
    "body_page_max": venue_policy.get("body_page_max") if isinstance(venue_policy, dict) else "",
    "reference_page_max": venue_policy.get("reference_page_max") if isinstance(venue_policy, dict) else "",
    "total_page_max": venue_policy.get("total_page_max") if isinstance(venue_policy, dict) else "",
    "official_min_references": venue_policy.get("official_min_references") if isinstance(venue_policy, dict) else 0,
    "reference_quality_target": venue_policy.get("reference_quality_target") if isinstance(venue_policy, dict) else citation_policy.get("quality_target_min_verified_references", 0),
    "reference_target_source": venue_policy.get("reference_target_source") if isinstance(venue_policy, dict) else "",
}, ensure_ascii=False, indent=2)}
```

Prepared template validation:
```json
{json.dumps(template_validation, ensure_ascii=False, indent=2)}
```

Hard requirements:
- Generate a real venue-formatted manuscript, not a gate report, status report, audit report, or project-management note.
- Use source-method references only as read-only implementation guidance. Public research logs, generated paper text, and returned summaries must call this the writing module.
- If the resolved venue/template profile has `family=springer-nature` or `nature_family_article_mode=true`, use TASTE Nature-family article mode: broad-reader framing, compact unstructured summary-style abstract when appropriate, Results/Discussion/Methods shape when the contract allows it, Data availability, Code availability, calibrated Nature-style claim verbs, and Nature Portfolio `sn-nature` template option for TASTE LaTeX/PDF preview.
- For Nature-family preview, read only the relevant fragments from the TASTE Nature-family writing reference directory when needed: writing core/manifest, journal Nature fragment, research/algorithmic paper-type fragment, abstract/introduction/method/experiments fragments, figure/citation/data availability guidance. Do not expose the source repository or skill names in paper text, UI text, or public logs.
- If Nature-family journal-specific instructions were inaccessible or only partially parsed, keep the manuscript preview useful but do not mark submission-ready; record the unresolved journal-specific compliance item in provenance/audit files, not in `paper.tex`.
- Do not use any prior assistant prose as scientific content.
- Do not fabricate metrics, citations, datasets, code paths, or positive results.
- Do not write a historical-route paper unless that route is the current selected base and has current-route claim support.
- Do not foreground failed hypotheses, negative experiment outcomes, blocker diagnostics, unsupported-claim lists, or legacy-route stories in the abstract/introduction/results as a contribution; use them only to decide which unsupported claims to omit.
- Read `workspace/inputs/idea.md`, `experimental_log.md`, `template.tex`, `conference_guidelines.md`, `method_contract.md`, and the resolved venue requirements under `projects/{project}/paper/venues/{slugify(venue)}/venue_requirements.json` as the only content input tuple.
- Treat `method_contract.md` as workflow law, not as paper prose.
- Treat `workspace/inputs/template.tex` as binding venue format. The final `workspace/final/paper.tex` must preserve the venue `\\documentclass`, required options, margins, fonts, and bibliography style from the template.
- If the resolved venue policy requires a specific `\\documentclass` or options, final TeX must preserve them exactly; do not replace the official template with a generic local template.
- Treat venue requirements as dynamic: rely on `venue_requirements.json` produced by venue-intelligence from current official sources, not on hard-coded assumptions about any single conference. Re-read that file before writing if the target venue changes.
- Figure quality is a hard preview gate. Every main-text figure must have a reproducible script, legible typography, concise caption, and claim-ready evidence. Do not disguise weak synthetic/probe outputs as polished results.
- If evidence is insufficient for positive empirical claims, still produce the best possible venue-formatted manuscript preview. Foreground the project-specific innovation thesis, mathematical formulation, algorithm, reproducibility details, verified reference calibration, and scientific plausibility derived from the prepared writing inputs.
- The manuscript should be complete in resolved venue-specific paper shape even when evidence is still incomplete: {manuscript_shape_requirement(venue, project=project)} Include references, at least one dataset/protocol table when supported, a reference-calibration table, and method/protocol figures when available. When figure footprint is the measured layout issue, repair floats/graphics before shortening manuscript substance.
- Venue-appropriate Results/Experiments prose may report verified reference calibration and describe current implementation/protocol details, but must not invent completed proposed-method numbers or superiority claims. Do not include negative/failed runs as the main result story, and do not label any section as planned work, study design, or ablation design.
- Keep unsupported, negative, failed, and legacy routes out of the manuscript body; record them only in provenance/audit files and do not pretend the paper is submission-ready.
- `workspace/final/paper.tex` must be manuscript content only. It must not contain headings such as Revision Status, Submission Blockers, Paper Blockers, Required Revision Actions, Evidence Snapshot, Section Ledger, Writing Blockers, or Next Actions.
- `workspace/final/paper.tex` must not contain visible sections or paragraphs titled Limitations, Future Work, Planned Study, Planned Ablation Study, Success Criteria, Inspection Draft, Failure, or Counterexample.
- Do not write internal state vocabulary into the manuscript, including inspection draft, candidate_observation_only, blocked, hold-markdown-only, claim promotion, audit-ready, unsupported claims, no empirical superiority claims, future empirical validation, or planned ablation.
- Do not include acknowledgments or any sentence that mentions TASTE, writing, automated research, project agents, source-method modules, anonymous reviewers, or manuscript-generation tooling.
- For anonymous Nature-family/Springer Nature previews, use a single anonymous author block only; do not add corresponding-author stars, numeric affiliation labels, emails, or placeholder affiliation text such as Department/Institution/City/Country.
- If a caveat is necessary, phrase it neutrally inside method/protocol prose rather than as a weakness list.
- If an ablation is not completed, mention only compact evaluation axes in prose; do not create an Ablation Study Design section and do not present it as a result table.
- If you can complete the pipeline, produce `workspace/final/paper.tex`, `workspace/final/paper.pdf`, `workspace/refs.bib`, `workspace/citation_pool.json`, and `workspace/provenance.json`.
- Before reporting success, ensure every `\cite...{{...}}` key in `paper.tex` resolves to `refs.bib`; the compiled PDF must not contain undefined citation markers such as `?` or `??`.
- Continue from actual files on disk, not from memory of prior runs.
- If current paper-preview regeneration is requested, write a new draft/final paper artifact from current TASTE evidence. Do not report success by reusing unchanged TeX/PDF.
- Treat page-fit as a diagnosis task: inspect venue page accounting, figure/table footprint, bibliography/reference-page footprint, and only then adjust prose if those are not the actual source. If body pages are within the official limit, do not describe or execute the task as prose shortening.
- When body pages are within the official limit but total pages look high, prioritize figure/table footprint, reference coverage, bibliography density, and venue-template details. Body-page compliance means the writing task is quality/layout/citation repair from current artifacts, not a request to reduce scientific content.
- Do not return "in progress" as success. This call succeeds only if final TeX/PDF and provenance exist, or if you write a concrete internal blocker report outside `paper.tex` with exact missing files.
- After writing, run writing validation, orphan citation checks, LaTeX sanity, venue template audit, figure quality audit, and PDF build if possible.

Return concise Markdown with:
Conclusion, Writing Steps Completed, Generated Files, Validation Notes, Evidence/Citation Boundaries, and Next Actions.
""".strip()


def invoke_claude(project: str, venue: str, title: str, workspace: Path, repo_dir: Path, timeout_sec: int, resume: bool, phase: str = "all", force_refresh: bool = False) -> dict[str, Any]:
    prompt = claude_prompt(project, venue, title, workspace, repo_dir, phase=phase, force_refresh=force_refresh)
    prompt_path = workspace / f"writing_prompt_{phase}.md"
    write_text(prompt_path, prompt)
    cmd = [
        sys.executable,
        str(ROOT / "framework" / "scripts" / "claude_project_session.py"),
        "--project",
        project,
        "--stage",
        f"writing:{phase}",
        "--message-file",
        str(prompt_path),
        "--timeout-sec",
        str(timeout_sec),
        "--agent-id",
        f"writing_{phase.replace('-', '_')}",
    ]
    if not resume:
        cmd.append("--no-resume")
    outer_timeout = None if timeout_sec <= 0 else max(timeout_sec + 900, 1800)
    result = run(cmd, cwd=ROOT, timeout=outer_timeout, required=False)
    result["prompt_path"] = str(prompt_path)
    return result


def collect_outputs(project: str, venue: str, workspace: Path) -> dict[str, Any]:
    paper = ensure_paper_dirs(project)
    venue_slug = slugify(venue)
    output_dir = paper["output_dir"] / venue_slug
    output_dir.mkdir(parents=True, exist_ok=True)
    final_tex = workspace / "final" / "paper.tex"
    final_pdf = workspace / "final" / "paper.pdf"
    raw_tex = output_dir / "writing_raw.tex"
    raw_pdf = output_dir / "writing_raw.pdf"
    raw_bib = output_dir / "writing_raw.bib"
    copied: dict[str, str] = {}
    if final_tex.exists():
        shutil.copy2(final_tex, raw_tex)
        copied["raw_tex"] = str(raw_tex)
    if final_pdf.exists():
        shutil.copy2(final_pdf, raw_pdf)
        copied["raw_pdf"] = str(raw_pdf)
    if (workspace / "refs.bib").exists():
        shutil.copy2(workspace / "refs.bib", raw_bib)
        copied["raw_bib"] = str(raw_bib)
    provenance = {
        "workspace": str(workspace),
        "final_tex": sha256_file(final_tex),
        "final_pdf": sha256_file(final_pdf),
        "refs_bib": sha256_file(workspace / "refs.bib"),
        "citation_pool": sha256_file(workspace / "citation_pool.json"),
        "copied": copied,
    }
    write_json(workspace / "project_bridge_provenance.json", provenance)
    return provenance


def run_phase_with_claude(
    project: str,
    venue: str,
    title: str,
    workspace: Path,
    repo_dir: Path,
    phase: str,
    *,
    timeout_sec: int,
    resume: bool,
    min_references: int,
    force_refresh: bool = False,
) -> dict[str, Any]:
    before = workspace_status(workspace)
    ready_before, missing_before = phase_ready(workspace, phase, min_references=min_references, venue=venue, project=project)
    if phase == "refinement" and "final_venue_template_format" in missing_before:
        # Force writing to regenerate instead of treating an
        # existing generic article TeX as a valid final manuscript.
        backup_dir = workspace / "format_blocked_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for name in ["paper.tex", "paper.pdf", "refs.bib"]:
            source = workspace / "final" / name
            if source.exists():
                shutil.copy2(source, backup_dir / f"{stamp}.final.{name}")
                source.unlink()
        ready_before, missing_before = phase_ready(workspace, phase, min_references=min_references, venue=venue, project=project)
    payload: dict[str, Any] = {
        "phase": phase,
        "started_at": now_iso(),
        "ready_before": ready_before,
        "missing_before": missing_before,
        "before": before,
    }
    can_skip_ready = ready_before and not (force_refresh and phase in {"section-writing", "refinement"})
    if can_skip_ready:
        payload.update({"status": "skipped-ready", "finished_at": now_iso(), "after": before, "missing_after": []})
        write_phase_log(workspace, phase, payload)
        return payload
    if ready_before and force_refresh and phase in {"section-writing", "refinement"}:
        missing_before = [*missing_before, "current_paper_regeneration_requires_new_paper_artifact"]
        payload["current_paper_regeneration_despite_ready"] = True
    claude = invoke_claude(project, venue, title, workspace, repo_dir, timeout_sec, resume=resume, phase=phase, force_refresh=force_refresh)
    after = workspace_status(workspace)
    ready_after, missing_after = phase_ready(workspace, phase, min_references=min_references, venue=venue, project=project)
    stdout = str(claude.get("stdout_tail") or claude.get("stdout") or "")
    stale_success = bool(re.search(r"\b(still running|in progress|queued|waiting for|will proceed)\b", stdout, flags=re.IGNORECASE))
    status = "pass" if ready_after and not stale_success else "blocked"
    payload.update({
        "claude": claude,
        "after": after,
        "ready_after": ready_after,
        "missing_after": missing_after,
        "stale_success_text_detected": stale_success,
        "status": status,
        "finished_at": now_iso(),
    })
    write_phase_log(workspace, phase, payload)
    return payload


def compile_final_pdf(project: str, workspace: Path, repo_dir: Path, *, venue: str = "", timeout_sec: int = 600) -> dict[str, Any]:
    scripts = paper_orchestra_scripts(repo_dir)
    final_dir = workspace / "final"
    final_dir.mkdir(parents=True, exist_ok=True)
    final_tex = final_dir / "paper.tex"
    payload: dict[str, Any] = {"phase": "compile", "started_at": now_iso(), "commands": [], "warnings": []}
    if not final_tex.exists() and (workspace / "drafts" / "paper.tex").exists():
        shutil.copy2(workspace / "drafts" / "paper.tex", final_tex)
        payload["warnings"].append("final/paper.tex was missing; copied drafts/paper.tex for compilation.")
    if not final_tex.exists():
        payload.update({"status": "blocked", "missing": ["final_tex"], "finished_at": now_iso()})
        write_phase_log(workspace, "compile", payload)
        return payload
    text = read_text(final_tex)
    normalized_text, front_matter_changes = normalize_venue_front_matter(text, venue, project=project)
    if front_matter_changes:
        text = normalized_text
        write_text(final_tex, text)
        payload["front_matter_normalization"] = front_matter_changes
        stale_pdf = final_dir / "paper.pdf"
        if stale_pdf.exists():
            stale_pdf.unlink()
            payload["stale_pdf_removed_before_compile"] = True
    cleaned_lines = []
    removed_private_comments = 0
    private_comment_markers = (
        "inspection draft",
        "candidate_observation_only",
        "unsupported",
        "blocked",
        "hold-markdown",
        "claim promotion",
        "gate",
        "Writing Module",
        "No empirical superiority claims",
        "reference calibration only",
    )
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("%") and any(marker.lower() in stripped.lower() for marker in private_comment_markers):
            removed_private_comments += 1
            continue
        cleaned_lines.append(line)
    if removed_private_comments:
        text = "\n".join(cleaned_lines) + "\n"
        write_text(final_tex, text)
    if "hyperref" in text and "\\hypersetup{hidelinks}" not in text:
        if "\\begin{document}" in text:
            text = text.replace("\\begin{document}", "\\hypersetup{hidelinks}\n\\begin{document}", 1)
            payload["hyperlink_style"] = "hidelinks"
            write_text(final_tex, text)
    requirements = load_venue_requirements(venue) if venue else {}
    template_req = requirements.get("template", {}) if isinstance(requirements, dict) and isinstance(requirements.get("template"), dict) else {}
    bib_style = str(template_req.get("bibliography_style") or "").strip()
    if bib_style.endswith(".bst"):
        bib_style = bib_style[:-4]
    if bib_style and "\\bibliographystyle" in text:
        text = re.sub(r"\\bibliographystyle\{[^{}]+\}", rf"\\bibliographystyle{{{bib_style}}}", text)
        payload["bibliography_style"] = bib_style
        write_text(final_tex, text)
    if "\\bibliography{" in text and "refs}" not in text and "{refs" not in text:
        text = re.sub(r"\\bibliography\{[^{}]+\}", r"\\bibliography{refs}", text)
        write_text(final_tex, text)
    refs = workspace / "refs.bib"
    if refs.exists() and not (final_dir / "refs.bib").exists():
        shutil.copy2(refs, final_dir / "refs.bib")
    figures_src = workspace / "figures"
    figures_dst = final_dir / "figures"
    if figures_src.exists() and not figures_dst.exists():
        shutil.copytree(figures_src, figures_dst)
    copied_sidecars = copy_workspace_template_sidecars_to_final(workspace, final_dir)
    if copied_sidecars:
        payload["template_sidecars"] = copied_sidecars
    latexmk = shutil.which("latexmk") or workspace_tool_path("latexmk")
    if latexmk and Path(latexmk).exists():
        payload["commands"].append(run([latexmk, "-pdf", "-interaction=nonstopmode", "paper.tex"], cwd=final_dir, timeout=timeout_sec, required=False))
    else:
        payload["warnings"].append("latexmk not found.")
    if not (final_dir / "paper.pdf").exists():
        pdflatex = shutil.which("pdflatex") or workspace_tool_path("pdflatex")
        if pdflatex and Path(pdflatex).exists():
            for _ in range(2):
                payload["commands"].append(run([pdflatex, "-interaction=nonstopmode", "paper.tex"], cwd=final_dir, timeout=timeout_sec, required=False))
        else:
            payload["warnings"].append("pdflatex not found.")
    if not (workspace / "provenance.json").exists():
        provenance = {
            "generated_at": now_iso(),
            "inputs": {
                "idea": sha256_file(workspace / "inputs" / "idea.md"),
                "experimental_log": sha256_file(workspace / "inputs" / "experimental_log.md"),
                "template": sha256_file(workspace / "inputs" / "template.tex"),
                "guidelines": sha256_file(workspace / "inputs" / "conference_guidelines.md"),
                "method_contract": sha256_file(workspace / "inputs" / "method_contract.md"),
            },
            "outline": sha256_file(workspace / "outline.json"),
            "refs": sha256_file(workspace / "refs.bib"),
            "final_tex": sha256_file(final_tex),
            "final_pdf": sha256_file(final_dir / "paper.pdf"),
        }
        write_json(workspace / "provenance.json", provenance)
    venue_validation = validate_venue_template_format(read_text(final_tex), venue, project=project) if venue else {"status": "pass"}
    payload["venue_template_validation"] = venue_validation
    payload["status"] = "pass" if (final_dir / "paper.pdf").exists() and venue_validation.get("status") == "pass" else "blocked"
    payload["workspace_status"] = workspace_status(workspace)
    payload["finished_at"] = now_iso()
    write_phase_log(workspace, "compile", payload)
    return payload


def run_deterministic_gates(project: str, venue: str, workspace: Path, repo_dir: Path, *, min_references: int) -> dict[str, Any]:
    scripts = paper_orchestra_scripts(repo_dir)
    draft = workspace / "drafts" / "paper.tex"
    final_tex = workspace / "final" / "paper.tex"
    tex_for_gates = final_tex if final_tex.exists() else draft
    payload: dict[str, Any] = {"phase": "audit", "started_at": now_iso(), "commands": [], "warnings": []}
    commands: list[list[str]] = []
    if scripts["validate_inputs"].exists():
        commands.append([sys.executable, str(scripts["validate_inputs"]), "--workspace", str(workspace)])
    if scripts["validate_outline"].exists() and (workspace / "outline.json").exists():
        commands.append([sys.executable, str(scripts["validate_outline"]), str(workspace / "outline.json")])
    if tex_for_gates.exists() and (workspace / "refs.bib").exists():
        commands.extend([
            [sys.executable, str(scripts["orphan_cite_gate"]), str(tex_for_gates), str(workspace / "refs.bib")],
            [sys.executable, str(scripts["latex_sanity"]), str(tex_for_gates)],
            [sys.executable, str(scripts["anti_leakage_check"]), str(tex_for_gates)],
        ])
    for cmd in commands:
        payload["commands"].append(run(cmd, cwd=ROOT, required=False))
    outputs = collect_outputs(project, venue, workspace)
    payload["outputs"] = outputs
    payload["workspace_status"] = workspace_status(workspace)
    payload["min_references"] = min_references
    venue_validation = validate_venue_template_format(read_text(tex_for_gates), venue, project=project) if tex_for_gates.exists() else {"status": "block", "failures": ["missing final/draft TeX"]}
    payload["venue_template_validation"] = venue_validation
    figure_audit_cmd = [sys.executable, str(ROOT / "modules" / "writing" / "scripts" / "audit_paper_figures.py"), "--project", project, "--venue", venue]
    figure_audit = run(figure_audit_cmd, cwd=ROOT, required=False)
    payload["commands"].append(figure_audit)
    figure_state = get_active_paper_state(project, venue=venue)
    payload["figure_quality"] = {
        "status": figure_state.get("paper_figure_quality_status", ""),
        "ready": bool(figure_state.get("paper_figure_quality_ready")),
        "report": figure_state.get("paper_figure_quality_report", ""),
        "blocked": figure_state.get("paper_figure_blocker_count", ""),
    }
    payload["status"] = (
        "pass"
        if outputs.get("final_pdf", {}).get("exists")
        and outputs.get("final_tex", {}).get("exists")
        and venue_validation.get("status") == "pass"
        and payload["figure_quality"]["ready"]
        else "blocked"
    )
    payload["finished_at"] = now_iso()
    write_phase_log(workspace, "audit", payload)
    return payload


def sync_preview(project: str, venue: str, title: str, workspace: Path, *, force_refresh: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {"phase": "sync-preview", "started_at": now_iso(), "commands": []}
    workspace_artifacts = workspace_final_artifacts(workspace)
    payload["workspace_final_artifacts"] = workspace_artifacts
    if force_refresh and not workspace_artifacts.get("ready"):
        payload["status"] = "blocked"
        payload["missing"] = workspace_artifacts.get("missing", [])
        payload["reason"] = "current_paper_regeneration_requires_current_workspace_final_artifacts"
        payload["paper_state"] = {
            "conference_preview_ready": False,
            "normal_preview_ready": False,
            "pdf_path": "",
            "conference_preview_pdf": "",
            "paper_normality_status": "blocked",
            "paper_normality_pages": "",
            "paper_normality_citation_count": "",
            "paper_figure_quality_status": "",
            "paper_figure_quality_ready": False,
            "paper_figure_blocker_count": "",
        }
        payload["workspace_status"] = workspace_status(workspace)
        payload["finished_at"] = now_iso()
        write_phase_log(workspace, "sync-preview", payload)
        return payload
    cmd = [sys.executable, str(ROOT / "modules" / "writing" / "scripts" / "build_conference_preview_paper.py"), "--project", project, "--venue", venue, "--title", title]
    result = run(cmd, cwd=ROOT, required=False)
    payload["commands"].append(result)
    state = get_active_paper_state(project, venue=venue)
    workspace_artifacts = workspace_final_artifacts(workspace)
    payload["workspace_final_artifacts"] = workspace_artifacts
    payload["paper_state"] = {
        "conference_preview_ready": bool(state.get("conference_preview_ready")),
        "normal_preview_ready": bool(state.get("normal_preview_ready") or state.get("paper_normality_ready")),
        "pdf_path": state.get("pdf_path", ""),
        "conference_preview_pdf": state.get("conference_preview_pdf", ""),
        "paper_normality_status": state.get("paper_normality_status", ""),
        "paper_normality_pages": state.get("paper_normality_pages", ""),
        "paper_normality_citation_count": state.get("paper_normality_citation_count", ""),
        "paper_figure_quality_status": state.get("paper_figure_quality_status", ""),
        "paper_figure_quality_ready": bool(state.get("paper_figure_quality_ready")),
        "paper_figure_blocker_count": state.get("paper_figure_blocker_count", ""),
    }
    missing: list[str] = []
    if not workspace_artifacts.get("ready"):
        missing.extend(workspace_artifacts.get("missing", []))
    if result.get("return_code") != 0:
        missing.append("conference_preview_build_failed")
    if not payload["paper_state"]["conference_preview_ready"]:
        missing.append("conference_preview_not_ready")
    if not payload["paper_state"]["normal_preview_ready"]:
        missing.append("normal_preview_not_ready")
    if not payload["paper_state"]["paper_figure_quality_ready"]:
        missing.append("paper_figure_quality_not_ready")
    payload["missing"] = missing
    payload["status"] = "pass" if not missing else "blocked"
    payload["workspace_status"] = workspace_status(workspace)
    payload["finished_at"] = now_iso()
    write_phase_log(workspace, "sync-preview", payload)
    return payload


def run_phase_machine(
    project: str,
    venue: str,
    title: str,
    workspace: Path,
    repo_dir: Path,
    *,
    timeout_sec: int,
    resume: bool,
    min_references: int,
    max_s2_queries: int,
    force_refresh: bool = False,
) -> dict[str, Any]:
    phases: list[dict[str, Any]] = []
    scripts = paper_orchestra_scripts(repo_dir)
    if scripts["validate_outline"].exists() and (workspace / "outline.json").exists():
        outline_result = run([sys.executable, str(scripts["validate_outline"]), str(workspace / "outline.json")], cwd=ROOT, required=False)
        outline_ok, outline_missing = phase_ready(workspace, "outline", min_references=min_references, venue=venue, project=project)
        outline_status = "pass" if outline_ok and outline_result["return_code"] in {0, 2} else "blocked"
        phases.append({
            "phase": "outline",
            "status": outline_status,
            "commands": [outline_result],
            "warnings": ["validate_outline.py could not run because jsonschema is missing; outline file existence was used as fallback."] if outline_result["return_code"] == 2 else [],
            "missing": outline_missing,
            "workspace_status": workspace_status(workspace),
        })
    else:
        phases.append(run_phase_with_claude(project, venue, title, workspace, repo_dir, "outline", timeout_sec=timeout_sec, resume=resume, min_references=min_references, force_refresh=force_refresh))

    phases.append(run_phase_with_claude(project, venue, title, workspace, repo_dir, "plotting", timeout_sec=timeout_sec, resume=resume, min_references=min_references, force_refresh=force_refresh))

    lit_ready, lit_missing = phase_ready(workspace, "literature", min_references=min_references, venue=venue, project=project)
    if not lit_ready:
        lit_verify = build_verified_literature_pool(workspace, repo_dir, min_references=min_references, max_s2_queries=max_s2_queries)
        phases.append(lit_verify)
    lit_ready, lit_missing = phase_ready(workspace, "literature", min_references=min_references, venue=venue, project=project)
    if not lit_ready:
        phases.append(run_phase_with_claude(project, venue, title, workspace, repo_dir, "literature", timeout_sec=timeout_sec, resume=False, min_references=min_references, force_refresh=force_refresh))

    phases.append(run_phase_with_claude(project, venue, title, workspace, repo_dir, "section-writing", timeout_sec=timeout_sec, resume=False, min_references=min_references, force_refresh=force_refresh))
    phases.append(run_phase_with_claude(project, venue, title, workspace, repo_dir, "refinement", timeout_sec=timeout_sec, resume=False, min_references=min_references, force_refresh=force_refresh))
    compile_phase = compile_final_pdf(project, workspace, repo_dir, venue=venue)
    phases.append(compile_phase)
    audit_phase = run_deterministic_gates(project, venue, workspace, repo_dir, min_references=min_references)
    phases.append(audit_phase)
    preview_phase = sync_preview(project, venue, title, workspace, force_refresh=force_refresh)
    phases.append(preview_phase)
    workspace_artifacts = workspace_final_artifacts(workspace)
    status = (
        "generated"
        if compile_phase.get("status") == "pass"
        and audit_phase.get("status") == "pass"
        and preview_phase.get("status") == "pass"
        and workspace_artifacts.get("ready")
        else "blocked"
    )
    return {
        "status": status,
        "phases": phases,
        "workspace_status": workspace_status(workspace),
        "current_paper_regeneration_requested": force_refresh,
        "workspace_final_artifacts": workspace_artifacts,
    }


def write_report(paths, payload: dict[str, Any]) -> Path:
    report = paths.reports / "paper_orchestra_bridge.md"
    phases = payload.get("phases", [])
    if not isinstance(phases, list):
        phases = []
    workspace_status_payload = payload.get("workspace_status", {}) if isinstance(payload.get("workspace_status", {}), dict) else {}
    lines = [
        "# Writing Bridge\n\n",
        f"- status: {payload.get('status')}\n",
        f"- project: {payload.get('project')}\n",
        f"- venue: {payload.get('venue')}\n",
        f"- source_module_path: {payload.get('repo', {}).get('repo_path', '')}\n",
        f"- source_module_commit: {payload.get('repo', {}).get('commit', '')}\n",
        f"- workspace: {payload.get('workspace', '')}\n",
        f"- final_pdf_exists: {workspace_status_payload.get('files', {}).get('final_pdf', {}).get('exists', False)}\n",
        f"- final_tex_exists: {workspace_status_payload.get('files', {}).get('final_tex', {}).get('exists', False)}\n",
        f"- bib_entry_count: {workspace_status_payload.get('bib_entry_count', 0)}\n",
        f"- citation_pool_count: {workspace_status_payload.get('citation_pool_count', 0)}\n",
        "\n## Warnings\n\n",
    ]
    warnings = payload.get("warnings", [])
    if warnings:
        for item in warnings:
            lines.append(f"- {item}\n")
    else:
        lines.append("- none\n")
    lines.append("\n## Phase Status\n\n")
    if phases:
        for phase in phases:
            if not isinstance(phase, dict):
                continue
            missing = phase.get("missing_after") or phase.get("missing") or phase.get("warnings") or []
            lines.append(f"- {phase.get('phase')}: {phase.get('status')} ({missing if missing else 'ok'})\n")
    else:
        lines.append("- no phases recorded\n")
    lines.append("\n## Last Claude Tail\n\n```text\n")
    last_claude = ""
    for phase in reversed(phases):
        if isinstance(phase, dict) and isinstance(phase.get("claude"), dict):
            last_claude = str(phase["claude"].get("stdout_tail") or phase["claude"].get("stderr_tail") or "")
            break
    lines.append(last_claude[-6000:])
    lines.append("\n```\n")
    write_text(report, "".join(lines))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Bridge TASTE paper state into the writing module workflow.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--venue", required=True)
    parser.add_argument("--title", default="")
    parser.add_argument("--repo-dir", default=str(DEFAULT_PAPER_ORCHESTRA_DIR))
    parser.add_argument("--timeout-sec", type=int, default=int(os.environ.get("PAPER_ORCHESTRA_TIMEOUT_SEC", "14400")))
    parser.add_argument("--skip-clone", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--resume", action="store_true", help="Resume the prior Claude session. Default starts a fresh writing call to avoid stale sub-agent waits.")
    parser.add_argument("--no-force-workspace", action="store_true", help="Keep existing writing intermediate files instead of overlaying regenerated TASTE inputs.")
    parser.add_argument("--refresh-current-paper", dest="force_refresh", action="store_true", help="Back up and remove existing draft/final paper outputs so writing regenerates the current venue-formatted paper preview from current evidence instead of reusing an old PDF.")
    parser.add_argument("--force-refresh", dest="force_refresh", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--min-references", type=int, default=0, help="Optional explicit reference target; default reads current venue_requirements.json.")
    parser.add_argument("--max-s2-queries", type=int, default=int(os.environ.get("PAPER_MAX_S2_QUERIES", "120")))
    args = parser.parse_args()
    os.environ["PROJECT_ID"] = args.project

    guard_rc = guard_fresh_base_blocker_entry(args.project, args.venue, Path(__file__).name, safe_unblock=False)
    if guard_rc is not None:
        return int(guard_rc)

    paths = build_paths(args.project)
    ensure_paper_dirs(args.project)
    title = args.title or str(get_active_paper_state(args.project, args.venue).get("title") or load_project_config(args.project).get("topic") or args.project)
    repo_dir = Path(args.repo_dir).resolve()
    reference_target_info = venue_reference_target(args.venue, project=args.project, explicit_min=args.min_references)
    args.min_references = int(reference_target_info.get("target") or 0)

    payload: dict[str, Any] = {
        "project": args.project,
        "venue": args.venue,
        "title": title,
        "started_at": now_iso(),
        "reference_target": reference_target_info,
        "source": {
            "repository": PAPER_ORCHESTRA_URL,
            "principle": "writing is used as the manuscript module. TASTE prepares inputs and Claude Code executes the writing instructions; this bridge does not write paper content itself.",
        },
        "warnings": [],
    }
    status = "blocked"
    try:
        vendor_status = ensure_writing_vendor(args.venue, repo_dir, skip_clone=args.skip_clone)
        payload["writing_vendor"] = vendor_status
        payload["warnings"].extend(vendor_status.get("warnings", []))
        if not vendor_status.get("required_ready"):
            raise RuntimeError("writing vendor references are not ready for the requested venue; run modules/writing/scripts/sync_writing_vendor.py for this workspace.")
        repo_info, warnings = clone_or_update(repo_dir, skip_clone=args.skip_clone)
        payload["repo"] = repo_info
        payload["warnings"].extend(warnings)
        skill_links, link_warnings = link_skills(repo_dir)
        payload["skill_links"] = skill_links
        payload["warnings"].extend(link_warnings)
        venue_contract = ensure_venue_contract(args.project, args.venue, refresh_current_venue=bool(args.force_refresh and not args.prepare_only))
        payload["venue_contract"] = venue_contract
        workspace, workspace_info = maybe_run_prepare_phase(args.project, args.venue, title, repo_dir, force=not args.no_force_workspace)
        payload["workspace"] = str(workspace)
        payload["workspace_info"] = workspace_info
        before_pdf = active_pdf_fingerprint(args.project, args.venue)
        payload["before_pdf"] = before_pdf
        if args.force_refresh and not args.prepare_only:
            refresh_payload = refresh_current_paper_workspace_outputs(workspace, reason="current venue-formatted paper preview regeneration requested from the web/API; this is a writing/layout/citation refresh, not a research intervention")
            payload["current_paper_regeneration"] = refresh_payload
            payload["warnings"].append("Existing paper draft/final outputs were backed up and removed so writing must produce current TeX/PDF artifacts; this does not authorize changing scientific claims, running experiments, or treating the preview as submission-ready.")
            stale_update = {
                "paper_orchestra_bridge_status": "running",
                "paper_current_regeneration_requested": True,
                "paper_orchestra_force_refresh": False,
                "paper_orchestra_pdf_changed": False,
                "paper_orchestra_pdf_generated": False,
                "paper_orchestra_tex_generated": False,
                "paper_orchestra_final_pdf": "",
                "paper_orchestra_final_tex": "",
                "conference_preview_ready": False,
                "conference_preview_pdf": "",
                "conference_preview_tex": "",
                "normal_preview_ready": False,
                "pdf_ready": False,
                "pdf_path": "",
                "rendered_tex": "",
                "blocked_preview_pdf": "",
                "blocked_preview_tex": "",
                "latest_preview_pdf": "",
                "latest_preview_tex": "",
                "paper_orchestra_bridge_warnings": payload["warnings"],
                "paper_orchestra_before_pdf": before_pdf,
            }
            update_pipeline_state(args.project, stale_update, venue=args.venue, promote_to_top=True)
        preflight_commands, preflight_warnings = preflight(repo_dir, workspace)
        payload["preflight"] = preflight_commands
        payload["warnings"].extend(preflight_warnings)
        if args.prepare_only:
            status = "prepared"
            payload["phases"] = [workspace_info.get("phase", {"phase": "prepare", "status": "prepared"})]
            payload["workspace_status"] = workspace_status(workspace)
        else:
            result = run_phase_machine(
                args.project,
                args.venue,
                title,
                workspace,
                repo_dir,
                timeout_sec=args.timeout_sec,
                resume=args.resume,
                min_references=args.min_references,
                max_s2_queries=args.max_s2_queries,
                force_refresh=args.force_refresh,
            )
            payload.update(result)
            outputs = collect_outputs(args.project, args.venue, workspace)
            payload["outputs"] = outputs
            status = str(result.get("status") or "blocked")
            copied = outputs.get("copied", {}) if isinstance(outputs.get("copied", {}), dict) else {}
            current_raw_pdf = existing_file_path(copied.get("raw_pdf"))
            current_raw_tex = existing_file_path(copied.get("raw_tex"))
            workspace_artifacts = workspace_final_artifacts(workspace)
            payload["workspace_final_artifacts"] = workspace_artifacts
            if args.force_refresh and not (current_raw_pdf and current_raw_tex and workspace_artifacts.get("ready")):
                status = "blocked"
                payload["warnings"].append("Current paper-preview regeneration did not produce current workspace final TeX/PDF and copied raw PDF; refusing to reuse stale paper state.")
            after_pdf = sha256_file(Path(current_raw_pdf)) if args.force_refresh and current_raw_pdf else active_pdf_fingerprint(args.project, args.venue)
            payload["after_pdf"] = after_pdf
            payload["pdf_changed"] = bool(after_pdf.get("exists") and before_pdf.get("sha256") != after_pdf.get("sha256"))
            if args.force_refresh and not current_raw_pdf:
                payload["pdf_changed"] = False
            if args.force_refresh and before_pdf.get("exists") and after_pdf.get("exists") and before_pdf.get("sha256") == after_pdf.get("sha256"):
                status = "blocked"
                payload["warnings"].append("Current paper-preview refresh ended with the same PDF hash; writing did not produce a new preview artifact, so this pass remains blocked.")
    except Exception as exc:
        payload["error"] = str(exc)
        payload["warnings"].append(str(exc))
        payload.setdefault("phases", [])
        status = "blocked"

    payload["status"] = status
    payload["finished_at"] = now_iso()
    state_path = paths.state / "paper_orchestra_bridge.json"
    write_json(state_path, payload)
    report = write_report(paths, payload)

    outputs = payload.get("outputs", {}) if isinstance(payload.get("outputs", {}), dict) else {}
    copied = outputs.get("copied", {}) if isinstance(outputs.get("copied", {}), dict) else {}
    final_state = get_active_paper_state(args.project, venue=args.venue)
    preview_pdf = existing_file_path(final_state.get("conference_preview_pdf"))
    preview_tex = existing_file_path(final_state.get("rendered_tex") or final_state.get("conference_preview_tex"))
    existing_raw_pdf = existing_file_path(final_state.get("paper_orchestra_final_pdf"))
    existing_raw_tex = existing_file_path(final_state.get("paper_orchestra_final_tex"))
    current_raw_pdf = existing_file_path(copied.get("raw_pdf"))
    current_raw_tex = existing_file_path(copied.get("raw_tex"))
    copied_raw_pdf = current_raw_pdf if args.force_refresh else (current_raw_pdf or existing_raw_pdf)
    copied_raw_tex = current_raw_tex if args.force_refresh else (current_raw_tex or existing_raw_tex)
    existing_pdf_path = "" if args.force_refresh else existing_file_path(final_state.get("pdf_path"))
    existing_rendered_tex = "" if args.force_refresh else existing_file_path(final_state.get("rendered_tex"))
    rendered_tex = preview_tex if status == "generated" and preview_tex else copied_raw_tex if status == "generated" and copied_raw_tex else existing_rendered_tex
    pdf_path = preview_pdf if status == "generated" and preview_pdf else copied_raw_pdf if status == "generated" and copied_raw_pdf else existing_pdf_path
    pdf_ready = bool(status == "generated" and preview_pdf)
    if not args.force_refresh and final_state.get("pdf_ready") and existing_file_path(final_state.get("pdf_path")):
        pdf_ready = bool(pdf_ready or final_state.get("pdf_ready"))
    update = {
        "paper_orchestra_bridge_status": status,
        "paper_orchestra_bridge_json": str(state_path),
        "paper_orchestra_bridge_report": str(report),
        "paper_orchestra_repo_path": str(repo_dir),
        "paper_orchestra_repo_commit": (payload.get("repo", {}) if isinstance(payload.get("repo", {}), dict) else {}).get("commit", ""),
        "paper_orchestra_workspace": payload.get("workspace", ""),
        "paper_orchestra_final_pdf": copied_raw_pdf,
        "paper_orchestra_final_tex": copied_raw_tex,
        "paper_orchestra_pdf_generated": bool(current_raw_pdf),
        "paper_orchestra_tex_generated": bool(current_raw_tex),
        "paper_orchestra_source_repo": PAPER_ORCHESTRA_URL,
        "paper_orchestra_bridge_warnings": payload.get("warnings", []),
        "paper_current_regeneration_requested": bool(payload.get("current_paper_regeneration")),
        "paper_orchestra_force_refresh": False,
        "paper_orchestra_pdf_changed": bool(payload.get("pdf_changed")),
        "paper_orchestra_before_pdf": payload.get("before_pdf", {}),
        "paper_orchestra_after_pdf": payload.get("after_pdf", {}),
        "paper_orchestra_bridge_phases": [
            {"phase": row.get("phase"), "status": row.get("status"), "missing": row.get("missing_after") or row.get("missing") or []}
            for row in payload.get("phases", [])
            if isinstance(row, dict)
        ],
        "rendered_tex": rendered_tex,
        "pdf_path": pdf_path,
        "pdf_ready": pdf_ready,
    }
    if args.force_refresh and status != "generated":
        update.update({
            "conference_preview_ready": False,
            "conference_preview_pdf": "",
            "conference_preview_tex": "",
            "normal_preview_ready": False,
            "pdf_ready": False,
            "pdf_path": "",
            "rendered_tex": "",
            "blocked_preview_pdf": "",
            "blocked_preview_tex": "",
            "latest_preview_pdf": "",
            "latest_preview_tex": "",
        })
    update_pipeline_state(args.project, update, venue=args.venue, promote_to_top=True)
    print(report)
    return 0 if status in {"prepared", "generated"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
