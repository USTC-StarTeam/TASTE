#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from paper_common import get_active_paper_state, slugify, update_pipeline_state, venue_submission_policy, workspace_tool_path, write_json, write_text
from project_paths import ROOT, build_paths, load_project_config, management_python
from pipeline_guard import guard_fresh_base_blocker_entry
from paper_self_review import validate_paper_self_review_receipt


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def decode_output(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def pdf_fingerprint(state: dict[str, Any]) -> dict[str, Any]:
    for key in ["conference_preview_pdf", "pdf_path", "blocked_preview_pdf", "latest_preview_pdf", "paper_orchestra_final_pdf"]:
        path = Path(str(state.get(key) or ""))
        if path.exists() and path.is_file():
            stat = path.stat()
            return {"path": str(path), "exists": True, "sha256": sha256_file(path), "bytes": stat.st_size, "mtime": stat.st_mtime}
    return {"path": "", "exists": False, "sha256": "", "bytes": 0, "mtime": 0}


def run(cmd: list[str], *, timeout: int | None = None, cwd: Path = ROOT) -> dict[str, Any]:
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout)
        stdout = decode_output(proc.stdout)
        stderr = decode_output(proc.stderr)
        return {
            "command": cmd,
            "cwd": str(cwd),
            "return_code": proc.returncode,
            "stdout_tail": stdout[-8000:],
            "stderr_tail": stderr[-8000:],
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": cmd,
            "cwd": str(cwd),
            "return_code": 124,
            "stdout_tail": decode_output(exc.stdout)[-8000:],
            "stderr_tail": (decode_output(exc.stderr) + f"\nTimed out after {timeout}s")[-8000:],
            "timed_out": True,
        }


def read_text(path: Path, limit: int = 20000) -> str:
    if not path.exists() or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[:limit]


def preview_ready(state: dict[str, Any]) -> bool:
    return bool(
        state.get("conference_preview_ready")
        and (state.get("normal_preview_ready") or state.get("paper_normality_ready"))
        and (state.get("venue_template_format_ready") or state.get("paper_venue_format_status") == "pass")
        and (state.get("paper_figure_quality_ready") or state.get("paper_figure_quality_status") == "pass")
        and bool(state.get("paper_self_review_ready"))
    )


def preview_repair_status(
    preview_is_ready: bool,
    self_review_ready: bool,
    *,
    force_refresh: bool = False,
    pdf_exists: bool = False,
    pdf_changed: bool = False,
    passed_status: str = "pass",
) -> dict[str, Any]:
    status = passed_status if preview_is_ready and self_review_ready else "blocked"
    refresh_pdf_note = ""
    if force_refresh and pdf_exists and not pdf_changed:
        refresh_pdf_note = "refresh_requested_but_pdf_content_unchanged"
    return {"status": status, "refresh_pdf_note": refresh_pdf_note}


def prompt_only_pipeline_update(report_path: Path, loop_path: Path, prompt_path: Path) -> dict[str, Any]:
    return {
        "paper_preview_repair_loop_status": "prompt_ready",
        "paper_preview_repair_loop_report": str(report_path),
        "paper_preview_repair_loop_json": str(loop_path),
        "paper_preview_repair_rounds": 0,
        "paper_preview_repair_prompt_path": str(prompt_path),
        "paper_preview_repair_prompt_only": True,
        "paper_preview_repair_force_refresh": False,
    }


def compile_workspace_pdf(workspace: Path, *, timeout_sec: int = 600) -> dict[str, Any]:
    final_dir = workspace / "final"
    final_tex = final_dir / "paper.tex"
    result: dict[str, Any] = {
        "workspace": str(workspace),
        "final_tex": str(final_tex),
        "commands": [],
        "return_code": 2,
        "pdf_exists": False,
    }
    if not final_tex.exists():
        result["stderr_tail"] = "missing final/paper.tex"
        return result
    refs = workspace / "refs.bib"
    if refs.exists():
        refs_dst = final_dir / "refs.bib"
        if refs.resolve() != refs_dst.resolve():
            shutil.copy2(refs, refs_dst)
    figures_src = workspace / "figures"
    figures_dst = final_dir / "figures"
    if figures_src.exists() and not figures_dst.exists():
        shutil.copytree(figures_src, figures_dst)
    latexmk = shutil.which("latexmk") or workspace_tool_path("latexmk")
    pdflatex = shutil.which("pdflatex") or workspace_tool_path("pdflatex")
    if latexmk and Path(latexmk).exists():
        result["commands"].append(run([latexmk, "-pdf", "-interaction=nonstopmode", "paper.tex"], cwd=final_dir, timeout=timeout_sec))
    elif pdflatex and Path(pdflatex).exists():
        for _ in range(2):
            result["commands"].append(run([pdflatex, "-interaction=nonstopmode", "paper.tex"], cwd=final_dir, timeout=timeout_sec))
    else:
        result["stderr_tail"] = "latexmk/pdflatex not found"
    pdf = final_dir / "paper.pdf"
    result["pdf_exists"] = pdf.exists()
    result["return_code"] = 0 if pdf.exists() else 2
    return result


def gate_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "conference_preview_ready",
        "normal_preview_ready",
        "paper_normality_ready",
        "paper_normality_status",
        "paper_normality_pages",
        "paper_normality_body_pages",
        "paper_normality_estimated_reference_pages",
        "venue_submission_policy_status",
        "paper_normality_citation_count",
        "paper_venue_format_status",
        "venue_template_format_ready",
        "paper_figure_quality_status",
        "paper_figure_quality_ready",
        "paper_figure_blocker_count",
        "paper_citation_render_status",
        "paper_citation_render_blockers",
        "paper_citation_render_diagnostics",
        "paper_orchestra_workspace",
        "blocked_preview_pdf",
        "latest_preview_pdf",
        "conference_preview_pdf",
        "pdf_path",
    ]
    return {key: state.get(key) for key in keys}


def intish(value: Any) -> int:
    try:
        if value in (None, ""):
            return 0
        return int(float(value))
    except Exception:
        return 0


def manuscript_shape_requirement(venue: str, project: str = "") -> str:
    policy = venue_submission_policy(venue, project=project) if venue else {}
    family = str(policy.get("template_family") or "").lower() if isinstance(policy, dict) else ""
    is_nature = family == "springer-nature" or (venue and "nature" in slugify(venue)) or bool(policy.get("nature_family_article_mode") if isinstance(policy, dict) else False)
    if is_nature:
        return "Keep Nature-family article shape: Introduction, Results, Discussion, Methods, Data availability, Code availability, and References. Do not use top-level Related Work, Experiments, Conclusion, or Keywords unless the resolved journal contract explicitly requires them."
    return "Keep the normal conference shape: Introduction, Related Work, Method, Experiments, Conclusion, and References."


def repair_focus_diagnosis(state: dict[str, Any]) -> str:
    policy = state.get("venue_submission_policy") if isinstance(state.get("venue_submission_policy"), dict) else {}
    body_pages = intish(state.get("conference_preview_body_pages") or state.get("paper_normality_body_pages"))
    body_limit = intish(state.get("conference_preview_body_page_limit") or policy.get("body_page_max"))
    total_pages = intish(state.get("conference_preview_pages") or state.get("paper_normality_pages"))
    ref_pages = intish(state.get("conference_preview_reference_pages") or state.get("paper_normality_estimated_reference_pages"))
    citation_count = intish(state.get("paper_normality_citation_count"))
    citation_target = intish(
        state.get("paper_normality_reference_target")
        or state.get("paper_reference_quality_target")
        or policy.get("reference_quality_target")
        or policy.get("reference_quality_target")
        or policy.get("official_min_references")
        or policy.get("min_references")
    )
    target_source = str(state.get("paper_normality_reference_target_source") or policy.get("reference_target_source") or "").strip()
    layout_warnings = state.get("paper_layout_footprint_warnings") if isinstance(state.get("paper_layout_footprint_warnings"), list) else []
    citation_render_status = str(state.get("paper_citation_render_status") or "").strip()
    citation_render_blockers = state.get("paper_citation_render_blockers") if isinstance(state.get("paper_citation_render_blockers"), list) else []
    lines: list[str] = []
    if body_pages and body_limit:
        if body_pages <= body_limit:
            lines.append(
                f"- Body pages are within the official venue limit: {body_pages}/{body_limit}. This is not a prose-shortening task. The current writing task is layout/citation/template quality repair: diagnose figure/table footprint, citation coverage, bibliography density, and unresolved citations first."
            )
        else:
            lines.append(
                f"- Body pages exceed the official venue limit: {body_pages}/{body_limit}. Diagnose float/table footprint before changing prose; edit prose only if prose is the measured source of overflow."
            )
    elif body_pages:
        lines.append(f"- Body pages measured as {body_pages}; the official body-page limit must be read from venue_requirements.json before any layout edit.")
    if total_pages:
        suffix = f", estimated reference pages={ref_pages}" if ref_pages else ""
        lines.append(f"- Total compiled pages={total_pages}{suffix}; references may be outside the body-page limit when the venue contract says so.")
    if citation_count and citation_target:
        label = "official citation minimum" if target_source == "official" else "writing reference-quality target"
        lines.append(f"- References/citations: {citation_count}/{citation_target} against {label}. If below target, add only real, verified, relevant references; never invent BibTeX entries or lower the target to pass.")
    elif citation_count:
        lines.append(f"- References/citations detected: {citation_count}; if the venue has no official minimum, use the writing quality target recorded in venue_requirements.json.")
    if layout_warnings:
        lines.append(f"- Figure/table footprint diagnostics are present ({len(layout_warnings)} warning rows). Treat oversized or single-column wide figures as the primary layout repair target before cutting scientific content.")
    if citation_render_blockers:
        rendered = []
        for item in citation_render_blockers[:5]:
            if isinstance(item, dict):
                rendered.append(f"{item.get('id', 'citation_render')}: {item.get('public_detail') or item.get('detail') or ''}")
            else:
                rendered.append(str(item))
        lines.append("- Citation rendering audit is blocked: " + " | ".join(rendered) + " Fix citation commands, BibTeX/style compatibility, and compiled logs before page/prose repair.")
    elif citation_render_status == "block":
        lines.append("- Citation rendering audit is blocked; inspect paper_citation_render_diagnostics, paper.log, compile.log, and the PDF text before page/prose repair.")
    return "\n".join(lines) if lines else "- No deterministic page/citation diagnosis is available yet; read venue_requirements.json and audits before editing."


def refresh_current_venue_contract(project: str, venue: str, timeout_sec: int) -> dict[str, Any]:
    """Resolve and validate the current venue rules/template before repair."""
    requirements = run(
        [
            sys.executable,
            str(ROOT / "scripts" / "resolve_venue_requirements.py"),
            "--project",
            project,
            "--venue",
            venue,
            "--refresh-current-venue",
        ],
        timeout=max(300, min(max(timeout_sec, 0) or 1800, 7200)),
    )
    template = run(
        [
            sys.executable,
            str(ROOT / "scripts" / "fetch_latex_template.py"),
            "--project",
            project,
            "--venue",
            venue,
        ],
        timeout=900,
    )
    return {"requirements": requirements, "template": template}


def claude_repair_prompt(project: str, venue: str, title: str, reports: dict[str, str], state: dict[str, Any]) -> str:
    venue_slug = slugify(venue)
    return f"""
You are the writing module for current-paper preview revision.

Project: {project}
Venue: {venue}
Title: {title}

Role: TASTE provides official venue requirements and deterministic diagnostics; project Claude Code owns manuscript revision decisions inside the prepared paper workspace. This loop must not edit experiment code, launch experiments, or change scientific state.

Objective: revise the current venue-formatted manuscript preview so `scripts/build_conference_preview_paper.py --project {project} --venue {venue} --title "{title}"` can pass its preview gates. Diagnose the actual blocker first: venue page accounting, figure/table footprint, citation correctness, template compliance, or manuscript-shape quality. Treat this as paper preview repair only, not a direct intervention in the underlying research project.

This is a manuscript repair task, not a scientific-result invention task. The output must read like a real venue-formatted research paper, while the deterministic preview gates remain honest about evidence and submission readiness. If the measured body pages are already within the official limit, do not frame the job as prose shortening; repair figure footprint, real citation coverage, bibliography density, and venue-template details.

Independent-first workflow requirement:
- Start with an open-ended manuscript review from the current artifacts, before using TASTE deterministic gate details as a checklist. TASTE gate diagnostics are a later cross-check, not the source of the issue list.
- Phase 1: independently read the compiled PDF text using `pdftotext`, the TeX source, refs.bib, paper.log/compile.log, and venue_requirements.json. Build your own issue inventory from those artifacts.
- Phase 2: repair manuscript/citation/template issues inside the paper writing workspace only.
- Phase 3: after Phase 1 and Phase 2, use the TASTE preview/normality/figure reports and gate snapshot below only as a cross-check to make sure no TASTE gate was missed.
- In `review_protocol`, record this order explicitly with `discovery_order` or `phase_order` showing `independent_artifact_review` before `gate_crosscheck`, and set `gate_crosscheck_after_independent_review=true`.
- Each `independent_findings` item must include `discovery_phase="independent_artifact_review"` or `found_before_gate_crosscheck=true`, plus raw PDF/TeX/BibTeX/log/venue evidence. Findings discovered only from TASTE gate names do not count.

Read and follow these local contracts and artifacts:
- `.claude/skills/writing/SKILL.md` and `modules/writing/SKILL.md`
- `projects/{project}/paper/writing/{venue_slug}/workspace/final/paper.tex`
- `projects/{project}/paper/writing/{venue_slug}/workspace/final/refs.bib` or `projects/{project}/paper/writing/{venue_slug}/workspace/refs.bib`
- `projects/{project}/paper/writing/{venue_slug}/workspace/final/paper.pdf`
- `projects/{project}/paper/writing/{venue_slug}/workspace/final/paper.log` and any `compile.log`
- `projects/{project}/paper/venues/{venue_slug}/venue_requirements.json`
- `projects/{project}/reports/paper_normality_audit.md`
- `projects/{project}/reports/paper_figure_quality_audit.md`
- `projects/{project}/paper/output/{venue_slug}/conference_preview_report.md`

Independent manuscript review requirement:
- Identify every manuscript-level issue you can find from Phase 1 artifact reading: citation rendering, bibliography fields/style compatibility, missing or stale references, title/abstract/front matter, venue article shape, section flow, figure/table footprint, unsupported claims, internal TASTE/status prose, and PDF-visible artifacts. Do not merely copy TASTE-listed deterministic blockers; record what project Claude Code independently found after reading the raw artifacts.
- Apply manuscript/citation/template repairs inside the paper writing workspace only. Do not edit experiment code, run experiments, change evidence gates, or invent claims.
- After reviewing and repairing, write `projects/{project}/state/paper_preview_self_review.json`. This receipt is mandatory; The workflow will keep the preview blocked without it.
- The receipt must include this schema: `status`, `reviewed_by`, `venue`, `artifact_fingerprints` with `pdf`, `pdf_text`, `tex`, `refs_bib`, `compile_log`, `paper_log`, `venue_requirements` entries, `review_protocol`, non-empty `independent_findings`, `repairs_applied`, `remaining_blockers`, and `final_checks` with `compiled=true`, `pdf_text_rechecked=true`, `venue_shape_rechecked=true`, `citation_render_rechecked=true`, `bibliography_rechecked=true`.
- `review_protocol` must prove this was not a fixed TASTE checklist: set `open_ended_review=true`, describe the open-ended manuscript issue-discovery scope, record the independent-first discovery order described above, and include `artifact_reading_log` entries for current `pdf`, `pdf_text`, `tex`, `refs_bib`, `compile_log`, `paper_log`, and `venue_requirements`. Each reading-log entry must include the artifact/path, the command or method used to read it, and evidence such as a sha256, excerpt, location, or observation.
- Clean checks belong in `review_protocol.artifact_reading_log`; `independent_findings` must list the actual manuscript issues Claude Code found from the current artifacts before/during repair. Each `independent_findings` item must be a structured object with `category`, `issue`, `review_source`/`discovery_method`, and artifact evidence such as `source_artifacts`, `location`, `pdf_text_excerpt`, `tex_location`, `bibtex_entry`, or `log_excerpt`. Do not write a generic "checked by Claude" sentence; The workflow will reject findings that only restate TASTE gate names without raw PDF/TeX/BibTeX/log/venue evidence.
- Each `repairs_applied` item must name the changed or checked file, describe the action, and include a post-repair verification command/result or evidence. If no file edit was needed for a finding, record the checked artifact and verification evidence rather than leaving the repair unstructured.
- For file artifacts, each entry must include `path` and `sha256`. For `pdf_text`, include either a text-file path plus `sha256`, or an excerpt/summary showing the PDF text was actually read.

TASTE deterministic gate cross-check, to use after the independent issue inventory:
{repair_focus_diagnosis(state)}

Current gate snapshot:
```json
{json.dumps(gate_snapshot(state), ensure_ascii=False, indent=2)}
```

Venue-formatted preview report:
```text
{reports.get("conference_preview", "")}
```

Paper normality audit:
```text
{reports.get("normality", "")}
```

Figure quality audit:
```text
{reports.get("figures", "")}
```

Repair requirements:
- Do not fabricate experiments, metrics, datasets, citations, or readiness.
- Do not change evidence/submission gates to make the paper look ready.
- Do not run new experiments, edit research code, change experiment registries, or alter scientific state. This loop is only for manuscript, citation, figure/table layout, and venue-template repair.
- If body pages are within the official venue limit, treat the repair as figure/table footprint, citation coverage, bibliography density, venue-template compliance, and manuscript-shape quality.
- If body pages are within the official venue limit, do not enter a prose-shortening workflow just because total PDF pages include references. Keep the paper substantive and repair figure footprint, real citation coverage, bibliography density, and template details first.
- If the blocker is word count/substance, add normal venue-appropriate manuscript prose only where it is justified by existing evidence, verified citations, implemented methods, or neutral method/protocol scope. Do not create new empirical claims or a weakness/negative-result narrative.
- If the blocker is venue page policy, read the normality audit page_breakdown and overflow_source first. Repair the real cause: figure/table footprint, bibliography/reference-page footprint, or prose length only when prose is actually the source. Do not apply one venue's page/template rule to another venue.
- If the blocker is venue format, preserve the resolved official target venue template, class, sidecar files, fonts, margins, and bibliography style from venue_requirements.json and workspace/inputs/template.tex.
- If the blocker is figures, redraw, resize, remove, or move weak/oversized figures according to the figure audit; do not hide weak evidence with cosmetic charts and do not cut scientific content before diagnosing float footprint.
- `paper.tex` must remain a full venue-formatted manuscript, not a revision-status report, gate report, or blocker summary.
- {manuscript_shape_requirement(venue, project=project)}
- Keep at least the reference count required by the current venue normality audit. If the venue has no official minimum, satisfy TASTE's recorded quality target from venue_requirements.json; do not invent an official citation rule. When the current count is below target, add only real, relevant, verified references and synchronized BibTeX entries; do not introduce anonymous/missing entries.
- If `citation_keys_resolved` is blocked, repair citations before declaring success: add real BibTeX entries for already cited works, replace unsupported keys with verified references already in `refs.bib`, or remove unsupported citation keys. The compiled PDF must not contain `?`, `??`, or undefined-citation warnings.
- If `citation_render_clean` is blocked or `paper_citation_render_status` is `block`, repair citation rendering before any page/prose repair. For Springer Nature numeric styles (`sn-nature`, `sn-basic`, `sn-mathphys-num`), do not use `\citet`, `\citeauthor`, `\citeyear`, `\citealp`, or `\citealt` in the manuscript. Rewrite author-led prose as normal narrative text followed by `\citep{{...}}` or the template-supported numeric citation command. Recompile and confirm `paper.log`/`compile.log` contain no `Author undefined`, no BibTeX/BST errors such as `can't pop an empty literal stack`, and the PDF text contains no `(author?)`, `[?]`, or `??`.
- Improve the paper shape while repairing: the Method should include the mathematical model and algorithmic design; The venue-appropriate Results/Experiments prose should include dataset/protocol/reference calibration plus a clean evaluation matrix for the proposed variants. It must not read like a to-do list.
- Compile `paper/writing/{venue_slug}/workspace/final/paper.pdf` after TeX edits if possible.
- Before returning, write/update `state/paper_preview_self_review.json` for the current PDF/TeX/refs/logs. The workflow will validate this receipt and keep the preview blocked if it is missing, stale, or not tied to the current artifacts.

Required local commands after edits:
- `{management_python()} scripts/audit_paper_figures.py --project {project} --venue {venue}`
- `{management_python()} scripts/build_conference_preview_paper.py --project {project} --venue {venue} --title "{title}"`

Return concise Markdown with: Conclusion, Files Changed, Gates Fixed, Remaining Blockers, and Next Actions.
""".strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Iterate writing preview revisions until the generated PDF is a valid current venue-formatted preview.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--venue", default="")
    parser.add_argument("--title", default="")
    parser.add_argument("--max-rounds", type=int, default=5)
    parser.add_argument("--timeout-sec", type=int, default=14400)
    parser.add_argument("--backend", default="claude")
    parser.add_argument("--refresh-current-paper", dest="force_refresh", action="store_true", help="Regenerate the current venue-formatted paper preview from current writing inputs instead of reusing an unchanged PDF.")
    parser.add_argument("--force-refresh", dest="force_refresh", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    guard_rc = guard_fresh_base_blocker_entry(args.project, args.venue, Path(__file__).name, safe_unblock=False)
    if guard_rc is not None:
        return int(guard_rc)

    paths = build_paths(args.project)
    cfg = load_project_config(args.project)
    venue = str(args.venue or get_active_paper_state(args.project).get("venue") or cfg.get("target_venue") or cfg.get("venue") or "").strip()
    loop_path = paths.state / "paper_preview_repair_loop.json"
    report_path = paths.reports / "paper_preview_repair_loop.md"
    if not venue:
        payload = {
            "project": args.project,
            "updated_at": now_iso(),
            "status": "blocked",
            "reason": "target venue is not configured; writing must not guess a venue template or page limit",
        }
        write_json(loop_path, payload)
        write_text(report_path, "# Writing Preview Revision\n\n- status: blocked\n- reason: target venue is not configured; writing must not guess a venue template or page limit\n")
        update_pipeline_state(
            args.project,
            {
                "paper_preview_repair_loop_status": "blocked",
                "paper_preview_repair_loop_report": str(report_path),
                "paper_preview_repair_loop_json": str(loop_path),
                "paper_preview_repair_blocker": "target venue is not configured",
            },
            promote_to_top=True,
        )
        print(report_path)
        return 2
    title = args.title or str(get_active_paper_state(args.project, venue=venue).get("title") or cfg.get("topic") or args.project)
    venue_slug = slugify(venue)
    rounds: list[dict[str, Any]] = []
    starting_state = get_active_paper_state(args.project, venue=venue)
    starting_pdf = pdf_fingerprint(starting_state)
    venue_contract = refresh_current_venue_contract(args.project, venue, args.timeout_sec)
    if venue_contract["requirements"].get("return_code") != 0 or venue_contract["template"].get("return_code") != 0:
        payload = {
            "project": args.project,
            "venue": venue,
            "title": title,
            "updated_at": now_iso(),
            "status": "blocked",
            "reason": "current venue requirements/template could not be verified before writing repair",
            "venue_contract": venue_contract,
        }
        write_json(loop_path, payload)
        write_text(report_path, "# Writing Preview Revision\n\n- status: blocked\n- reason: current venue requirements/template could not be verified before writing repair\n")
        update_pipeline_state(
            args.project,
            {
                "paper_preview_repair_loop_status": "blocked",
                "paper_preview_repair_loop_report": str(report_path),
                "paper_preview_repair_loop_json": str(loop_path),
                "paper_preview_repair_rounds": 0,
                "paper_preview_repair_blocker": "current venue requirements/template could not be verified",
            },
            venue=venue,
            promote_to_top=True,
        )
        print(report_path)
        return 2

    for round_index in range(1, max(1, args.max_rounds) + 1):
        preview_before = run([
            sys.executable,
            str(ROOT / "scripts" / "build_conference_preview_paper.py"),
            "--project",
            args.project,
            "--venue",
            venue,
            "--title",
            title,
        ])
        state = get_active_paper_state(args.project, venue=venue)
        if preview_ready(state) and not (args.force_refresh and round_index == 1):
            rounds.append({"round": round_index, "status": "already_passed", "preview_before": preview_before, "state": gate_snapshot(state)})
            break

        reports = {
            "conference_preview": read_text(paths.root / "paper" / "output" / venue_slug / "conference_preview_report.md"),
            "normality": read_text(paths.reports / "paper_normality_audit.md"),
            "figures": read_text(paths.reports / "paper_figure_quality_audit.md"),
        }
        prompt = claude_repair_prompt(args.project, venue, title, reports, state)
        prompt_path = paths.root / "paper" / "metadata" / f"writing_revision_prompt_round_{round_index}.md"
        write_text(prompt_path, prompt)
        if args.backend == "off":
            prompt_round = {
                "round": round_index,
                "status": "prompt_ready",
                "prompt_path": str(prompt_path),
                "preview_before": preview_before,
                "state": gate_snapshot(state),
                "writer_call": "not_started_backend_off",
            }
            payload = {
                "project": args.project,
                "venue": venue,
                "title": title,
                "updated_at": now_iso(),
                "status": "prompt_ready",
                "prompt_only": True,
                "reason": "backend=off writes the next project-Claude repair prompt only; it does not validate or mutate paper/self-review gates",
                "prompt_path": str(prompt_path),
                "rounds": [prompt_round],
                "max_rounds": args.max_rounds,
                "current": gate_snapshot(state),
                "venue_contract": venue_contract,
            }
            write_json(loop_path, payload)
            write_text(
                report_path,
                "# Writing Preview Revision\n\n"
                "- status: prompt_ready\n"
                "- writer_call: not started; prompt was written for the next writing pass\n"
                f"- prompt: {prompt_path}\n",
            )
            update_pipeline_state(args.project, prompt_only_pipeline_update(report_path, loop_path, prompt_path), venue=venue, promote_to_top=True)
            print(report_path)
            return 0
        else:
            claude = run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "claude_project_session.py"),
                    "--project",
                    args.project,
                    "--stage",
                    "paper",
                    "--message-file",
                    str(prompt_path),
                    "--timeout-sec",
                    str(args.timeout_sec),
                    "--agent-id",
                    "writing_revision",
                    "--no-resume",
                ],
                timeout=args.timeout_sec + 900 if args.timeout_sec > 0 else None,
            )
            claude["prompt_path"] = str(prompt_path)

        state_mid = get_active_paper_state(args.project, venue=venue)
        workspace = Path(str(state_mid.get("paper_orchestra_workspace") or paths.root / "paper" / "writing" / venue_slug / "workspace"))
        compile_result = compile_workspace_pdf(workspace)
        preview_after = run([
            sys.executable,
            str(ROOT / "scripts" / "build_conference_preview_paper.py"),
            "--project",
            args.project,
            "--venue",
            venue,
            "--title",
            title,
        ])
        state_after = get_active_paper_state(args.project, venue=venue)
        workspace_after = Path(str(state_after.get("paper_orchestra_workspace") or paths.root / "paper" / "writing" / venue_slug / "workspace"))
        self_review = validate_paper_self_review_receipt(
            paths.root,
            venue,
            current_pdf=workspace_after / "final" / "paper.pdf",
            current_tex=workspace_after / "final" / "paper.tex",
            current_refs=(workspace_after / "final" / "refs.bib") if (workspace_after / "final" / "refs.bib").exists() else (workspace_after / "refs.bib"),
            min_mtime=prompt_path.stat().st_mtime if prompt_path.exists() else None,
        )
        after_pdf = pdf_fingerprint(state_after)
        pdf_changed = bool(after_pdf.get("exists") and starting_pdf.get("sha256") != after_pdf.get("sha256"))
        round_decision = preview_repair_status(
            preview_ready(state_after),
            bool(self_review.get("ready")),
            force_refresh=args.force_refresh,
            pdf_exists=bool(after_pdf.get("exists")),
            pdf_changed=pdf_changed,
            passed_status="passed",
        )
        round_status = str(round_decision.get("status") or "blocked")
        refresh_pdf_note = str(round_decision.get("refresh_pdf_note") or "")
        rounds.append(
            {
                "round": round_index,
                "status": round_status,
                "refresh_requested": bool(args.force_refresh),
                "pdf_changed": pdf_changed,
                "refresh_pdf_note": refresh_pdf_note,
                "pdf_before": starting_pdf,
                "pdf_after": after_pdf,
                "preview_before": preview_before,
                "claude": claude,
                "compile": compile_result,
                "preview_after": preview_after,
                "self_review": self_review,
                "state_after": gate_snapshot(state_after),
            }
        )
        if round_status == "passed":
            break

    final_state = get_active_paper_state(args.project, venue=venue)
    final_workspace = Path(str(final_state.get("paper_orchestra_workspace") or paths.root / "paper" / "writing" / venue_slug / "workspace"))
    final_self_review = validate_paper_self_review_receipt(
        paths.root,
        venue,
        current_pdf=final_workspace / "final" / "paper.pdf",
        current_tex=final_workspace / "final" / "paper.tex",
        current_refs=(final_workspace / "final" / "refs.bib") if (final_workspace / "final" / "refs.bib").exists() else (final_workspace / "refs.bib"),
    )
    final_pdf = pdf_fingerprint(final_state)
    pdf_changed = bool(final_pdf.get("exists") and starting_pdf.get("sha256") != final_pdf.get("sha256"))
    final_decision = preview_repair_status(
        preview_ready(final_state),
        bool(final_self_review.get("ready")),
        force_refresh=args.force_refresh,
        pdf_exists=bool(final_pdf.get("exists")),
        pdf_changed=pdf_changed,
        passed_status="pass",
    )
    status = str(final_decision.get("status") or "blocked")
    refresh_pdf_note = str(final_decision.get("refresh_pdf_note") or "")
    payload = {
        "project": args.project,
        "venue": venue,
        "title": title,
        "updated_at": now_iso(),
        "status": status,
        "refresh_requested": bool(args.force_refresh),
        "pdf_changed": pdf_changed,
        "refresh_pdf_note": refresh_pdf_note,
        "pdf_before": starting_pdf,
        "pdf_after": final_pdf,
        "rounds": rounds,
        "max_rounds": args.max_rounds,
        "final": gate_snapshot(final_state),
        "final_self_review": final_self_review,
        "venue_contract": venue_contract,
    }
    write_json(loop_path, payload)
    lines = [
        "# Writing Preview Revision\n\n",
        f"- status: {status}\n",
        f"- rounds: {len(rounds)} / {args.max_rounds}\n",
        f"- current_paper_preview_regeneration_requested: {bool(args.force_refresh)}\n",
        f"- pdf_changed: {pdf_changed}\n",
        f"- refresh_pdf_note: {refresh_pdf_note}\n",
        f"- conference_preview_ready: {bool(final_state.get('conference_preview_ready'))}\n",
        f"- normality: {final_state.get('paper_normality_status', '')}\n",
        f"- body_pages: {final_state.get('paper_normality_body_pages', '')}\n",
        f"- estimated_reference_pages: {final_state.get('paper_normality_estimated_reference_pages', '')}\n",
        f"- venue_template_format: {final_state.get('paper_venue_format_status', '')}\n",
        f"- figure_quality: {final_state.get('paper_figure_quality_status', '')}\n",
        f"- claude_self_review: {final_self_review.get('status')}\n",
        f"- claude_self_review_receipt: {final_self_review.get('path', '')}\n",
        f"- blocked_preview_pdf: {final_state.get('blocked_preview_pdf') or final_state.get('latest_preview_pdf') or ''}\n",
        "\n## Rounds\n\n",
    ]
    for row in rounds:
        lines.append(f"- round {row.get('round')}: {row.get('status')}\n")
        claude = row.get("claude", {}) if isinstance(row.get("claude"), dict) else {}
        if claude.get("prompt_path"):
            lines.append(f"  - prompt: {claude.get('prompt_path')}\n")
        if str(claude.get("stderr_tail") or "").startswith("backend=off"):
            lines.append("  - writer_call: not started; prompt was written for the next writing pass\n")
        elif claude.get("return_code") not in {None, 0}:
            lines.append(f"  - claude_return_code: {claude.get('return_code')}\n")
        self_review = row.get("self_review", {}) if isinstance(row.get("self_review"), dict) else {}
        if self_review:
            lines.append(f"  - claude_self_review: {self_review.get('status')}\n")
            for item in (self_review.get("blockers", []) if isinstance(self_review.get("blockers", []), list) else [])[:4]:
                if isinstance(item, dict):
                    lines.append(f"    - {item.get('id')}: {item.get('detail', '')}\n")
            for item in (self_review.get("evidence_blockers", []) if isinstance(self_review.get("evidence_blockers", []), list) else [])[:4]:
                if isinstance(item, dict):
                    lines.append(f"    - evidence/{item.get('category', item.get('id'))}: {item.get('detail') or item.get('issue', '')}\n")
    write_text(report_path, "".join(lines))
    update_pipeline_state(
        args.project,
        {
            "paper_preview_repair_loop_status": status,
            "paper_preview_repair_loop_report": str(report_path),
            "paper_preview_repair_loop_json": str(loop_path),
            "paper_preview_repair_rounds": len(rounds),
            "paper_preview_repair_refresh_requested": bool(args.force_refresh),
            "paper_preview_repair_force_refresh": False,
            "paper_preview_repair_pdf_changed": pdf_changed,
            "paper_preview_repair_refresh_pdf_note": refresh_pdf_note,
            "paper_self_review_status": final_self_review.get("status"),
            "paper_self_review_ready": bool(final_self_review.get("ready")),
            "paper_self_review_receipt": final_self_review.get("path", ""),
            "paper_self_review_blockers": final_self_review.get("blockers", []),
            "paper_self_review_evidence_blockers": final_self_review.get("evidence_blockers", []),
            "paper_self_review_evidence_blocker_count": final_self_review.get("evidence_blocker_count", 0),
            "paper_self_review_preview_only_ready": bool(final_self_review.get("preview_only_ready")),
            "paper_self_review_submission_evidence_ready": bool(final_self_review.get("submission_evidence_ready")),
            "paper_self_review_independent_findings_count": final_self_review.get("independent_findings_count", 0),
            "paper_self_review_repairs_count": final_self_review.get("repairs_count", 0),
        },
        venue=venue,
        promote_to_top=True,
    )
    print(report_path)
    return 0 if status == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
