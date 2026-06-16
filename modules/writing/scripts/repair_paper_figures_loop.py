#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from paper_common import get_active_paper_state, load_json, slugify, update_pipeline_state, write_json, write_text
from project_paths import ROOT, build_paths, load_project_config, management_python
from pipeline_guard import guard_fresh_base_blocker_entry


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def run(cmd: list[str], *, timeout: int | None = None) -> dict[str, Any]:
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=timeout)
    return {
        "command": cmd,
        "return_code": proc.returncode,
        "stdout_tail": proc.stdout[-6000:],
        "stderr_tail": proc.stderr[-6000:],
    }


def read_text(path: Path, limit: int = 16000) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[:limit]


def claude_repair_prompt(project: str, venue: str, title: str, report: str, state: dict[str, Any]) -> str:
    return f"""
You are TASTE's writing figure-quality repair agent.

Project: {project}
Venue: {venue}
Title: {title}

Goal: keep iterating until the generated PDF can be shown as an accepted conference preview. The current PDF must remain viewable as a blocked preview, but your job is to repair it so `modules/writing/scripts/audit_paper_figures.py` passes.

Read and follow these local contracts:
- `framework/resources/claude/skills/writing/SKILL.md`
- `state/paper_figure_quality_audit.json`
- `reports/paper_figure_quality_audit.md`
- `paper/orchestra/{slugify(venue)}/workspace/final/paper.tex`
- `paper/orchestra/{slugify(venue)}/workspace/final/figures/*`
- `paper/orchestra/{slugify(venue)}/workspace/figures/*`

Current figure audit report:
```text
{report}
```

Current paper state snapshot:
```json
{json.dumps({k: state.get(k) for k in [
    "paper_figure_quality_status",
    "paper_figure_blocker_count",
    "paper_normality_status",
    "paper_venue_format_status",
    "conference_preview_ready",
    "pdf_ready",
    "paper_orchestra_workspace",
]}, ensure_ascii=False, indent=2)}
```

Repair requirements:
- Do not fabricate scientific results, metrics, datasets, or claims.
- Do not hide weak evidence with cosmetic plots.
- If a figure is synthetic/probe/future-work/blocker-only, remove it from main text or move it to limitations text without a main-text figure.
- For ACM/CIKM two-column layout, single-column `figure` must use `width=\\columnwidth` or `width=\\linewidth`; use `figure*` only when the wide overview genuinely needs two columns.
- Rebuild or edit adjacent plotting scripts when a retained figure is redrawn.
- Keep final TeX in ACM `acmart`/`sigconf` format and keep references intact.
- Compile final/paper.pdf if possible.
- Return only after files are updated, or after writing an exact blocker explaining why no valid figure can be kept.

Required local commands after edits:
- `{management_python()} modules/writing/scripts/audit_paper_figures.py --project {project} --venue {venue}`
- `{management_python()} modules/writing/scripts/build_conference_preview_paper.py --project {project} --venue {venue} --title "{title}"`
""".strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Iterate writing figure repair until blocked-preview figures become acceptable.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--venue", default="")
    parser.add_argument("--title", default="")
    parser.add_argument("--max-rounds", type=int, default=3)
    parser.add_argument("--timeout-sec", type=int, default=14400)
    parser.add_argument("--backend", default="claude")
    args = parser.parse_args()

    guard_rc = guard_fresh_base_blocker_entry(args.project, args.venue, Path(__file__).name, safe_unblock=False)
    if guard_rc is not None:
        return int(guard_rc)

    paths = build_paths(args.project)
    cfg = load_project_config(args.project)
    venue = args.venue or str(get_active_paper_state(args.project).get("venue") or "ICLR")
    title = args.title or str(get_active_paper_state(args.project, venue=venue).get("title") or cfg.get("topic") or args.project)
    loop_path = paths.state / "paper_figure_repair_loop.json"
    report_path = paths.reports / "paper_figure_repair_loop.md"
    rounds: list[dict[str, Any]] = []

    for round_index in range(1, args.max_rounds + 1):
        audit = run([sys.executable, str(ROOT / "modules" / "writing" / "scripts" / "audit_paper_figures.py"), "--project", args.project, "--venue", venue])
        state = get_active_paper_state(args.project, venue=venue)
        if state.get("paper_figure_quality_ready"):
            preview = run([sys.executable, str(ROOT / "modules" / "writing" / "scripts" / "build_conference_preview_paper.py"), "--project", args.project, "--venue", venue, "--title", title])
            rounds.append({"round": round_index, "status": "already_passed", "audit": audit, "preview": preview})
            break

        report = read_text(paths.reports / "paper_figure_quality_audit.md")
        prompt = claude_repair_prompt(args.project, venue, title, report, state)
        prompt_path = paths.root / "paper" / "metadata" / f"figure_repair_prompt_round_{round_index}.md"
        write_text(prompt_path, prompt)
        if args.backend == "off":
            claude = {"return_code": 2, "stdout_tail": "", "stderr_tail": "backend=off; repair prompt written only", "prompt_path": str(prompt_path)}
        else:
            claude = run(
                [
                    sys.executable,
                    str(ROOT / "framework" / "scripts" / "claude_project_session.py"),
                    "--project",
                    args.project,
                    "--stage",
                    "paper-figure-repair",
                    "--message-file",
                    str(prompt_path),
                    "--timeout-sec",
                    str(args.timeout_sec),
                    "--agent-id",
                    "paper_figure_repair",
                    "--no-resume",
                ],
                timeout=args.timeout_sec + 900 if args.timeout_sec > 0 else None,
            )
            claude["prompt_path"] = str(prompt_path)

        preview = run([sys.executable, str(ROOT / "modules" / "writing" / "scripts" / "build_conference_preview_paper.py"), "--project", args.project, "--venue", venue, "--title", title])
        state_after = get_active_paper_state(args.project, venue=venue)
        rounds.append(
            {
                "round": round_index,
                "status": "passed" if state_after.get("paper_figure_quality_ready") else "blocked",
                "audit_before": audit,
                "claude": claude,
                "preview": preview,
                "paper_figure_quality_status": state_after.get("paper_figure_quality_status", ""),
                "paper_figure_blocker_count": state_after.get("paper_figure_blocker_count", ""),
                "conference_preview_ready": bool(state_after.get("conference_preview_ready")),
                "pdf_ready": bool(state_after.get("pdf_ready")),
            }
        )
        if state_after.get("paper_figure_quality_ready") and state_after.get("conference_preview_ready"):
            break

    final_state = get_active_paper_state(args.project, venue=venue)
    status = "pass" if final_state.get("paper_figure_quality_ready") else "blocked"
    payload = {
        "project": args.project,
        "venue": venue,
        "title": title,
        "updated_at": now_iso(),
        "status": status,
        "rounds": rounds,
        "max_rounds": args.max_rounds,
        "final": {
            "paper_figure_quality_status": final_state.get("paper_figure_quality_status", ""),
            "paper_figure_quality_ready": bool(final_state.get("paper_figure_quality_ready")),
            "paper_figure_blocker_count": final_state.get("paper_figure_blocker_count", ""),
            "conference_preview_ready": bool(final_state.get("conference_preview_ready")),
            "pdf_ready": bool(final_state.get("pdf_ready")),
            "pdf_path": final_state.get("pdf_path", ""),
            "blocked_pdf_path": final_state.get("paper_orchestra_final_pdf", ""),
        },
    }
    write_json(loop_path, payload)
    lines = [
        "# Paper Figure Repair Loop\n\n",
        f"- status: {status}\n",
        f"- rounds: {len(rounds)} / {args.max_rounds}\n",
        f"- figure_quality: {payload['final']['paper_figure_quality_status']}\n",
        f"- figure_blockers: {payload['final']['paper_figure_blocker_count']}\n",
        f"- conference_preview_ready: {payload['final']['conference_preview_ready']}\n",
        "- note: this loop reports figure quality only; use the paper preview repair loop for normality/venue/overall preview blockers.\n",
        "\n## Rounds\n\n",
    ]
    for row in rounds:
        lines.append(f"- round {row.get('round')}: {row.get('status')} figure_quality={row.get('paper_figure_quality_status', '')} blockers={row.get('paper_figure_blocker_count', '')}\n")
        claude = row.get("claude", {}) if isinstance(row.get("claude"), dict) else {}
        if claude.get("prompt_path"):
            lines.append(f"  - prompt: {claude.get('prompt_path')}\n")
        if claude.get("return_code") not in {None, 0}:
            lines.append(f"  - claude_return_code: {claude.get('return_code')}\n")
    write_text(report_path, "".join(lines))
    update_pipeline_state(
        args.project,
        {
            "paper_figure_repair_loop_status": status,
            "paper_figure_repair_loop_report": str(report_path),
            "paper_figure_repair_loop_json": str(loop_path),
            "paper_figure_repair_rounds": len(rounds),
        },
        venue=venue,
        promote_to_top=True,
    )
    print(report_path)
    return 0 if status == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
