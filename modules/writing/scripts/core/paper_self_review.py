#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path as _WritingDevPath
import sys as _writing_sys
_WRITING_SCRIPT_ROOT = next((p for p in _WritingDevPath(__file__).resolve().parents if p.name == "scripts"), _WritingDevPath(__file__).resolve().parent)
for _writing_path in [_WRITING_SCRIPT_ROOT, *[p for p in _WRITING_SCRIPT_ROOT.iterdir() if p.is_dir()]]:
    _writing_text = str(_writing_path)
    if _writing_text not in _writing_sys.path:
        _writing_sys.path.insert(0, _writing_text)


import hashlib
import json
import re
from pathlib import Path
from typing import Any

from paper_common import slugify


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def sha256_file(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def paper_self_review_receipt_path(project_root: Path) -> Path:
    return project_root / "state" / "paper_preview_self_review.json"


def normalize_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _artifact_rows(payload: dict[str, Any]) -> dict[str, Any]:
    rows = payload.get("artifact_fingerprints")
    if isinstance(rows, dict):
        return rows
    rows = payload.get("artifacts_reviewed")
    if isinstance(rows, dict):
        return rows
    return {}


def _artifact_path(row: Any) -> str:
    if isinstance(row, dict):
        return str(row.get("path") or row.get("file") or row.get("source") or "").strip()
    if isinstance(row, str):
        return row.strip()
    return ""


def _artifact_sha(row: Any) -> str:
    if isinstance(row, dict):
        return str(row.get("sha256") or row.get("hash") or "").strip()
    return ""


def _artifact_has_text(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    for key in ["excerpt", "text_excerpt", "summary", "note", "notes", "observation", "observations", "evidence", "pdf_text_excerpt"]:
        value = row.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return True
        if str(value or "").strip():
            return True
    try:
        return int(row.get("length_chars") or 0) > 0
    except Exception:
        return False


def _resolve_project_path(project_root: Path, raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = project_root / path
    return path


def _path_exists(project_root: Path, raw: str) -> bool:
    if not raw:
        return False
    try:
        return _resolve_project_path(project_root, raw).is_file()
    except Exception:
        return False


def _hash_matches(project_root: Path, raw: str, expected: str) -> bool:
    if not expected or not raw:
        return True
    try:
        path = _resolve_project_path(project_root, raw)
        return sha256_file(path) == expected
    except Exception:
        return False


def _current_hash_matches(project_root: Path, row: Any, current: Path | None) -> bool:
    if current is None or not current.exists() or not current.is_file():
        return True
    row_hash = _artifact_sha(row)
    if not row_hash:
        return False
    return row_hash == sha256_file(current)


def _list_payload(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _compact_text(value: Any) -> str:
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, dict):
        return " ".join(_compact_text(item) for item in value.values() if item not in (None, ""))
    if isinstance(value, list):
        return " ".join(_compact_text(item) for item in value if item not in (None, ""))
    return " ".join(str(value or "").split())


def _row_field_text(row: Any, keys: list[str]) -> str:
    if not isinstance(row, dict):
        return _compact_text(row)
    return " ".join(_compact_text(row.get(key)) for key in keys if row.get(key) not in (None, "")).strip()


def _row_has_any(row: Any, keys: list[str]) -> bool:
    return bool(_row_field_text(row, keys))


def _finding_is_structured(row: Any) -> bool:
    return isinstance(row, dict)


def _finding_has_issue(row: Any) -> bool:
    return _row_has_any(row, ["issue", "problem", "detail", "summary", "finding", "observation", "description"])


def _finding_has_artifact_evidence(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    evidence_text = _row_field_text(
        row,
        [
            "evidence",
            "evidence_source",
            "evidence_sources",
            "artifact",
            "artifacts",
            "source_artifact",
            "source_artifacts",
            "location",
            "locations",
            "pdf_text_excerpt",
            "tex_location",
            "bibtex_entry",
            "log_excerpt",
            "compile_log_excerpt",
            "paper_log_excerpt",
            "quote",
            "excerpt",
            "page",
            "line",
            "lines",
            "observed_in",
            "read_from",
            "discovered_from",
        ],
    )
    return bool(evidence_text)


def _finding_independence_evidence(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    provenance = _row_field_text(row, ["review_source", "discovery_method", "discovered_by", "origin", "source", "rationale"])
    if not provenance:
        return False
    lowered = provenance.lower()
    independent_terms = ["claude", "independent", "pdf", "tex", "bib", "log", "venue", "pdftotext", "compiled"]
    return any(term in lowered for term in independent_terms)


def _finding_only_restates_ar(row: Any) -> bool:
    text = _compact_text(row).lower()
    if not text:
        return False
    terms = ["deterministic blocker", "gate snapshot", "conference_preview_report"]
    raw_artifact_terms = ["pdf", "pdftotext", "tex", "bib", "refs.bib", "paper.log", "compile.log", "venue_requirements"]
    return any(term in text for term in terms) and not any(term in text for term in raw_artifact_terms)


def _finding_is_generic_review_note(row: Any) -> bool:
    text = _compact_text(row).lower()
    if not text:
        return False
    no_issue_phrases = ["no issue", "no issues", "no blocker", "nothing found", "all clear", "looks good"]
    if any(phrase in text for phrase in no_issue_phrases):
        return True
    generic_actions = ["checked", "reviewed", "inspected", "read", "verified", "looked at"]
    issue_terms = [
        "undefined", "unresolved", "missing", "mismatch", "failed", "failure", "error", "warning",
        "invalid", "unsupported", "stale", "incorrect", "inconsistent", "overflow", "oversized",
        "author?", "[?]", "??", "empty literal stack", "internal", "todo", "artifact", "broken",
    ]
    return any(action in text for action in generic_actions) and not any(term in text for term in issue_terms)


EVIDENCE_BLOCKER_CATEGORY_MARKERS = {
    "missing_empirical_validation",
    "unsupported_claim",
    "unsupported_claims",
    "untested_design",
    "untested_design_space",
    "results_contains_untested_design_space",
    "evaluation_scope_mismatch",
    "contribution_scope_mismatch",
    "contribution_3_evaluation_scope_mismatch",
    "claim_evidence_mismatch",
    "evidence_scope_mismatch",
    "missing_data_or_code_availability",
    "data_code_availability",
    "data_code_availability_statements_need_urls",
}

EVIDENCE_BLOCKER_TEXT_MARKERS = [
    "missing empirical validation",
    "no empirical results",
    "zero empirical results",
    "lacks empirical",
    "lack empirical",
    "not empirically validated",
    "has not been empirically validated",
    "no experiments compare",
    "no ablation",
    "untested architecture",
    "untested design",
    "untested architectural",
    "speculative design",
    "misleading impression",
    "unsupported claim",
    "unsupported claims",
    "claim evidence mismatch",
    "evaluation scope mismatch",
    "contribution scope mismatch",
    "evaluates only the backbone",
    "not the paper's primary claimed contribution",
    "not the paper's primary contribution",
    "data availability",
    "code availability",
    "without a url",
    "no url",
    "missing url",
    "missing doi",
]

RESOLVED_STATUS_MARKERS = {"fixed", "resolved", "repaired", "closed", "non_blocking", "non-blocking", "preview_only", "preview-only"}
UNRESOLVED_REPAIR_MARKERS = ["no repair", "not repaired", "would violate", "would be", "required before", "needs", "need ", "todo"]
RESOLUTION_EVIDENCE_MARKERS = ["verification", "verified", "validated", "rechecked", "confirmed", "pass", "compiled", "resolved", "fixed", "repaired"]


def _finding_category_text(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    return _row_field_text(row, ["category", "id", "type", "issue_type", "name"])


def _normalized_marker_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _finding_matches_evidence_blocker(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    category = _normalized_marker_text(_finding_category_text(row))
    if any(marker in category for marker in EVIDENCE_BLOCKER_CATEGORY_MARKERS):
        return True
    issue_text = _compact_text(
        [
            row.get("issue"),
            row.get("problem"),
            row.get("detail"),
            row.get("summary"),
            row.get("finding"),
            row.get("observation"),
            row.get("description"),
        ]
    ).lower()
    if not issue_text:
        return False
    if any(marker in issue_text for marker in EVIDENCE_BLOCKER_TEXT_MARKERS):
        if "data availability" in issue_text or "code availability" in issue_text:
            return any(term in issue_text for term in ["without", "missing", "no url", "url", "doi", "repository", "fail"])
        return True
    return False


def _finding_resolution_status(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    return _normalized_marker_text(
        _row_field_text(
            row,
            [
                "status",
                "resolution_status",
                "evidence_status",
                "submission_status",
                "blocker_status",
                "repair_status",
            ],
        )
    )


def _finding_has_verified_resolution(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("resolved") is True or row.get("evidence_blocker_resolved") is True:
        return True
    status = _finding_resolution_status(row)
    if status in RESOLVED_STATUS_MARKERS:
        return True
    if any(marker in status for marker in RESOLVED_STATUS_MARKERS):
        return True
    repair_text = _compact_text(
        [
            row.get("repair"),
            row.get("resolution"),
            row.get("fix"),
            row.get("verification"),
            row.get("repair_verification"),
            row.get("postcheck"),
            row.get("resolved_by"),
        ]
    ).lower()
    if not repair_text:
        return False
    if any(marker in repair_text for marker in UNRESOLVED_REPAIR_MARKERS):
        return False
    return any(marker in repair_text for marker in ["resolved", "fixed", "repaired"]) and any(
        marker in repair_text for marker in RESOLUTION_EVIDENCE_MARKERS
    )


def _evidence_blocker_id(row: dict[str, Any], index: int) -> str:
    raw = row.get("id") or row.get("category") or row.get("issue_type") or f"finding_{index}"
    token = _normalized_marker_text(raw) or f"finding_{index}"
    return f"self_review_evidence_{token}"


def self_review_evidence_blockers(payload: dict[str, Any]) -> list[dict[str, Any]]:
    findings = _list_payload(payload.get("independent_findings") or payload.get("issues_found") or payload.get("review_findings"))
    blockers: list[dict[str, Any]] = []
    for index, row in enumerate(findings, start=1):
        if not isinstance(row, dict):
            continue
        if not _finding_matches_evidence_blocker(row):
            continue
        if _finding_has_verified_resolution(row):
            continue
        issue = _row_field_text(row, ["issue", "problem", "detail", "summary", "finding", "observation", "description"])
        category = _finding_category_text(row) or "self_review_evidence"
        evidence = []
        for key in ["source_artifacts", "artifacts", "evidence_sources", "location", "locations", "tex_location", "pdf_text_excerpt", "log_excerpt", "venue_requirement"]:
            value = row.get(key)
            if value in (None, "", []):
                continue
            evidence.append(value)
        blockers.append(
            {
                "id": _evidence_blocker_id(row, index),
                "category": str(category),
                "issue": issue or _compact_text(row),
                "detail": issue or _compact_text(row),
                "source": "paper_self_review_independent_finding",
                "evidence": evidence,
                "finding_index": index,
                "preview_blocker": False,
                "submission_blocker": True,
            }
        )
    return blockers


def _remaining_blocker_issue_text(row: Any) -> str:
    return _row_field_text(row, ["issue", "problem", "detail", "summary", "finding", "observation", "description", "mitigation"])


def _remaining_blocker_is_submission_evidence(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("requires_experiment") is True or row.get("submission_blocker") is True:
        return True
    if row.get("preview_blocker") is False and row.get("submission_blocker") is not False:
        return True
    if _finding_matches_evidence_blocker(row):
        return True
    text = _remaining_blocker_issue_text(row).lower()
    evidence_terms = [
        "evidence gate", "evidence-limited", "evidence limited", "paper evidence audit",
        "no experimental results", "proposed method", "requires experiment", "requires running experiments",
        "completion requires running", "submission", "claim evidence", "not submission-ready",
    ]
    return any(term in text for term in evidence_terms)


def _remaining_blocker_is_nonblocking_note(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    status_text = _normalized_marker_text(_row_field_text(row, ["status", "severity", "resolution_status", "blocker_status"]))
    if any(marker in status_text for marker in ["info", "warn", "warning", "non_blocking", "non_blocker", "resolved", "fixed", "mitigated"]):
        return True
    text = _remaining_blocker_issue_text(row).lower()
    mitigation = _compact_text(row.get("mitigation")).lower()
    return bool(mitigation and any(term in mitigation for term in ["resolved", "fixed", "reduced", "mitigated", "valid", "verified"])) and not any(
        term in text for term in ["undefined citation", "compile error", "fatal", "missing pdf", "missing tex"]
    )


def remaining_blocker_evidence_blockers(remaining: list[Any]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for index, row in enumerate(remaining, start=1):
        if not _remaining_blocker_is_submission_evidence(row):
            continue
        issue = _remaining_blocker_issue_text(row) or _compact_text(row)
        category = _finding_category_text(row) if isinstance(row, dict) else "remaining_submission_evidence"
        blockers.append(
            {
                "id": f"self_review_remaining_evidence_{_normalized_marker_text(category) or index}",
                "category": str(category or "remaining_submission_evidence"),
                "issue": issue,
                "detail": issue,
                "source": "paper_self_review_remaining_blocker",
                "evidence": row,
                "finding_index": index,
                "preview_blocker": False,
                "submission_blocker": True,
            }
        )
    return blockers


def remaining_preview_blockers(remaining: list[Any]) -> list[Any]:
    out: list[Any] = []
    for row in remaining:
        if _remaining_blocker_is_submission_evidence(row) or _remaining_blocker_is_nonblocking_note(row):
            continue
        out.append(row)
    return out


def _review_protocol(payload: dict[str, Any]) -> dict[str, Any]:
    protocol = payload.get("review_protocol")
    if not isinstance(protocol, dict):
        protocol = payload.get("manuscript_review_protocol")
    return protocol if isinstance(protocol, dict) else {}


def _review_protocol_open_ended(protocol: dict[str, Any]) -> bool:
    if protocol.get("open_ended_review") is True or protocol.get("open_issue_discovery") is True:
        return True
    text = _compact_text([protocol.get("mode"), protocol.get("scope"), protocol.get("objective"), protocol.get("method")]).lower()
    return "open" in text and any(term in text for term in ["issue", "manuscript", "independent", "artifact"])


def _position_any(text: str, aliases: list[str]) -> int:
    positions = [text.find(alias) for alias in aliases if text.find(alias) >= 0]
    return min(positions) if positions else -1


def _review_protocol_independent_first(protocol: dict[str, Any]) -> bool:
    explicit_flag = (
        protocol.get("gate_crosscheck_after_independent_review") is True
        or protocol.get("independent_artifact_review_before_gate_crosscheck") is True
    )
    order_text = _compact_text(
        [
            protocol.get("discovery_order"),
            protocol.get("phase_order"),
            protocol.get("workflow_order"),
            protocol.get("review_sequence"),
            protocol.get("method_sequence"),
            protocol.get("steps"),
        ]
    ).lower()
    independent_pos = _position_any(
        order_text,
        [
            "independent_artifact_review",
            "independent artifact review",
            "independent issue discovery",
            "raw artifact",
            "phase 1",
            "phase one",
        ],
    )
    pos = _position_any(
        order_text,
        [
            "gate_crosscheck",
            "ar gate crosscheck",
            "ar gate cross-check",
            "deterministic gate",
            "gate snapshot",
            "phase 3",
            "phase three",
        ],
    )
    return explicit_flag and independent_pos >= 0 and pos >= 0 and independent_pos < pos


def _finding_from_independent_phase(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("found_before_gate_crosscheck") is True:
        return True
    phase_text = _row_field_text(
        row,
        [
            "discovery_phase",
            "review_phase",
            "phase",
            "found_during",
            "review_stage",
            "discovery_stage",
        ],
    ).lower()
    return "independent_artifact_review" in phase_text or "independent artifact review" in phase_text


def _reading_log_entries(protocol: dict[str, Any], payload: dict[str, Any]) -> list[Any]:
    for key in ["artifact_reading_log", "artifacts_read", "reviewed_artifacts", "artifact_review_log"]:
        rows = protocol.get(key)
        if isinstance(rows, list):
            return rows
    rows = payload.get("artifact_reading_log")
    return rows if isinstance(rows, list) else []


def _reading_entry_matches(entry: Any, key: str) -> bool:
    text = _compact_text(entry).lower()
    aliases = {
        "pdf": ["pdf", "paper.pdf"],
        "pdf_text": ["pdf_text", "pdf text", "pdftotext", "paper.txt"],
        "tex": ["tex", "latex", "paper.tex"],
        "refs_bib": ["refs_bib", "refs.bib", "bibliography", "bibtex"],
        "compile_log": ["compile_log", "compile.log", "latexmk", "compile log", "paper.log", "latex log"],
        "paper_log": ["paper_log", "paper.log", "latex log"],
        "venue_requirements": ["venue_requirements", "venue requirements", "venue contract", "template contract"],
    }
    return any(alias in text for alias in aliases.get(key, [key]))


def _reading_entry_has_method(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    return _row_has_any(entry, ["method", "command", "commands", "read_with", "tool", "procedure", "how_read"] )


def _reading_entry_has_evidence(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    return _row_has_any(entry, ["excerpt", "text_excerpt", "finding_summary", "observation", "observations", "note", "notes", "evidence", "location", "line", "page", "hash", "sha256"] )


def _repair_has_file(row: Any) -> bool:
    return isinstance(row, dict) and _row_has_any(row, ["file", "path", "target", "artifact", "files", "paths"])


def _repair_has_action(row: Any) -> bool:
    return isinstance(row, dict) and _row_has_any(row, ["action", "repair", "change", "detail", "summary", "description"])


def _repair_has_verification(row: Any) -> bool:
    return isinstance(row, dict) and _row_has_any(
        row,
        ["verification", "validated_by", "command", "commands", "check", "checks", "result", "evidence_after", "postcheck"],
    )


def validate_paper_self_review_receipt(
    project_root: Path,
    venue: str,
    *,
    current_pdf: Path | None = None,
    current_tex: Path | None = None,
    current_refs: Path | None = None,
    min_mtime: float | None = None,
) -> dict[str, Any]:
    """Validate the project Claude Code paper self-review receipt.

    This is intentionally about process evidence rather than known-error
    matching: Claude Code must show it independently inspected the compiled
    paper, source, bibliography, logs, and current venue contract before a
    preview can be treated as repaired.
    """
    receipt_path = paper_self_review_receipt_path(project_root)
    payload = read_json(receipt_path, {})
    blockers: list[dict[str, str]] = []
    if not isinstance(payload, dict) or not payload:
        blockers.append({"id": "missing_claude_self_review_receipt", "detail": f"missing {receipt_path}"})
        return {"status": "block", "ready": False, "path": str(receipt_path), "payload": {}, "blockers": blockers, "evidence_blockers": [], "evidence_blocker_count": 0, "preview_only_ready": False, "submission_evidence_ready": False}
    if min_mtime is not None:
        try:
            if receipt_path.stat().st_mtime + 1e-6 < min_mtime:
                blockers.append({"id": "stale_claude_self_review_receipt", "detail": "receipt is older than the current repair prompt"})
        except Exception:
            blockers.append({"id": "stale_claude_self_review_receipt", "detail": "receipt timestamp could not be checked"})
    expected_venue = normalize_token(venue) or normalize_token(slugify(venue))
    actual_venue = normalize_token(payload.get("venue") or payload.get("venue_slug") or payload.get("target_venue"))
    if expected_venue and actual_venue and expected_venue != actual_venue and normalize_token(slugify(venue)) != actual_venue:
        blockers.append({"id": "self_review_wrong_venue", "detail": f"receipt venue={payload.get('venue') or payload.get('venue_slug')}, expected={venue}"})
    reviewer = str(payload.get("reviewed_by") or payload.get("agent") or payload.get("backend") or "").lower()
    if "claude" not in reviewer or "project" not in reviewer:
        blockers.append({"id": "self_review_reviewer_not_project_claude", "detail": "receipt must identify project Claude Code as the reviewer"})
    status = str(payload.get("status") or payload.get("conclusion_status") or "").strip().lower()
    if status not in {"pass", "passed", "fixed", "completed", "ready", "reviewed"}:
        blockers.append({"id": "self_review_not_passed", "detail": f"receipt status={status or 'missing'}"})
    artifacts = _artifact_rows(payload)
    required_artifacts = ["pdf", "pdf_text", "tex", "refs_bib", "compile_log", "paper_log", "venue_requirements"]
    for key in required_artifacts:
        row = artifacts.get(key)
        raw_path = _artifact_path(row)
        if key == "pdf_text" and _artifact_has_text(row):
            continue
        if not raw_path:
            blockers.append({"id": f"self_review_missing_{key}", "detail": f"artifact_fingerprints.{key}.path is required"})
            continue
        if not _path_exists(project_root, raw_path):
            blockers.append({"id": f"self_review_missing_{key}", "detail": f"reviewed artifact does not exist: {raw_path}"})
            continue
        if not _hash_matches(project_root, raw_path, _artifact_sha(row)):
            blockers.append({"id": f"self_review_hash_mismatch_{key}", "detail": f"sha256 mismatch for reviewed artifact: {raw_path}"})
    if current_pdf is not None and current_pdf.exists() and current_pdf.is_file() and not _current_hash_matches(project_root, artifacts.get("pdf"), current_pdf):
        blockers.append({"id": "self_review_pdf_not_current", "detail": "receipt PDF fingerprint does not match the current preview PDF"})
    if current_tex is not None and current_tex.exists() and current_tex.is_file() and not _current_hash_matches(project_root, artifacts.get("tex"), current_tex):
        blockers.append({"id": "self_review_tex_not_current", "detail": "receipt TeX fingerprint does not match the current preview TeX"})
    if current_refs is not None and current_refs.exists() and current_refs.is_file() and not _current_hash_matches(project_root, artifacts.get("refs_bib"), current_refs):
        blockers.append({"id": "self_review_refs_not_current", "detail": "receipt refs.bib fingerprint does not match the current preview bibliography"})
    protocol = _review_protocol(payload)
    reading_entries = _reading_log_entries(protocol, payload)
    if not protocol:
        blockers.append({"id": "self_review_missing_open_review_protocol", "detail": "receipt must include review_protocol/manuscript_review_protocol for Claude Code's open-ended artifact review"})
    elif not _review_protocol_open_ended(protocol):
        blockers.append({"id": "self_review_protocol_not_open_ended", "detail": "review_protocol must mark open-ended independent issue discovery, not a fixed TASTE checklist"})
    elif not _review_protocol_independent_first(protocol):
        blockers.append({"id": "self_review_protocol_not_independent_first", "detail": "review_protocol must prove project Claude Code performed independent artifact review before using TASTE gates as a cross-check"})
    if not reading_entries:
        blockers.append({"id": "self_review_missing_artifact_reading_log", "detail": "review_protocol.artifact_reading_log must record how Claude Code read current PDF text, TeX, BibTeX, logs, and venue contract"})
    else:
        for key in required_artifacts:
            matches = [entry for entry in reading_entries if _reading_entry_matches(entry, key)]
            if not matches:
                blockers.append({"id": f"self_review_reading_log_missing_{key}", "detail": f"artifact_reading_log must include current {key} review evidence"})
                continue
            if not any(_reading_entry_has_method(entry) for entry in matches):
                blockers.append({"id": f"self_review_reading_log_missing_method_{key}", "detail": f"artifact_reading_log entry for {key} must record command/method used by Claude Code"})
            if not any(_reading_entry_has_evidence(entry) for entry in matches):
                blockers.append({"id": f"self_review_reading_log_missing_evidence_{key}", "detail": f"artifact_reading_log entry for {key} must include excerpt, location, hash, or observation evidence"})
    independent_findings = _list_payload(payload.get("independent_findings") or payload.get("issues_found") or payload.get("review_findings"))
    repairs = _list_payload(payload.get("repairs_applied") or payload.get("repair_actions") or payload.get("files_changed"))
    remaining = _list_payload(payload.get("remaining_blockers") or payload.get("remaining_issues"))
    final_checks = payload.get("final_checks") if isinstance(payload.get("final_checks"), dict) else {}
    if not isinstance(independent_findings, list):
        independent_findings = []
    if "independent_findings" not in payload and "issues_found" not in payload and "review_findings" not in payload:
        blockers.append({"id": "self_review_missing_independent_findings", "detail": "receipt must contain independent_findings/issues_found from Claude Code's own manuscript review"})
    elif not independent_findings:
        blockers.append({"id": "self_review_missing_independent_findings", "detail": "Claude Code self-review must record at least one independently found manuscript issue tied to reviewed artifacts"})
    for index, finding in enumerate(independent_findings, start=1):
        if not _finding_is_structured(finding):
            blockers.append({"id": "self_review_finding_not_structured", "detail": f"independent_findings[{index}] must be an object with issue, evidence, and provenance"})
            continue
        if not _finding_has_issue(finding):
            blockers.append({"id": "self_review_finding_missing_issue", "detail": f"independent_findings[{index}] must describe the manuscript issue Claude Code found"})
        if not _finding_has_artifact_evidence(finding):
            blockers.append({"id": "self_review_finding_missing_artifact_evidence", "detail": f"independent_findings[{index}] must cite PDF/TeX/BibTeX/log/venue evidence, location, or excerpt"})
        if not _finding_independence_evidence(finding):
            blockers.append({"id": "self_review_finding_missing_independence_provenance", "detail": f"independent_findings[{index}] must say how Claude Code independently found it from current artifacts"})
        if not _finding_from_independent_phase(finding):
            blockers.append({"id": "self_review_finding_not_independent_first", "detail": f"independent_findings[{index}] must record discovery_phase=independent_artifact_review or found_before_gate_crosscheck=true"})
        if _finding_only_restates_ar(finding):
            blockers.append({"id": "self_review_finding_only_restates_gate", "detail": f"independent_findings[{index}] appears to restate a workflow gate without raw-artifact evidence"})
        if _finding_is_generic_review_note(finding):
            blockers.append({"id": "self_review_finding_generic_review_note", "detail": f"independent_findings[{index}] is a generic check/review note; clean checks belong in artifact_reading_log, while independent_findings must be actual issues Claude Code found"})
    if not repairs:
        blockers.append({"id": "self_review_missing_repairs", "detail": "receipt must record repairs_applied/repair_actions with file, action, and verification evidence"})
    for index, repair in enumerate(repairs, start=1):
        if not isinstance(repair, dict):
            blockers.append({"id": "self_review_repair_not_structured", "detail": f"repairs_applied[{index}] must be an object with file, action, and verification"})
            continue
        if not _repair_has_file(repair):
            blockers.append({"id": "self_review_repair_missing_file", "detail": f"repairs_applied[{index}] must name the manuscript/BibTeX/log artifact it changed or checked"})
        if not _repair_has_action(repair):
            blockers.append({"id": "self_review_repair_missing_action", "detail": f"repairs_applied[{index}] must describe the repair action"})
        if not _repair_has_verification(repair):
            blockers.append({"id": "self_review_repair_missing_verification", "detail": f"repairs_applied[{index}] must include post-repair verification command/result or evidence"})
    remaining_preview = remaining_preview_blockers(remaining)
    if remaining_preview:
        blockers.append({"id": "self_review_remaining_blockers", "detail": f"Claude self-review reports remaining preview blockers: {remaining_preview[:5]}"})
    for key in ["compiled", "pdf_text_rechecked", "venue_shape_rechecked", "citation_render_rechecked", "bibliography_rechecked"]:
        if final_checks.get(key) is not True:
            blockers.append({"id": f"self_review_final_check_missing_{key}", "detail": f"final_checks.{key}=true is required"})
    scope_blocker_ids = {
        "self_review_wrong_venue",
        "self_review_pdf_not_current",
        "self_review_tex_not_current",
        "self_review_refs_not_current",
    }
    current_scope_invalid = any(str(row.get("id") or "") in scope_blocker_ids for row in blockers if isinstance(row, dict))
    evidence_blockers = [] if current_scope_invalid else [*self_review_evidence_blockers(payload), *remaining_blocker_evidence_blockers(remaining)]
    ready = not blockers
    return {
        "status": "pass" if ready else "block",
        "ready": ready,
        "path": str(receipt_path),
        "payload": payload,
        "blockers": blockers,
        "evidence_blockers": evidence_blockers,
        "evidence_blocker_count": len(evidence_blockers),
        "preview_only_ready": bool(ready and evidence_blockers),
        "submission_evidence_ready": bool(ready and not evidence_blockers),
        "independent_findings_count": len(independent_findings),
        "repairs_count": len(repairs),
        "remaining_blockers_count": len(remaining),
        "artifact_reading_log_count": len(reading_entries),
        "open_review_protocol_ready": bool(protocol and _review_protocol_open_ended(protocol)),
    }
