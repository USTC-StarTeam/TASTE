#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from paper_common import get_active_paper_state, read_text, slugify, update_pipeline_state, venue_reference_target, venue_submission_policy, write_json, write_text
from project_paths import ROOT, build_paths
from pipeline_guard import guard_fresh_base_blocker_entry


CITE_RE = re.compile(r"\\cite\w*\*?(?:\s*\[[^\]]*\])*\s*\{([^{}]+)\}")


def parse_bib_titles(path: Path) -> dict[str, str]:
    text = read_text(path) if path.exists() else ""
    out: dict[str, str] = {}
    for match in re.finditer(r"@\w+\s*\{\s*([^,\s]+)\s*,(.*?)(?=\n@\w+\s*\{|\Z)", text, flags=re.DOTALL):
        key = match.group(1).strip()
        body = match.group(2)
        title_match = re.search(r"\btitle\s*=\s*\{(.*?)\}\s*,?", body, flags=re.DOTALL | re.IGNORECASE)
        if title_match:
            out[key] = re.sub(r"\s+", " ", title_match.group(1)).strip()
    return out


def cited_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    if not path.exists():
        return keys
    text = read_text(path)
    for match in CITE_RE.finditer(text):
        keys.update(key.strip() for key in match.group(1).split(",") if key.strip())
    return keys


def manuscript_shape_requirement(venue: str, project: str = "") -> str:
    policy = venue_submission_policy(venue, project=project) if venue else {}
    family = str(policy.get("template_family") or "").lower() if isinstance(policy, dict) else ""
    is_nature = family == "springer-nature" or (venue and "nature" in slugify(venue)) or bool(policy.get("nature_family_article_mode") if isinstance(policy, dict) else False)
    if is_nature:
        return "Keep Nature-family article shape: Introduction, Results, Discussion, Methods, Data availability, Code availability, and References. Do not use top-level Related Work, Experiments, Conclusion, or Keywords unless the resolved journal contract explicitly requires them."
    return "Keep the normal 5-section paper structure: Introduction, Related Work, Method, Experiments, Conclusion."


def run(cmd: list[str], *, timeout: int | None = None) -> dict[str, Any]:
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=timeout)
    return {
        "cmd": cmd,
        "return_code": proc.returncode,
        "stdout_tail": proc.stdout[-12000:],
        "stderr_tail": proc.stderr[-12000:],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Ask project agent writing to revise citation coverage using only verified refs.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--venue", required=True)
    parser.add_argument("--min-references", type=int, default=0, help="Optional explicit reference target; default reads current venue_requirements.json.")
    parser.add_argument("--timeout-sec", type=int, default=2400)
    parser.add_argument("--title", default="")
    args = parser.parse_args()

    guard_rc = guard_fresh_base_blocker_entry(args.project, args.venue, Path(__file__).name, safe_unblock=False)
    if guard_rc is not None:
        return int(guard_rc)

    reference_target_info = venue_reference_target(args.venue, project=args.project, explicit_min=args.min_references)
    args.min_references = int(reference_target_info.get("target") or 0)
    paths = build_paths(args.project)
    state = get_active_paper_state(args.project, venue=args.venue)
    venue_slug = slugify(args.venue)
    workspace = Path(str(state.get("paper_orchestra_workspace") or paths.root / "paper" / "orchestra" / venue_slug / "workspace"))
    final_tex = workspace / "final" / "paper.tex"
    refs_bib = workspace / "refs.bib"
    report_json = paths.state / "paper_citation_coverage_revision.json"
    report_md = paths.reports / "paper_citation_coverage_revision.md"
    bib_titles = parse_bib_titles(refs_bib)
    cited = cited_keys(final_tex)
    unused = sorted(set(bib_titles) - cited)

    if args.min_references <= 0:
        report = {
            "status": "blocked" if reference_target_info.get("source") == "unresolved_venue_policy" else "pass",
            "reason": "venue reference target is unresolved; run resolve_venue_requirements.py first" if reference_target_info.get("source") == "unresolved_venue_policy" else "no venue or TASTE reference-count target is recorded",
            "reference_target": reference_target_info,
            "workspace": str(workspace),
            "final_tex": str(final_tex),
            "refs_bib": str(refs_bib),
            "bib_entry_count": len(bib_titles),
            "cited_key_count": len(cited),
        }
        write_json(report_json, report)
        write_text(report_md, "# Paper Citation Coverage Revision\n\n- status: " + report["status"] + "\n- reason: " + report["reason"] + "\n")
        update_pipeline_state(args.project, {
            "paper_citation_coverage_status": report["status"],
            "paper_citation_coverage_report": str(report_md),
            "paper_citation_coverage_json": str(report_json),
        }, venue=args.venue, promote_to_top=True)
        print(report_md)
        return 0 if report["status"] == "pass" else 2

    if len(bib_titles) < args.min_references:
        report = {
            "status": "blocked",
            "reason": f"verified refs.bib has {len(bib_titles)} entries, below required {args.min_references}",
            "workspace": str(workspace),
            "final_tex": str(final_tex),
            "refs_bib": str(refs_bib),
            "bib_entry_count": len(bib_titles),
            "cited_key_count": len(cited),
        }
        write_json(report_json, report)
        write_text(report_md, "# Paper Citation Coverage Revision\n\n- status: blocked\n- reason: verified refs.bib is below the required reference count\n")
        update_pipeline_state(args.project, {
            "paper_citation_coverage_status": "blocked",
            "paper_citation_coverage_report": str(report_md),
            "paper_citation_coverage_json": str(report_json),
        }, venue=args.venue, promote_to_top=True)
        print(report_md)
        return 2

    if len(cited) >= args.min_references:
        status = "pass"
        claude_result: dict[str, Any] = {"status": "skipped", "reason": "citation coverage already satisfies the gate"}
    else:
        unused_preview = "\n".join(f"- {key}: {bib_titles[key]}" for key in unused[:80])
        instruction = f"""
Use the writing module skills to revise citation coverage for this generated paper.

Workspace: {workspace}
Final TeX: {final_tex}
Verified BibTeX: {refs_bib}
Target: at least {args.min_references} distinct cited keys in final/paper.tex.

Current state:
- verified BibTeX entries: {len(bib_titles)}
- current distinct cited keys: {len(cited)}
- unused verified citation keys available:
{unused_preview}

Hard requirements:
- Do not write a new paper from scratch.
- Do not add or invent BibTeX entries.
- Use only keys already present in {refs_bib}.
- Add citations only where the cited work is contextually relevant to the existing paragraph.
- {manuscript_shape_requirement(args.venue, project=args.project)}
- Preserve all existing figures, tables, claims, evidence limitations, and evidence-gate honesty.
- After editing, run writing citation/LaTeX gates and compile final/paper.pdf.
- If you cannot reach {args.min_references} relevant distinct citations without citation stuffing, leave the paper blocked and write the exact blocker into the workspace.

Return concise Markdown with the changed files, final distinct citation count, compile status, and blockers if any.
""".strip()
        prompt_path = workspace / "writing_citation_coverage_prompt.md"
        write_text(prompt_path, instruction)
        claude_result = run([
            sys.executable,
            str(ROOT / "scripts" / "claude_project_session.py"),
            "--project",
            args.project,
            "--stage",
            "writing:citation-coverage",
            "--message-file",
            str(prompt_path),
            "--timeout-sec",
            str(args.timeout_sec),
            "--agent-id",
            "writing_citation_coverage",
            "--no-resume",
        ], timeout=max(args.timeout_sec + 900, 1800) if args.timeout_sec > 0 else None)
        cited = cited_keys(final_tex)
        status = "pass" if claude_result["return_code"] == 0 and len(cited) >= args.min_references else "blocked"

    commands = [
        run([sys.executable, str(ROOT / "scripts" / "repair_paper_orchestra_citations.py"), "--project", args.project, "--venue", args.venue, "--min-good-refs", str(args.min_references), "--max-queries", "160"]),
        run([sys.executable, str(ROOT / "scripts" / "run_paper_orchestra_bridge.py"), "--project", args.project, "--venue", args.venue, "--title", args.title or str(state.get("title") or args.project), "--skip-clone", "--no-force-workspace", "--timeout-sec", "0"]),
    ]
    preview_rc = commands[-1]["return_code"]
    cited = cited_keys(final_tex)
    status = "pass" if status == "pass" and preview_rc == 0 and len(cited) >= args.min_references else "blocked"
    report = {
        "status": status,
        "workspace": str(workspace),
        "final_tex": str(final_tex),
        "refs_bib": str(refs_bib),
        "bib_entry_count": len(parse_bib_titles(refs_bib)),
        "cited_key_count": len(cited),
        "reference_target": reference_target_info,
        "claude_result": claude_result,
        "commands": commands,
    }
    write_json(report_json, report)
    lines = [
        "# Paper Citation Coverage Revision\n\n",
        f"- status: {status}\n",
        f"- bib_entry_count: {report['bib_entry_count']}\n",
        f"- cited_key_count: {report['cited_key_count']}\n",
        f"- final_tex: {final_tex}\n",
        f"- refs_bib: {refs_bib}\n",
    ]
    write_text(report_md, "".join(lines))
    update_pipeline_state(args.project, {
        "paper_citation_coverage_status": status,
        "paper_citation_coverage_report": str(report_md),
        "paper_citation_coverage_json": str(report_json),
        "paper_citation_coverage_cited_keys": len(cited),
    }, venue=args.venue, promote_to_top=True)
    print(report_md)
    return 0 if status == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
