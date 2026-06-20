#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path as _WritingDevPath
import sys as _writing_sys
_WRITING_SCRIPT_ROOT = next((p for p in _WritingDevPath(__file__).resolve().parents if p.name == "scripts"), _WritingDevPath(__file__).resolve().parent)
for _writing_path in [_WRITING_SCRIPT_ROOT, *[p for p in _WRITING_SCRIPT_ROOT.iterdir() if p.is_dir()]]:
    _writing_text = str(_writing_path)
    if _writing_text not in _writing_sys.path:
        _writing_sys.path.insert(0, _writing_text)

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from writing_paths import MODULE_ROOT

CITE_RE = re.compile(r"\\cite\w*\*?\{([^{}]+)\}")
BIB_RE = re.compile(r"@\w+\s*\{\s*([^,\s]+)", re.M)
TEXT_EXTENSIONS = {".md", ".txt", ".csv", ".json", ".tex", ".bib", ".yaml", ".yml"}
BAD_TERMS = [
    "TODO", "TBD", "placeholder", "lorem ipsum", "gate report", "blocked", "preview draft",
    "inspection", "workflow", "pipeline status", "future work plan", "unverified claim",
]
PLACEHOLDER_PATTERNS = [
    ("假作者名 Author One/Two/Three", r"\bAuthor\s+(?:One|Two|Three|Four|[0-9]+)\b"),
    ("假机构 Institution Name", r"\bInstitution Name\b"),
    ("假邮箱 author.one@institution.edu", r"author\.one@institution\.edu"),
    ("假邮编 00000", r"\b00000\b"),
    ("占位 GitHub 链接", r"github\.com/placeholder"),
    ("方括号占位说明", r"\[(?:Institution Name|Names|Author|Authors|TODO|TBD)[^\]]*\]"),
    ("空地址字段", r"\\(?:street|city|postcode|state|country)\{\}"),
]
UNSUPPORTED_RELEASE_PATTERNS = [
    ("未提供公开地址却声称框架/代码/基础设施已发布", r"\b(?:full\s+)?(?:ERIR\s+)?(?:framework|framework design|framework implementation|reproducible infrastructure|code|artifacts)[^.]{0,140}\b(?:are|is|has been|have been)\s+released\b"),
]

OVERCLAIM_PATTERNS = [
    ("state-of-the-art", r"state[- ]of[- ]the[- ]art"),
    ("outperform", r"\boutperform\w*\b"),
    ("surpass", r"\bsurpass(?:es|ed|ing)?\b"),
    ("significant improvement", r"significant(?:ly)?\s+improv"),
    ("superior to", r"\bsuperior\s+to\b"),
    ("first framework/method", r"\bfirst\s+(?:framework|method|approach|system)\b"),
    ("多数据集验证/校准", r"\b(?:validated?|verified|evaluated|calibrat\w+)\b.{0,90}\b(?:across|on)\b.{0,90}\b(?:four|multiple|several)\s+datasets\b"),
    ("0.013 秒级延迟", r"\b0\.013\s*(?:s|sec|secs|second|seconds)\b"),
    ("百倍/两个数量级加速", r"\b(?:100[- ]?fold|two orders of magnitude)\b"),
    ("保留/维持准确率收益", r"\b(?:retain\w*|maintain\w*|preserv\w*)\b.{0,90}\baccuracy\b.{0,50}\b(?:benefit|gain|performance)s?\b"),
]
EVIDENCE_LIMITED_PATTERNS = [
    r"没有超过", r"未超过", r"不能支撑", r"暂不能支撑", r"未能支撑", r"不足以支撑",
    r"弱结果", r"负结果", r"不显著", r"没有稳定收益", r"未观察到稳定",
    r"did not (?:beat|improve|outperform|surpass)", r"failed to (?:beat|improve|outperform|surpass)",
    r"no (?:meaningful |consistent |statistically significant )?improvement", r"negative result",
]


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def pdf_pages(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        proc = subprocess.run(["pdfinfo", str(path)], text=True, capture_output=True, timeout=30)
    except Exception:
        return 0
    match = re.search(r"^Pages:\s*(\d+)\s*$", proc.stdout, re.M)
    return int(match.group(1)) if match else 0


def citation_keys(tex: str) -> set[str]:
    keys: set[str] = set()
    for match in CITE_RE.finditer(tex):
        keys.update(k.strip() for k in match.group(1).split(",") if k.strip())
    return keys


def bib_keys(bib: str) -> set[str]:
    return {m.group(1).strip() for m in BIB_RE.finditer(bib)}


def term_hits(terms: list[str], text: str) -> list[str]:
    lower = text.lower()
    return [term for term in terms if term.lower() in lower]


def regex_hits(patterns: list[tuple[str, str]], text: str) -> list[str]:
    hits: list[str] = []
    for label, pattern in patterns:
        if re.search(pattern, text, flags=re.I | re.S):
            hits.append(label)
    return hits


def collect_input_text(inputs_dir: Path, *, max_chars: int = 800_000) -> str:
    if not inputs_dir.exists():
        return ""
    parts: list[str] = []
    total = 0
    for path in sorted(inputs_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        chunk = read_text(path)
        if not chunk:
            continue
        remaining = max_chars - total
        if remaining <= 0:
            break
        parts.append(chunk[:remaining])
        total += min(len(chunk), remaining)
    return "\n".join(parts)


def evidence_is_limited(input_text: str) -> bool:
    return any(re.search(pattern, input_text, flags=re.I | re.S) for pattern in EVIDENCE_LIMITED_PATTERNS)


def venue_reference_floor(kind: str) -> int:
    if kind in {"iclr", "nature"}:
        return 30
    return 20


def venue_kind(venue: str, requirements: dict[str, Any]) -> str:
    text = " ".join([venue, json.dumps(requirements, ensure_ascii=False)]).lower()
    if "nature" in text or "springer" in text:
        return "nature"
    if "iclr" in text:
        return "iclr"
    return "generic"


def audit(run_dir: Path) -> dict[str, Any]:
    workspace = run_dir / "workspace"
    final = workspace / "final"
    audits = workspace / "audits"
    tex_path = final / "paper.tex"
    pdf_path = final / "paper.pdf"
    bib_path = workspace / "refs.bib"
    if not bib_path.exists():
        bib_path = final / "refs.bib"
    req_path = run_dir / "venue" / "venue_requirements.json"
    source_dir = run_dir / "venue" / "template_source"
    source_json = run_dir / "venue" / "template_source.json"
    tex = read_text(tex_path)
    bib = read_text(bib_path)
    req = load_json(req_path, {})
    page_audit_path = audits / "page_audit.json"
    page_audit = load_json(page_audit_path, {})
    claim_audit_path = audits / "claim_evidence_audit.json"
    claim_audit = load_json(claim_audit_path, {})
    manifest = load_json(run_dir / "run_manifest.json", {})
    venue = str(manifest.get("venue") or req.get("venue") or "")
    kind = venue_kind(venue, req if isinstance(req, dict) else {})
    cites = citation_keys(tex)
    bibs = bib_keys(bib)
    missing_bib = sorted(cites - bibs)
    uncited = sorted(bibs - cites)
    pages = pdf_pages(pdf_path)
    input_text = collect_input_text(workspace / "inputs")
    limited_evidence = evidence_is_limited(input_text)
    bad_hits = term_hits(BAD_TERMS, tex)
    placeholder_hits = regex_hits(PLACEHOLDER_PATTERNS, tex)
    overclaim_hits = regex_hits(OVERCLAIM_PATTERNS, tex)
    unsupported_release_hits = regex_hits(UNSUPPORTED_RELEASE_PATTERNS, tex)
    sections = re.findall(r"\\section\*?\{([^{}]+)\}", tex)
    body_page_max = 0
    try:
        body_page_max = int(((req.get("page_policy") or {}).get("body_page_max") or (req.get("venue_submission_policy") or {}).get("body_page_max") or 0))
    except Exception:
        body_page_max = 0
    reference_target = 0
    try:
        reference_target = int(((req.get("citation_policy") or {}).get("min_verified_references") or (req.get("venue_submission_policy") or {}).get("reference_quality_target") or 0))
    except Exception:
        reference_target = 0
    effective_reference_target = max(reference_target, venue_reference_floor(kind))
    blockers: list[str] = []
    warnings: list[str] = []
    if not tex_path.exists():
        blockers.append("缺少 final/paper.tex")
    if not pdf_path.exists():
        blockers.append("缺少 final/paper.pdf")
    if not req_path.exists():
        blockers.append("缺少 venue_requirements.json")
    if not source_dir.exists() and not source_json.exists():
        blockers.append("缺少官方模板来源记录")
    if len(cites) == 0:
        blockers.append("正文没有 LaTeX 引用")
    if missing_bib:
        blockers.append(f"正文引用缺少 BibTeX 条目: {missing_bib[:12]}")
    if effective_reference_target and len(bibs) < effective_reference_target:
        blockers.append(f"BibTeX 条目数 {len(bibs)} 低于目标 {effective_reference_target}")
    if effective_reference_target and len(cites) < effective_reference_target:
        blockers.append(f"正文实际引用去重数 {len(cites)} 低于目标 {effective_reference_target}")
    if len(bibs) < 20:
        warnings.append(f"BibTeX 条目偏少: {len(bibs)}")
    if bad_hits:
        blockers.append(f"正文含项目管理/占位词: {bad_hits}")
    if placeholder_hits:
        blockers.append(f"正文/首页含假作者、假机构或占位元数据: {placeholder_hits}")
    if unsupported_release_hits:
        blockers.append(f"正文含未核实的发布/开放可用性表述: {unsupported_release_hits}")
    if not page_audit_path.exists():
        blockers.append("缺少 page_audit.json，未证明正文页数达标")
    elif str(page_audit.get("status", "")).lower() in {"blocked", "over_limit", "fail", "failed"}:
        blockers.append(f"页数审计未通过: {page_audit.get('status')}")
    if not claim_audit_path.exists():
        blockers.append("缺少 claim_evidence_audit.json，未建立主要 claim 到实验/文献证据的台账")
    if overclaim_hits and limited_evidence:
        blockers.append(f"输入证据有限但正文含强 claim: {overclaim_hits}")
    elif overclaim_hits:
        warnings.append(f"正文含高风险强 claim 词，需要人工确认是否有证据: {overclaim_hits}")
    if pages <= 0:
        warnings.append("无法读取 PDF 页数")
    if kind == "iclr":
        needed = ["Introduction", "Related", "Method", "Experiment"]
        for item in needed:
            if not any(item.lower() in sec.lower() for sec in sections):
                blockers.append(f"ICLR 论文缺少章节: {item}")
        if body_page_max and pages and pages > body_page_max + 3:
            warnings.append(f"PDF 总页数 {pages} 可能超出 ICLR 正文页数上限 {body_page_max}，需人工看 page_audit")
    if kind == "nature":
        for item in ["Results", "Discussion", "Methods"]:
            if not any(item.lower() in sec.lower() for sec in sections):
                blockers.append(f"Nature 正刊形状缺少章节: {item}")
        intro_declared = any("Introduction".lower() in sec.lower() for sec in sections)
        intro_declared = intro_declared or "Introduction".lower() in json.dumps(page_audit, ensure_ascii=False).lower()
        if not intro_declared:
            warnings.append("Nature 正刊稿件未检测到显式 Introduction；若采用无标题导言，需在 page_audit 中说明")
        if any("Related Work".lower() in sec.lower() for sec in sections):
            warnings.append("Nature 正刊稿件出现会议式 Related Work 顶层章节")
    status = "pass" if not blockers else "blocked"
    payload = {
        "status": status,
        "run_dir": str(run_dir),
        "venue": venue,
        "venue_kind": kind,
        "paper_tex": str(tex_path),
        "paper_pdf": str(pdf_path),
        "pdf_pages": pages,
        "sections": sections,
        "citation_count": len(cites),
        "bib_entry_count": len(bibs),
        "missing_bib_keys": missing_bib,
        "uncited_bib_keys": uncited[:80],
        "reference_target": reference_target,
        "effective_reference_target": effective_reference_target,
        "limited_evidence_detected": limited_evidence,
        "claim_evidence_audit_exists": claim_audit_path.exists(),
        "claim_evidence_audit_status": claim_audit.get("status") if isinstance(claim_audit, dict) else None,
        "body_page_max": body_page_max,
        "blockers": blockers,
        "warnings": warnings,
        "official_template_recorded": source_dir.exists() or source_json.exists(),
        "venue_requirements_exists": req_path.exists(),
    }
    write_json(audits / "standalone_quality_audit.json", payload)
    lines = [
        "# Standalone Paper Quality Audit", "",
        f"- status: {status}",
        f"- venue: {venue}",
        f"- venue_kind: {kind}",
        f"- pdf_pages: {pages}",
        f"- citation_count: {len(cites)}",
        f"- bib_entry_count: {len(bibs)}",
        f"- reference_target: {reference_target}",
        f"- effective_reference_target: {effective_reference_target}",
        f"- limited_evidence_detected: {limited_evidence}",
        f"- claim_evidence_audit_exists: {claim_audit_path.exists()}",
        "", "## Blockers", "",
    ]
    lines.extend(f"- {item}" for item in blockers) if blockers else lines.append("- none")
    lines.extend(["", "## Warnings", ""])
    lines.extend(f"- {item}" for item in warnings) if warnings else lines.append("- none")
    lines.extend(["", "## Sections", ""])
    lines.extend(f"- {item}" for item in sections) if sections else lines.append("- none")
    write_text(audits / "standalone_quality_audit.md", "\n".join(lines))
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="审计 writing standalone 生成论文的基础质量。")
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args()
    run_dir = Path(args.run_dir).expanduser().resolve()
    module_runs = (MODULE_ROOT / "runs").resolve()
    if module_runs != run_dir and module_runs not in run_dir.parents:
        raise SystemExit("run-dir 必须位于 modules/writing/runs 内。")
    payload = audit(run_dir)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("status") == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
