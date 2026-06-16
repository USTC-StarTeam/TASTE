#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import struct
from pathlib import Path
from typing import Any

from paper_common import (
    ensure_paper_dirs,
    get_active_paper_state,
    read_text,
    slugify,
    update_pipeline_state,
    write_json,
    write_text,
)
from project_paths import build_paths


FIGURE_BLOCK_RE = re.compile(r"\\begin\{(?P<env>figure\*?)\}(?P<body>.*?)\\end\{(?P=env)\}", re.DOTALL)
TABLE_BLOCK_RE = re.compile(r"\\begin\{(?P<env>table\*?)\}(?P<body>.*?)\\end\{(?P=env)\}", re.DOTALL)
TABULAR_BEGIN_RE = re.compile(r"\\begin\{(?P<env>tabular\*?|tabularx)\}", re.DOTALL)
INCLUDE_RE = re.compile(r"\\includegraphics(?:\[(?P<opts>[^\]]*)\])?\{(?P<path>[^{}]+)\}")
CAPTION_START_RE = re.compile(r"\\caption(?:\[[^\]]*\])?\{", re.DOTALL)

EVIDENCE_LIMIT_TERMS = [
    "synthetic",
    "toy",
    "smoke test",
    "pipeline plumbing",
    "future work",
    "not yet implemented",
    "planned",
    "near zero",
    "mismatch",
    "blocked",
]

POLISH_RISK_TERMS = [
    "expected range",
    "worst slice",
    "requires resolved",
    "does not support scientific conclusions",
]


def has_evidence_limit_term(text: str, term: str) -> bool:
    term = str(term or "").strip().lower()
    if not term:
        return False
    pattern = r"(?<![a-z0-9])" + re.escape(term).replace(r"\ ", r"\s+") + r"(?![a-z0-9])"
    return bool(re.search(pattern, str(text or "").lower()))


def evidence_limit_terms_in(*texts: str) -> list[str]:
    joined = "\n".join(str(item or "") for item in texts)
    return [term for term in EVIDENCE_LIMIT_TERMS if has_evidence_limit_term(joined, term)]


def braced_content(text: str, open_brace_index: int) -> str:
    depth = 0
    out: list[str] = []
    for char in text[open_brace_index:]:
        if char == "{":
            depth += 1
            if depth == 1:
                continue
        elif char == "}":
            depth -= 1
            if depth == 0:
                break
        if depth >= 1:
            out.append(char)
    return "".join(out).strip()


def first_caption(body: str) -> str:
    match = CAPTION_START_RE.search(body)
    if not match:
        return ""
    return braced_content(body, match.end() - 1)


def png_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        data = path.read_bytes()[:24]
    except OSError:
        return None
    if len(data) >= 24 and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return struct.unpack(">II", data[16:24])
    return None


def resolve_figure_path(raw: str, tex_path: Path, output_dir: Path, workspace: Path | None) -> Path:
    candidate = Path(raw)
    bases = [tex_path.parent, output_dir]
    if workspace:
        bases.extend([workspace / "final", workspace])
    if candidate.is_absolute():
        return candidate
    for base in bases:
        path = base / candidate
        if path.exists():
            return path
    return tex_path.parent / candidate


def option_width(opts: str) -> str:
    for part in opts.split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key.strip() == "width":
            return value.strip()
    return ""


def width_fraction(width: str) -> float:
    text = str(width or '').replace(' ', '')
    match = re.match(r'^(0?\.\d+|1(?:\.0+)?)\\(?:line|column|text)width$', text)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return 0.0
    if text in {'\\linewidth', '\\columnwidth', '\\textwidth'}:
        return 1.0
    return 0.0


def strip_latex_commands(text: str) -> str:
    text = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?(?:\{([^{}]*)\})?", r"\1", text)
    text = text.replace("\\_", "_").replace("\\%", "%").replace("\\&", "&")
    return re.sub(r"\s+", " ", text).strip()




def _read_latex_braced_argument(text: str, open_index: int) -> tuple[str, int]:
    if open_index >= len(text) or text[open_index] != "{":
        return "", open_index
    depth = 0
    out: list[str] = []
    for pos in range(open_index, len(text)):
        char = text[pos]
        if char == "{":
            depth += 1
            if depth == 1:
                continue
        elif char == "}":
            depth -= 1
            if depth == 0:
                return "".join(out), pos + 1
        if depth >= 1:
            out.append(char)
    return "", open_index


def _skip_latex_space(text: str, index: int) -> int:
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def first_tabular(body: str) -> dict[str, str] | None:
    match = TABULAR_BEGIN_RE.search(body)
    if not match:
        return None
    env = match.group("env")
    pos = _skip_latex_space(body, match.end())
    args: list[str] = []
    expected_args = 2 if env in {"tabular*", "tabularx"} else 1
    for _ in range(expected_args):
        pos = _skip_latex_space(body, pos)
        if pos >= len(body) or body[pos] != "{":
            break
        value, pos = _read_latex_braced_argument(body, pos)
        args.append(value)
    col_spec = args[1] if env in {"tabular*", "tabularx"} and len(args) >= 2 else (args[0] if args else "")
    end_token = "\\end{" + env + "}"
    end = body.find(end_token, pos)
    tabulbody = body[pos:end] if end >= 0 else body[pos:]
    return {"env": env, "cols": col_spec, "body": tabulbody}


def likely_script_path(image_path: Path, output_dir: Path, workspace: Path | None) -> Path | None:
    candidates = [image_path.with_suffix(".py")]
    bases = [image_path.parent, output_dir / "figures", output_dir]
    if workspace:
        bases.extend([workspace / "figures", workspace / "final" / "figures", workspace, workspace / "final"])
    for base in bases:
        if not base:
            continue
        candidates.extend([
            base / f"{image_path.stem}.py",
            base / "generate_figures.py",
            base / "plot_figures.py",
        ])
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            resolved = candidate
        if resolved in seen:
            continue
        seen.add(resolved)
        if candidate.exists():
            return candidate
    return None


def script_quality_issues(script_path: Path | None) -> list[str]:
    if not script_path or not script_path.exists():
        return ["no reproducible plotting script found for this figure"]
    text = read_text(script_path)
    issues: list[str] = []
    if "placeholder" in text.lower() or "mock" in text.lower():
        issues.append("plotting script appears placeholder/mock-like")
    if not re.search(r"(matplotlib|seaborn|plotly|networkx|graphviz|PIL|ImageDraw)", text):
        issues.append("plotting script does not appear to generate a real visual artifact")
    return issues


def table_rows(tex: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, block in enumerate(TABLE_BLOCK_RE.finditer(tex), start=1):
        env = block.group("env")
        body = block.group("body")
        caption = first_caption(body)
        label_match = re.search(r"\\label\{([^{}]+)\}", body)
        table_id = label_match.group(1).replace(":", "_") if label_match else f"table_{index}"
        issues: list[str] = []
        warnings: list[str] = []
        lower_caption = caption.lower()
        lower_body = body.lower()
        tabular = first_tabular(body)
        col_spec = tabular.get("cols", "") if tabular else ""
        tabulbody = tabular.get("body", "") if tabular else ""
        column_count = len(re.findall(r"[lcrpmbxX]", re.sub(r"\{[^{}]*\}", "", col_spec)))
        tabulenv = tabular.get("env", "") if tabular else ""
        uses_scaling = any(token in body for token in ["\\resizebox", "\\scalebox", "\\adjustbox", "\\begin{tabularx}", "\\begin{tabular*}"])
        uses_small_font = any(token in body for token in ["\\small", "\\scriptsize", "\\footnotesize"])
        if not tabular:
            issues.append("table has no tabular/tabular*/tabularx body")
        cleaned = strip_latex_commands(tabulbody) if tabulbody else ""
        long_tokens = [token for token in re.split(r"\s*&\s*|\\\\", cleaned) if len(token.strip()) > 34]
        if env == "table" and column_count >= 6 and not uses_scaling and not uses_small_font:
            issues.append(f"single-column table has {column_count} columns without resizebox/adjustbox/tabularx/smaller font; high overflow risk")
        elif env == "table" and column_count >= 4 and not uses_scaling and not uses_small_font:
            warnings.append(f"single-column table has {column_count} columns without resizebox/adjustbox/tabularx/smaller font; inspect compiled PDF for overflow")
        if env == "table" and column_count >= 3 and long_tokens and not uses_scaling:
            issues.append("single-column table contains long cells without scaling or wrapping; likely exceeds column width")
        evidence_terms = evidence_limit_terms_in(lower_caption, lower_body)
        if evidence_terms:
            issues.append("main-text table is evidence-limited or probe-only: " + ", ".join(evidence_terms[:6]))
        if "---" in body or "near zero" in lower_caption or "not audit-verified" in lower_body:
            issues.append("table contains placeholder/near-zero/non-audit-ready values and must not pass as polished result evidence")
        if not caption:
            issues.append("table has no caption")
        if len(caption) > 360:
            warnings.append("caption is too long and likely hurts readability")
        rows.append(
            {
                "table_id": table_id,
                "environment": env,
                "tabulenvironment": tabulenv,
                "tex_index": index,
                "caption": caption,
                "column_count": column_count,
                "uses_scaling": uses_scaling,
                "uses_small_font": uses_small_font,
                "status": "block" if issues else "warn" if warnings else "pass",
                "issues": issues,
                "warnings": warnings,
            }
        )
    return rows


def figure_rows(tex: str, tex_path: Path, output_dir: Path, workspace: Path | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, block in enumerate(FIGURE_BLOCK_RE.finditer(tex), start=1):
        env = block.group("env")
        body = block.group("body")
        caption = first_caption(body)
        includes = list(INCLUDE_RE.finditer(body))
        if not includes:
            rows.append(
                {
                    "figure_id": f"figure_{index}",
                    "environment": env,
                    "caption": caption,
                    "status": "block",
                    "issues": ["figure environment has no includegraphics"],
                    "warnings": [],
                }
            )
            continue
        for inc_index, include in enumerate(includes, start=1):
            raw_path = include.group("path").strip()
            opts = include.group("opts") or ""
            width = option_width(opts)
            image_path = resolve_figure_path(raw_path, tex_path, output_dir, workspace)
            dims = png_dimensions(image_path) if image_path.exists() else None
            script_path = likely_script_path(image_path, output_dir, workspace)
            issues: list[str] = []
            warnings: list[str] = []
            lower_caption = caption.lower()
            lower_id = image_path.stem.lower()

            if not image_path.exists():
                issues.append("included figure file is missing")
            if image_path.exists() and image_path.suffix.lower() not in {".png", ".pdf", ".jpg", ".jpeg"}:
                issues.append("figure file type is not a standard paper image format")
            width_scale = width_fraction(width)
            if dims:
                width_px, height_px = dims
                aspect = width_px / max(1, height_px)
                if width_px < 900 or height_px < 500:
                    issues.append(f"figure resolution is too low for a venue-formatted PDF: {width_px}x{height_px}")
                if env == "figure*" and (width_px < 1800 or height_px < 1000):
                    issues.append(f"two-column figure resolution is too low for a readable venue-formatted preview: {width_px}x{height_px}; redraw at >=1800x1000 or simplify")
                if env == "figure" and aspect > 1.65:
                    warnings.append(f"wide {aspect:.2f}:1 graphic is squeezed into a single-column figure")
                if env == "figure" and width_scale >= 0.9 and height_px >= 900:
                    warnings.append("large single-column figure footprint; if body pages are tight, resize/redraw, use a spanning float, or move this float before editing manuscript prose")
            else:
                if image_path.exists() and image_path.suffix.lower() == ".png":
                    warnings.append("PNG dimensions could not be read")

            if env == "figure" and "\\textwidth" in width:
                issues.append("single-column figure uses width=\\textwidth; use figure* or width=\\columnwidth/\\linewidth")
            if env == "figure*" and "\\linewidth" in width and dims:
                width_px, height_px = dims
                if width_px / max(1, height_px) > 2.2:
                    issues.append("wide figure* has an extreme aspect ratio and is likely unreadable in the PDF")
            if not width:
                warnings.append("includegraphics has no explicit width")
            issues.extend(script_quality_issues(script_path))
            if not caption:
                issues.append("figure has no caption")
            if len(caption) > 430:
                warnings.append("caption is too long and likely hurts readability")

            evidence_terms = evidence_limit_terms_in(lower_caption, lower_id)
            if evidence_terms:
                issues.append("main-text figure is evidence-limited or proposal/probe-only: " + ", ".join(evidence_terms[:6]))
            polish_terms = [term for term in POLISH_RISK_TERMS if term in lower_caption]
            if polish_terms:
                warnings.append("caption reads like an internal blocker note rather than a polished paper figure: " + ", ".join(polish_terms[:4]))

            rows.append(
                {
                    "figure_id": image_path.stem or f"figure_{index}_{inc_index}",
                    "environment": env,
                    "tex_index": index,
                    "include_path": raw_path,
                    "image_path": str(image_path),
                    "exists": image_path.exists(),
                    "script_path": str(script_path) if script_path and script_path.exists() else "",
                    "include_options": opts,
                    "width": width,
                    "width_fraction": width_scale,
                    "dimensions": {"width": dims[0], "height": dims[1]} if dims else {},
                    "caption": caption,
                    "status": "block" if issues else "warn" if warnings else "pass",
                    "issues": issues,
                    "warnings": warnings,
                }
            )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit whether writing figures are venue-formatted manuscript quality.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--venue", default="")
    args = parser.parse_args()

    paths = build_paths(args.project)
    paper = ensure_paper_dirs(args.project)
    state = get_active_paper_state(args.project, venue=args.venue)
    venue = args.venue or str(state.get("venue") or "")
    venue_slug = slugify(venue) if venue else str(state.get("venue_slug") or state.get("active_venue") or "")
    output_dir = paper["output_dir"] / venue_slug if venue_slug else paper["output_dir"]
    workspace_text = str(state.get("paper_orchestra_workspace") or "")
    workspace = Path(workspace_text) if workspace_text else None
    workspace_tex = workspace / "final" / "paper.tex" if workspace else None
    tex_candidates = [
        workspace_tex,
        Path(str(state.get("rendered_tex") or "")) if state.get("rendered_tex") else None,
        Path(str(state.get("conference_preview_tex") or "")) if state.get("conference_preview_tex") else None,
        output_dir / "paper.tex",
    ]
    tex_path = next((path for path in tex_candidates if path and path.exists()), output_dir / "paper.tex")
    tex = read_text(tex_path) if tex_path.exists() else ""
    rows = figure_rows(tex, tex_path, output_dir, workspace) if tex else []
    tables = table_rows(tex) if tex else []
    blocked = [row for row in [*rows, *tables] if row.get("status") == "block"]
    warned = [row for row in [*rows, *tables] if row.get("status") == "warn"]
    status = "pass" if not blocked else "blocked"

    layout_warnings = [
        warning
        for row in rows
        for warning in row.get("warnings", [])
        if "footprint" in str(warning).lower() or "squeezed" in str(warning).lower()
    ]
    payload = {
        "project": args.project,
        "venue": venue,
        "status": status,
        "figure_quality_ready": status == "pass",
        "source_path": str(tex_path) if tex_path.exists() else "",
        "figure_count": len(rows),
        "table_count": len(tables),
        "blocked_count": len(blocked),
        "warning_count": len(warned),
        "figures": rows,
        "tables": tables,
        "failed_figures": [row for row in rows if row.get("status") == "block"],
        "failed_tables": [row for row in tables if row.get("status") == "block"],
        "failed_items": blocked,
        "warning_figures": [row for row in rows if row.get("status") == "warn"],
        "warning_tables": [row for row in tables if row.get("status") == "warn"],
        "warning_items": warned,
        "layout_footprint_warnings": layout_warnings,
        "principle": "Figures must be readable, venue-layout compatible, reproducible, and backed by claim-ready evidence before a PDF is treated as a valid venue-formatted preview. Diagnose figure/table footprint before editing manuscript prose for page fit.",
    }
    out_json = paths.state / "paper_figure_quality_audit.json"
    out_md = paths.reports / "paper_figure_quality_audit.md"
    write_json(out_json, payload)

    lines = [
        "# Paper Figure Quality Audit\n\n",
        f"- status: {status}\n",
        f"- figure_quality_ready: {payload['figure_quality_ready']}\n",
        f"- figure_count: {len(rows)}\n",
        f"- table_count: {len(tables)}\n",
        f"- blocked_count: {len(blocked)}\n",
        f"- warning_count: {len(warned)}\n",
        f"- source_path: {tex_path if tex_path.exists() else ''}\n",
        "\n## Figure Checks\n\n",
    ]
    if not rows:
        lines.append("- [pass] no figures found; there are no bad figures to promote. Paper taste/readiness remains controlled by the normality and evidence gates.\n")
    for row in rows:
        lines.append(f"- [{row['status']}] {row['figure_id']}: {row.get('image_path', '')}\n")
        for issue in row.get("issues", []):
            lines.append(f"  - block: {issue}\n")
        for warning in row.get("warnings", []):
            lines.append(f"  - warn: {warning}\n")
    lines.append("\n## Table Checks\n\n")
    if not tables:
        lines.append("- [pass] no tables found.\n")
    for row in tables:
        lines.append(f"- [{row['status']}] {row['table_id']}: columns={row.get('column_count', '')} env={row.get('environment', '')}\n")
        for issue in row.get("issues", []):
            lines.append(f"  - block: {issue}\n")
        for warning in row.get("warnings", []):
            lines.append(f"  - warn: {warning}\n")
    lines.append("\n## Required TASTE Action\n\n")
    if blocked:
        lines.append("- Re-run the TASTE paper refinement loop so Claude Code redraws blocked figures, rewrites or scales overflowing tables, and removes/moves evidence-limited visual/table evidence out of the main paper.\n")
    elif layout_warnings:
        lines.append("- PDF page pressure should be repaired by reducing, redrawing, or moving oversized floats before changing manuscript prose.\n")
    else:
        lines.append("- Keep figure scripts and captions synchronized with the final TeX before exposing the PDF.\n")
    write_text(out_md, "".join(lines))

    update_pipeline_state(
        args.project,
        {
            "paper_figure_quality_status": status,
            "paper_figure_quality_ready": status == "pass",
            "paper_figure_quality_report": str(out_md),
            "paper_figure_quality_audit": str(out_json),
            "paper_figure_count": len(rows),
            "paper_table_count": len(tables),
            "paper_figure_blocker_count": len(blocked),
            "paper_figure_warning_count": len(warned),
            "paper_figure_failed": blocked[:20],
            "paper_table_failed": [row for row in tables if row.get("status") == "block"][:20],
        },
        venue=venue,
        promote_to_top=True,
    )
    print(out_md)
    return 0 if status == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
