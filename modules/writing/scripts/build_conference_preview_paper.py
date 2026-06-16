#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from paper_common import (
    get_active_paper_state,
    slugify,
    springer_nature_article_shape_failures,
    springer_nature_front_matter_failures,
    springer_nature_pdf_front_matter_failures,
    update_pipeline_state,
    venue_submission_policy,
    write_text,
)
from project_paths import ROOT, build_paths
from pipeline_guard import guard_fresh_base_blocker_entry
from paper_self_review import validate_paper_self_review_receipt


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


DEFAULT_CANONICAL_MANUSCRIPT_SECTIONS = ["introduction", "related work", "method", "experiments", "conclusion"]


def _normalized_latex_sections(text: str) -> list[str]:
    headings = re.findall(r"\\section\*?\{([^{}]+)\}", text)
    return [re.sub(r"[^a-z0-9 ]+", " ", item.lower()).strip() for item in headings]

def _canonical_sections_for_venue(project: str, venue: str) -> list[str]:
    policy = venue_submission_policy(venue, project=project) if venue else {}
    raw = policy.get("canonical_sections", policy.get("paper_shape", {}).get("canonical_sections")) if isinstance(policy, dict) else []
    if raw is None:
        raw = []
    sections = [re.sub(r"[^a-z0-9 ]+", " ", str(item).lower()).strip() for item in raw if str(item).strip()]
    return sections or DEFAULT_CANONICAL_MANUSCRIPT_SECTIONS


def _heading_matches_required(required: str, headings: list[str]) -> bool:
    aliases = {required}
    if required.endswith("s"):
        aliases.add(required[:-1])
    if required == "method":
        aliases.add("methods")
    if required == "methods":
        aliases.add("method")
    if required == "experiments":
        aliases.update({"experiment", "experimental results", "results"})
    if required == "results":
        aliases.update({"result", "experiments", "experimental results"})
    return any(any(alias in heading for alias in aliases if alias) for heading in headings)


ROUTE_STORY_HARD_MARKERS = [
    "current selected route",
    "current route",
    "selected route",
    "selected base",
    "current base",
    "active repository",
    "active repo",
    "current repository",
    "current repo",
    "base-switch",
    "base switch",
    "candidate route",
    "legacy route",
    "non-authoritative",
    "loader",
    "codebase",
    "repo-defined",
]

ROUTE_STORY_ACTION_RE = re.compile(
    r"\b(?:we|our|this work|this paper|the paper|the manuscript|the framework|the method)\b"
    r".{0,140}\b(?:use|uses|used|using|adopt|adopts|adopted|base|based|build|builds|built|implement|implements|implemented|"
    r"run|runs|ran|train|trains|trained|evaluate|evaluates|evaluated|select|selects|selected|switch|switches|switched)\w*\b",
    flags=re.IGNORECASE | re.DOTALL,
)

ROUTE_STORY_TECHNICAL_RE = re.compile(
    r"\b(?:repository|repo|codebase|implementation|environment|loader|dataset|data pipeline|artifact|experiment|backbone|base)\b",
    flags=re.IGNORECASE,
)

PRIOR_WORK_SECTION_WORDS = {"related work", "background", "introduction"}
PRIOR_WORK_CONTEXT_RE = re.compile(
    r"(?:\\cite\w*\{|prior work|existing work|existing method|recent method|previous work|"
    r"proposes|proposed|introduces|introduced|addresses|addressed|constructs|constructed|achieves|achieved|"
    r"demonstrates|demonstrated|reports|reported|shows|showed|leverages|leveraged|formalizes|formalized)",
    flags=re.IGNORECASE,
)


def _route_term_candidates(value: object) -> list[str]:
    raw = str(value or "").strip()
    if not raw:
        return []
    variants: set[str] = set()
    is_url = bool(re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", raw))
    if is_url:
        raw = raw.rstrip("/").split("/")[-1]
    repo_slug = bool(re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?$", raw))
    leaf = re.split(r"[/\\]", raw.rstrip("/\\"))[-1]
    leaf_suffix = Path(leaf).suffix.lower()
    source_items = [leaf]
    if leaf_suffix not in {".json", ".md", ".txt", ".log"}:
        source_items.append(Path(leaf).stem)
    if repo_slug or ("/" not in raw and "\\" not in raw):
        source_items.insert(0, raw)
    for item in source_items:
        cleaned = re.sub(r"\.git$", "", str(item).strip(), flags=re.IGNORECASE)
        cleaned = cleaned.strip(" `[](){}.,;:'\"\n\t")
        if not cleaned or cleaned.lower().endswith((".json", ".md", ".txt", ".log")):
            continue
        # Long paper titles belong in citation/reference metadata, not in the
        # route-name policy. Route-story checks should focus on repo/base labels.
        if " " in cleaned and len(cleaned) > 40:
            continue
        variants.add(cleaned)
    return sorted(variants, key=str.lower)



def _route_string_looks_artifact_path(value: object) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    leaf = re.split(r"[/\\]", raw.rstrip("/\\"))[-1]
    if Path(leaf).suffix.lower() in {".json", ".md", ".txt", ".log"}:
        return True
    parts = {part.lower() for part in re.split(r"[/\\]", raw)}
    return bool(("reports" in parts or "state" in parts) and "repos" not in parts)


def _legacy_route_terms(project: str) -> list[str]:
    state = ROOT / "projects" / project / "state"
    terms: list[str] = []
    for state_name in ["base_switch_execution.json", "base_switch_gate.json", "obsolete_baseline_cleanup_plan.json"]:
        payload = read_json(state / state_name, {})
        if not isinstance(payload, dict):
            continue
        candidates = [payload.get("candidate_route"), payload.get("new_route"), payload.get("invalidated_previous_new_route")]
        candidates.extend(payload.get("blocked_candidate_paths", []) if isinstance(payload.get("blocked_candidate_paths"), list) else [])
        for row in candidates:
            if isinstance(row, dict):
                for value in [row.get("repo"), row.get("name"), row.get("repo_name"), row.get("path"), row.get("repo_path"), row.get("local_path")]:
                    terms.extend(_route_term_candidates(value))
            else:
                if _route_string_looks_artifact_path(row):
                    continue
                terms.extend(_route_term_candidates(row))
    return sorted({item for item in terms if len(item) >= 4}, key=str.lower)


def _latex_section_ranges(text: str) -> list[tuple[int, str]]:
    ranges: list[tuple[int, str]] = []
    for match in re.finditer(r"\\(?:section|subsection|subsubsection|paragraph)\*?\{([^{}]+)\}", text):
        title = re.sub(r"[^a-z0-9 ]+", " ", match.group(1).lower()).strip()
        ranges.append((match.start(), title))
    return ranges


def _section_at(section_ranges: list[tuple[int, str]], index: int) -> str:
    current = ""
    for start, title in section_ranges:
        if start > index:
            break
        current = title
    return current


def _sentence_window(text: str, start: int, end: int, radius: int = 520) -> str:
    left_candidates = [text.rfind(". ", 0, start), text.rfind("\n\n", 0, start), text.rfind("\n", 0, start)]
    left = max(left_candidates)
    if left < 0 or start - left > radius:
        left = max(0, start - radius)
    else:
        left += 1
    right_candidates = [pos for pos in [text.find(". ", end), text.find("\n\n", end), text.find("\n", end)] if pos >= 0]
    right = min(right_candidates) if right_candidates else min(len(text), end + radius)
    if right - end > radius:
        right = min(len(text), end + radius)
    return text[left:right].strip()


def _is_prior_work_context(section: str, window: str) -> bool:
    lowered_section = section.lower()
    if any(word in lowered_section for word in PRIOR_WORK_SECTION_WORDS) and PRIOR_WORK_CONTEXT_RE.search(window):
        return True
    if "\\cite" in window and PRIOR_WORK_CONTEXT_RE.search(window):
        return True
    return False


def _is_route_story_context(section: str, window: str) -> bool:
    lowered = window.lower()
    if any(marker in lowered for marker in ROUTE_STORY_HARD_MARKERS):
        return True
    if ROUTE_STORY_ACTION_RE.search(window) and ROUTE_STORY_TECHNICAL_RE.search(window):
        return True
    unsafe_section = any(word in section for word in ["method", "experiment", "implementation", "result", "reproduc", "protocol"])
    if unsafe_section and ROUTE_STORY_ACTION_RE.search(window):
        return True
    return False


UNSUPPORTED_COMPLETED_EXPERIMENT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "unsupported_completed_evaluation_claim",
        re.compile(
            r"\bwe\s+(?:evaluate|evaluated|benchmark|benchmarked|compare|compared|conduct|conducted|run|ran|report|reported)\b",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "unsupported_experiment_protocol_claim",
        re.compile(r"\ball\s+experiments\s+(?:use|used|are|were|run|ran|report|reported)\b", flags=re.IGNORECASE),
    ),
    (
        "unsupported_repeated_results_claim",
        re.compile(
            r"\b(?:results?|metrics?)\s+(?:are\s+|were\s+)?(?:averaged|reported|shown|show|shows|computed)"
            r".{0,140}\b(?:mean\s*(?:±|\+/-|and)\s*(?:standard\s+deviation|std)|standard\s+deviation|std|random\s+seeds?|seeds?)\b"
            r"|\b(?:mean\s*(?:±|\+/-|and)\s*(?:standard\s+deviation|std)|standard\s+deviation|std)\s+(?:across|over)\s+\d+\s+(?:random\s+)?seeds?\b"
            r"|\brepeated\s+(?:\w+\s+){0,6}(?:three|3|multiple)\s+times\b",
            flags=re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "unsupported_seed_configuration_claim",
        re.compile(
            r"\brandom\s+seeds?\b.{0,100}\b(?:documented|available|recorded|reported|fixed|used)\b"
            r"|\b(?:documented|available|recorded|reported|fixed|used)\b.{0,100}\brandom\s+seeds?\b",
            flags=re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "unsupported_metric_uncertainty_claim",
        re.compile(
            r"\b(?:ndcg|recall|hit|hr|auc|map|mrr|precision|rmse|mae)\s*@?\s*\d*\b.{0,80}(?:±|\\pm|\$\\pm\$|\+/-)\s*\d",
            flags=re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "unsupported_proposed_method_result_claim",
        re.compile(
            r"\b(?:our|the proposed|this)\s+(?:method|framework|approach|model)\b.{0,160}\b(?:achieves|achieve|outperforms|outperform|improves|improve|beats|beat|surpasses|surpass)\b",
            flags=re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "unsupported_hardware_runtime_claim",
        re.compile(
            r"\b(?:experiments?|runs?|training|reference\s+baseline|baseline)\s+(?:use|uses|used|run|runs|ran|is\s+trained|was\s+trained)"
            r".{0,140}\b(?:single\s+)?(?:nvidia\s+)?(?:a100|h100|a800|b200|rtx\s*\d{4}|gpu|tpu)\b"
            r"|\b(?:single\s+)?(?:nvidia\s+)?(?:a100|h100|a800|b200|rtx\s*\d{4})\b"
            r"|\bapproximately\s+\d+(?:\.\d+)?\s+hours?\s+per\b",
            flags=re.IGNORECASE | re.DOTALL,
        ),
    ),
]

REFERENCE_CALIBRATION_CONTEXT_RE = re.compile(
    r"\b(?:reference calibration|reference reproduction|reference baseline|baseline calibration|selected-base reference)\b",
    flags=re.IGNORECASE,
)


def _scientific_progress_allows_completed_experiment_claims(project: str) -> bool:
    progress = read_json(ROOT / "projects" / project / "state" / "scientific_progress_gate.json", {})
    if not isinstance(progress, dict) or str(progress.get("status") or "").lower() != "pass":
        return False
    candidate = progress.get("best_candidate")
    return isinstance(candidate, dict) and bool(candidate)


def _project_claim_support_payloads(project: str) -> list[object]:
    project_state = ROOT / "projects" / project / "state"
    payloads: list[object] = []
    for name in [
        "scientific_progress_gate.json",
        "reference_reproduction_gate.json",
        "fresh_base_reference_full_reproduction_audit.json",
    ]:
        payload = read_json(project_state / name, {})
        if isinstance(payload, (dict, list)):
            payloads.append(payload)
    return payloads


def _walk_json_values(value: object):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key), child
            yield from _walk_json_values(child)
    elif isinstance(value, list):
        for child in value:
            yield "", child
            yield from _walk_json_values(child)


def _explicit_repeated_run_evidence(project: str) -> bool:
    for payload in _project_claim_support_payloads(project):
        for key, value in _walk_json_values(payload):
            normalized_key = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
            if normalized_key in {"seed_count", "num_seeds", "n_seeds", "repeat_count", "num_repeats", "replicate_count", "num_replicates"}:
                try:
                    if int(value) >= 2:
                        return True
                except Exception:
                    pass
            if "seed" in normalized_key and isinstance(value, list) and len({str(item) for item in value}) >= 2:
                return True
            if normalized_key in {"seeds", "random_seeds", "seed_values"} and isinstance(value, str):
                numbers = re.findall(r"\d+", value)
                if len(set(numbers)) >= 2:
                    return True
    return False


def _hardware_tokens(text: str) -> set[str]:
    compact = re.sub(r"\s+", "", text.lower())
    tokens: set[str] = set()
    for token in ["a100", "h100", "a800", "b200", "tpu"]:
        if token in compact:
            tokens.add(token)
    for match in re.finditer(r"rtx\s*([0-9]{4})", text, flags=re.IGNORECASE):
        tokens.add(f"rtx{match.group(1)}")
    return tokens


def _explicit_hardware_evidence(project: str, claim_window: str) -> bool:
    tokens = _hardware_tokens(claim_window)
    if not tokens:
        return False
    evidence = json.dumps(_project_claim_support_payloads(project), ensure_ascii=False).lower()
    evidence_compact = re.sub(r"\s+", "", evidence)
    return all(token in evidence_compact for token in tokens)


def _reference_calibration_available(project: str) -> bool:
    gate = read_json(ROOT / "projects" / project / "state" / "reference_reproduction_gate.json", {})
    if isinstance(gate, dict) and str(gate.get("status") or "").lower() == "pass":
        return True
    audit = read_json(ROOT / "projects" / project / "state" / "fresh_base_reference_full_reproduction_audit.json", {})
    return isinstance(audit, dict) and str(audit.get("status") or audit.get("decision") or "").lower() in {"pass", "passed", "continue_base"}


def _compact_manuscript_window(window: str) -> str:
    text = re.sub(r"\\(?:cite\w*|ref|label)\{[^{}]*\}", "", window)
    text = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?", "", text)
    text = re.sub(r"[{}]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:220]


def unsupported_completed_experiment_claim_violations(project: str, text: str) -> list[str]:
    """Reject completed-experiment prose when no current proposed-method result can support it.

    Preview PDFs may be generated before submission evidence is complete, but the
    manuscript must not describe unrun proposed-method evaluations as finished.
    Reference calibration and cited prior work are handled separately.
    """
    allow_completed_result_claims = _scientific_progress_allows_completed_experiment_claims(project)
    reference_ok = _reference_calibration_available(project)
    section_ranges = _latex_section_ranges(text)
    violations: list[str] = []
    seen: set[str] = set()
    for blocker_id, pattern in UNSUPPORTED_COMPLETED_EXPERIMENT_PATTERNS:
        for match in pattern.finditer(text):
            window = _sentence_window(text, match.start(), match.end(), radius=620)
            section = _section_at(section_ranges, match.start())
            unsafe_experiment_section = any(word in section.lower() for word in ["method", "experiment", "implementation", "result", "reproduc", "protocol"])
            if not unsafe_experiment_section and _is_prior_work_context(section, window):
                continue
            if blocker_id in {"unsupported_repeated_results_claim", "unsupported_seed_configuration_claim", "unsupported_metric_uncertainty_claim"} and _explicit_repeated_run_evidence(project):
                continue
            if blocker_id == "unsupported_hardware_runtime_claim" and _explicit_hardware_evidence(project, window):
                continue
            if blocker_id not in {"unsupported_repeated_results_claim", "unsupported_seed_configuration_claim", "unsupported_metric_uncertainty_claim", "unsupported_hardware_runtime_claim"}:
                if allow_completed_result_claims:
                    continue
                if blocker_id == "unsupported_completed_evaluation_claim" and reference_ok and REFERENCE_CALIBRATION_CONTEXT_RE.search(window):
                    continue
            detail = _compact_manuscript_window(window)
            if not detail:
                continue
            key = f"{blocker_id}:{detail}"
            if key in seen:
                continue
            seen.add(key)
            violations.append(key)
            if len(violations) >= 8:
                return violations
    return violations


def legacy_route_story_violations(project: str, text: str, active_name: str = "") -> list[str]:
    section_ranges = _latex_section_ranges(text)
    violations: list[str] = []
    active_lower = active_name.lower()
    for term in _legacy_route_terms(project):
        term_lower = term.lower()
        if active_lower and term_lower in active_lower:
            continue
        pattern = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(term) + r"(?![A-Za-z0-9_])", flags=re.IGNORECASE)
        for match in pattern.finditer(text):
            window = _sentence_window(text, match.start(), match.end())
            section = _section_at(section_ranges, match.start())
            if _is_route_story_context(section, window):
                violations.append(f"legacy_route_story_in_manuscript:{term}")
                break
            if _is_prior_work_context(section, window):
                continue
            if section and any(word in section for word in ["method", "experiment", "implementation", "result", "reproduc", "protocol"]):
                violations.append(f"legacy_route_story_in_manuscript:{term}")
                break
    return violations


def latex_text_with_inputs(path: Path, *, seen: set[Path] | None = None) -> str:
    if not path.exists() or not path.is_file():
        return ""
    seen = seen or set()
    try:
        resolved = path.resolve()
    except Exception:
        resolved = path
    if resolved in seen:
        return ""
    seen.add(resolved)
    text = path.read_text(encoding="utf-8", errors="replace")

    def repl(match: re.Match[str]) -> str:
        raw = match.group(1).strip()
        if not raw:
            return ""
        candidate = Path(raw)
        if not candidate.suffix:
            candidate = candidate.with_suffix(".tex")
        candidates = [candidate] if candidate.is_absolute() else [path.parent / candidate, path.parent.parent / candidate]
        for item in candidates:
            if item.exists() and item.is_file():
                return "\n" + latex_text_with_inputs(item, seen=seen) + "\n"
        return ""

    return re.sub(r"\\(?:input|include)\{([^{}]+)\}", repl, text)


def pdf_pages(path: Path) -> int:
    if not path.exists() or not path.is_file():
        return 0
    try:
        proc = subprocess.run(["pdfinfo", str(path)], text=True, capture_output=True, timeout=10)
        match = re.search(r"^Pages:\s+(\d+)", proc.stdout, flags=re.MULTILINE)
        if match:
            return int(match.group(1))
    except Exception:
        pass
    return 0


def _candidate_manifest(candidates: list[dict[str, object]]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for row in candidates:
        pdf = Path(str(row.get("pdf") or ""))
        tex = Path(str(row.get("tex") or ""))
        rows.append({
            "label": str(row.get("label") or ""),
            "pdf": str(pdf) if str(pdf) != "." else "",
            "tex": str(tex) if str(tex) != "." else "",
            "pdf_exists": pdf.exists() and pdf.is_file(),
            "tex_exists": tex.exists() and tex.is_file(),
            "pages": int(row.get("pages") or 0),
            "violations": list(row.get("violations") or []),
            "selected": bool(row.get("selected")),
        })
    return rows


def _first_existing_candidate_path(candidate_manifest: list[dict[str, object]], key: str) -> Path | None:
    for row in candidate_manifest:
        path_text = str(row.get(key) or "").strip()
        if not path_text:
            continue
        path = Path(path_text)
        if path.exists() and path.is_file():
            return path
    return None


def _candidate_rejection_blockers(candidate_manifest: list[dict[str, object]]) -> list[dict[str, object]]:
    blockers: list[dict[str, object]] = []
    for row in candidate_manifest:
        violations = [str(item) for item in (row.get("violations") or []) if str(item).strip()]
        if not violations:
            continue
        label = str(row.get("label") or "candidate").strip() or "candidate"
        blockers.append({
            "id": "manuscript_candidate_rejected",
            "status": "block",
            "detail": f"{label}: " + "; ".join(violations[:8]),
            "source": "paper_content_candidate_audit",
            "preview_blocker": True,
            "submission_blocker": True,
        })
    return blockers


def _public_blocker_detail(item: object) -> tuple[str, str]:
    if isinstance(item, dict):
        blocker_id = str(item.get("id") or item.get("category") or "preview_check")
        detail = str(item.get("public_detail") or item.get("detail") or item.get("issue") or "").strip()
        if not detail:
            detail = json.dumps(item, ensure_ascii=False, sort_keys=True)[:500]
        return blocker_id, detail
    text = str(item).strip()
    return text.split(":", 1)[0] if ":" in text else "preview_check", text


def _preview_gate_blockers(
    normality_failures: list[object],
    figure_status: object,
    figure_ready: bool,
    layout_warnings: list[object],
    self_review_blockers: list[object],
) -> list[dict[str, object]]:
    blockers: list[dict[str, object]] = []
    for item in normality_failures[:20]:
        blocker_id, detail = _public_blocker_detail(item)
        blockers.append({"id": blocker_id, "status": "block", "public_detail": detail, "source": "paper_normality_audit"})
    if figure_status and not figure_ready:
        warnings = [str(item) for item in layout_warnings[:3] if str(item).strip()]
        detail = "图表质量审计未通过" + ("：" + "；".join(warnings) if warnings else f"：status={figure_status}")
        blockers.append({"id": "figure_quality", "status": "block", "public_detail": detail, "source": "paper_figure_quality_audit"})
    for item in self_review_blockers[:20]:
        blocker_id, detail = _public_blocker_detail(item)
        blockers.append({"id": blocker_id, "status": "block", "public_detail": detail, "source": "paper_self_review"})
    return blockers


def _preview_blocker_summary(blockers: list[dict[str, object]]) -> str:
    details = [str(item.get("public_detail") or item.get("detail") or "").strip() for item in blockers if str(item.get("public_detail") or item.get("detail") or "").strip()]
    return "；".join(details[:5])


def select_manuscript_candidate(project: str, venue: str, candidates: list[tuple[str, Path, Path]]) -> tuple[str, Path, Path, list[dict[str, object]]]:
    checked: list[dict[str, object]] = []
    for label, pdf, tex in candidates:
        violations: list[str] = []
        if not pdf.exists() or not pdf.is_file():
            violations.append("missing_pdf")
        if not tex.exists() or not tex.is_file():
            violations.append("missing_tex")
        tex_text = ""
        if tex.exists() and tex.is_file():
            tex_text = tex.read_text(encoding="utf-8", errors="replace")
            violations.extend(manuscript_policy_violations(project, tex, venue=venue))
            violations.extend(springer_nature_front_matter_failures(tex_text, venue, project=project))
            violations.extend(springer_nature_article_shape_failures(tex_text, venue, project=project))
        if tex_text and pdf.exists() and pdf.is_file():
            pdf_failures, first_page = springer_nature_pdf_front_matter_failures(pdf, tex_text, venue, project=project)
            violations.extend(pdf_failures)
            if pdf_failures:
                first_lines = first_page.get("lines") if isinstance(first_page.get("lines"), list) else []
                violations.append("pdf_first_page_text:" + " | ".join(str(item) for item in first_lines[:3]))
        pages = pdf_pages(pdf)
        if pages and pages < 8:
            violations.append(f"too_few_pdf_pages:{pages}")
        row: dict[str, object] = {"label": label, "pdf": str(pdf), "tex": str(tex), "violations": violations, "pages": pages, "selected": False}
        checked.append(row)
        if not violations:
            row["selected"] = True
            return label, pdf, tex, _candidate_manifest(checked)
    return "", Path("__missing_manuscript_pdf__"), Path("__missing_manuscript_tex__"), _candidate_manifest(checked)


def manuscript_policy_violations(project: str, tex_path: Path, venue: str = "") -> list[str]:
    if not tex_path.exists() or not tex_path.is_file():
        return ["missing_tex_for_content_policy"]
    raw_text = tex_path.read_text(encoding="utf-8", errors="replace")
    text = latex_text_with_inputs(tex_path) or raw_text
    active = read_json(ROOT / "projects" / project / "state" / "active_repo.json", {})
    active_name = str(active.get("name") or active.get("repo_name") or "") if isinstance(active, dict) else ""
    violations: list[str] = []
    violations.extend(legacy_route_story_violations(project, text, active_name=active_name))
    violations.extend(unsupported_completed_experiment_claim_violations(project, text))
    if re.search(r"\\(?:section|subsection)\*?\{[^{}]*(Failure|Counterexample|负结果|失败)[^{}]*\}", text, flags=re.IGNORECASE):
        violations.append("failure_or_counterexample_section_in_manuscript")
    if re.search(r"\\(?:section|subsection|paragraph)\*?\{[^{}]*Acknowledg", text, flags=re.IGNORECASE):
        violations.append("acknowledgments_section_in_anonymous_preview")
    forbidden_phrases = [
        "failed hypothesis",
        "negative result",
        "negative experiment",
        "hold-markdown",
        "claim promotion",
        "paper evidence audit",
        "submission readiness",
        "internal review verdict",
        "weakest dimensions",
        "editor summary",
        "current contribution story",
        "submission blockers",
        "ar paper blockers",
        "required revision actions",
        "section-by-section revision queue",
        "evidence and experiment snapshot",
        "scope discipline",
        "revised abstract draft",
        "appendix: original draft snapshot",
        "submission status",
        "paper_stage_state",
        "promotion_gate_recommendation",
        "claim ledger",
        "unsupported claims",
        "still unsupported",
        "paper is currently blocked",
        "blocked draft",
        "venue-format inspection artifact",
        "no claim-ready positive experiment",
        "internal audit diagnostics",
        "do not promote legacy-route",
        "blocker",
        "writing module",
        "automated research manuscript",
        "developed using the TASTE",
        "anonymous reviewers for their constructive feedback",
    ]
    lowered = text.lower()
    for phrase in forbidden_phrases:
        if phrase.lower() in lowered:
            violations.append(f"internal_audit_phrase_in_manuscript:{phrase}")

    manuscript_only_forbidden = [
        "empirical superiority claims are deferred",
        "claims are deferred",
        "audit-ready results emerge",
        "success criteria",
        "candidate_observation_only",
        "reference calibration only",
        "audit-ready artifacts",
        "promotable result",
        "requires audit",
        "this draft presents",
        "expected evidence needed",
        "inspection draft",
        "not a submission",
        "blocked draft",
        "preview only",
        "this paper is still under active evidence-building",
        "strongest defensible version",
        "submission-ready headline claim",
        "venue-format inspection artifact",
        "revised abstract draft",
        "revised writing plan",
        "appendix: original draft snapshot",
        "submission status",
        "evidence and experiment snapshot",
        "scope discipline",
        "ablation study design",
        "comprehensive ablation study design",
        "we further design a comprehensive ablation",
        "evaluation matrix enables",
        "email@example.com",
        "city, country",
        "affiliation",
        "affiliation",
    ]
    for phrase in manuscript_only_forbidden:
        if phrase.lower() in lowered:
            violations.append(f"non_manuscript_status_phrase:{phrase}")
    if re.search(r"\\(?:section|subsection|paragraph)\*?\{[^{}]*(Planned|Future Work|Success Criteria|Inspection Draft)[^{}]*\}", text, flags=re.IGNORECASE):
        violations.append("non_manuscript_section_heading")
    if re.search(r"\\(?:section|subsection|paragraph)\*?\{[^{}]*(Ablation Study Design|Experimental Plan|Evaluation Plan|Study Design|Submission Status|Evidence and Experiment Snapshot|Revised Writing Plan|Original Draft Snapshot)[^{}]*\}", text, flags=re.IGNORECASE):
        violations.append("proposal_or_internal_status_section_heading")
    sections = _normalized_latex_sections(text)
    required_sections = _canonical_sections_for_venue(project, venue)
    missing = [section for section in required_sections if not _heading_matches_required(section, sections)]
    if missing:
        violations.append("missing_canonical_manuscript_sections:" + ",".join(missing))
    if "\\begin{abstract}" not in text and "\\abstract{" not in text:
        violations.append("missing_abstract")
    violations.extend(springer_nature_front_matter_failures(text, venue, project=project))
    word_count = len(re.findall(r"\b\w+\b", text))
    if word_count < 3500:
        violations.append(f"too_short_for_conference_manuscript:{word_count}")
    return violations


def run(cmd: list[str], required: bool = False) -> int:
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    if required and proc.returncode != 0:
        raise SystemExit(proc.returncode)
    return int(proc.returncode)


def copy_if_ready(source: Path, target: Path) -> bool:
    if not source.exists() or not source.is_file():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != target.resolve():
        shutil.copy2(source, target)
    return target.exists()


def copy_template_sidecars(source_dir: Path, target_dir: Path) -> list[str]:
    copied: list[str] = []
    suffixes = {'.sty', '.bst', '.cls', '.bbx', '.cbx'}
    asset_suffixes = {'.png', '.pdf', '.jpg', '.jpeg'}
    allowed_asset_dirs = {'figures', 'figure', 'images', 'img'}
    for source in sorted(source_dir.rglob('*') if source_dir.exists() else []):
        if not source.is_file():
            continue
        rel = source.relative_to(source_dir)
        is_template_sidecar = source.suffix.lower() in suffixes or source.name == 'math_commands.tex'
        is_paper_asset = source.suffix.lower() in asset_suffixes and rel.parts and rel.parts[0] in allowed_asset_dirs
        is_repro_script = source.suffix.lower() == '.py' and rel.parts and rel.parts[0] in allowed_asset_dirs and source.name in {'generate_figures.py', 'plot_figures.py'}
        if not (is_template_sidecar or is_paper_asset or is_repro_script):
            continue
        target = target_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(str(target))
    return copied


def _page_cap_label(max_value: object, label: str) -> str:
    try:
        value = int(max_value or 0)
    except Exception:
        value = 0
    return f"{label} <= {value}" if value > 0 else f"{label}: no hard cap recorded"


def _page_range_label(min_value: object, max_value: object, label: str) -> str:
    try:
        lo = int(min_value or 0)
    except Exception:
        lo = 0
    try:
        hi = int(max_value or 0)
    except Exception:
        hi = 0
    if lo > 0 and hi > 0:
        return f"{label} {lo}-{hi}"
    if hi > 0:
        return f"{label} <= {hi}"
    if lo > 0:
        return f"{label} >= {lo}"
    return f"{label}: no hard range recorded"


def venue_page_rule_label(policy: dict[str, object]) -> str:
    return "; ".join([
        _page_range_label(policy.get("body_page_min"), policy.get("body_page_max"), "main/body pages"),
        _page_cap_label(policy.get("reference_page_max"), "reference pages"),
        _page_cap_label(policy.get("total_page_max"), "total pages"),
    ])

def preview_labels(project: str, venue: str) -> dict[str, str]:
    policy = venue_submission_policy(venue, project=project) if venue else {}
    family = str(policy.get("template_family") or "").lower() if isinstance(policy, dict) else ""
    slug = slugify(venue) if venue else ""
    is_journal = family in {"springer-nature"} or "nature" in slug or "journal" in slug
    return {
        "title": "Journal Manuscript Preview" if is_journal else "Conference Preview Paper",
        "venue_zh": "期刊" if is_journal else "会议",
        "preview_kind": "journal manuscript preview" if is_journal else "conference preview",
        "paper_kind": "journal manuscript" if is_journal else "conference paper",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Expose a venue-formatted manuscript preview only when writing generated it "
            "and the normal-paper audit passes. This script intentionally does not "
            "write scientific paper prose."
        )
    )
    parser.add_argument("--project", required=True)
    parser.add_argument("--venue", required=True)
    parser.add_argument("--title", default="")
    args = parser.parse_args()

    guard_rc = guard_fresh_base_blocker_entry(args.project, args.venue, Path(__file__).name, safe_unblock=False)
    if guard_rc is not None:
        return int(guard_rc)

    paths = build_paths(args.project)
    venue_slug = slugify(args.venue)
    state = get_active_paper_state(args.project, venue=args.venue)
    labels = preview_labels(args.project, args.venue)
    output_dir = paths.root / "paper" / "output" / venue_slug
    output_dir.mkdir(parents=True, exist_ok=True)
    report = output_dir / "conference_preview_report.md"

    workspace = Path(str(state.get("paper_orchestra_workspace") or paths.root / "paper" / "writing" / venue_slug / "workspace"))
    workspace_final = ("workspace_final", workspace / "final" / "paper.pdf", workspace / "final" / "paper.tex")
    state_pdf = Path(str(state.get("paper_orchestra_final_pdf") or "__missing_state_pdf__"))
    state_tex = Path(str(state.get("paper_orchestra_final_tex") or "__missing_state_tex__"))
    fresh_workspace_required = bool(
        state.get("paper_current_regeneration_requested")
        or state.get("paper_orchestra_force_refresh")
        or state.get("paper_orchestra_bridge_status") in {"running", "blocked"}
        or state.get("paper_generation_skipped")
    )
    workspace_attempt_exists = workspace_final[1].is_file() or workspace_final[2].is_file()
    candidates: list[tuple[str, Path, Path]] = [workspace_final]
    if fresh_workspace_required and workspace_attempt_exists:
        # A fresh writing attempt exists but is not yet acceptable; do not
        # fall back to stale paper.pdf and pretend the current generation worked.
        candidates.append(("writing_raw", output_dir / "writing_raw.pdf", output_dir / "writing_raw.tex"))
        if state_pdf.is_file() or state_tex.is_file():
            candidates.append(("state_final", state_pdf, state_tex))
    else:
        candidates.append(("current_output", output_dir / "paper.pdf", output_dir / "paper.tex"))
        if state_pdf.is_file() or state_tex.is_file():
            candidates.append(("state_final", state_pdf, state_tex))
        candidates.extend([
            ("writing_raw", output_dir / "writing_raw.pdf", output_dir / "writing_raw.tex"),
            ("legacy_raw", output_dir / "paper_orchestra_raw.pdf", output_dir / "paper_orchestra_raw.tex"),
        ])
    selected_label, orchestra_pdf, orchestra_tex, candidate_manifest = select_manuscript_candidate(args.project, args.venue, candidates)

    if not orchestra_pdf.is_file() or not orchestra_tex.is_file():
        rejected_pdf = _first_existing_candidate_path(candidate_manifest, "pdf")
        rejected_tex = _first_existing_candidate_path(candidate_manifest, "tex")
        rejection_blockers = _candidate_rejection_blockers(candidate_manifest)
        rejection_summary = str(rejection_blockers[0].get("detail") or "") if rejection_blockers else "no acceptable full-manuscript PDF/TeX exists"
        lines = [
            f"# {labels['title']}\n\n",
            "- status: blocked\n",
            "- reason: no acceptable full-manuscript PDF/TeX exists; rejected candidates are listed below\n",
            "- principle: The workflow must expose a real manuscript, not an internal gate report or blocker checklist. Regenerate through writing if all candidates are rejected.\n",
            f"- writing_bridge_report: {state.get('paper_orchestra_bridge_report', '')}\n",
            "\n## Candidate audit\n\n",
            "```json\n" + json.dumps(candidate_manifest, ensure_ascii=False, indent=2) + "\n```\n",
        ]
        write_text(report, "".join(lines))
        update_pipeline_state(
            args.project,
            {
                "conference_preview_ready": False,
                "normal_preview_ready": False,
                "conference_preview_report": str(report),
                "conference_preview_pdf": "",
                "conference_preview_tex": "",
                "latest_preview_pdf": "",
                "latest_preview_tex": "",
                "pdf_ready": False,
                "pdf_path": "",
                "rendered_tex": "",
                "blocked_preview_pdf": "",
                "blocked_preview_tex": "",
                "blocked_preview_available": False,
                "latest_generated_pdf_path": str(rejected_pdf) if rejected_pdf else "",
                "latest_generated_tex_path": str(rejected_tex) if rejected_tex else "",
                "paper_stage_status": "blocked_content_policy" if rejection_blockers else "blocked_no_acceptable_manuscript",
                "paper_normality_status": "blocked",
                "paper_content_policy_status": "blocked",
                "paper_content_candidate_audit": candidate_manifest,
                "conference_preview_blockers": rejection_blockers,
                "conference_preview_blocker_summary": rejection_summary,
            },
            venue=args.venue,
            promote_to_top=True,
        )
        print(report)
        return 2

    policy_violations = manuscript_policy_violations(args.project, orchestra_tex, venue=args.venue)
    if policy_violations:
        rejection_blockers = [{
            "id": "manuscript_content_policy_violation",
            "status": "block",
            "detail": "; ".join(policy_violations[:8]),
            "source": "paper_content_policy",
            "preview_blocker": True,
            "submission_blocker": True,
        }]
        lines = [
            f"# {labels['title']}\n\n",
            "- status: blocked\n",
            "- reason: manuscript content policy violation; old or invalid paper output is not exposed as current preview\n",
            f"- violations: {', '.join(policy_violations)}\n",
            "- principle: paper previews must not present failed hypotheses, negative-result narratives, internal gate diagnostics, or legacy-route stories as manuscript content. Regenerate through writing from current selected-route evidence.\n",
            f"- source_tex: {orchestra_tex}\n",
        ]
        write_text(report, "".join(lines))
        update_pipeline_state(
            args.project,
            {
                "conference_preview_ready": False,
                "normal_preview_ready": False,
                "conference_preview_report": str(report),
                "conference_preview_pdf": "",
                "conference_preview_tex": "",
                "latest_preview_pdf": "",
                "latest_preview_tex": "",
                "pdf_ready": False,
                "pdf_path": "",
                "rendered_tex": "",
                "blocked_preview_pdf": "",
                "blocked_preview_tex": "",
                "blocked_preview_available": False,
                "latest_generated_pdf_path": str(orchestra_pdf),
                "latest_generated_tex_path": str(orchestra_tex),
                "paper_stage_status": "blocked_content_policy",
                "paper_normality_status": "blocked",
                "paper_content_policy_status": "blocked",
                "paper_content_policy_violations": policy_violations,
                "conference_preview_blockers": rejection_blockers,
                "conference_preview_blocker_summary": str(rejection_blockers[0].get("detail") or ""),
            },
            venue=args.venue,
            promote_to_top=True,
        )
        print(report)
        return 2

    pdf_target = output_dir / "paper.pdf"
    tex_target = output_dir / "paper.tex"
    bib_source = orchestra_tex.parent / "refs.bib"
    if not bib_source.is_file():
        bib_source = workspace / "refs.bib"
    bib_target = output_dir / "refs.bib"
    copy_if_ready(orchestra_pdf, pdf_target)
    copy_if_ready(orchestra_tex, tex_target)
    copy_if_ready(bib_source, bib_target)
    copied_sidecars = copy_template_sidecars(orchestra_tex.parent, output_dir)

    audit_rc = run(
        [
            sys.executable,
            str(ROOT / "modules" / "writing" / "scripts" / "audit_paper_normality.py"),
            "--project",
            args.project,
            "--venue",
            args.venue,
        ],
        required=False,
    )
    figure_rc = run(
        [
            sys.executable,
            str(ROOT / "modules" / "writing" / "scripts" / "audit_paper_figures.py"),
            "--project",
            args.project,
            "--venue",
            args.venue,
        ],
        required=False,
    )
    audit_state = get_active_paper_state(args.project, venue=args.venue)
    normality_payload = read_json(paths.root / "state" / "paper_normality_audit.json", {})
    figure_payload = read_json(paths.root / "state" / "paper_figure_quality_audit.json", {})
    raw_normality_failures = normality_payload.get("failed_checks", []) if isinstance(normality_payload.get("failed_checks", []), list) else []
    public_normality_failures = normality_payload.get("public_failed_checks", []) if isinstance(normality_payload.get("public_failed_checks", []), list) else []
    normality_failures = public_normality_failures or raw_normality_failures
    layout_warnings = figure_payload.get("layout_footprint_warnings", []) if isinstance(figure_payload.get("layout_footprint_warnings", []), list) else []
    figure_status = audit_state.get("paper_figure_quality_status", "")
    figure_ready = bool(audit_state.get("paper_figure_quality_ready"))
    normal_ready = bool(audit_state.get("paper_normality_ready") or audit_state.get("normal_preview_ready"))
    pages = audit_state.get("paper_normality_pages", "")
    body_pages = audit_state.get("paper_normality_body_pages", "")
    reference_pages = audit_state.get("paper_normality_estimated_reference_pages", "")
    citation_count = audit_state.get("paper_normality_citation_count", "")
    venue_format_status = audit_state.get("paper_venue_format_status", "")
    citation_render_status = audit_state.get("paper_citation_render_status", "")
    citation_render_blockers = audit_state.get("paper_citation_render_blockers", []) if isinstance(audit_state.get("paper_citation_render_blockers", []), list) else []
    venue_policy = audit_state.get("venue_submission_policy", {}) if isinstance(audit_state.get("venue_submission_policy", {}), dict) else {}
    self_review = validate_paper_self_review_receipt(
        paths.root,
        args.venue,
        current_pdf=pdf_target if pdf_target.exists() else None,
        current_tex=tex_target if tex_target.exists() else None,
        current_refs=bib_target if bib_target.exists() else None,
    )
    self_review_ready = bool(self_review.get("ready"))
    self_review_blockers = self_review.get("blockers", []) if isinstance(self_review.get("blockers", []), list) else []
    self_review_evidence_blockers = self_review.get("evidence_blockers", []) if isinstance(self_review.get("evidence_blockers", []), list) else []
    self_review_preview_only_ready = bool(self_review.get("preview_only_ready"))
    venue_ready = venue_format_status == "pass"
    ready = (
        audit_rc == 0
        and figure_rc == 0
        and normal_ready
        and venue_ready
        and figure_ready
        and self_review_ready
    )
    body_limit = venue_policy.get("body_page_max", "") if isinstance(venue_policy, dict) else ""
    try:
        body_pages_int = int(body_pages or 0)
    except Exception:
        body_pages_int = 0
    try:
        body_limit_int = int(body_limit or 0)
    except Exception:
        body_limit_int = 0
    body_page_diagnostic = ""
    if body_pages_int and body_limit_int:
        if body_pages_int <= body_limit_int:
            body_page_diagnostic = f"正文页数符合当前{labels['venue_zh']}官方要求：{body_pages_int}/{body_limit_int}。"
        else:
            body_page_diagnostic = f"正文页数超过当前{labels['venue_zh']}官方要求：{body_pages_int}/{body_limit_int}；先诊断图表/表格占地，再决定是否调整正文。"
    elif body_pages_int:
        body_page_diagnostic = f"正文页数={body_pages_int}；当前{labels['venue_zh']}官方正文页数上限尚未解析完成。"
    reference_target = audit_state.get("paper_normality_reference_target") or audit_state.get("paper_reference_quality_target") or ""
    reference_target_source = str(audit_state.get("paper_normality_reference_target_source") or "")
    reference_diagnostic = ""
    if citation_count and reference_target:
        label = "官方引用要求" if reference_target_source == "official" else "写作引用质量目标"
        reference_diagnostic = f"{label}：{citation_count}/{reference_target}。"
    preview_blockers = _preview_gate_blockers(normality_failures, figure_status, figure_ready, layout_warnings, self_review_blockers)
    preview_blocker_summary = _preview_blocker_summary(preview_blockers)
    lines = [
        f"# {labels['title']}\n\n",
        f"- status: {'ready' if ready else 'blocked'}\n",
        f"- source: real writing manuscript candidate ({selected_label})\n",
        f"- tex_path: {tex_target if tex_target.exists() else ''}\n",
        f"- pdf_path: {pdf_target if pdf_target.exists() else ''}\n",
        f"- pages: {pages}\n",
        f"- body_pages: {body_pages}\n",
        f"- body_page_diagnostic: {body_page_diagnostic}\n",
        f"- estimated_reference_pages: {reference_pages}\n",
        f"- venue_page_rule: {venue_page_rule_label(venue_policy)}\n",
        f"- citation_count: {citation_count}\n",
        f"- reference_diagnostic: {reference_diagnostic}\n",
        f"- venue_template_format: {venue_format_status}\n",
        f"- citation_render: {citation_render_status}\n",
        f"- figure_quality: {figure_status}\n",
        f"- claude_self_review: {self_review.get('status')}\n",
        f"- claude_self_review_receipt: {self_review.get('path', '')}\n",
        f"- normality_audit: {audit_state.get('paper_normality_report', '')}\n",
        f"- figure_quality_audit: {audit_state.get('paper_figure_quality_report', '')}\n",
        "- principle: accepted-preview PDF exposure requires a real manuscript shape plus venue-template and figure-quality checks. Internal gate reports must be rejected/reverted, not shown as paper output. Scientific submission readiness is still controlled by evidence/readiness gates.\n",
    ]
    if preview_blockers:
        lines.extend(["\n## Preview Blockers\n\n"])
        for item in preview_blockers[:12]:
            lines.append(f"- {item.get('id', 'check')}: {item.get('public_detail') or item.get('detail', '')}\n")
    if self_review_evidence_blockers:
        lines.extend(["\n## Self-review Evidence Blockers\n\n"])
        lines.append("- PDF/TeX preview checks may pass, but project Claude Code found unresolved scientific-evidence issues. Keep this as an inspection preview, not a submission-ready manuscript.\n")
        for item in self_review_evidence_blockers[:8]:
            if isinstance(item, dict):
                lines.append(f"- {item.get('category', item.get('id', 'self_review_evidence'))}: {item.get('detail') or item.get('issue', '')}\n")
            else:
                lines.append(f"- {item}\n")
    if layout_warnings:
        lines.extend(["\n## Layout Diagnostics\n\n"])
        for item in layout_warnings[:8]:
            lines.append(f"- {item}\n")
        if body_pages_int and body_limit_int and body_pages_int <= body_limit_int:
            lines.append("- 正文页数已满足官方限制；优先处理图表/表格占地、参考文献覆盖和参考文献排版密度。\n")
        else:
            lines.append("- Page-fit repair should diagnose figure/table footprint and bibliography/reference-page footprint before changing manuscript prose.\n")
    lines.extend([
        "\n## Candidate audit\n\n",
        "```json\n" + json.dumps(candidate_manifest, ensure_ascii=False, indent=2) + "\n```\n",
    ])
    write_text(report, "".join(lines))
    update_pipeline_state(
        args.project,
        {
            "conference_preview_ready": ready,
            "conference_preview_pdf": str(pdf_target) if ready and pdf_target.exists() else "",
            "conference_preview_tex": str(tex_target) if ready and tex_target.exists() else "",
            "conference_preview_pages": pages,
            "conference_preview_body_pages": body_pages,
            "conference_preview_body_page_limit": body_limit,
            "conference_preview_body_page_diagnostic": body_page_diagnostic,
            "conference_preview_reference_pages": reference_pages,
            "conference_preview_reference_diagnostic": reference_diagnostic,
            "conference_preview_reference_page_limit": venue_policy.get("reference_page_max", "") if isinstance(venue_policy, dict) else "",
            "conference_preview_total_page_limit": venue_policy.get("total_page_max", "") if isinstance(venue_policy, dict) else "",
            "venue_submission_policy": venue_policy,
            "venue_submission_policy_status": audit_state.get("venue_submission_policy_status", ""),
            "conference_preview_report": str(report),
            "blocked_preview_pdf": str(pdf_target) if pdf_target.exists() else "",
            "blocked_preview_tex": str(tex_target) if tex_target.exists() else "",
            "blocked_preview_available": pdf_target.exists(),
            "latest_preview_pdf": str(pdf_target) if pdf_target.exists() else "",
            "latest_preview_tex": str(tex_target) if tex_target.exists() else "",
            "paper_venue_format_status": venue_format_status,
            "paper_venue_format_validation": audit_state.get("paper_venue_format_validation", {}),
            "venue_template_format_ready": venue_ready,
            "paper_citation_render_status": citation_render_status,
            "paper_citation_render_diagnostics": audit_state.get("paper_citation_render_diagnostics", {}),
            "paper_citation_render_blockers": citation_render_blockers,
            "paper_citation_render_ready": citation_render_status == "pass",
            "citation_render_ready": citation_render_status == "pass",
            "paper_self_review_status": self_review.get("status"),
            "paper_self_review_ready": self_review_ready,
            "paper_self_review_receipt": self_review.get("path", ""),
            "paper_self_review_blockers": self_review_blockers,
            "paper_self_review_evidence_blockers": self_review_evidence_blockers,
            "paper_self_review_evidence_blocker_count": len(self_review_evidence_blockers),
            "paper_self_review_preview_only_ready": self_review_preview_only_ready,
            "paper_self_review_submission_evidence_ready": bool(self_review.get("submission_evidence_ready")),
            "paper_self_review_independent_findings_count": self_review.get("independent_findings_count", 0),
            "paper_self_review_repairs_count": self_review.get("repairs_count", 0),
            "paper_figure_quality_status": figure_status,
            "paper_figure_quality_ready": figure_ready,
            "paper_figure_quality_report": audit_state.get("paper_figure_quality_report", ""),
            "paper_figure_quality_audit": audit_state.get("paper_figure_quality_audit", ""),
            "paper_figure_count": audit_state.get("paper_figure_count", ""),
            "paper_figure_blocker_count": audit_state.get("paper_figure_blocker_count", ""),
            "paper_figure_warning_count": audit_state.get("paper_figure_warning_count", ""),
            "paper_figure_failed": audit_state.get("paper_figure_failed", []),
            "conference_preview_blockers": preview_blockers[:20],
            "conference_preview_blocker_summary": preview_blocker_summary,
            "conference_preview_internal_blockers": raw_normality_failures[:20],
            "paper_layout_footprint_warnings": layout_warnings[:20],
            "normal_preview_ready": normal_ready,
            "paper_stage_status": "preview_ready" if ready else "blocked_preview_gate",
            "paper_content_policy_status": "pass",
            "paper_content_policy_violations": [],
            "paper_content_source_label": selected_label,
            "paper_template_sidecars": copied_sidecars,
            "paper_content_candidate_audit": candidate_manifest,
            "pdf_ready": ready,
            "pdf_path": str(pdf_target) if ready and pdf_target.exists() else "",
            "rendered_tex": str(tex_target) if ready and tex_target.exists() else "",
        },
        venue=args.venue,
        promote_to_top=True,
    )
    print(report)
    return 0 if ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
