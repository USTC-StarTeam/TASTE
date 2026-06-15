#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from paper_common import (
    ensure_paper_dirs,
    get_active_paper_state,
    load_json,
    read_text,
    slugify,
    springer_nature_article_shape_failures,
    springer_nature_pdf_front_matter_failures,
    update_pipeline_state,
    validate_venue_template_format,
    venue_reference_target,
    venue_submission_policy,
    venue_template_profile,
    write_json,
    write_text,
)
from project_paths import build_paths


CANONICAL_MAIN_SECTIONS = [
    "introduction",
    "related work",
    "method",
    "experiments",
    "conclusion",
]

SECTION_ALIASES = {
    "introduction": {"introduction", "intro"},
    "related work": {"related work", "background", "preliminaries"},
    "method": {"method", "methodology", "approach", "model", "proposed method"},
    "experiments": {"experiments", "experimental setup", "results", "evaluation"},
    "conclusion": {"conclusion", "conclusions", "summary"},
}

BAD_TOP_LEVEL_TERMS = {
    "paperorchestra",
    "evidence-limited",
    "preview status",
    "readiness matrix",
    "reviewer-facing",
    "appendix",
    "next experimental milestones",
    "claim ledger",
    "autonomous research trajectory",
    "project-state narrative",
    "anticipated reviewer questions",
    "concrete experiment specification",
    "what would make the paper",
    "internal review response",
    "ar assurance",
}


def pdf_pages(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        proc = subprocess.run(["pdfinfo", str(path)], text=True, capture_output=True)
        match = re.search(r"^Pages:\s+(\d+)", proc.stdout, flags=re.MULTILINE)
        if match:
            return int(match.group(1))
    except FileNotFoundError:
        pass
    try:
        proc = subprocess.run(["mdls", "-name", "kMDItemNumberOfPages", "-raw", str(path)], text=True, capture_output=True)
        value = proc.stdout.strip()
        if value.isdigit():
            return int(value)
    except FileNotFoundError:
        pass
    try:
        data = path.read_bytes()
        # Conservative fallback when pdfinfo is unavailable. Avoid counting the
        # /Pages tree object by excluding the trailing "s".
        return len(re.findall(rb"/Type\s*/Page\b(?!s)", data))
    except Exception:
        return 0


def tex_text(path: Path) -> str:
    return read_text(path) if path.exists() else ""


def markdown_headings(text: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r"^#{1,2}\s+(.+?)\s*$", text, flags=re.MULTILINE)]


def latex_sections(text: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r"\\section\*?\{([^{}]+)\}", text)]


def normalize_heading(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9 ]+", " ", str(text).lower())
    text = re.sub(r"\s+", " ", text).strip()
    return text


def has_canonical_section(headings: list[str], canonical: str) -> bool:
    normalized = [normalize_heading(item) for item in headings]
    aliases = SECTION_ALIASES.get(canonical, {canonical})
    for heading in normalized:
        if heading in aliases:
            return True
        if canonical == "experiments" and any(token in heading for token in ["experiment", "result", "evaluation"]):
            return True
        if canonical == "method" and any(token in heading for token in ["method", "approach", "model"]):
            return True
    return False


def count_citations(text: str) -> int:
    keys: set[str] = set()
    for match in re.finditer(r"\\cite\w*\{([^{}]+)\}", text):
        for key in match.group(1).split(","):
            key = key.strip()
            if key:
                keys.add(key)
    bibitems = set(re.findall(r"\\bibitem\{([^{}]+)\}", text))
    bib_entries = set(re.findall(r"@\w+\s*\{\s*([^,\s]+)", text))
    markdown_keys = set(re.findall(r"\[@([A-Za-z0-9:_-]+)\]", text))
    markdown_keys.update(re.findall(r"(?<!\w)@([A-Za-z][A-Za-z0-9:_-]{2,})", text))
    refs_match = re.search(r"^#{1,3}\s+References\b(.*?)(?:^#{1,3}\s+|\Z)", text, flags=re.IGNORECASE | re.MULTILINE | re.DOTALL)
    markdown_refs: set[str] = set()
    if refs_match:
        for idx, line in enumerate(refs_match.group(1).splitlines(), start=1):
            stripped = line.strip()
            if re.match(r"^[-*]\s+\S", stripped) or re.match(r"^\d+[\.)]\s+\S", stripped):
                markdown_refs.add(str(idx))
    return max(len(keys), len(bibitems), len(bib_entries), len(markdown_keys), len(markdown_refs))


def bibliography_entry_count(*paths: Path | None) -> int:
    best = 0
    for path in paths:
        if not path or not path.exists():
            continue
        text = read_text(path)
        best = max(best, len(re.findall(r"@\w+\s*\{\s*([^,\s]+)", text)))
        best = max(best, len(re.findall(r"\\bibitem\{([^{}]+)\}", text)))
    return best


def citation_keys(text: str) -> set[str]:
    keys: set[str] = set()
    for match in re.finditer(r"\\cite\w*\{([^{}]+)\}", text):
        for key in match.group(1).split(","):
            key = key.strip()
            if key:
                keys.add(key)
    return keys


def bib_keys_from_paths(*paths: Path | None) -> set[str]:
    keys: set[str] = set()
    for path in paths:
        if not path or not path.exists():
            continue
        text = read_text(path)
        keys.update(re.findall(r"@\w+\s*\{\s*([^,\s]+)", text))
        keys.update(re.findall(r"\\bibitem\{([^{}]+)\}", text))
    return keys


def venue_policy_source_detail(policy: dict[str, Any]) -> str:
    if policy.get("source_label"):
        return str(policy.get("source_label"))
    sources = policy.get("official_sources") if isinstance(policy.get("official_sources"), list) else []
    for item in sources:
        if isinstance(item, dict):
            label = str(item.get("label") or "").strip()
            url = str(item.get("url") or "").strip()
            if label or url:
                return label + (f" ({url})" if url and label else url)
    if policy.get("format_label"):
        return str(policy.get("format_label"))
    return str(policy.get("policy_gap") or "venue policy source not recorded")


def reference_start_page(pdf_path: Path, total_pages: int) -> dict[str, Any]:
    empty = {"page": 0, "line_index": 0, "line_count": 0, "starts_netop": False}
    if not pdf_path.exists() or total_pages <= 0:
        return empty
    for page_num in range(1, total_pages + 1):
        try:
            proc = subprocess.run(
                ["pdftotext", "-layout", "-f", str(page_num), "-l", str(page_num), str(pdf_path), "-"],
                text=True,
                capture_output=True,
                timeout=15,
            )
        except Exception:
            return empty
        if proc.returncode != 0:
            return empty
        lines = [re.sub(r"\s+", " ", line).strip() for line in (proc.stdout or "").splitlines() if line.strip()]
        for idx, line in enumerate(lines):
            compact = re.sub(r"\s+", "", line).lower()
            compact_no_lineno = re.sub(r"^\d+", "", compact)
            plain = re.sub(r"^\s*\d+\s+", "", line.lower())
            if compact_no_lineno.startswith("references") or compact_no_lineno.startswith("bibliography") or re.match(r"^(references|bibliography)\b", plain):
                return {
                    "page": page_num,
                    "line_index": idx,
                    "line_count": len(lines),
                    "starts_netop": idx <= 4,
                }
    return empty


def estimate_page_breakdown(total_pages: int, citation_count: int, policy: dict[str, Any], pdf_path: Path | None = None) -> dict[str, Any]:
    ref_page_max = int(policy.get("reference_page_max") or 0)
    refs_per_page = int(policy.get("estimated_references_per_page") or 34)
    ref_info = reference_start_page(pdf_path, total_pages) if pdf_path else {"page": 0, "line_index": 0, "line_count": 0, "starts_netop": False}
    ref_start = int(ref_info.get("page") or 0)
    if ref_start:
        estimated_reference_pages = max(0, total_pages - ref_start + 1)
        # If References starts in the middle or bottom of a page, the main text
        # still occupies that page for venue body-page accounting.
        body_pages = max(0, ref_start - 1 if ref_info.get("starts_netop") else ref_start)
        allowed_reference_pages = min(estimated_reference_pages, ref_page_max) if ref_page_max else estimated_reference_pages
        overflow_reference_pages = max(0, estimated_reference_pages - ref_page_max) if ref_page_max else 0
        method = "pdftotext_reference_heading_page"
    else:
        if total_pages <= 0:
            estimated_reference_pages = 0
        elif citation_count <= 0:
            estimated_reference_pages = 0
        else:
            estimated_reference_pages = max(1, (citation_count + max(1, refs_per_page) - 1) // max(1, refs_per_page))
        allowed_reference_pages = min(estimated_reference_pages, ref_page_max) if ref_page_max else estimated_reference_pages
        body_pages = max(0, total_pages - allowed_reference_pages)
        overflow_reference_pages = max(0, estimated_reference_pages - ref_page_max) if ref_page_max else 0
        method = "pdf_pages_minus_estimated_reference_pages_from_reference_count"
    overflow_source = "none"
    body_max = int(policy.get("body_page_max") or 0)
    total_max = int(policy.get("total_page_max") or 0)
    if body_max and body_pages > body_max:
        overflow_source = "body_layout_or_figure_table_footprint"
    elif ref_page_max and estimated_reference_pages > ref_page_max:
        overflow_source = "bibliography_reference_pages"
    elif total_max and total_pages > total_max:
        overflow_source = "total_pages_after_body_and_references"
    return {
        "total_pages": total_pages,
        "estimated_reference_pages": estimated_reference_pages,
        "allowed_reference_pages": allowed_reference_pages,
        "body_pages": body_pages,
        "reference_start_page": ref_start,
        "reference_heading_line_index": int(ref_info.get("line_index") or 0),
        "reference_heading_line_count": int(ref_info.get("line_count") or 0),
        "reference_starts_netop": bool(ref_info.get("starts_netop")),
        "overflow_reference_pages": overflow_reference_pages,
        "overflow_source": overflow_source,
        "estimation_method": method,
    }


def page_cap_label(max_value: int, label: str) -> str:
    return f"{label} <= {max_value}" if max_value > 0 else f"{label}: no hard cap recorded"


def page_range_label(min_value: int, max_value: int, label: str) -> str:
    if min_value > 0 and max_value > 0:
        return f"{label} {min_value}-{max_value}"
    if max_value > 0:
        return f"{label} <= {max_value}"
    if min_value > 0:
        return f"{label} >= {min_value}"
    return f"{label}: no hard range recorded"


def venue_page_rule_label(policy: dict[str, Any]) -> str:
    body_min = int(policy.get("body_page_min") or 0)
    body_max = int(policy.get("body_page_max") or 0)
    ref_max = int(policy.get("reference_page_max") or 0)
    total_max = int(policy.get("total_page_max") or 0)
    return "; ".join([
        page_range_label(body_min, body_max, "main/body pages"),
        page_cap_label(ref_max, "reference pages"),
        page_cap_label(total_max, "total pages"),
    ])


def author_identity_hits(text: str) -> list[str]:
    hits: list[str] = []
    author_match = re.search(r"\\author\{([^{}]+)\}", text, flags=re.DOTALL)
    if author_match:
        value = re.sub(r"\s+", " ", author_match.group(1)).strip()
        lowered = value.lower()
        if value and "anonymous" not in lowered and "anon" not in lowered:
            hits.append(f"author={value[:120]}")
    for pattern in [r"\\affiliation\{([^{}]+)\}", r"\\institution\{([^{}]+)\}", r"\\email\{([^{}]+)\}"]:
        for match in re.finditer(pattern, text, flags=re.DOTALL):
            value = re.sub(r"\s+", " ", match.group(1)).strip()
            if value:
                hits.append(value[:120])
    return hits[:10]



TEXTUAL_CITATION_RE = re.compile(
    r"\\(citet|citeauthor|citeyear|citealp|citealt)\*?\s*(?:\[[^\]]*\]\s*){0,2}\{([^{}]+)\}"
)


def textual_citation_commands(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for match in TEXTUAL_CITATION_RE.finditer(text or ""):
        start = match.start()
        line = (text or "").count("\n", 0, start) + 1
        keys = [key.strip() for key in match.group(2).split(",") if key.strip()]
        rows.append({"command": match.group(1), "keys": keys, "line": line, "snippet": match.group(0)[:160]})
    return rows


def latex_citation_warning_findings(log_text: str) -> dict[str, Any]:
    text = log_text or ""
    author_undefined = sorted(set(re.findall(r"Package natbib Warning:\s*Author undefined for citation`([^']+)'", text)))
    citation_undefined = sorted(set(re.findall(r"(?:LaTeX|Package natbib) Warning:\s*Citation [`']([^`']+)[`'].*?undefined", text)))
    repeated_messages = sorted(set(re.findall(r"There were undefined citations", text, flags=re.IGNORECASE)))
    return {
        "author_undefined_keys": author_undefined,
        "undefined_citation_keys": citation_undefined,
        "undefined_citation_summary_count": len(repeated_messages),
        "warning_count": len(author_undefined) + len(citation_undefined) + len(repeated_messages),
    }


def bibtex_error_findings(log_text: str) -> dict[str, Any]:
    text = log_text or ""
    empty_literal_stack_entries = sorted(set(re.findall(r"You can't pop an empty literal stack for entry\s+([^\s]+)", text)))
    bibtex_error_summaries = re.findall(r"\(There (?:was|were) \d+ error messages?\)", text, flags=re.IGNORECASE)
    fatal_lines: list[str] = []
    fatal_patterns = [
        r"Bib[Tt]eX errors?:[^\n]+",
        r"bibtex\s+[^\n]*errors?:[^\n]+",
        r"I couldn't open (?:style|database) file[^\n]*",
        r"I found no \\citation commands[^\n]*",
        r"I found no \\bibdata command[^\n]*",
        r"I found no \\bibstyle command[^\n]*",
        r"You're missing a field name[^\n]*",
        r"Repeated entry[^\n]*",
    ]
    for pattern in fatal_patterns:
        fatal_lines.extend(re.findall(pattern, text, flags=re.IGNORECASE))
    fatal_lines = sorted(set(line.strip() for line in fatal_lines if line.strip()))
    error_count_match = re.search(r"\(There (?:was|were) (\d+) error messages?\)", text, flags=re.IGNORECASE)
    explicit_error_count = int(error_count_match.group(1)) if error_count_match else 0
    return {
        "empty_literal_stack_entries": empty_literal_stack_entries,
        "bibtex_error_summaries": bibtex_error_summaries,
        "fatal_lines": fatal_lines[:30],
        "error_count": max(explicit_error_count, len(empty_literal_stack_entries), len(fatal_lines)),
    }

def pdf_unresolved_citation_markers(pdf_text: str) -> list[str]:
    markers: list[str] = []
    for line in (pdf_text or "").splitlines():
        compact = re.sub(r"\s+", " ", line).strip()
        lower = compact.lower()
        if not compact:
            continue
        if "author?" in lower or re.search(r"\[(?:\?|\?\?)\]", compact) or "??" in compact:
            markers.append(compact[:240])
    return markers[:30]


def read_pdf_text(path: Path, max_chars: int = 240000) -> str:
    if not path.exists() or not path.is_file():
        return ""
    try:
        proc = subprocess.run(["pdftotext", "-layout", str(path), "-"], text=True, capture_output=True, timeout=45)
        if proc.returncode == 0:
            return (proc.stdout or "")[:max_chars]
    except Exception:
        return ""
    return ""


def _existing_log_paths(output_dir: Path, tex_path: Path | None, state: dict[str, Any]) -> list[Path]:
    """Return logs for the current compile, not stale workspace attempts.

    PaperOrchestra can leave an old `workspace/paper.log` from a failed early
    compile beside a clean `workspace/final/paper.log`. Citation rendering is a
    property of the current PDF/TeX pair, so we use the first tier with existing
    logs and only fall back to older workspace-root logs when no current/final
    log exists.
    """

    def existing(candidates: list[Path]) -> list[Path]:
        seen: set[Path] = set()
        paths: list[Path] = []
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except Exception:
                resolved = candidate
            if resolved in seen or not candidate.exists() or not candidate.is_file():
                continue
            seen.add(resolved)
            paths.append(candidate)
        return paths

    tiers: list[list[Path]] = []
    if tex_path:
        tiers.append([tex_path.with_suffix(".log"), tex_path.parent / "compile.log", tex_path.parent / "paper.log"])
    if output_dir:
        tiers.append([output_dir / "compile.log", output_dir / "paper.log"])
    workspace_value = str(state.get("paper_orchestra_workspace") or "") if isinstance(state, dict) else ""
    if workspace_value:
        workspace = Path(workspace_value)
        tiers.append([workspace / "final" / "paper.log", workspace / "final" / "compile.log"])
        tiers.append([workspace / "compile.log", workspace / "paper.log"])
    for tier in tiers:
        paths = existing(tier)
        if paths:
            return paths
    return []


def citation_render_diagnostics(
    source: str,
    *,
    pdf_path: Path,
    tex_path: Path | None,
    output_dir: Path,
    state: dict[str, Any],
    venue_profile: dict[str, Any],
) -> dict[str, Any]:
    log_paths = _existing_log_paths(output_dir, tex_path, state)
    log_text = "\n".join(read_text(log_path)[-120000:] for log_path in log_paths)
    warning_findings = latex_citation_warning_findings(log_text)
    bibtex_findings = bibtex_error_findings(log_text)
    pdf_text = read_pdf_text(pdf_path)
    pdf_markers = pdf_unresolved_citation_markers(pdf_text)
    textual_commands = textual_citation_commands(source)
    documentclass = venue_profile.get("documentclass", {}) if isinstance(venue_profile, dict) else {}
    options: list[str] = []
    if isinstance(documentclass, dict):
        options.extend(str(item).lower() for item in documentclass.get("options", []) if str(item).strip())
    if isinstance(venue_profile, dict):
        for key in ["required_options", "recommended_options", "documentclass_options", "required_documentclass_options"]:
            raw_options = venue_profile.get(key, [])
            if isinstance(raw_options, list):
                options.extend(str(item).lower() for item in raw_options if str(item).strip())
        template = venue_profile.get("template") if isinstance(venue_profile.get("template"), dict) else {}
        if isinstance(template, dict):
            raw_options = template.get("documentclass_options", [])
            if isinstance(raw_options, list):
                options.extend(str(item).lower() for item in raw_options if str(item).strip())
            bibliography_style = str(template.get("bibliography_style") or "").lower()
            if bibliography_style:
                options.append(bibliography_style)
        bibliography_style = str(venue_profile.get("bibliography_style") or "").lower()
        if bibliography_style:
            options.append(bibliography_style)
    options = sorted(set(options))
    family = str(venue_profile.get("family") or venue_profile.get("template_family") or "").lower() if isinstance(venue_profile, dict) else ""
    numeric_nature_styles = {"sn-nature", "sn-basic", "sn-mathphys-num"}
    numeric_nature_style = family == "springer-nature" and any(item in numeric_nature_styles for item in options)
    blockers: list[dict[str, Any]] = []
    if warning_findings.get("author_undefined_keys"):
        blockers.append({
            "id": "natbib_author_undefined",
            "detail": "natbib Author undefined warnings for keys=" + ", ".join(warning_findings["author_undefined_keys"][:30]),
            "public_detail": "参考文献作者型引用未正确渲染，PDF 会出现 `(author?) [n]`；需要修复引用命令或 bibliography style 后重新编译。",
        })
    if warning_findings.get("undefined_citation_keys") or warning_findings.get("undefined_citation_summary_count"):
        keys = warning_findings.get("undefined_citation_keys") or []
        blockers.append({
            "id": "latex_undefined_citations",
            "detail": "LaTeX undefined citation warnings" + ((" for keys=" + ", ".join(keys[:30])) if keys else ""),
            "public_detail": "LaTeX 编译仍有未解析引用，不能作为正常论文预览展示。",
        })
    if bibtex_findings.get("error_count"):
        entries = bibtex_findings.get("empty_literal_stack_entries") or []
        fatal = bibtex_findings.get("fatal_lines") or []
        detail_parts = []
        if entries:
            detail_parts.append("empty literal stack entries=" + ", ".join(str(item) for item in entries[:30]))
        if fatal:
            detail_parts.append("fatal lines=" + " | ".join(str(item) for item in fatal[:8]))
        if bibtex_findings.get("bibtex_error_summaries"):
            detail_parts.append("summaries=" + " | ".join(str(item) for item in bibtex_findings.get("bibtex_error_summaries", [])[:4]))
        blockers.append({
            "id": "bibtex_compile_errors",
            "detail": "; ".join(detail_parts) or "BibTeX compile errors were found in paper logs",
            "public_detail": "BibTeX/参考文献样式编译仍有错误（例如 Springer Nature sn-nature.bst 的 empty literal stack），即使 PDF 文件存在也不能作为正常论文预览；需要修复 refs.bib 字段、引用命令或模板兼容性后重新编译。",
        })
    if pdf_markers:
        blockers.append({
            "id": "pdf_unresolved_citation_markers",
            "detail": "PDF unresolved citation markers: " + " | ".join(pdf_markers[:8]),
            "public_detail": "PDF 正文含 `(author?)`、`[?]` 或 `??` 等未解析引用标记，需要重新修订并编译。",
        })
    if numeric_nature_style and textual_commands:
        sample = [f"line {row['line']}: \\{row['command']}{{{','.join(row['keys'])}}}" for row in textual_commands[:12]]
        blockers.append({
            "id": "nature_numeric_style_textual_citations",
            "detail": "Springer Nature numeric bibliography style should not use textual natbib citation commands that require author metadata: " + "; ".join(sample),
            "public_detail": "Nature 数字引用模板下检测到 `\\citet`/作者型引用命令，容易生成 `(author?) [n]`；应改为正常叙述加 `\\citep` 或纯数字引用。",
        })
    return {
        "status": "pass" if not blockers else "block",
        "blockers": blockers,
        "log_paths": [str(path) for path in log_paths],
        "latex_warnings": warning_findings,
        "bibtex_errors": bibtex_findings,
        "pdf_unresolved_markers": pdf_markers,
        "textual_citation_commands": textual_commands[:40],
        "numeric_nature_style": numeric_nature_style,
    }

def source_metrics(source_path: Path) -> dict[str, Any]:
    text = read_text(source_path)
    headings = markdown_headings(text) if source_path.suffix.lower() in {".md", ".markdown"} else latex_sections(text)
    paragraphs = [chunk.strip() for chunk in re.split(r"\n\s*\n", text) if len(chunk.strip()) > 120]
    return {
        "source_path": str(source_path),
        "heading_count": len(headings),
        "headings": headings[:40],
        "paragraph_count": len(paragraphs),
        "word_count": len(re.findall(r"\b\w+\b", text)),
        "citation_count": count_citations(text),
    }


def find_sources(project: str, venue: str) -> tuple[Path | None, Path | None]:
    paper = ensure_paper_dirs(project)
    state = get_active_paper_state(project, venue=venue)
    venue_slug = slugify(venue) if venue else str(state.get("venue_slug") or state.get("active_venue") or "")
    output_dir = paper["output_dir"] / venue_slug if venue_slug else paper["output_dir"]
    tex = Path(state.get("rendered_tex") or state.get("conference_preview_tex") or output_dir / "paper.tex")
    markdown_candidates = [paper["revised_md"], paper["draft_md"]]
    markdown = next((path for path in markdown_candidates if path.exists() and read_text(path).strip()), None)
    return (markdown, tex if tex.exists() else None)


def _venue_zh_label(venue: str, policy: dict[str, object]) -> str:
    family = str(policy.get("template_family") or "").lower() if isinstance(policy, dict) else ""
    slug = str(venue or "").lower()
    return "期刊" if family == "springer-nature" or "nature" in slug or "journal" in slug else "会议"


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit whether the generated paper looks like a normal conference paper instead of a workflow process dump.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--venue", default="")
    parser.add_argument("--min-pages", type=int, default=8)
    parser.add_argument("--max-pages", type=int, default=12)
    parser.add_argument("--min-references", type=int, default=0, help="Optional explicit reference target; default reads current venue_requirements.json.")
    parser.add_argument("--min-words", type=int, default=0)
    parser.add_argument("--max-main-sections", type=int, default=7)
    parser.add_argument("--draft-only", action="store_true")
    args = parser.parse_args()
    os.environ["PROJECT_ID"] = args.project

    paths = build_paths(args.project)
    paper = ensure_paper_dirs(args.project)
    state = get_active_paper_state(args.project, venue=args.venue)
    venue = args.venue or str(state.get("venue") or "")
    venue_slug = slugify(venue) if venue else str(state.get("venue_slug") or state.get("active_venue") or "")
    output_dir = paper["output_dir"] / venue_slug if venue_slug else paper["output_dir"]
    pdf_path = Path(state.get("pdf_path") or output_dir / "paper.pdf")
    markdown_path, tex_path = find_sources(args.project, venue)
    source_path = tex_path or markdown_path
    source = tex_text(tex_path) if tex_path else read_text(markdown_path) if markdown_path else ""
    headings = latex_sections(source) if tex_path else markdown_headings(source)
    normalized_headings = [normalize_heading(item) for item in headings]
    bad_headings = [item for item in headings if any(term in normalize_heading(item) for term in BAD_TOP_LEVEL_TERMS)]
    pages = pdf_pages(pdf_path)
    citation_count = count_citations(source)
    bib_count = bibliography_entry_count(
        tex_path,
        output_dir / "refs.bib",
        (Path(str(state.get("paper_orchestra_workspace") or "")) / "refs.bib") if state.get("paper_orchestra_workspace") else None,
        (Path(str(state.get("paper_orchestra_workspace") or "")) / "final" / "refs.bib") if state.get("paper_orchestra_workspace") else None,
    )
    citation_count = max(citation_count, bib_count)
    bib_paths = [
        output_dir / "refs.bib",
        (Path(str(state.get("paper_orchestra_workspace") or "")) / "refs.bib") if state.get("paper_orchestra_workspace") else None,
        (Path(str(state.get("paper_orchestra_workspace") or "")) / "final" / "refs.bib") if state.get("paper_orchestra_workspace") else None,
    ]
    cited_keys = citation_keys(source)
    available_bib_keys = bib_keys_from_paths(*bib_paths)
    missing_citation_keys = sorted(key for key in cited_keys if key not in available_bib_keys)
    metrics = source_metrics(source_path) if source_path else {}
    venue_template_validation = validate_venue_template_format(source, venue, project=args.project) if venue else {"status": "pass", "failures": [], "warnings": []}
    venue_profile = venue_template_profile(venue, project=args.project) if venue else {}
    venue_policy = venue_submission_policy(venue, project=args.project) if venue else {}
    citation_render = citation_render_diagnostics(
        source,
        pdf_path=pdf_path,
        tex_path=tex_path,
        output_dir=output_dir,
        state=state if isinstance(state, dict) else {},
        venue_profile=venue_profile if isinstance(venue_profile, dict) else {},
    )
    citation_render_blockers = citation_render.get("blockers", []) if isinstance(citation_render.get("blockers"), list) else []
    venue_zh = _venue_zh_label(venue, venue_policy)
    policy_status = str(venue_policy.get("status") or "")
    required_main_sections = [str(item).strip().lower() for item in venue_policy.get("canonical_sections", []) if str(item).strip()] if isinstance(venue_policy, dict) else []
    if not required_main_sections:
        required_main_sections = CANONICAL_MAIN_SECTIONS
    missing_sections = [section for section in required_main_sections if not has_canonical_section(headings, section)]
    known_policy = policy_status == "known"
    body_min = int(venue_policy.get("body_page_min") or (args.min_pages if not known_policy else 0))
    body_max = int(venue_policy.get("body_page_max") or (args.max_pages if not known_policy else 0))
    total_max = int(venue_policy.get("total_page_max") or (args.max_pages if not known_policy else 0))
    ref_max = int(venue_policy.get("reference_page_max") or 0)
    reference_target_info = venue_reference_target(venue, project=args.project, explicit_min=args.min_references) if venue else {"target": int(args.min_references or 0), "source": "explicit_cli" if args.min_references else "none", "official_min_references": 0, "reference_quality_target": 0}
    min_references = int(reference_target_info.get("target") or 0)
    official_min_references = int(reference_target_info.get("official_min_references") or 0)
    reference_quality_target = int(reference_target_info.get("reference_quality_target") or 0)
    reference_target_source = str(reference_target_info.get("source") or "none")
    reference_check_id = "reference_count" if reference_target_source == "official" else "reference_quality_target"
    reference_detail = (
        f"references/citation keys={citation_count}; target >= {min_references}; "
        f"target_source={reference_target_source}; official_min={official_min_references}; quality_target={reference_quality_target}"
    )
    reference_public_detail = (
        f"参考文献覆盖不足：当前 {citation_count}/{min_references}，需要补充真实且相关的已验证引用。"
        if min_references and citation_count < min_references else
        f"参考文献覆盖达到目标：{citation_count}/{min_references}。" if min_references else ""
    )
    min_word_count = int(venue_policy.get("min_word_count") or args.min_words or 0)
    max_main_sections = int(venue_policy.get("max_main_sections") or args.max_main_sections)
    page_breakdown = estimate_page_breakdown(pages, citation_count, venue_policy, pdf_path)
    nature_article_shape_failures = springer_nature_article_shape_failures(source, venue, project=args.project) if tex_path else []
    if tex_path:
        pdf_front_matter_failures, pdf_first_page = springer_nature_pdf_front_matter_failures(pdf_path, source, venue, project=args.project)
    else:
        pdf_front_matter_failures, pdf_first_page = [], {"skipped": True}
    body_pages = int(page_breakdown.get("body_pages") or 0)
    estimated_reference_pages = int(page_breakdown.get("estimated_reference_pages") or 0)
    identity_hits = author_identity_hits(source)
    body_page_diagnostic = ""
    if body_pages and body_max:
        if body_pages <= body_max:
            body_page_diagnostic = f"正文页数符合当前{venue_zh}官方要求：{body_pages}/{body_max}；应优先检查图表/表格占地、参考文献覆盖和参考文献排版密度。"
        else:
            body_page_diagnostic = f"正文页数超过当前{venue_zh}官方要求：{body_pages}/{body_max}；先诊断图表/表格占地，再决定是否调整正文。"
    elif body_pages:
        body_page_diagnostic = f"正文页数={body_pages}；当前{venue_zh}官方正文页数上限尚未解析完成。"
    reference_quality_diagnostic = ""
    if min_references:
        ref_label = "官方引用要求" if reference_target_source == "official" else "写作引用质量目标"
        reference_quality_diagnostic = f"{ref_label}：{citation_count}/{min_references}。"

    checks = [
        {
            "id": "paper_source_exists",
            "status": "pass" if bool(source_path and source.strip()) else "block",
            "detail": f"source_path={source_path or ''}",
        },
        {
            "id": "venue_policy_known",
            "status": "pass" if policy_status == "known" or not venue else "block",
            "detail": venue_policy_source_detail(venue_policy),
        },
        {
            "id": "body_page_count_in_range",
            "status": "pass" if args.draft_only or ((not body_min or body_pages >= body_min) and (not body_max or body_pages <= body_max)) else "block",
            "detail": "skipped for draft-only audit" if args.draft_only else f"body_pages={body_pages}; expected {page_range_label(body_min, body_max, 'main/body pages')}; total_pages={pages}; estimated_reference_pages={estimated_reference_pages}; reference_start_page={page_breakdown.get('reference_start_page') or 0}; overflow_source={page_breakdown.get('overflow_source')}",
        },
        {
            "id": "total_page_limit",
            "status": "pass" if args.draft_only or not total_max or pages <= total_max else "block",
            "detail": "skipped for draft-only audit" if args.draft_only else f"total_pages={pages}; {page_cap_label(total_max, 'total pages')}",
        },
        {
            "id": "reference_page_limit",
            "status": "pass" if args.draft_only or not ref_max or estimated_reference_pages <= ref_max else "block",
            "detail": "skipped for draft-only audit" if args.draft_only else f"estimated_reference_pages={estimated_reference_pages}; {page_cap_label(ref_max, 'reference pages')}; references={citation_count}",
        },
        {
            "id": "venue_template_format",
            "status": "pass" if venue_template_validation.get("status") == "pass" else "block",
            "detail": "format="
            + str(venue_profile.get("format_label") or venue or "venue")
            + "; "
            + (
                "documentclass="
                + str(venue_template_validation.get("documentclass", {}).get("class", ""))
                + "; options="
                + ",".join(str(item) for item in venue_template_validation.get("documentclass", {}).get("options", []))
                if venue_template_validation.get("status") == "pass"
                else "failures=" + "; ".join(str(item) for item in venue_template_validation.get("failures", []))
            ),
        },
        {
            "id": "pdf_front_matter_rendered",
            "status": "pass" if not pdf_front_matter_failures else "block",
            "detail": "PDF first page front matter is consistent with the venue template" if not pdf_front_matter_failures else "; ".join(pdf_front_matter_failures),
            "public_detail": "PDF 首页标题/摘要区渲染异常，需要由 writing 重新生成当前 venue 预览。" if pdf_front_matter_failures else "",
        },
        {
            "id": "nature_family_article_shape",
            "status": "pass" if not nature_article_shape_failures else "block",
            "detail": "Nature-family article structure is consistent with the resolved venue contract" if not nature_article_shape_failures else "; ".join(nature_article_shape_failures),
            "public_detail": "Nature-family 文章形态不合格：" + "; ".join(nature_article_shape_failures) if nature_article_shape_failures else "",
        },
        {
            "id": reference_check_id,
            "status": "pass" if not min_references or citation_count >= min_references else "block",
            "detail": reference_detail,
            "public_detail": reference_public_detail,
        },
        {
            "id": "citation_keys_resolved",
            "status": "pass" if not missing_citation_keys else "block",
            "detail": "all cited keys resolve to refs.bib" if not missing_citation_keys else "missing bib entries for cited keys=" + ", ".join(missing_citation_keys[:20]),
        },
        {
            "id": "citation_render_clean",
            "status": "pass" if not citation_render_blockers else "block",
            "detail": "compiled PDF/logs have no unresolved citation render markers" if not citation_render_blockers else "; ".join(str(item.get("id", "citation_render")) + ": " + str(item.get("detail", "")) for item in citation_render_blockers[:8]),
            "public_detail": "；".join(str(item.get("public_detail") or item.get("detail") or "") for item in citation_render_blockers[:6]) if citation_render_blockers else "",
        },
        {
            "id": "anonymous_submission",
            "status": "pass" if not venue_policy.get("anonymous_required") or not identity_hits else "block",
            "detail": "anonymous review metadata is clean" if not identity_hits else "possible author identity fields=" + "; ".join(identity_hits[:6]),
        },
        {
            "id": "canonical_main_sections",
            "status": "pass" if not missing_sections else "block",
            "detail": "missing=" + ", ".join(missing_sections) if missing_sections else "all canonical sections present",
        },
        {
            "id": "section_count_not_fragmented",
            "status": "pass" if 4 <= len(headings) <= max_main_sections else "block",
            "detail": f"top-level/main section count={len(headings)}; expected 4-{max_main_sections}",
        },
        {
            "id": "no_process_dump_sections",
            "status": "pass" if not bad_headings else "block",
            "detail": "bad headings=" + "; ".join(bad_headings[:12]) if bad_headings else "no TASTE process headings detected",
        },
        {
            "id": "enough_body_substance",
            "status": "pass" if not min_word_count or int(metrics.get("word_count", 0) or 0) >= min_word_count else "block",
            "detail": f"word_count={metrics.get('word_count', 0)}; " + (f"expected >= {min_word_count}" if min_word_count else "no hard word-count minimum recorded"),
        },
    ]
    failed = [row for row in checks if row["status"] != "pass"]
    status = "pass" if not failed else "blocked"
    public_failed = []
    for row in failed:
        if isinstance(row, dict):
            public_failed.append({
                "id": row.get("id", "check"),
                "status": row.get("status", "block"),
                "public_detail": row.get("public_detail") or row.get("detail") or "",
            })
    payload = {
        "project": args.project,
        "venue": venue,
        "status": status,
        "normal_preview_ready": status == "pass",
        "submission_ready": bool(state.get("submission_ready")) and status == "pass",
        "checks": checks,
        "draft_only": bool(args.draft_only),
        "failed_checks": failed,
        "public_failed_checks": public_failed,
        "pdf_path": str(pdf_path) if pdf_path.exists() else "",
        "tex_path": str(tex_path) if tex_path else "",
        "markdown_path": str(markdown_path) if markdown_path else "",
        "total_pages": pages,
        "pages": pages,
        "body_pages": body_pages,
        "body_page_min": body_min,
        "body_page_max": body_max,
        "estimated_reference_pages": estimated_reference_pages,
        "reference_page_limit": ref_max,
        "total_page_limit": total_max,
        "citation_count": citation_count,
        "reference_target": min_references,
        "reference_quality_target": min_references,
        "reference_target_source": reference_target_source,
        "official_min_references": official_min_references,
        "reference_quality_target": reference_quality_target,
        "body_page_diagnostic": body_page_diagnostic,
        "reference_quality_diagnostic": reference_quality_diagnostic,
        "page_breakdown": page_breakdown,
        "pdf_front_matter_failures": pdf_front_matter_failures,
        "nature_article_shape_failures": nature_article_shape_failures,
        "pdf_first_page_text": pdf_first_page,
        "venue_submission_policy": venue_policy,
        "venue_template_validation": venue_template_validation,
        "venue_template_profile": venue_profile,
        "citation_render_diagnostics": citation_render,
        "paper_citation_render_status": citation_render.get("status"),
        "paper_citation_render_blockers": citation_render_blockers,
        "paper_citation_render_ready": not citation_render_blockers,
        "metrics": {
            **metrics,
            "pdf_path": str(pdf_path) if pdf_path.exists() else "",
            "tex_path": str(tex_path) if tex_path else "",
            "markdown_path": str(markdown_path) if markdown_path else "",
            "pages": pages,
            "body_pages": body_pages,
            "estimated_reference_pages": estimated_reference_pages,
            "reference_page_limit": ref_max,
            "total_page_limit": total_max,
            "body_page_min": body_min,
            "body_page_max": body_max,
            "page_breakdown": page_breakdown,
            "pdf_front_matter_failures": pdf_front_matter_failures,
            "nature_article_shape_failures": nature_article_shape_failures,
            "pdf_first_page_text": pdf_first_page,
            "citation_count": citation_count,
            "reference_target": min_references,
            "reference_target_source": reference_target_source,
            "body_page_diagnostic": body_page_diagnostic,
            "reference_quality_diagnostic": reference_quality_diagnostic,
            "official_min_references": official_min_references,
            "reference_quality_target": reference_quality_target,
            "cited_key_count": len(cited_keys),
            "bib_key_count": len(available_bib_keys),
            "missing_citation_keys": missing_citation_keys,
            "missing_sections": missing_sections,
            "bad_headings": bad_headings,
            "normalized_headings": normalized_headings[:40],
            "anonymous_identity_hits": identity_hits,
            "venue_template_profile": venue_profile,
            "venue_template_validation": venue_template_validation,
            "venue_submission_policy": venue_policy,
            "citation_render_diagnostics": citation_render,
            "citation_render_blockers": citation_render_blockers,
        },
        "principle": "This audit checks paper normality only. It does not make scientific claims true; evidence/readiness gates still apply.",
    }
    out_json = paths.state / "paper_normality_audit.json"
    out_md = paths.reports / "paper_normality_audit.md"
    write_json(out_json, payload)
    lines = [
        "# Paper Normality Audit\n\n",
        f"- status: {status}\n",
        f"- normal_preview_ready: {payload['normal_preview_ready']}\n",
        f"- venue_template_format: {venue_template_validation.get('status')}\n",
        f"- pages: {pages}\n",
        f"- body_pages: {body_pages}\n",
        f"- body_page_diagnostic: {body_page_diagnostic}\n",
        f"- estimated_reference_pages: {estimated_reference_pages}\n",
        f"- citation_count: {citation_count}\n",
        f"- reference_quality_diagnostic: {reference_quality_diagnostic}\n",
        f"- reference_target: {min_references}\n",
        f"- reference_target_source: {reference_target_source}\n",
        f"- official_min_references: {official_min_references}\n",
        f"- reference_quality_target: {reference_quality_target}\n",
        f"- venue_policy: {policy_status}\n",
        f"- venue_page_rule: {venue_page_rule_label(venue_policy)}\n",
        f"- pdf_front_matter_failures: {pdf_front_matter_failures}\n",
        f"- nature_article_shape_failures: {nature_article_shape_failures}\n",
        f"- citation_render_status: {citation_render.get('status')}\n",
        f"- citation_render_blockers: {[item.get('id') for item in citation_render_blockers]}\n",
        f"- source_path: {source_path or ''}\n",
        f"- pdf_path: {pdf_path if pdf_path.exists() else ''}\n",
        "\n## Page Breakdown\n\n",
        f"- reference_start_page: {page_breakdown.get('reference_start_page') or 0}\n",
        f"- reference_starts_netop: {bool(page_breakdown.get('reference_starts_netop'))}\n",
        f"- overflow_source: {page_breakdown.get('overflow_source')}\n",
        f"- estimation_method: {page_breakdown.get('estimation_method')}\n",
        "\n## Checks\n\n",
    ]
    for row in checks:
        lines.append(f"- [{row['status']}] {row['id']}: {row['detail']}\n")
    if bad_headings:
        lines.append("\n## Desk-Reject Risk Headings\n\n")
        for heading in bad_headings:
            lines.append(f"- {heading}\n")
    write_text(out_md, "".join(lines))
    update_pipeline_state(
        args.project,
        {
            "paper_normality_status": status,
            "paper_normality_ready": status == "pass",
            "paper_normality_report": str(out_md),
            "paper_normality_audit": str(out_json),
            "paper_normality_pages": pages,
            "paper_normality_body_pages": body_pages,
            "paper_normality_estimated_reference_pages": estimated_reference_pages,
            "paper_venue_page_policy": venue_policy,
            "paper_venue_page_breakdown": page_breakdown,
            "paper_normality_citation_count": citation_count,
            "paper_normality_reference_target": min_references,
            "paper_normality_reference_target_source": reference_target_source,
            "paper_body_page_diagnostic": body_page_diagnostic,
            "paper_reference_quality_diagnostic": reference_quality_diagnostic,
            "paper_reference_quality_target": reference_quality_target,
            "paper_reference_official_min": official_min_references,
            "paper_venue_format_status": venue_template_validation.get("status"),
            "paper_citation_render_status": citation_render.get("status"),
            "paper_citation_render_diagnostics": citation_render,
            "paper_citation_render_blockers": citation_render_blockers,
            "paper_venue_format_profile": venue_profile,
            "paper_venue_format_validation": venue_template_validation,
            "venue_template_format_ready": venue_template_validation.get("status") == "pass",
            "venue_submission_policy_status": policy_status,
            "venue_submission_policy": venue_policy,
            "venue_desk_reject_risks": venue_policy.get("desk_reject_risks", []) if isinstance(venue_policy, dict) else [],
            "normal_preview_ready": status == "pass",
        },
        venue=venue,
        promote_to_top=True,
    )
    print(out_md)
    return 0 if status == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
