#!/usr/bin/env python3
from __future__ import annotations

import argparse
import difflib
import datetime as dt
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from literature_policy import build_literature_policy, core_topic_fit_from_text, score_paper
from llm_client import call_llm, llm_available, llm_disabled_reason
from project_paths import ROOT, build_paths, configured_max_ideas, load_project_config, management_python
from project_config import project_target_venue
from runtime_env import find_binary as runtime_find_binary
from auto_research.paths import LEGACY_RUNS_DIR, RUNS_DIR


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def parse_iso_time(value: Any) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _append_revision_payload_times(candidates: list[dt.datetime], payload: Any, *, run_id: str = "") -> None:
    if not isinstance(payload, dict):
        return
    payload_run_id = str(payload.get("run_id") or payload.get("current_find_run_id") or "").strip()
    if run_id and payload_run_id and payload_run_id != run_id:
        return
    for key in ["updated_at", "generated_at", "acquired_at", "checked_at"]:
        parsed = parse_iso_time(payload.get(key))
        if parsed is not None:
            candidates.append(parsed)
    strict = payload.get("strict_reclassification")
    if isinstance(strict, dict):
        parsed = parse_iso_time(strict.get("generated_at"))
        if parsed is not None:
            candidates.append(parsed)


def _append_revision_file_time(candidates: list[dt.datetime], file_path: Path, *, run_id: str = "") -> None:
    payload = load_json(file_path, {})
    if isinstance(payload, dict):
        payload_run_id = str(payload.get("run_id") or payload.get("current_find_run_id") or "").strip()
        if run_id and payload_run_id and payload_run_id != run_id:
            return
        _append_revision_payload_times(candidates, payload, run_id=run_id)
    try:
        candidates.append(dt.datetime.fromtimestamp(file_path.stat().st_mtime, dt.timezone.utc))
    except OSError:
        pass


def current_find_revision_time(paths, find_results: dict[str, Any]) -> dt.datetime | None:
    candidates: list[dt.datetime] = []
    run_id = str((find_results if isinstance(find_results, dict) else {}).get("run_id") or "").strip()
    _append_revision_payload_times(candidates, find_results, run_id=run_id)
    taste_dir = paths.planning / "finding"
    for file_path in [
        taste_dir / "find_results.json",
        taste_dir / "find_progress.json",
        taste_dir / "full_text_reading" / "full_text_packet.json",
        paths.state / "current_find_full_text_evidence_repair.json",
    ]:
        _append_revision_file_time(candidates, file_path, run_id=run_id)
    return max(candidates) if candidates else None


def claude_takeover_is_current(takeover: Any, current_revision: dt.datetime | None) -> bool:
    if not isinstance(takeover, dict) or takeover.get("return_code") != 0:
        return False
    status = str(takeover.get("status") or "").strip().lower()
    if "stale" in status or "missing_current_find_takeover" in status:
        return False
    if not str(takeover.get("prompt_path") or "").strip():
        return False
    finished = parse_iso_time(takeover.get("finished_at")) or parse_iso_time(takeover.get("started_at"))
    if finished is None:
        return False
    if current_revision is None:
        return True
    return finished + dt.timedelta(seconds=2) >= current_revision


def record_stale_claude_takeover(paths, run_id: str, takeover: Any, current_revision: dt.datetime | None) -> None:
    if not isinstance(takeover, dict) or takeover.get("return_code") != 0:
        return
    payload = {
        "run_id": run_id,
        "status": "stale_current_find_claude_takeover",
        "detected_at": now_iso(),
        "takeover_finished_at": takeover.get("finished_at"),
        "current_find_revision_at": current_revision.isoformat() if current_revision is not None else "",
        "policy": "A Claude current-Find takeover may only be normalized when it finished after the latest find_results/find_progress revision or strict reclassification. Stale takeover output must not be presented as current Claude understanding.",
    }
    save_json(paths.state / "current_find_claude_takeover_stale.json", payload)


def claude_output_payloads_are_current(payloads: list[Any], current_revision: dt.datetime | None) -> bool:
    if current_revision is None:
        return True
    for payload in payloads:
        if not isinstance(payload, dict):
            return False
        generated = parse_iso_time(payload.get("generated_at"))
        if generated is None or generated + dt.timedelta(seconds=2) < current_revision:
            return False
    return True


def claude_output_payloads_or_files_are_current(payloads: list[Any], file_paths: list[Path], current_revision: dt.datetime | None) -> bool:
    if current_revision is None:
        return True
    for payload, file_path in zip(payloads, file_paths):
        if not isinstance(payload, dict):
            return False
        generated_raw = str(payload.get("generated_at") or "").strip()
        generated = parse_iso_time(generated_raw)
        candidates: list[dt.datetime] = []
        if generated is not None:
            candidates.append(generated)
        try:
            candidates.append(dt.datetime.fromtimestamp(Path(file_path).stat().st_mtime, dt.timezone.utc))
        except OSError:
            pass
        if not candidates:
            return False
        if max(candidates) + dt.timedelta(seconds=2) < current_revision:
            return False
    return True


def current_reading_validation_needs_full_text_evidence(validation: Any) -> bool:
    if not isinstance(validation, dict) or validation.get("valid") is True:
        return False
    if _positive_int(validation.get("pending_without_evidence_count")) > 0:
        return True
    if _positive_int(validation.get("pending_deep_read_synthesis_count")) > 0 or validation.get("deep_read_content_gap_details"):
        return False
    blocker_text = " ".join(str(item or "") for item in as_list(validation.get("blockers"))).lower()
    return bool("lack full-text evidence" in blocker_text or ("full-text evidence" in blocker_text and "still lack" in blocker_text))


def current_reading_validation_needs_claude_rewrite(validation: Any) -> bool:
    if not isinstance(validation, dict) or validation.get("valid") is True:
        return False
    if validation.get("status") == "artifact_parse_failed" or validation.get("artifact_parse_failures"):
        return True
    if _positive_int(validation.get("pending_deep_read_synthesis_count")) > 0:
        return True
    if validation.get("deep_read_content_gap_details"):
        return True
    if _positive_int(validation.get("pending_full_text_reading_count")) > 0:
        return not current_reading_validation_needs_full_text_evidence(validation)
    if validation.get("blockers"):
        return True
    return False


def current_reading_validation_requires_fresh_takeover(validation: Any, run_id: str) -> bool:
    """Return True only when Claude must rewrite current-Find artifacts.

    Missing full-text/PDF/HTML evidence is an upstream evidence acquisition
    blocker. Re-running Claude over the same unavailable paper only reproduces
    the same blocked artifact, so The workflow must first acquire or prove the missing
    full-text source before another Read/Idea/Plan rewrite is useful.
    """
    if not isinstance(validation, dict):
        return False
    if str(validation.get("run_id") or "").strip() != str(run_id or "").strip():
        return False
    return current_reading_validation_needs_claude_rewrite(validation)


def current_reading_validation_ready(validation: Any, run_id: str, required_count: int) -> bool:
    if not isinstance(validation, dict):
        return False
    if str(validation.get("run_id") or "").strip() != str(run_id or "").strip():
        return False
    if validation.get("valid") is not True:
        return False
    if validation.get("policy_version") != FULL_TEXT_READ_POLICY_VERSION:
        return False
    required = required_count or _positive_int(validation.get("expected_recommendation_count"))
    return bool(
        required
        and _positive_int(validation.get("actual_reading_count")) == required
        and _positive_int(validation.get("full_text_reading_count")) >= required
        and _positive_int(validation.get("pending_full_text_reading_count")) == 0
        and not validation.get("blockers")
    )


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def load_json_with_error(path: Path, default: Any) -> tuple[Any, dict[str, Any] | None]:
    if not path.exists():
        return default, None
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except json.JSONDecodeError as exc:
        return default, {
            "path": str(path),
            "error_type": "json_decode_error",
            "message": exc.msg,
            "line": exc.lineno,
            "column": exc.colno,
            "position": exc.pos,
        }
    except Exception as exc:
        return default, {
            "path": str(path),
            "error_type": exc.__class__.__name__,
            "message": str(exc),
        }


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


CURRENT_FIND_CONTENT_ARTIFACT_NAMES = (
    "read_results.json",
    "read.md",
    "ideas.json",
    "idea.md",
    "plans.json",
    "plan.md",
)
CURRENT_FIND_JSON_ARTIFACT_NAMES = ("read_results.json", "ideas.json", "plans.json")
CURRENT_FIND_DEEP_READ_FRAGMENT_DIR_NAME = "current_find_deep_read_fragments"
CURRENT_FIND_DEEP_READ_FRAGMENT_SOURCE = "claude_subagent_deep_read_fragment"
CURRENT_FIND_DEEP_READ_FRAGMENT_REPAIR_SOURCE = "claude_subagent_deep_read_fragment_repair"
LLM_CURRENT_FIND_FALLBACK_SOURCE = "llm_current_find_fallback"
CURRENT_FIND_DEEP_READ_FRAGMENT_SOURCES = {CURRENT_FIND_DEEP_READ_FRAGMENT_SOURCE, CURRENT_FIND_DEEP_READ_FRAGMENT_REPAIR_SOURCE}


def _safe_timestamp_for_path(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]+", "-", str(value or "")).strip("-") or "time"


def _current_find_artifact_path(paths, name: str) -> Path:
    return paths.planning / "finding" / name


def snapshot_current_find_artifacts(paths, run_id: str, attempt: int) -> dict[str, Any]:
    created_at = now_iso()
    backup_dir = paths.state / "current_find_artifact_backups" / f"{_safe_timestamp_for_path(created_at)}_attempt{attempt}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, Any]] = []
    for name in CURRENT_FIND_CONTENT_ARTIFACT_NAMES:
        source = _current_find_artifact_path(paths, name)
        backup = backup_dir / name
        record = {"name": name, "source": str(source), "backup": str(backup), "existed": source.exists()}
        if source.exists():
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, backup)
            record["size"] = backup.stat().st_size
        files.append(record)
    fragment_dir = paths.planning / "finding" / CURRENT_FIND_DEEP_READ_FRAGMENT_DIR_NAME
    fragment_records: list[dict[str, Any]] = []
    if fragment_dir.exists():
        fragment_backup_dir = backup_dir / CURRENT_FIND_DEEP_READ_FRAGMENT_DIR_NAME
        for source in sorted(fragment_dir.glob("*.json")):
            backup = fragment_backup_dir / source.name
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, backup)
            fragment_records.append({"name": source.name, "source": str(source), "backup": str(backup), "size": backup.stat().st_size})
    snapshot = {
        "status": "snapshotted",
        "run_id": run_id,
        "attempt": attempt,
        "created_at": created_at,
        "backup_dir": str(backup_dir),
        "files": files,
        "deep_read_fragments": fragment_records,
    }
    save_json(backup_dir / "manifest.json", snapshot)
    save_json(paths.state / "current_find_artifact_transaction_last_snapshot.json", snapshot)
    return snapshot


def current_find_json_artifact_failures(paths) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for name in CURRENT_FIND_JSON_ARTIFACT_NAMES:
        path = _current_find_artifact_path(paths, name)
        _payload, error = load_json_with_error(path, {})
        if isinstance(error, dict):
            failures.append({**error, "artifact": name})
    return failures


def restore_current_find_artifact_snapshot(paths, snapshot: dict[str, Any], reason: str, parse_failures: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    restored: list[dict[str, Any]] = []
    for record in as_list((snapshot if isinstance(snapshot, dict) else {}).get("files")):
        if not isinstance(record, dict):
            continue
        name = str(record.get("name") or "").strip()
        if not name:
            continue
        target = _current_find_artifact_path(paths, name)
        backup = Path(str(record.get("backup") or ""))
        existed = bool(record.get("existed"))
        if existed and backup.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, target)
            restored.append({"name": name, "action": "restored", "target": str(target), "backup": str(backup)})
        elif not existed and target.exists():
            target.unlink()
            restored.append({"name": name, "action": "removed_new_file", "target": str(target)})
    receipt = {
        "status": "restored",
        "run_id": (snapshot if isinstance(snapshot, dict) else {}).get("run_id"),
        "attempt": (snapshot if isinstance(snapshot, dict) else {}).get("attempt"),
        "restored_at": now_iso(),
        "reason": reason,
        "parse_failures": parse_failures or [],
        "snapshot_path": str(Path(str((snapshot if isinstance(snapshot, dict) else {}).get("backup_dir") or "")) / "manifest.json") if isinstance(snapshot, dict) and snapshot.get("backup_dir") else "",
        "restored_files": restored,
        "policy": "Current-Find content artifacts are transaction-protected. If Claude is stopped by tool policy, exits nonzero, or leaves invalid JSON, TASTE restores the previous parseable artifacts and records this receipt instead of accepting partial scientific content.",
    }
    save_json(paths.state / "current_find_artifact_transaction_restore.json", receipt)
    return receipt


def _pending_current_find_artifact_payload(name: str, run_id: str, reason: str) -> dict[str, Any]:
    payload = {"run_id": run_id, "source": f"pending_after_{reason}", "status": "pending", "created_at": now_iso()}
    if name == "read_results.json":
        payload["readings"] = []
    elif name == "ideas.json":
        payload["ideas"] = []
    elif name == "plans.json":
        payload["plans"] = []
    return payload


def quarantine_corrupt_current_find_json_artifacts(paths, run_id: str, failures: list[dict[str, Any]], reason: str) -> dict[str, Any]:
    if not failures:
        return {"status": "not_needed", "run_id": run_id, "reason": reason, "files": []}
    created_at = now_iso()
    backup_dir = paths.state / "current_find_artifact_backups" / f"corrupt_{_safe_timestamp_for_path(created_at)}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, Any]] = []
    for failure in failures:
        name = str((failure if isinstance(failure, dict) else {}).get("artifact") or "").strip()
        if name not in CURRENT_FIND_JSON_ARTIFACT_NAMES:
            continue
        target = _current_find_artifact_path(paths, name)
        backup = backup_dir / name
        record = {"name": name, "target": str(target), "backup": str(backup), "reason": reason}
        if target.exists():
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
            record["size"] = backup.stat().st_size
        save_json(target, _pending_current_find_artifact_payload(name, run_id, reason))
        record["action"] = "quarantined_and_reset_to_pending"
        files.append(record)
    receipt = {
        "status": "quarantined_corrupt_json_artifacts",
        "run_id": run_id,
        "created_at": created_at,
        "reason": reason,
        "parse_failures": failures,
        "backup_dir": str(backup_dir),
        "files": files,
        "policy": "A new current-Find Claude takeover must not snapshot already-corrupt JSON as the rollback base; corrupt artifacts are quarantined and replaced by same-run pending shells before the next Claude attempt.",
    }
    save_json(paths.state / "current_find_corrupt_artifact_quarantine.json", receipt)
    return receipt


def current_find_deep_read_fragment_failures(paths, run_id: str) -> list[dict[str, Any]]:
    fragment_dir = paths.planning / "finding" / CURRENT_FIND_DEEP_READ_FRAGMENT_DIR_NAME
    expected = str(run_id or "").strip()
    failures: list[dict[str, Any]] = []
    if not fragment_dir.exists():
        return failures
    for fragment_path in sorted(fragment_dir.glob("*.json")):
        artifact = f"{CURRENT_FIND_DEEP_READ_FRAGMENT_DIR_NAME}/{fragment_path.name}"
        payload, error = load_json_with_error(fragment_path, {})
        if isinstance(error, dict):
            failures.append({**error, "artifact": artifact, "fragment_name": fragment_path.name})
            continue
        payload_run = str((payload if isinstance(payload, dict) else {}).get("run_id") or (payload if isinstance(payload, dict) else {}).get("current_find_run_id") or "").strip()
        if payload_run and expected and payload_run != expected:
            failures.append({
                "path": str(fragment_path),
                "artifact": artifact,
                "fragment_name": fragment_path.name,
                "error_type": "run_id_mismatch",
                "message": f"fragment run_id {payload_run} does not match current run_id {expected}",
            })
    return failures


def _deep_read_fragment_repair_key(name: Any) -> str:
    stem = Path(str(name or "")).stem
    for marker in ("_repair_attempt", "_repair"):
        if marker in stem:
            stem = stem.split(marker, 1)[0]
            break
    return stem.strip()


def _deep_read_fragment_rows_from_payload(payload: Any, path: Path, run_id: str) -> list[dict[str, Any]]:
    if not _fragment_payload_is_current(payload, path, run_id, None):
        return []
    rows: list[dict[str, Any]] = []
    if isinstance(payload, dict) and isinstance(payload.get("reading"), dict):
        rows.append(_merge_fragment_top_level_reading_fields(payload, payload["reading"]))
    rows.extend(_reading_rows_from_subagent_payload(payload))
    return [row for row in rows if isinstance(row, dict) and _valid_claude_reading(row)]


def _deep_read_fragment_identity_keys(rows: list[dict[str, Any]]) -> set[str]:
    keys: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        for identity in _identity_values(row):
            keys.add(f"identity:{identity}")
    return keys


def _current_find_valid_fragment_replacements(fragment_dir: Path, run_id: str, excluded_paths: set[Path]) -> dict[str, list[dict[str, Any]]]:
    replacements: dict[str, list[dict[str, Any]]] = {}
    if not fragment_dir.exists():
        return replacements
    for path in sorted(fragment_dir.glob("*.json")):
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in excluded_paths:
            continue
        payload, error = load_json_with_error(path, {})
        if isinstance(error, dict):
            continue
        rows = _deep_read_fragment_rows_from_payload(payload, path, run_id)
        if not rows:
            continue
        record = {
            "name": path.name,
            "path": str(path),
            "reading_count": len(rows),
            "paper_ids": [str(row.get("paper_id") or row.get("id") or "").strip() for row in rows if str(row.get("paper_id") or row.get("id") or "").strip()],
            "titles": [str(row.get("title") or "").strip() for row in rows if str(row.get("title") or "").strip()],
        }
        keys = {_deep_read_fragment_repair_key(path.name)} | _deep_read_fragment_identity_keys(rows)
        for key in keys:
            if key:
                replacements.setdefault(key, []).append(record)
    return replacements


def _fragment_failure_replacement_keys(failure: dict[str, Any]) -> set[str]:
    path = Path(str((failure if isinstance(failure, dict) else {}).get("path") or ""))
    keys = {_deep_read_fragment_repair_key((failure if isinstance(failure, dict) else {}).get("fragment_name") or path.name)}
    payload, error = load_json_with_error(path, {}) if str(path) else ({}, {"error_type": "missing_path"})
    if not isinstance(error, dict) and isinstance(payload, dict):
        rows: list[dict[str, Any]] = []
        if isinstance(payload.get("reading"), dict):
            rows.append(_merge_fragment_top_level_reading_fields(payload, payload["reading"]))
        rows.extend(_reading_rows_from_subagent_payload(payload))
        keys |= _deep_read_fragment_identity_keys([row for row in rows if isinstance(row, dict)])
    return {key for key in keys if key}


def quarantine_corrupt_current_find_deep_read_fragments(paths, run_id: str, failures: list[dict[str, Any]] | None = None, reason: str = "validated_current_find_fragment_repair") -> dict[str, Any]:
    fragment_dir = paths.planning / "finding" / CURRENT_FIND_DEEP_READ_FRAGMENT_DIR_NAME
    failures = failures if failures is not None else current_find_deep_read_fragment_failures(paths, run_id)
    if not failures:
        return {"status": "not_needed", "run_id": run_id, "reason": reason, "files": []}
    created_at = now_iso()
    backup_dir = paths.state / "current_find_artifact_backups" / f"corrupt_{_safe_timestamp_for_path(created_at)}" / CURRENT_FIND_DEEP_READ_FRAGMENT_DIR_NAME
    excluded: set[Path] = set()
    for failure in failures:
        path = Path(str((failure if isinstance(failure, dict) else {}).get("path") or ""))
        if str(path):
            try:
                excluded.add(path.resolve())
            except OSError:
                excluded.add(path)
    replacements = _current_find_valid_fragment_replacements(fragment_dir, run_id, excluded)
    files: list[dict[str, Any]] = []
    for failure in failures:
        target = Path(str((failure if isinstance(failure, dict) else {}).get("path") or ""))
        name = str((failure if isinstance(failure, dict) else {}).get("fragment_name") or target.name or "").strip()
        record = {"name": name, "target": str(target), "reason": reason, "parse_failure": failure}
        try:
            target.relative_to(fragment_dir)
        except ValueError:
            record["action"] = "skipped_outside_current_find_fragment_dir"
            files.append(record)
            continue
        matches = [item for key in _fragment_failure_replacement_keys(failure) for item in replacements.get(key, [])]
        unique_matches = list({item["path"]: item for item in matches}.values())
        if not target.exists():
            record["action"] = "skipped_missing"
        elif not unique_matches:
            record["action"] = "skipped_no_valid_same_run_replacement"
        else:
            backup = backup_dir / name
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(backup))
            record.update({"action": "quarantined_with_valid_same_run_replacement", "backup": str(backup), "replacement_fragments": unique_matches, "size": backup.stat().st_size})
        files.append(record)
    moved = [row for row in files if row.get("action") == "quarantined_with_valid_same_run_replacement"]
    receipt = {
        "status": "quarantined_corrupt_deep_read_fragments" if moved else "no_fragments_quarantined",
        "run_id": run_id,
        "created_at": created_at,
        "reason": reason,
        "parse_failures": failures,
        "backup_dir": str(backup_dir),
        "files": files,
        "policy": "Invalid current-Find deep-read fragments may be removed from the active directory only when a parseable same-run fragment with valid deep-read fields already replaces the same fragment/identity. Scientific content is not generated or edited by this quarantine step.",
    }
    save_json(paths.state / "current_find_corrupt_deep_read_fragment_quarantine.json", receipt)
    return receipt


def compact(value: Any, limit: int = 900) -> str:
    text = " ".join(str(value or "").replace("\r", "\n").split())
    return text if len(text) <= limit else text[: max(0, limit - 3)].rstrip() + "..."


def contains_cjk(value: Any) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", str(value or "")))


PUBLIC_STATUS_EN: dict[str, str] = {
    "approved": "approved for planning",
    "approved_for_planning": "approved for planning",
    "wait_for_environment_base_selection": "waiting for environment review",
    "waiting_for_environment_base_selection": "waiting for environment review",
    "wait_for_literature_gate_repair": "repair literature gate before environment review",
    "wait_for_literature_gate_repair_then_environment_base_selection": "repair literature gate before environment review",
    "blocked_literature_recommendation_gate": "blocked by literature recommendation gate",
}
TRUSTED_PUBLIC_PROJECTION_SOURCES = {
    "project_agent_public_i18n",
    "current_find_project_agent_i18n",
    "claude_code_current_find_public_i18n",
}
PUBLIC_PROJECTION_EN_FIELDS = [
    "title_en",
    "hypothesis_en",
    "mechanism_en",
    "rationale_en",
    "new_method_en",
    "method_details_en",
    "initial_experiment_en",
    "min_experiment_en",
    "minimum_experiment_en",
    "experiment_design_en",
    "experimental_design_en",
]
GENERIC_MIN_EXPERIMENT_EN = "Initial experiment details are pending project-agent completion from the current full readings; no generic experiment placeholder is accepted as a design."
GENERIC_IDEA_GUARDRAIL_EN = "Generated from the current Find/read packet by the project agent. It may guide planning, but it does not bind a repository, dataset, command, or paper claim before the required gates pass."
GENERIC_PLAN_STEPS_EN = [
    "Verify the current Find run ID and guarded read/idea/plan outputs.",
    "Audit candidate repositories, data, and protocols in the environment stage.",
    "Run same-protocol baseline/candidate/ablation experiments only after gates pass.",
    "Refresh scientific-progress, paper-evidence, and submission-readiness gates.",
]
GENERIC_SUCCESS_GATES_EN = [
    "environment review completed",
    "repo/data/protocol checks passed",
    "metrics and bad cases written",
    "evidence gates refreshed",
]


def public_status_en(value: Any) -> str:
    text = str(value or "").strip()
    return PUBLIC_STATUS_EN.get(text, text.replace("_", " "))


def _public_display_sanitize_en(value: Any) -> str:
    text = compact(value, 1800)
    if not text:
        return ""
    replacements = [
        (r"\bClaude Code\b", "project agent"),
        (r"\benvironment-stage base selection\b", "environment review"),
        (r"\benvironment-stage\b", "environment"),
        (r"\bno environment base is selected\b", "environment review is pending"),
        (r"\bno base is selected\b", "environment review is pending"),
        (r"\bbase is selected\b", "environment review is completed"),
        (r"\bcurrent base's\b", "the selected repository's"),
        (r"\bcurrent base\b", "selected repository"),
        (r"\bselected base\b", "selected repository"),
        (r"\bbase switch\b", "route change"),
        (r"\bbase-switch\b", "route-change"),
        (r"\bclaim-ready\b", "auditable"),
        (r"\bclaim ready\b", "auditable"),
        (r"\bpaper claim\b", "paper conclusion"),
        (r"\bclaim promotion\b", "paper-conclusion promotion"),
    ]
    for pattern, repl in replacements:
        text = re.sub(pattern, repl, text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def _public_en_text_ok(value: Any, *, min_words: int = 4) -> bool:
    text = _public_display_sanitize_en(value)
    if not text or contains_cjk(text):
        return False
    lowered = text.lower()
    blocked = [
        "available in the chinese view",
        "english projection is pending",
        "project-agent idea text is available",
        "chinese project text",
        "llmsemantic-gated",
        "moearchitecture",
        "moegate",
        "prefergrow preference ratio",
    ]
    if any(marker in lowered for marker in blocked):
        return False
    words = re.findall(r"[A-Za-z0-9]+", text)
    return len(words) >= min_words


def _trusted_public_projection(row: dict[str, Any]) -> bool:
    projection = row.get("public_projection") if isinstance(row.get("public_projection"), dict) else {}
    source = str(projection.get("source") or row.get("public_projection_source") or "").strip()
    return source in TRUSTED_PUBLIC_PROJECTION_SOURCES or projection.get("trusted") is True


def _cleuntrusted_public_en(row: dict[str, Any]) -> None:
    if _trusted_public_projection(row):
        return
    for key in PUBLIC_PROJECTION_EN_FIELDS:
        row.pop(key, None)
    for key in ["bad_case_slice_en", "success_gate_en", "steps_en", "status_en", "recommendation_en", "guardrail_en"]:
        row.pop(key, None)


def _trusted_public_en(row: dict[str, Any], key: str, *, min_words: int = 4) -> str:
    i18n = row.get(f"{key}_i18n") if isinstance(row.get(f"{key}_i18n"), dict) else {}
    value = str(i18n.get("en") or row.get(f"{key}_en") or "").strip()
    if value and (_trusted_public_projection(row) or isinstance(i18n, dict)) and _public_en_text_ok(value, min_words=min_words):
        return compact(_public_display_sanitize_en(value), 1400)
    return ""


def _generic_public_gate_list_en(values: Any) -> list[str]:
    mapped = {
        "Environment validates candidate base proposal": "environment review completed",
        "environment review completed": "environment review completed",
        "repo/data/env/protocol gate passed": "repo/data/environment/protocol checks passed",
        "repo/data/protocol evidence ready": "repo/data/protocol evidence ready",
        "metrics and bad cases written": "metrics and bad cases written",
        "evidence gates refreshed": "evidence gates refreshed",
        "local evidence gates pass after Environment validation": "local evidence gates pass after Environment validation",
        "audit JSON exists": "local audit JSON exists",
        "metrics parsed": "metrics parsed",
        "bad-case slice written": "bad-case slices written",
    }
    out: list[str] = []
    for value in as_list(values):
        raw = str(value or "").strip()
        text = mapped.get(raw, raw.replace("_", " "))
        if text and not contains_cjk(text) and text not in out:
            out.append(text)
    return out


def enrich_public_idea_projection(idea: dict[str, Any], idx: int) -> dict[str, Any]:
    _cleuntrusted_public_en(idea)
    title_en = _trusted_public_en(idea, "title", min_words=2)
    new_method_en = _trusted_public_en(idea, "new_method", min_words=12) or _trusted_public_en(idea, "hypothesis", min_words=12)
    mechanism_en = _trusted_public_en(idea, "method_details", min_words=12) or _trusted_public_en(idea, "mechanism", min_words=12) or _trusted_public_en(idea, "rationale", min_words=12)
    min_experiment_en = _trusted_public_en(idea, "initial_experiment", min_words=8) or _trusted_public_en(idea, "min_experiment", min_words=8) or _trusted_public_en(idea, "minimum_experiment", min_words=8)
    if title_en:
        idea["title_en"] = title_en
    if new_method_en:
        idea["new_method_en"] = new_method_en
        idea["hypothesis_en"] = new_method_en
    if mechanism_en:
        idea["method_details_en"] = mechanism_en
        idea["mechanism_en"] = mechanism_en
    if min_experiment_en and not _generic_idea_experiment(min_experiment_en):
        idea["initial_experiment_en"] = min_experiment_en
        idea["min_experiment_en"] = min_experiment_en
    idea["status_en"] = public_status_en(idea.get("status"))
    idea["recommendation_en"] = public_status_en(idea.get("recommendation"))
    idea["bad_case_slice_en"] = _generic_public_gate_list_en(idea.get("bad_case_slice")) or ["cold-start or sparse cases", "long-tail cases", "high-confidence errors", "semantic-behavior conflicts"]
    idea["success_gate_en"] = _generic_public_gate_list_en(idea.get("success_gate")) or list(GENERIC_SUCCESS_GATES_EN)
    idea["guardrail_en"] = _trusted_public_en(idea, "guardrail", min_words=10) or GENERIC_IDEA_GUARDRAIL_EN
    projection = idea.get("public_projection") if isinstance(idea.get("public_projection"), dict) else {}
    projection.update({
        "status": "project_agent_i18n_ready" if title_en and new_method_en and mechanism_en else "needs_project_agent_i18n",
        "source": projection.get("source") if projection.get("source") in TRUSTED_PUBLIC_PROJECTION_SOURCES else "current_find_public_projection_guard",
        "required_fields": ["title_en", "new_method_en", "method_details_en", "initial_experiment_en"],
    })
    idea["public_projection"] = projection
    return idea


def first_nonempty_success_gate_en(plan: dict[str, Any]) -> list[str]:
    versions = as_list(plan.get("versions"))
    for version in versions:
        if isinstance(version, dict):
            implementation = version.get("implementation") if isinstance(version.get("implementation"), dict) else {}
            gates = _generic_public_gate_list_en(implementation.get("success_gate")) if isinstance(implementation, dict) else []
            if gates:
                return gates
    return ["environment review completed", "local evidence gates pass after Environment validation"]


def enrich_public_plan_projection(plan: dict[str, Any], idx: int, idea_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    _cleuntrusted_public_en(plan)
    idea = idea_by_id.get(str(plan.get("idea_id") or ""), {})
    title_en = _trusted_public_en(plan, "title", min_words=2) or _trusted_public_en(idea, "title", min_words=2)
    hypothesis_en = _trusted_public_en(plan, "hypothesis", min_words=12) or _trusted_public_en(idea, "hypothesis", min_words=12)
    experiment_design_en = (
        _trusted_public_en(plan, "experiment_design", min_words=8)
        or _trusted_public_en(plan, "experimental_design", min_words=8)
        or _trusted_public_en(plan, "minimum_experiment", min_words=8)
        or _trusted_public_en(idea, "min_experiment", min_words=8)
        or GENERIC_MIN_EXPERIMENT_EN
    )
    if title_en:
        plan["title_en"] = title_en
    if hypothesis_en:
        plan["hypothesis_en"] = hypothesis_en
    plan["experiment_design_en"] = experiment_design_en
    plan["minimum_experiment_en"] = experiment_design_en
    plan["status_en"] = public_status_en(plan.get("status"))
    trusted_steps = as_list(plan.get("steps_en")) if _trusted_public_projection(plan) and as_list(plan.get("steps_en")) else []
    plan["steps_en"] = [_public_display_sanitize_en(item) for item in trusted_steps if _public_en_text_ok(item, min_words=3)] or list(GENERIC_PLAN_STEPS_EN)
    trusted_gates = as_list(plan.get("success_gate_en")) if _trusted_public_projection(plan) and as_list(plan.get("success_gate_en")) else []
    plan["success_gate_en"] = [_public_display_sanitize_en(item) for item in trusted_gates if _public_en_text_ok(item, min_words=2)] or _generic_public_gate_list_en(plan.get("success_gate")) or first_nonempty_success_gate_en(plan) or list(GENERIC_SUCCESS_GATES_EN)
    versions = as_list(plan.get("versions"))
    for version in versions:
        if not isinstance(version, dict):
            continue
        final = version.get("final_plan") if isinstance(version.get("final_plan"), dict) else {}
        if isinstance(final, dict):
            final.setdefault("experimental_design_en", plan["experiment_design_en"])
            final.setdefault("steps_en", plan["steps_en"])
        implementation = version.get("implementation") if isinstance(version.get("implementation"), dict) else {}
        if isinstance(implementation, dict):
            implementation.setdefault("minimum_experiment_en", plan["experiment_design_en"])
            implementation.setdefault("success_gate_en", plan["success_gate_en"])
    projection = plan.get("public_projection") if isinstance(plan.get("public_projection"), dict) else {}
    projection.update({
        "status": "project_agent_i18n_ready" if title_en and hypothesis_en else "needs_project_agent_i18n",
        "source": projection.get("source") if projection.get("source") in TRUSTED_PUBLIC_PROJECTION_SOURCES else "current_find_public_projection_guard",
        "required_fields": ["title_en", "hypothesis_en", "experiment_design_en"],
    })
    plan["public_projection"] = projection
    return plan


def enrich_public_projections(ideas: list[dict[str, Any]], plans: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    for idx, idea in enumerate(ideas, 1):
        if isinstance(idea, dict):
            enrich_public_idea_projection(idea, idx)
    idea_by_id = {str(idea.get("id") or idea.get("idea_id") or ""): idea for idea in ideas if isinstance(idea, dict)}
    for idx, plan in enumerate(plans, 1):
        if isinstance(plan, dict):
            enrich_public_plan_projection(plan, idx, idea_by_id)
    return ideas, plans



def norm_title(title: Any) -> str:
    text = str(title or "").strip().lower()
    text = (
        text.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201b", "'")
        .replace("`", "'")
        .replace("\u00b4", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2013", "-")
        .replace("\u2014", "-")
    )
    return re.sub(r"\s+", " ", text).strip()


def _reading_content_view(row: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    nested = row.get("reading") if isinstance(row.get("reading"), dict) else None
    if not nested:
        return row
    clean = dict(nested)
    for key, value in row.items():
        if key == "reading":
            continue
        if value not in (None, "", []):
            clean.setdefault(key, value)
    return clean


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def first_text(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, "", []):
            if isinstance(value, list):
                return ", ".join(str(item) for item in value if str(item).strip())
            return str(value)
    return ""


GENERIC_IDEA_EXPERIMENT_MARKERS = (
    "after environment-stage base selection",
    "after environment review",
    "run a minimal same-protocol baseline/candidate/ablation",
    "baseline/candidate/ablation experiment with audited metrics and bad cases",
)


def _generic_idea_experiment(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return bool(text and any(marker in text for marker in GENERIC_IDEA_EXPERIMENT_MARKERS))


CURRENT_FIND_EXECUTION_TRUE_VALUES = {"1", "true", "yes", "y", "selected", "select", "execute", "execute_next", "primary", "best", "best_idea", "best_plan"}
CURRENT_FIND_EXECUTION_FALSE_VALUES = {"0", "false", "no", "n", "rejected", "reject", "skip", "backlog", "candidate_only", "not_selected"}
CURRENT_FIND_SELECTION_FIELD_KEYS = ["selected_idea_id", "selected_plan_id", "selected_idea", "selected_plan", "selected_by", "execution_policy"]
CURRENT_FIND_SELECTION_FAILURE_TYPES = {"missing_selected_plan", "ambiguous_selected_plan", "selected_plan_id_missing", "selected_plan_missing_matching_idea"}


def _execution_truthy(value: Any) -> bool:
    if value is True:
        return True
    if value in (False, None, ""):
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in CURRENT_FIND_EXECUTION_TRUE_VALUES


def _execution_falsey(value: Any) -> bool:
    if value is False:
        return True
    if value in (None, ""):
        return False
    return str(value).strip().lower() in CURRENT_FIND_EXECUTION_FALSE_VALUES


def _current_find_idea_key(row: dict[str, Any]) -> str:
    return str(row.get("id") or row.get("idea_id") or row.get("title") or "").strip()


def _current_find_plan_id(row: dict[str, Any]) -> str:
    return str(row.get("plan_id") or row.get("id") or "").strip()


def _current_find_plan_idea_key(row: dict[str, Any]) -> str:
    return str(row.get("idea_id") or row.get("id") or row.get("title") or "").strip()


def _current_find_status_allows_selection(row: dict[str, Any]) -> bool:
    status = str(row.get("status") or row.get("recommendation") or "").strip().lower()
    if status in {"deleted", "rejected", "reject", "archived", "blocked", "blocked_with_reason"}:
        return False
    if row.get("approved") is True or row.get("approved_for_planning") is True or row.get("pursue") is True:
        return True
    return not status or status == "approved" or "approved" in status or "pursue" in status or "ready" in status


def _current_find_explicit_execution_selection(row: dict[str, Any], *, kind: str) -> bool:
    keys = ["selected_for_execution", "execute_next", "primary", "selected"]
    keys.append("best_idea" if kind == "idea" else "best_plan")
    for key in keys:
        if _execution_truthy(row.get(key)):
            return True
        if _execution_falsey(row.get(key)):
            return False
    selection = row.get("execution_selection") if isinstance(row.get("execution_selection"), dict) else {}
    for key in ["selected", "selected_for_execution", "execute_next", "primary"]:
        if _execution_truthy(selection.get(key)):
            return True
    decision = str(row.get("execution_decision") or row.get("selection_decision") or "").strip().lower()
    return decision in CURRENT_FIND_EXECUTION_TRUE_VALUES


def _current_find_numeric(value: Any, default: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip().lower()
    if not text:
        return default
    mapping = {"very_high": 4.0, "high": 3.0, "medium": 2.0, "med": 2.0, "low": 1.0, "very_low": 0.5}
    if text in mapping:
        return mapping[text]
    try:
        return float(text)
    except ValueError:
        return default


def _current_find_execution_score(row: dict[str, Any], index: int = 0) -> float:
    rank = _current_find_numeric(row.get("execution_rank") or row.get("rank") or row.get("idea_rank") or row.get("plan_rank"), 0.0)
    score = 1000.0 - rank if rank > 0 else 0.0
    for key in ["execution_score", "judge_score", "idea_score", "plan_score", "score", "feasibility_score", "evidence_score"]:
        score += _current_find_numeric(row.get(key), 0.0)
    for key in ["evidence_strength", "feasibility", "novelty", "readiness"]:
        score += _current_find_numeric(row.get(key), 0.0)
    return score - index * 0.0001


def _current_find_select_item(rows: list[dict[str, Any]], *, kind: str) -> tuple[dict[str, Any] | None, str]:
    candidates = [row for row in rows if isinstance(row, dict)]
    if not candidates:
        return None, "none"
    explicit = [row for row in candidates if _current_find_explicit_execution_selection(row, kind=kind)]
    if not explicit:
        return None, "no_explicit_selection"
    if len(explicit) > 1:
        return None, "ambiguous_explicit_selection"
    return explicit[0], "explicit"


def _current_find_selected_summary(row: dict[str, Any] | None, *, kind: str) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    keys = ["title", "new_method", "hypothesis", "method_details", "initial_experiment", "inspired_by", "supporting_papers", "status"]
    keys = (["id", "idea_id"] if kind == "idea" else ["plan_id", "idea_id"]) + keys
    return {key: row.get(key) for key in keys if key in row}


def apply_current_find_execution_selection(ideas: list[dict[str, Any]], plans: list[dict[str, Any]], *, source: str = "claude_code_current_find_takeover", executable: bool = True) -> dict[str, Any]:
    idea_rows = [row for row in ideas if isinstance(row, dict)]
    plan_rows = [row for row in plans if isinstance(row, dict)]
    explicit_plan_rows = [row for row in plan_rows if _current_find_explicit_execution_selection(row, kind="plan")]
    selected_plan: dict[str, Any] | None = explicit_plan_rows[0] if len(explicit_plan_rows) == 1 else None
    selected_idea: dict[str, Any] | None = None
    selected_by = "claude_or_human_explicit_plan_selection" if selected_plan is not None else "no_explicit_current_find_selection"
    selection_issue = ""
    if len(explicit_plan_rows) > 1:
        selected_by = "ambiguous_explicit_plan_selection"
        selection_issue = "ambiguous_selected_plan"
    elif not explicit_plan_rows and plan_rows:
        selection_issue = "missing_selected_plan"
    if selected_plan is not None:
        selected_idea = next((row for row in idea_rows if _current_find_idea_key(row) == _current_find_plan_idea_key(selected_plan)), None)
        if selected_idea is None and _current_find_plan_idea_key(selected_plan):
            selection_issue = "selected_plan_missing_matching_idea"
    selected_idea_id = _current_find_idea_key(selected_idea) if isinstance(selected_idea, dict) else str((selected_plan or {}).get("idea_id") or "").strip()
    selected_plan_id = _current_find_plan_id(selected_plan) if isinstance(selected_plan, dict) else ""
    if selected_plan is not None and not selected_plan_id:
        selection_issue = "selected_plan_id_missing"
    if selection_issue != "ambiguous_selected_plan":
        for row in idea_rows:
            chosen = bool(selected_plan_id and selected_idea_id and _current_find_idea_key(row) == selected_idea_id)
            row["selected_for_execution"] = chosen
            row["execute_next"] = chosen
            row["execution_selection"] = {
                "selected": chosen,
                "selected_plan_id": selected_plan_id if chosen else "",
                "source": source,
                "selected_by": selected_by if chosen else "not_selected_candidate_backlog",
            }
        for row in plan_rows:
            chosen = bool(selected_plan_id and _current_find_plan_id(row) == selected_plan_id)
            row["selected_for_execution"] = chosen
            row["execute_next"] = chosen
            row["execution_policy"] = {
                "status": "selected_plan_only" if chosen and executable else "blocked_selected_plan_pending_gate" if chosen else "candidate_backlog_only",
                "downstream_consumes": "selected_plan_id" if chosen else "selected plan only; this plan is not executable unless promoted by Claude/human supervision",
                "requires": [] if executable and selected_plan_id else ["current-Find full-text reading validation", "Claude read/idea/plan contract", "main Claude Code explicit selected_plan_id"],
                "source": source,
            }
    else:
        for row in plan_rows:
            row["execution_policy"] = {
                **(row.get("execution_policy") if isinstance(row.get("execution_policy"), dict) else {}),
                "status": "ambiguous_selected_plan",
                "downstream_consumes": "blocked_until_exactly_one_selected_plan_id",
                "requires": ["main Claude Code explicit exactly-one selected_plan_id"],
                "source": source,
            }
    policy_status = "selected_plan_only" if executable and selected_plan_id else "blocked_selected_plan_pending_gate" if selected_plan_id else (selection_issue or "no_selected_plan")
    return {
        "selected_idea_id": selected_idea_id if selected_plan_id else "",
        "selected_plan_id": selected_plan_id,
        "selected_idea": _current_find_selected_summary(selected_idea, kind="idea") if selected_plan_id else {},
        "selected_plan": _current_find_selected_summary(selected_plan, kind="plan") if selected_plan_id else {},
        "selected_by": selected_by,
        "selection_issue": selection_issue,
        "status": policy_status,
        "execution_policy": {
            "status": policy_status,
            "downstream_consumes": "selected_plan_id",
            "candidate_backlog_policy": "Non-selected ideas/plans remain visible for supervision but must not drive environment, experiment, paper, or claim execution.",
            "requires": [] if executable and selected_plan_id else ["current-Find full-text reading validation", "Claude read/idea/plan contract", "main Claude Code explicit selected_plan_id"],
            "source": source,
        },
    }


def current_find_selection_fields(ideas: list[dict[str, Any]], plans: list[dict[str, Any]], *, source: str = "claude_code_current_find_takeover", executable: bool = True) -> dict[str, Any]:
    selection = apply_current_find_execution_selection(ideas, plans, source=source, executable=executable)
    return {key: selection.get(key) for key in CURRENT_FIND_SELECTION_FIELD_KEYS}


def _public_ref_from_source(source: Any) -> dict[str, Any] | None:
    if isinstance(source, dict):
        title = first_text(source, "title", "paper_title", "name")
        if not title:
            return None
        return {
            "title": title,
            "source": first_text(source, "source", "venue", "evidence_role"),
            "year": first_text(source, "year"),
            "url": first_text(source, "url", "pdf_url"),
            "reason": first_text(source, "reason", "use", "mechanism", "note"),
        }
    item_text = str(source or "").strip()
    return {"title": item_text, "source": "", "year": "", "url": "", "reason": ""} if item_text else None


INTERNAL_PAPER_REF_RE = re.compile(r"^paper[_-][A-Za-z0-9]+$", re.IGNORECASE)


def _looks_like_internal_paper_ref(value: Any) -> bool:
    return bool(INTERNAL_PAPER_REF_RE.fullmatch(str(value or "").strip()))


def _paper_reference_lookup_keys(value: Any) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    lower = text.lower()
    keys = [lower, f"id:{lower}", f"paper_id:{lower}", f"entry_id:{lower}", f"paper_key:{lower}"]
    if lower.startswith("http://") or lower.startswith("https://"):
        keys.extend([f"url:{lower}", f"abs_url:{lower}", f"pdf_url:{lower}"])
    title_key = norm_title(text)
    if title_key:
        keys.append(f"title:{title_key}")
    return keys


def _iter_paper_reference_rows(value: Any):
    if isinstance(value, dict):
        if first_text(value, "title", "paper_title", "name"):
            yield value
        for key in [
            "strong_recommendations",
            "recommendations",
            "articles",
            "read_candidates",
            "readings",
            "papers",
            "selected_papers",
            "items",
        ]:
            for item in as_list(value.get(key)):
                yield from _iter_paper_reference_rows(item)
    else:
        for item in as_list(value):
            if isinstance(item, dict):
                yield from _iter_paper_reference_rows(item)


def _paper_reference_index(*sources: Any) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for source in sources:
        for row in _iter_paper_reference_rows(source):
            ref = _public_ref_from_source(row)
            if not ref or _looks_like_internal_paper_ref(ref.get("title")):
                continue
            values: list[str] = []
            for key in ["id", "paper_id", "entry_id", "url", "abs_url", "pdf_url", "doi"]:
                raw = str(row.get(key) or "").strip()
                if raw:
                    values.append(raw)
                    values.append(f"{key}:{raw}")
                    if key in {"id", "paper_id", "entry_id"}:
                        values.append(f"paper_key:{raw}")
            title_key = norm_title(ref.get("title"))
            if title_key:
                values.append(f"title:{title_key}")
            for raw in values:
                key = str(raw or "").strip().lower()
                if key and key not in index:
                    index[key] = ref
    return index


def _resolve_public_ref_from_source(source: Any, paper_index: dict[str, dict[str, Any]] | None = None) -> dict[str, Any] | None:
    ref = _public_ref_from_source(source)
    index = paper_index or {}
    lookup_values: list[Any] = []
    if isinstance(source, dict):
        for key in ["id", "paper_id", "entry_id", "url", "abs_url", "pdf_url", "doi"]:
            value = source.get(key)
            if value not in (None, ""):
                lookup_values.append(value)
        if ref and _looks_like_internal_paper_ref(ref.get("title")):
            lookup_values.append(ref.get("title"))
    else:
        lookup_values.append(source)
    for value in lookup_values:
        for key in _paper_reference_lookup_keys(value):
            mapped = index.get(key)
            if mapped:
                resolved = dict(mapped)
                if ref and ref.get("reason") and not resolved.get("reason"):
                    resolved["reason"] = ref.get("reason")
                return resolved
    if ref and _looks_like_internal_paper_ref(ref.get("title")):
        return None
    return ref


def _normalize_inspired_refs(value: Any, fallback: list[dict[str, Any]], paper_index: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in as_list(value):
        ref = _resolve_public_ref_from_source(item, paper_index)
        if ref:
            rows.append(ref)
    if not rows:
        for item in fallback:
            ref = _resolve_public_ref_from_source(item, paper_index)
            if ref:
                rows.append(ref)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = (str(row.get("title") or "") + "|" + str(row.get("url") or "")).lower()
        if not key.strip("|") or key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out[:8]


def _inspired_refs_text(rows: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for row in rows:
        meta = " ".join(str(row.get(key) or "").strip() for key in ["source", "year"] if str(row.get(key) or "").strip())
        parts = [str(row.get("title") or "").strip()]
        if meta:
            parts.append(meta)
        if row.get("reason"):
            parts.append(str(row.get("reason")))
        if row.get("url"):
            parts.append(str(row.get("url")))
        lines.append(" | ".join(part for part in parts if part))
    return "\n".join(lines)


def numeric(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _numeric_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip().lower()
    if not text or text in {"none", "null", "nan", "n/a", "na"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _idea_objective_scores(row: dict[str, Any]) -> dict[str, float]:
    raw = row.get("objective_scores") if isinstance(row.get("objective_scores"), dict) else {}
    scores: dict[str, float] = {}
    aliases = {
        "novelty": ["novelty", "novelty_score"],
        "evidence_alignment": ["evidence_alignment", "evidence_score", "evidence_alignment_score", "literature_alignment"],
        "feasibility": ["feasibility", "feasibility_score"],
        "experimentability": ["experimentability", "experimentability_score", "testability", "testability_score"],
        "risk_control": ["risk_control", "risk_control_score", "risk", "risk_score"],
        "overall": ["overall", "overall_score", "score", "idea_score", "judge_score"],
    }
    for canonical, keys in aliases.items():
        for key in keys:
            value = _numeric_or_none(raw.get(key) if isinstance(raw, dict) and key in raw else row.get(key))
            if value is not None:
                scores[canonical] = value
                break
    direct_score = _numeric_or_none(row.get("score") or row.get("idea_score") or row.get("judge_score"))
    if direct_score is not None and "overall" not in scores:
        scores["overall"] = direct_score
    return scores


def _idea_score_audit_ready(row: dict[str, Any]) -> bool:
    audit = row.get("idea_score_audit")
    if not isinstance(audit, dict):
        audit = row.get("objective_score_audit") if isinstance(row.get("objective_score_audit"), dict) else {}
    mode = str(audit.get("mode") or audit.get("source") or "").strip().lower()
    status = str(audit.get("status") or "").strip().lower()
    if mode in {"llm_fallback", LLM_CURRENT_FIND_FALLBACK_SOURCE} and status in {"completed", "complete", "pass", "passed"}:
        return True
    return bool(
        audit.get("subagent_used") is True
        and mode in {"task_subagent", "subagent", "claude_task_subagent"}
        and status in {"completed", "complete", "pass", "passed"}
    )


def _idea_score_contract_ready(row: dict[str, Any]) -> bool:
    scores = _idea_objective_scores(row)
    required = {"novelty", "evidence_alignment", "feasibility", "experimentability", "risk_control", "overall"}
    if not required.issubset(scores):
        return False
    if any(scores[key] <= 0 for key in required):
        return False
    if scores["overall"] < IDEA_OBJECTIVE_SCORE_MIN_OVERALL:
        return False
    if _numeric_or_none(row.get("score")) is None or _numeric_or_none(row.get("idea_score")) is None:
        return False
    return _idea_score_audit_ready(row)


def _normalized_idea_score(row: dict[str, Any], default: float = 0.0) -> float:
    scores = _idea_objective_scores(row)
    value = scores.get("overall")
    return float(value) if value is not None else default


def _idea_display_score(row: dict[str, Any]) -> Any:
    scores = _idea_objective_scores(row) if isinstance(row, dict) else {}
    if scores.get("overall") is not None:
        return scores.get("overall")
    if isinstance(row, dict):
        return row.get("score") or row.get("idea_score") or row.get("judge_score")
    return None


def _score_display(value: Any, *, missing: str = "未评分") -> str:
    number = _numeric_or_none(value)
    if number is None:
        return missing
    text = f"{number:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def _paper_key(row: dict[str, Any]) -> str:
    for key in ["id", "paper_id", "url", "pdf_url"]:
        value = str(row.get(key) or "").strip()
        if value:
            return value.lower()
    return norm_title(row.get("title"))


POSITIVE_READING_MARKERS = (
    "claim_ready",
    "claim-ready",
    "positive_anchor",
    "positive support",
    "supporting_evidence",
    "supporting evidence",
)
CRITIQUE_READING_MARKERS = (
    "critique",
    "boundary",
    "audit",
    "search_expansion",
    "search expansion",
    "negative",
    "misfit",
    "not_positive",
    "not positive",
    "foundation_borrowing",
    "foundation borrowing",
    "component_reference",
    "component reference",
    "transferable_method_reference",
    "transferable method reference",
    "contrast_or_boundary_reference",
    "contrast or boundary reference",
    "recommended_reading_boundary",
    "recommended reading boundary",
    "boundary_audit",
    "boundary audit",
    "weak_or_boundary",
    "weak or boundary",
    "reading_reference",
    "reading reference",
    "current_find_reading_reference",
    "current find reading reference",
    "recommended_reading_reference",
    "recommended reading reference",
)
NON_POSITIVE_TIERS = {"nethreshold_for_reading", "critique_or_boundary_case", "retrieval_only", "weak_or_boundary"}


ENVIRONMENT_SELECTION_STAGE = "environment_claude_code"
STALE_EXECUTION_BINDING_PATTERNS = (
    "ready_to_execute",
    "train.py --data",
    "hardcoded_dataset_path",
    "base switch gate",
    "baseswitch gate",
    "已通过参考复现和base",
    "已通过参考复现和 base",
)

PRE_ENV_BASE_BINDING_LITERAL_MARKERS = (
    "repo_path",
    "local_path",
    "active_repo",
    "current_active_repo",
    "selected_base_repo",
    "current_selected_repo",
    "training_script",
    "ready_to_execute",
    "当前选定基底",
    "当前选定基库",
    "当前基底",
    "已选定基底",
    "已选择基底",
    "环境阶段选出的当前基底",
    "环境阶段已选择当前基底",
    "已通过参考复现",
    "已通过 repo/data/env",
    "现有训练脚本",
)

PRE_ENV_BASE_BINDING_REGEXES = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(?:repo_path|local_path|selected_repo_path|active_repo_path)\s*[\"']?\s*[:=]\s*[\"']?(?:/|~|\$HOME|[A-Za-z]:)",
        r"(?:训练命令|运行命令|training_script|command)\s*[\"']?\s*[:=]",
        r"(?:当前|已选|已选择|已选定|已通过)[^\n。；;]{0,70}(?:基底|基库|仓库|repo|训练脚本)",
        r"(?:环境阶段|environment)[^\n。；;]{0,50}(?:已|已经|选出|选定|选择了)[^\n。；;]{0,70}(?:基底|仓库|repo|base)",
        r"(?:selected base|current base|active repo|existing repo)\s*[:=]",
    )
)

PRE_ENV_IDEA_BINDING_KEYS = (
    "title",
    "status",
    "recommendation",
    "new_method",
    "hypothesis",
    "method_details",
    "mechanism",
    "initial_experiment",
    "experiment_design",
    "experimental_design",
    "min_experiment",
    "minimum_experiment",
    "inspired_by",
    "inspired_by_text",
    "supporting_papers",
    "positive_anchor_papers",
    "implementation_details",
    "implementation_plan",
    "execution_selection",
    "ready_to_execute",
    "repo_path",
    "local_path",
    "selected_repo_path",
    "active_repo_path",
    "base_repo",
    "repo",
    "repo_url",
    "training_script",
    "command",
)

PRE_ENV_PLAN_BINDING_KEYS = (
    "title",
    "description",
    "hypothesis",
    "new_method",
    "method_details",
    "mechanism",
    "initial_experiment",
    "min_experiment",
    "minimum_experiment",
    "steps",
    "experiment_steps",
    "success_gate",
    "stop_condition",
    "baseline_and_ablation",
    "environment_phase",
    "implementation_details",
    "implementation_plan",
    "execution_selection",
    "versions",
    "repo_path",
    "local_path",
    "selected_repo_path",
    "active_repo_path",
    "base_repo",
    "repo",
    "repo_url",
    "training_script",
    "command",
)

IDEA_OBJECTIVE_SCORE_KEYS = (
    "novelty",
    "evidence_alignment",
    "feasibility",
    "experimentability",
    "risk_control",
    "overall",
)
IDEA_OBJECTIVE_SCORE_MIN_OVERALL = 7.0


def _identity_values(row: dict[str, Any]) -> set[str]:
    row = _reading_content_view(row)
    values: set[str] = set()
    title = norm_title(row.get("title") or row.get("paper_title"))
    if title:
        values.add(f"title:{title}")
    for key in ["id", "paper_id", "entry_id", "url", "abs_url", "pdf_url"]:
        value = str(row.get(key) or "").strip().lower()
        if value:
            values.add(f"{key}:{value}")
            if key in {"id", "paper_id", "entry_id"}:
                values.add(f"paper_key:{value}")
    return values


def _current_positive_identities(find_results: dict[str, Any]) -> tuple[set[str], list[str]]:
    identities: set[str] = set()
    titles: list[str] = []
    seen_titles: set[str] = set()
    for pool in ["strong_recommendations", "articles"]:
        for row in as_list(find_results.get(pool)):
            if not isinstance(row, dict) or not _paper_is_current_positive(row, pool):
                continue
            identities.update(_identity_values(row))
            title = norm_title(row.get("title"))
            if title and title not in seen_titles:
                seen_titles.add(title)
                titles.append(str(row.get("title") or "").strip())
    return identities, titles


def _current_recommendation_rows(find_results: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pool, role in [("strong_recommendations", "user_visible_recommendation"), ("articles", "user_visible_article")]:
        for index, raw in enumerate(as_list(find_results.get(pool)), 1):
            if not isinstance(raw, dict):
                continue
            title = first_text(raw, "title")
            key = _paper_key(raw)
            if not title or not key or key in seen:
                continue
            row = dict(raw)
            row["taste_pool"] = pool
            row["taste_pool_role"] = row.get("taste_pool_role") or role
            row["taste_pool_rank"] = index
            row["recommended_for_deep_reading"] = True
            rows.append(row)
            seen.add(key)
    return rows


def _row_has_packet_body(row: dict[str, Any], packet_index: dict[str, dict[str, Any]], packet_path: str) -> bool:
    entry = _matching_full_text_packet_entry(row, packet_index)
    return _packet_entry_has_paper_body(entry, packet_path)


READ_REPLACEMENT_VALIDATION_POOLS = (
    "screened_ranking",
    "read_candidates",
    "evaluated_candidates",
    "triage_candidates",
    "audit_candidates",
    "critique_candidates",
    "title_candidates",
)
READ_REPLACEMENT_BLOCKED_TIERS = {
    "weak_or_boundary",
    "retrieval_only",
    "nethreshold_for_reading",
    "critique_or_boundary_case",
    "audit_or_search_expansion_only",
}
READ_REPLACEMENT_BLOCKED_ROLES = {"weak_or_boundary", "negative", "critique_only"}


def _read_replacement_candidate_allowed(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("weak_candidate_for_critique") or row.get("not_positive_support") or row.get("foundation_demoted_from_strong"):
        return False
    if row.get("topic_evidence_supported") is False:
        return False
    tier = str(row.get("evidence_tier") or "").strip().lower()
    role = str(row.get("evidence_role") or "").strip().lower()
    if tier in READ_REPLACEMENT_BLOCKED_TIERS or role in READ_REPLACEMENT_BLOCKED_ROLES:
        return False
    return True


def _find_replacement_source_row(find_results: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(find_results, dict) or not isinstance(entry, dict):
        return {}
    keys = _identity_values(entry)
    if not keys:
        return {}
    for pool in READ_REPLACEMENT_VALIDATION_POOLS:
        for row in as_list(find_results.get(pool)):
            if isinstance(row, dict) and keys & _identity_values(row):
                return row
    return {}


def _reading_packet_replacement_entries(packet: dict[str, Any], packet_path: str, find_results: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not isinstance(packet, dict):
        return rows
    find_results = find_results if isinstance(find_results, dict) else {}
    for entry in as_list(packet.get("papers")):
        if not isinstance(entry, dict) or entry.get("read_replacement") is not True:
            continue
        if not _packet_entry_has_paper_body(entry, packet_path):
            continue
        source_row = _find_replacement_source_row(find_results, entry)
        if source_row:
            if not _read_replacement_candidate_allowed(source_row):
                continue
            merged = dict(source_row)
            merged.update(entry)
            rows.append(merged)
        elif _read_replacement_candidate_allowed(entry):
            rows.append(entry)
    return rows


def _replacement_entry_for_unavailable(row: dict[str, Any], replacements: list[dict[str, Any]], used_titles: set[str]) -> dict[str, Any]:
    title = norm_title(row.get("title") or row.get("paper_title"))
    for entry in replacements:
        replacement_title = norm_title(entry.get("title") or entry.get("paper_title"))
        if replacement_title and replacement_title in used_titles:
            continue
        if norm_title(entry.get("replacement_for_unavailable_recommendation")) == title:
            return entry
    return {}


def _row_from_replacement_entry(entry: dict[str, Any], original_row: dict[str, Any]) -> dict[str, Any]:
    row = dict(entry)
    row["reading_packet_role"] = "read_stage_replacement"
    row["recommended_for_deep_reading"] = True
    row["read_replacement"] = True
    row["replacement_for_unavailable_recommendation"] = first_text(entry, "replacement_for_unavailable_recommendation") or first_text(original_row, "title", "paper_title")
    row["replaced_original_recommendation"] = {
        "title": first_text(original_row, "title", "paper_title"),
        "url": first_text(original_row, "url", "abs_url"),
        "venue": first_text(original_row, "venue"),
        "year": first_text(original_row, "year"),
    }
    row.setdefault("taste_pool", first_text(entry, "replacement_source_pool") or "read_stage_full_text_replacement")
    row.setdefault("taste_pool_role", "read_stage_full_text_replacement")
    row.setdefault("taste_pool_rank", entry.get("replacement_source_rank") or entry.get("taste_pool_rank"))
    return row


def _reading_packet_replacement_rows(reading_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in reading_rows if isinstance(row, dict) and row.get("reading_packet_role") == "read_stage_replacement"]


def _reading_packet_unavailable_rows(reading_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in reading_rows if isinstance(row, dict) and row.get("reading_packet_role") == "unavailable_original_recommendation"]


def _current_reading_packet_rows(find_results: dict[str, Any], packet: dict[str, Any] | None = None, limit: int = 0) -> list[dict[str, Any]]:
    recommendations = _current_recommendation_rows(find_results if isinstance(find_results, dict) else {})
    target = limit if limit and limit > 0 else len(recommendations)
    if target <= 0:
        target = len(recommendations)
    if not recommendations:
        return []
    packet = packet if isinstance(packet, dict) else {}
    packet_path = str(packet.get("path") or "")
    packet_index = _full_text_packet_index(packet)
    replacements = _reading_packet_replacement_entries(packet, packet_path, find_results if isinstance(find_results, dict) else {})
    used_replacement_titles: set[str] = set()
    rows: list[dict[str, Any]] = []
    for row in recommendations[:target]:
        clean = dict(row)
        if packet_index and _row_has_packet_body(row, packet_index, packet_path):
            clean["reading_packet_role"] = "original_recommendation_with_full_text"
            rows.append(clean)
            continue
        replacement_entry = _replacement_entry_for_unavailable(row, replacements, used_replacement_titles)
        if replacement_entry:
            replacement_row = _row_from_replacement_entry(replacement_entry, row)
            replacement_title = norm_title(replacement_row.get("title") or replacement_row.get("paper_title"))
            if replacement_title:
                used_replacement_titles.add(replacement_title)
            rows.append(replacement_row)
            continue
        clean["reading_packet_role"] = "unavailable_original_recommendation"
        rows.append(clean)
    return rows


def _reading_packet_find_results(find_results: dict[str, Any], reading_rows: list[dict[str, Any]]) -> dict[str, Any]:
    packet = dict(find_results if isinstance(find_results, dict) else {})
    packet["strong_recommendations"] = [dict(row) for row in reading_rows]
    packet["articles"] = []
    replacement_rows = _reading_packet_replacement_rows(reading_rows)
    unavailable_rows = _reading_packet_unavailable_rows(reading_rows)
    packet["current_reading_packet"] = {
        "source": "current_find_reading_packet_read_stage_full_text_selection",
        "paper_count": len(reading_rows),
        "replacement_count": len(replacement_rows),
        "replacement_titles": [first_text(row, "title", "paper_title") for row in replacement_rows],
        "replaced_unavailable_recommendation_titles": [first_text(row, "replacement_for_unavailable_recommendation") for row in replacement_rows],
        "unavailable_original_recommendation_count": len(unavailable_rows),
        "unavailable_original_recommendation_titles": [first_text(row, "title", "paper_title") for row in unavailable_rows],
        "policy": "Read validates the full-text reading packet, not the immutable user-visible Find Top-N. Replacements are same-run ranked candidates used only when an original recommendation has no verified public full text.",
    }
    return packet


def _current_reading_validation_view(find_results: dict[str, Any], full_text_packet: dict[str, Any] | None, read_limit: int = 0) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    original_recommendation_rows = _current_recommendation_rows(find_results if isinstance(find_results, dict) else {})
    target_read_count = read_limit if read_limit and read_limit > 0 else len(original_recommendation_rows)
    reading_rows = _current_reading_packet_rows(find_results if isinstance(find_results, dict) else {}, full_text_packet if isinstance(full_text_packet, dict) else {}, target_read_count)
    return original_recommendation_rows, reading_rows, _reading_packet_find_results(find_results if isinstance(find_results, dict) else {}, reading_rows)


def _current_reading_validation_metadata(original_recommendation_rows: list[dict[str, Any]], reading_rows: list[dict[str, Any]], full_text_packet: dict[str, Any] | None, read_limit: int) -> dict[str, Any]:
    replacement_rows = _reading_packet_replacement_rows(reading_rows)
    unavailable_rows = _reading_packet_unavailable_rows(reading_rows)
    return {
        "normalization_source": "current_reading_packet_read_stage_full_text_selection_guard",
        "original_recommendation_count": len(original_recommendation_rows),
        "reading_replacement_count": len(replacement_rows),
        "reading_replacement_titles": [first_text(row, "title", "paper_title") for row in replacement_rows],
        "replaced_unavailable_recommendation_titles": [first_text(row, "replacement_for_unavailable_recommendation") for row in replacement_rows],
        "unavailable_original_recommendation_count": len(unavailable_rows),
        "unavailable_original_recommendation_titles": [first_text(row, "title", "paper_title") for row in unavailable_rows],
        "full_text_packet": full_text_packet_summary(full_text_packet if isinstance(full_text_packet, dict) else {}),
        "read_limit_input": read_limit,
        "enforced_current_recommendation_count": len(reading_rows),
        "policy": "Find Top-N remains immutable; Read coverage is measured on the full-text reading packet after same-run replacement selection.",
    }


def _current_recommendation_identities(find_results: dict[str, Any]) -> tuple[set[str], list[str]]:
    identities: set[str] = set()
    titles: list[str] = []
    for row in _current_recommendation_rows(find_results):
        identities.update(_identity_values(row))
        title = str(row.get("title") or "").strip()
        if title:
            titles.append(title)
    return identities, titles


def _enforce_current_find_claim_policy(readings: list[dict[str, Any]], positive_ids: set[str]) -> list[dict[str, Any]]:
    """Keep current-Find deep readings separate from paper-claim evidence.

    Find recommendations are reading and ideation inputs.  A reading row may only
    carry claim-ready flags when its paper is in the explicit strict-positive
    whitelist; otherwise The workflow must preserve the reading while demoting claim flags
    to reference/boundary roles.
    """
    normalized: list[dict[str, Any]] = []
    for row in readings:
        if not isinstance(row, dict):
            continue
        clean = dict(row)
        is_positive = bool(_reading_identity_values(clean) & positive_ids)
        if not is_positive:
            for key in [
                "claim_ready_anchor",
                "positive_claim_evidence",
                "positive_anchor_for_planning",
                "claim_ready",
                "supporting_evidence",
            ]:
                clean[key] = False
            clean["not_positive_support"] = True
            clean.setdefault("support_policy", "current_find_reading_reference_not_claim_evidence")
            role_text = " ".join(str(clean.get(key) or "") for key in ["verdict", "support_role", "evidence_role", "role"]).lower()
            declares_positive = any(marker in role_text for marker in POSITIVE_READING_MARKERS)
            if declares_positive or not _reading_declares_critique(clean):
                clean["verdict"] = "recommended_reading_reference"
                clean["support_role"] = "reading_reference"
                clean["evidence_role"] = "current_find_reading_reference"
                if "role" in clean and any(marker in str(clean.get("role") or "").lower() for marker in POSITIVE_READING_MARKERS):
                    clean["role"] = "current_find_reading_reference"
            clean.setdefault("claim_evidence_policy", "Only local repo/data/env/experiment artifacts can support paper claims; current Find readings can inspire ideas and plans only.")
        normalized.append(clean)
    return normalized


def _current_known_identities(find_results: dict[str, Any]) -> set[str]:
    identities: set[str] = set()
    for pool in ["strong_recommendations", "articles", "read_candidates", "triage_candidates", "audit_candidates", "evaluated_candidates", "critique_candidates", "title_candidates", "retrieval_candidates", "arxiv_prefiltered"]:
        for row in as_list(find_results.get(pool)):
            if isinstance(row, dict):
                identities.update(_identity_values(row))
    return identities


def _reading_identity_values(row: dict[str, Any]) -> set[str]:
    row = _reading_content_view(row)
    values = _identity_values(row)
    if row.get("paper_title") and not row.get("title"):
        title = norm_title(row.get("paper_title"))
        if title:
            values.add(f"title:{title}")
    return values


def _reading_declares_positive(row: dict[str, Any]) -> bool:
    row = _reading_content_view(row)
    text = " ".join(str(row.get(key) or "") for key in ["verdict", "support_role", "evidence_role", "role", "recommendation", "status"]).lower()
    return any(marker in text for marker in POSITIVE_READING_MARKERS)


def _reading_declares_critique(row: dict[str, Any]) -> bool:
    row = _reading_content_view(row)
    text = " ".join(str(row.get(key) or "") for key in ["verdict", "support_role", "evidence_role", "role", "critique_reason", "limitations"]).lower()
    return any(marker in text for marker in CRITIQUE_READING_MARKERS)


def _find_row_declares_borrowed_or_boundary(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("not_positive_support") or row.get("weak_candidate_for_critique") or row.get("foundation_demoted_from_strong"):
        return True
    text = " ".join(
        str(row.get(key) or "")
        for key in [
            "evidence_role",
            "evidence_tier",
            "support_policy",
            "topic_evidence",
            "recommendation_note",
            "recommendation_note_zh",
            "reason",
            "reason_zh",
            "fit_explanation",
            "fit_explanation_zh",
        ]
    ).lower()
    tier_markers = (
        "critique_or_boundary_case",
        "nethreshold_for_reading",
        "retrieval_only",
        "weak_or_boundary",
        "audit_or_search_expansion_only",
    )
    return any(marker in text for marker in CRITIQUE_READING_MARKERS) or any(marker in text for marker in tier_markers)


FULL_TEXT_READ_POLICY_VERSION = "full_text_required_v5_detailed_deep_read"
FULL_TEXT_SUBAGENT_POLICY_VERSION = "main_claude_must_delegate_each_recommended_reading_v1"
FULL_TEXT_MIN_CHARS = 1200
FULL_TEXT_PACKET_MIN_PAPER_BODY_CHARS = 8000
FULL_TEXT_PACKET_RELATIVE_PATH = Path("planning/finding/full_text_reading/full_text_packet.json")
FULL_TEXT_PENDING_MARKERS = (
    "pending", "metadata_only", "abstract_only", "abstract only", "unavailable", "failed", "blocked",
    "not_read", "not read", "needs_full_text", "no_pdf", "no pdf", "missing_pdf", "missing pdf",
    "bibliographic", "citation_only", "title_only", "dblp_abstract_only", "待补", "不可访问",
)
FULL_TEXT_READ_MARKERS = (
    "full_text_read", "full text read", "pdf_text_read", "pdf text read",
    "html_text_read", "paper_text_read", "text_extracted", "full_text_available",
    "全文已读", "正文已读",
)
FULL_TEXT_CONTENT_PLACEHOLDERS = (
    "中文摘要待补",
    "论文动机待补",
    "动机待补",
    "详细方法待补",
    "方法细节待补",
    "详细方法待中文精读补齐",
    "实验设置与结果待补",
    "实验设置待补",
    "实验设置与结果待中文精读补齐",
    "局限性待补",
    "当前记录未提供中文摘要",
    "当前自动 fallback",
    "全文未读取",
    "待补全文精读",
    "方法差异和优缺点待正文精读后确认",
    "不能仅凭题录或摘要确认",
    "全文文本证据已抓取；但项目代理还没有",
    "全文文本证据已抓取，但",
    "待正文精读",
    "当前可访问正文证据不足",
    "当前可访问证据不足",
    "该字段未提供合格精读内容",
    "需要进一步确认",
    "需进一步确认",
)
DEEP_READ_FIELD_MIN_CHARS = {
    "abstract_zh": 260,
    "motivation_zh": 180,
    "method_details_zh": 650,
    "experiments_zh": 420,
    "limitations_zh": 220,
}
DEEP_READ_LIST_MIN_ITEMS = 2
DEEP_READ_LIST_ITEM_MIN_CHARS = 55
DEEP_READ_LIST_ITEM_MIN_SPECIFIC_CHARS = 24
DEEP_READ_LIST_TOTAL_MIN_CHARS = 60
RECOMMENDATION_RATIONALE_COPY_SIMILARITY = 0.82


def _positive_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _full_text_evidence_dicts(row: dict[str, Any]) -> list[dict[str, Any]]:
    evidences: list[dict[str, Any]] = []
    for key in ["source_evidence", "full_text_evidence", "pdf_evidence", "text_evidence", "deep_read_audit"]:
        value = row.get(key)
        if isinstance(value, dict):
            evidences.append(value)
    for key in ["source_evidences", "full_text_evidences", "evidence_sources"]:
        for item in as_list(row.get(key)):
            if isinstance(item, dict):
                evidences.append(item)
    return evidences


def _full_text_status_blob(value: dict[str, Any]) -> str:
    keys = [
        "full_text_status",
        "read_status",
        "claude_read_status",
        "pdf_status",
        "source_status",
        "status",
        "evidence_status",
        "source",
        "evidence_type",
        "kind",
        "note",
    ]
    return " ".join(str(value.get(key) or "") for key in keys).lower()


def _full_text_status_is_pending_or_metadata(value: dict[str, Any]) -> bool:
    blob = _full_text_status_blob(value)
    return bool(blob and any(marker in blob for marker in FULL_TEXT_PENDING_MARKERS))


def _full_text_status_is_read(value: dict[str, Any]) -> bool:
    blob = _full_text_status_blob(value)
    return bool(blob and any(marker in blob for marker in FULL_TEXT_READ_MARKERS))


def _reading_status_quality_score(row: dict[str, Any]) -> int:
    if not isinstance(row, dict):
        return -10
    score = 0
    if row.get("full_text_available") is True:
        score += 3
    if _full_text_status_is_read(row):
        score += 3
    if _full_text_status_is_deep_read_pending(row):
        score -= 4
    elif _full_text_status_is_pending_or_metadata(row):
        score -= 2
    if _reading_claims_full_text_unavailable(row):
        score -= 6
    return score


def _full_text_status_is_deep_read_pending(value: dict[str, Any]) -> bool:
    blob = _full_text_status_blob(value)
    markers = (
        "pending_deep_read_synthesis",
        "ready_pending_deep_read",
        "full_text_packet_ready_pending",
        "pending_claude_rewrite",
    )
    return bool(blob and any(marker in blob for marker in markers))


def _packet_text_path_abs(path_text: str, packet_path: str = "") -> Path:
    path = Path(str(path_text or ""))
    if path.is_absolute():
        return path
    if packet_path:
        base = Path(packet_path).parent
        candidate = base / path
        if candidate.exists():
            return candidate
        try:
            roots = [Path(packet_path).parents[index] for index in range(min(5, len(Path(packet_path).parents)))]
        except Exception:
            roots = []
        for root in roots:
            candidate = root / path
            if candidate.exists():
                return candidate
    return Path(path_text)


def _text_has_paper_body_shape(text: str, title: str = "") -> bool:
    if len(text or "") < FULL_TEXT_PACKET_MIN_PAPER_BODY_CHARS:
        return False
    lowered = str(text or "").lower()
    title_tokens = [token for token in re.findall(r"[a-zA-Z]{4,}", str(title or "").lower())[:8]]
    title_hits = sum(1 for token in title_tokens if token in lowered)
    if title_tokens and title_hits < max(1, min(3, len(title_tokens))):
        return False
    section_hits = sum(1 for marker in ["abstract", "introduction", "method", "methodology", "experiment", "experiments", "evaluation", "results", "conclusion", "references"] if marker in lowered)
    return section_hits >= 4


def _packet_entry_has_paper_body(entry: dict[str, Any], packet_path: str = "") -> bool:
    if not isinstance(entry, dict):
        return False
    chars = _positive_int(entry.get("text_chars") or entry.get("pdf_text_chars") or entry.get("full_text_chars"))
    text_path = first_text(entry, "text_path")
    pdf_url = first_text(entry, "pdf_url")
    html_url = first_text(entry, "html_url")
    if chars < FULL_TEXT_PACKET_MIN_PAPER_BODY_CHARS and html_url and "icml.cc/virtual/" in html_url.lower():
        return False
    if chars >= FULL_TEXT_PACKET_MIN_PAPER_BODY_CHARS and pdf_url:
        return True
    if not text_path or chars < FULL_TEXT_PACKET_MIN_PAPER_BODY_CHARS:
        return False
    path = _packet_text_path_abs(text_path, packet_path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:300000]
    except Exception:
        return chars >= FULL_TEXT_PACKET_MIN_PAPER_BODY_CHARS and bool(pdf_url)
    return _text_has_paper_body_shape(text, first_text(entry, "title"))


def _has_full_text_locator(value: dict[str, Any]) -> bool:
    for key in ["text_path", "full_text_text_path", "pdf_url", "html_url", "full_text_url", "pdf_path"]:
        if str(value.get(key) or "").strip():
            return True
    return False


def _full_text_evidence_chars(value: dict[str, Any]) -> int:
    if not isinstance(value, dict) or _full_text_status_is_pending_or_metadata(value):
        return 0
    if value.get("paper_body_verified") is False:
        return 0
    for key in ["pdf_text_chars", "full_text_chars", "text_chars", "source_text_chars", "body_text_chars", "chars", "character_count", "evidence_chars"]:
        chars = _positive_int(value.get(key))
        if chars >= FULL_TEXT_MIN_CHARS:
            return chars
    return 0


def _reading_has_full_text_evidence(row: dict[str, Any]) -> bool:
    row = _reading_content_view(row)
    if not isinstance(row, dict):
        return False
    status_allows_evidence = (
        not _full_text_status_is_pending_or_metadata(row)
        or _full_text_status_is_deep_read_pending(row)
        or _full_text_status_is_read(row)
        or _has_full_text_locator(row)
    )
    if status_allows_evidence:
        for key in ["pdf_text_chars", "full_text_chars", "text_chars", "body_text_chars"]:
            if _positive_int(row.get(key)) >= FULL_TEXT_MIN_CHARS:
                return True
    if _positive_int(row.get("source_text_chars")) >= FULL_TEXT_MIN_CHARS and (_full_text_status_is_read(row) or _has_full_text_locator(row) or _full_text_status_is_deep_read_pending(row)):
        return True
    for evidence in _full_text_evidence_dicts(row):
        if _full_text_evidence_chars(evidence) >= FULL_TEXT_MIN_CHARS:
            return True
    return False


def _reading_claims_full_text_unavailable(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    haystack_parts: list[str] = []
    for key in [
        "full_text_status",
        "read_status",
        "claude_read_status",
        "source_status",
        "inaccessibility_reason",
        "reading_status_note_zh",
        "full_text_note",
    ]:
        haystack_parts.append(str(row.get(key) or ""))
    for key in ["source_evidence", "full_text_evidence", "pdf_evidence", "text_evidence"]:
        value = row.get(key)
        if isinstance(value, dict):
            haystack_parts.append(_full_text_status_blob(value))
            haystack_parts.append(str(value.get("inaccessibility_reason") or value.get("note") or ""))
        else:
            haystack_parts.append(str(value or ""))
    blob = " ".join(haystack_parts).lower()
    if not blob:
        return False
    deep_read_pending_markers = (
        "pending_deep_read_synthesis",
        "ready_pending_deep_read",
        "full_text_packet_ready_pending",
        "pending_claude_rewrite",
    )
    if any(marker in blob for marker in deep_read_pending_markers):
        return False
    conflict_markers = [
        "full_text_inaccessible",
        "inaccessible_all_channels",
        "all channels blocked",
        "network policy",
        "paywall",
        "metadata_and_abstract_only",
        "metadata only",
        "abstract_only",
        "abstract only",
        "no_local_text",
        "no local text",
        "no_pdf_available",
        "no pdf available",
        "not_found_no_arxiv_no_pdf",
        "摘要仅",
        "全文不可访问",
        "无法访问",
    ]
    return any(marker in blob for marker in conflict_markers)


def _reading_full_text_packet_conflict(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    return bool(row.get("full_text_packet_conflict") and _positive_int(row.get("full_text_packet_text_chars")) >= FULL_TEXT_MIN_CHARS)


def _contains_cjk(text: Any) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", str(text or "")))


def _field_text(value: Any, limit: int = 5000) -> str:
    if isinstance(value, list):
        value = "\n".join(str(item or "") for item in value)
    elif isinstance(value, dict):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return _sanitize_read_text(value, limit)


def _nonspace_len(text: Any) -> int:
    return len(re.sub(r"\s+", "", str(text or "")))


def _sentence_like_count(text: Any) -> int:
    clean = str(text or "").strip()
    if not clean:
        return 0
    parts = [part for part in re.split(r"[。！？!?；;]\s*", clean) if part.strip()]
    return max(1, len(parts))


SCIENTIFIC_CHINESE_NUMERAL_RE = re.compile(
    r"(?:百分之[零一二三四五六七八九十百千万点两]+|[零一二三四五六七八九十两]+点[零一二三四五六七八九十]+|[一二三四五六七八九十两]+乘以十的[负正]?[一二三四五六七八九十两]+次方|K为[一二三四五六七八九十两]+)"
)
SCIENTIFIC_CONTEXT_RE = re.compile(
    r"(?:NDCG|HR|Hit|Recall|Precision|AUC|MRR|DCG|p值|p-value|top-K|K为|命中率|提升|下降|达到|相对|指标|数据集|基线|消融|学习率|温度|批次|参数|GPU|GB|延迟|用户数量|物品数量|交互数量)",
    re.IGNORECASE,
)
PUBLIC_PLACEHOLDER_LEAK_RE = re.compile(r"(?:@@TASTE_|TASTE_INLINE|\[\[(?:LATEX|PRESERVE|TASTE)[^\]]*\]\])")


def _scientific_notation_style_gap(field: str, value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    if PUBLIC_PLACEHOLDER_LEAK_RE.search(text):
        return "含有内部公式/占位符标记；必须重新生成，用户可见产物不能泄漏 TASTE/LATEX placeholder"
    if SCIENTIFIC_CHINESE_NUMERAL_RE.search(text) and SCIENTIFIC_CONTEXT_RE.search(text):
        return "科学数字、百分比、p 值、K 值、指标、模型规模、超参数和实验结果必须保留阿拉伯数字/原始符号，不得写成“零点/百分之/一乘以十”等中文数字"
    return ""


def _keyword_group_count(text: str, groups: list[tuple[str, ...]]) -> int:
    return sum(1 for group in groups if any(token in text for token in group))


def _similarity(a: Any, b: Any) -> float:
    left = re.sub(r"\s+", "", str(a or "").lower())
    right = re.sub(r"\s+", "", str(b or "").lower())
    if not left or not right:
        return 0.0
    if len(left) < 40 or len(right) < 40:
        return 0.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def _looks_like_recommendation_rationale_copy(row: dict[str, Any], abstract_zh: str) -> bool:
    # Find may already contain the paper's original abstract. That abstract can
    # seed abstract_zh; recommendation rationales and workflow notes cannot.
    for key in [
        "recommendation_summary",
        "recommendation_note",
        "recommendation_note_zh",
        "reason",
        "reason_zh",
        "fit_explanation",
        "fit_explanation_zh",
        "relevance",
        "critique_reason",
        "reading_status_note_zh",
    ]:
        source = _field_text(row.get(key), 1800)
        if not source:
            continue
        if _similarity(abstract_zh, source) >= RECOMMENDATION_RATIONALE_COPY_SIMILARITY:
            return True
        left = re.sub(r"\s+", "", abstract_zh)
        right = re.sub(r"\s+", "", source)
        if len(left) >= 60 and len(right) >= 60 and (left in right or right in left):
            return True
    return False


def _deep_read_public_talk_markers() -> tuple[str, ...]:
    return READ_VISIBLE_BANNED_MARKERS + (
        "当前项目配置的主题轴",
        "当前用户可见推荐文章",
        "摘要级线索",
        "后续精读",
        "进入精读",
        "本地证据才能支撑",
        "repo/data/env",
        "environment gate",
        "experiment gate",
    )


def _deep_read_field_ok(value: Any, min_chars: int) -> bool:
    text = _field_text(value, max(min_chars * 8, 2600))
    if _nonspace_len(text) < min_chars:
        return False
    if not _contains_cjk(text):
        return False
    if _sentence_like_count(text) < 2 and min_chars >= 180:
        return False
    if any(marker in text for marker in FULL_TEXT_CONTENT_PLACEHOLDERS):
        return False
    if any(marker in text for marker in _deep_read_public_talk_markers()):
        return False
    if _scientific_notation_style_gap("field", text):
        return False
    return True


def _deep_read_abstract_candidate(row: dict[str, Any], *, include_find_trace: bool = True) -> str:
    row = _reading_content_view(row)
    if not isinstance(row, dict):
        return ""
    keys = ["deep_read_abstract_zh", "abstract_zh", "summary"]
    if include_find_trace:
        keys.extend(["find_abstract_zh", "abstract_from_find", "abstract_original", "summary_zh", "abstract_cn", "abstract_chinese"])
    seen: set[str] = set()
    for key in keys:
        text = _field_text(row.get(key), 2600)
        if not text:
            continue
        marker = re.sub(r"\s+", "", text)
        if marker in seen:
            continue
        seen.add(marker)
        if not _deep_read_field_ok(text, DEEP_READ_FIELD_MIN_CHARS["abstract_zh"]):
            continue
        if _looks_like_recommendation_rationale_copy(row, text):
            continue
        return text
    return ""


def _deep_read_list_values(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    out: list[str] = []
    for item in values:
        text = _field_text(item, 1000)
        if text:
            out.append(text)
    return out


def _deep_read_list_item_ok(value: Any) -> bool:
    text = _field_text(value, 1000)
    if _nonspace_len(text) < DEEP_READ_LIST_ITEM_MIN_SPECIFIC_CHARS:
        return False
    if not _contains_cjk(text):
        return False
    if any(marker in text for marker in FULL_TEXT_CONTENT_PLACEHOLDERS):
        return False
    if any(marker in text for marker in _deep_read_public_talk_markers()):
        return False
    if _scientific_notation_style_gap("field", text):
        return False
    return True


def _deep_read_list_ok(value: Any) -> bool:
    items = [item for item in _deep_read_list_values(value) if _deep_read_list_item_ok(item)]
    if len(items) < DEEP_READ_LIST_MIN_ITEMS:
        return False
    total_chars = sum(_nonspace_len(item) for item in items)
    return total_chars >= DEEP_READ_LIST_TOTAL_MIN_CHARS


def _field_specific_deep_read_gaps(row: dict[str, Any]) -> list[str]:
    row = _reading_content_view(row)
    gaps: list[str] = []
    abstract = _field_text(_deep_read_abstract_candidate(row), 2200)
    motivation = _field_text(row.get("motivation_zh") or row.get("problem"), 2200)
    method = _field_text(row.get("method_details_zh") or row.get("method"), 3200)
    experiments = _field_text(row.get("experiments_zh") or row.get("experiments"), 2800)
    limitations = _field_text(row.get("limitations_zh") or row.get("limitations"), 2200)
    if abstract:
        if _looks_like_recommendation_rationale_copy(row, abstract):
            gaps.append("abstract_zh: 原论文摘要必须呈现论文原摘要的中文内容，不能用推荐理由、主题命中、流程说明或 critique_reason 冒充")
        if _sentence_like_count(abstract) < 2 or _nonspace_len(abstract) < DEEP_READ_FIELD_MIN_CHARS["abstract_zh"]:
            gaps.append("abstract_zh: 原论文摘要过短；必须用中文概括论文问题、方法和主要发现，不能只写一句推荐摘要")
    if motivation and (_sentence_like_count(motivation) < 2 or _nonspace_len(motivation) < DEEP_READ_FIELD_MIN_CHARS["motivation_zh"]):
        gaps.append("motivation_zh: 论文动机过短；必须说明论文要解决的具体矛盾、已有方法不足和任务背景")
    if method:
        method_groups = [
            ("模型", "框架", "架构", "模块", "网络", "专家", "门控"),
            ("训练", "优化", "损失", "目标", "学习", "偏好", "奖励"),
            ("推理", "采样", "反向", "去噪", "扩散", "生成", "解码"),
            ("输入", "输出", "表示", "token", "用户", "物品", "序列", "轨迹"),
        ]
        if _keyword_group_count(method, method_groups) < 3:
            gaps.append("method_details_zh: 详细方法缺少模型结构、训练目标、推理/采样流程、输入输出等关键机制信息")
        if _sentence_like_count(method) < 4:
            gaps.append("method_details_zh: 详细方法必须是多句中文 synthesis，不能只列缩写或一句公式摘要")
    if experiments:
        experiment_groups = [
            ("数据", "dataset", "数据集", "任务", "文本到图像", "图像", "视频", "推荐", "CIFAR", "MovieLens", "Amazon"),
            ("基线", "baseline", "对照", "比较", "方法", "Vanilla", "DDPM", "CBDM", "互学习", "无互学习", "control"),
            ("指标", "metric", "Recall", "NDCG", "HR", "AUC", "准确", "胜率", "偏好", "FID", "IS", "Inception", "KL", "散度"),
            ("结果", "提升", "显著", "消融", "ablation", "分析", "表", "敏感性", "超参数"),
        ]
        if _keyword_group_count(experiments, experiment_groups) < 3:
            gaps.append("experiments_zh: 实验设置与结果必须覆盖数据/任务、基线或对照、指标、主要结果或消融")
        if _sentence_like_count(experiments) < 4:
            gaps.append("experiments_zh: 实验设置与结果必须是多句中文 synthesis，不能只列任务名或一句结论")
    if limitations and (_sentence_like_count(limitations) < 3 or _nonspace_len(limitations) < DEEP_READ_FIELD_MIN_CHARS["limitations_zh"]):
        gaps.append("limitations_zh: 局限性必须结合正文说明实验边界、适用范围和迁移风险，不能只写一句限制")
    for field_name, field_value in [
        ("abstract_zh", abstract),
        ("motivation_zh", motivation),
        ("method_details_zh", method),
        ("experiments_zh", experiments),
        ("limitations_zh", limitations),
    ]:
        notation_gap = _scientific_notation_style_gap(field_name, field_value)
        if notation_gap:
            gaps.append(f"{field_name}: {notation_gap}")
    if not _deep_read_list_ok(row.get("method_advantages_zh")):
        gaps.append("method_advantages_zh: 每篇论文必须写至少两条具体方法优点，每条为中文且不能是占位话术")
    if not _deep_read_list_ok(row.get("method_disadvantages_zh")):
        gaps.append("method_disadvantages_zh: 每篇论文必须写至少两条具体方法不足/局限，每条为中文且不能是占位话术")
    return gaps


def _reading_deep_read_content_gaps(row: dict[str, Any]) -> list[str]:
    row = _reading_content_view(row)
    if not isinstance(row, dict):
        return ["reading row is not an object"]
    checks = [
        ("abstract_zh", _deep_read_abstract_candidate(row), DEEP_READ_FIELD_MIN_CHARS["abstract_zh"], "原论文摘要必须写入 abstract_zh 或 summary，且为中文；可直接使用/翻译 Find 捕获的论文原摘要，但推荐理由、主题命中和流程说明不能替代该字段"),
        ("motivation_zh", row.get("motivation_zh") or row.get("problem"), DEEP_READ_FIELD_MIN_CHARS["motivation_zh"], "论文动机必须写入 motivation_zh 或 problem，且为中文全文 synthesis；relevance 枚举值不能替代动机"),
        ("method_details_zh", row.get("method_details_zh") or row.get("method"), DEEP_READ_FIELD_MIN_CHARS["method_details_zh"], "详细方法必须写入 method_details_zh 或 method，且为中文全文 synthesis"),
        ("experiments_zh", row.get("experiments_zh") or row.get("experiments"), DEEP_READ_FIELD_MIN_CHARS["experiments_zh"], "实验设置与结果必须写入 experiments_zh 或 experiments，且为中文全文 synthesis"),
        ("limitations_zh", row.get("limitations_zh") or row.get("limitations"), DEEP_READ_FIELD_MIN_CHARS["limitations_zh"], "局限性必须写入 limitations_zh 或 limitations，且为中文全文 synthesis"),
    ]
    gaps: list[str] = []
    for field, value, min_chars, message in checks:
        if not _deep_read_field_ok(value, min_chars):
            gaps.append(f"{field}: {message}")
    gaps.extend(_field_specific_deep_read_gaps(row))
    return gaps


def _reading_subagent_audit_ok(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    audit = row.get("deep_read_audit") if isinstance(row.get("deep_read_audit"), dict) else {}
    delegated = bool(
        row.get("subagent_deep_read") is True
        or row.get("subagent_used") is True
        or row.get("deep_read_subagent_used") is True
        or audit.get("subagent_used") is True
    )
    mode = str(row.get("deep_read_mode") or audit.get("mode") or "").strip().lower()
    if mode in {"task_subagent", "subagent", "delegated_full_text_reading", "claude_task"}:
        delegated = True
    source = str(row.get("deep_read_source") or audit.get("source") or "").strip().lower()
    if "subagent" in source or "task" in source:
        delegated = True
    status = str(row.get("subagent_status") or audit.get("status") or "").strip().lower()
    if (mode in {"llm_fallback", LLM_CURRENT_FIND_FALLBACK_SOURCE} or source == LLM_CURRENT_FIND_FALLBACK_SOURCE) and status in {"completed", "complete", "pass", "passed"}:
        return True
    if delegated and status not in {"blocked", "failed", "unavailable", "missing"}:
        return True
    return False


def _session_log_has_subagent_usage(paths: Any, run_id: str) -> bool:
    candidates = [
        getattr(paths, "reports", Path("")) / "claude_project_session.md",
        getattr(paths, "state", Path("")) / "claude_project_session_last_result.json",
        getattr(paths, "state", Path("")) / "current_find_claude_takeover_result.json",
    ]
    haystack_parts: list[str] = []
    for path in candidates:
        try:
            path = Path(path)
            if path.exists():
                haystack_parts.append(path.read_text(encoding="utf-8", errors="replace")[-300000:])
        except Exception:
            continue
    takeover = load_json(getattr(paths, "state", Path("")) / "current_find_claude_takeover_result.json", {})
    subagent_dirs = _current_find_subagent_log_dirs(paths, takeover if isinstance(takeover, dict) else {})
    if any(Path(directory).glob("*.jsonl") for directory in subagent_dirs):
        haystack_parts.append(f"{run_id} subagent task logs present")
    haystack = "\n".join(haystack_parts)
    if run_id and run_id not in haystack:
        return False
    patterns = (
        "Claude 调用工具: Task",
        "调用工具: Task",
        "\"name\":\"Task\"",
        "\"name\": \"Task\"",
        "Agent input=",
        "subagent",
        "子任务",
        "逐篇精读任务",
    )
    return any(pattern in haystack for pattern in patterns)


def _fragment_delivery_audit_ok(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict) or not _reading_subagent_audit_ok(row):
        return False
    audit = row.get("deep_read_audit") if isinstance(row.get("deep_read_audit"), dict) else {}
    source = str(row.get("deep_read_source") or audit.get("source") or "").strip()
    fragment_path = str(audit.get("fragment_path") or row.get("fragment_path") or "").strip()
    return source in CURRENT_FIND_DEEP_READ_FRAGMENT_SOURCES and bool(fragment_path)


def _subagent_reading_audit_report(readings: list[dict[str, Any]], paths: Any, run_id: str) -> dict[str, Any]:
    valid_rows = [row for row in readings if isinstance(row, dict)]
    per_row_ok = [
        str(row.get("title") or row.get("paper_title") or "Untitled")
        for row in valid_rows
        if _reading_subagent_audit_ok(row)
    ]
    fragment_ok = [
        str(row.get("title") or row.get("paper_title") or "Untitled")
        for row in valid_rows
        if _fragment_delivery_audit_ok(row)
    ]
    log_ok = _session_log_has_subagent_usage(paths, run_id)
    expected = len(valid_rows)
    fragment_delivery_covers_readings = bool(expected and len(fragment_ok) >= expected)
    llm_fallback_covers_readings = bool(expected and len([row for row in valid_rows if str((row.get("deep_read_audit") if isinstance(row.get("deep_read_audit"), dict) else {}).get("source") or row.get("deep_read_source") or "").strip() == LLM_CURRENT_FIND_FALLBACK_SOURCE]) >= expected)
    return {
        "policy_version": FULL_TEXT_SUBAGENT_POLICY_VERSION,
        "expected_reading_count": expected,
        "row_level_subagent_audit_count": len(per_row_ok),
        "row_level_subagent_audit_titles": per_row_ok[:20],
        "fragment_delivery_audit_count": len(fragment_ok),
        "fragment_delivery_audit_titles": fragment_ok[:20],
        "fragment_delivery_covers_readings": fragment_delivery_covers_readings,
        "llm_fallback_covers_readings": llm_fallback_covers_readings,
        "session_log_has_task_or_subagent": log_ok,
        "valid": bool(expected and len(per_row_ok) >= expected and (log_ok or fragment_delivery_covers_readings or llm_fallback_covers_readings)),
        "policy": "Main Claude Code must delegate every recommended paper to an auditable full-text deep-reading subtask. Only when the Claude CLI is unavailable may TASTE accept explicit llm_current_find_fallback rows; that fallback is marked as LLM-authored and is not used for environment/experiment/paper execution.",
    }


def _reading_has_full_text_content(row: dict[str, Any]) -> bool:
    return not _reading_deep_read_content_gaps(row)




def _project_root_from_paths(paths: Any) -> Path:
    root = getattr(paths, "root", None)
    if root:
        return Path(root)
    planning = getattr(paths, "planning", None)
    if planning:
        return Path(planning).parent
    return Path(".")


def _full_text_packet_path(paths: Any) -> Path:
    return _project_root_from_paths(paths) / FULL_TEXT_PACKET_RELATIVE_PATH


def load_current_full_text_packet(paths: Any, run_id: str) -> dict[str, Any]:
    packet_path = _full_text_packet_path(paths)
    packet = load_json(packet_path, {})
    if not isinstance(packet, dict):
        return {}
    packet_run_id = str(packet.get("run_id") or packet.get("current_find_run_id") or "").strip()
    if packet_run_id and str(run_id or "").strip() and packet_run_id != str(run_id or "").strip():
        return {}
    papers = [dict(row, packet_path=str(packet_path)) for row in as_list(packet.get("papers")) if isinstance(row, dict)]
    if not papers:
        return {}
    return {**packet, "path": str(packet_path), "papers": papers}




def load_full_text_packet_from_taste_dir(taste_dir: Path, run_id: str) -> dict[str, Any]:
    packet_path = Path(taste_dir) / "full_text_reading" / "full_text_packet.json"
    packet = load_json(packet_path, {})
    if not isinstance(packet, dict):
        return {}
    packet_run_id = str(packet.get("run_id") or packet.get("current_find_run_id") or "").strip()
    if packet_run_id and str(run_id or "").strip() and packet_run_id != str(run_id or "").strip():
        return {}
    papers = [dict(row, packet_path=str(packet_path)) for row in as_list(packet.get("papers")) if isinstance(row, dict)]
    if not papers:
        return {}
    return {**packet, "path": str(packet_path), "papers": papers}

def current_find_full_text_packet_evidence_report(taste_dir: Path, run_id: str, find_results: dict[str, Any]) -> dict[str, Any]:
    packet = load_full_text_packet_from_taste_dir(taste_dir, run_id)
    recommendation_rows = _current_recommendation_rows(find_results if isinstance(find_results, dict) else {})
    target_count = len(recommendation_rows)
    reading_rows = _current_reading_packet_rows(find_results if isinstance(find_results, dict) else {}, packet, target_count)
    packet_index = _full_text_packet_index(packet)
    packet_path = str(packet.get("path") or "")
    readable_titles: list[str] = []
    missing_titles: list[str] = []
    for row in reading_rows:
        title = str(row.get("title") or row.get("paper_title") or "Untitled").strip()
        entry = _matching_full_text_packet_entry(row, packet_index)
        evidence = _packet_full_text_evidence(entry, packet_path)
        if _full_text_evidence_chars(evidence) >= FULL_TEXT_MIN_CHARS and str(evidence.get("text_path") or "").strip():
            readable_titles.append(title)
        else:
            missing_titles.append(title)
    return {
        "run_id": run_id,
        "expected_recommendation_count": len(readable_titles) + len(missing_titles),
        "original_recommendation_count": target_count,
        "reading_packet_count": len(reading_rows),
        "reading_replacement_count": len(_reading_packet_replacement_rows(reading_rows)),
        "reading_replacement_titles": [first_text(row, "title", "paper_title") for row in _reading_packet_replacement_rows(reading_rows)],
        "replaced_unavailable_recommendation_titles": [first_text(row, "replacement_for_unavailable_recommendation") for row in _reading_packet_replacement_rows(reading_rows)],
        "full_text_evidence_count": len(readable_titles),
        "pending_without_evidence_count": len(missing_titles),
        "full_text_evidence_titles": readable_titles[:20],
        "pending_without_evidence_titles": missing_titles[:20],
        "full_text_packet": full_text_packet_summary(packet),
    }


def current_find_full_text_packet_missing_titles(taste_dir: Path, run_id: str, find_results: dict[str, Any]) -> list[str]:
    report = current_find_full_text_packet_evidence_report(taste_dir, run_id, find_results)
    return [str(item).strip() for item in as_list(report.get("pending_without_evidence_titles")) if str(item).strip()]


def _current_find_evidence_preflight_validation(run_id: str, report: dict[str, Any], *, status: str, blockers: list[str]) -> dict[str, Any]:
    expected = _positive_int(report.get("expected_recommendation_count"))
    evidence_count = _positive_int(report.get("full_text_evidence_count"))
    missing_titles = [str(item).strip() for item in as_list(report.get("pending_without_evidence_titles")) if str(item).strip()]
    evidence_titles = [str(item).strip() for item in as_list(report.get("full_text_evidence_titles")) if str(item).strip()]
    missing_count = len(missing_titles)
    return {
        "valid": False,
        "status": status,
        "run_id": run_id,
        "source": CLAUDE_TAKEOVER_SOURCE,
        "policy_version": FULL_TEXT_READ_POLICY_VERSION,
        "expected_recommendation_count": expected,
        "actual_reading_count": 0,
        "full_text_evidence_count": evidence_count,
        "reading_packet_count": report.get("reading_packet_count"),
        "reading_replacement_count": report.get("reading_replacement_count", 0),
        "reading_replacement_titles": report.get("reading_replacement_titles", []),
        "replaced_unavailable_recommendation_titles": report.get("replaced_unavailable_recommendation_titles", []),
        "original_recommendation_count": report.get("original_recommendation_count"),
        "full_text_reading_count": 0,
        "pending_deep_read_synthesis_count": 0 if missing_count else expected,
        "pending_without_evidence_count": missing_count,
        "pending_full_text_reading_count": missing_count,
        "full_text_evidence_titles": evidence_titles[:20],
        "pending_deep_read_synthesis_titles": [] if missing_count else evidence_titles[:20],
        "pending_without_evidence_titles": missing_titles[:20],
        "pending_full_text_reading_titles": missing_titles[:20],
        "blockers": blockers,
        "generated_at": now_iso(),
        "preflight": "before_current_find_claude_takeover",
        "full_text_packet": report.get("full_text_packet") if isinstance(report.get("full_text_packet"), dict) else {},
    }


def ensure_current_find_full_text_evidence_before_claude(project: str, paths: Any, taste_dir: Path, run_id: str, find_results: dict[str, Any]) -> dict[str, Any]:
    report = current_find_full_text_packet_evidence_report(taste_dir, run_id, find_results)
    missing = [str(item).strip() for item in as_list(report.get("pending_without_evidence_titles")) if str(item).strip()]
    if not missing:
        validation = _current_find_evidence_preflight_validation(
            run_id,
            report,
            status="current_find_full_text_evidence_ready_pending_claude_deep_read",
            blockers=["Read-stage full-text packet evidence is ready; Claude Code must now synthesize detailed per-paper deep readings"],
        )
        save_json(paths.state / "current_find_claude_reading_validation.json", validation)
        return {"status": "current_find_full_text_evidence_ready", "run_id": run_id, "missing_count": 0, "full_text_packet": report.get("full_text_packet") or {}}
    validation = _current_find_evidence_preflight_validation(
        run_id,
        report,
        status="blocked_current_find_full_text_evidence_pending",
        blockers=["Read-stage full-text packet is missing or stale; The workflow must acquire same-run PDF/HTML evidence before Claude deep reading"],
    )
    save_json(paths.state / "current_find_claude_reading_validation.json", validation)
    try:
        from repair_current_find_full_text_evidence import repair_current_find_full_text_evidence
        repair_rc, repair_receipt = repair_current_find_full_text_evidence(project, force=True)
    except Exception as exc:
        repair_rc = 2
        repair_receipt = {"status": "full_text_evidence_repair_exception", "error": exc.__class__.__name__}
    refreshed_find_results = load_json(taste_dir / "find_results.json", find_results)
    if isinstance(refreshed_find_results, dict) and str(refreshed_find_results.get("run_id") or "").strip() == run_id:
        find_results = refreshed_find_results
    report_after = current_find_full_text_packet_evidence_report(taste_dir, run_id, find_results)
    missing_after = [str(item).strip() for item in as_list(report_after.get("pending_without_evidence_titles")) if str(item).strip()]
    if missing_after:
        validation_after = _current_find_evidence_preflight_validation(
            run_id,
            report_after,
            status="blocked_current_find_full_text_evidence_pending",
            blockers=["Read-stage full-text packet still misses at least one packet entry; The workflow must acquire same-paper publisher, repository, title/author-verified preprint evidence, or an eligible same-run replacement before Claude deep reading"],
        )
        validation_after["repair_return_code"] = repair_rc
        validation_after["full_text_repair_status"] = str((repair_receipt if isinstance(repair_receipt, dict) else {}).get("status") or "")
        save_json(paths.state / "current_find_claude_reading_validation.json", validation_after)
        blocked = {
            "status": "blocked_current_find_full_text_evidence_pending",
            "failure_type": "full_text_evidence_missing",
            "next_required_action": "acquire_current_find_full_text_evidence",
            "next_required_stage": "acquire_current_find_full_text_evidence",
            "base_selection_status": "blocked_by_current_find_full_text_evidence",
            "run_id": run_id,
            "takeover_ready": False,
            "claude_current_find_ready": False,
            "read_idea_plan_ready": False,
            "execution_ready": False,
            "current_find_reading_count": 0,
            "current_find_idea_count": 0,
            "current_find_plan_count": 0,
            "full_text_evidence_count": validation_after["full_text_evidence_count"],
            "pending_without_evidence_count": validation_after["pending_without_evidence_count"],
            "pending_without_evidence_titles": validation_after["pending_without_evidence_titles"],
            "reading_validation": validation_after,
            "full_text_repair": repair_receipt,
            "blockers": ["同一 Find run 的全文证据仍未覆盖全部推荐论文；系统不会让 Claude 用摘要或二手来源冒充全文精读。"],
            "policy": "Current-Find Read must acquire same-run full-text packet evidence before Claude Code deep reading, idea generation, or planning.",
            "generated_at": now_iso(),
        }
        save_json(paths.state / "current_find_research_plan.json", blocked)
        return blocked
    validation_after = _current_find_evidence_preflight_validation(
        run_id,
        report_after,
        status="current_find_full_text_evidence_ready_pending_claude_deep_read",
        blockers=["Read-stage full-text packet evidence is ready after repair; Claude Code must now synthesize detailed per-paper deep readings"],
    )
    validation_after["repair_return_code"] = repair_rc
    validation_after["full_text_repair_status"] = str((repair_receipt if isinstance(repair_receipt, dict) else {}).get("status") or "")
    save_json(paths.state / "current_find_claude_reading_validation.json", validation_after)
    return {"status": "current_find_full_text_evidence_ready_after_repair", "run_id": run_id, "repair_return_code": repair_rc, "full_text_repair": repair_receipt, "missing_count": 0, "full_text_packet": report_after.get("full_text_packet") or {}}


def _full_text_packet_index(packet: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    if not isinstance(packet, dict):
        return index
    for row in as_list(packet.get("papers")):
        if not isinstance(row, dict):
            continue
        for key in _identity_values(row):
            index[key] = row
        title = norm_title(row.get("title"))
        if title:
            index[f"title:{title}"] = row
    return index


def _matching_full_text_packet_entry(row: dict[str, Any], packet_index: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(row, dict) or not packet_index:
        return {}
    for key in _reading_identity_values(row):
        if key in packet_index:
            return packet_index[key]
    title = norm_title(row.get("title") or row.get("paper_title"))
    return packet_index.get(f"title:{title}", {}) if title else {}


def _packet_full_text_evidence(entry: dict[str, Any], packet_path: str = "") -> dict[str, Any]:
    if not isinstance(entry, dict) or not entry:
        return {}
    chars = _positive_int(entry.get("text_chars") or entry.get("pdf_text_chars") or entry.get("full_text_chars"))
    page_count = _positive_int(entry.get("page_count"))
    text_path = first_text(entry, "text_path")
    pdf_url = first_text(entry, "pdf_url")
    html_url = first_text(entry, "html_url")
    verified = _packet_entry_has_paper_body(entry, packet_path)
    if verified:
        status = "pdf_text_read" if pdf_url else "html_text_read"
    else:
        status = "metadata_and_abstract_only_no_pdf_url" if not pdf_url else "full_text_packet_missing_paper_body"
    return {
        "source": "planning/finding/full_text_reading/full_text_packet.json",
        "pdf_status": status,
        "full_text_status": status,
        "full_text_available": bool(verified),
        "paper_body_verified": bool(verified),
        "paper_body_min_chars": FULL_TEXT_PACKET_MIN_PAPER_BODY_CHARS,
        "pdf_text_read": bool(pdf_url and verified),
        "pdf_text_chars": chars if verified else 0,
        "full_text_chars": chars if verified else 0,
        "text_chars": chars if verified else 0,
        "raw_text_chars": chars,
        "page_count": page_count,
        "text_path": text_path,
        "pdf_url": pdf_url,
        "html_url": html_url,
    }


def _packet_original_abstract_zh(entry: dict[str, Any]) -> str:
    if not isinstance(entry, dict) or not entry:
        return ""
    for key in ["abstract_from_find", "find_abstract_zh", "abstract_zh", "summary_zh", "abstract_cn", "abstract_chinese"]:
        text = _sanitize_read_text(entry.get(key), 2400)
        if (
            text
            and _contains_cjk(text)
            and _nonspace_len(text) >= DEEP_READ_FIELD_MIN_CHARS["abstract_zh"]
            and not _read_text_is_placeholder(text)
        ):
            return text
    return ""


def _set_original_abstract_fields(clean: dict[str, Any], original_abstract: str, source_key: str) -> None:
    if not original_abstract:
        return
    original_abstract = str(_ensure_read_public_sentence_value(original_abstract) or "")
    original_len = _nonspace_len(original_abstract)
    original_is_zh = _deep_read_field_ok(original_abstract, DEEP_READ_FIELD_MIN_CHARS["abstract_zh"]) and not _looks_like_recommendation_rationale_copy(clean, original_abstract)
    current = _field_text(_deep_read_abstract_candidate(clean, include_find_trace=False) or clean.get("abstract_zh") or clean.get("summary"), 2400)
    current_len = _nonspace_len(current)
    current_is_zh = bool(current) and _deep_read_field_ok(current, DEEP_READ_FIELD_MIN_CHARS["abstract_zh"]) and not _looks_like_recommendation_rationale_copy(clean, current)

    existing_from_find = _field_text(clean.get("abstract_from_find"), 2400)
    if (not existing_from_find) or original_len >= max(_nonspace_len(existing_from_find) + 80, int(max(1, _nonspace_len(existing_from_find)) * 1.15)):
        clean["abstract_from_find"] = original_abstract

    if current_is_zh and current != original_abstract and not clean.get("deep_read_abstract_zh"):
        clean["deep_read_abstract_zh"] = current

    if original_is_zh:
        use_original = (
            not current_is_zh
            or _looks_like_recommendation_rationale_copy(clean, current)
            or original_len >= max(current_len + 80, int(current_len * 1.15))
        )
        if use_original:
            clean["abstract_zh"] = original_abstract
            clean["summary"] = original_abstract
        elif current_is_zh and not clean.get("summary"):
            clean["summary"] = current
        existing_find_zh = _field_text(clean.get("find_abstract_zh"), 2400)
        if (not existing_find_zh) or not _contains_cjk(existing_find_zh) or original_len >= max(_nonspace_len(existing_find_zh) + 80, int(max(1, _nonspace_len(existing_find_zh)) * 1.15)):
            clean["find_abstract_zh"] = original_abstract
    else:
        existing_find_zh = _field_text(clean.get("find_abstract_zh"), 2400)
        if existing_find_zh and not _contains_cjk(existing_find_zh):
            clean.pop("find_abstract_zh", None)
    clean["original_abstract_source"] = source_key


def _apply_preferred_deep_read_abstract_fields(clean: dict[str, Any]) -> dict[str, Any]:
    candidate = _deep_read_abstract_candidate(clean)
    if not candidate:
        return clean
    for key in ["abstract_zh", "summary"]:
        current = _field_text(clean.get(key), 2600)
        current_ok = bool(current) and _deep_read_field_ok(current, DEEP_READ_FIELD_MIN_CHARS["abstract_zh"]) and not _looks_like_recommendation_rationale_copy(clean, current)
        if not current_ok:
            clean[key] = candidate
    return clean


def normalize_reading_full_text_evidence(row: dict[str, Any], packet_entry: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(row, dict):
        return row
    clean = dict(row)
    evidence_candidates = _full_text_evidence_dicts(clean)
    claims_unavailable = _reading_claims_full_text_unavailable(clean)
    packet_entry = packet_entry or {}
    if packet_entry:
        clean = _merge_find_metadata_into_reading(clean, packet_entry)
    packet_evidence = _packet_full_text_evidence(packet_entry, str((packet_entry or {}).get("packet_path") or ""))
    if packet_evidence:
        packet_abstract = _packet_original_abstract_zh(packet_entry or {})
        if packet_abstract:
            _set_original_abstract_fields(clean, packet_abstract, "full_text_packet")
        packet_chars = _positive_int(packet_evidence.get("text_chars") or packet_evidence.get("pdf_text_chars") or packet_evidence.get("full_text_chars"))
        packet_has_full_text = packet_chars >= FULL_TEXT_MIN_CHARS
        if packet_has_full_text and "当前 reading 仍声明全文不可访问" in str(clean.get("reading_status_note_zh") or ""):
            clean.pop("reading_status_note_zh", None)
        claims_unavailable = _reading_claims_full_text_unavailable(clean)
        if packet_has_full_text and not claims_unavailable:
            for stale_key in ["full_text_packet_conflict", "full_text_packet_text_path", "full_text_packet_pdf_url", "full_text_packet_text_chars"]:
                clean.pop(stale_key, None)
            if "当前 reading 仍声明全文不可访问" in str(clean.get("reading_status_note_zh") or ""):
                clean.pop("reading_status_note_zh", None)
        if packet_has_full_text and claims_unavailable:
            clean["full_text_packet_conflict"] = True
            clean["full_text_packet_text_path"] = packet_evidence.get("text_path") or ""
            clean["full_text_packet_pdf_url"] = packet_evidence.get("pdf_url") or ""
            clean["full_text_packet_text_chars"] = packet_chars
            clean["full_text_status"] = "full_text_packet_ready_pending_claude_rewrite_conflict"
            clean["full_text_available"] = False
            clean["pdf_text_read"] = False
            clean["reading_status_note_zh"] = "TASTE 已取得全文文本证据，但当前 reading 仍声明全文不可访问；必须由项目代理打开 text_path 正文后重写精读内容。"
        evidence_candidates.append(packet_evidence)
        if clean.get("full_text_packet_conflict"):
            clean["full_text_evidence"] = packet_evidence
        else:
            clean.setdefault("full_text_evidence", packet_evidence)
    best: dict[str, Any] = {}
    best_chars = 0
    for evidence in evidence_candidates:
        chars = _full_text_evidence_chars(evidence)
        if chars >= best_chars:
            best = evidence
            best_chars = chars
    if best:
        clean.setdefault("source_evidence", best)
        if best_chars:
            clean["pdf_text_chars"] = _positive_int(clean.get("pdf_text_chars")) or best_chars
            clean["full_text_chars"] = _positive_int(clean.get("full_text_chars")) or best_chars
            clean["source_text_chars"] = _positive_int(clean.get("source_text_chars")) or best_chars
        if best.get("text_path") and not clean.get("full_text_text_path"):
            clean["full_text_text_path"] = best.get("text_path")
        has_content = _reading_has_full_text_content(clean)
        best_status = str(best.get("full_text_status") or best.get("pdf_status") or "").strip()
        if clean.get("full_text_packet_conflict"):
            clean["full_text_available"] = False
            clean["pdf_text_read"] = False
        elif has_content and (best.get("full_text_available") is True or best_chars >= FULL_TEXT_MIN_CHARS):
            if best_status:
                clean["full_text_status"] = best_status
            clean["full_text_available"] = True
            clean["pdf_text_read"] = bool(clean.get("pdf_text_read") or str(clean.get("full_text_status") or "").lower() == "pdf_text_read")
        elif best_chars >= FULL_TEXT_MIN_CHARS:
            best_read = _full_text_status_is_read(best) or _full_text_status_is_read({"full_text_status": best_status})
            clean_read = _full_text_status_is_read(clean)
            if (best_read or clean_read) and not claims_unavailable:
                if best_read and (not clean_read or _full_text_status_is_deep_read_pending(clean)):
                    clean["full_text_status"] = best_status or "full_text_read"
                elif not clean_read:
                    clean["full_text_status"] = "full_text_read"
                clean["full_text_available"] = True
                clean["pdf_text_read"] = bool(clean.get("pdf_text_read") or "pdf_text_read" in str(clean.get("full_text_status") or best_status or "").lower())
                note = str(clean.get("reading_status_note_zh") or "")
                if "全文文本证据已抓取" in note or "当前 reading 仍声明全文不可访问" in note:
                    clean.pop("reading_status_note_zh", None)
            else:
                clean["full_text_available"] = False
                clean["pdf_text_read"] = False
                status = str(clean.get("full_text_status") or "").strip().lower()
                if not status or any(marker in status for marker in FULL_TEXT_PENDING_MARKERS) or status in {"pdf_text_read", "html_text_read", "full_text_read"}:
                    clean["full_text_status"] = "full_text_packet_ready_pending_deep_read_synthesis"
                clean["reading_status_note_zh"] = "全文文本证据已抓取，但精读内容仍需项目代理基于正文重写摘要、动机、方法、实验和局限。"
                if not _deep_read_field_ok(clean.get("method_details_zh") or clean.get("method"), 80):
                    clean["method"] = _pending_deep_read_method_text()
                    clean["method_details_zh"] = _pending_deep_read_method_text()
                if not _deep_read_field_ok(clean.get("experiments_zh") or clean.get("experiments"), 60):
                    clean["experiments"] = _pending_deep_read_experiment_text()
                    clean["experiments_zh"] = _pending_deep_read_experiment_text()
                if not _deep_read_field_ok(clean.get("limitations_zh") or clean.get("limitations"), 30):
                    clean["limitations"] = _pending_deep_read_limit_text()
                    clean["limitations_zh"] = _pending_deep_read_limit_text()
                disadvantages = clean.get("method_disadvantages_zh") if isinstance(clean.get("method_disadvantages_zh"), list) else []
                if not disadvantages or any("全文未读取" in str(item) or "待正文精读" in str(item) for item in disadvantages):
                    clean["method_disadvantages_zh"] = ["全文文本证据已抓取，但方法差异、优缺点和实验边界仍待项目代理基于正文完成中文精读后确认。"]
    return _sanitize_reading_public_fields(_apply_preferred_deep_read_abstract_fields(clean))


def normalize_readings_full_text_evidence(readings: list[dict[str, Any]], packet: dict[str, Any] | None) -> list[dict[str, Any]]:
    packet_index = _full_text_packet_index(packet)
    out: list[dict[str, Any]] = []
    for row in readings:
        if not isinstance(row, dict):
            continue
        out.append(_sanitize_reading_public_fields(normalize_reading_full_text_evidence(row, _matching_full_text_packet_entry(row, packet_index))))
    return out


def full_text_packet_summary(packet: dict[str, Any] | None) -> dict[str, Any]:
    rows = [row for row in as_list((packet or {}).get("papers")) if isinstance(row, dict)]
    packet_path = str((packet or {}).get("path") or "")
    readable = [row for row in rows if _packet_entry_has_paper_body(row, packet_path)]
    short_or_unverified = [str(row.get("title") or row.get("paper_title") or "") for row in rows if row not in readable]
    return {
        "available": bool(rows),
        "path": packet_path,
        "run_id": str((packet or {}).get("run_id") or ""),
        "paper_count": len(rows),
        "full_text_evidence_count": len(readable),
        "missing_full_text_count": max(0, len(rows) - len(readable)),
        "paper_body_min_chars": FULL_TEXT_PACKET_MIN_PAPER_BODY_CHARS,
        "unverified_or_short_titles": short_or_unverified[:20],
    }

def validate_claude_readings_against_current_find(readings: list[dict[str, Any]], find_results: dict[str, Any], read_limit: int, paths: Any = None, run_id: str = "") -> tuple[bool, dict[str, Any]]:
    positive_ids, positive_titles = _current_positive_identities(find_results)
    readings = _enforce_current_find_claim_policy([_reading_content_view(row) for row in readings if isinstance(row, dict)], positive_ids)
    recommendation_ids, recommendation_titles = _current_recommendation_identities(find_results)
    recommendation_index: dict[str, dict[str, Any]] = {}
    recommendation_by_title: dict[str, dict[str, Any]] = {}
    for recommendation_row in _current_recommendation_rows(find_results):
        title_key = norm_title(recommendation_row.get("title") or recommendation_row.get("paper_title"))
        if title_key:
            recommendation_by_title.setdefault(title_key, recommendation_row)
        for identity in _identity_values(recommendation_row):
            recommendation_index[identity] = recommendation_row
    validation_packet = load_current_full_text_packet(paths, run_id) if paths is not None and run_id else {}
    packet_index = _full_text_packet_index(validation_packet if isinstance(validation_packet, dict) else {})
    packet_path = str((validation_packet if isinstance(validation_packet, dict) else {}).get("path") or "")
    known_ids = _current_known_identities(find_results) | recommendation_ids
    expected_titles = recommendation_titles or positive_titles
    positive_readings: list[str] = []
    critique_readings: list[str] = []
    full_text_readings: list[str] = []
    full_text_evidence_titles: list[str] = []
    pending_full_text_readings: list[str] = []
    pending_deep_read_synthesis: list[str] = []
    pending_without_evidence: list[str] = []
    full_text_packet_conflicts: list[str] = []
    deep_read_content_gap_details: list[dict[str, Any]] = []
    invalid_positive: list[str] = []
    unknown_readings: list[str] = []
    unlabeled_non_positive: list[str] = []
    present_positive_titles: set[str] = set()
    present_recommendation_titles: set[str] = set()
    extra_readings: list[str] = []
    reading_title_counts: dict[str, int] = {}
    for raw_row in readings:
        row = _reading_content_view(raw_row)
        title = str(row.get("title") or row.get("paper_title") or "Untitled").strip()
        ids = _reading_identity_values(row)
        is_recommended = bool(ids & recommendation_ids)
        is_positive = bool(ids & positive_ids)
        is_known = bool(ids & known_ids)
        find_row = next((recommendation_index[key] for key in ids if key in recommendation_index), {})
        if find_row:
            row = _merge_find_abstract_into_reading(row, find_row)
        declares_positive = _reading_declares_positive(row)
        declares_critique = _reading_declares_critique(row) or (not is_positive and _find_row_declares_borrowed_or_boundary(find_row))
        norm = norm_title(title)
        if norm:
            reading_title_counts[norm] = reading_title_counts.get(norm, 0) + 1
        if is_recommended and norm:
            present_recommendation_titles.add(norm)
            packet_conflict = _reading_full_text_packet_conflict(row)
            has_evidence = _reading_has_full_text_evidence(row) or packet_conflict
            content_gaps = _reading_deep_read_content_gaps(row)
            if packet_conflict:
                full_text_packet_conflicts.append(title)
                content_gaps = [
                    "full_text_packet_conflict: full_text_packet has readable text_path/pdf evidence, but read_results/read.md still claims full text is inaccessible or metadata-only; Claude must open the text_path and rewrite the Chinese deep read."
                ] + content_gaps
            has_content = not content_gaps
            if has_evidence:
                full_text_evidence_titles.append(title)
            if has_evidence and has_content:
                full_text_readings.append(title)
            elif has_evidence:
                pending_deep_read_synthesis.append(title)
                if len(deep_read_content_gap_details) < 20:
                    deep_read_content_gap_details.append({"title": title, "missing_or_invalid_fields": content_gaps})
            else:
                pending_without_evidence.append(title)
                pending_full_text_readings.append(title)
        if not is_recommended:
            extra_readings.append(title)
        if is_positive:
            positive_readings.append(title)
            if norm:
                present_positive_titles.add(norm)
            continue
        if declares_positive:
            invalid_positive.append(title)
        elif declares_critique:
            critique_readings.append(title)
        else:
            unlabeled_non_positive.append(title)
        if not is_known:
            unknown_readings.append(title)
    missing_recommendations = [title for title in expected_titles if norm_title(title) not in present_recommendation_titles]
    for title in missing_recommendations:
        if not title or title in pending_full_text_readings:
            continue
        find_row = recommendation_by_title.get(norm_title(title), {})
        packet_entry = _matching_full_text_packet_entry(find_row or {"title": title}, packet_index)
        if packet_entry and _packet_entry_has_paper_body(packet_entry, packet_path):
            if title not in full_text_evidence_titles:
                full_text_evidence_titles.append(title)
            if title not in pending_deep_read_synthesis:
                pending_deep_read_synthesis.append(title)
            if len(deep_read_content_gap_details) < 20:
                deep_read_content_gap_details.append({
                    "title": title,
                    "missing_or_invalid_fields": [
                        "missing_reading: full_text_packet has readable text_path/pdf evidence, but no current-Find reading row was synthesized; Claude must open text_path and write the complete Chinese deep-read fragment."
                    ],
                })
        else:
            pending_without_evidence.append(title)
            pending_full_text_readings.append(title)
    missing_positive = [title for title in positive_titles if norm_title(title) not in present_positive_titles]
    duplicate_readings = [title for title in expected_titles if reading_title_counts.get(norm_title(title), 0) > 1]
    expected_count = len(expected_titles)
    actual_count = len(readings)
    valid = True
    blockers: list[str] = []
    if expected_count and actual_count != expected_count:
        valid = False
        blockers.append("Claude readings count must equal the current Read-stage packet entry count")
    if extra_readings:
        valid = False
        blockers.append("readings include papers outside the current Read-stage packet or allowed audit pools")
    if duplicate_readings:
        valid = False
        blockers.append("readings contain duplicate Read-stage packet entries")
    if invalid_positive:
        valid = False
        blockers.append("non-strong readings were labelled as claim-ready/positive anchors")
    if unlabeled_non_positive:
        valid = False
        blockers.append("non-strong readings lack critique/boundary/audit role")
    if unknown_readings:
        valid = False
        blockers.append("readings include papers absent from the current Find pools")
    if missing_recommendations:
        valid = False
        blockers.append("not all current Read-stage packet entries were read")
    if missing_positive:
        valid = False
        blockers.append("not all current strict positive anchors were read")
    if pending_full_text_readings or pending_deep_read_synthesis:
        valid = False
        if pending_deep_read_synthesis:
            if not pending_without_evidence:
                blockers.append("Read-stage full-text packet evidence is ready; Claude Code must now synthesize detailed per-paper deep readings")
            blockers.append("recommended readings have full-text packets but still need Claude Code deep-read synthesis in read_results/read.md")
            if full_text_packet_conflicts:
                blockers.append("recommended readings contradict full_text_packet evidence: read_results/read.md still claims full text is inaccessible although The workflow has a readable text_path/pdf")
            if deep_read_content_gap_details:
                blockers.append("recommended readings lack required Chinese deep-read JSON fields: abstract_zh, motivation_zh, method_details_zh/method, experiments_zh/experiments, limitations_zh/limitations")
        if pending_without_evidence:
            blockers.append("recommended readings still lack full-text evidence; Claude Code must read the full paper/PDF/page before marking deep reading complete")
    if len(readings) < min(read_limit, max(len(expected_titles), 1)):
        valid = False
        blockers.append("Claude readings are below the required current-Find recommendation coverage")
    subagent_audit = _subagent_reading_audit_report(readings, paths, run_id) if paths is not None else {}
    if subagent_audit and subagent_audit.get("valid") is not True:
        valid = False
        blockers.append("recommended readings lack auditable per-paper Claude Task/subagent full-text deep-reading delegation")
    report = {
        "valid": valid,
        "expected_recommendation_count": expected_count,
        "policy_version": FULL_TEXT_READ_POLICY_VERSION,
        "subagent_policy_version": FULL_TEXT_SUBAGENT_POLICY_VERSION,
        "actual_reading_count": actual_count,
        "recommended_reading_count": len([row for row in readings if _reading_identity_values(row) & recommendation_ids]),
        "full_text_reading_count": len(full_text_readings),
        "full_text_evidence_count": len(full_text_evidence_titles),
        "pending_deep_read_synthesis_count": len(pending_deep_read_synthesis),
        "pending_full_text_reading_count": len(pending_full_text_readings),
        "pending_without_evidence_count": len(pending_without_evidence),
        "full_text_reading_titles": full_text_readings[:12],
        "full_text_evidence_titles": full_text_evidence_titles[:12],
        "pending_deep_read_synthesis_titles": pending_deep_read_synthesis[:12],
        "full_text_packet_conflict_titles": full_text_packet_conflicts[:12],
        "deep_read_content_gap_details": deep_read_content_gap_details[:20],
        "pending_without_evidence_titles": pending_without_evidence[:12],
        "pending_full_text_reading_titles": pending_full_text_readings[:12],
        "positive_anchor_count": len(positive_readings),
        "critique_or_boundary_count": len(critique_readings),
        "unlabeled_non_positive_count": len(unlabeled_non_positive),
        "invalid_positive_count": len(invalid_positive),
        "unknown_reading_count": len(unknown_readings),
        "extra_reading_count": len(extra_readings),
        "duplicate_reading_count": len(duplicate_readings),
        "subagent_deep_read_audit": subagent_audit,
        "positive_anchor_titles": positive_readings,
        "critique_or_boundary_titles": critique_readings,
        "invalid_positive_titles": invalid_positive,
        "unlabeled_non_positive_titles": unlabeled_non_positive,
        "unknown_reading_titles": unknown_readings,
        "extra_reading_titles": extra_readings,
        "duplicate_reading_titles": duplicate_readings,
        "missing_recommendation_titles": missing_recommendations,
        "missing_positive_titles": missing_positive,
        "expected_recommendation_titles": expected_titles,
        "expected_positive_titles": positive_titles,
        "blockers": blockers,
        "policy": "Every Read-stage packet paper must have full-text/PDF/page evidence, auditable Claude Task/subagent delegation, and non-placeholder deep-read synthesis before it counts as completed deep reading. Find Top-N remains immutable; same-run replacements only affect Read coverage. Only strict positive anchors may be labelled positive; weak/boundary papers remain reading, critique, or search-expansion evidence.",
    }
    return valid, report

def sanitize_find_results_non_positive_flags(find_results: dict[str, Any]) -> bool:
    changed = False
    for pool in ["triage_candidates", "audit_candidates", "evaluated_candidates", "critique_candidates", "title_candidates", "retrieval_candidates", "arxiv_prefiltered"]:
        for row in as_list(find_results.get(pool)):
            if not isinstance(row, dict):
                continue
            tier = str(row.get("evidence_tier") or row.get("source_evidence_tier") or "").lower()
            should_mark = bool(row.get("weak_candidate_for_critique") or row.get("foundation_demoted_from_strong") or row.get("retrieval_pool_only") or tier in NON_POSITIVE_TIERS)
            if pool not in {"strong_recommendations", "articles"} and tier != "strong_recommendation":
                should_mark = True
            if should_mark:
                if row.get("not_positive_support") is not True:
                    row["not_positive_support"] = True
                    changed = True
                if row.get("weak_candidate_for_critique") is not True and tier != "strong_recommendation":
                    row["weak_candidate_for_critique"] = True
                    changed = True
                row.setdefault("support_policy", "audit_or_search_expansion_only_not_positive_claim_evidence")
    return changed


def _paper_is_current_positive(row: dict[str, Any], pool: str) -> bool:
    if row.get("not_positive_support") or row.get("foundation_demoted_from_strong"):
        return False
    tier = str(row.get("evidence_tier") or "").lower()
    role = str(row.get("evidence_role") or "").lower()
    if tier in {"retrieval_only", "nethreshold_for_reading", "weak_or_boundary"}:
        return False
    if role in {"weak_or_boundary", "negative", "critique_only"}:
        return False
    if pool in {"strong_recommendations", "articles"}:
        # Every strict strong recommendation must be read. Foundation-borrowing
        # anchors remain restricted at claim time, but excluding them here causes
        # current-Find coverage to silently miss strong papers.
        return not bool(row.get("weak_candidate_for_critique"))
    if role == "foundation_borrowing":
        return False
    return True


def pick_current_find_papers(find_results: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in _current_recommendation_rows(find_results):
        key = _paper_key(row)
        if not key or key in seen:
            continue
        out.append(row)
        seen.add(key)
        if len(out) >= limit:
            return out
    for pool, role in [("triage_candidates", "triage_candidate"), ("audit_candidates", "audit_candidate"), ("evaluated_candidates", "evaluated_candidate"), ("critique_candidates", "critique_candidate")]:
        for index, raw in enumerate(as_list(find_results.get(pool)), 1):
            if not isinstance(raw, dict):
                continue
            key = _paper_key(raw)
            if not key or key in seen:
                continue
            row = dict(raw)
            row["taste_pool"] = pool
            row["taste_pool_role"] = role
            row["taste_pool_rank"] = index
            out.append(row)
            seen.add(key)
            if len(out) >= limit:
                return out
    return out


def downstream_matches_current_find(
    read_results: dict[str, Any],
    ideas_results: dict[str, Any],
    plans_results: dict[str, Any],
    run_id: str,
    expected_papers: list[dict[str, Any]],
) -> bool:
    if not (
        isinstance(read_results, dict)
        and read_results.get("run_id") == run_id
        and isinstance(ideas_results, dict)
        and ideas_results.get("run_id") == run_id
        and isinstance(plans_results, dict)
        and plans_results.get("run_id") == run_id
    ):
        return False
    readings = [row for row in as_list(read_results.get("readings")) if isinstance(row, dict)]
    ideas = [row for row in as_list(ideas_results.get("ideas")) if isinstance(row, dict)]
    plans = [row for row in as_list(plans_results.get("plans")) if isinstance(row, dict)]
    if not readings or not ideas or not plans:
        return False
    expected_titles = [norm_title(row.get("title")) for row in expected_papers if norm_title(row.get("title"))]
    reading_titles = [norm_title(row.get("title")) for row in readings if norm_title(row.get("title"))]
    if reading_titles != expected_titles[: len(reading_titles)] or len(reading_titles) != len(expected_titles):
        return False
    allowed = set(expected_titles)
    candidate_pool = [row for row in as_list(ideas_results.get("candidate_pool")) if isinstance(row, dict)]
    for row in candidate_pool:
        title = norm_title(row.get("title"))
        if title and title not in allowed:
            return False
    return True

def _axis_source_blob(row: dict[str, Any]) -> str:
    # Axis classification must come from source metadata, not from LLM critique
    # text such as "lacks <topic-axis>"; otherwise negated explanations become
    # false positive topic hits.
    values: list[str] = []
    for key in [
        "title",
        "abstract",
        "abstract_zh",
        "summary",
        "keywords",
        "category",
        "hit_directions",
        "hit_directions_zh",
        "hit_directions_en",
    ]:
        value = row.get(key)
        if isinstance(value, list):
            values.extend(str(item) for item in value)
        elif value:
            values.append(str(value))
    return " ".join(values).lower()


def _has_unnegated_axis(blob: str, patterns: list[str], negative_patterns: list[str]) -> bool:
    if any(re.search(pattern, blob, flags=re.IGNORECASE) for pattern in negative_patterns):
        return False
    return any(re.search(pattern, blob, flags=re.IGNORECASE) for pattern in patterns)


def classify_paper(row: dict[str, Any], cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    blob = _axis_source_blob(row)
    fit = core_topic_fit_from_text(blob, cfg or {})
    hits = fit.get("topic_group_hits", {}) if isinstance(fit.get("topic_group_hits"), dict) else {}
    matched = [str(name) for name, ok in hits.items() if ok]
    required = [str(name) for name in fit.get("required_topic_groups", []) if str(name).strip()] if isinstance(fit.get("required_topic_groups"), list) else []
    code_blob = " ".join(str(row.get(key) or "") for key in ["code_links", "github_url", "code_url", "url", "source"]).lower()
    has_code = any(token in code_blob or token in blob for token in ["code", "github", "开源", "代码"])
    return {
        "required_topic_groups": required,
        "topic_group_hits": hits,
        "matched_topic_groups": matched,
        "matched_topic_group_count": len(matched),
        "missing_topic_groups": fit.get("missing_topic_groups", []),
        "hard_topic_mismatch": bool(fit.get("hard_topic_mismatch")),
        "has_code_signal": has_code or bool(row.get("code_links") or row.get("github_url") or row.get("code_url")),
    }


READ_VISIBLE_BANNED_MARKERS = (
    "project_topic",
    "对系统实现的直接含义",
    "对系统实现的直接含义",
        "Guardrail",
    "实验与证据限制",
    "摘要级线索",
    "Strong/foundation anchors",
    "Strong/foundation",
    "repo/data/env/experiment gate",
    "repo/data/env/experiment",
    "paper claims",
    "paper claim",
    "论文 claim",
    "claim promotion",
)

READ_VISIBLE_SECTION_RE = re.compile(
    r"(?:^|\n)\s*(?:#{1,6}\s*)?(?:对\s*(?:TASTE\s*)?(?:系统)?实现的直接含义|实验与证据限制|Guardrail|使用边界)\s*[:：]?\s*.*?(?=(?:\n\s*(?:#{1,6}\s*)?(?:原论文摘要|论文动机|详细方法|实验设置与结果|局限性|方法优缺点|方法机制|摘要|动机|方法|实验|局限)\b)|\Z)",
    re.I | re.S,
)

READ_VISIBLE_REGEX_REPLACEMENTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"对\s*(?:TASTE\s*)?(?:系统)?实现的直接含义", re.I), ""),
    (re.compile(r"(?:TASTE\s*)?系统实现", re.I), ""),
    (re.compile(r"\bGuardrail\b", re.I), ""),
    (re.compile(r"\bproject_topic\b", re.I), "当前主题"),
    (re.compile(r"摘要级线索"), "摘要信息"),
    (re.compile(r"Strong/foundation\s+anchors?\s+may\s+guide\s+planning[^.。]*[.。]?", re.I), ""),
    (re.compile(r"\bpaper\s+claims?\b", re.I), "论文表述"),
    (re.compile(r"论文\s*claim", re.I), "论文表述"),
    (re.compile(r"\bclaim\s+promotion\b", re.I), ""),
    (re.compile(r"repo/data/env/experiment\s+gate", re.I), "实验验证"),
    (re.compile(r"只有\s*repo/data/env/experiment[^。]*。?", re.I), ""),
    (re.compile(r"该条目是当前用户可见推荐文章[^。]*。?"), ""),
    (re.compile(r"必须进入精读[^。]*。?"), ""),
]


def _sanitize_read_text(value: Any, limit: int = 1400) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return ""
    text = READ_VISIBLE_SECTION_RE.sub("\n", text)
    replacements = {
        "命中当前项目配置的主题轴：project_topic；需进一步确认其数据、指标和可复现实验协议。": "",
        "需要从论文、代码和本地运行进一步确认数据集、评测协议、负采样、切片和消融；摘要级线索不能当作本地实验结果。": "",
        "可借鉴价值在于为后续 TASTE 精读、基底选择、实验设计或反例压力测试提供可复用线索；": "其价值在于提供方法、数据、协议或边界参考；",
        "后续 TASTE 精读": "后续精读",
        "TASTE 精读": "精读",
        "证据边界：": "适用边界：",
        "claim-ready": "可复核",
        "claim_ready": "可复核",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    for pattern, target in READ_VISIBLE_REGEX_REPLACEMENTS:
        text = pattern.sub(target, text)
    for marker in READ_VISIBLE_BANNED_MARKERS:
        text = text.replace(marker, "")
    text = re.sub(r"\s*[:：]\s*no\s*[.。]?\s*$", "", text, flags=re.I)
    text = re.sub(r"\s*[:：]\s*[.。]\s*$", "", text)
    text = re.sub(r"\s+", " ", text).strip(" -；;\t")
    return compact(text, limit)


def _sanitize_read_public_value(value: Any, limit: int = 1400) -> Any:
    if isinstance(value, str):
        return _sanitize_read_text(value, limit)
    if isinstance(value, list):
        cleaned: list[Any] = []
        for item in value:
            next_item = _sanitize_read_public_value(item, limit)
            if next_item not in ("", [], {}):
                cleaned.append(next_item)
        return cleaned
    if isinstance(value, dict):
        return {str(key): _sanitize_read_public_value(item, limit) for key, item in value.items()}
    return value


def _ensure_read_public_sentence_value(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if text and re.search(r"[\u4e00-\u9fff]", text):
            if text[-1] in "。！？.!?":
                return text
            if text[-1] in "）)】]`$" and len(text) > 1 and text[-2] in "。！？.!?":
                return text
            return text + "。"
        return text
    if isinstance(value, list):
        return [_ensure_read_public_sentence_value(item) for item in value]
    return value


def _sanitize_reading_public_fields(row: dict[str, Any]) -> dict[str, Any]:
    clean = dict(row)
    public_keys = {
        "summary", "abstract_zh", "deep_read_abstract_zh", "abstract_original", "abstract_from_find", "find_abstract_zh", "problem", "motivation_zh",
        "method", "method_details_zh", "method_family_zh", "experiments", "experiments_zh",
        "limitations", "limitations_zh", "method_advantages_zh", "method_disadvantages_zh",
        "relevance", "critique_reason", "reading_status_note_zh",
    }
    sentence_keys = {
        "summary", "abstract_zh", "deep_read_abstract_zh", "abstract_from_find", "find_abstract_zh", "problem", "motivation_zh",
        "method", "method_details_zh", "experiments", "experiments_zh", "limitations", "limitations_zh",
        "relevance", "critique_reason", "reading_status_note_zh",
    }
    for key in public_keys:
        if key in clean:
            clean[key] = _sanitize_read_public_value(clean[key], 2200)
            if key in sentence_keys or key in {"method_advantages_zh", "method_disadvantages_zh"}:
                clean[key] = _ensure_read_public_sentence_value(clean[key])
    return clean


def _split_sentences(text: Any, limit: int = 3, max_chars: int = 900) -> str:
    clean = _sanitize_read_text(text, max_chars * 2)
    if not clean:
        return ""
    parts = [part.strip() for part in re.split(r"(?<=[。！？.!?])\s+|(?<=[。！？])", clean) if part.strip()]
    if not parts:
        parts = [clean]
    return compact("".join(parts[:limit]), max_chars)


INLINE_MATH_FRAGMENT_RE = re.compile(
    r"(?<![`$])([A-Za-z0-9α-ωΑ-ΩθΘλΛϕφΦ̃ˆ_{}^|·⊤∈∉≤≥≈≠+*/=<>()[\],-]{2,}(?:[=^_⊤θΘλΛϕφΦ̃ˆ∈∉≤≥≈≠·|][A-Za-z0-9α-ωΑ-ΩθΘλΛϕφΦ̃ˆ_{}^|·⊤∈∉≤≥≈≠+*/=<>()[\],-]*)+)(?![`$])"
)


def _ensure_zh_sentence_end(text: str) -> str:
    clean = str(text or "").strip()
    if not clean:
        return ""
    if clean[-1] in "。！？.!?":
        return clean
    if clean[-1] in "）)】]`$" and len(clean) > 1 and clean[-2] in "。！？.!?":
        return clean
    return clean + "。"


def _protect_inline_math(text: str) -> str:
    clean = str(text or "")
    # The web preview does not run a TeX renderer. Regex-guessing math spans
    # made Chinese deep reads visibly worse, for example `$θ$−$y`. Keep
    # formulas readable as plain text, and only repair historical split spans.
    clean = re.sub(r"\$([^$\n]{1,80})\$([−-])\$([^$\n]{1,80})\$", r"\1\2\3", clean)
    clean = re.sub(r"\$([^$\n]{1,80})\$([−-])", r"\1\2", clean)
    clean = re.sub(r"([−-])\$([^$\n]{1,80})\$", r"\1\2", clean)
    return clean


def _render_read_paragraph(value: Any, fallback: str = "", limit: int = 2600) -> str:
    text = _sanitize_read_text(value, limit * 2)
    if not text:
        text = fallback
    if not text:
        return ""
    return _protect_inline_math(compact(_ensure_zh_sentence_end(text), limit))


def _method_family_zh(text: str) -> str:
    clean = _sanitize_read_text(text, 1600)
    if not clean:
        return "机制类别缺失"
    rules = [
        (("可信", "扩散", "推荐"), "可信内容扩散推荐"),
        (("偏好", "淡化", "离散扩散"), "离散扩散偏好比率建模"),
        (("PreferGrow", "偏好", "衰减"), "离散扩散偏好比率建模"),
        (("掩码", "扩散", "语言模型"), "掩码扩散语言模型"),
        (("半自回归", "专家"), "测试时多调度扩散解码"),
        (("HEX", "扩散LLM"), "测试时多调度扩散解码"),
        (("强化学习", "扩散", "策略"), "扩散模型强化学习对齐"),
        (("轨迹", "偏好", "奖励"), "轨迹级偏好优化"),
        (("信息增益", "标记"), "信息增益加权生成优化"),
        (("信息增益", "token"), "信息增益加权生成优化"),
        (("PANTHER", "用户行为"), "生成式用户行为预训练"),
        (("结构化分词", "交易"), "生成式用户行为预训练"),
        (("长尾", "相互学习"), "长尾扩散互学习"),
        (("相互学习", "纳什"), "长尾扩散互学习"),
        (("稀疏自编码器", "扩散"), "扩散模型可解释特征学习"),
        (("SAE", "Transformer块"), "扩散模型可解释特征学习"),
        (("分类", "偏好", "扩散"), "分类式扩散偏好对齐"),
        (("ABC", "分类"), "分类式扩散偏好对齐"),
        (("结构化推理", "推荐"), "LLM推理增强推荐"),
        (("RecZero", "GRPO"), "LLM推理增强推荐"),
        (("负反馈", "图对比"), "符号双通道图对比推荐"),
        (("SDCGCL", "双通道"), "符号双通道图对比推荐"),
        (("指令调优", "推荐"), "推荐指令调优数据集"),
        (("ITDR", "数据集"), "推荐指令调优数据集"),
        (("逻辑", "标签", "推荐"), "LLM增强逻辑推荐"),
        (("TagCF", "标签"), "LLM增强逻辑推荐"),
        (("物品ID", "专家"), "Item-ID/文本专家门控推荐"),
        (("IDIOMoE", "专家"), "Item-ID/文本专家门控推荐"),
        (("Language Ranker", "排序器"), "轻量级解码重排序"),
        (("listwise", "pointwise", "排序器"), "轻量级解码重排序"),
        (("Diffusion-DRO", "轨迹奖励"), "扩散轨迹偏好优化"),
        (("SLRM", "TAPO"), "噪声潜变量轨迹偏好优化"),
        (("连贯偏好", "噪声"), "LLM连贯偏好对齐推荐"),
        (("C-APO", "连贯"), "LLM连贯偏好对齐推荐"),
        (("多目标", "偏好", "扩散"), "多目标偏好引导扩散优化"),
        (("帕累托", "偏好分类器"), "多目标偏好引导扩散优化"),
        (("专家", "门控"), "专家门控条件融合"),
        (("语义", "推荐"), "语义条件推荐建模"),
        (("扩散", "推荐"), "扩散推荐建模"),
        (("扩散", "生成"), "扩散生成建模"),
    ]
    clean_lower = clean.lower()
    for tokens, label in rules:
        if all(token.lower() in clean_lower for token in tokens):
            return label
    title_like = clean.split("。", 1)[0].strip()
    return compact(title_like, 40) if title_like else "机制类别缺失"

def _method_advantages_zh(text: str, signals: dict[str, Any]) -> list[str]:
    if signals.get("has_code_signal"):
        return ["存在代码或实现线索；具体方法优点必须由全文精读合同确认后写入。"]
    return ["具体方法优点必须由全文精读合同确认后写入。"]


def _method_disadvantages_zh(text: str, signals: dict[str, Any]) -> list[str]:
    missing = [str(group) for group in signals.get("missing_topic_groups", []) if str(group).strip()] if isinstance(signals.get("missing_topic_groups"), list) else []
    if missing:
        return ["当前题录信号未覆盖全部配置主题组件；具体局限必须由全文精读合同确认后写入。"]
    return ["具体方法不足、适用边界和实验局限必须由全文精读合同确认后写入。"]


def _pending_full_text_method_text() -> str:
    return "详细方法待补；当前未读取到论文全文，不能仅凭题录或摘要确认模型结构、训练目标和推理流程。"


def _pending_full_text_experiment_text() -> str:
    return "实验设置与结果待补；需要从论文正文确认数据集、评价指标、对照方法、负采样、消融和主要结果。"


def _pending_full_text_limit_text() -> str:
    return "局限性待补；需要结合论文正文中的实验、消融和失败案例确认。"


def _pending_deep_read_method_text() -> str:
    return "全文文本证据已抓取；但项目代理还没有基于正文写出合格的中文详细方法，需补齐模型结构、训练目标、推理流程和关键模块。"


def _pending_deep_read_experiment_text() -> str:
    return "全文文本证据已抓取；但项目代理还没有基于正文写出合格的中文实验设置与结果，需补齐数据集、评价指标、对照方法、负采样、消融和主要结果。"


def _pending_deep_read_limit_text() -> str:
    return "全文文本证据已抓取；但项目代理还没有基于正文写出合格的中文局限性，需结合实验、消融和失败案例确认。"


def build_reading(row: dict[str, Any], cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    title = first_text(row, "title") or "Untitled"
    venue = first_text(row, "venue", "source")
    year = first_text(row, "year", "published", "updated")
    raw_abstract = first_text(row, "abstract_zh") or first_text(row, "abstract", "summary")
    abstract_zh = _sanitize_read_text(raw_abstract, 1600)
    reason = _sanitize_read_text(first_text(row, "fit_explanation_zh", "fit_explanation", "reason_zh", "reason", "recommendation_note_zh", "recommendation_note"), 1000)
    hits_zh = row.get("hit_directions_zh") or row.get("hit_directions") or []
    if not isinstance(hits_zh, list):
        hits_zh = [str(hits_zh)] if hits_zh else []
    hits_zh = [_sanitize_read_text(item, 120) for item in hits_zh if _sanitize_read_text(item, 120)]
    signals = classify_paper(row, cfg)
    pool = str(row.get("taste_pool") or "").strip()
    role = str(row.get("evidence_role") or "").strip() or "direct_target"
    positive = _paper_is_current_positive(row, pool) if pool else not bool(row.get("not_positive_support") or row.get("weak_candidate_for_critique")) and str(row.get("evidence_tier") or "").lower() == "strong_recommendation"
    full_text = " ".join(part for part in [title, abstract_zh, reason, " ".join(hits_zh)] if part)
    family = _method_family_zh(full_text)
    motivation = reason or _split_sentences(abstract_zh, limit=2, max_chars=700) or f"论文围绕《{title}》所处方向提出问题，需结合正文进一步确认研究动机。"
    method_details = _pending_full_text_method_text()
    experiments = _pending_full_text_experiment_text()
    limitations = _pending_full_text_limit_text()
    advantages: list[str] = []
    disadvantages = ["全文未读取，方法差异和优缺点待正文精读后确认。"]
    verdict = "core_reading" if positive and role == "direct_target" else "method_reference" if positive else "contrast_or_boundary_reading"
    support_role = "core_method_reference" if positive and role == "direct_target" else "transferable_method_reference" if positive else "contrast_or_boundary_reference"
    critique_reason = "" if positive else (reason or "该论文适合作为边界、对照或补充阅读，用于说明相邻方法与当前主题的差异。")
    return {
        "paper_id": first_text(row, "id", "paper_id") or re.sub(r"[^a-zA-Z0-9]+", "_", title.lower()).strip("_")[:80],
        "title": title,
        "url": first_text(row, "url", "abs_url"),
        "pdf_url": first_text(row, "pdf_url"),
        "venue": venue,
        "year": year,
        "score": row.get("recommendation_score") or row.get("score") or row.get("fit_score"),
        "score_source": row.get("score_source"),
        "taste_pool": row.get("taste_pool"),
        "taste_pool_rank": row.get("taste_pool_rank"),
        "hit_directions_zh": hits_zh,
        "abstract_original": first_text(row, "abstract") or first_text(row, "summary"),
        "abstract_zh": abstract_zh,
        "summary": abstract_zh,
        "problem": motivation,
        "motivation_zh": motivation,
        "method": method_details,
        "method_details_zh": method_details,
        "method_family_zh": family,
        "experiments": experiments,
        "experiments_zh": experiments,
        "limitations": limitations,
        "limitations_zh": limitations,
        "method_advantages_zh": advantages,
        "method_disadvantages_zh": disadvantages,
        "relevance": reason,
        "signals": signals,
        "abstract_available": bool(first_text(row, "abstract", "abstract_zh", "summary")),
        "full_text_available": False,
        "full_text_status": "pending_full_text_reading" if first_text(row, "pdf_url", "url") else "metadata_only",
        "verdict": verdict,
        "support_role": support_role,
        "critique_reason": critique_reason,
        "claim_ready_anchor": bool(positive),
        "positive_claim_evidence": bool(positive),
        "recommended_for_deep_reading": row.get("taste_pool") in {"strong_recommendations", "articles"} or bool(row.get("recommended_for_deep_reading")),
        "not_positive_support": not bool(positive),
        "weak_candidate_for_critique": bool(row.get("weak_candidate_for_critique") or not positive),
        "evidence_role": role if positive else "contrast_or_boundary",
        "evidence_tier": row.get("evidence_tier") or ("strong_recommendation" if positive else "critique_or_boundary_case"),
    }

def select_support(readings: list[dict[str, Any]], *, require_topic_match: bool = False, limit: int = 5) -> list[dict[str, Any]]:
    selected = []
    for row in readings:
        sig = row.get("signals", {}) if isinstance(row.get("signals"), dict) else {}
        if require_topic_match and not sig.get("matched_topic_group_count"):
            continue
        selected.append({"title": row.get("title"), "venue": row.get("venue"), "year": row.get("year"), "url": row.get("url"), "paper_id": row.get("paper_id")})
        if len(selected) >= limit:
            break
    return selected


def _project_topic(cfg: dict[str, Any]) -> str:
    for key in ["topic", "research_interest", "user_prompt", "title", "name"]:
        value = str(cfg.get(key) or "").strip()
        if value:
            return value
    return "当前研究主题"


def _paper_ref(row: dict[str, Any]) -> dict[str, Any]:
    return {"title": row.get("title"), "venue": row.get("venue"), "year": row.get("year"), "paper_id": row.get("paper_id"), "url": row.get("url")}



def build_ideas(readings: list[dict[str, Any]], repo: dict[str, Any], fresh_plan: dict[str, Any], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    topic = _project_topic(cfg)
    method_refs = [row for row in readings if row.get("claim_ready_anchor")][:6] or readings[:6]
    topic_axis_refs = select_support(readings, require_topic_match=True, limit=8)
    pending_target = {
        "selection_stage": "read_idea_plan_candidate_proposal",
        "status": "candidate_pending_environment_validation",
        "candidate_repo_hint": "to be proposed from current Find/Read evidence",
        "dataset_contract": {
            "status": "candidate_pending_environment_validation",
            "policy": "Find/Read/Idea/Plan must propose candidate base papers/repos and concrete modification targets when evidence supports them, but must not claim a repo is environment-selected, write local repo_path, or emit runnable training commands. Environment-stage Claude Code validates and locks repo/data/protocol evidence.",
        },
    }
    strong_refs = [_paper_ref(row) for row in method_refs[:6]]
    return [
        {
            "id": "idea-current-find-001",
            "idea_id": "idea-current-find-001",
            "title": "当前 Find 强推荐候选池的基础路线选择与复现审计",
            "status": "approved_for_planning",
            "recommendation": "candidate_repo_base_proposal_ready_for_environment_validation",
            "score": 9.0,
            "idea_score": 9.0,
            "hypothesis": f"围绕“{topic}”的后续科研应由 Idea/Plan 先提出候选基底论文或候选 repo、说明要修改的模块和验证目标；Environment 阶段再基于当前强推荐、代码、数据和协议证据验证并锁定可复现基底。Find 排名或历史 active_repo 不能直接决定主线。",
            "mechanism": "把当前强推荐论文拆成候选基底池；每个候选必须写清候选 repo/官方代码线索、可插入模块、预期最小改动、数据和指标要求，再交给 Environment 阶段审计 repo 可获得性、数据合同、入口命令、指标解析、复现协议和失败风险。",
            "implementation_target": pending_target,
            "initial_experiment": "先为当前强推荐论文池建立候选基底表：每个候选至少包含论文标题、官方或作者 repo 线索（若无 repo 则写明需搜索的官方代码位置）、拟修改模块、预期数据/指标协议和最小验证目标；Environment 阶段只负责验证并锁定其中一个候选，不得使用历史 active_repo 代替当前候选。",
            "initial_experiment_required": False,
            "bad_case_slice": ["代码不可得", "数据不可得", "协议不完整", "指标不可解析", "复现失败"],
            "success_gate": ["idea/plan 明确列出候选基底或候选 repo 线索", "Environment 阶段验证 fresh_find_run_id 与当前 Find 一致", "repo_path/data/protocol/metrics 均由 Environment 审计后写入", "没有使用历史 active_repo 作为默认主线"],
            "supporting_papers": strong_refs,
            "claude_code_tasks": ["读取当前 Find 强推荐、精读、ideas、plans 和补充检索主题。", "为每个候选基底审计代码、数据、协议、可运行入口和复现风险。", "只在证据充分时写入 evidence_ready_repo_selection.json；否则保持 blocked。"],
            "guardrail": "这是文献到候选基底 proposal 的规划 idea，不是 Environment 已选基底结论。",
        },
        {
            "id": "idea-current-find-002",
            "idea_id": "idea-current-find-002",
            "title": f"{topic} 的机制增强与消融路线",
            "status": "approved_for_planning",
            "recommendation": "candidate_repo_base_proposal_ready_for_environment_validation",
            "score": 8.2,
            "idea_score": 8.2,
            "hypothesis": "先在 idea/plan 中提出最适合承载该机制的候选基底论文或候选 repo，并说明最小增强模块；Environment 验证锁定该候选后，只有同协议 ablation 稳定提升才继续推进。",
            "mechanism": "从强推荐文献中提取可实现机制，作为条件信息、表示学习、生成/去噪、排序或约束模块，明确建议接入的候选基底及其待改模块；未通过 Environment 审计前不得写成已选主线。",
            "implementation_target": pending_target,
            "initial_experiment": "选择 1-2 个由强推荐论文支持的候选基底或官方 repo 线索，写清最小模块替换位置、同协议 baseline/control/ablation、主指标和坏例切片；Environment 阶段验证 repo/data/protocol 后才把候选变成本地主线。",
            "initial_experiment_required": False,
            "bad_case_slice": ["主线方法失败样本", "短序列/稀疏样本", "长尾样本", "机制预期受益样本"],
            "success_gate": ["候选基底 proposal 可追溯到当前 Find/Read 证据", "Environment 阶段基底验证通过", "基础复现已通过", "新增模块有 ablation", "整体指标和坏例切片同时报告"],
            "supporting_papers": strong_refs[:4] + topic_axis_refs,
            "claude_code_tasks": ["在选定 repo 中定位最小可插拔模块位置。", "实现单一增强模块和开关参数。", "输出 ablation metrics 与坏例切片，不允许只报告平均值。"],
            "guardrail": "二阶段 idea，必须先提出候选基底和修改点，再等待 Environment 验证；不能越过复现 gate。",
        },
        {
            "id": "idea-current-find-003",
            "idea_id": "idea-current-find-003",
            "title": f"{topic} 的坏例切片与反例压力测试",
            "status": "approved_for_planning",
            "recommendation": "candidate_repo_base_proposal_ready_for_environment_validation",
            "score": 7.8,
            "idea_score": 7.8,
            "hypothesis": "如果候选方法只提升平均指标却在声明相关的困难切片上失败，论文主张必须收窄或停止。",
            "mechanism": "从评测日志抽取错误高置信样本、长尾失败、冷启动失败和语义/行为冲突样本，形成反例压力测试。",
            "implementation_target": pending_target,
            "initial_experiment": "",
            "initial_experiment_required": True,
            "bad_case_slice": ["高置信错误", "冷启动", "长尾", "语义冲突", "时间漂移"],
            "success_gate": ["bad-case 文件可审计", "反例数量和切片指标随每次实验更新", "claim ledger 不允许忽略失败切片"],
            "supporting_papers": [_paper_ref(row) for row in readings[:6]],
            "claude_code_tasks": ["实现或修复 bad-case 抽取脚本。", "把 bad-case/counterexample 写入实验注册表。", "刷新 claim/evidence gate，阻止没有切片证据的论文提升。"],
            "guardrail": "坏例证据是论文提升前置条件，不是事后装饰。",
        },
        {
            "id": "idea-current-find-004",
            "idea_id": "idea-current-find-004",
            "title": f"{topic} 的跨数据与稳健性验证",
            "status": "approved_for_planning",
            "recommendation": "candidate_repo_base_proposal_ready_for_environment_validation",
            "score": 7.4,
            "idea_score": 7.4,
            "hypothesis": "一个可投稿主张必须在至少一个主数据集和必要对照上站得住；如果只依赖单次偶然结果，应保持 blocked。",
            "mechanism": "扩展到第二数据集、第二 seed 或关键超参，验证效果是否稳定，并记录失败边界。",
            "implementation_target": pending_target,
            "initial_experiment": "",
            "initial_experiment_required": True,
            "bad_case_slice": ["跨数据迁移失败", "seed 敏感", "超参敏感", "训练预算敏感"],
            "success_gate": ["所有运行有命令和日志", "失败结果也记录", "最终 claim 明确适用边界"],
            "supporting_papers": [_paper_ref(row) for row in readings[:8]],
            "claude_code_tasks": ["整理可运行数据集和 seed 矩阵。", "批量运行前先做预算和进程守护。", "把成功和失败都写入 experiment_records 与 evidence manifest。"],
            "guardrail": "稳健性验证不能替代主线复现，只能在主线复现后推进。",
        },
        {
            "id": "idea-current-find-005",
            "idea_id": "idea-current-find-005",
            "title": f"{topic} 的推荐列表质量修复与主题边界再检索路线",
            "status": "approved_for_planning",
            "recommendation": "wait_for_literature_gate_repair_then_environment_base_selection",
            "score": 7.2,
            "idea_score": 7.2,
            "hypothesis": "如果当前推荐列表数量不足或主题纯度不足，后续科研应先修复检索、摘要抓取、LLM 标题+摘要评分和误推荐边界，而不是把未评分/无摘要候选补成推荐或提前绑定基底。",
            "mechanism": "从推荐列表和边界审计中归纳缺口，生成补充检索主题、复核 LLM 打分理由、降级误推荐，并在推荐数量达标后再交给环境阶段选择基底。",
            "implementation_target": pending_target,
            "initial_experiment": "",
            "initial_experiment_required": True,
            "bad_case_slice": ["泛主题论文误推荐", "只命中单一弱主题轴", "任务或评测协议不匹配", "只有 foundation-only 支撑", "摘要缺失或证据不足"],
            "success_gate": ["推荐论文数量达到配置目标", "误推荐进入边界审计而非正锚点", "targeted_search_queries 已记录", "source_status 可审计", "补检索仅通过 TASTE 统一 literature tool 受控执行"],
            "supporting_papers": [_paper_ref(row) for row in readings[:8]],
            "claude_code_tasks": ["读取当前 Find 的推荐论文、边界审计和来源状态。", "围绕缺口生成补充检索主题；如推荐数量短缺，通过 modules/finding/main.py --action run_literature_tool 受控补检索并刷新 packet。", "复核评分/降级原因并刷新 literature gate，禁止用未评分/无摘要论文凑推荐数量。"],
            "guardrail": "这是 current Find 质量修复路线；它不能替代环境阶段基底选择，也不能作为论文 claim 证据。",
        },
    ]


def build_plans(ideas: list[dict[str, Any]], fresh_plan: dict[str, Any]) -> list[dict[str, Any]]:
    plans: list[dict[str, Any]] = []
    common = [
        "确认当前 Find run_id、read_results、ideas、plans 全部一致，旧 fallback 或旧候选不得驱动后续流程。",
        "Idea/Plan 必须给出候选基底论文或候选 repo 线索、最小改动位置、数据/指标协议和失败停止条件；Environment 阶段负责验证并锁定这些候选。",
        "Environment 阶段验证通过后才能写入 state/evidence_ready_repo_selection.json，且 selection_stage 必须等于 environment_claude_code、fresh_find_run_id 必须等于当前 run_id。",
        "在 Environment 锁定前，候选 repo 只能作为 proposal；不得写本地 repo_path、不得写具体数据集训练命令、不得标记 ready_to_execute、不得把历史 active_repo 当作当前主线。",
        "每个实验必须记录 command、env、repo commit/hash、dataset contract、stdout/loss、metrics、bad-case/counterexample。",
        "实验完成后刷新 reference/scientific/evidence/submission gates，未过 gate 不得 promotion。",
    ]
    for idea in ideas:
        idea_steps = list(idea.get("claude_code_tasks", [])) if isinstance(idea.get("claude_code_tasks"), list) else []
        steps = idea_steps + common
        plans.append({
            "plan_id": "plan-" + str(idea.get("id")),
            "idea_id": idea.get("id"),
            "title": idea.get("title"),
            "hypothesis": idea.get("hypothesis"),
            "status": "waiting_for_environment_base_selection",
            "completed": False,
            "completed_at": "",
            "fresh_find_run_id": fresh_plan.get("fresh_find_run_id") if isinstance(fresh_plan, dict) else "",
            "versions": [{
                "version": 1,
                "status": "waiting_for_environment_base_selection",
                "generated_at": now_iso(),
                "implementation": {
                    "target": idea.get("implementation_target", {}),
                    "minimum_experiment": idea.get("min_experiment"),
                    "bad_case_slice": idea.get("bad_case_slice", []),
                    "success_gate": idea.get("success_gate", []),
                    "metrics": ["primary task metric", "ranking/retrieval metrics", "bad-case slice metrics", "runtime/budget", "counterexample count"],
                    "claude_code_tasks": idea_steps,
                },
                "evaluation_rounds": [{
                    "round": 1,
                    "evaluation": "计划已绑定当前 Find 推荐文章和候选基底 proposal，但必须等待 Environment 阶段验证并锁定 repo/data/env/protocol。",
                    "weaknesses": ["仍需本地数据、代码和实验审计确认", "摘要级文献不能直接支撑论文 claim"],
                    "repair_summary": ["加入环境阶段基底选择、gate、bad-case、命令/日志/metrics 落盘要求。"],
                }],
                "final_plan": {
                    "experimental_design": idea.get("min_experiment"),
                    "steps": steps,
                    "go_no_go": "如果环境阶段基底选择、repo/data/env 或实验审计不满足，则保持 blocked，继续修复数据/代码/实验，不启动论文写作或 claim promotion。",
                    "paper_claim_policy": "只有 environment/reference/scientific/evidence gates 通过后才能 promotion。",
                },
                "llm": {"generator": "claude_code_current_find_takeover_guarded", "evaluator": "current_find_environment_selection_gate"},
            }],
        })
    return plans

def _read_text_is_placeholder(text: Any) -> bool:
    value = str(text or "")
    if not value.strip():
        return False
    placeholder_markers = FULL_TEXT_CONTENT_PLACEHOLDERS + (
        "全文未读取",
        "待补全文",
        "当前可访问正文证据不足",
        "当前可访问证据不足",
    )
    return any(marker in value for marker in placeholder_markers)


def _read_field(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if isinstance(value, list):
            text = "；".join(_sanitize_read_text(item, 500) for item in value if _sanitize_read_text(item, 500))
        else:
            text = _sanitize_read_text(value, 2200)
        if text and not _read_text_is_placeholder(text):
            return text
    return ""


def _read_list(row: dict[str, Any], *keys: str) -> list[str]:
    out: list[str] = []
    for key in keys:
        value = row.get(key)
        values = value if isinstance(value, list) else [value]
        for item in values:
            text = _sanitize_read_text(item, 500)
            if text and not _read_text_is_placeholder(text) and text not in out:
                out.append(text)
    return out


def _read_original_abstract_for_display(row: dict[str, Any]) -> str:
    return _deep_read_abstract_candidate(row)


def _find_row_original_abstract(row: dict[str, Any]) -> str:
    if not isinstance(row, dict):
        return ""
    for key in ["abstract_zh", "summary_zh", "abstract_cn", "abstract_chinese", "abstract", "abstract_en", "summary"]:
        text = _sanitize_read_text(row.get(key), 2400)
        if text and not _read_text_is_placeholder(text):
            return text
    return ""


def _find_row_for_reading(row: dict[str, Any], recommendation_index: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(row, dict) or not recommendation_index:
        return {}
    for identity in _reading_identity_values(row):
        if identity in recommendation_index:
            return recommendation_index[identity]
    title = norm_title(row.get("title") or row.get("paper_title"))
    return recommendation_index.get(f"title:{title}", {}) if title else {}


def _merge_find_metadata_into_reading(row: dict[str, Any], find_row: dict[str, Any] | None) -> dict[str, Any]:
    clean = dict(row) if isinstance(row, dict) else {}
    source = find_row if isinstance(find_row, dict) else {}
    if not source:
        return clean
    if not first_text(clean, "paper_id", "id", "entry_id"):
        paper_id = first_text(source, "paper_id", "id", "entry_id")
        if paper_id:
            clean["paper_id"] = paper_id
    for dest, keys in {
        "title": ("title", "paper_title"),
        "venue": ("venue", "source"),
        "year": ("year", "published", "updated"),
        "url": ("url", "abs_url"),
        "pdf_url": ("pdf_url",),
    }.items():
        if not first_text(clean, dest):
            value = first_text(source, *keys)
            if value:
                clean[dest] = value
    if _numeric_or_none(clean.get("score")) is None:
        for key in ["recommendation_score", "score", "fit_score", "taste_score"]:
            value = _numeric_or_none(source.get(key))
            if value is not None:
                clean["score"] = value
                clean.setdefault("score_source", key)
                break
    if _numeric_or_none(clean.get("fit_score")) is None:
        value = _numeric_or_none(source.get("fit_score"))
        if value is not None:
            clean["fit_score"] = value
    for key in ["taste_pool", "taste_pool_role", "taste_pool_rank", "evidence_tier", "evidence_role"]:
        if clean.get(key) in (None, "", []):
            value = source.get(key)
            if value not in (None, "", []):
                clean[key] = value
    return clean


def _merge_find_abstract_into_reading(row: dict[str, Any], find_row: dict[str, Any] | None) -> dict[str, Any]:
    clean = _merge_find_metadata_into_reading(row, find_row)
    find_abstract = _find_row_original_abstract(find_row or {})
    if not find_abstract:
        return clean
    _set_original_abstract_fields(clean, find_abstract, "find_results")
    return clean


def _render_method_overview(readings: list[dict[str, Any]]) -> list[str]:
    lines = ["## 方法差异、优缺点总览\n\n"]
    lines.append("| # | 论文 | 机制类别 | 主要优点 | 主要局限 |\n")
    lines.append("|---|---|---|---|---|\n")
    families: list[str] = []
    for idx, row in enumerate(readings, 1):
        title = _sanitize_read_text(row.get("title"), 180).replace("|", " ")
        family = _read_field(row, "method_family_zh") or _method_family_zh(" ".join(str(row.get(k) or "") for k in ["title", "abstract_zh", "method_details_zh", "method"]))
        family = family.replace("待全文精读确认的方法类别", "机制类别缺失")
        if family and family != "机制类别缺失" and family not in families:
            families.append(family)
        advantages = "；".join(_ensure_zh_sentence_end(item) for item in _read_list(row, "method_advantages_zh")[:2]) or "机制优点缺失，需项目代理返修。"
        disadvantages = "；".join(_ensure_zh_sentence_end(item) for item in _read_list(row, "method_disadvantages_zh", "limitations_zh", "limitations")[:2]) or "方法局限缺失，需项目代理返修。"
        lines.append(f"| {idx} | {title} | {_protect_inline_math(family).replace('|', ' ')} | {_protect_inline_math(advantages).replace('|', ' ')} | {_protect_inline_math(disadvantages).replace('|', ' ')} |\n")
    lines.append("\n### 总体差异\n\n")
    if families:
        lines.append(_ensure_zh_sentence_end("这些论文覆盖" + "、".join(families[:8]) + "等机制路线，具体取舍以上表的优点和局限为准") + "\n")
    else:
        lines.append("机制类别仍缺失，需项目代理重新完成逐篇精读和方法对比。\n")
    return lines


def render_read_md(readings: list[dict[str, Any]], run_id: str) -> str:
    visible_readings = [row for row in readings if isinstance(row, dict)]
    completed = sum(1 for row in visible_readings if _reading_has_full_text_evidence(row) and _reading_has_full_text_content(row))
    pending = max(0, len(visible_readings) - completed)
    lines = ["# 当前 Find 推荐论文精读\n\n", f"- run_id: `{run_id}`\n", f"- readings: {len(visible_readings)}\n", f"- 全文精读完成: {completed}\n"]
    if pending:
        lines.append(f"- 未通过精读合同: {pending}\n")
    lines.append("\n")
    for idx, row in enumerate(readings, 1):
        title = _sanitize_read_text(row.get("title"), 240)
        meta = [str(row.get("venue") or "").strip(), str(row.get("year") or "").strip()]
        meta_text = " ".join(item for item in meta if item).strip()
        lines.extend([
            f"## {idx}. {title}\n\n",
            f"- venue/year: {meta_text or '未回填'}\n",
            f"- score: {_score_display(row.get('score'))}\n",
            f"- URL: {row.get('url') or '未回填'}\n",
            f"- PDF: {row.get('pdf_url') or '未回填'}\n\n",
            "### 原论文摘要（中文）\n",
            _render_read_paragraph(_read_original_abstract_for_display(row), "（原论文摘要未通过精读合同。）", 2600) + "\n\n",
            "### 论文动机\n",
            _render_read_paragraph(_read_field(row, "motivation_zh", "problem"), "（论文动机未通过精读合同。）", 2600) + "\n\n",
            "### 详细方法\n",
            _render_read_paragraph(_read_field(row, "method_details_zh", "method"), "（详细方法未通过精读合同。）", 4200) + "\n\n",
            "### 实验设置与结果\n",
            _render_read_paragraph(_read_field(row, "experiments_zh", "experiments"), "（实验设置与结果未通过精读合同。）", 3200) + "\n\n",
            "### 局限性\n",
            _render_read_paragraph(_read_field(row, "limitations_zh", "limitations"), "（局限性未通过精读合同。）", 2600) + "\n\n",
            "### 方法优缺点\n",
        ])
        advantages = _read_list(row, "method_advantages_zh")
        disadvantages = _read_list(row, "method_disadvantages_zh")
        if advantages:
            lines.append("优点：\n")
            for item in advantages:
                lines.append(f"- {_protect_inline_math(_ensure_zh_sentence_end(item))}\n")
        if disadvantages:
            lines.append("不足：\n")
            for item in disadvantages:
                lines.append(f"- {_protect_inline_math(_ensure_zh_sentence_end(item))}\n")
        lines.append("\n")
    lines.extend(_render_method_overview(readings))
    return "".join(lines)


def render_idea_md(ideas: list[dict[str, Any]], run_id: str, paper_index: dict[str, dict[str, Any]] | None = None) -> str:
    lines = ["# 当前 Find 驱动 Ideas\n\n", f"- run_id: `{run_id}`\n", f"- ideas: {len(ideas)}\n\n"]
    for idx, idea in enumerate(ideas, 1):
        inspired = _normalize_inspired_refs(idea.get("inspired_by"), as_list(idea.get("supporting_papers") or idea.get("positive_anchor_papers")), paper_index)
        metadata = [
            f"- id: `{idea.get('id')}`\n" if str(idea.get("id") or "").strip() else "",
            f"- status: {idea.get('status')}\n" if str(idea.get("status") or "").strip() else "",
            f"- score: {_score_display(_idea_display_score(idea))}\n",
        ]
        recommendation = str(idea.get("recommendation") or "").strip()
        if recommendation and recommendation.lower() not in {"none", "null", "n/a", "na"}:
            metadata.append(f"- recommendation: {recommendation}\n")
        objective_scores = _idea_objective_scores(idea) if isinstance(idea, dict) else {}
        if objective_scores:
            score_labels = {
                "novelty": "novelty",
                "evidence_alignment": "evidence",
                "feasibility": "feasibility",
                "experimentability": "experimentability",
                "risk_control": "risk_control",
                "overall": "overall",
            }
            score_text = "; ".join(f"{score_labels.get(key, key)}={_score_display(value)}" for key, value in objective_scores.items())
            if score_text:
                metadata.append(f"- objective_scores: {score_text}\n")
        audit = idea.get("idea_score_audit") if isinstance(idea.get("idea_score_audit"), dict) else {}
        if audit.get("subagent_used") is True:
            metadata.append("- scoring_audit: subagent completed\n")
        lines.extend([
            f"## {idx}. {idea.get('title')}\n\n",
            *[item for item in metadata if item],
            "\n",
            "### 新方法\n",
            compact(
                "\n\n".join(
                    part
                    for part in [
                        compact(idea.get("new_method") or idea.get("hypothesis"), 2000),
                        compact(idea.get("method_details") or idea.get("mechanism"), 2000),
                    ]
                    if part
                ),
                3600,
            ) + "\n\n",
            "### 初步实验\n",
            (compact(idea.get("initial_experiment") or idea.get("min_experiment") or idea.get("minimum_experiment"), 2000) or "待项目代理根据精读结果补齐：需要说明基于哪项工作或基底、做什么最小改动、对比哪些 baseline/control/ablation、使用哪些指标和坏例切片。") + "\n\n",
            "### 启发来源\n",
        ])
        for source in inspired:
            meta = " ".join(str(source.get(key) or "").strip() for key in ["source", "year"] if str(source.get(key) or "").strip())
            suffix = f" ({meta})" if meta else ""
            url = str(source.get("url") or "").strip()
            reason = str(source.get("reason") or "").strip()
            lines.append(f"- {source.get('title')}{suffix}{(' - ' + reason) if reason else ''}{(' - ' + url) if url else ''}\n")
        lines.append("\n")
    return "".join(lines)


def _claude_takeover_timeout() -> int:
    try:
        return int(os.environ.get("CURRENT_FIND_CLAUDE_TIMEOUT_SEC", "3600") or 3600)
    except Exception:
        return 3600


def _claude_takeover_no_progress_timeout() -> int:
    try:
        return int(os.environ.get("CURRENT_FIND_CLAUDE_NO_PROGRESS_TIMEOUT_SEC", "300") or 300)
    except Exception:
        return 300


def _current_find_artifact_latest_mtime(paths) -> float:
    candidates: list[Path] = []
    finding = paths.planning / "finding"
    for name in CURRENT_FIND_CONTENT_ARTIFACT_NAMES:
        candidates.append(finding / name)
    fragment_dir = finding / CURRENT_FIND_DEEP_READ_FRAGMENT_DIR_NAME
    if fragment_dir.exists():
        candidates.extend(fragment_dir.glob("*.json"))
    candidates.extend([
        paths.state / "current_find_claude_reading_validation.json",
        paths.state / "current_find_full_text_evidence_repair.json",
    ])
    latest = 0.0
    for path in candidates:
        try:
            latest = max(latest, path.stat().st_mtime)
        except OSError:
            continue
    return latest


def _terminate_process_group(proc: subprocess.Popen, *, grace_sec: float = 3.0) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    deadline = time.monotonic() + max(0.1, grace_sec)
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)
    if proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def _trim_text_tail(lines: list[str], limit: int = 8000) -> str:
    text = "\n".join(lines)
    return text[-limit:]


def _write_live_claude_result(path: Path, payload: dict[str, Any]) -> None:
    try:
        save_json(path, payload)
    except Exception:
        pass


def _run_claude_session_streaming(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    paths,
    result_path: Path,
    stdout_path: Path,
    prompt_path: Path,
    run_id: str,
    attempt: int,
    stage: str,
    timeout_sec: int,
    started: str,
) -> dict[str, Any]:
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    output_lines: list[str] = []
    stdout_chars = 0
    last_activity = time.monotonic()
    last_artifact_mtime = _current_find_artifact_latest_mtime(paths)
    no_progress_timeout = max(60, _claude_takeover_no_progress_timeout())
    hard_timeout = max(60, int(timeout_sec or 3600) + 180)
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        start_new_session=True,
    )
    status = "running"
    stop_reason = ""
    started_mono = time.monotonic()
    last_status_write = 0.0
    with stdout_path.open("w", encoding="utf-8") as stdout_handle:
        assert proc.stdout is not None
        while True:
            now = time.monotonic()
            ready, _, _ = select.select([proc.stdout], [], [], 1.0)
            if ready:
                line = proc.stdout.readline()
                if line:
                    line = line.rstrip("\n")
                    stdout_handle.write(line + "\n")
                    stdout_handle.flush()
                    print(line, flush=True)
                    output_lines.append(line)
                    if len(output_lines) > 2000:
                        output_lines = output_lines[-1000:]
                    stdout_chars += len(line) + 1
                    compact_line = " ".join(line.split()).strip().lower()
                    if compact_line != "claude: still running; waiting for claude code output":
                        last_activity = now
                elif proc.poll() is not None:
                    break
            artifact_mtime = _current_find_artifact_latest_mtime(paths)
            if artifact_mtime > last_artifact_mtime:
                last_artifact_mtime = artifact_mtime
                last_activity = now
            if now - last_status_write >= 5:
                _write_live_claude_result(
                    result_path,
                    {
                        "status": "running",
                        "stage": stage,
                        "run_id": run_id,
                        "return_code": None,
                        "started_at": started,
                        "updated_at": now_iso(),
                        "prompt_path": str(prompt_path),
                        "repair_attempt": attempt,
                        "stdout_tail": _trim_text_tail(output_lines),
                        "stdout_path": str(stdout_path),
                        "stdout_chcount": stdout_chars,
                        "no_progress_timeout_sec": no_progress_timeout,
                    },
                )
                last_status_write = now
            if proc.poll() is not None:
                break
            if now - started_mono > hard_timeout:
                status = "timeout"
                stop_reason = "claude_session_hard_timeout"
                _terminate_process_group(proc)
                break
            if now - last_activity > no_progress_timeout:
                status = "timeout_no_progress"
                stop_reason = "claude_session_no_progress_timeout"
                _terminate_process_group(proc)
                break
    rc = proc.poll()
    if rc is None:
        rc = proc.wait(timeout=5)
    if status == "running":
        status = "completed" if rc == 0 else "failed"
    return {
        "status": status,
        "return_code": int(rc or 0),
        "stop_reason": stop_reason,
        "stdout_tail": _trim_text_tail(output_lines),
        "stdout_path": str(stdout_path),
        "stdout_chcount": stdout_chars,
        "stderr_tail": "",
        "no_progress_timeout_sec": no_progress_timeout,
    }


def _compact_validation_for_prompt(validation: Any) -> dict[str, Any]:
    if not isinstance(validation, dict):
        return {}
    keys = [
        "status",
        "valid",
        "policy_version",
        "expected_recommendation_count",
        "actual_reading_count",
        "full_text_evidence_count",
        "full_text_reading_count",
        "pending_deep_read_synthesis_count",
        "pending_without_evidence_count",
        "pending_full_text_reading_count",
        "blockers",
        "pending_deep_read_synthesis_titles",
        "full_text_packet_conflict_titles",
        "deep_read_content_gap_details",
        "pending_without_evidence_titles",
        "pending_full_text_reading_titles",
        "full_text_evidence_titles",
        "full_text_reading_titles",
        "missing_recommendation_titles",
        "duplicate_reading_titles",
        "extra_reading_titles",
        "invalid_positive_titles",
        "invalid_positive_count",
        "unlabeled_non_positive_titles",
        "unlabeled_non_positive_count",
        "critique_or_boundary_titles",
        "critique_or_boundary_count",
        "expected_recommendation_titles",
        "expected_positive_titles",
        "artifact_parse_failures",
        "idea_contract_issues",
        "raw_idea_contract_issues",
        "next_required_action",
        "subagent_policy_version",
        "subagent_deep_read_audit",
    ]
    source = validation.get("validation") if isinstance(validation.get("validation"), dict) else validation
    compacted: dict[str, Any] = {}
    for key in keys:
        if key not in source:
            continue
        value = source.get(key)
        compacted[key] = value[:20] if isinstance(value, list) else value
    nested = source.get("reading_validation") if isinstance(source, dict) else None
    if isinstance(nested, dict):
        compacted["reading_validation"] = _compact_validation_for_prompt(nested)
    for key in ["observed", "takeover"]:
        value = validation.get(key)
        if isinstance(value, dict):
            compacted[key] = value
    return compacted




def stale_deep_read_fragment_summary(paths, run_id: str, limit: int = 12) -> dict[str, Any]:
    """Report same-directory deep-read fragments that cannot belong to the current Find run."""
    planning_root = getattr(paths, "planning", None)
    if planning_root is None:
        return {"run_id": run_id, "stale_fragment_count": 0, "stale_fragments": []}
    fragment_dir = Path(planning_root) / "finding" / CURRENT_FIND_DEEP_READ_FRAGMENT_DIR_NAME
    expected = str(run_id or "").strip()
    stale: list[dict[str, Any]] = []
    run_counts: dict[str, int] = {}
    if not fragment_dir.exists():
        return {"current_run_id": expected, "stale_fragment_count": 0, "stale_run_ids": [], "stale_fragments": []}
    for path in sorted(fragment_dir.glob("*.json")):
        payload, error = load_json_with_error(path, {})
        if error or not isinstance(payload, dict):
            continue
        payload_run = str(payload.get("run_id") or payload.get("current_find_run_id") or "").strip()
        if not payload_run or payload_run == expected:
            continue
        reading = payload.get("reading") if isinstance(payload.get("reading"), dict) else {}
        title = str(reading.get("title") or payload.get("title") or "").strip()
        run_counts[payload_run] = run_counts.get(payload_run, 0) + 1
        if len(stale) < limit:
            stale.append({"name": path.name, "run_id": payload_run, "title": title})
    return {
        "current_run_id": expected,
        "stale_fragment_count": sum(run_counts.values()),
        "stale_run_ids": sorted(run_counts),
        "stale_run_id_counts": run_counts,
        "stale_fragments": stale,
        "policy": "Fragments whose top-level run_id differs from the current Find run are audit-only. They must not be reused as current Read evidence; write a new fragment with the current run_id after reading the current full_text_packet text_path.",
    }

def write_claude_takeover_prompt(paths, project: str, run_id: str, read_limit: int, idea_count: int, repair_validation: dict[str, Any] | None = None, attempt: int = 1) -> Path:
    prompt_path = paths.state / ("current_find_claude_takeover_prompt.md" if attempt <= 1 else f"current_find_claude_takeover_repair_prompt_attempt{attempt}.md")
    target_venue = project_target_venue(project) or "ICLR"
    repair_block = ""
    stale_fragment_report = stale_deep_read_fragment_summary(paths, run_id)
    stale_fragment_block = ""
    if int(stale_fragment_report.get("stale_fragment_count") or 0) > 0:
        stale_fragment_block = f"""

旧 run_id deep-read 分片审计（只读，不是当前 Read 证据）：
```json
{json.dumps(stale_fragment_report, ensure_ascii=False, indent=2)}
```
这些分片的顶层 run_id 与当前 run_id `{run_id}` 不一致，wrapper 会拒绝它们。你不得把这些旧分片当成当前 Read 已完成，也不得复制旧顶层 run_id；如果同题论文仍在当前 reading packet 中，必须重新打开当前 `full_text_reading/full_text_packet.json` 的对应 `text_path`，用 Claude Code 文件工具写入新的唯一分片文件，且新分片顶层 `run_id` 必须严格等于 `{run_id}`。
"""
    idea_plan_count = max(1, _positive_int(idea_count) or 5)
    not_selected_count = max(0, idea_plan_count - 1)
    read_item_count = max(0, _positive_int(read_limit) or 0)
    canonical_reading_packet_block = ""
    try:
        full_text_packet_for_prompt = load_current_full_text_packet(paths, run_id)
    except Exception:
        full_text_packet_for_prompt = {}
    if isinstance(full_text_packet_for_prompt, dict) and str(full_text_packet_for_prompt.get("run_id") or "").strip() == str(run_id or "").strip():
        readable_packet_rows: list[dict[str, Any]] = []
        audit_only_rows: list[dict[str, Any]] = []
        for packet_index, entry in enumerate(as_list(full_text_packet_for_prompt.get("papers")), 1):
            if not isinstance(entry, dict):
                continue
            text_chars = _positive_int(entry.get("text_chars") or entry.get("pdf_text_chars") or entry.get("full_text_chars") or entry.get("source_text_chars"))
            row = {
                "packet_index": packet_index,
                "paper_id": first_text(entry, "paper_id", "id", "entry_id"),
                "title": first_text(entry, "title", "paper_title"),
                "text_path": first_text(entry, "text_path"),
                "text_chars": text_chars,
                "read_replacement": bool(entry.get("read_replacement")),
                "replacement_for_unavailable_recommendation": first_text(entry, "replacement_for_unavailable_recommendation", "replacement_for"),
            }
            if row["text_path"] and text_chars >= FULL_TEXT_MIN_CHARS:
                readable_packet_rows.append(row)
            else:
                audit_only_rows.append({k: v for k, v in row.items() if v not in ("", None, False)})
        required_rows = readable_packet_rows[: read_item_count or len(readable_packet_rows)]
        packet_prompt_payload = {
            "run_id": run_id,
            "canonical_source": "planning/finding/full_text_reading/full_text_packet.json",
            "required_readable_paper_count": len(required_rows),
            "required_readable_papers": required_rows,
            "audit_only_unavailable_count": len(audit_only_rows),
            "audit_only_unavailable_rows": audit_only_rows,
            "policy": "Read fragments must cover required_readable_papers exactly. Audit-only unavailable rows and originals replaced by read_replacement rows must not receive deep-read fragments.",
        }
        canonical_reading_packet_block = f"""

当前 Read canonical reading packet（机器生成，必须优先于原始 Find Top-N 解读）：
```json
{json.dumps(packet_prompt_payload, ensure_ascii=False, indent=2)}
```
`full_text_packet.papers` 是当前 Read 阶段唯一 canonical reading packet。你必须只为 `required_readable_papers` 中列出的有 `text_path` 且正文长度足够的论文写 deep-read 分片；其中 `read_replacement=true` 的论文是同一 run 的合法 Read 输入。`audit_only_unavailable_rows` 仅用于审计不可读原推荐或被替换对象，不能为这些行写精读分片，不能把它们计入 Read 完成数。
"""
    if isinstance(repair_validation, dict) and repair_validation:
        repair_payload = _compact_validation_for_prompt(repair_validation)
        failure_type = str(repair_validation.get("failure_type") or repair_payload.get("failure_type") or "").strip()
        validation_payload = repair_validation.get("validation") if isinstance(repair_validation.get("validation"), dict) else repair_payload.get("validation") if isinstance(repair_payload.get("validation"), dict) else repair_payload
        observed_payload = repair_validation.get("observed") if isinstance(repair_validation.get("observed"), dict) else repair_payload.get("observed") if isinstance(repair_payload.get("observed"), dict) else {}
        reading_contract_valid = bool(
            validation_payload.get("valid") is True
            and _positive_int(validation_payload.get("actual_reading_count")) >= max(1, read_item_count)
            and _positive_int(validation_payload.get("pending_deep_read_synthesis_count")) == 0
            and _positive_int(validation_payload.get("pending_full_text_reading_count")) == 0
            and not validation_payload.get("deep_read_content_gap_details")
        )
        idea_only_repair = failure_type == "idea_contract_failed" and reading_contract_valid
        if idea_only_repair:
            prompt = f"""
你是项目 `{project}` 的持久 Claude Code 科研会话。TASTE 已经完成当前 Find 的 Read 机器校验；本轮只允许做 Idea/Plan 窄返修，不允许重做精读。

当前 run_id: `{run_id}`
目标 idea/plan 数: {idea_plan_count}

上一轮失败合同（必须逐项修复）：
```json
{json.dumps(repair_payload, ensure_ascii=False, indent=2)}
```

只读输入：
- `planning/finding/read_results.json`：已经由 wrapper 从 current-run deep-read fragments 重建，Read contract 已通过，只读参考。
- `planning/finding/ideas.json`：本轮需要用 Claude 文件工具修订。
- `planning/finding/plans.json`：如 selected plan 与修复后的 idea 不匹配，本轮需要用 Claude 文件工具同步修订。
- `planning/finding/find_results.json` 与 `planning/finding/full_text_reading/full_text_packet.json`：只用于核对 paper_id/title，不要重写。

硬性禁止：
- 不要启动 Task/subagent 重新精读 20 篇论文；Read 已通过，重做精读是低效且会污染当前返修边界。
- 不要 Write/Edit/MultiEdit `planning/finding/current_find_deep_read_fragments/`、`read_results.json`、`read.md`、`idea.md`、`plan.md`。
- 不要写入 `state/current_find_research_plan.json`、`state/idea_candidates.json`、`state/experiment_plan.json` 或任何 gate/state 文件。
- 不要用 Bash/Python/cat/heredoc 读取、生成或修补 `ideas.json`、`plans.json`、`read_results.json` 或 deep-read fragments。内容查看必须用 Claude Read；写入必须用 Claude Write/Edit/MultiEdit。Bash 只允许 `{management_python()} -m json.tool planning/finding/ideas.json >/dev/null` 和同类 JSON 语法检查。
- 不要启动 Find、训练、实验、paper、full-cycle 或后台进程。

必须完成：
1. 修复 `planning/finding/ideas.json` 顶层 `run_id`，必须严格等于 `{run_id}`，`source` 保持 `claude_code_current_find_takeover`。
2. 恰好保留 {idea_plan_count} 个 ideas。每个 idea 必须有非空 `id`, `title`, `new_method`, `initial_experiment`, `inspired_by`, `supporting_papers`。
3. `inspired_by` 必须是非空数组；每项至少包含可核对的 `paper_id` 或 `title`，以及具体 `insight`/`role`，说明该论文如何启发新方法。不能只写论文名列表。
4. 每个 idea 必须有 `objective_scores={{"novelty":...,"evidence_alignment":...,"feasibility":...,"experimentability":...,"risk_control":...,"overall":...}}`，同时写 `score` 和 `idea_score`，且 `idea_score_audit={{"mode":"task_subagent","subagent_used":true,"status":"completed","criteria":"TASTE-like objective idea scoring"}}`。若当前 Claude 工具面板没有 Task/subagent，仍必须诚实写 `status:"blocked_task_subagent_unavailable"` 并说明，不能伪造已调用。
5. 如果修复 idea id/title 后导致 `plans.json` 的 `idea_id` 或 `selected_idea_id` 不匹配，必须同步修复 `plans.json`。`plans.json` 必须仍然恰好 {idea_plan_count} 个 plans，且只能有一个 `selected_for_execution=true` 和 `execute_next=true`，其 `idea_id` 必须匹配一个修复后的 idea。
6. Plan/Idea 阶段必须提出候选基底论文/候选 repo 线索、为什么适合、怎么改、Environment 需要验证哪些 repo/data/protocol 证据；但不得写本地 `repo_path`、具体数据集训练命令、`ready_to_execute`，也不得声称环境/实验已通过；保持 `candidate_base_proposal_waiting_for_environment_validation` / `ready_for_gate` 语义。

最后只输出简短中文摘要：修复了哪些 idea 字段、selected plan 是否仍唯一、是否需要环境阶段继续授权基底。
""".strip() + "\n"
            prompt_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text(prompt, encoding="utf-8")
            return prompt_path
    if isinstance(repair_validation, dict) and repair_validation:
        repair_payload = _compact_validation_for_prompt(repair_validation)
        repair_block = f"""

本次是第 {attempt} 次受控返修。上一轮 Claude Code 进程返回码可能为 0，但 TASTE 合同校验判定它没有完成当前 Find 精读/Idea/Plan，因此这不是成功执行。
必须先修复下面机器可读失败清单，不要启动 Find、训练、full-cycle 或论文写作。精读内容必须先按论文写入逐篇 JSON 分片，wrapper 会合并分片并渲染 Markdown artifact。

上一轮失败合同：
```json
{json.dumps(repair_payload, ensure_ascii=False, indent=2)}
```

返修要求：
- failure_type=claude_current_find_takeover_failed 表示上一轮 Claude Code 进程失败，不能用 deterministic fallback 或旧 artifact 代替本轮精读；必须重新执行当前 Find 的受控 Read/Idea/Plan，并让产物时间跟随本轮 takeover。
- failure_type=stale_or_missing_current_find_takeover 表示当前 Find 或 full_text_packet/full-text evidence 已经在上一轮 Claude 输出之后更新，或者上一轮没有真实 prompt_path；必须重新打开 full_text_packet.json 和其中的 text_path 正文，逐篇重写 read_results/read.md/ideas/plans，不得把旧精读与新全文证据拼接成通过状态。
- artifact_parse_failures 表示上一轮写出的 JSON artifact 已损坏，TASTE 无法解析其内容；本轮精读必须改用逐篇分片协议：每篇论文用 Claude Code 文件工具写一个 `planning/finding/current_find_deep_read_fragments/<rank>_<paper_id>.json` 或返修分片，每个分片只含该论文的 reading 对象；`read_results.json` 由 wrapper 从分片重建，不能直接改；`ideas.json` 和 `plans.json` 必须用 Claude 文件工具生成或修订，最终必须是完整可解析 JSON，并由 wrapper 做合同校验。不要写 read.md/idea.md/plan.md，Markdown 由 wrapper 渲染；不得用 Bash/Python/cat/heredoc 批量修写科研 artifact 或分片。
- pending_deep_read_synthesis_titles 表示已有 PDF/HTML 正文抽取证据，但 Claude 没有基于正文写出原论文摘要、动机、详细方法、实验结果、局限和优缺点；必须打开 full_text_packet.json 的 text_path 正文并逐篇重写。
- full_text_packet_conflict_titles 表示 TASTE 已取得可读正文 text_path/pdf_url/text_chars，但上一轮 read_results/read.md 仍声称全文不可访问、摘要仅或 metadata-only；必须以 full_text_packet 为准，打开对应 text_path，删除不可访问结论并重写该论文精读。
- deep_read_content_gap_details 是逐篇字段合同失败清单；必须按其中列出的字段写入 `abstract_zh`、`motivation_zh`、`method_details_zh` 或中文 `method`、`experiments_zh` 或中文 `experiments`、`limitations_zh` 或中文 `limitations`，并写入 `method_advantages_zh`、`method_disadvantages_zh` 各至少两条。`abstract_from_find` 可以作为论文原摘要溯源来生成/迁移 `abstract_zh`，但不能替代 `motivation_zh`、`method_details_zh`、`experiments_zh`、`limitations_zh`、优点和不足；英文-only method/experiments/limitations、`relevance=direct_target|foundation_borrowing|boundary_audit` 枚举值也不能替代这些字段。
- 如果 deep_read_content_gap_details 指出字段过短、摘要冒充、方法/实验关键词不足或优缺点缺失，必须重新打开对应 `text_path` 全文逐篇重写；禁止在旧短句上补几个形容词冒充精读。
- idea_contract_issues 表示上一轮 idea 不是合格科研 idea：缺 `objective_scores`、`score/idea_score` 为 None、没有独立评分子任务、初步实验仍绑定历史基底/旧 base switch gate，或 `new_method`/`initial_experiment`/`inspired_by` 三段缺失。必须重新基于精读结果生成 {idea_plan_count} 个 idea；每个 idea 都要先由主控 Claude 写出详细 `new_method`、`initial_experiment`、`inspired_by`，再调用独立 Task/subagent 客观评分，写入 `objective_scores={{novelty,evidence_alignment,feasibility,experimentability,risk_control,overall}}`、`score`、`idea_score` 和 `idea_score_audit={{mode:"task_subagent",subagent_used:true,status:"completed"}}`。`overall < 7.0` 的 idea 必须由主控 Claude 深思修改后重新评分；不得把 None/空分数或旧路线记忆当成通过。
- 如果 idea_contract_issues 含 `stale_or_preselected_base_binding_detected`，说明 idea/实验仍继承了历史已选基底、旧执行 gate、本地 `repo_path`、具体训练命令、base switch gate、已通过参考复现等权威状态。必须新开主控 Claude Code，在项目目录内重新阅读 `state/current_find_research_plan.json`、`planning/finding/` 下当前 Find 精读产物、`state/taste_plan_bridge.json` 和当前 main route 状态，生成不继承旧会话记忆的 idea。不得读取或依赖仓库根 `工作状态.txt` 作为项目科研记忆。Find/Read/Idea/Plan 阶段应明确提出候选基底论文/候选 repo 线索、修改位置和验证要求，但只能写成 proposal；禁止写成环境已选、禁止写本地 `repo_path`、禁止写具体训练命令或 `ready_to_execute`。
- unlabeled_non_positive_titles 表示当前推荐论文在 Find 里是 boundary/critique/weak/borrowed/foundation 价值，但 reading 没有明确非 claim 角色；必须补 `support_role`/`verdict`，说明为什么值得精读、能借鉴什么、不能证明什么。若 Find 行本身已有 `evidence_role=foundation_borrowing`、`evidence_tier=critique_or_boundary_case`、`not_positive_support=true` 或 `weak_candidate_for_critique=true`，优先沿用该边界角色，不要把它改成 claim-ready。
- pending_without_evidence_titles 表示还缺正文证据；必须读取可用 PDF/OpenReview/网页/代码说明，若确实无法访问，要记录具体不可访问原因，不能写“待补”冒充精读。
- subagent_deep_read_audit 表示主控 Claude Code 没有把每篇推荐论文交给可审计的 Task/subagent 做全文精读，或没有把交付记录写入每个 reading；这会被视为未精读。必须对每篇推荐论文调用 Task/subagent（若工具名为 Task，直接使用 Task；若当前 Claude 工具面板没有 Task/subagent，立即停止并报告 `blocked: task_subagent_unavailable_for_deep_reading`，不要由主控自己短写替代）。每个 reading 必须写 `subagent_deep_read=true`，并写 `deep_read_audit={{"mode":"task_subagent","subagent_used":true,"status":"completed","text_path":"...","evidence_chars":...}}`。
- 如果上一轮 takeover.tool_policy_guard.policy_type 是 current_find_artifact_writer，说明你尝试用 Bash/Python/cat/heredoc 批量写科研 artifact，或直接修补 wrapper-owned `read_results.json`，已被 TASTE 拦截；本轮精读只能使用 Claude Code 文件工具写逐篇分片，路径为 `planning/finding/current_find_deep_read_fragments/<rank>_<paper_id>.json` 或唯一返修分片。`read_results.json` 只能由 wrapper 从这些分片重建；不要直接 Write/Edit/MultiEdit 它。`ideas.json` 和 `plans.json` 可以用 Claude 文件工具 Write/Edit/MultiEdit，但每次结束时必须保持完整可解析 JSON，且需要满足 idea/plan 合同。单篇 deep-read fragment JSON 可以用 Claude 文件工具自审修正，但不得用 Bash/Python/cat/heredoc/json.dump/open(..., "w") 生成或补丁式改写；禁止写 `read.md`/`idea.md`/`plan.md`，这些 Markdown 由 wrapper 在 JSON 校验后自动生成。禁止在 Bash 中出现 `open(...read_results.json`、`Path(...current_find_deep_read_fragments`、`json.dump`、`python <<`、`cat > planning/finding`、heredoc 或任何会写入 Read/Idea/Plan artifact/分片的命令。Bash 只允许只读检查，例如 `{management_python()} -m json.tool planning/finding/ideas.json >/dev/null`、`rg`、`sed -n`、`head`、`tail`、`wc`。禁止在 Bash 中使用 `python -c`、`python <<`、`open(...)`、`Path(...).read_text()` 或任何自写解析脚本触碰 `planning/finding/current_find_deep_read_fragments/`、`read_results.json`、`ideas.json`、`plans.json`，即使你认为只是只读；内容查看必须用 Claude Read 工具，JSON 语法检查只能用 `{management_python()} -m json.tool <path> >/dev/null`。如果分片 JSON 校验失败，必须用 Claude 文件工具重新写该论文完整分片；绝不能用 Bash/Python 修补。若当前 Claude Code 会话没有文件写入工具，必须停止并在最终输出中报告 `blocked: write_tool_unavailable_for_current_find_artifacts`。若上一轮 policy_type 是 current_find_gate_state_writer，说明你直接改写了 TASTE-owned gate/state 文件；本轮只能写 planning/finding 的逐篇精读分片、ideas.json、plans.json，state/current_find_research_plan.json、state/idea_candidates.json、state/experiment_plan.json 必须保持只读，由 wrapper 在机器校验后统一写入。
- 返修时不得覆盖已有 `current_find_deep_read_fragments/*.json`。如果某篇已有分片但 validation 指出字段缺口，必须写一个新的 `planning/finding/current_find_deep_read_fragments/<rank>_<paper_id>_repair_attempt{attempt}.json` 或带时间戳的新分片；wrapper 会按全文证据、字段完整度和质量分选择最佳分片。禁止用较短、字段更少或 evidence 更弱的新分片覆盖旧分片。
- 返修后 {read_item_count} 个精读分片、ideas.json、plans.json 必须同一 run_id 且必须等于当前 `{run_id}`、同一推荐列表、同一 {idea_plan_count} 个 idea/plan，并且 plans.json 必须显式且只能显式选择一个执行计划；read_results.json/read.md/idea.md/plan.md 由 wrapper 生成或刷新。
- failure_type=missing_selected_plan/ambiguous_selected_plan/selected_plan_id_missing/selected_plan_missing_matching_idea 表示精读、idea、plan 内容可能已经合格，但主控 Claude Code 没有在 {idea_plan_count} 个计划中作出唯一最佳执行选择，或多个计划被同时标记选择。必须基于全文精读结果重新比较 {idea_plan_count} 个 plan，写出 exactly one `selected_for_execution=true` 且 `execute_next=true` 的 plan，其余 plan 必须显式 `selected_for_execution=false`、`execute_next=false`、`execution_selection.selected=false` 并作为 backlog。
"""
    prompt = f"""
你是 项目 `{project}` 的持久 Claude Code 科研会话。现在由 TASTE 控制你接管当前 Find 后面的 Read/Idea/Plan，不允许用确定性模板伪造科研判断。
{repair_block}{stale_fragment_block}{canonical_reading_packet_block}

必须读取的真实输入：
- `planning/finding/find_results.json`
- `planning/finding/article.md`
- `planning/finding/source_status.md`
- `planning/finding/full_text_reading/full_text_packet.json`；如果该文件存在且 run_id 匹配，它就是当前 Read 的 canonical reading packet。必须优先读取其中有 `text_path` 且正文长度足够的 {read_item_count} 篇 `papers`，包括 same-run `read_replacement=true` replacement；不要重新下载同一 PDF，也不要只引用题录/摘要。
- `planning/finding/full_text_reading/texts/*.txt` 中与 canonical reading packet 可读行对应的正文抽取文件；写逐篇精读分片时必须把这些正文证据归一化为 `source_evidence`/`full_text_evidence`/`pdf_text_chars`。没有正文的原始 Find 推荐或已被 replacement 替换的原推荐只作审计，不得写 deep-read 分片。
- `planning/finding/current_find_deep_read_fragments/`：这是本轮精读唯一允许的逐篇内容写入目录。每篇论文一个 JSON 文件，文件由 Claude Code 文件工具写入或修正，禁止 Bash/Python/cat/heredoc 生成。wrapper 会合并这些分片为 read_results.json/read.md。
- `state/literature_tool_packet.json` 或 `planning/literature_tool_packet.md`
- 当前环境选择证据（只读审计）：`state/fresh_research_base.json`、`state/evidence_ready_repo_selection.json`、`planning/reference_workflow_and_claude_code.md`。`state/active_repo.json` 和 `state/fresh_base_implementation_plan.json` 只能作为历史/候选证据读取；只有 `selection_stage=environment_claude_code` 且 `fresh_find_run_id={run_id}` 的 `evidence_ready_repo_selection.json` 才能把某个 repo/base 当作当前基底。

硬性要求：
1. 只围绕当前 run_id `{run_id}`。所有新写入的 `current_find_deep_read_fragments/*.json`、`ideas.json`、`plans.json` 顶层 `run_id` 都必须严格等于 `{run_id}`；任何旧 run_id 分片只能作为审计反例，不能作为当前产物。必须精读 Read canonical reading packet 里的全部 {read_limit} 篇可读论文；当 `full_text_reading/full_text_packet.json` run_id 匹配且 `papers` 非空时，canonical packet 就是其中有 `text_path` 且正文长度足够的 rows，包括同一 run 的 `read_replacement=true` replacement。原始 Find Top-N 是用户可见 Find 输出，不是 Read 完成数的唯一来源；无正文的原推荐、已由 replacement 替掉的原推荐和 packet 中 0 字符 rows 只作审计，禁止写 deep-read 分片或计入完成。不得重写 Find 的用户可见推荐。若 `full_text_reading/full_text_packet.json` 已提供正文抽取文本，必须打开对应 `text_path` 并基于正文写原论文摘要（中文）、论文动机、详细方法、实验设置与结果、局限性、方法优缺点；不得把“全文包已就绪”当成精读内容。主控 Claude Code 必须把每篇论文拆成独立精读任务，并且必须调用 Task/subagent 逐篇审读全文；禁止主控自己用几句 synthesis 替代子任务。若当前会话没有 Task/subagent 工具，必须停止并报告 `blocked: task_subagent_unavailable_for_deep_reading`，不能降级为主控直接写。每篇 reading 必须记录 `subagent_deep_read=true` 和 `deep_read_audit`，包括 `mode=task_subagent`、`subagent_used=true`、`status=completed`、对应 `text_path` 和正文长度。对每篇都要优先使用可用 `pdf_url`/OpenReview 页面/项目页/代码链接和已有正文文本；如果 PDF 或网页无法访问，必须在该条 `full_text_status` 中说明不可访问原因，不能用流程话术代替论文内容。KDD/ACM 论文必须先按正确来源协议探索：从 DOI/ACM DL 页面、OpenAlex/Crossref/DBLP 元数据和作者主页确认论文身份，再查找同题 arXiv 或机构 repository PDF；只有确认同题同作者/年份后才能使用 arXiv/repository PDF，不得一上来堆 fallback，也不得把 ACM 页面抓取失败写成全文不可读。每篇成功精读的 reading 必须写 `full_text_available=true`，`full_text_status` 为 `pdf_text_read`/`html_text_read`/`full_text_read` 之一，并记录 `pdf_text_chars`、`full_text_chars`、`source_text_chars` 或 `source_evidence` 中的正文长度/来源；同时必须写中文合同字段 `abstract_zh`、`motivation_zh`、`method_details_zh` 或中文 `method`、`experiments_zh` 或中文 `experiments`、`limitations_zh` 或中文 `limitations`，以及 `method_advantages_zh`、`method_disadvantages_zh`。`abstract_zh` 可以直接使用或翻译 Find 阶段捕获的论文原摘要；但只有题录、推荐理由、主题命中、流程话术、“全文包已就绪但未写精读 synthesis”、`abstract_from_find` 单独存在且其余精读字段缺失、英文-only 精读字段或“待补全文”都会被 TASTE 门控拒绝。
   字段质量下限：
   - `abstract_zh` 至少 260 个非空白中文字符，必须呈现论文原摘要的中文内容；可以直接使用/翻译 Find 捕获的论文原摘要，但不能用推荐理由、主题命中、`critique_reason` 或流程说明冒充摘要。
   - `motivation_zh` 至少 180 个非空白中文字符，说明论文要解决的具体矛盾、已有方法不足和任务背景。
   - `method_details_zh` 至少 650 个非空白中文字符，并覆盖模型/框架结构、训练或优化目标、推理/采样/解码流程、输入输出或关键模块。
   - `experiments_zh` 至少 420 个非空白中文字符，并覆盖数据/任务、基线或对照、指标、主要结果或消融。
   - `limitations_zh` 至少 220 个非空白中文字符，并结合正文说明实验边界、适用范围、成本或迁移风险。
   - `method_advantages_zh` 和 `method_disadvantages_zh` 各至少两条，每条至少 55 个非空白中文字符，必须是具体中文结论，不能写“待正文确认”“仅视觉生成验证”这类短占位。
   - 所有公式、LaTeX、代码式标识符、模型名、数据集名、指标名、阿拉伯数字、百分比、p 值、K 值、top-K、学习率、温度、显存/参数规模和实验结果必须保留原始符号写法；禁止把 `0.0264` 写成“零点零二六四”、把 `50.86%` 写成“百分之五十点八六”、把 `1e-5` 写成“一乘以十的负五次方”，也不要把普通短语包成公式。
   提交分片前必须逐篇自查并显著超过上述下限，不要卡线：建议 `abstract_zh` 至少 360 个非空白中文字符、`motivation_zh` 至少 260 个、`method_details_zh` 至少 850 个、`experiments_zh` 至少 560 个、`limitations_zh` 至少 320 个，优缺点每条至少 80 个；如果某字段接近下限，必须继续基于正文补充具体机制、实验或边界，而不是让 wrapper 返修。
2. 推荐列表里的论文可以是 strict positive、foundation/borrowing 或 boundary/critique 价值，但都必须写入 `read_results.json.readings`。未达到 strict positive 条件的推荐论文必须标为 `support_role=boundary_audit|search_expansion`、`verdict=recommended_reading_boundary|critique_only|boundary_only`，说明为什么值得读、能借鉴什么、不能证明什么。
3. 严禁把 `triage_candidates/audit_candidates/evaluated_candidates/title_candidates/retrieval_candidates` 中未进入推荐列表或 strict positive 白名单的论文写成 `claim_ready_anchor`、`positive_anchor_for_planning`、`supporting_evidence` 或 `component_reference`。TASTE 守卫会直接拒绝这种输出。
4. 检查每篇候选是否真与当前科研主题相关；把无关、泛主题、泛 agent、泛 memory、仅新闻/论文选择/平台选择的文章降为 critique，不得作为 strong evidence。
5. Idea 必须生成 {idea_plan_count} 个，并且每个 idea 都要综合多篇强锚点、边界审计结论和当前 repo/data/env 约束，不能照抄单篇论文。每个 idea 必须包含三段：`new_method`（详细的新方法，原 hypothesis 的升级版，说明核心机制/模块/训练或推理作用点）、`initial_experiment`（初步详细实验，必须说明候选基底论文或候选 repo 线索、为什么适合、做什么最小改动、对比哪些 baseline/control/ablation、指标和坏例切片、Environment 阶段需验证什么）、`inspired_by`（启发该方法的论文/工作及启发点）。严禁继承旧会话记忆里的历史已选基底、旧 base switch gate、已通过参考复现基底或已经废弃的路线；Find/Read/Idea/Plan 阶段可以且应该提出候选仓库/候选基底 proposal，但不能把任何仓库写成已选基底或可直接执行路径。
6. 主控 Claude Code 必须为每个 idea 调用独立 Task/subagent 做客观评分，不得由主控直接填 None 或空分数。评分项必须写入 `objective_scores`，字段为 `novelty`、`evidence_alignment`、`feasibility`、`experimentability`、`risk_control`、`overall`，每项 0-10 分；同时写入 `score` 和 `idea_score`，且 `idea_score_audit={{"mode":"task_subagent","subagent_used":true,"status":"completed","criteria":"TASTE-like objective idea scoring"}}`。若 `overall < 7.0` 或任一项为 0，主控必须先修改 idea 再重新评分；不能把低分 idea 交给 plan。
7. 你要自行决定至少 3 个补充检索主题，并写入 `targeted_search_queries`。当当前推荐门控短缺且本轮 run 尚未刚完成受控补检索时，优先调用一次 TASTE 统一 literature tool 做受控补检索并刷新 packet：`{management_python()} modules/finding/main.py --action run_literature_tool --project {project} --venue {target_venue} --query "<topic 1>" --query "<topic 2>" --query "<topic 3>" --fast-mode --publish-current-find`。如果 `state/literature_tool_last_run.json` 已显示刚完成的 `current_find_run_id` 等于当前 run_id，或当前任务就是为了把刚产生的新 Find run 同步成 Read/Idea/Plan，则不要再启动下一轮 Find；只记录/复用 `targeted_search_queries`，先为当前 run 写出一致的 Read/Idea/Plan。严禁绕过 wrapper 直接调用会失去审计的原始 TASTE 命令。
7. 为 {idea_plan_count} 个 idea 分别生成 plan：环境阶段如何比较强推荐论文、如何审计 repo/data/protocol、最小实验、baseline/ablation、bad-case slice、success gate、失败时的停止条件。主控 Claude Code 必须在 {idea_plan_count} 个 plan 中选择唯一最佳执行计划；唯一选中的 plan 必须写 `selected_for_execution=true`、`execute_next=true`、`execution_selection={{"selected": true, "reason": "...", "selected_by": "main_claude_code_after_deep_read"}}`，并具有非空 `plan_id` 与匹配 `idea_id`；其他 {not_selected_count} 个 plan 必须显式写 `selected_for_execution=false`、`execute_next=false`、`execution_selection={{"selected": false, "selected_by": "not_selected_candidate_backlog", "reason": "..."}}`。禁止按第一个、分数、排序或模板代选；必须用精读证据说明为什么被选中的 plan 最值得进入环境阶段。
8. Find/Read/Idea/Plan 阶段必须给出候选基底 proposal（候选论文/候选 repo 名称或 URL、拟修改模块、数据/指标协议、验证风险），但严禁声称它已经是“当前基底”、严禁写入本地 `repo_path`、严禁写具体数据集训练命令、严禁标记 `ready_to_execute`。Environment 阶段负责验证并锁定基底，只有它才能写入 `state/evidence_ready_repo_selection.json`。
9. 不启动论文 claim promotion；没有 Environment 阶段基底验证和 repo/data/env/experiment gate 证据时，计划必须保持候选 proposal / `ready_for_gate`，而不是声称已经有结论或已选本地主线。
10. 写内容产物必须使用 Claude Code 文件工具；这是强制要求，不是偏好。精读内容必须写成逐篇 JSON 分片，避免一次性写 {read_item_count} 篇大 JSON 把语法或内容写坏。`read_results.json` 由 wrapper 从分片重建，不要直接 Write/Edit/MultiEdit；`ideas.json` 和 `plans.json` 可以用 Claude 文件工具 Write/Edit/MultiEdit 生成或修订，但结束时必须保持完整可解析 JSON。如果某个分片 JSON 校验失败，用 Claude 文件工具重新写该论文完整分片。Bash 只允许用于只读检查、JSON 语法校验或必要的官方 wrapper；JSON 语法校验只能使用 `python -m json.tool <path> >/dev/null`。不要用 Bash/Python/cat here-doc 代替文件工具来批量写 `read_results.json`、`current_find_deep_read_fragments/*.json`、`ideas.json`、`plans.json`，也不要写 `read.md`、`idea.md`、`plan.md`；也不要用 `python -c`、`python <<`、`open(...)`、`Path(...).read_text()` 在 Bash 中读取或解析这些 artifact，内容查看必须用 Claude Read。严禁写入或修改 `state/current_find_research_plan.json`、`state/idea_candidates.json`、`state/experiment_plan.json`；这些 TASTE-owned gate/state 文件由 wrapper 在机器校验后统一写入。如果必须用临时脚本校验 JSON，只能写到 `/tmp`，不得启动实验、训练、Find、full-cycle 或后台进程。如果你无法用文件写入工具写这些 artifact，必须报告 blocked，不能尝试脚本 fallback。
11. 本阶段只允许读取当前 Find 相关输入：`planning/finding/find_results.json`、`planning/finding/full_text_reading/`、`planning/finding/source_status.md`、已有有效 deep-read 分片，以及为 plan 可执行性所需的当前 selected repo 少量 README/数据说明。禁止读取 `state/current_find_artifact_backups/`、`paper/`、`obsidian/`、`discover/`、旧草稿、旧 idea/plan 备份来拼接当前科研判断；这些只保留审计历史，不是当前 Find 证据。
12. Idea 评分最多两轮：初评 + 一次有实质变化的重评。若重评后仍没有达到阈值的 idea，不要继续扩散检索或无限改写；用 Claude 文件工具写入当前候选及评分/失败原因，让 wrapper 以 `idea_plan_artifacts_incomplete` 或相应合同失败阻塞，并在最终回复里说明 `blocked: idea_score_threshold_not_met`。

必须写入这些结构化 JSON 内容产物，且 JSON 必须可解析。不要写 Markdown artifact，wrapper 会从分片/JSON 渲染 `read.md`、`idea.md`、`plan.md`：
- `planning/finding/current_find_deep_read_fragments/<rank>_<paper_id>.json`，每篇论文一个文件；返修轮次优先使用 `<rank>_<paper_id>_repair_attempt{attempt}.json` 或唯一时间戳文件名。字段：`run_id`, `source="claude_subagent_deep_read_fragment"`, `reading`。`reading` 至少含 `paper_id`, `title`, `verdict`, `support_role`, `critique_reason`, `abstract_zh`, `motivation_zh`, `method_details_zh` 或中文 `method`, `experiments_zh` 或中文 `experiments`, `limitations_zh` 或中文 `limitations`, `method_advantages_zh`, `method_disadvantages_zh`, `full_text_available`, `full_text_status`, `subagent_deep_read=true`, `deep_read_audit`，并在成功读取 PDF/HTML 正文时写入正文长度证据字段。`abstract_from_find` 可以保留作溯源，但不能替代 `abstract_zh`。
- `planning/finding/ideas.json`，字段：`run_id`, `source="claude_code_current_find_takeover"`, `ideas`，恰好 {idea_plan_count} 个 idea，均为 approved 或 blocked_with_reason。每个 idea 必须含 `id`, `title`, `new_method`, `initial_experiment`, `inspired_by`, `supporting_papers`；可选兼容字段 `method_details`/`mechanism` 可用于内部计划，但用户可见 `idea.md` 只呈现 `new_method`, `initial_experiment`, `inspired_by` 三段；兼容字段 `hypothesis` 应等同 `new_method`，`min_experiment` 应等同 `initial_experiment`。
- `planning/finding/plans.json`，字段：`run_id`, `source="claude_code_current_find_takeover"`, `plans`，对应 {idea_plan_count} 个 idea。`plans` 中必须且只能有一个 plan 同时满足：非空 `plan_id`、匹配某个 idea 的 `idea_id`、`selected_for_execution=true`、`execute_next=true`、`execution_selection.selected=true`、`execution_selection.selected_by="main_claude_code_after_deep_read"`、`execution_selection.reason` 说明基于精读证据的选择理由。其余 plan 必须显式为 backlog，不能留下空白让 TASTE 代选。

不要写入或修改这些 TASTE-owned gate/state 文件：`state/current_find_research_plan.json`、`state/idea_candidates.json`、`state/experiment_plan.json`。wrapper 会在读取上述内容产物并通过/阻塞机器校验后，同步这些 state 文件。

最后输出简短中文 Markdown：读了哪些论文、每篇全文/PDF访问状态、剔除了哪些误推荐、{idea_plan_count} 个 idea 标题、下一步 gate/实验阻塞。
""".strip() + "\n"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt, encoding="utf-8")
    return prompt_path


def run_claude_current_find_takeover(project: str, paths, run_id: str, read_limit: int, idea_count: int, repair_validation: dict[str, Any] | None = None, attempt: int = 1) -> dict[str, Any]:
    fragment_dir = paths.planning / "finding" / CURRENT_FIND_DEEP_READ_FRAGMENT_DIR_NAME
    fragment_dir.mkdir(parents=True, exist_ok=True)
    fragment_parse_failures = current_find_deep_read_fragment_failures(paths, run_id)
    fragment_quarantine = quarantine_corrupt_current_find_deep_read_fragments(paths, run_id, fragment_parse_failures, "pre_takeover_parse_failure") if fragment_parse_failures else {}
    if isinstance(fragment_quarantine, dict) and any((row if isinstance(row, dict) else {}).get("action") == "quarantined_with_valid_same_run_replacement" for row in as_list(fragment_quarantine.get("files"))):
        fragment_parse_failures = current_find_deep_read_fragment_failures(paths, run_id)
    preexisting_parse_failures = current_find_json_artifact_failures(paths)
    if preexisting_parse_failures or fragment_parse_failures:
        quarantine = quarantine_corrupt_current_find_json_artifacts(paths, run_id, preexisting_parse_failures, "pre_takeover_parse_failure") if preexisting_parse_failures else {}
        if fragment_quarantine:
            quarantine = {**(quarantine if isinstance(quarantine, dict) else {}), "deep_read_fragment_quarantine": fragment_quarantine}
        all_parse_failures = preexisting_parse_failures + fragment_parse_failures
        repair_validation = {
            **(repair_validation if isinstance(repair_validation, dict) else {}),
            "status": "artifact_parse_failed",
            "artifact_parse_failures": all_parse_failures,
            "fragment_parse_failures": fragment_parse_failures,
            "corrupt_artifact_quarantine": quarantine,
            "next_required_action": "rerun_current_find_claude_takeover_repair_rewrite_parseable_artifacts",
        }
        record_current_find_artifact_parse_failure(paths.state, run_id, all_parse_failures)
    prompt_path = write_claude_takeover_prompt(paths, project, run_id, read_limit, idea_count, repair_validation=repair_validation, attempt=attempt)
    snapshot = snapshot_current_find_artifacts(paths, run_id, attempt)
    session_key = "current_find_read_idea_plan"
    cmd = [
        sys.executable,
        str(ROOT / "framework" / "scripts" / "claude_project_session.py"),
        "--project",
        project,
        "--stage",
        "current-find-claude-read-idea-plan",
        "--message-file",
        str(prompt_path),
        "--timeout-sec",
        str(_claude_takeover_timeout()),
        "--agent-id",
        session_key,
        "--no-resume",
    ]
    started = now_iso()
    env = os.environ.copy()
    env["USE_EXISTING_LITERATURE_PACKET"] = env.get("USE_EXISTING_LITERATURE_PACKET", "1")
    env["CLAUDE_FIRST_OUTPUT_TIMEOUT_SEC"] = os.environ.get("CURRENT_FIND_CLAUDE_FIRST_OUTPUT_TIMEOUT_SEC", "180")
    env["CLAUDE_NO_EVENT_TIMEOUT_SEC"] = os.environ.get("CURRENT_FIND_CLAUDE_NO_EVENT_TIMEOUT_SEC", "300")
    env["CLAUDE_CURRENT_FIND_NO_EVENT_FLOOR_SEC"] = os.environ.get("CURRENT_FIND_CLAUDE_NO_EVENT_FLOOR_SEC", "300")
    env["CLAUDE_MAX_PARTIAL_OUTPUT_BYTES"] = os.environ.get("CURRENT_FIND_CLAUDE_MAX_PARTIAL_OUTPUT_BYTES", "8000000")
    env["CLAUDE_MAX_STDOUT_CHUNKS_PER_TICK"] = os.environ.get("CURRENT_FIND_CLAUDE_MAX_STDOUT_CHUNKS_PER_TICK", "64")
    result_path = paths.state / "current_find_claude_takeover_result.json"
    stdout_path = paths.state / f"current_find_claude_takeover_attempt{attempt}_stdout.log"
    stream = _run_claude_session_streaming(
        cmd,
        cwd=ROOT,
        env=env,
        paths=paths,
        result_path=result_path,
        stdout_path=stdout_path,
        prompt_path=prompt_path,
        run_id=run_id,
        attempt=attempt,
        stage="current-find-claude-read-idea-plan",
        timeout_sec=_claude_takeover_timeout(),
        started=started,
    )
    session_result_path = paths.state / f"claude_project_session_last_result_{session_key}.json"
    session_result = load_json(session_result_path, {})
    session_status = str(session_result.get("status") or "").strip() if isinstance(session_result, dict) else ""
    tool_policy_guard = session_result.get("tool_policy_guard") if isinstance(session_result, dict) and isinstance(session_result.get("tool_policy_guard", {}), dict) else {}
    parse_failures = current_find_json_artifact_failures(paths)
    return_code = int(stream.get("return_code") or 0)
    stream_status = str(stream.get("status") or "").strip()
    status = stream_status if stream_status.startswith("timeout") else (session_status or stream_status or ("completed" if return_code == 0 else "failed"))
    restore_reason = ""
    if parse_failures:
        restore_reason = "artifact_parse_failed_after_claude_takeover"
    elif stream_status == "timeout_no_progress":
        restore_reason = "claude_takeover_no_progress_timeout"
    elif stream_status == "timeout":
        restore_reason = "claude_takeover_timeout"
    elif return_code != 0:
        restore_reason = "claude_takeover_nonzero_return"
    elif isinstance(tool_policy_guard, dict) and tool_policy_guard.get("status") == "blocked":
        restore_reason = "claude_takeover_tool_policy_blocked"
    artifact_transaction: dict[str, Any] = {
        "status": "kept",
        "snapshot_path": str(Path(str(snapshot.get("backup_dir") or "")) / "manifest.json") if snapshot.get("backup_dir") else "",
        "parse_failures": parse_failures,
    }
    if restore_reason:
        artifact_transaction = restore_current_find_artifact_snapshot(paths, snapshot, restore_reason, parse_failures)
        artifact_transaction["post_restore_parse_failures"] = current_find_json_artifact_failures(paths)
        save_json(paths.state / "current_find_artifact_transaction_restore.json", artifact_transaction)
        if parse_failures and return_code == 0:
            status = "artifact_parse_failed_restored"
    stdout_tail = str(stream.get("stdout_tail") or "")
    if isinstance(session_result, dict) and session_result.get("stdout"):
        stdout_tail = (stdout_tail + "\n" + str(session_result.get("stdout") or ""))[-8000:]
    result = {
        "status": status,
        "return_code": return_code,
        "stop_reason": str(stream.get("stop_reason") or ""),
        "started_at": started,
        "finished_at": now_iso(),
        "prompt_path": str(prompt_path),
        "repair_attempt": attempt,
        "stdout_tail": stdout_tail[-8000:],
        "stdout_path": str(stream.get("stdout_path") or ""),
        "stdout_chcount": int(stream.get("stdout_chcount") or 0),
        "stderr_tail": str(stream.get("stderr_tail") or "")[-4000:],
        "claude_session_result_path": str(session_result_path),
        "claude_session_status": session_status,
        "claude_session_id": str(session_result.get("session_id") or "") if isinstance(session_result, dict) else "",
        "tool_policy_guard": tool_policy_guard,
        "artifact_transaction": artifact_transaction,
    }
    save_json(result_path, result)
    return result


def write_claude_selection_prompt(paths, project: str, run_id: str, observed: dict[str, Any] | None = None, attempt: int = 1, idea_count: int = 5) -> Path:
    prompt_path = paths.state / ("current_find_claude_selection_prompt.md" if attempt <= 1 else f"current_find_claude_selection_prompt_attempt{attempt}.md")
    observed_payload = observed if isinstance(observed, dict) else {}
    idea_plan_count = max(1, _positive_int(idea_count) or 5)
    prompt = f"""
你是 项目 `{project}` 的主控 Claude Code。当前 Find 的全文精读、{idea_plan_count} 个 idea 和 {idea_plan_count} 个 plan 已经通过内容合同，但没有唯一执行计划。

本阶段是 selection-only，不是重新精读、不是重新生成 idea、不是重新生成 plan 正文。

必须先读取：
- `planning/finding/read_results.json` 和 `planning/finding/read.md`：完整精读证据。
- `planning/finding/ideas.json` 和 `planning/finding/idea.md`：{idea_plan_count} 个候选 idea，三段字段为 `new_method`、`initial_experiment`、`inspired_by`。
- `planning/finding/plans.json` 和 `planning/finding/plan.md`：{idea_plan_count} 个候选 plan。
- `state/current_find_claude_reading_validation.json`、`state/current_find_research_plan.json`、`state/experiment_plan.json` 只读，用于确认阻塞原因。

机器观测到的合同状态：
```json
{json.dumps(_compact_validation_for_prompt(observed_payload), ensure_ascii=False, indent=2)}
```

你必须完成的唯一写入：
- 只能用 Claude Code 的 Write 工具完整重写 `planning/finding/plans.json`。
- 不准写 `read_results.json`、`ideas.json`、`read.md`、`idea.md`、`plan.md`、`current_find_deep_read_fragments/` 或任何 `state/*.json`。
- 不准用 Bash/Python/cat/heredoc/json.dump/open(..., 'w') 生成或修补 current-Find artifact；Bash 只能做只读检查，例如 `rg`、`sed -n`、`head`、`tail`、`wc`、`python -m json.tool planning/finding/plans.json >/dev/null`。禁止 `python -c`、`python <<`、`open(...)` 或 `Path(...).read_text()` 触碰 current-Find artifact；内容查看必须用 Claude Read。
- 不准启动 Find、full-cycle、环境、训练、实验、论文写作或任何后台任务。

选择合同：
1. 基于完整精读结果，比较 {idea_plan_count} 个 idea/plan 的新方法价值、初步实验可执行性、受哪些论文启发、与当前 repo/data/env gate 的兼容性、失败边界和最小可测性。
2. 只选择一个最佳 plan 作为后续环境/实验入口。唯一选中的 plan 必须包含：
   - `selected_for_execution: true`
   - `execute_next: true`
   - `execution_selection.selected: true`
   - `execution_selection.selected_by: "main_claude_code_after_deep_read"`
   - `execution_selection.reason`: 非空中文说明，必须引用 read/idea/plan 证据说明为什么选择它。
3. 其它所有 plan 必须包含：
   - `selected_for_execution: false`
   - `execute_next: false`
   - `execution_selection.selected: false`
   - `execution_selection.selected_by: "not_selected_candidate_backlog"`
   - `execution_selection.reason`: 非空中文说明，解释为什么暂列 backlog。
4. 不要更改 plan 的核心科学内容、步骤、方法正文、实验正文；只补齐/修正选择相关字段。
5. 写完后只做 JSON 只读校验。最终回复只报告 selected_plan_id、selected_idea_id 和选择理由摘要。
""".strip() + "\n"
    prompt_path.write_text(prompt, encoding="utf-8")
    return prompt_path


def run_claude_current_find_selection(project: str, paths, run_id: str, observed: dict[str, Any] | None = None, attempt: int = 1, idea_count: int = 5) -> dict[str, Any]:
    prompt_path = write_claude_selection_prompt(paths, project, run_id, observed=observed, attempt=attempt, idea_count=idea_count)
    snapshot = snapshot_current_find_artifacts(paths, run_id, 200 + attempt)
    session_key = "current_find_select_plan"
    cmd = [
        sys.executable,
        str(ROOT / "framework" / "scripts" / "claude_project_session.py"),
        "--project",
        project,
        "--stage",
        "current-find-claude-select-plan",
        "--message-file",
        str(prompt_path),
        "--timeout-sec",
        str(_claude_takeover_timeout()),
        "--agent-id",
        session_key,
        "--no-resume",
    ]
    started = now_iso()
    env = __import__("os").environ.copy()
    env["USE_EXISTING_LITERATURE_PACKET"] = env.get("USE_EXISTING_LITERATURE_PACKET", "1")
    env["CLAUDE_FIRST_OUTPUT_TIMEOUT_SEC"] = os.environ.get("CURRENT_FIND_CLAUDE_FIRST_OUTPUT_TIMEOUT_SEC", "180")
    env["CLAUDE_NO_EVENT_TIMEOUT_SEC"] = os.environ.get("CURRENT_FIND_CLAUDE_NO_EVENT_TIMEOUT_SEC", "300")
    env["CLAUDE_CURRENT_FIND_NO_EVENT_FLOOR_SEC"] = os.environ.get("CURRENT_FIND_CLAUDE_NO_EVENT_FLOOR_SEC", "300")
    env["CLAUDE_MAX_PARTIAL_OUTPUT_BYTES"] = os.environ.get("CURRENT_FIND_CLAUDE_MAX_PARTIAL_OUTPUT_BYTES", "4000000")
    env["CLAUDE_MAX_STDOUT_CHUNKS_PER_TICK"] = os.environ.get("CURRENT_FIND_CLAUDE_MAX_STDOUT_CHUNKS_PER_TICK", "64")
    proc = subprocess.run(cmd, cwd=ROOT, env=env, text=True, capture_output=True, timeout=_claude_takeover_timeout() + 180)
    session_result_path = paths.state / f"claude_project_session_last_result_{session_key}.json"
    session_result = load_json(session_result_path, {})
    session_status = str(session_result.get("status") or "").strip() if isinstance(session_result, dict) else ""
    tool_policy_guard = session_result.get("tool_policy_guard") if isinstance(session_result, dict) and isinstance(session_result.get("tool_policy_guard"), dict) else {}
    parse_failures = current_find_json_artifact_failures(paths)
    status = session_status or ("completed" if proc.returncode == 0 else "failed")
    restore_reason = ""
    if parse_failures:
        restore_reason = "artifact_parse_failed_after_claude_selection"
    elif proc.returncode != 0:
        restore_reason = "claude_selection_nonzero_return"
    elif isinstance(tool_policy_guard, dict) and tool_policy_guard.get("status") == "blocked":
        restore_reason = "claude_selection_tool_policy_blocked"
    artifact_transaction: dict[str, Any] = {
        "status": "kept",
        "snapshot_path": str(Path(str(snapshot.get("backup_dir") or "")) / "manifest.json") if snapshot.get("backup_dir") else "",
        "parse_failures": parse_failures,
        "policy": "selection-only may only update plans.json selection fields; wrapper validates exactly one selected plan before syncing state.",
    }
    if restore_reason:
        artifact_transaction = restore_current_find_artifact_snapshot(paths, snapshot, restore_reason, parse_failures)
        artifact_transaction["post_restore_parse_failures"] = current_find_json_artifact_failures(paths)
        save_json(paths.state / "current_find_selection_artifact_transaction_restore.json", artifact_transaction)
        if parse_failures and proc.returncode == 0:
            status = "artifact_parse_failed_restored"
    result = {
        "status": status,
        "stage": "current-find-claude-select-plan",
        "selection_only": True,
        "return_code": proc.returncode,
        "started_at": started,
        "finished_at": now_iso(),
        "prompt_path": str(prompt_path),
        "repair_attempt": attempt,
        "stdout_tail": ((str(proc.stdout or "") + "\n" + str(session_result.get("stdout") or ""))[-8000:] if isinstance(session_result, dict) else str(proc.stdout or "")[-8000:]),
        "stderr_tail": proc.stderr[-4000:],
        "claude_session_result_path": str(session_result_path),
        "claude_session_status": session_status,
        "claude_session_id": str(session_result.get("session_id") or "") if isinstance(session_result, dict) else "",
        "tool_policy_guard": tool_policy_guard,
        "artifact_transaction": artifact_transaction,
    }
    save_json(paths.state / "current_find_claude_selection_result.json", result)
    return result


def sync_current_find_selection_success_receipt(
    paths,
    run_id: str,
    takeover: dict[str, Any],
    ideas: list[dict[str, Any]],
    plans: list[dict[str, Any]],
    validation: dict[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    selection_fields = current_find_selection_fields(ideas, plans, source=CLAUDE_TAKEOVER_SOURCE, executable=True)
    selected_plan_id = str(selection_fields.get("selected_plan_id") or "").strip()
    selected_idea_id = str(selection_fields.get("selected_idea_id") or "").strip()
    issue = current_find_selected_execution_issue(ideas, plans)
    return_code_raw = (takeover if isinstance(takeover, dict) else {}).get("return_code")
    try:
        return_code = int(return_code_raw or 0)
    except (TypeError, ValueError):
        return_code = 0
    receipt = {
        **(takeover if isinstance(takeover, dict) else {}),
        "status": "already_current_valid_claude_selection" if reason == "valid_artifacts_ready" else "completed_valid_claude_selection",
        "stage": str((takeover if isinstance(takeover, dict) else {}).get("stage") or "current-find-claude-select-plan"),
        "selection_only": True,
        "return_code": return_code,
        "run_id": run_id,
        "contract_validation_valid": True,
        "contract_failure": None,
        "reading_validation": validation if isinstance(validation, dict) else {},
        "selected_execution": selection_fields,
        "selected_plan_id": selected_plan_id,
        "selected_idea_id": selected_idea_id,
        "validated_at": now_iso(),
        "sync_reason": reason,
        "policy": "Current-Find selection receipt is valid only when TASTE validation sees exactly one explicit selected_plan_id from Claude or human supervision; downstream stages consume only that selected_plan_id.",
    }
    if not selected_plan_id or not selected_idea_id or issue:
        receipt["contract_validation_valid"] = False
        receipt["contract_failure"] = {
            "status": "failed_contract_validation",
            "failure_type": issue or "missing_selected_plan",
            "next_required_action": "rerun_current_find_claude_takeover_select_single_best_plan",
        }
    save_json(paths.state / "current_find_claude_selection_result.json", receipt)
    return receipt


def current_find_selection_artifacts_follow_takeover(takeover: Any, taste_dir: Path) -> bool:
    if not isinstance(takeover, dict) or takeover.get("return_code") != 0 or not str(takeover.get("prompt_path") or "").strip():
        return False
    started = parse_iso_time(takeover.get("started_at")) or parse_iso_time(takeover.get("finished_at"))
    if started is None:
        return False
    plan_time = _artifact_generated_or_modified_at(Path(taste_dir) / "plans.json")
    return bool(plan_time is not None and plan_time + dt.timedelta(seconds=2) >= started)

def _refresh_current_find_claude_outputs(paths, taste_dir: Path, run_id: str, find_results: dict[str, Any], effective_read_limit: int, find_revision: dt.datetime | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[str], dict[str, Any]]:
    readings, ideas, plans = load_claude_outputs(taste_dir, run_id, find_results, effective_read_limit, paths.state, find_revision, paths, write_pending_validation=False)
    full_text_packet = load_full_text_packet_from_taste_dir(taste_dir, run_id)
    original_recommendation_rows, reading_rows_for_validation, validation_find_results = _current_reading_validation_view(find_results, full_text_packet, effective_read_limit)
    positive_ids, _positive_titles = _current_positive_identities(validation_find_results)
    readings = _enforce_current_find_claim_policy(readings, positive_ids)
    read_payload = load_json(taste_dir / "read_results.json", {})
    idea_payload = load_json(taste_dir / "ideas.json", {})
    plan_payload = load_json(taste_dir / "plans.json", {})
    current_plan = load_json(paths.state / "current_find_research_plan.json", {})
    targeted_queries = extract_targeted_search_queries(
        paths,
        read_payload if isinstance(read_payload, dict) else {},
        idea_payload if isinstance(idea_payload, dict) else {},
        plan_payload if isinstance(plan_payload, dict) else {},
        current_plan if isinstance(current_plan, dict) else {},
    )
    valid, validation = validate_claude_readings_against_current_find(readings, validation_find_results, len(reading_rows_for_validation), paths, run_id)
    validation = {
        **validation,
        "run_id": run_id,
        "source": CLAUDE_TAKEOVER_SOURCE,
        "generated_at": now_iso(),
        **_current_reading_validation_metadata(original_recommendation_rows, reading_rows_for_validation, full_text_packet, effective_read_limit),
    }
    save_json(paths.state / "current_find_claude_reading_validation.json", validation)
    return readings, ideas, plans, targeted_queries, validation

def current_find_selected_execution_summary(ideas: list[dict[str, Any]], plans: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize the explicit current-Find execution selection without mutating artifacts."""
    idea_rows = [dict(row) for row in ideas if isinstance(row, dict)]
    plan_rows = [dict(row) for row in plans if isinstance(row, dict)]
    return apply_current_find_execution_selection(
        idea_rows,
        plan_rows,
        source=CLAUDE_TAKEOVER_SOURCE,
        executable=False,
    )


def current_find_selected_execution_issue(ideas: list[dict[str, Any]], plans: list[dict[str, Any]]) -> str:
    plan_rows = [row for row in plans if isinstance(row, dict)]
    if not plan_rows:
        return "idea_plan_artifacts_incomplete"
    explicit_plans = [row for row in plan_rows if _current_find_explicit_execution_selection(row, kind="plan")]
    if not explicit_plans:
        return "missing_selected_plan"
    if len(explicit_plans) > 1:
        return "ambiguous_selected_plan"
    selected_plan_id = _current_find_plan_id(explicit_plans[0])
    if not selected_plan_id:
        return "selected_plan_id_missing"
    idea_id = _current_find_plan_idea_key(explicit_plans[0])
    idea_rows = [row for row in ideas if isinstance(row, dict)]
    if idea_id and not any(_current_find_idea_key(row) == idea_id for row in idea_rows):
        return "selected_plan_missing_matching_idea"
    return ""


def current_find_selected_execution_ready(ideas: list[dict[str, Any]], plans: list[dict[str, Any]]) -> bool:
    return current_find_selected_execution_issue(ideas, plans) == ""


def _current_find_content_ready_without_selection(readings: list[dict[str, Any]], ideas: list[dict[str, Any]], plans: list[dict[str, Any]], targeted_queries: list[str], validation: dict[str, Any], run_id: str, min_required_readings: int, idea_count: int, find_revision: dt.datetime | None) -> bool:
    return bool(
        len(readings) == min_required_readings
        and current_reading_validation_ready(validation, run_id, min_required_readings)
        and claude_output_payloads_are_current([validation], find_revision)
        and len(ideas) >= idea_count
        and _ideas_three_part_ready(ideas, idea_count)
        and len(plans) >= idea_count
        and _plans_contract_ready(plans, idea_count)
        and len(targeted_queries) >= 3
    )


def _current_find_contract_ready(readings: list[dict[str, Any]], ideas: list[dict[str, Any]], plans: list[dict[str, Any]], targeted_queries: list[str], validation: dict[str, Any], run_id: str, min_required_readings: int, idea_count: int, find_revision: dt.datetime | None) -> bool:
    return bool(
        _current_find_content_ready_without_selection(readings, ideas, plans, targeted_queries, validation, run_id, min_required_readings, idea_count, find_revision)
        and current_find_selected_execution_ready(ideas, plans)
    )


def current_find_takeover_repairable_failure(takeover: Any) -> bool:
    if not isinstance(takeover, dict):
        return False
    guard = takeover.get("tool_policy_guard") if isinstance(takeover.get("tool_policy_guard"), dict) else {}
    haystack = "\n".join(
        str(value or "")
        for value in [
            guard.get("policy_type"),
            guard.get("reason"),
            guard.get("policy"),
            takeover.get("status"),
            takeover.get("stdout_tail"),
        ]
    )
    return bool(
        guard.get("recoverable_by_current_find_repair") is True
        or guard.get("policy_type") in {"current_find_artifact_writer", "current_find_gate_state_writer"}
        or "current-Find Read/Idea/Plan artifacts" in haystack
        or "TASTE-owned current-Find gate/state files" in haystack
        or "Blocked Bash/Python generation of current-Find Read/Idea/Plan artifacts" in haystack
    )



def current_find_takeover_observed(
    taste_dir: Path,
    readings: list[dict[str, Any]],
    ideas: list[dict[str, Any]],
    plans: list[dict[str, Any]],
    targeted_queries: list[str],
    validation: dict[str, Any],
    idea_count: int,
) -> dict[str, Any]:
    read_payload = load_json(taste_dir / "read_results.json", {})
    idea_payload = load_json(taste_dir / "ideas.json", {})
    plan_payload = load_json(taste_dir / "plans.json", {})
    raw_readings = [row for row in as_list((read_payload if isinstance(read_payload, dict) else {}).get("readings")) if isinstance(row, dict)]
    raw_ideas = [row for row in as_list((idea_payload if isinstance(idea_payload, dict) else {}).get("ideas")) if isinstance(row, dict)]
    raw_plans = [row for row in as_list((plan_payload if isinstance(plan_payload, dict) else {}).get("plans")) if isinstance(row, dict)]
    validation = validation if isinstance(validation, dict) else {}
    selection = current_find_selected_execution_summary(ideas, plans)
    selection_issue = current_find_selected_execution_issue(ideas, plans) or str(selection.get("selection_issue") or "")
    idea_contract_issues = _idea_rows_contract_issues(ideas, idea_count)
    raw_idea_contract_issues = _idea_rows_contract_issues(raw_ideas, idea_count)
    plan_contract_issues = _plan_rows_contract_issues(plans, idea_count)
    raw_plan_contract_issues = _plan_rows_contract_issues(raw_plans, idea_count)
    content_ready_without_selection = bool(
        readings
        and validation.get("valid") is True
        and len(ideas) >= idea_count
        and not idea_contract_issues
        and len(plans) >= idea_count
        and not plan_contract_issues
        and len(targeted_queries) >= 3
    )
    return {
        "readings": len(readings),
        "ideas": len(ideas),
        "plans": len(plans),
        "contract_reading_count": len(readings),
        "contract_idea_count": len(ideas),
        "contract_plan_count": len(plans),
        "raw_artifact_reading_count": len(raw_readings),
        "raw_artifact_idea_count": len(raw_ideas),
        "raw_artifact_plan_count": len(raw_plans),
        "validation_actual_reading_count": _positive_int(validation.get("actual_reading_count")),
        "validation_full_text_reading_count": _positive_int(validation.get("full_text_reading_count")),
        "validation_pending_full_text_reading_count": _positive_int(validation.get("pending_full_text_reading_count")),
        "validation_pending_without_evidence_count": _positive_int(validation.get("pending_without_evidence_count")),
        "idea_schema_ready": len(ideas) >= idea_count and not idea_contract_issues,
        "raw_idea_schema_ready": len(raw_ideas) >= idea_count and not raw_idea_contract_issues,
        "plan_schema_ready": len(plans) >= idea_count and not plan_contract_issues,
        "raw_plan_schema_ready": len(raw_plans) >= idea_count and not raw_plan_contract_issues,
        "idea_contract_issues": idea_contract_issues[:20],
        "raw_idea_contract_issues": raw_idea_contract_issues[:20],
        "plan_contract_issues": plan_contract_issues[:20],
        "raw_plan_contract_issues": raw_plan_contract_issues[:20],
        "targeted_search_queries": len(targeted_queries),
        "selected_plan_id": str(selection.get("selected_plan_id") or ""),
        "selected_idea_id": str(selection.get("selected_idea_id") or ""),
        "selected_execution_issue": selection_issue,
        "selected_execution_status": (selection.get("execution_policy") or {}).get("status", "") if isinstance(selection.get("execution_policy"), dict) else "",
        "content_ready_without_selection": content_ready_without_selection,
        "read_idea_plan_ready_without_selection": content_ready_without_selection,
    }


def _artifact_generated_or_modified_at(path: Path) -> dt.datetime | None:
    payload = load_json(path, {})
    candidates: list[dt.datetime] = []
    generated = parse_iso_time(payload.get("generated_at")) if isinstance(payload, dict) else None
    if generated is not None:
        candidates.append(generated)
    try:
        candidates.append(dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc))
    except OSError:
        pass
    return max(candidates) if candidates else None


def current_find_artifacts_follow_takeover(takeover: Any, taste_dir: Path, validation: Any) -> bool:
    if not isinstance(takeover, dict) or not str(takeover.get("prompt_path") or "").strip():
        return False
    started = parse_iso_time(takeover.get("started_at")) or parse_iso_time(takeover.get("finished_at"))
    if started is None:
        return False
    threshold = started - dt.timedelta(seconds=2)
    for artifact in ["read_results.json", "ideas.json", "plans.json"]:
        artifact_time = _artifact_generated_or_modified_at(Path(taste_dir) / artifact)
        if artifact_time is None or artifact_time < threshold:
            return False
    if isinstance(validation, dict):
        validation_time = parse_iso_time(validation.get("generated_at"))
        if validation_time is not None and validation_time < threshold:
            return False
    return True


def _current_find_observed_with_takeover(observed: dict[str, Any], takeover: Any, current_revision: dt.datetime | None, taste_dir: Path | None = None, validation: Any = None) -> dict[str, Any]:
    out = dict(observed) if isinstance(observed, dict) else {}
    takeover_dict = takeover if isinstance(takeover, dict) else {}
    guard = takeover_dict.get("tool_policy_guard") if isinstance(takeover_dict.get("tool_policy_guard"), dict) else {}
    out.update({
        "takeover_process_current": claude_takeover_is_current(takeover_dict, current_revision),
        "takeover_artifacts_current": current_find_artifacts_follow_takeover(takeover_dict, taste_dir, validation) if taste_dir is not None else None,
        "takeover_status": str(takeover_dict.get("status") or ""),
        "takeover_return_code": takeover_dict.get("return_code"),
        "takeover_prompt_path_present": bool(str(takeover_dict.get("prompt_path") or "").strip()),
        "takeover_current_find_revision_at": current_revision.isoformat() if current_revision is not None else "",
        "takeover_tool_policy_type": str(guard.get("policy_type") or ""),
        "takeover_tool_policy_recoverable": bool(guard.get("recoverable_by_current_find_repair")),
        "takeover_tool_policy_reason": compact(guard.get("reason"), 500) if guard else "",
    })
    return out


def _sync_current_find_plan_reading_validation(payload: dict[str, Any], validation: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(validation, dict) or not validation:
        return payload
    payload["reading_validation"] = validation
    observed = payload.get("observed")
    if isinstance(observed, dict):
        observed = dict(observed)
        observed["reading_validation"] = validation
        observed["validation_actual_reading_count"] = _positive_int(validation.get("actual_reading_count"))
        observed["validation_full_text_reading_count"] = _positive_int(validation.get("full_text_reading_count"))
        observed["validation_pending_full_text_reading_count"] = _positive_int(validation.get("pending_full_text_reading_count"))
        observed["validation_pending_without_evidence_count"] = _positive_int(validation.get("pending_without_evidence_count"))
        payload["observed"] = observed
    return payload


def current_find_contract_failure_type(validation: Any, observed: Any, idea_count: int = 5) -> str:
    required_idea_count = max(1, _positive_int(idea_count) or 5)
    if isinstance(validation, dict) and (validation.get("status") == "artifact_parse_failed" or validation.get("artifact_parse_failures")):
        return "artifact_parse_failed"
    if isinstance(observed, dict):
        policy_type = str(observed.get("takeover_tool_policy_type") or "").strip()
        policy_recoverable = bool(observed.get("takeover_tool_policy_recoverable"))
        if policy_type == "current_find_artifact_writer":
            return "recoverable_current_find_tool_policy_blocked" if policy_recoverable else "current_find_artifact_write_policy_blocked"
        if policy_type == "current_find_gate_state_writer":
            return "recoverable_current_find_tool_policy_blocked" if policy_recoverable else "current_find_gate_state_write_policy_blocked"
        takeover_return_code = observed.get("takeover_return_code")
        takeover_status = str(observed.get("takeover_status") or "").strip()
        if takeover_return_code is not None and _positive_int(takeover_return_code) != 0:
            return "claude_current_find_takeover_failed"
        if takeover_status in {"blocked_tool_policy", "failed_tool_policy"}:
            return "claude_current_find_takeover_failed"
    if current_reading_validation_needs_full_text_evidence(validation):
        return "full_text_evidence_missing"
    if isinstance(observed, dict):
        selection_issue = str(observed.get("selected_execution_issue") or "").strip()
        if selection_issue in CURRENT_FIND_SELECTION_FAILURE_TYPES and observed.get("content_ready_without_selection"):
            return selection_issue
    if current_reading_validation_needs_claude_rewrite(validation):
        return "claude_deep_read_rewrite_required"
    if isinstance(observed, dict) and (observed.get("idea_contract_issues") or observed.get("raw_idea_contract_issues")) and (
        _positive_int(observed.get("raw_artifact_idea_count")) > 0 or _positive_int(observed.get("contract_idea_count")) > 0 or _positive_int(observed.get("ideas")) > 0
    ):
        return "idea_contract_failed"
    if isinstance(observed, dict) and (observed.get("plan_contract_issues") or observed.get("raw_plan_contract_issues")) and (
        _positive_int(observed.get("raw_artifact_plan_count")) > 0 or _positive_int(observed.get("contract_plan_count")) > 0 or _positive_int(observed.get("plans")) > 0
    ):
        return "plan_contract_failed"
    if isinstance(observed, dict):
        if observed.get("takeover_process_current") is False or observed.get("takeover_artifacts_current") is False:
            return "stale_or_missing_current_find_takeover"
    if isinstance(observed, dict):
        selection_issue = str(observed.get("selected_execution_issue") or "").strip()
        if selection_issue in CURRENT_FIND_SELECTION_FAILURE_TYPES:
            return selection_issue
        if _positive_int(observed.get("raw_artifact_reading_count")) == 0:
            return "claude_artifacts_missing"
        if _positive_int(observed.get("raw_artifact_idea_count")) < required_idea_count or _positive_int(observed.get("raw_artifact_plan_count")) < required_idea_count:
            return "idea_plan_artifacts_incomplete"
        if selection_issue:
            return selection_issue
    return "contract_validation_failed"


def current_find_contract_next_required_action(validation: Any, observed: Any, idea_count: int = 5) -> str:
    failure_type = current_find_contract_failure_type(validation, observed, idea_count=idea_count)
    if failure_type == "full_text_evidence_missing":
        return "acquire_current_find_full_text_evidence"
    if failure_type == "stale_or_missing_current_find_takeover":
        return "rerun_current_find_claude_takeover_for_current_revision"
    if failure_type == "claude_current_find_takeover_failed":
        return "rerun_current_find_claude_takeover_after_process_failure"
    if failure_type == "artifact_parse_failed":
        return "rerun_current_find_claude_takeover_repair_rewrite_parseable_artifacts"
    if failure_type == "recoverable_current_find_tool_policy_blocked":
        return "rerun_current_find_claude_takeover_after_tool_policy_repair"
    if failure_type == "current_find_artifact_write_policy_blocked":
        return "rerun_current_find_claude_takeover_with_complete_write_artifacts"
    if failure_type == "current_find_gate_state_write_policy_blocked":
        return "rerun_current_find_claude_takeover_without_state_writes"
    if failure_type == "claude_deep_read_rewrite_required":
        return "rerun_current_find_claude_takeover_repair_deep_read_synthesis"
    if failure_type == "idea_contract_failed":
        return "rerun_current_find_claude_takeover_rewrite_and_score_ideas"
    if failure_type == "plan_contract_failed":
        return "rerun_current_find_claude_takeover_rewrite_plans_without_preselected_base"
    if failure_type in CURRENT_FIND_SELECTION_FAILURE_TYPES:
        return "rerun_current_find_claude_takeover_select_single_best_plan"
    return "rerun_current_find_claude_takeover_repair"


def record_claude_takeover_contract_result(paths, run_id: str, takeover: dict[str, Any], valid: bool, validation: dict[str, Any], observed: dict[str, Any], attempt: int, idea_count: int = 5) -> dict[str, Any]:
    failure = None
    if not valid:
        failure_type = current_find_contract_failure_type(validation, observed, idea_count=idea_count)
        next_required_action = current_find_contract_next_required_action(validation, observed, idea_count=idea_count)
        failure = {
            "status": "failed_contract_validation",
            "failure_type": failure_type,
            "run_id": run_id,
            "generated_at": now_iso(),
            "repair_attempt": attempt,
            "validation": _compact_validation_for_prompt(validation),
            "observed": observed,
            "next_required_action": next_required_action,
            "takeover": {
                "status": takeover.get("status"),
                "return_code": takeover.get("return_code"),
                "repair_attempt": takeover.get("repair_attempt"),
                "tool_policy_guard": takeover.get("tool_policy_guard") if isinstance(takeover.get("tool_policy_guard"), dict) else {},
            },
            "policy": f"Claude process return_code=0 is not sufficient. Current-Find Read/Idea/Plan is complete only after full-text evidence, non-placeholder deep-read synthesis, {max(1, _positive_int(idea_count) or 5)} three-part ideas, {max(1, _positive_int(idea_count) or 5)} plans, exactly one explicit selected_plan_id, and targeted search topics pass TASTE validation. If full-text evidence is missing, The workflow must acquire or prove the missing PDF/HTML/page source before rerunning Claude; recoverable artifact writer, deep-read synthesis, or selected-plan failures may be rerun as repair prompts.",
        }
        save_json(paths.state / "current_find_claude_takeover_contract_failure.json", failure)
    updated = {**takeover, "contract_validation_valid": bool(valid), "repair_attempt": attempt}
    if failure is not None:
        updated["contract_failure"] = {
            "status": failure["status"],
            "failure_type": failure["failure_type"],
            "next_required_action": failure["next_required_action"],
            "path": str(paths.state / "current_find_claude_takeover_contract_failure.json"),
            "blockers": validation.get("blockers", []),
            "observed": observed,
        }
    else:
        updated.pop("contract_failure", None)
    save_json(paths.state / "current_find_claude_takeover_result.json", updated)
    return updated


def maybe_repair_current_find_takeover(project: str, paths, taste_dir: Path, run_id: str, find_results: dict[str, Any], takeover: dict[str, Any], readings: list[dict[str, Any]], ideas: list[dict[str, Any]], plans: list[dict[str, Any]], targeted_queries: list[str], validation: dict[str, Any], effective_read_limit: int, min_required_readings: int, idea_count: int, find_revision: dt.datetime | None, selection_only_requested: bool = False) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[str], dict[str, Any], str]:
    if not isinstance(takeover, dict):
        takeover = {"status": "missing_current_find_takeover", "return_code": 0, "prompt_path": ""}
    takeover_current = claude_takeover_is_current(takeover, find_revision)
    artifacts_current = current_find_artifacts_follow_takeover(takeover, taste_dir, validation)
    contract_ready = _current_find_contract_ready(readings, ideas, plans, targeted_queries, validation, run_id, min_required_readings, idea_count, find_revision)
    ready = bool(takeover_current and contract_ready)
    observed = _current_find_observed_with_takeover(
        current_find_takeover_observed(taste_dir, readings, ideas, plans, targeted_queries, validation, idea_count),
        takeover,
        find_revision,
        taste_dir,
        validation,
    )
    takeover = record_claude_takeover_contract_result(paths, run_id, takeover, ready, validation, observed, int(takeover.get("repair_attempt") or 1), idea_count=idea_count)
    next_required_action = current_find_contract_next_required_action(validation, observed, idea_count=idea_count)
    failure_type = current_find_contract_failure_type(validation, observed, idea_count=idea_count)
    content_ready_without_selection = _current_find_content_ready_without_selection(readings, ideas, plans, targeted_queries, validation, run_id, min_required_readings, idea_count, find_revision)
    if ready or next_required_action == "acquire_current_find_full_text_evidence":
        return takeover, readings, ideas, plans, targeted_queries, validation, ""
    current_attempt = max(1, _positive_int(takeover.get("repair_attempt")) or 1)
    if failure_type in CURRENT_FIND_SELECTION_FAILURE_TYPES and content_ready_without_selection:
        failure = load_json(paths.state / "current_find_claude_takeover_contract_failure.json", {})
        selection_takeover = run_claude_current_find_selection(project, paths, run_id, observed=failure if isinstance(failure, dict) and failure else observed, attempt=current_attempt + 1, idea_count=idea_count)
        changed_run = _find_run_changed(paths, run_id)
        if changed_run:
            return selection_takeover, readings, ideas, plans, targeted_queries, validation, changed_run
        readings, ideas, plans, targeted_queries, validation = _refresh_current_find_claude_outputs(paths, taste_dir, run_id, find_results, effective_read_limit, find_revision)
        selection_artifacts_current = current_find_selection_artifacts_follow_takeover(selection_takeover, taste_dir)
        ready = bool(selection_artifacts_current and _current_find_contract_ready(readings, ideas, plans, targeted_queries, validation, run_id, min_required_readings, idea_count, find_revision))
        observed = _current_find_observed_with_takeover(
            current_find_takeover_observed(taste_dir, readings, ideas, plans, targeted_queries, validation, idea_count),
            selection_takeover,
            find_revision,
            taste_dir,
            validation,
        )
        observed["takeover_artifacts_current"] = selection_artifacts_current
        observed["selection_only_artifacts_current"] = selection_artifacts_current
        selection_takeover = record_claude_takeover_contract_result(paths, run_id, selection_takeover, ready, validation, observed, current_attempt + 1)
        save_json(paths.state / "current_find_claude_selection_result.json", selection_takeover)
        return selection_takeover, readings, ideas, plans, targeted_queries, validation, ""
    if selection_only_requested:
        return takeover, readings, ideas, plans, targeted_queries, validation, ""
    repairable_failure = current_find_takeover_repairable_failure(takeover)
    if takeover.get("return_code") != 0 and not repairable_failure:
        return takeover, readings, ideas, plans, targeted_queries, validation, ""
    if repairable_failure and current_attempt >= CURRENT_FIND_MAX_TAKEOVER_REPAIR_ATTEMPTS:
        return takeover, readings, ideas, plans, targeted_queries, validation, ""
    failure = load_json(paths.state / "current_find_claude_takeover_contract_failure.json", {})
    repair_takeover = run_claude_current_find_takeover(project, paths, run_id, effective_read_limit, idea_count, repair_validation=failure if isinstance(failure, dict) else validation, attempt=current_attempt + 1)
    changed_run = _find_run_changed(paths, run_id)
    if changed_run:
        return repair_takeover, readings, ideas, plans, targeted_queries, validation, changed_run
    # A repair attempt start time is not a scientific-content revision boundary.
    # Same-run Claude artifacts that are current to the Find revision must remain
    # eligible; otherwise a no-op repair can erase valid ideas/plans as stale.
    readings, ideas, plans, targeted_queries, validation = _refresh_current_find_claude_outputs(paths, taste_dir, run_id, find_results, effective_read_limit, find_revision)
    repair_current = claude_takeover_is_current(repair_takeover, find_revision)
    repair_artifacts_current = current_find_artifacts_follow_takeover(repair_takeover, taste_dir, validation)
    repair_contract_ready = _current_find_contract_ready(readings, ideas, plans, targeted_queries, validation, run_id, min_required_readings, idea_count, find_revision)
    ready = bool(repair_current and repair_contract_ready)
    observed = _current_find_observed_with_takeover(
        current_find_takeover_observed(taste_dir, readings, ideas, plans, targeted_queries, validation, idea_count),
        repair_takeover,
        find_revision,
        taste_dir,
        validation,
    )
    next_attempt = current_attempt + 1
    repair_takeover = record_claude_takeover_contract_result(paths, run_id, repair_takeover, ready, validation, observed, next_attempt)
    if (
        not ready
        and next_attempt < CURRENT_FIND_MAX_TAKEOVER_REPAIR_ATTEMPTS
        and current_find_takeover_repairable_failure(repair_takeover)
    ):
        return maybe_repair_current_find_takeover(
            project,
            paths,
            taste_dir,
            run_id,
            find_results,
            repair_takeover,
            readings,
            ideas,
            plans,
            targeted_queries,
            validation,
            effective_read_limit,
            min_required_readings,
            idea_count,
            find_revision,
            selection_only_requested=selection_only_requested,
        )
    return repair_takeover, readings, ideas, plans, targeted_queries, validation, ""


CURRENT_FIND_MAX_TAKEOVER_REPAIR_ATTEMPTS = 3

CLAUDE_TAKEOVER_SOURCE = "claude_code_current_find_takeover"
CURRENT_FIND_READ_ARTIFACT_SOURCES = {CLAUDE_TAKEOVER_SOURCE, LLM_CURRENT_FIND_FALLBACK_SOURCE, "reading_recommended_articles", "taste_reading", "current_find_bridge", "current_find_bridge_compatibility_only"}
CURRENT_FIND_IDEA_ARTIFACT_SOURCES = {CLAUDE_TAKEOVER_SOURCE, LLM_CURRENT_FIND_FALLBACK_SOURCE, "taste_ideation", "current_find_bridge", "current_find_bridge_compatibility_only"}
CURRENT_FIND_PLAN_ARTIFACT_SOURCES = {CLAUDE_TAKEOVER_SOURCE, LLM_CURRENT_FIND_FALLBACK_SOURCE, "taste_planning", "current_find_bridge", "current_find_bridge_compatibility_only"}


def _current_find_execution_contract(paths: Any) -> dict[str, Any]:
    try:
        from run_project import current_find_execution_contract as build_contract
    except Exception:
        return {}
    try:
        contract = build_contract(paths)
    except Exception:
        return {}
    return contract if isinstance(contract, dict) else {}


def _contract_selected_for_run(contract: Any, run_id: str) -> bool:
    if not isinstance(contract, dict):
        return False
    contract_run = str(contract.get("run_id") or "").strip()
    if run_id and contract_run and contract_run != str(run_id).strip():
        return False
    return bool(str(contract.get("selected_plan_id") or "").strip() and not str(contract.get("selection_issue") or "").strip())


def _current_payload_source_allowed(payload: Any, allowed_sources: set[str]) -> bool:
    if not isinstance(payload, dict):
        return False
    source = str(payload.get("source") or "").strip()
    return not source or source in allowed_sources


def _current_payload_is_current(payload: Any, path: Path, run_id: str, allowed_sources: set[str], current_revision: dt.datetime | None) -> bool:
    return bool(
        isinstance(payload, dict)
        and str(payload.get("run_id") or payload.get("current_find_run_id") or "").strip() == str(run_id or "").strip()
        and _current_payload_source_allowed(payload, allowed_sources)
        and claude_output_payloads_or_files_are_current([payload], [path], current_revision)
    )


def _state_source_for_current_artifacts(read_payload: Any, idea_payload: Any, plan_payload: Any) -> str:
    sources = [
        str((plan_payload if isinstance(plan_payload, dict) else {}).get("source") or "").strip(),
        str((idea_payload if isinstance(idea_payload, dict) else {}).get("source") or "").strip(),
        str((read_payload if isinstance(read_payload, dict) else {}).get("source") or "").strip(),
    ]
    if any(source == LLM_CURRENT_FIND_FALLBACK_SOURCE for source in sources):
        return LLM_CURRENT_FIND_FALLBACK_SOURCE
    if any(source and source != CLAUDE_TAKEOVER_SOURCE for source in sources):
        return "current_find_execution_contract"
    return CLAUDE_TAKEOVER_SOURCE


def _apply_contract_selection_fields(selection_fields: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    if not _contract_selected_for_run(contract, str(contract.get("run_id") or "")):
        return selection_fields
    merged = dict(selection_fields)
    for key in CURRENT_FIND_SELECTION_FIELD_KEYS:
        value = contract.get(key)
        if value not in (None, "", [], {}):
            merged[key] = value
    return merged


def _reading_visible_blob(row: dict[str, Any]) -> str:
    keys = ["summary", "abstract_zh", "motivation_zh", "problem", "method", "method_details_zh", "experiments", "experiments_zh", "limitations", "limitations_zh", "relevance", "critique_reason"]
    return " ".join(str(row.get(key) or "") for key in keys)


def _valid_claude_reading(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    title = str(row.get("title") or row.get("paper_title") or "").strip()
    if not title:
        return False
    has_identity = bool(str(row.get("paper_id") or row.get("id") or row.get("entry_id") or row.get("url") or row.get("pdf_url") or title).strip())
    has_reading_signal = any(row.get(key) for key in [
        "verdict",
        "support_role",
        "abstract_zh",
        "motivation_zh",
        "method_details_zh",
        "method",
        "experiments_zh",
        "experiments",
        "limitations_zh",
        "limitations",
        "summary",
    ])
    if not (has_identity and has_reading_signal):
        return False
    visible = _reading_visible_blob(row)
    return not any(marker in visible for marker in READ_VISIBLE_BANNED_MARKERS)


def _idea_is_selected_for_execution(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    execution_selection = row.get("execution_selection") if isinstance(row.get("execution_selection"), dict) else {}
    return bool(row.get("selected_for_execution") is True or row.get("execute_next") is True or execution_selection.get("selected") is True)


def _idea_is_explicitly_blocked_candidate(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict) or _idea_is_selected_for_execution(row):
        return False
    status = str(row.get("status") or row.get("recommendation") or "").strip().lower()
    has_block_reason = bool(str(row.get("block_reason") or row.get("rejection_reason") or row.get("why_not_selected") or "").strip())
    return bool(has_block_reason or status in {"blocked", "blocked_with_reason", "rejected", "reject", "not_selected", "backlog"})


def _idea_contract_issues(row: dict[str, Any]) -> list[str]:
    if not isinstance(row, dict):
        return ["idea row is not an object"]
    issues: list[str] = []
    new_method = compact(row.get("new_method") or row.get("hypothesis"), 2200)
    initial_experiment = compact(
        row.get("initial_experiment")
        or row.get("experiment_design")
        or row.get("experimental_design")
        or row.get("min_experiment")
        or row.get("minimum_experiment"),
        2200,
    )
    inspired_by = _normalize_inspired_refs(row.get("inspired_by"), as_list(row.get("supporting_papers") or row.get("positive_anchor_papers")))
    if len(new_method) < 40:
        issues.append("new_method_missing_or_too_short")
    if len(initial_experiment) < 40:
        issues.append("initial_experiment_missing_or_too_short")
    if _generic_idea_experiment(initial_experiment):
        issues.append("initial_experiment_is_generic_environment_placeholder")
    if not inspired_by:
        issues.append("inspired_by_missing")
    if _contains_pre_environment_base_binding(_binding_view(row, PRE_ENV_IDEA_BINDING_KEYS)):
        issues.append("stale_or_preselected_base_binding_detected")
    scores = _idea_objective_scores(row)
    required_scores = set(IDEA_OBJECTIVE_SCORE_KEYS)
    if not required_scores.issubset(scores):
        issues.append("objective_scores_missing")
    elif any(scores[key] <= 0 for key in required_scores):
        issues.append("objective_scores_contain_zero_or_negative")
    elif scores["overall"] < IDEA_OBJECTIVE_SCORE_MIN_OVERALL and not _idea_is_explicitly_blocked_candidate(row):
        issues.append("objective_score_below_threshold")
    if _numeric_or_none(row.get("score")) is None or _numeric_or_none(row.get("idea_score")) is None:
        issues.append("score_or_idea_score_missing")
    if not _idea_score_audit_ready(row):
        issues.append("idea_score_audit_missing_or_not_subagent")
    return issues


def _idea_three_part_ready(row: dict[str, Any]) -> bool:
    return not _idea_contract_issues(row)


def _idea_rows_contract_issues(ideas: list[dict[str, Any]], required_count: int = 5) -> list[dict[str, Any]]:
    rows = [row for row in ideas if isinstance(row, dict)]
    issues: list[dict[str, Any]] = []
    if len(rows) < required_count:
        issues.append({"scope": "idea_count", "issue": "idea_count_below_required", "observed": len(rows), "required": required_count})
    for idx, row in enumerate(rows[:required_count], 1):
        row_issues = _idea_contract_issues(row)
        if row_issues:
            issues.append({"index": idx, "id": row.get("id") or row.get("idea_id"), "title": row.get("title"), "issues": row_issues})
    return issues


IDEA_CONTRACT_ISSUE_LABELS_ZH = {
    "idea_count_below_required": "idea 数量不足",
    "new_method_missing_or_too_short": "新方法过短或缺失",
    "initial_experiment_missing_or_too_short": "初步实验过短或缺失",
    "initial_experiment_is_generic_environment_placeholder": "初步实验仍是环境阶段占位话术",
    "inspired_by_missing": "缺少 inspired by 文献依据",
    "stale_or_preselected_base_binding_detected": "继承了旧基底或预选执行 gate",
    "objective_scores_missing": "缺少客观评分分项",
    "objective_scores_contain_zero_or_negative": "客观评分含 0 或负数",
    "objective_score_below_threshold": "总体评分低于 7 分",
    "score_or_idea_score_missing": "缺少 score 或 idea_score",
    "idea_score_audit_missing_or_not_subagent": "缺少独立 subagent 评分审计",
}


def _idea_contract_issue_summary_zh(issues: list[dict[str, Any]], limit: int = 5) -> list[str]:
    lines: list[str] = []
    for item in issues[:limit]:
        if not isinstance(item, dict):
            continue
        if item.get("scope") == "idea_count":
            lines.append(f"idea 数量不足：当前 {item.get('observed', 0)} / 需要 {item.get('required', 5)}")
            continue
        title = compact(item.get("title") or item.get("id") or f"idea {item.get('index', '')}", 80)
        labels = [IDEA_CONTRACT_ISSUE_LABELS_ZH.get(str(issue), str(issue)) for issue in as_list(item.get("issues"))]
        if labels:
            lines.append(f"{title}: " + "、".join(labels[:6]))
    return lines


def _ideas_three_part_ready(ideas: list[dict[str, Any]], required_count: int = 5) -> bool:
    rows = [row for row in ideas if isinstance(row, dict)]
    return len(rows) >= required_count and not _idea_rows_contract_issues(rows, required_count)


def _plan_contract_issues(row: dict[str, Any]) -> list[str]:
    if not isinstance(row, dict):
        return ["plan row is not an object"]
    issues: list[str] = []
    if _contains_pre_environment_base_binding(_binding_view(row, PRE_ENV_PLAN_BINDING_KEYS)):
        issues.append("stale_or_preselected_base_binding_detected")
    return issues


def _plan_rows_contract_issues(plans: list[dict[str, Any]], required_count: int = 5) -> list[dict[str, Any]]:
    rows = [row for row in plans if isinstance(row, dict)]
    issues: list[dict[str, Any]] = []
    if len(rows) < required_count:
        issues.append({"scope": "plan_count", "issue": "plan_count_below_required", "observed": len(rows), "required": required_count})
    for idx, row in enumerate(rows[:required_count], 1):
        row_issues = _plan_contract_issues(row)
        if row_issues:
            issues.append({"index": idx, "id": row.get("plan_id") or row.get("id"), "title": row.get("title"), "issues": row_issues})
    return issues


def _plans_contract_ready(plans: list[dict[str, Any]], required_count: int = 5) -> bool:
    rows = [row for row in plans if isinstance(row, dict)]
    return len(rows) >= required_count and not _plan_rows_contract_issues(rows, required_count)


def _plan_contract_issue_summary_zh(issues: list[dict[str, Any]], limit: int = 5) -> list[str]:
    lines: list[str] = []
    for item in issues[:limit]:
        if not isinstance(item, dict):
            continue
        if item.get("scope") == "plan_count":
            lines.append(f"plan 数量不足：当前 {item.get('observed', 0)} / 需要 {item.get('required', 5)}")
            continue
        title = compact(item.get("title") or item.get("id") or f"plan {item.get('index', '')}", 80)
        labels = [IDEA_CONTRACT_ISSUE_LABELS_ZH.get(str(issue), str(issue)) for issue in as_list(item.get("issues"))]
        if labels:
            lines.append(f"{title}: " + "、".join(labels[:6]))
    return lines


def _idea_parseable_for_audit(row: dict[str, Any]) -> bool:
    new_method = compact(row.get("new_method") or row.get("hypothesis"), 2200)
    initial_experiment = compact(
        row.get("initial_experiment")
        or row.get("experiment_design")
        or row.get("experimental_design")
        or row.get("min_experiment")
        or row.get("minimum_experiment"),
        2200,
    )
    inspired_by = _normalize_inspired_refs(row.get("inspired_by"), as_list(row.get("supporting_papers") or row.get("positive_anchor_papers")))
    return bool(len(new_method) >= 20 and len(initial_experiment) >= 20 and inspired_by)


def _valid_claude_idea(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    status = str(row.get("status") or row.get("recommendation") or "").lower()
    has_title = bool(str(row.get("title") or row.get("idea_id") or row.get("id") or "").strip())
    has_allowed_status = status.startswith("approved") or "blocked" in status or status.startswith("pursue") or status.startswith("watch")
    contract_ready = not _idea_contract_issues(row)
    return has_title and (has_allowed_status or contract_ready) and _idea_parseable_for_audit(row)


def _valid_current_find_idea(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    has_id_or_title = bool(str(row.get("id") or row.get("idea_id") or row.get("title") or "").strip())
    has_method_signal = bool(
        compact(row.get("new_method") or row.get("hypothesis") or row.get("mechanism"), 400)
        or compact(row.get("method_details") or row.get("summary") or row.get("description"), 400)
    )
    return bool(has_id_or_title and has_method_signal and not _contains_pre_environment_base_binding(_binding_view(row, PRE_ENV_IDEA_BINDING_KEYS)))


def _valid_claude_plan(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    has_id = bool(str(row.get("plan_id") or row.get("idea_id") or row.get("id") or "").strip())
    has_title = bool(str(row.get("title") or "").strip())
    has_action = bool(
        row.get("steps")
        or row.get("final_plan")
        or row.get("versions")
        or row.get("experiment_steps")
        or row.get("success_gate")
        or row.get("minimal_experiment")
        or row.get("initial_experiment")
        or row.get("environment_phase")
        or row.get("baseline_and_ablation")
        or row.get("bad_case_slice")
        or row.get("execution_selection")
        or row.get("stop_condition")
    )
    return has_id and (has_action or has_title) and not _plan_contract_issues(row)


def _current_find_artifact_parse_failure_report(run_id: str, failures: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "valid": False,
        "status": "artifact_parse_failed",
        "run_id": run_id,
        "source": CLAUDE_TAKEOVER_SOURCE,
        "policy_version": FULL_TEXT_READ_POLICY_VERSION,
        "actual_reading_count": 0,
        "full_text_reading_count": 0,
        "pending_full_text_reading_count": 0,
        "artifact_parse_failures": failures,
        "blockers": [
            "current-Find Claude artifact JSON parse failed; Claude Code must rewrite the damaged JSON artifact with one complete Claude Write before TASTE can validate deep reading",
        ],
        "next_required_action": "rerun_current_find_claude_takeover_repair_rewrite_parseable_artifacts",
        "policy": "A current-Find Read/Idea/Plan artifact must be valid JSON before TASTE can inspect full-text evidence or Chinese deep-read fields. Do not normalize or hand-edit scientific content around a corrupt artifact.",
        "generated_at": now_iso(),
    }


def record_current_find_artifact_parse_failure(state_dir: Path | None, run_id: str, failures: list[dict[str, Any]]) -> None:
    if state_dir is None:
        return
    report = _current_find_artifact_parse_failure_report(run_id, failures)
    save_json(state_dir / "current_find_claude_reading_validation.json", report)
    save_json(state_dir / "current_find_claude_artifact_parse_failure.json", report)


def _claude_project_log_root(workspace_path: Path) -> Path:
    try:
        resolved = str(Path(workspace_path).expanduser().resolve())
    except Exception:
        resolved = str(Path(workspace_path).expanduser())
    encoded = resolved.replace("\\", "-").replace("/", "-")
    return Path.home() / ".claude" / "projects" / encoded


def _current_find_takeover_session_ids(paths: Any, takeover: Any) -> list[str]:
    values: list[str] = []
    payloads = [takeover if isinstance(takeover, dict) else {}]
    state_dir = getattr(paths, "state", None)
    if state_dir:
        payloads.extend([
            load_json(Path(state_dir) / "current_find_claude_takeover_result.json", {}),
            load_json(Path(state_dir) / "claude_project_session_last_result.json", {}),
        ])
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for key in ["claude_session_id", "session_id"]:
            value = str(payload.get(key) or "").strip()
            if value and value not in values:
                values.append(value)
        for key in ["stdout_tail", "stdout"]:
            for match in re.finditer(r"session initialized \(([0-9a-fA-F-]{12,})\)", str(payload.get(key) or "")):
                value = match.group(1).strip()
                if value and value not in values:
                    values.append(value)
    return values


def _current_find_subagent_log_dirs(paths: Any, takeover: Any) -> list[Path]:
    dirs: list[Path] = []
    payloads = [takeover if isinstance(takeover, dict) else {}]
    state_dir = getattr(paths, "state", None)
    if state_dir:
        payloads.append(load_json(Path(state_dir) / "current_find_claude_takeover_result.json", {}))
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for key in ["claude_subagent_log_dir", "subagent_log_dir", "claude_subagents_dir", "subagents_dir"]:
            value = str(payload.get(key) or "").strip()
            if value:
                candidate = Path(value).expanduser()
                if candidate.exists() and candidate not in dirs:
                    dirs.append(candidate)
    claude_roots: list[Path] = []
    for root_value in [ROOT, getattr(paths, "root", None)]:
        if root_value is None:
            continue
        candidate_root = _claude_project_log_root(Path(root_value))
        if candidate_root not in claude_roots:
            claude_roots.append(candidate_root)
    for session_id in _current_find_takeover_session_ids(paths, takeover):
        for claude_root in claude_roots:
            candidate = claude_root / session_id / "subagents"
            if candidate.exists() and candidate not in dirs:
                dirs.append(candidate)
    return dirs


def _assistant_texts_from_subagent_log(path: Path) -> list[str]:
    texts: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return texts
    for line in lines:
        try:
            event = json.loads(line)
        except Exception:
            continue
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        if message.get("role") != "assistant":
            continue
        chunks: list[str] = []
        for item in as_list(message.get("content")):
            if isinstance(item, dict) and item.get("type") == "text":
                chunks.append(str(item.get("text") or ""))
        text = "\n".join(chunk for chunk in chunks if chunk)
        if text:
            texts.append(text)
    return texts


def _json_payloads_from_claude_text(text: str) -> list[Any]:
    payloads: list[Any] = []
    fence_pattern = "```" + r"(?:json)?\s*(.*?)" + "```"
    for match in re.finditer(fence_pattern, str(text or ""), re.I | re.S):
        block = match.group(1).strip()
        if not block:
            continue
        try:
            payloads.append(json.loads(block))
        except Exception:
            continue
    if payloads:
        return payloads
    decoder = json.JSONDecoder()
    raw = str(text or "")
    for index, char in enumerate(raw):
        if char not in "[{":
            continue
        try:
            payload, _end = decoder.raw_decode(raw[index:])
        except Exception:
            continue
        payloads.append(payload)
        break
    return payloads


def _reading_rows_from_subagent_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ["readings", "papers", "results"]:
            rows = [row for row in as_list(payload.get(key)) if isinstance(row, dict)]
            if rows:
                return rows
        if _valid_claude_reading(payload):
            return [payload]
    return []


def _jsonish_object_slices(text: str) -> list[str]:
    raw = str(text or "")
    starts = [match.start() for match in re.finditer(r"\{\s*\"paper_id\"\s*:", raw)]
    slices: list[str] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(raw)
        chunk = raw[start:end].strip()
        if index + 1 < len(starts):
            chunk = chunk.rsplit("}", 1)[0] + "}" if "}" in chunk else chunk
        else:
            close = chunk.rfind("}")
            if close >= 0:
                chunk = chunk[: close + 1]
        if chunk.startswith("{") and chunk.endswith("}"):
            slices.append(chunk)
    return slices


def _jsonish_value_by_key(object_text: str, key: str, ordered_keys: list[str]) -> str:
    key_match = re.search(rf"\"{re.escape(key)}\"\s*:", object_text)
    if not key_match:
        return ""
    start = key_match.end()
    end = len(object_text)
    for later_key in ordered_keys[ordered_keys.index(key) + 1 :]:
        later = re.search(rf",\s*\n?\s*\"{re.escape(later_key)}\"\s*:", object_text[start:])
        if later:
            end = min(end, start + later.start())
    value = object_text[start:end].strip().rstrip(",").strip()
    return value


def _jsonish_scalar(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith('"'):
        text = text[1:]
    if text.endswith('"'):
        text = text[:-1]
    return text.replace('\\"', '"').replace('\\n', '\n').strip()


def _jsonish_string_list(value: str) -> list[str]:
    text = str(value or "").strip()
    if not text.startswith("["):
        return []
    try:
        parsed = json.loads(text)
        return [str(item).strip() for item in parsed if str(item).strip()] if isinstance(parsed, list) else []
    except Exception:
        pass
    inner = text.strip()[1:-1]
    items: list[str] = []
    for match in re.finditer(r'"(.*?)(?:"\s*,|"\s*$)', inner, re.S):
        item = _jsonish_scalar('"' + match.group(1) + '"')
        if item:
            items.append(item)
    if not items:
        for part in re.split(r'"\s*,\s*"', inner.strip().strip('"')):
            item = _jsonish_scalar('"' + part + '"')
            if item:
                items.append(item)
    return items


def _jsonish_dict(value: str) -> dict[str, Any]:
    text = str(value or "").strip()
    if not text.startswith("{"):
        return {}
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass
    out: dict[str, Any] = {}
    for key in ["mode", "subagent_used", "status", "text_path", "evidence_chars", "log_path"]:
        match = re.search(rf"\"{key}\"\s*:\s*(true|false|[0-9]+|\".*?\")", text, re.S)
        if not match:
            continue
        raw = match.group(1).strip()
        if raw == "true":
            out[key] = True
        elif raw == "false":
            out[key] = False
        elif raw.isdigit():
            out[key] = int(raw)
        else:
            out[key] = _jsonish_scalar(raw)
    return out


def _jsonish_reading_rows_from_text(text: str) -> list[dict[str, Any]]:
    ordered_keys = [
        "paper_id",
        "title",
        "verdict",
        "support_role",
        "critique_reason",
        "abstract_zh",
        "motivation_zh",
        "method_details_zh",
        "method",
        "experiments_zh",
        "experiments",
        "limitations_zh",
        "limitations",
        "method_advantages_zh",
        "method_disadvantages_zh",
        "full_text_available",
        "full_text_status",
        "pdf_text_chars",
        "full_text_chars",
        "subagent_deep_read",
        "deep_read_audit",
    ]
    rows: list[dict[str, Any]] = []
    for object_text in _jsonish_object_slices(text):
        row: dict[str, Any] = {}
        for key in ordered_keys:
            value = _jsonish_value_by_key(object_text, key, ordered_keys)
            if not value:
                continue
            if key in {"method_advantages_zh", "method_disadvantages_zh"}:
                row[key] = _jsonish_string_list(value)
            elif key == "deep_read_audit":
                row[key] = _jsonish_dict(value)
            elif key in {"full_text_available", "subagent_deep_read"}:
                row[key] = str(value).strip().lower().startswith("true")
            elif key in {"pdf_text_chars", "full_text_chars"}:
                row[key] = _positive_int(value.strip().strip('"'))
            else:
                row[key] = _jsonish_scalar(value)
        if _valid_claude_reading(row):
            rows.append(row)
    return rows


def _current_find_subagent_reading_candidates(paths: Any, takeover: Any) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for directory in _current_find_subagent_log_dirs(paths, takeover):
        for log_path in sorted(Path(directory).glob("*.jsonl")):
            for text in _assistant_texts_from_subagent_log(log_path):
                if "paper_id" not in text and "abstract_zh" not in text and "method_details_zh" not in text:
                    continue
                rows: list[dict[str, Any]] = []
                for payload in _json_payloads_from_claude_text(text):
                    rows.extend(_reading_rows_from_subagent_payload(payload))
                rows.extend(_jsonish_reading_rows_from_text(text))
                seen_titles: set[str] = set()
                for row in rows:
                    if not _valid_claude_reading(row):
                        continue
                    clean = dict(row)
                    title_key = norm_title(clean.get("title") or clean.get("paper_title"))
                    if title_key and title_key in seen_titles:
                        continue
                    if title_key:
                        seen_titles.add(title_key)
                    audit = clean.get("deep_read_audit") if isinstance(clean.get("deep_read_audit"), dict) else {}
                    audit = dict(audit)
                    audit.setdefault("mode", "task_subagent")
                    audit.setdefault("subagent_used", True)
                    audit.setdefault("status", "completed")
                    audit.setdefault("log_path", str(log_path))
                    clean["deep_read_audit"] = audit
                    clean["subagent_deep_read"] = True
                    clean.setdefault("deep_read_source", "claude_subagent_jsonl")
                    candidates.append(clean)
    return candidates



FRAGMENT_READING_TOP_LEVEL_MERGE_KEYS = (
    "paper_id", "id", "entry_id", "title", "paper_title", "authors", "venue", "year", "url", "pdf_url", "code_url",
    "verdict", "support_role", "critique_reason", "summary", "abstract_zh", "deep_read_abstract_zh", "abstract_original",
    "abstract_from_find", "find_abstract_zh", "problem", "motivation_zh", "method", "method_details_zh",
    "method_family_zh", "experiments", "experiments_zh", "limitations", "limitations_zh",
    "method_advantages_zh", "method_disadvantages_zh", "full_text_available", "full_text_status",
    "pdf_text_chars", "full_text_chars", "source_text_chars", "source_evidence", "subagent_deep_read",
    "deep_read_audit", "deep_read_source", "reading_status_note_zh",
)

FRAGMENT_READING_TOP_LEVEL_ALIASES = (
    ("advantages", "method_advantages_zh"),
    ("method_advantages", "method_advantages_zh"),
    ("disadvantages", "method_disadvantages_zh"),
    ("method_disadvantages", "method_disadvantages_zh"),
    ("full_text", "full_text_available"),
)


def _fragment_value_present(value: Any) -> bool:
    return value not in (None, "", [], {})


def _fragment_alias_value(target_key: str, value: Any) -> Any:
    if target_key == "full_text_available":
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        if text in {"true", "yes", "available", "read", "full_text_read", "pdf_text_read", "html_text_read", "text_extracted"}:
            return True
        if text in {"false", "no", "unavailable", "not_available", "missing"}:
            return False
    if target_key in {"method_advantages_zh", "method_disadvantages_zh"} and isinstance(value, str):
        return [value]
    return value


def _merge_fragment_top_level_reading_fields(payload: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    """Accept the fragment envelope used by Claude subagents without weakening the reading contract."""
    clean = dict(row)
    for key in FRAGMENT_READING_TOP_LEVEL_MERGE_KEYS:
        if not _fragment_value_present(clean.get(key)) and _fragment_value_present(payload.get(key)):
            clean[key] = payload.get(key)
    for source_key, target_key in FRAGMENT_READING_TOP_LEVEL_ALIASES:
        if not _fragment_value_present(clean.get(target_key)) and _fragment_value_present(payload.get(source_key)):
            clean[target_key] = _fragment_alias_value(target_key, payload.get(source_key))
    return clean

def _fragment_payload_is_current(payload: Any, file_path: Path, run_id: str, current_revision: dt.datetime | None = None) -> bool:
    if not isinstance(payload, dict):
        return False
    expected_run = str(run_id or "").strip()
    payload_run = str(payload.get("run_id") or payload.get("current_find_run_id") or "").strip()
    if expected_run and payload_run != expected_run:
        return False
    if payload_run and expected_run and payload_run != expected_run:
        return False
    source = str(payload.get("source") or payload.get("artifact_source") or "").strip()
    if source and source not in (CURRENT_FIND_DEEP_READ_FRAGMENT_SOURCES | {CLAUDE_TAKEOVER_SOURCE}):
        return False
    # Per-paper fragments are immutable deliveries for a specific Find run.  A
    # later full_text_packet/current revision can arrive after an already valid
    # fragment; that must not make the same-run fragment stale.  Staleness is
    # guarded by run_id, while quality selection happens via _reading_quality_score.
    if payload_run and expected_run:
        return True
    if current_revision is None:
        return True
    generated = parse_iso_time(payload.get("generated_at"))
    if generated is None:
        try:
            generated = dt.datetime.fromtimestamp(Path(file_path).stat().st_mtime, dt.timezone.utc)
        except OSError:
            generated = None
    return bool(generated is not None and generated + dt.timedelta(seconds=2) >= current_revision)


def _current_find_deep_read_fragment_rows(taste_dir: Path, run_id: str, current_revision: dt.datetime | None = None) -> list[dict[str, Any]]:
    fragment_dir = Path(taste_dir) / CURRENT_FIND_DEEP_READ_FRAGMENT_DIR_NAME
    if not fragment_dir.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(fragment_dir.glob("*.json")):
        payload, error = load_json_with_error(path, {})
        if error or not _fragment_payload_is_current(payload, path, run_id, current_revision):
            continue
        source = str((payload if isinstance(payload, dict) else {}).get("source") or (payload if isinstance(payload, dict) else {}).get("artifact_source") or "").strip()
        payload_rows: list[dict[str, Any]] = []
        if isinstance(payload, dict) and isinstance(payload.get("reading"), dict):
            payload_rows.append(_merge_fragment_top_level_reading_fields(payload, payload["reading"]))
        payload_rows.extend(_reading_rows_from_subagent_payload(payload))
        for row in payload_rows:
            if not isinstance(row, dict) or not _valid_claude_reading(row):
                continue
            clean = dict(row)
            clean.setdefault("run_id", run_id)
            audit = clean.get("deep_read_audit") if isinstance(clean.get("deep_read_audit"), dict) else {}
            audit = dict(audit)
            audit.setdefault("mode", "task_subagent")
            audit.setdefault("subagent_used", True)
            audit.setdefault("status", "completed")
            audit.setdefault("fragment_path", str(path))
            if source:
                audit.setdefault("source", source)
            clean["deep_read_audit"] = audit
            clean["subagent_deep_read"] = True
            clean.setdefault("deep_read_source", source or CURRENT_FIND_DEEP_READ_FRAGMENT_SOURCE)
            rows.append(clean)
    return rows


def _reading_pass_count(readings: list[dict[str, Any]]) -> int:
    return sum(1 for row in readings if isinstance(row, dict) and not _reading_deep_read_content_gaps(row))


def _reading_quality_score(row: dict[str, Any]) -> tuple[int, int, int, int, int]:
    if not isinstance(row, dict):
        return (-999, -10, 0, 0, 0)
    gaps = _reading_deep_read_content_gaps(row)
    status_score = _reading_status_quality_score(row)
    field_len = sum(
        min(_nonspace_len(row.get(key) or row.get(fallback) or ""), 1200)
        for key, fallback in [
            ("abstract_zh", "summary"),
            ("motivation_zh", "problem"),
            ("method_details_zh", "method"),
            ("experiments_zh", "experiments"),
            ("limitations_zh", "limitations"),
        ]
    )
    list_items = len(_read_list(row, "method_advantages_zh")) + len(_read_list(row, "method_disadvantages_zh"))
    evidence = _positive_int(row.get("pdf_text_chars") or row.get("full_text_chars") or row.get("source_text_chars"))
    if evidence < FULL_TEXT_MIN_CHARS:
        evidence = max((_full_text_evidence_chars(item) for item in _full_text_evidence_dicts(row)), default=0)
    return (-len(gaps), status_score, field_len, list_items, evidence)


def _index_better_candidate(index: dict[str, dict[str, Any]], key: str, candidate: dict[str, Any]) -> None:
    if not key:
        return
    existing = index.get(key)
    if existing is None or _reading_quality_score(candidate) > _reading_quality_score(existing):
        index[key] = candidate


def _best_candidate_for_find_row(candidate_index: dict[str, dict[str, Any]], candidates: list[dict[str, Any]], find_row: dict[str, Any]) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for identity in _identity_values(find_row):
        candidate = candidate_index.get(identity)
        if candidate is not None:
            matches.append(candidate)
    title = norm_title(find_row.get("title"))
    if title:
        candidate = candidate_index.get(f"title:{title}")
        if candidate is not None:
            matches.append(candidate)
    if matches:
        return max(matches, key=_reading_quality_score)
    if not title:
        return {}
    best_ratio = 0.0
    best_candidate: dict[str, Any] = {}
    for candidate in candidates:
        candidate_title = norm_title(candidate.get("title") or candidate.get("paper_title"))
        if not candidate_title:
            continue
        ratio = difflib.SequenceMatcher(None, title, candidate_title).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_candidate = candidate
    return best_candidate if best_ratio >= 0.94 else {}


def _recover_current_find_readings_from_subagents(paths: Any, run_id: str, find_results: dict[str, Any], full_text_packet: dict[str, Any], takeover: Any, taste_dir: Path | None = None, current_revision: dt.datetime | None = None) -> list[dict[str, Any]]:
    if not isinstance(find_results, dict):
        return []
    candidates: list[dict[str, Any]] = []
    if taste_dir is not None:
        candidates.extend(_current_find_deep_read_fragment_rows(Path(taste_dir), run_id, current_revision))
    candidates.extend(_current_find_subagent_reading_candidates(paths, takeover))
    if not candidates:
        return []
    candidate_index: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        for identity in _reading_identity_values(candidate):
            _index_better_candidate(candidate_index, identity, candidate)
        title = norm_title(candidate.get("title") or candidate.get("paper_title"))
        if title:
            _index_better_candidate(candidate_index, f"title:{title}", candidate)
    packet_index = _full_text_packet_index(full_text_packet)
    recovered: list[dict[str, Any]] = []
    for find_row in _current_reading_packet_rows(find_results, full_text_packet, 0):
        match = _best_candidate_for_find_row(candidate_index, candidates, find_row)
        if not match:
            continue
        clean = _sanitize_reading_public_fields(dict(match))
        clean.setdefault("paper_id", find_row.get("id") or find_row.get("paper_id") or find_row.get("entry_id"))
        for key in ["id", "url", "pdf_url", "venue", "year", "score", "fit_score", "evidence_tier", "taste_pool", "taste_pool_role", "taste_pool_rank"]:
            if find_row.get(key) not in (None, "", []):
                clean[key] = find_row.get(key)
        clean["title"] = str(find_row.get("title") or clean.get("title") or "").strip()
        clean = _merge_find_abstract_into_reading(clean, find_row)
        packet_entry = _matching_full_text_packet_entry(clean or find_row, packet_index)
        clean = normalize_reading_full_text_evidence(clean, packet_entry)
        audit = clean.get("deep_read_audit") if isinstance(clean.get("deep_read_audit"), dict) else {}
        audit = dict(audit)
        packet_text_path = first_text(packet_entry, "text_path") if packet_entry else ""
        packet_chars = _positive_int((packet_entry or {}).get("text_chars") or (packet_entry or {}).get("pdf_text_chars") or clean.get("pdf_text_chars"))
        if packet_text_path:
            audit["text_path"] = packet_text_path
        if packet_chars:
            audit["evidence_chars"] = packet_chars
        audit.setdefault("mode", "task_subagent")
        audit.setdefault("subagent_used", True)
        audit.setdefault("status", "completed")
        clean["deep_read_audit"] = audit
        clean["subagent_deep_read"] = True
        recovered.append(clean)
    return recovered


def _refresh_same_run_payload_timestamp(path: Path, payload: Any, run_id: str, recovered_at: str) -> None:
    if not isinstance(payload, dict):
        return
    if payload.get("run_id") != run_id or payload.get("source") != CLAUDE_TAKEOVER_SOURCE:
        return
    updated = dict(payload)
    updated["generated_at"] = recovered_at
    recovery = updated.get("artifact_recovery") if isinstance(updated.get("artifact_recovery"), dict) else {}
    recovery.update({"refreshed_at": recovered_at, "source": "wrapper_same_run_reserialize"})
    updated["artifact_recovery"] = recovery
    save_json(path, strip_verbose_claude_takeover(updated))


def _maybe_recover_read_results_from_subagent_logs(
    paths: Any,
    taste_dir: Path,
    run_id: str,
    find_results: dict[str, Any],
    full_text_packet: dict[str, Any],
    existing_readings: list[dict[str, Any]],
    read_payload: dict[str, Any],
    idea_payload: dict[str, Any],
    plan_payload: dict[str, Any],
    current_revision: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    if paths is None:
        return existing_readings
    takeover = load_json(getattr(paths, "state", Path("")) / "current_find_claude_takeover_result.json", {})
    recovered = _recover_current_find_readings_from_subagents(paths, run_id, find_results, full_text_packet, takeover if isinstance(takeover, dict) else {}, Path(taste_dir), current_revision)
    if not recovered:
        return existing_readings
    existing_score = _reading_pass_count(existing_readings)
    recovered_score = _reading_pass_count(recovered)
    if recovered_score < existing_score:
        return existing_readings
    expected = len(_current_reading_packet_rows(find_results, full_text_packet, 0)) if isinstance(find_results, dict) else len(recovered)
    if expected and len(recovered) < expected:
        return existing_readings
    recovered_at = now_iso()
    payload = dict(read_payload) if isinstance(read_payload, dict) else {}
    payload.update({
        "run_id": run_id,
        "source": CLAUDE_TAKEOVER_SOURCE,
        "generated_at": recovered_at,
        "readings": recovered,
        "artifact_recovery": {
            "source": "claude_subagent_jsonl",
            "recovered_at": recovered_at,
            "policy": "wrapper recovered structured deep-reading rows authored by Claude subagents in the same takeover session; no deterministic scientific content was generated.",
        },
    })
    save_json(Path(taste_dir) / "read_results.json", strip_verbose_claude_takeover(payload))
    _refresh_same_run_payload_timestamp(Path(taste_dir) / "ideas.json", idea_payload, run_id, recovered_at)
    _refresh_same_run_payload_timestamp(Path(taste_dir) / "plans.json", plan_payload, run_id, recovered_at)
    receipt = {
        "status": "recovered_current_find_read_results_from_claude_subagents",
        "run_id": run_id,
        "recovered_at": recovered_at,
        "recovered_reading_count": len(recovered),
        "previous_deep_read_pass_count": existing_score,
        "recovered_deep_read_pass_count": recovered_score,
        "source": "claude_subagent_jsonl",
    }
    save_json(getattr(paths, "state", Path(".")) / "current_find_subagent_reading_recovery.json", receipt)
    return recovered


def _select_current_find_readings_from_candidates(candidates: list[dict[str, Any]], find_results: dict[str, Any], full_text_packet: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(find_results, dict):
        return [row for row in candidates if isinstance(row, dict) and _valid_claude_reading(row)]
    recommendation_rows = _current_reading_packet_rows(find_results, full_text_packet, 0)
    if not recommendation_rows:
        return [row for row in candidates if isinstance(row, dict) and _valid_claude_reading(row)]
    candidate_index: dict[str, dict[str, Any]] = {}
    clean_candidates: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or not _valid_claude_reading(candidate):
            continue
        clean = _sanitize_reading_public_fields(dict(candidate))
        clean_candidates.append(clean)
        for identity in _reading_identity_values(clean):
            _index_better_candidate(candidate_index, identity, clean)
        title = norm_title(clean.get("title") or clean.get("paper_title"))
        if title:
            _index_better_candidate(candidate_index, f"title:{title}", clean)
    if not clean_candidates:
        return []
    packet_index = _full_text_packet_index(full_text_packet)
    selected: list[dict[str, Any]] = []
    for find_row in recommendation_rows:
        match = _best_candidate_for_find_row(candidate_index, clean_candidates, find_row)
        if not match:
            continue
        clean = _sanitize_reading_public_fields(dict(match))
        clean.setdefault("paper_id", find_row.get("id") or find_row.get("paper_id") or find_row.get("entry_id"))
        for key in ["id", "url", "pdf_url", "venue", "year", "score", "fit_score", "evidence_tier", "taste_pool", "taste_pool_role", "taste_pool_rank"]:
            if find_row.get(key) not in (None, "", []):
                clean[key] = find_row.get(key)
        clean["title"] = str(find_row.get("title") or clean.get("title") or "").strip()
        clean = _merge_find_abstract_into_reading(clean, find_row)
        clean = normalize_reading_full_text_evidence(clean, _matching_full_text_packet_entry(clean or find_row, packet_index))
        clean = _sanitize_reading_public_fields(clean)
        selected.append(clean)
    return selected


def load_claude_outputs(taste_dir: Path, run_id: str, find_results: dict[str, Any] | None = None, read_limit: int = 10, state_dir: Path | None = None, current_revision: dt.datetime | None = None, project_paths: Any = None, write_pending_validation: bool = True) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    read_payload_path = taste_dir / "read_results.json"
    idea_payload_path = taste_dir / "ideas.json"
    plan_payload_path = taste_dir / "plans.json"
    read_payload, read_error = load_json_with_error(read_payload_path, {})
    idea_payload, idea_error = load_json_with_error(idea_payload_path, {})
    plan_payload, plan_error = load_json_with_error(plan_payload_path, {})
    full_text_packet = load_full_text_packet_from_taste_dir(taste_dir, run_id)
    fragment_rows = _current_find_deep_read_fragment_rows(Path(taste_dir), run_id, current_revision)
    parse_failures = [
        {**failure, "artifact": artifact}
        for artifact, failure in [
            ("read_results.json", read_error),
            ("ideas.json", idea_error),
            ("plans.json", plan_error),
        ]
        if isinstance(failure, dict)
    ]
    if parse_failures:
        blocking_failures = [failure for failure in parse_failures if failure.get("artifact") != "read_results.json" or not fragment_rows]
        if blocking_failures:
            record_current_find_artifact_parse_failure(state_dir, run_id, blocking_failures)
            return [], [], []
    idea_current = _current_payload_is_current(idea_payload, idea_payload_path, run_id, CURRENT_FIND_IDEA_ARTIFACT_SOURCES, current_revision)
    plan_current = _current_payload_is_current(plan_payload, plan_payload_path, run_id, CURRENT_FIND_PLAN_ARTIFACT_SOURCES, current_revision)
    read_current = _current_payload_is_current(read_payload, read_payload_path, run_id, CURRENT_FIND_READ_ARTIFACT_SOURCES, current_revision)
    recommendation_index: dict[str, dict[str, Any]] = {}
    if isinstance(find_results, dict):
        for recommendation_row in _current_recommendation_rows(find_results):
            for identity in _identity_values(recommendation_row):
                recommendation_index.setdefault(identity, recommendation_row)
    parsed_readings: list[dict[str, Any]] = []
    candidate_readings: list[dict[str, Any]] = []
    candidate_readings.extend(fragment_rows)
    if read_current:
        for row in as_list(read_payload.get("readings")):
            if not isinstance(row, dict):
                continue
            sanitized = _sanitize_reading_public_fields(row)
            sanitized = _merge_find_abstract_into_reading(sanitized, _find_row_for_reading(sanitized, recommendation_index))
            if _valid_claude_reading(sanitized):
                candidate_readings.append(sanitized)
    if candidate_readings and isinstance(find_results, dict):
        parsed_readings = _select_current_find_readings_from_candidates(candidate_readings, find_results, full_text_packet)
    elif candidate_readings:
        parsed_readings = candidate_readings
    readings = normalize_readings_full_text_evidence(parsed_readings, full_text_packet)
    if isinstance(find_results, dict) and not readings:
        readings = _maybe_recover_read_results_from_subagent_logs(
            project_paths,
            taste_dir,
            run_id,
            find_results,
            full_text_packet,
            readings,
            read_payload if isinstance(read_payload, dict) else {},
            idea_payload if isinstance(idea_payload, dict) else {},
            plan_payload if isinstance(plan_payload, dict) else {},
            current_revision,
        )
    if idea_current:
        strict_idea_source = str(idea_payload.get("source") or "").strip() == CLAUDE_TAKEOVER_SOURCE
        idea_validator = _valid_claude_idea if strict_idea_source else _valid_current_find_idea
        ideas = [row for row in as_list(idea_payload.get("ideas")) if idea_validator(row)]
    else:
        ideas = []
    plans = [row for row in as_list(plan_payload.get("plans")) if _valid_claude_plan(row)] if plan_current else []
    if _contains_stale_execution_binding({"ideas": ideas, "plans": plans}):
        return [], [], []
    if isinstance(find_results, dict):
        reading_rows_for_validation = _current_reading_packet_rows(find_results, full_text_packet, read_limit)
        validation_find_results = _reading_packet_find_results(find_results, reading_rows_for_validation)
        valid, report = validate_claude_readings_against_current_find(readings, validation_find_results, len(reading_rows_for_validation), project_paths, run_id)
        if state_dir is not None and (write_pending_validation or readings):
            validation_payload = {**report, "run_id": run_id, "source": CLAUDE_TAKEOVER_SOURCE, "full_text_packet": full_text_packet_summary(full_text_packet), "generated_at": now_iso()}
            if not idea_current or not plan_current:
                validation_payload["idea_plan_artifact_status"] = "stale_or_pending_current_claude_idea_plan"
                if not readings:
                    validation_payload["artifact_status"] = "pending_current_claude_read_idea_plan"
            save_json(state_dir / "current_find_claude_reading_validation.json", validation_payload)
        # Failed validation means the artifacts are not gate-ready; it does not
        # mean Claude failed to execute. Keep parsed same-run outputs so upper
        # layers can distinguish "executed but missing evidence/synthesis" from
        # "not executed" and route to the correct repair action.
    return readings, ideas, plans



def _recursively_block_environment_wait(value: Any, reason: str) -> None:
    if isinstance(value, dict):
        if value.get("status") == "waiting_for_environment_base_selection":
            value["status"] = "blocked_literature_recommendation_gate"
            value["blocked_by"] = "literature_recommendation_shortfall"
            value["blocked_reason"] = reason
        if value.get("ready_to_execute") is True:
            value["ready_to_execute"] = False
        for child in value.values():
            _recursively_block_environment_wait(child, reason)
    elif isinstance(value, list):
        for child in value:
            _recursively_block_environment_wait(child, reason)


def enforce_literature_gate_on_ideas_and_plans(ideas: list[dict[str, Any]], plans: list[dict[str, Any]], literature_gate: dict[str, Any]) -> None:
    if not isinstance(literature_gate, dict) or not literature_gate.get("blocked"):
        return
    reason = (
        f"current Find recommended papers are below target: "
        f"{literature_gate.get('strong_recommendations', 0)}/{literature_gate.get('recommendation_target_count', 0)}; "
        f"shortfall={literature_gate.get('recommendation_shortfall', 0)}"
    )
    for row in ideas:
        if not isinstance(row, dict):
            continue
        _recursively_block_environment_wait(row, reason)
        row["status"] = "blocked_literature_recommendation_gate"
        row["recommendation"] = "wait_for_literature_gate_repair"
        row["blocked_by"] = "literature_recommendation_shortfall"
        row["blocked_reason"] = reason
        row["ready_for_environment_base_selection"] = False
    for row in plans:
        if not isinstance(row, dict):
            continue
        _recursively_block_environment_wait(row, reason)
        row["status"] = "blocked_literature_recommendation_gate"
        row["blocked_by"] = "literature_recommendation_shortfall"
        row["blocked_reason"] = reason
        row["ready_to_execute"] = False
        row["repo_path"] = ""
        row["train_command"] = ""


def strip_verbose_claude_takeover(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    takeover = payload.get("claude_takeover")
    if isinstance(takeover, dict):
        payload["claude_takeover"] = {key: takeover.get(key) for key in ["status", "return_code", "started_at", "finished_at", "prompt_path"] if key in takeover}
    return payload

def _normalize_queries(values: Any) -> list[str]:
    raw = as_list(values) if not isinstance(values, str) else [values]
    out: list[str] = []
    seen: set[str] = set()
    for value in raw:
        if isinstance(value, dict):
            text = first_text(value, "query", "topic", "title", "q")
        else:
            text = str(value or "")
        text = " ".join(text.split()).strip()
        key = text.lower()
        if len(text) >= 8 and key not in seen:
            seen.add(key)
            out.append(text)
    return out


def extract_targeted_search_queries(paths, read_results: dict[str, Any], ideas_results: dict[str, Any], plans_results: dict[str, Any], state_plan: dict[str, Any] | None = None) -> list[str]:
    queries: list[str] = []
    for payload in [read_results, ideas_results, plans_results, state_plan or {}, load_json(paths.state / "taste_targeted_queries.json", {})]:
        if not isinstance(payload, dict):
            continue
        for key in ["targeted_search_queries", "supplemental_search_queries", "search_queries", "additional_search_queries", "queries"]:
            queries.extend(_normalize_queries(payload.get(key)))
        meta = payload.get("claude_takeover") if isinstance(payload.get("claude_takeover"), dict) else {}
        for key in ["targeted_search_queries", "supplemental_search_queries", "search_queries"]:
            queries.extend(_normalize_queries(meta.get(key)))
    for row in as_list((state_plan or {}).get("ideas")) + as_list(ideas_results.get("ideas")) + as_list(plans_results.get("plans")):
        if isinstance(row, dict):
            for key in ["targeted_search_queries", "supporting_search_queries", "literature_search_queries"]:
                queries.extend(_normalize_queries(row.get(key)))
    seen: set[str] = set()
    out: list[str] = []
    for query in queries:
        key = query.lower()
        if key not in seen:
            seen.add(key)
            out.append(query)
    return out


def ensure_claude_plan_state(project: str, paths, run_id: str, readings: list[dict[str, Any]], ideas: list[dict[str, Any]], plans: list[dict[str, Any]], takeover: dict[str, Any], idea_count: int = 5) -> dict[str, Any]:
    payload = load_json(paths.state / "current_find_research_plan.json", {})
    if not isinstance(payload, dict):
        payload = {}
    read_payload = load_json(paths.planning / "finding" / "read_results.json", {})
    idea_payload = load_json(paths.planning / "finding" / "ideas.json", {})
    plan_payload = load_json(paths.planning / "finding" / "plans.json", {})
    targeted_queries = extract_targeted_search_queries(paths, read_payload if isinstance(read_payload, dict) else {}, idea_payload if isinstance(idea_payload, dict) else {}, plan_payload if isinstance(plan_payload, dict) else {}, payload)
    ideas, plans = enrich_public_projections(ideas, plans)
    targeted_tool = load_json(paths.state / "taste_targeted_queries.json", {})
    if not isinstance(targeted_tool, dict):
        targeted_tool = {}
    targeted_run_id = str(targeted_tool.get("current_find_run_id") or targeted_tool.get("run_id") or targeted_tool.get("find_run_id") or "").strip()
    if targeted_run_id and run_id and targeted_run_id != run_id:
        targeted_tool = {}
    find_results_for_coverage = load_json(paths.planning / "finding" / "find_results.json", {})
    current_recommendation_rows = _current_recommendation_rows(find_results_for_coverage if isinstance(find_results_for_coverage, dict) else {})
    expected_recommendation_readings = len(current_recommendation_rows) or len(_current_recommendation_identities(find_results_for_coverage if isinstance(find_results_for_coverage, dict) else {})[1]) or 0
    required_idea_count = max(1, _positive_int(idea_count) or 5)
    raw_ideas = [row for row in as_list((idea_payload if isinstance(idea_payload, dict) else {}).get("ideas")) if isinstance(row, dict)]
    raw_plans = [row for row in as_list((plan_payload if isinstance(plan_payload, dict) else {}).get("plans")) if isinstance(row, dict)]
    selected_contract = _current_find_execution_contract(paths)
    selected_contract_ready_for_run = _contract_selected_for_run(selected_contract, run_id)
    state_source = _state_source_for_current_artifacts(read_payload, idea_payload, plan_payload)
    relaxed_current_contract = bool(
        selected_contract_ready_for_run
        and (
            _current_payload_source_allowed(idea_payload, CURRENT_FIND_IDEA_ARTIFACT_SOURCES)
            or _current_payload_source_allowed(plan_payload, CURRENT_FIND_PLAN_ARTIFACT_SOURCES)
        )
    )
    raw_idea_contract_issues_actual = _idea_rows_contract_issues(raw_ideas, required_idea_count)
    raw_plan_contract_issues_actual = _plan_rows_contract_issues(raw_plans, required_idea_count)

    def contract_issues_include_preselected_base_binding(items: list[dict[str, Any]]) -> bool:
        for item in items:
            for issue in as_list(item.get("issues") if isinstance(item, dict) else []):
                if str(issue) == "stale_or_preselected_base_binding_detected":
                    return True
        return False

    if contract_issues_include_preselected_base_binding(raw_idea_contract_issues_actual) or contract_issues_include_preselected_base_binding(raw_plan_contract_issues_actual):
        relaxed_current_contract = False
    idea_contract_issues = [] if relaxed_current_contract else _idea_rows_contract_issues(ideas, required_idea_count)
    raw_idea_contract_issues = [] if relaxed_current_contract else raw_idea_contract_issues_actual
    plan_contract_issues = [] if relaxed_current_contract else _plan_rows_contract_issues(plans, required_idea_count)
    raw_plan_contract_issues = [] if relaxed_current_contract else raw_plan_contract_issues_actual
    idea_schema_ready = bool(ideas) if relaxed_current_contract else (len(ideas) >= required_idea_count and not idea_contract_issues)
    plan_schema_ready = bool(plans) if relaxed_current_contract else (len(plans) >= required_idea_count and not plan_contract_issues)
    validation = load_json(paths.state / "current_find_claude_reading_validation.json", {})
    if not isinstance(validation, dict):
        validation = {}
    validation_ready = current_reading_validation_ready(validation, run_id, expected_recommendation_readings)
    counts_ready = bool(readings and len(ideas) >= required_idea_count and len(plans) >= required_idea_count and len(targeted_queries) >= 3 and idea_schema_ready and plan_schema_ready)
    if expected_recommendation_readings and len(readings) != expected_recommendation_readings:
        counts_ready = False
    contract_counts_ready = bool(
        relaxed_current_contract
        and readings
        and ideas
        and plans
        and (not expected_recommendation_readings or len(readings) == expected_recommendation_readings)
    )
    if contract_counts_ready:
        counts_ready = True
    content_ready = bool(counts_ready and validation_ready)
    selection_issue = "" if selected_contract_ready_for_run and content_ready else (current_find_selected_execution_issue(ideas, plans) if content_ready else "")
    execution_ready = bool(content_ready and not selection_issue)
    ready = execution_ready
    positive_anchor_readings = sum(
        1
        for row in readings
        if str(row.get("support_role") or "").lower() in {"positive_anchor_for_planning", "foundation_anchor_for_planning"}
        or bool(row.get("claim_ready_anchor"))
    )
    critique_or_boundary_readings = sum(
        1
        for row in readings
        if str(row.get("support_role") or "").lower() in {"critique_pool", "boundary_audit", "search_expansion"}
        or str(row.get("verdict") or "").lower() in {"critique_only", "boundary_only"}
    )
    invalid_positive_readings = len(validation.get("invalid_positive_titles", [])) if isinstance(validation.get("invalid_positive_titles"), list) else 0
    cfg = load_project_config(project)
    venue = project_target_venue(project, str(cfg.get("target_venue") or cfg.get("venue") or "ICLR"))
    progress = load_json(paths.planning / "finding" / "find_progress.json", {})
    packet = load_json(paths.state / "literature_tool_packet.json", {})
    packet_summary = packet.get("summary", {}) if isinstance(packet, dict) and isinstance(packet.get("summary"), dict) else {}
    progress_counts = progress.get("counts", {}) if isinstance(progress, dict) and isinstance(progress.get("counts"), dict) else {}
    strong = _safe_int((progress if isinstance(progress, dict) else {}).get("strong_recommendation_count") or packet_summary.get("strong_paper_anchors") or positive_anchor_readings, 0)
    target = _safe_int((progress if isinstance(progress, dict) else {}).get("recommendation_target_count") or packet_summary.get("recommendation_target_count"), 0)
    shortfall = _safe_int((progress if isinstance(progress, dict) else {}).get("recommendation_shortfall") or packet_summary.get("recommendation_shortfall") or (max(0, target - strong) if target else 0), 0)
    literature_gate = {
        "status": "shortfall" if shortfall > 0 else "pass" if target else "unknown",
        "blocked": shortfall > 0,
        "run_id": run_id,
        "strong_recommendations": strong,
        "recommendation_target_count": target,
        "recommendation_shortfall": shortfall,
        "evaluated_candidates": _safe_int(progress_counts.get("evaluated_candidates"), 0),
        "source": "planning/finding/find_progress.json",
    }
    literature_repair_commands = [
        f"{management_python()} modules/finding/main.py --action build_literature_tool_packet --project {project} --venue {venue}",
        f"{management_python()} modules/finding/main.py --action run_literature_tool --project {project} --venue {venue} --query \"<targeted literature gap query>\" --fast-mode --publish-current-find",
        f"{management_python()} modules/writing/main.py --action audit_submission_readiness --project {project} --venue {venue}",
        f"{management_python()} modules/planning/main.py --action build_blocker_action_plan --project {project} --venue {venue}",
    ]
    literature_gate_blocked = shortfall > 0
    literature_blockers = []
    if literature_gate_blocked:
        literature_blockers.append(f"current Find recommended papers are below target: {strong}/{target}; shortfall={shortfall}")
        enforce_literature_gate_on_ideas_and_plans(ideas, plans, literature_gate)
        if isinstance(idea_payload, dict):
            idea_payload["ideas"] = ideas
            strip_verbose_claude_takeover(idea_payload)
            save_json(paths.planning / "finding" / "ideas.json", idea_payload)
        if isinstance(plan_payload, dict):
            plan_payload["plans"] = plans
            strip_verbose_claude_takeover(plan_payload)
            save_json(paths.planning / "finding" / "plans.json", plan_payload)
        if isinstance(read_payload, dict):
            strip_verbose_claude_takeover(read_payload)
            save_json(paths.planning / "finding" / "read_results.json", read_payload)
    if not idea_schema_ready:
        literature_blockers.append(f"Claude Code takeover must rewrite and objectively score {required_idea_count} ideas: each idea needs detailed new_method, initial_experiment, inspired_by, objective_scores, score, idea_score, and completed Task/subagent scoring audit; stale candidate/base-switch bindings are rejected.")
        literature_blockers.extend(_idea_contract_issue_summary_zh(idea_contract_issues or raw_idea_contract_issues))
    if not plan_schema_ready:
        literature_blockers.append("Claude Code takeover must rewrite plans without preselecting a concrete repo/base/data path/training command; Environment owns base selection after current candidate audit.")
        literature_blockers.extend(_plan_contract_issue_summary_zh(plan_contract_issues or raw_plan_contract_issues))
    validation_needs_full_text = current_reading_validation_needs_full_text_evidence(validation)
    if not validation_ready:
        validation_blockers = [str(item) for item in as_list(validation.get("blockers")) if str(item).strip()]
        literature_blockers.extend(validation_blockers or ["current-Find reading validation has not passed the full-text deep-read policy."])
    if not counts_ready and not validation_needs_full_text:
        literature_blockers.append(f"Claude Code takeover must read exactly the current Read-stage packet entries ({expected_recommendation_readings}), generate {required_idea_count} ideas/plans, and record at least 3 supplemental search topics.")
    failure_type = ""
    next_required_action = "environment_base_selection_and_repo_data_protocol_audit"
    if literature_gate_blocked:
        plan_status = "blocked_literature_recommendation_gate"
        failure_type = "literature_recommendation_shortfall"
        next_required_action = "repair_current_find_literature_scoring_packet"
    elif ready:
        plan_status = "claude_current_find_read_idea_plan_ready_waiting_for_environment_base_selection"
    elif validation_needs_full_text:
        plan_status = "blocked_current_find_full_text_evidence_pending"
        failure_type = "full_text_evidence_missing"
        next_required_action = "acquire_current_find_full_text_evidence"
    elif not validation_ready:
        plan_status = "blocked_current_find_deep_read_validation_pending"
        failure_type = "claude_deep_read_rewrite_required"
        next_required_action = "rerun_current_find_claude_takeover_repair_deep_read_synthesis"
    elif not idea_schema_ready:
        plan_status = "blocked_current_find_idea_contract_failed"
        failure_type = "idea_contract_failed"
        next_required_action = "rerun_current_find_claude_takeover_rewrite_and_score_ideas"
    elif not plan_schema_ready:
        plan_status = "blocked_current_find_plan_contract_failed"
        failure_type = "plan_contract_failed"
        next_required_action = "rerun_current_find_claude_takeover_rewrite_plans_without_preselected_base"
    else:
        plan_status = "blocked_claude_current_find_incomplete"
        failure_type = "current_find_read_idea_plan_contract_incomplete"
        next_required_action = "rerun_current_find_claude_takeover_repair"
    if literature_gate_blocked:
        base_selection_status = "blocked_by_literature_gate"
        next_required_stage = "repair_current_find_literature_scoring_packet"
    elif validation_needs_full_text:
        base_selection_status = "blocked_by_current_find_full_text_evidence"
        next_required_stage = "acquire_current_find_full_text_evidence"
    elif not validation_ready:
        base_selection_status = "blocked_by_current_find_reading_validation"
        next_required_stage = "repair_current_find_claude_reading_validation"
    elif not idea_schema_ready:
        base_selection_status = "blocked_by_current_find_idea_contract"
        next_required_stage = "rerun_current_find_claude_takeover_rewrite_and_score_ideas"
    elif not plan_schema_ready:
        base_selection_status = "blocked_by_current_find_plan_contract"
        next_required_stage = "rerun_current_find_claude_takeover_rewrite_plans_without_preselected_base"
    elif not counts_ready:
        base_selection_status = "blocked_by_current_find_read_idea_plan_contract"
        next_required_stage = "repair_current_find_claude_read_idea_plan"
    else:
        base_selection_status = "waiting_for_environment_claude_code"
        next_required_stage = "environment_base_selection_and_repo_data_protocol_audit"
    blocked_until = [
        "当前 Find 强推荐门控达到目标",
        "Environment 阶段验证并锁定候选基底",
        "主线 repo/data/env 合同通过",
        "baseline 与候选方法复现审计通过",
        "reference/scientific/evidence gates 通过",
    ] if literature_gate_blocked else [
        "Environment 阶段验证并锁定候选基底",
        "主线 repo/data/env 合同通过",
        "baseline 与候选方法复现审计通过",
        "reference/scientific/evidence gates 通过",
    ]
    execution_selection = apply_current_find_execution_selection(ideas, plans, source=state_source, executable=execution_ready)
    selection_fields = {key: execution_selection.get(key) for key in CURRENT_FIND_SELECTION_FIELD_KEYS}
    if selected_contract_ready_for_run:
        selection_fields = _apply_contract_selection_fields(selection_fields, selected_contract)
    selected_execution_issue = "" if selected_contract_ready_for_run and content_ready else (selection_issue or str(execution_selection.get("selection_issue") or ""))
    if content_ready and selected_execution_issue:
        plan_status = "blocked_ambiguous_selected_plan" if selected_execution_issue == "ambiguous_selected_plan" else "blocked_missing_selected_plan"
        failure_type = selected_execution_issue
        next_required_action = current_find_contract_next_required_action(validation, {"selected_execution_issue": selected_execution_issue})
        base_selection_status = plan_status
        next_required_stage = next_required_action
        if selected_execution_issue == "ambiguous_selected_plan":
            literature_blockers.append("current Find Read/Idea/Plan artifacts are ready, but multiple plans were explicitly selected; main Claude Code must choose exactly one selected_plan_id")
        else:
            literature_blockers.append("current Find Read/Idea/Plan artifacts are ready, but no explicit selected_plan_id was written by main Claude Code")
    payload.update({
        "project": project,
        "generated_at": now_iso(),
        "source": state_source,
        "run_id": run_id,
        "status": plan_status,
        "content_ready": content_ready,
        "read_idea_plan_ready": content_ready,
        "execution_ready": execution_ready,
        "takeover_ready": execution_ready,
        "claude_current_find_ready": content_ready,
        "selected_execution_issue": selected_execution_issue,
        "current_find_reading_count": len(readings),
        "current_find_idea_count": len(ideas),
        "current_find_plan_count": len(plans),
        "idea_schema_ready": idea_schema_ready,
        "plan_schema_ready": plan_schema_ready,
        "idea_contract_issues": idea_contract_issues[:20],
        "raw_idea_contract_issues": raw_idea_contract_issues[:20],
        "plan_contract_issues": plan_contract_issues[:20],
        "raw_plan_contract_issues": raw_plan_contract_issues[:20],
        "positive_anchor_readings": positive_anchor_readings,
        "critique_or_boundary_readings": critique_or_boundary_readings,
        "invalid_positive_readings": invalid_positive_readings,
        "reading_validation": validation,
        "readings": readings,
        "targeted_search_queries": targeted_queries,
        "targeted_search_query_count": len(targeted_queries),
        "targeted_search_tool_status": {
            "status": targeted_tool.get("status"),
            "venue": targeted_tool.get("venue"),
            "packet_return_code": targeted_tool.get("packet_return_code"),
            "return_codes": targeted_tool.get("return_codes"),
            "failure_summary": targeted_tool.get("failure_summary"),
            "guardrail": targeted_tool.get("guardrail"),
        },
        "literature_gate": literature_gate,
        "blockers": literature_blockers,
        "failure_type": failure_type,
        "next_required_action": next_required_action,
        "base_selection_status": base_selection_status,
        "base_selection_policy": "Find/Read/Idea/Plan can expose multiple candidates, but only one explicit selected_plan_id from main Claude Code or human supervision may drive environment, experiment, paper, and claim execution. Environment-stage Claude Code must still write evidence_ready_repo_selection.json for the current run before repo/data/command execution.",
        "next_required_stage": next_required_stage,
        "allowed_actions": literature_repair_commands if literature_gate_blocked else [],
        "literature_repair_policy": "targeted_find_allowed" if literature_gate_blocked else "literature_gate_cleared",
        "blocked_until": blocked_until,
        "ideas": ideas,
        "plans": plans,
        **selection_fields,
        "claude_takeover": {key: takeover.get(key) for key in ["status", "return_code", "started_at", "finished_at", "prompt_path"]},
        "guardrails": [
            "Read/Idea/Plan were delegated to Claude Code under TASTE control, not generated from deterministic templates.",
            "Strong recommendations must remain strict; weak or critique papers cannot become claim evidence.",
            "Plans are literature/repo planning evidence only until repo/data/env/experiment gates pass.",
        ],
    })
    payload = _sync_current_find_plan_reading_validation(payload, validation)
    save_json(paths.state / "current_find_research_plan.json", payload)
    save_json(paths.state / "idea_candidates.json", {"generated_at": now_iso(), "project": project, "source": state_source, "current_find_run_id": run_id, "ideas": ideas, **selection_fields, "summary": {"idea_count": len(ideas), "pursue_count": sum(1 for row in ideas if str(row.get("recommendation", row.get("status", ""))).startswith(("pursue", "approved"))), "current_find_run_id": run_id}})
    experiment_plan = load_json(paths.state / "experiment_plan.json", {})
    if not isinstance(experiment_plan, dict):
        experiment_plan = {}
    experiment_plan.update({
        "project": project,
        "source": state_source,
        "run_id": run_id,
        "status": payload["status"],
        "content_ready": content_ready,
        "read_idea_plan_ready": content_ready,
        "execution_ready": execution_ready,
        "takeover_ready": execution_ready,
        "claude_current_find_ready": content_ready,
        "failure_type": failure_type,
        "next_required_action": next_required_action,
        "next_required_stage": next_required_stage,
        "base_selection_status": base_selection_status,
        "selected_execution_issue": selected_execution_issue,
        "current_find_reading_count": len(readings),
        "current_find_idea_count": len(ideas),
        "current_find_plan_count": len(plans),
        "idea_schema_ready": idea_schema_ready,
        "plan_schema_ready": plan_schema_ready,
        "idea_contract_issues": idea_contract_issues[:20],
        "raw_idea_contract_issues": raw_idea_contract_issues[:20],
        "plan_contract_issues": plan_contract_issues[:20],
        "raw_plan_contract_issues": raw_plan_contract_issues[:20],
        "positive_anchor_readings": positive_anchor_readings,
        "critique_or_boundary_readings": critique_or_boundary_readings,
        "invalid_positive_readings": invalid_positive_readings,
        "reading_validation": validation,
        "ideas": ideas,
        "plans": plans,
        "targeted_search_queries": targeted_queries,
        "targeted_search_query_count": len(targeted_queries),
        "literature_gate": literature_gate,
        "blockers": literature_blockers,
        "allowed_actions": literature_repair_commands if literature_gate_blocked else [],
        **selection_fields,
    })
    experiment_plan = _sync_current_find_plan_reading_validation(experiment_plan, validation)
    save_json(paths.state / "experiment_plan.json", experiment_plan)
    return payload



def _paper_public_ref(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "paper_id": first_text(row, "id", "paper_id") or re.sub(r"[^a-zA-Z0-9]+", "_", str(row.get("title") or "paper").lower()).strip("_")[:80],
        "title": first_text(row, "title") or "Untitled",
        "venue": first_text(row, "venue", "source"),
        "year": first_text(row, "year", "published", "updated"),
        "url": first_text(row, "url", "abs_url"),
        "pdf_url": first_text(row, "pdf_url"),
        "evidence_role": row.get("evidence_role") or "",
        "evidence_tier": row.get("evidence_tier") or "",
    }


def _select_guarded_audit_papers(find_results: dict[str, Any], positive_ids: set[str], limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    pools = ["triage_candidates", "audit_candidates", "critique_candidates", "evaluated_candidates", "title_candidates", "retrieval_candidates", "arxiv_prefiltered"]
    for pool in pools:
        for index, raw in enumerate(as_list(find_results.get(pool)), 1):
            if not isinstance(raw, dict):
                continue
            ids = _identity_values(raw)
            if ids & positive_ids:
                continue
            key = _paper_key(raw)
            if not key or key in seen:
                continue
            title = first_text(raw, "title")
            if not title:
                continue
            row = dict(raw)
            row["taste_pool"] = pool
            row["taste_pool_role"] = "audit_or_search_expansion"
            row["taste_pool_rank"] = index
            row["not_positive_support"] = True
            row["weak_candidate_for_critique"] = True
            tier = str(row.get("evidence_tier") or "").strip()
            if not tier or tier == "strong_recommendation":
                row["evidence_tier"] = "critique_or_boundary_case"
            row["support_policy"] = "audit_or_search_expansion_only_not_positive_claim_evidence"
            rows.append(row)
            seen.add(key)
            if len(rows) >= limit:
                return rows
    return rows


def _guarded_reading_from_positive(row: dict[str, Any], rank: int, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    item = build_reading(row, cfg)
    if not item.get("claim_ready_anchor"):
        item.update({
            "verdict": "recommended_reading_boundary",
            "support_role": "boundary_audit",
            "claim_ready_anchor": False,
            "positive_claim_evidence": False,
            "not_positive_support": True,
            "weak_candidate_for_critique": True,
            "evidence_role": "contrast_or_boundary",
            "audit_rank": rank,
        })
        return item
    role = str(row.get("evidence_role") or "direct_target").strip() or "direct_target"
    item.update({
        "verdict": "core_reading" if role == "direct_target" else "method_reference",
        "support_role": "core_method_reference" if role == "direct_target" else "transferable_method_reference",
        "critique_reason": "",
        "claim_ready_anchor": True,
        "positive_claim_evidence": True,
        "evidence_role": role,
        "evidence_tier": row.get("evidence_tier") or "strong_recommendation",
        "strong_anchor_rank": rank,
    })
    return item


def _guarded_reading_from_audit(row: dict[str, Any], rank: int, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    item = build_reading(row, cfg)
    reason = compact(first_text(row, "recommendation_note_zh", "recommendation_note", "reason_zh", "reason", "fit_explanation_zh", "fit_explanation"), 900)
    item.update({
        "verdict": "contrast_or_boundary_reading",
        "support_role": "contrast_or_boundary_reference",
        "critique_reason": reason or "该论文更适合作为边界、对照或补充阅读，用于说明相邻方法与当前主题的差异。",
        "claim_ready_anchor": False,
        "positive_claim_evidence": False,
        "not_positive_support": True,
        "weak_candidate_for_critique": True,
        "evidence_role": "contrast_or_boundary",
        "evidence_tier": row.get("evidence_tier") or "critique_or_boundary_case",
        "audit_rank": rank,
    })
    return item


def _load_best_claude_payload(paths, run_id: str, filename: str) -> dict[str, Any]:
    candidates = [
        paths.planning / "finding" / filename,
        RUNS_DIR / run_id / filename,
        LEGACY_RUNS_DIR / run_id / filename,
    ]
    best: dict[str, Any] = {}
    best_mtime = -1.0
    for path in candidates:
        payload = load_json(path, {})
        if not isinstance(payload, dict):
            continue
        payload_run_id = str(payload.get("run_id") or payload.get("current_find_run_id") or "").strip()
        if payload_run_id and payload_run_id != str(run_id or "").strip():
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        rows = payload.get("ideas") or payload.get("plans") or payload.get("readings") or []
        if isinstance(rows, list) and rows and mtime >= best_mtime:
            best = payload
            best_mtime = mtime
    return best



def _contract_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else str(value or "")


def _contains_stale_execution_binding(value: Any) -> bool:
    text = _contract_text(value).lower()
    return any(pattern in text for pattern in STALE_EXECUTION_BINDING_PATTERNS)


def _binding_view(row: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    return {key: row.get(key) for key in keys if key in row and row.get(key) not in (None, "", [], {})}


def _strip_pre_environment_prohibition_phrases(raw_text: str) -> str:
    if not raw_text:
        return raw_text
    return re.sub(
        r"(?:不写|不得写|禁止写入|严禁写入|不能写入|不要写|must\s+not\s+write|do\s+not\s+write|without\s+writing)[^\n。；;]{0,90}(?:repo_path|local_path|selected_repo_path|active_repo_path|training_script|ready_to_execute|训练命令|运行命令)(?!\s*[:=])",
        "",
        raw_text,
        flags=re.IGNORECASE,
    )


def _contains_pre_environment_base_binding(value: Any) -> bool:
    raw_text = _contract_text(value)
    detection_text = _strip_pre_environment_prohibition_phrases(raw_text)
    text = detection_text.lower()
    if _contains_stale_execution_binding(value):
        return True
    if any(marker in text for marker in PRE_ENV_BASE_BINDING_LITERAL_MARKERS):
        return True
    return any(pattern.search(detection_text) for pattern in PRE_ENV_BASE_BINDING_REGEXES)


def _safe_plan_text(value: Any, fallback: str, limit: int = 900) -> str:
    text = compact(value, limit)
    if not text or _contains_stale_execution_binding(text):
        return fallback
    return text


def _safe_list(values: Any, fallback: list[str]) -> list[str]:
    out: list[str] = []
    for value in as_list(values):
        text = _safe_plan_text(value, "")
        if text:
            out.append(text)
    return out or fallback



def _sanitize_idea(row: dict[str, Any], idx: int, strong_refs: list[dict[str, Any]], audit_refs: list[dict[str, Any]], queries: list[str]) -> dict[str, Any]:
    raw_title = first_text(row, "title")
    fallback_title = f"current Find guarded idea {idx}: candidate repo/base proposal ready for Environment validation"
    title = _safe_plan_text(raw_title, fallback_title, 220)
    raw_status = str(row.get("status") or row.get("recommendation") or "approved_for_planning").strip() or "approved_for_planning"
    if raw_status.lower() in {"pursue", "watch", "ready", "ready_to_execute"} or _contains_stale_execution_binding(raw_status):
        raw_status = "approved_for_planning"
    new_method = _safe_plan_text(row.get("new_method") or row.get("hypothesis"), "待项目代理根据精读结果补齐详细新方法。", 1600)
    method_details = _safe_plan_text(row.get("method_details") or row.get("mechanism") or row.get("rationale"), "待项目代理根据精读结果补齐方法机制。", 1800)
    initial_experiment = _safe_plan_text(row.get("initial_experiment") or row.get("experiment_design") or row.get("experimental_design") or row.get("min_experiment") or row.get("minimum_experiment"), "", 1800)
    if _generic_idea_experiment(initial_experiment):
        initial_experiment = ""
    initial_experiment_required = not bool(initial_experiment)
    if initial_experiment_required and raw_status.lower().startswith("approved"):
        raw_status = "blocked_with_reason"
    inspired_by = _normalize_inspired_refs(row.get("inspired_by"), as_list(row.get("supporting_papers") or row.get("positive_anchor_papers")) or strong_refs)
    return {
        "id": row.get("id") or row.get("idea_id") or f"idea-current-find-guarded-{idx:03d}",
        "idea_id": row.get("idea_id") or row.get("id") or f"idea-current-find-guarded-{idx:03d}",
        "title": title,
        "status": raw_status,
        "recommendation": "candidate_repo_base_proposal_ready_for_environment_validation",
        "source": CLAUDE_TAKEOVER_SOURCE,
        "score": row.get("score") or row.get("idea_score") or 0,
        "idea_score": row.get("idea_score") or row.get("score") or 0,
        "new_method": new_method,
        "hypothesis": new_method,
        "method_details": method_details,
        "mechanism": method_details,
        "initial_experiment": initial_experiment,
        "min_experiment": initial_experiment,
        "minimum_experiment": initial_experiment,
        "initial_experiment_required": initial_experiment_required,
        "blocked_reason": "项目代理尚未根据精读结果补齐初步实验。" if initial_experiment_required else row.get("blocked_reason", ""),
        "inspired_by": inspired_by,
        "inspired_by_text": _inspired_refs_text(inspired_by),
        "implementation_target": {
            "selection_stage": "read_idea_plan_candidate_proposal",
            "status": "candidate_pending_environment_validation",
            "candidate_repo_hint": first_text(row, "candidate_repo", "repo", "repo_url", "base_repo") or "to be proposed from current Find/Read evidence",
            "dataset_contract": {"status": "candidate_pending_environment_validation"},
        },
        "bad_case_slice": _safe_list(row.get("bad_case_slice"), ["cold-start/sparse cases", "long-tail cases", "high-confidence errors", "semantic-behavior conflict cases"]),
        "success_gate": _safe_list(row.get("success_gate"), ["Environment validates the proposed repo/base, data, and protocol", "repo/data/env/protocol gate passed", "metrics and bad cases written", "evidence gates refreshed"]),
        "supporting_papers": strong_refs,
        "positive_anchor_papers": strong_refs,
        "audit_context_papers": audit_refs[:5],
        "targeted_search_queries": queries[:8],
        "evidence_policy": "Positive support is restricted to current strong_recommendations/articles. Audit/boundary papers can inspire stress tests or search expansion but cannot be claim evidence.",
        "claude_code_tasks": _safe_list(row.get("claude_code_tasks"), [
            "Read current Find strong recommendations, readings, ideas, plans, and targeted search notes.",
            "Validate the candidate repos/base works proposed by Idea/Plan, including data, protocol, and reproducibility evidence.",
            "Lock the candidate repo/base only after Environment validation proves repo/data/protocol evidence is ready.",
        ]),
        "guardrail": "Idea came from Claude Code under TASTE control and was normalized by the current-Find evidence guard; it proposes candidate repos/base works for Environment validation before any experiment execution.",
    }


def _generic_plan_steps(steps: Any) -> bool:
    rows = as_list(steps)
    if not rows:
        return True
    joined = "\n".join(str(row or "") for row in rows).lower()
    generic_markers = [
        "verify current find run_id",
        "environment-stage claude code reads",
        "accept a base only by writing",
        "evidence_ready_repo_selection.json",
        "refresh reference/scientific/evidence/submission gates",
        "run minimal baseline/candidate/ablation experiments",
    ]
    hits = sum(1 for marker in generic_markers if marker in joined)
    metric_pattern = re.search(r"\b(?:ndcg|hr|recall|mrr)@\d+\b", joined)
    return hits >= 2 and not bool(metric_pattern)


def _plan_specific_steps(initial_experiment: str, new_method: str) -> list[str]:
    experiment = compact(initial_experiment, 700)
    method = compact(new_method, 500)
    steps: list[str] = []
    if experiment:
        steps.append(f"以该初步实验作为执行合同：{experiment}")
    if method:
        steps.append(f"实现新方法的最小可测改动，并把改动点限定在：{method}")
    steps.extend([
        "环境阶段先确认基底代码、数据集、负采样/切分、指标解析和随机种子均可审计；未通过则保持 blocked。",
        "在同一数据、同一 seed、同一指标下运行 baseline、candidate 和关键 ablation，分别保存命令、配置、日志和指标 JSON。",
        "按初步实验中定义的坏例切片解析失败样本，输出坏例表、反例压力测试和停止/继续理由。",
        "刷新实验记录、科学进展、论文证据和投稿准备度门控；只有本地证据通过后才允许论文结论提升。",
    ])
    return steps



def _normalized_plan_selection_fields(row: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    selection_payload: dict[str, Any] = {}
    for key in ["execution_selection", "plans_selection", "plan_selection", "selection", "selected_execution"]:
        value = row.get(key)
        if isinstance(value, dict):
            selection_payload.update(value)
    selected_value = None
    for key in ["selected_for_execution", "execute_next", "selected", "primary", "best_plan"]:
        if key in row:
            selected_value = row.get(key)
            break
    for key in ["selected", "selected_for_execution", "execute_next", "primary", "best_plan"]:
        if selected_value is None and key in selection_payload:
            selected_value = selection_payload.get(key)
            break
    if selected_value is None:
        decision = str(row.get("execution_decision") or row.get("selection_decision") or selection_payload.get("decision") or selection_payload.get("selection_decision") or "").strip().lower()
        if decision in CURRENT_FIND_EXECUTION_TRUE_VALUES:
            selected_value = True
        elif decision in CURRENT_FIND_EXECUTION_FALSE_VALUES:
            selected_value = False
    if selected_value is None:
        return {}
    selected = _execution_truthy(selected_value)
    if not selected and not _execution_falsey(selected_value):
        return {}
    reason = first_text(selection_payload, "reason", "selection_reason", "rationale") or first_text(row, "selection_reason", "selected_reason", "reason")
    selected_by = first_text(selection_payload, "selected_by", "source") or ("main_claude_code_after_deep_read" if selected else "not_selected_candidate_backlog")
    return {
        "selected_for_execution": selected,
        "execute_next": selected,
        "execution_selection": {
            "selected": selected,
            "selected_by": selected_by,
            "reason": reason,
            "source": CLAUDE_TAKEOVER_SOURCE,
        },
    }

def _sanitize_plan(row: dict[str, Any], idea: dict[str, Any], idx: int, strong_refs: list[dict[str, Any]], audit_refs: list[dict[str, Any]], queries: list[str]) -> dict[str, Any]:
    title = _safe_plan_text(first_text(row, "title") or first_text(idea, "title"), f"current Find guarded plan {idx}: candidate repo/base proposal ready for Environment validation", 240)
    plan_id = row.get("plan_id") or f"plan-{idea.get('id') or idx}"
    plan_initial_experiment = _safe_plan_text(row.get("initial_experiment") or row.get("min_experiment") or row.get("minimum_experiment") or idea.get("initial_experiment") or idea.get("min_experiment"), "", 1800)
    if _generic_idea_experiment(plan_initial_experiment):
        plan_initial_experiment = ""
    new_method = _safe_plan_text(row.get("new_method") or idea.get("new_method") or idea.get("hypothesis"), "", 1800)
    method_details = _safe_plan_text(row.get("method_details") or row.get("mechanism") or idea.get("method_details") or idea.get("mechanism"), "", 1800)
    base_steps = _safe_list(row.get("steps") or row.get("experiment_steps"), [])
    if _generic_plan_steps(base_steps):
        base_steps = []
    generic_gate_steps = [
        "核对当前 Find run_id 与受门控保护的 read/idea/plan 产物。",
        "Environment 验证 Idea/Plan 已提出的候选 repo/base、数据协议和复现风险；未通过则保持 blocked。",
        "只有 Environment 对上述候选 repo/base 的 repo/data/protocol 证据审核通过后，才进入仓库、数据和实验执行。",
    ]
    steps = base_steps or _plan_specific_steps(plan_initial_experiment, new_method)
    steps = (steps + generic_gate_steps)[:12]
    inspired_by = _normalize_inspired_refs(row.get("inspired_by") or idea.get("inspired_by"), as_list(row.get("supporting_papers") or idea.get("supporting_papers") or idea.get("positive_anchor_papers")) or strong_refs)
    plan = {
        "plan_id": plan_id,
        "idea_id": idea.get("id") or idea.get("idea_id") or row.get("idea_id") or f"idea-current-find-guarded-{idx:03d}",
        "title": title,
        "source": CLAUDE_TAKEOVER_SOURCE,
        "status": "waiting_for_environment_base_selection",
        "hypothesis": _safe_plan_text(row.get("new_method") or row.get("hypothesis") or idea.get("new_method") or idea.get("hypothesis"), "The idea remains a planning hypothesis until environment-stage base selection and local evidence gates pass."),
        "new_method": new_method,
        "method_details": method_details,
        "initial_experiment": plan_initial_experiment,
        "inspired_by": inspired_by,
        "inspired_by_text": _inspired_refs_text(inspired_by),
        "positive_anchor_papers": strong_refs,
        "audit_context_papers": audit_refs[:5],
        "targeted_search_queries": queries[:8],
        "evidence_policy": "Do not promote claims from literature alone; candidate repo/base proposals must wait for Environment validation before repo/data/command execution.",
        "steps": steps,
        "success_gate": _safe_list(row.get("success_gate") or idea.get("success_gate"), ["Environment validates the proposed repo/base, data, and protocol", "repo/data/protocol evidence ready", "metrics parsed", "bad-case slice written", "audit JSON exists", "scientific gate refreshed"]),
        "versions": [{
            "version": 1,
            "status": "waiting_for_environment_base_selection",
            "generated_at": now_iso(),
            "implementation": {
                "target": idea.get("implementation_target", {}),
                "minimum_experiment": plan_initial_experiment,
                "bad_case_slice": _safe_list(row.get("bad_case_slice") or idea.get("bad_case_slice"), ["cold-start/sparse cases", "long-tail cases", "high-confidence errors"]),
                "success_gate": _safe_list(row.get("success_gate") or idea.get("success_gate"), ["Environment validates the proposed repo/base, data, and protocol", "local evidence gates pass after Environment validation"]),
                "metrics": ["primary task metric", "ranking/retrieval metrics", "bad-case slice metrics", "runtime/budget", "counterexample count"],
                "claude_code_tasks": idea.get("claude_code_tasks", []),
            },
            "final_plan": {
                "experimental_design": plan_initial_experiment,
                "steps": steps,
                "go_no_go": "No repo/data/experiment execution until Environment validates and locks the proposed repo/base with passing gates.",
                "paper_claim_policy": "Only environment/reference/scientific/evidence gates can unlock claim promotion.",
            },
            "llm": {"generator": "claude_code_current_find_takeover_guarded", "evaluator": "current_find_environment_selection_gate"},
        }],
    }
    plan.update(_normalized_plan_selection_fields(row))
    return plan

def _latest_find_results(paths) -> dict[str, Any]:
    payload = load_json(paths.planning / "finding" / "find_results.json", {})
    return payload if isinstance(payload, dict) else {}


def _find_run_changed(paths, expected_run_id: str) -> str:
    latest = _latest_find_results(paths)
    latest_run = str(latest.get("run_id") or "").strip()
    return latest_run if latest_run and latest_run != str(expected_run_id or "").strip() else ""


def _write_find_changed_blocker(paths, old_run_id: str, new_run_id: str, takeover: dict[str, Any]) -> None:
    payload = {
        "status": "blocked_current_find_changed_during_claude_takeover",
        "run_id": new_run_id,
        "previous_run_id": old_run_id,
        "source": CLAUDE_TAKEOVER_SOURCE,
        "generated_at": now_iso(),
        "current_find_reading_count": 0,
        "current_find_idea_count": 0,
        "current_find_plan_count": 0,
        "claude_takeover": takeover,
        "blockers": ["A controlled targeted Find produced a newer run while Claude was preparing Read/Idea/Plan; stale downstream files must not be accepted."],
        "next_required_stage": "rerun_current_find_claude_takeover_for_new_run",
        "guardrail": "Do not promote old Read/Idea/Plan across Find run_id changes. Rerun the current-Find takeover against the new run first.",
    }
    save_json(paths.state / "current_find_research_plan.json", payload)


def normalize_claude_outputs_to_current_find_policy(project: str, paths, run_id: str, find_results: dict[str, Any], takeover: dict[str, Any], read_limit: int, idea_count: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    cfg = load_project_config(project)
    idea_payload = _load_best_claude_payload(paths, run_id, "ideas.json")
    plan_payload = _load_best_claude_payload(paths, run_id, "plans.json")
    read_payload = _load_best_claude_payload(paths, run_id, "read_results.json")
    full_text_packet = load_current_full_text_packet(paths, run_id)
    original_recommendation_rows, recommendation_rows, validation_find_results = _current_reading_validation_view(find_results, full_text_packet, read_limit)
    positive_ids, positive_titles = _current_positive_identities(validation_find_results)
    recommendation_ids, recommendation_titles = _current_recommendation_identities(validation_find_results)
    supplemental_audit_rows: list[dict[str, Any]] = []
    strong_rows = [row for row in recommendation_rows if _identity_values(row) & positive_ids]
    audit_rows = [row for row in recommendation_rows if not (_identity_values(row) & positive_ids)] + supplemental_audit_rows
    packet_index = _full_text_packet_index(full_text_packet)
    current_revision = parse_iso_time(takeover.get("current_find_revision_at") or takeover.get("current_find_revision") or takeover.get("find_results_updated_at"))
    taste_dir = paths.planning / "finding"
    reading_candidates: list[dict[str, Any]] = []
    reading_candidates.extend(_current_find_deep_read_fragment_rows(taste_dir, run_id, current_revision))
    for row in as_list(read_payload.get("readings")):
        if isinstance(row, dict):
            reading_candidates.append(row)
    selected_readings = _select_current_find_readings_from_candidates(reading_candidates, validation_find_results, full_text_packet) if reading_candidates else []
    if selected_readings:
        readings = normalize_readings_full_text_evidence(selected_readings, full_text_packet)
        readings = _enforce_current_find_claim_policy(readings, positive_ids)
    else:
        existing_by_title = {
            norm_title(row.get("title") or row.get("paper_title")): _sanitize_reading_public_fields(row)
            for row in as_list(read_payload.get("readings"))
            if isinstance(row, dict) and norm_title(row.get("title") or row.get("paper_title"))
        }
        readings = []
        for index, row in enumerate(recommendation_rows, 1):
            base = existing_by_title.get(norm_title(row.get("title")))
            if not base:
                base = _guarded_reading_from_positive(row, index, cfg)
            base = _merge_find_abstract_into_reading(base, row)
            reading = normalize_reading_full_text_evidence(base, _matching_full_text_packet_entry(base or row, packet_index))
            readings.append(_sanitize_reading_public_fields(reading))
        readings = _enforce_current_find_claim_policy(readings, positive_ids)
    readings.extend(
        _sanitize_reading_public_fields(normalize_reading_full_text_evidence(_guarded_reading_from_audit(row, index, cfg), _matching_full_text_packet_entry(row, packet_index)))
        for index, row in enumerate(supplemental_audit_rows, 1)
    )
    queries = extract_targeted_search_queries(paths, read_payload, idea_payload, plan_payload, load_json(paths.state / "current_find_research_plan.json", {}))
    if len(queries) < 3:
        queries = []
    strong_refs = [_paper_public_ref(row) for row in strong_rows]
    audit_refs = [_paper_public_ref(row) for row in audit_rows]
    raw_ideas = [row for row in as_list(idea_payload.get("ideas")) if isinstance(row, dict)]
    raw_plans = [row for row in as_list(plan_payload.get("plans")) if isinstance(row, dict)]
    if len(raw_ideas) < idea_count:
        fresh_plan = load_json(paths.state / "fresh_base_implementation_plan.json", {})
        repo = fresh_plan.get("repo", {}) if isinstance(fresh_plan, dict) and isinstance(fresh_plan.get("repo", {}), dict) else {}
        raw_ideas = build_ideas(readings, repo, fresh_plan if isinstance(fresh_plan, dict) else {}, cfg)
    ideas = [_sanitize_idea(row, idx, strong_refs, audit_refs, queries) for idx, row in enumerate(raw_ideas[:idea_count], 1)]
    if len(raw_plans) < idea_count:
        raw_plans = build_plans(ideas, load_json(paths.state / "fresh_base_implementation_plan.json", {}))
    plans = [_sanitize_plan(raw_plans[idx - 1] if idx - 1 < len(raw_plans) else {}, idea, idx, strong_refs, audit_refs, queries) for idx, idea in enumerate(ideas[:idea_count], 1)]
    ideas, plans = enrich_public_projections(ideas, plans)
    generated_at = now_iso()
    read_results = {
        "run_id": run_id,
        "source": CLAUDE_TAKEOVER_SOURCE,
        "generated_at": generated_at,
        "normalization_source": "claude_code_output_guarded_by_current_find_policy",
        "claude_takeover": {key: takeover.get(key) for key in ["status", "return_code", "started_at", "finished_at", "prompt_path"] if key in takeover},
        "readings": readings,
        "positive_anchor_count": len(strong_rows),
        "audit_or_boundary_count": len(audit_rows),
        "targeted_search_queries": queries,
        "guardrail": "Only strong_recommendations/articles are positive anchors; audit rows cannot support claims.",
        "full_text_packet": full_text_packet_summary(full_text_packet),
    }
    idea_results = {
        "run_id": run_id,
        "source": CLAUDE_TAKEOVER_SOURCE,
        "generated_at": generated_at,
        "normalization_source": "claude_code_output_guarded_by_current_find_policy",
        "ideas": ideas,
        "candidate_pool": strong_refs,
        "audit_context": audit_refs,
        "targeted_search_queries": queries,
    }
    plan_results = {
        "run_id": run_id,
        "source": CLAUDE_TAKEOVER_SOURCE,
        "generated_at": generated_at,
        "normalization_source": "claude_code_output_guarded_by_current_find_policy",
        "plans": plans,
        "targeted_search_queries": queries,
    }
    taste_dir = paths.planning / "finding"
    validation_ok, validation = validate_claude_readings_against_current_find(readings, validation_find_results, len(recommendation_rows), paths, run_id)
    execution_selection = apply_current_find_execution_selection(ideas, plans, source=CLAUDE_TAKEOVER_SOURCE, executable=validation_ok)
    selection_fields = {key: execution_selection.get(key) for key in CURRENT_FIND_SELECTION_FIELD_KEYS}
    idea_results.update(selection_fields)
    plan_results.update(selection_fields)
    idea_results["ideas"] = ideas
    plan_results["plans"] = plans
    validation.update({
        "run_id": run_id,
        "source": CLAUDE_TAKEOVER_SOURCE,
        "generated_at": generated_at,
        "normalized": True,
        **_current_reading_validation_metadata(original_recommendation_rows, recommendation_rows, full_text_packet, read_limit),
    })
    save_json(paths.state / "current_find_claude_reading_validation.json", validation)
    if not validation_ok:
        missing_full_text = current_reading_validation_needs_full_text_evidence(validation)
        blocked_payload = {
            "project": project,
            "generated_at": generated_at,
            "source": CLAUDE_TAKEOVER_SOURCE,
            "run_id": run_id,
            "status": "blocked_current_find_full_text_evidence_pending" if missing_full_text else "blocked_current_find_deep_read_validation_pending",
            "failure_type": "full_text_evidence_missing" if missing_full_text else "claude_deep_read_rewrite_required",
            "takeover_ready": False,
            "claude_current_find_ready": False,
            "current_find_reading_count": int(validation.get("full_text_reading_count") or 0),
            "raw_reading_count": len(readings),
            "current_find_idea_count": 0 if not missing_full_text else len(ideas),
            "current_find_plan_count": 0 if not missing_full_text else len(plans),
            "raw_idea_count": len(ideas),
            "raw_plan_count": len(plans),
            "targeted_search_queries": queries,
            "targeted_search_query_count": len(queries),
            "reading_validation": validation,
            "blockers": validation.get("blockers", []),
            "base_selection_status": "blocked_by_current_find_full_text_evidence" if missing_full_text else "blocked_by_current_find_reading_validation",
            "next_required_stage": "acquire_current_find_full_text_evidence" if missing_full_text else "repair_current_find_claude_reading_validation",
            "next_required_action": "acquire_current_find_full_text_evidence" if missing_full_text else "rerun_current_find_claude_takeover_repair_deep_read_synthesis",
            "policy": "Normalization preserves same-run Claude readings and full-text packet evidence, but must not mark takeover ready unless every Read-stage packet paper has both full-text evidence and non-placeholder deep-read synthesis.",
            "full_text_packet": full_text_packet_summary(full_text_packet),
            **selection_fields,
        }
        if not missing_full_text:
            display_read_results = {
                **read_results,
                "status": "blocked_full_text_pending",
                "display_only": True,
                "takeover_ready": False,
                "claude_current_find_ready": False,
                "reading_validation": validation,
            }
            display_idea_results = {
                **idea_results,
                "status": "blocked_full_text_pending",
                "display_only": True,
                "takeover_ready": False,
                "claude_current_find_ready": False,
                "reading_validation": validation,
            }
            display_plan_results = {
                **plan_results,
                "status": "blocked_full_text_pending",
                "display_only": True,
                "takeover_ready": False,
                "claude_current_find_ready": False,
                "reading_validation": validation,
            }
            save_json(taste_dir / "read_results.json", strip_verbose_claude_takeover(display_read_results))
            save_json(taste_dir / "ideas.json", strip_verbose_claude_takeover(display_idea_results))
            save_json(taste_dir / "plans.json", strip_verbose_claude_takeover(display_plan_results))
            write_text(taste_dir / "read.md", render_read_md(readings, run_id))
            write_text(taste_dir / "idea.md", render_idea_md(ideas, run_id, _paper_reference_index(readings)))
            write_text(taste_dir / "plan.md", render_plan_md(plans, run_id, _paper_reference_index(readings)))
            copy_to_taste_run(paths, run_id, ["read_results.json", "ideas.json", "plans.json", "read.md", "idea.md", "plan.md"])
        save_json(paths.state / "current_find_research_plan.json", blocked_payload)
        return [], [], [], blocked_payload
    save_json(taste_dir / "read_results.json", read_results)
    save_json(taste_dir / "ideas.json", idea_results)
    save_json(taste_dir / "plans.json", plan_results)
    write_text(taste_dir / "read.md", render_read_md(readings, run_id))
    write_text(taste_dir / "idea.md", render_idea_md(ideas, run_id, _paper_reference_index(readings)))
    write_text(taste_dir / "plan.md", render_plan_md(plans, run_id, _paper_reference_index(readings)))
    copy_to_taste_run(paths, run_id, ["read_results.json", "ideas.json", "plans.json", "read.md", "idea.md", "plan.md"])
    state_payload = ensure_claude_plan_state(project, paths, run_id, readings, ideas, plans, {**takeover, "normalized": True, "normalization_source": "current_find_policy_guard"}, idea_count=idea_count)
    update_literature_packet(paths, run_id, readings, ideas, plans)
    update_frontend_state(paths, run_id, readings, ideas, plans)
    return readings, ideas, plans, state_payload

def _plan_latest_final(plan: dict[str, Any]) -> dict[str, Any]:
    versions = plan.get("versions") if isinstance(plan.get("versions"), list) else []
    for version in reversed(versions):
        if not isinstance(version, dict):
            continue
        final = version.get("final_plan")
        if isinstance(final, dict):
            return final
    return {}


def _render_plan_refs(lines: list[str], refs: Any, paper_index: dict[str, dict[str, Any]] | None = None) -> None:
    rows = _normalize_inspired_refs([], as_list(refs), paper_index)
    if not rows:
        return
    lines.append("### 启发来源\n")
    for ref in rows:
        title = compact(ref.get("title") or ref.get("name") or ref.get("paper_title"), 180)
        meta = " ".join(str(ref.get(key) or "").strip() for key in ["source", "year"] if str(ref.get(key) or "").strip())
        reason = compact(ref.get("reason") or ref.get("evidence_role"), 260)
        url = compact(ref.get("url") or ref.get("pdf_url"), 220)
        tail = ""
        if meta:
            tail += f"（{meta}）"
        if reason:
            tail += f"：{reason}"
        if url:
            tail += f"（{url}）"
        lines.append(f"- {title}{tail}\n")
    lines.append("\n")

def _plan_display_text(plan: dict[str, Any], *keys: str, limit: int = 2600) -> str:
    for key in keys:
        value = plan.get(key)
        if value in (None, "", []):
            continue
        if isinstance(value, list):
            text = "；".join(compact(item, 700) for item in value if str(item or "").strip())
        elif isinstance(value, dict):
            text = "；".join(f"{k}: {compact(v, 700)}" for k, v in value.items() if str(v or "").strip())
        else:
            text = compact(value, limit)
        if text:
            return text
    return ""


def _render_plan_text_section(lines: list[str], title: str, text: str) -> None:
    if not text:
        return
    lines.extend([f"### {title}\n", f"{text}\n\n"])


def _render_plan_list_section(lines: list[str], title: str, values: Any) -> None:
    items = [compact(item, 900) for item in as_list(values) if str(item or "").strip()]
    if not items:
        return
    lines.append(f"### {title}\n")
    for item in items:
        lines.append(f"- {_protect_inline_math(_ensure_zh_sentence_end(item))}\n")
    lines.append("\n")


def _public_plan_title(plan: dict[str, Any], index: int) -> str:
    title = compact(plan.get("title") or plan.get("name"), 240)
    return title or f"计划 {index}"


def _public_selection_title(row: Any, fallback: str = "已选择") -> str:
    if not isinstance(row, dict):
        return fallback
    title = compact(row.get("title") or row.get("name") or row.get("summary"), 240)
    return title or fallback


def render_plan_md(plans: list[dict[str, Any]], run_id: str, paper_index: dict[str, dict[str, Any]] | None = None) -> str:
    lines = ["# 当前 Find 驱动 Plans\n\n", f"- run_id: `{run_id}`\n", f"- plans: {len(plans)}\n"]
    selected_plan_index, selected_plan = next(
        ((idx, plan) for idx, plan in enumerate(plans, 1) if isinstance(plan, dict) and plan.get("selected_for_execution") is True),
        (0, None),
    )
    if selected_plan:
        lines.extend([
            f"- 当前执行计划: {_public_plan_title(selected_plan, selected_plan_index or 1)}\n",
            "- execution_policy: 后续环境、实验、写作和 claim 只能消费该执行计划；其他 plan 是候选池。\n",
        ])
    lines.append("\n")
    for idx, plan in enumerate(plans, 1):
        final = _plan_latest_final(plan)
        new_method = _plan_display_text(plan, "new_method", "hypothesis", "method_design", limit=2600)
        method_details = _plan_display_text(plan, "method_details", "mechanism", limit=2600)
        method_comparison = _plan_display_text(plan, "method_comparison", "method_positioning", "literature_comparison", limit=3200)
        environment_phase = _plan_display_text(plan, "environment_phase", "environment_plan", "base_selection_plan", limit=2400)
        repo_data_audit = _plan_display_text(plan, "repo_data_audit", "repo_audit", "data_audit", "protocol_audit", limit=3200)
        initial_experiment = _plan_display_text(
            plan,
            "initial_experiment",
            "minimal_experiment",
            "minimum_experiment",
            "min_experiment",
            limit=3200,
        ) or compact(final.get("experimental_design") or final.get("minimum_experiment"), 3200)
        baseline_and_ablation = _plan_display_text(plan, "baseline_and_ablation", "baselines", "ablation", "controls", limit=3200)
        bad_case_slice = _plan_display_text(plan, "bad_case_slice", "bad_case_slices", "failure_slices", limit=2400)
        stop_condition = _plan_display_text(plan, "stop_condition", "failure_stop_condition", "negative_result_policy", limit=2200)
        steps = as_list(final.get("steps") or plan.get("steps") or plan.get("experiment_steps"))
        if _generic_plan_steps(steps):
            steps = _plan_specific_steps(initial_experiment, new_method or method_comparison)
        metadata = [
            f"- status: {plan.get('status')}\n" if str(plan.get("status") or "").strip() else "",
            f"- selected_for_execution: {bool(plan.get('selected_for_execution'))}\n",
        ]
        go_no_go = str(final.get("go_no_go") or plan.get("go_no_go") or "").strip()
        if go_no_go and go_no_go.lower() not in {"none", "null", "n/a", "na"}:
            metadata.append(f"- go_no_go: {go_no_go}\n")
        lines.extend([
            f"## {idx}. {plan.get('title')}\n\n",
            *[item for item in metadata if item],
            "\n",
        ])
        _render_plan_text_section(lines, "环境阶段与前置门控", environment_phase)
        _render_plan_text_section(lines, "新方法", new_method)
        _render_plan_text_section(lines, "方法细节", method_details)
        _render_plan_text_section(lines, "方法对比", method_comparison)
        _render_plan_text_section(lines, "仓库 / 数据 / 协议审计", repo_data_audit)
        _render_plan_text_section(lines, "初步实验", initial_experiment)
        _render_plan_text_section(lines, "Baseline / Control / Ablation", baseline_and_ablation)
        _render_plan_text_section(lines, "Bad-Case Slice", bad_case_slice)
        _render_plan_refs(lines, plan.get("inspired_by") or plan.get("supporting_papers") or plan.get("positive_anchor_papers"), paper_index)
        if steps:
            lines.append("### 执行步骤\n")
            for step in steps:
                lines.append(f"- {compact(step, 900)}\n")
            lines.append("\n")
        success_gate = as_list(plan.get("success_gate"))
        if success_gate or stop_condition:
            lines.append("### 成功 / 停止门控\n")
            for gate in success_gate:
                lines.append(f"- 成功门控：{compact(gate, 500)}\n")
            if stop_condition:
                lines.append(f"- 停止条件：{stop_condition}\n")
            lines.append("\n")
        _render_plan_list_section(lines, "约束", plan.get("guardrails") or plan.get("guardrail"))
    return "".join(lines)

def paper_quality_from_readings(readings: list[dict[str, Any]], cfg: dict[str, Any], reference_time: dt.datetime, run_id: str) -> dict[str, Any]:
    policy = build_literature_policy(cfg)
    papers = []
    for row in readings:
        meta = {"paper_id": row.get("paper_id"), "title": row.get("title"), "source": "taste_current_find", "summary": row.get("abstract_zh") or row.get("summary"), "published": row.get("year"), "updated": row.get("year"), "venue": row.get("venue"), "url": row.get("url"), "abs_url": row.get("url"), "pdf_url": row.get("pdf_url"), "citations": 0, "taste_score": row.get("score"), "taste_reason": row.get("relevance"), "not_positive_support": not bool(row.get("claim_ready_anchor"))}
        scored = score_paper(meta, cfg, reference_time=reference_time)
        signals = row.get("signals", {}) if isinstance(row.get("signals"), dict) else {}
        matched_topic_groups = int(signals.get("matched_topic_group_count") or 0)
        required_topic_groups = len(signals.get("required_topic_groups") or []) if isinstance(signals.get("required_topic_groups"), list) else 0
        novelty = "high" if required_topic_groups and matched_topic_groups >= required_topic_groups else "medium" if matched_topic_groups else "low"
        top_ready = "promising" if row.get("claim_ready_anchor") and numeric(row.get("score")) >= 7.0 else "watch" if row.get("claim_ready_anchor") else "weak"
        scored.update({"paper_id": row.get("paper_id"), "title": row.get("title"), "source": "taste_current_find", "run_id": run_id, "venue": row.get("venue"), "year": row.get("year"), "url": row.get("url"), "novelty": novelty, "claim_strength": "medium" if row.get("abstract_available") else "low", "counterexample_pressure": "medium", "taste": "high" if top_ready == "promising" else "medium", "broad_claim": False, "top_tier_readiness": top_ready, "concerns": ["需要全文/代码/数据审计确认，不能直接作为本地实验 claim。"], "next_checks": row.get("implementation_implications", []) or ["读取论文与代码，建立实验合同。"], "discovery_priority_score": max(numeric(scored.get("discovery_priority_score")), numeric(row.get("score"))), "idea_worthiness_score": max(numeric(scored.get("idea_worthiness_score")), numeric(row.get("score")) + (2.0 if top_ready == "promising" else 0.0)), "high_quality_recent": top_ready == "promising", "not_positive_support": not bool(row.get("claim_ready_anchor"))})
        papers.append(scored)
    papers.sort(key=lambda row: (-numeric(row.get("idea_worthiness_score")), str(row.get("title", "")).lower()))
    summary = {"paper_count": len(papers), "recent_high_priority_count": sum(1 for row in papers if row.get("top_tier_readiness") == "promising"), "recent_candidate_count": sum(1 for row in papers if row.get("top_tier_readiness") == "watch"), "older_foundational_count": 0, "deprioritized_count": sum(1 for row in papers if row.get("top_tier_readiness") == "weak"), "promising_count": sum(1 for row in papers if row.get("top_tier_readiness") == "promising"), "current_find_run_id": run_id}
    return {"generated_at": reference_time.isoformat(), "reference_time": reference_time.isoformat(), "source": "current_find_bridge", "run_id": run_id, "literature_policy": policy, "summary": summary, "papers": papers}


def render_paper_quality_md(payload: dict[str, Any]) -> str:
    lines = ["# Paper Quality Assessment\n\n", f"- source: {payload.get('source')}\n", f"- run_id: `{payload.get('run_id')}`\n"]
    summary = payload.get("summary", {}) if isinstance(payload.get("summary"), dict) else {}
    for key in ["paper_count", "recent_high_priority_count", "recent_candidate_count", "promising_count", "deprioritized_count"]:
        lines.append(f"- {key}: {summary.get(key, 0)}\n")
    lines.append("\n## 当前 Find 高质量论文\n")
    for row in payload.get("papers", [])[:20]:
        lines.append(f"- {row.get('top_tier_readiness')} | idea={row.get('idea_worthiness_score')} | {row.get('venue')} {row.get('year')} | {row.get('title')}\n")
    return "".join(lines)


def build_execution_plan(project: str, cfg: dict[str, Any], run_id: str, readings: list[dict[str, Any]], ideas: list[dict[str, Any]], plans: list[dict[str, Any]], fresh_plan: dict[str, Any]) -> dict[str, Any]:
    venue = project_target_venue(project, str(cfg.get("target_venue") or cfg.get("venue") or "ICLR"))
    paths = build_paths(project)
    progress = load_json(paths.planning / "finding" / "find_progress.json", {})
    packet = load_json(paths.state / "literature_tool_packet.json", {})
    packet_summary = packet.get("summary", {}) if isinstance(packet, dict) and isinstance(packet.get("summary"), dict) else {}
    progress_counts = progress.get("counts", {}) if isinstance(progress, dict) and isinstance(progress.get("counts"), dict) else {}
    strong = _safe_int((progress if isinstance(progress, dict) else {}).get("strong_recommendation_count") or packet_summary.get("strong_paper_anchors") or len(readings), 0)
    target = _safe_int((progress if isinstance(progress, dict) else {}).get("recommendation_target_count") or packet_summary.get("recommendation_target_count"), 0)
    shortfall = _safe_int((progress if isinstance(progress, dict) else {}).get("recommendation_shortfall") or packet_summary.get("recommendation_shortfall") or (max(0, target - strong) if target else 0), 0)
    literature_gate = {
        "status": "shortfall" if shortfall > 0 else "pass" if target else "unknown",
        "blocked": shortfall > 0,
        "run_id": run_id,
        "strong_recommendations": strong,
        "recommendation_target_count": target,
        "recommendation_shortfall": shortfall,
        "evaluated_candidates": _safe_int(progress_counts.get("evaluated_candidates"), 0),
        "source": "planning/finding/find_progress.json",
    }
    read_payload = load_json(paths.planning / "finding" / "read_results.json", {})
    idea_payload = load_json(paths.planning / "finding" / "ideas.json", {})
    plan_payload = load_json(paths.planning / "finding" / "plans.json", {})
    existing_state_plan = load_json(paths.state / "current_find_research_plan.json", {})
    targeted_queries = extract_targeted_search_queries(
        paths,
        read_payload if isinstance(read_payload, dict) else {},
        idea_payload if isinstance(idea_payload, dict) else {},
        plan_payload if isinstance(plan_payload, dict) else {},
        existing_state_plan if isinstance(existing_state_plan, dict) else {},
    )
    targeted_tool = load_json(paths.state / "taste_targeted_queries.json", {})
    if not isinstance(targeted_tool, dict):
        targeted_tool = {}
    targeted_run_id = str(targeted_tool.get("current_find_run_id") or targeted_tool.get("run_id") or targeted_tool.get("find_run_id") or "").strip()
    if targeted_run_id and run_id and targeted_run_id != run_id:
        targeted_tool = {}
    selected_base: dict[str, Any] = {}
    if isinstance(fresh_plan, dict) and not literature_gate["blocked"]:
        env = fresh_plan.get("environment_selection") if isinstance(fresh_plan.get("environment_selection"), dict) else {}
        maybe = fresh_plan.get("selected_base") if isinstance(fresh_plan.get("selected_base"), dict) else fresh_plan.get("selected") if isinstance(fresh_plan.get("selected"), dict) else {}
        if maybe and env.get("selection_stage") == ENVIRONMENT_SELECTION_STAGE and str(env.get("fresh_find_run_id") or "") == run_id and env.get("accepted_by_claude"):
            selected_base = maybe
    fallback_route_title = "current Find literature gate blocked" if literature_gate["blocked"] else "environment-stage Claude Code base selection pending"
    route_title = str(selected_base.get("title") or selected_base.get("name") or selected_base.get("repo") or fallback_route_title).strip()
    gate_refresh_commands = [
        f"{management_python()} modules/experimenting/main.py --action audit_reference_reproduction --project {project} --venue {venue}",
        f"{management_python()} modules/experimenting/scripts/audit_experiment_iteration.py --project {project}",
        f"{management_python()} framework/scripts/build_research_trajectory_system.py --project {project} --venue {venue}",
        f"{management_python()} modules/planning/main.py --action build_blocker_action_plan --project {project} --venue {venue}",
    ]
    literature_repair_commands = [
        f"{management_python()} modules/finding/main.py --action build_literature_tool_packet --project {project} --venue {venue}",
        f"{management_python()} modules/finding/main.py --action run_literature_tool --project {project} --venue {venue} --query \"<targeted literature gap query>\" --fast-mode --publish-current-find",
        f"{management_python()} modules/writing/main.py --action audit_submission_readiness --project {project} --venue {venue}",
        f"{management_python()} modules/planning/main.py --action build_blocker_action_plan --project {project} --venue {venue}",
    ]
    data_command = f"{management_python()} modules/environment/main.py --action build_fresh_base_implementation_plan --project {project}"
    failure_type = ""
    next_required_action = "environment_base_selection_and_repo_data_protocol_audit"
    if literature_gate["blocked"]:
        enforce_literature_gate_on_ideas_and_plans(ideas, plans, literature_gate)
        status = "blocked_literature_recommendation_gate"
        failure_type = "literature_recommendation_shortfall"
        next_required_action = "repair_current_find_literature_scoring_packet"
        blockers = [f"current Find recommended papers are below target: {strong}/{target}; shortfall={shortfall}"]
        base_selection_status = "blocked_by_literature_gate"
        next_required_stage = "repair_current_find_literature_scoring_packet"
        blocked_until = [
            "当前 Find 推荐论文数达到目标，且均有真实摘要与 LLM 标题+摘要评分",
            "Environment 阶段验证并锁定候选基底",
            "主线 repo/data/env 合同通过",
            "baseline 与候选方法复现审计通过",
            "reference/scientific/evidence gates 通过",
        ]
    else:
        status = "environment_base_selected_ready_for_implementation_gate" if selected_base else "waiting_for_environment_base_selection" if ideas and plans else "blocked_missing_ideas_or_plans"
        if status == "blocked_missing_ideas_or_plans":
            failure_type = "current_find_read_idea_plan_contract_incomplete"
            next_required_action = "rerun_current_find_claude_takeover_repair"
        blockers = [] if status != "blocked_missing_ideas_or_plans" else ["missing current Find ideas or plans"]
        base_selection_status = "selected" if selected_base else "waiting_for_environment_claude_code"
        next_required_stage = "environment_base_selection_and_repo_data_protocol_audit" if not failure_type else "repair_current_find_claude_read_idea_plan"
        blocked_until = ["Environment 阶段验证并锁定候选基底", "主线 repo/data/env 合同通过", "baseline 与候选方法复现审计通过", "reference/scientific/evidence gates 通过"]
    execution_selection = apply_current_find_execution_selection(
        ideas,
        plans,
        source="current_find_bridge",
        executable=bool(plans and not str(status).startswith("blocked")),
    )
    selection_fields = {key: execution_selection.get(key) for key in CURRENT_FIND_SELECTION_FIELD_KEYS}
    return {
        "project": project,
        "generated_at": now_iso(),
        "source": "current_find_bridge",
        "run_id": run_id,
        "status": status,
        "current_find_reading_count": len(readings),
        "current_find_idea_count": len(ideas),
        "current_find_plan_count": len(plans),
        "selected_base": selected_base,
        "fresh_base_status": fresh_plan.get("status") if isinstance(fresh_plan, dict) else "",
        "primary_route": route_title,
        "literature_gate": literature_gate,
        "blockers": blockers,
        "failure_type": failure_type,
        "next_required_action": next_required_action,
        "base_selection_status": base_selection_status,
        "next_required_stage": next_required_stage,
        "targeted_search_queries": targeted_queries,
        "targeted_search_query_count": len(targeted_queries),
        "targeted_search_tool_status": {
            "status": targeted_tool.get("status"),
            "venue": targeted_tool.get("venue"),
            "packet_return_code": targeted_tool.get("packet_return_code"),
            "return_codes": targeted_tool.get("return_codes"),
            "failure_summary": targeted_tool.get("failure_summary"),
            "guardrail": targeted_tool.get("guardrail"),
            "allow_new_find_approved": targeted_tool.get("allow_new_find_approved"),
        },
        "allowed_actions": literature_repair_commands if literature_gate["blocked"] else gate_refresh_commands,
        "literature_repair_policy": "targeted_find_allowed" if literature_gate["blocked"] else "literature_gate_cleared",
        "blocked_until": blocked_until,
        "claude_code_autonomous_loop": [
            {"stage": "read_current_find", "input": "planning/finding/find_results.json", "output": "planning/finding/read_results.json, planning/finding/read.md", "status": "completed"},
            {"stage": "literature_gate_repair", "input": "current Find title+abstract scoring packet audit", "output": "recommended paper count >= recommendation_target_count or truthful blocker", "blocked_by": "literature_recommendation_shortfall" if literature_gate["blocked"] else "cleared", "targeted_search_queries": targeted_queries, "commands": literature_repair_commands if literature_gate["blocked"] else []},
            {"stage": "environment_base_selection", "input": "current Find strong recommendations + read/idea/plan + candidate repo/data/protocol evidence", "output": "state/evidence_ready_repo_selection.json with selection_stage=environment_claude_code and current fresh_find_run_id", "blocked_by": "blocked_by_literature_gate" if literature_gate["blocked"] else "waiting_for_environment_base_selection"},
            {"stage": "fresh_base_data_contract", "input": "environment-selected repo only", "output": "state/fresh_base_implementation_plan.json updated with ready/blocked data evidence", "commands": [] if literature_gate["blocked"] else [data_command]},
            {"stage": "implementation_smoke", "input": "selected repo entrypoints and at least one ready dataset", "output": "artifact dir with stdout/loss/metrics/audit", "blocked_by": "blocked_by_literature_gate" if literature_gate["blocked"] else "blocked_data_or_environment_contract until dataset and entrypoint probes pass"},
            {"stage": "gate_refresh", "commands": gate_refresh_commands},
        ],
        "ideas": ideas,
        "plans": plans,
        **selection_fields,
        "guardrails": [
            "Do not launch raw or duplicate Find jobs from this script; use modules/finding/main.py --action run_literature_tool for controlled targeted literature repair.",
            "Do not use stale read/idea/plan from another run_id or from candidates removed by current gates.",
            "Do not use screened_ranking as positive evidence.",
            "Do not select or promote a base while the current Find recommendation count gate has a shortfall.",
            "Do not weaken evidence gates or promote paper claims before local experiment evidence passes.",
        ],
    }

def update_literature_packet(paths, run_id: str, readings: list[dict[str, Any]], ideas: list[dict[str, Any]], plans: list[dict[str, Any]]) -> None:
    packet_path = paths.state / "literature_tool_packet.json"
    packet = load_json(packet_path, {})
    if not isinstance(packet, dict):
        packet = {}
    current_plan = load_json(paths.state / "current_find_research_plan.json", {})
    if not isinstance(current_plan, dict):
        current_plan = {}
    plan_status = str(current_plan.get("status") or "").strip().lower()
    selection_fields = current_find_selection_fields(
        ideas,
        plans,
        source=str(current_plan.get("source") or CLAUDE_TAKEOVER_SOURCE),
        executable=bool(plans and not plan_status.startswith("blocked")),
    )
    packet["current_find_reading"] = {"run_id": run_id, "status": "current_read_idea_plan_ready", "readings": [{"title": row.get("title"), "venue": row.get("venue"), "year": row.get("year"), "score": row.get("score"), "method_family_zh": row.get("method_family_zh"), "method_details_zh": row.get("method_details_zh"), "limitations_zh": row.get("limitations_zh")} for row in readings], "idea_count": len(ideas), "plan_count": len(plans), **selection_fields}
    packet["current_find_execution_selection"] = {"run_id": run_id, **selection_fields}
    workflow = packet.get("workflow", []) if isinstance(packet.get("workflow"), list) else []
    note = "Before base repair or Claude Code experiments, verify planning/finding/read_results.json, ideas.json, and plans.json all match the latest find_results.run_id."
    if note not in workflow:
        workflow.insert(0, note)
    packet["workflow"] = workflow
    summary = packet.get("summary", {}) if isinstance(packet.get("summary"), dict) else {}
    summary.update({"current_find_readings": len(readings), "current_find_ideas": len(ideas), "current_find_plans": len(plans), "current_find_run_id": run_id, "selected_idea_id": selection_fields.get("selected_idea_id", ""), "selected_plan_id": selection_fields.get("selected_plan_id", ""), "execution_policy_status": (selection_fields.get("execution_policy") or {}).get("status", "") if isinstance(selection_fields.get("execution_policy"), dict) else ""})
    packet["summary"] = summary
    save_json(packet_path, packet)
    md_path = paths.planning / "literature_tool_packet.md"
    existing = md_path.read_text(encoding="utf-8", errors="ignore") if md_path.exists() else "# TASTE Literature Tool Packet\n"
    section = [
        "\n## 当前 Find 精读/Idea/Plan 状态\n",
        f"- run_id: `{run_id}`\n",
        f"- current_find_readings: {len(readings)}\n",
        f"- current_find_ideas: {len(ideas)}\n",
        f"- current_find_plans: {len(plans)}\n",
        f"- selected_idea: {_public_selection_title(selection_fields.get('selected_idea'), '已选择')}\n" if selection_fields.get('selected_idea_id') else "",
        f"- selected_plan: {_public_selection_title(selection_fields.get('selected_plan'), '已选择')}\n" if selection_fields.get('selected_plan_id') else "",
        f"- execution_policy: `{((selection_fields.get('execution_policy') or {}).get('status') if isinstance(selection_fields.get('execution_policy'), dict) else '')}`\n",
        "- 规则：主控 Claude Code 可以生成多个 idea/plan 候选，但后续环境、实验、写作和 claim 只能消费选中的执行计划；未选中候选只能留作 backlog，不得驱动主线。\n",
        "- 规则：Claude Code 在修复 base、设计实验、写代码前必须优先读取这些当前 run 产物；旧 run/fallback 不得驱动主线。\n",
    ]
    marker = "\n## 当前 Find 精读/Idea/Plan 状态\n"
    existing = existing.split(marker)[0].rstrip() + "\n" + "".join(section) if marker in existing else existing.rstrip() + "\n" + "".join(section)
    write_text(md_path, existing)


def copy_to_taste_run(paths, run_id: str, names: list[str]) -> None:
    taste_dir = paths.planning / "finding"
    for taste_run_dir in [RUNS_DIR / run_id, LEGACY_RUNS_DIR / run_id]:
        if not taste_run_dir.exists():
            continue
        for name in names:
            source = taste_dir / name
            if source.exists():
                (taste_run_dir / name).write_bytes(source.read_bytes())


def write_current_find_structured_artifacts(
    paths,
    taste_dir: Path,
    run_id: str,
    readings: list[dict[str, Any]],
    ideas: list[dict[str, Any]],
    plans: list[dict[str, Any]],
    takeover: dict[str, Any] | None = None,
    validation: dict[str, Any] | None = None,
) -> None:
    """Persist validated Claude-authored current-Find JSON and Markdown artifacts."""
    generated_at = now_iso()
    takeover = takeover if isinstance(takeover, dict) else {}
    validation = validation if isinstance(validation, dict) else {}
    validation_ok = validation.get("valid") is True
    if not validation_ok:
        save_json(paths.state / "current_find_claude_reading_validation.json", validation)
        return
    normalization_source = "validated_current_find_deep_read_fragments"
    existing_idea_payload = load_json(taste_dir / "ideas.json", {})
    existing_plan_payload = load_json(taste_dir / "plans.json", {})
    if not ideas and isinstance(existing_idea_payload, dict):
        same_run = str(existing_idea_payload.get("run_id") or existing_idea_payload.get("current_find_run_id") or "").strip() == str(run_id or "").strip()
        same_source = existing_idea_payload.get("source") == CLAUDE_TAKEOVER_SOURCE
        preserved_ideas = [row for row in as_list(existing_idea_payload.get("ideas")) if _valid_claude_idea(row)]
        if same_run and same_source and preserved_ideas:
            ideas = preserved_ideas
    if not plans and isinstance(existing_plan_payload, dict):
        same_run = str(existing_plan_payload.get("run_id") or existing_plan_payload.get("current_find_run_id") or "").strip() == str(run_id or "").strip()
        same_source = existing_plan_payload.get("source") == CLAUDE_TAKEOVER_SOURCE
        preserved_plans = [row for row in as_list(existing_plan_payload.get("plans")) if _valid_claude_plan(row)]
        if same_run and same_source and preserved_plans:
            plans = preserved_plans
    selection_fields = current_find_selection_fields(
        ideas,
        plans,
        source=CLAUDE_TAKEOVER_SOURCE,
        executable=bool(validation.get("valid", True) is not False),
    )
    read_payload = {
        "run_id": run_id,
        "source": CLAUDE_TAKEOVER_SOURCE,
        "generated_at": generated_at,
        "normalization_source": normalization_source,
        "readings": readings,
        "claude_takeover": {key: takeover.get(key) for key in ["status", "return_code", "started_at", "finished_at", "prompt_path"] if key in takeover},
        "reading_validation": validation,
        "full_text_packet": full_text_packet_summary(load_full_text_packet_from_taste_dir(taste_dir, run_id)),
        "artifact_recovery": {
            "source": CURRENT_FIND_DEEP_READ_FRAGMENT_SOURCE,
            "refreshed_at": generated_at,
            "policy": "wrapper persisted same-run per-paper Claude deep-read fragments; the embedded reading_validation field remains the pass/block gate. No deterministic scientific content was generated.",
        },
        **selection_fields,
    }
    idea_payload = existing_idea_payload if isinstance(existing_idea_payload, dict) else {}
    idea_payload.update({
        "run_id": run_id,
        "source": CLAUDE_TAKEOVER_SOURCE,
        "generated_at": generated_at,
        "normalization_source": normalization_source,
        "ideas": ideas,
        **selection_fields,
    })
    plan_payload = existing_plan_payload if isinstance(existing_plan_payload, dict) else {}
    plan_payload.update({
        "run_id": run_id,
        "source": CLAUDE_TAKEOVER_SOURCE,
        "generated_at": generated_at,
        "normalization_source": normalization_source,
        "plans": plans,
        **selection_fields,
    })
    save_json(taste_dir / "read_results.json", strip_verbose_claude_takeover(read_payload))
    save_json(taste_dir / "ideas.json", strip_verbose_claude_takeover(idea_payload))
    save_json(taste_dir / "plans.json", strip_verbose_claude_takeover(plan_payload))
    write_current_find_artifact_markdowns(paths, taste_dir, run_id, readings, ideas, plans)
    quarantine_corrupt_current_find_deep_read_fragments(
        paths, run_id, reason="validated_current_find_structured_artifacts"
    )


def write_current_find_artifact_markdowns(paths, taste_dir: Path, run_id: str, readings: list[dict[str, Any]], ideas: list[dict[str, Any]] | None = None, plans: list[dict[str, Any]] | None = None) -> None:
    """Refresh public Markdown projections from structured current-Find artifacts."""
    paper_index = _paper_reference_index(readings)
    if readings:
        write_text(taste_dir / "read.md", render_read_md(readings, run_id))
    if ideas is not None:
        write_text(taste_dir / "idea.md", render_idea_md(ideas, run_id, paper_index))
    if plans is not None:
        write_text(taste_dir / "plan.md", render_plan_md(plans, run_id, paper_index))
    names = ["read_results.json", "ideas.json", "plans.json", "read.md"]
    if ideas is not None:
        names.append("idea.md")
    if plans is not None:
        names.append("plan.md")
    copy_to_taste_run(paths, run_id, names)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(str(value).strip()))
    except Exception:
        return default


def _current_find_survey_stats(find_results: dict[str, Any]) -> dict[str, Any]:
    category_rows = find_results.get("category_scan_report") if isinstance(find_results.get("category_scan_report"), list) else []
    title_rows = find_results.get("title_filter_report") if isinstance(find_results.get("title_filter_report"), list) else []
    venue_rows = find_results.get("venue_health_report") if isinstance(find_results.get("venue_health_report"), list) else []
    source_rows = find_results.get("source_status") if isinstance(find_results.get("source_status"), list) else []
    evaluated = find_results.get("evaluated_candidates") if isinstance(find_results.get("evaluated_candidates"), list) else []
    raw_count = len(find_results.get("raw_title_index") or [])
    if not raw_count:
        raw_count = sum(_safe_int((row if isinstance(row, dict) else {}).get("corpus_count") or (row if isinstance(row, dict) else {}).get("sample_count") or (row if isinstance(row, dict) else {}).get("raw_title_index_count"), 0) for row in venue_rows)
    if not raw_count:
        raw_count = sum(_safe_int((row if isinstance(row, dict) else {}).get("raw_title_index_count"), 0) for row in source_rows)
    return {
        "raw_title_index_papers": raw_count,
        "venue_total_papers_available": raw_count,
        "venue_corpus_audited_papers": raw_count,
        "category_corpus_audited_papers": sum(_safe_int((row if isinstance(row, dict) else {}).get("corpus_audit_papers") or (row if isinstance(row, dict) else {}).get("total_papers"), 0) for row in category_rows),
        "venue_category_selected_papers": sum(_safe_int((row if isinstance(row, dict) else {}).get("selected_category_papers"), 0) for row in category_rows),
        "venue_title_filter_input_papers": sum(_safe_int((row if isinstance(row, dict) else {}).get("title_filter_input_papers"), 0) for row in title_rows),
        "venue_final_title_candidates": sum(_safe_int((row if isinstance(row, dict) else {}).get("final_title_candidates"), 0) for row in title_rows) or len(find_results.get("retrieval_candidates") or find_results.get("title_candidates") or []),
        "venue_detail_fetched_candidates": len(evaluated),
        "venue_evaluated_candidates": len(evaluated),
        "llm_scored_candidates": sum(1 for row in evaluated if isinstance(row, dict) and str(row.get("reason_source") or "") == "llm abstract evaluation") or len(evaluated),
        "full_venue_corpus_audit": bool(raw_count),
        "llm_scoring_policy": "Full venue corpus is audited; category/title-screened candidates are batch-scored by LLM for efficiency.",
        "venue_read_candidates": len(find_results.get("read_candidates") or []),
        "strong_recommendations": len(find_results.get("strong_recommendations") or find_results.get("articles") or []),
        "category_scan_reports": len(category_rows),
        "title_filter_reports": len(title_rows),
        "arxiv_raw_count": len(find_results.get("arxiv_raw") or []),
        "arxiv_prefiltered_count": len(find_results.get("arxiv_prefiltered") or []),
        "arxiv_pages_fetched": 0,
        "arxiv_full_scan": False,
        "arxiv_deduped_count": 0,
    }


def update_frontend_state(paths, run_id: str, readings: list[dict[str, Any]], ideas: list[dict[str, Any]], plans: list[dict[str, Any]]) -> None:
    state_path = paths.state / "finding_frontend.json"
    state = load_json(state_path, {})
    if not isinstance(state, dict):
        state = {}
    current_plan = load_json(paths.state / "current_find_research_plan.json", {})
    if not isinstance(current_plan, dict):
        current_plan = {}
    plan_status = str(current_plan.get("status") or "").strip()
    if plan_status.startswith("blocked"):
        frontend_stage = plan_status
        frontend_status = plan_status
    elif plan_status:
        frontend_stage = plan_status
        frontend_status = plan_status
    else:
        frontend_stage = "plan_completed_current_find_bridge"
        frontend_status = "plan_completed_current_find_bridge"
    taste_dir = paths.planning / "finding"
    find_results = load_json(taste_dir / "find_results.json", {})
    if not isinstance(find_results, dict):
        find_results = {}
    survey_stats = _current_find_survey_stats(find_results)
    plan_status_for_selection = str(current_plan.get("status") or "").strip().lower()
    selection_fields = current_find_selection_fields(
        ideas,
        plans,
        source=str(current_plan.get("source") or CLAUDE_TAKEOVER_SOURCE),
        executable=bool(plans and not plan_status_for_selection.startswith("blocked")),
    )
    root = Path(__file__).resolve().parents[1]
    project_name = getattr(getattr(paths, "root", None), "name", "") or Path(paths.planning).parent.name
    cfg = load_project_config(project_name)
    llm = cfg.get("llm", {}) if isinstance(cfg.get("llm", {}), dict) else {}
    provider = str(llm.get("provider") or state.get("provider") or "mock")
    api_base = str(llm.get("api_base") or state.get("base_url") or "")
    model = str(llm.get("model") or state.get("model") or "")
    api_mode = str(llm.get("api_mode") or state.get("api_mode") or "chat_completions")
    counts = state.get("counts", {}) if isinstance(state.get("counts"), dict) else {}
    counts.update({
        "articles": len(find_results.get("strong_recommendations") or find_results.get("articles") or []),
        "read_candidates": len(find_results.get("read_candidates") or []),
        "evaluated_candidates": len(find_results.get("evaluated_candidates") or []),
        "title_candidates": len(find_results.get("title_candidates") or find_results.get("retrieval_candidates") or []),
        "raw_title_index": len(find_results.get("raw_title_index") or []),
        "huggingface": len(find_results.get("huggingface") or []),
        "github": len(find_results.get("github") or []),
        "readings": len(readings),
        "ideas": len(ideas),
        "plans": len(plans),
    })
    state.update({
        "repo_root": str(root),
        "framework_root": str(root / "framework"),
        "taste_run_id": run_id,
        "taste_run_dir": str(RUNS_DIR / run_id),
        "stage": frontend_stage,
        "status": frontend_status,
        "provider": provider,
        "base_url": api_base,
        "model": model,
        "llm_enabled": bool(provider and provider != "mock" and model and api_base),
        "api_mode": api_mode,
        "max_papers": len(find_results.get("strong_recommendations") or find_results.get("articles") or []),
        "max_ideas": len(ideas),
        "output_dir": str(taste_dir),
        "survey_stats": survey_stats,
        "counts": counts,
        "current_find_bridge": {"generated_at": now_iso(), "readings": len(readings), "ideas": len(ideas), "plans": len(plans), **selection_fields, "guardrail": "Downstream artifacts were rebuilt from the current Find result; downstream environment/experiment/paper/claim stages must consume only selected_plan_id. Targeted literature repair must use modules/finding/main.py --action run_literature_tool."},
        **selection_fields,
    })
    save_json(state_path, state)

    full_cycle_path = paths.state / "full_research_cycle.json"
    full_cycle = load_json(full_cycle_path, {})
    if isinstance(full_cycle, dict):
        progress = load_json(taste_dir / "find_progress.json", {})
        if not isinstance(progress, dict):
            progress = {}
        paper_cfg = cfg.get("paper", {}) if isinstance(cfg.get("paper", {}), dict) else {}
        venue = str(cfg.get("target_venue") or cfg.get("venue") or paper_cfg.get("target_venue") or full_cycle.get("venue") or full_cycle.get("target_venue") or "").strip()
        recommendation_count = len(find_results.get("strong_recommendations") or find_results.get("articles") or [])
        read_candidate_count = len(find_results.get("read_candidates") or []) or recommendation_count
        target = _safe_int(progress.get("recommendation_target_count") or find_results.get("recommendation_target_count") or recommendation_count, recommendation_count)
        shortfall = max(0, target - recommendation_count)
        full_cycle.update({
            "venue": venue or full_cycle.get("venue", ""),
            "target_venue": venue or full_cycle.get("target_venue", ""),
            "find_run_id": run_id,
            "current_find_run_id": run_id,
            "recommendation_target_count": target,
            "recommendation_actual_count": recommendation_count,
            "recommendation_count": recommendation_count,
            "read_candidate_count": read_candidate_count,
            "recommendation_shortfall": shortfall,
            "current_find_reading_count": len(readings),
            "current_find_idea_count": len(ideas),
            "current_find_plan_count": len(plans),
            **selection_fields,
            "literature_gate": {
                "status": "positive_anchors_ready" if recommendation_count and shortfall == 0 else "recommendation_shortfall",
                "run_id": run_id,
                "strong_recommendations": recommendation_count,
                "recommendation_target_count": target,
                "recommendation_shortfall": shortfall,
                "read_candidates": read_candidate_count,
                "readings": len(readings),
                "ideas": len(ideas),
                "plans": len(plans),
                "source": "ensure_current_find_research_plan.update_frontend_state",
            },
            "updated_at": now_iso(),
        })
        save_json(full_cycle_path, full_cycle)



def _current_find_claude_available(project: str, cfg: dict[str, Any]) -> bool:
    try:
        return bool(runtime_find_binary("claude", project=project, cfg=cfg))
    except Exception:
        return bool(shutil.which("claude"))


def _extract_llm_json_object(content: str) -> dict[str, Any]:
    text = str(content or "").strip()
    for match in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.I | re.S):
        block = match.group(1).strip()
        if not block:
            continue
        try:
            payload = json.loads(block)
            if isinstance(payload, dict):
                return payload
        except Exception:
            continue
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            payload, _end = decoder.raw_decode(text[index:])
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _packet_entry_text_excerpt(entry: dict[str, Any], limit: int = 9000) -> str:
    text_parts: list[str] = []
    text_path = first_text(entry, "text_path") if isinstance(entry, dict) else ""
    if text_path:
        try:
            path = Path(text_path).expanduser()
            if path.exists():
                text_parts.append(path.read_text(encoding="utf-8", errors="replace")[:limit])
        except Exception:
            pass
    for key in ["text", "body_text", "abstract", "summary"]:
        value = str((entry or {}).get(key) or "").strip()
        if value:
            text_parts.append(value[:limit])
    return "\n".join(part for part in text_parts if part).strip()[:limit]


def _build_llm_current_find_prompt(project: str, cfg: dict[str, Any], run_id: str, papers: list[dict[str, Any]], full_text_packet: dict[str, Any], idea_count: int) -> str:
    packet_index = _full_text_packet_index(full_text_packet)
    rows: list[dict[str, Any]] = []
    for idx, row in enumerate(papers, 1):
        entry = _matching_full_text_packet_entry(row, packet_index)
        rows.append({
            "rank": idx,
            "paper_id": first_text(row, "id", "paper_id", "entry_id") or _paper_key(row),
            "title": first_text(row, "title"),
            "venue": first_text(row, "venue", "source"),
            "year": first_text(row, "year", "published", "updated"),
            "url": first_text(row, "url", "abs_url"),
            "pdf_url": first_text(row, "pdf_url"),
            "abstract": first_text(row, "abstract_zh", "abstract", "summary"),
            "find_reason": first_text(row, "reason_zh", "reason", "fit_explanation_zh", "fit_explanation", "recommendation_note_zh", "recommendation_note"),
            "reader_instruction": first_text(row, "reader_instruction_zh", "reader_instruction", "reader_instruction_en", "reader_checklist_zh", "reader_checklist"),
            "evidence_role": row.get("evidence_role") or "",
            "evidence_tier": row.get("evidence_tier") or "",
            "full_text_excerpt": _packet_entry_text_excerpt(entry if isinstance(entry, dict) else {}, 9000),
        })
    schema = {
        "readings": [
            {
                "paper_id": "same as input paper_id",
                "title": "same as input title",
                "verdict": "core_reading | method_reference | contrast_or_boundary_reading",
                "support_role": "core_method_reference | transferable_method_reference | contrast_or_boundary_reference",
                "abstract_zh": "中文原论文摘要，至少260字，可翻译输入摘要但不能写推荐理由",
                "motivation_zh": "中文动机，至少180字",
                "method_details_zh": "中文详细方法，至少650字",
                "experiments_zh": "中文实验设置与结果，至少420字",
                "limitations_zh": "中文局限，至少220字",
                "method_advantages_zh": ["至少两条具体中文优点"],
                "method_disadvantages_zh": ["至少两条具体中文不足"],
                "full_text_available": True,
                "full_text_status": "pdf_text_read | html_text_read | full_text_read",
            }
        ],
        "ideas": [
            {
                "id": "idea-current-find-001",
                "title": "中文标题",
                "status": "approved_for_planning",
                "new_method": "详细新方法，至少120字",
                "initial_experiment": "初步实验，至少120字；写明候选基底论文或候选 repo 名称/URL、为什么适合、拟修改模块、baseline/control/ablation、指标、坏例切片和 Environment 需要核查的 repo/data/protocol 证据",
                "inspired_by": [{"title": "paper title", "paper_id": "paper id", "reason": "如何启发"}],
                "objective_scores": {"novelty": 8, "evidence_alignment": 8, "feasibility": 8, "experimentability": 8, "risk_control": 8, "overall": 8},
                "score": 8,
                "idea_score": 8,
            }
        ],
        "plans": [
            {
                "plan_id": "plan-idea-current-find-001",
                "idea_id": "idea-current-find-001",
                "title": "中文标题",
                "steps": ["列出 Idea/Plan 提出的候选 repo/base、拟修改模块、数据与指标协议，并说明 Environment 验证项", "细化最小实验、baseline/control/ablation、指标与坏例切片"],
                "selected_for_execution": True,
                "execute_next": True,
                "execution_selection": {"selected": True, "selected_by": "llm_fallback_after_deep_read", "reason": "为什么唯一选择"},
            }
        ],
        "targeted_search_queries": ["至少三个后续补检索主题"],
    }
    return (
        f"你是 TASTE 项目 {project} 的 Read/Idea/Plan 兜底 LLM。Claude Code CLI 不可用，因此只允许你完成 Find 后面的精读、idea、plan 结构化产物；"
        "你不得执行环境配置、实验、代码修改、论文撰写或 claim promotion。\n"
        f"当前 run_id={run_id}，需要精读 Read-stage reading packet 中的全部 {len(rows)} 篇论文，并生成 {idea_count} 个 idea 与 {idea_count} 个 plan。\n"
        "必须基于 full_text_excerpt 写中文精读；如果 excerpt 为空，必须在对应 reading 中明确 full_text_available=false 且说明不可访问原因，不能伪装全文已读。\n"
        "Find/Read/Idea/Plan 阶段必须提出候选基底论文/候选 repo 线索、拟修改模块和 Environment 验证要求；但严禁写本地 repo_path、具体数据集训练命令或 ready_to_execute，也不得声称环境已选基底。\n"
        "plans 中必须且只能有一个 selected_for_execution=true / execute_next=true，其余 plan 必须显式 false。\n"
        "只返回 JSON，不要 Markdown。JSON schema 示例：\n"
        f"{json.dumps(schema, ensure_ascii=False, indent=2)}\n\n"
        "输入论文：\n"
        f"{json.dumps(rows, ensure_ascii=False, indent=2)}\n"
    )


def _llm_row_index(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in _reading_identity_values(row) or _identity_values(row):
            index.setdefault(key, row)
        title = norm_title(row.get("title") or row.get("paper_title"))
        if title:
            index.setdefault(f"title:{title}", row)
    return index


def _normalize_llm_fallback_readings(raw_rows: list[dict[str, Any]], papers: list[dict[str, Any]], full_text_packet: dict[str, Any], cfg: dict[str, Any], run_id: str, model: str) -> list[dict[str, Any]]:
    raw_index = _llm_row_index(raw_rows)
    packet_index = _full_text_packet_index(full_text_packet)
    readings: list[dict[str, Any]] = []
    for idx, find_row in enumerate(papers, 1):
        raw = _best_candidate_for_find_row(raw_index, raw_rows, find_row) or (raw_rows[idx - 1] if idx - 1 < len(raw_rows) and isinstance(raw_rows[idx - 1], dict) else {})
        base = build_reading(find_row, cfg)
        clean = _merge_find_metadata_into_reading({**base, **(raw if isinstance(raw, dict) else {})}, find_row)
        clean.setdefault("paper_id", first_text(find_row, "id", "paper_id", "entry_id") or _paper_key(find_row))
        clean["title"] = first_text(find_row, "title") or first_text(clean, "title")
        clean = normalize_reading_full_text_evidence(clean, _matching_full_text_packet_entry(clean or find_row, packet_index))
        audit = clean.get("deep_read_audit") if isinstance(clean.get("deep_read_audit"), dict) else {}
        audit = dict(audit)
        audit.update({
            "mode": "llm_fallback",
            "source": LLM_CURRENT_FIND_FALLBACK_SOURCE,
            "status": "completed",
            "subagent_used": False,
            "model": model,
            "run_id": run_id,
        })
        clean["deep_read_audit"] = audit
        clean["subagent_deep_read"] = False
        clean["deep_read_source"] = LLM_CURRENT_FIND_FALLBACK_SOURCE
        clean["source"] = LLM_CURRENT_FIND_FALLBACK_SOURCE
        readings.append(_sanitize_reading_public_fields(clean))
    positive_ids, _ = _current_positive_identities({"strong_recommendations": papers})
    return _enforce_current_find_claim_policy(readings, positive_ids)


def _normalize_llm_fallback_ideas(raw_rows: list[dict[str, Any]], idea_count: int, readings: list[dict[str, Any]], model: str) -> list[dict[str, Any]]:
    strong_refs = [_paper_public_ref(row) for row in readings if row.get("claim_ready_anchor")] or [_paper_public_ref(row) for row in readings[:6]]
    ideas: list[dict[str, Any]] = []
    pending_target = {"selection_stage": "read_idea_plan_candidate_proposal", "status": "candidate_pending_environment_validation", "candidate_repo_hint": "to be proposed from current Find/Read evidence", "dataset_contract": {"status": "candidate_pending_environment_validation"}}
    for idx, raw in enumerate([row for row in raw_rows if isinstance(row, dict)][:idea_count], 1):
        idea = dict(raw)
        idea_id = str(idea.get("id") or idea.get("idea_id") or f"idea-current-find-{idx:03d}").strip()
        idea["id"] = idea_id
        idea["idea_id"] = str(idea.get("idea_id") or idea_id)
        idea.setdefault("title", f"current Find LLM fallback idea {idx}")
        idea.setdefault("status", "approved_for_planning")
        idea["recommendation"] = "candidate_repo_base_proposal_ready_for_environment_validation"
        idea["source"] = LLM_CURRENT_FIND_FALLBACK_SOURCE
        idea["implementation_target"] = pending_target
        idea["inspired_by"] = _normalize_inspired_refs(idea.get("inspired_by"), as_list(idea.get("supporting_papers")) or strong_refs)
        idea.setdefault("supporting_papers", strong_refs)
        scores = _idea_objective_scores(idea)
        if scores:
            idea.setdefault("objective_scores", scores)
            if _numeric_or_none(idea.get("score")) is None and scores.get("overall") is not None:
                idea["score"] = scores["overall"]
            if _numeric_or_none(idea.get("idea_score")) is None and scores.get("overall") is not None:
                idea["idea_score"] = scores["overall"]
        audit = idea.get("idea_score_audit") if isinstance(idea.get("idea_score_audit"), dict) else {}
        audit = dict(audit)
        audit.update({"mode": "llm_fallback", "source": LLM_CURRENT_FIND_FALLBACK_SOURCE, "status": "completed", "subagent_used": False, "model": model})
        idea["idea_score_audit"] = audit
        idea["guardrail"] = "This Read/Idea/Plan artifact was generated by LLM fallback only because Claude Code CLI was unavailable; environment, experiment, and paper execution remain Claude Code-only."
        ideas.append(idea)
    return ideas


def _normalize_llm_fallback_plans(raw_rows: list[dict[str, Any]], ideas: list[dict[str, Any]], model: str) -> list[dict[str, Any]]:
    plans: list[dict[str, Any]] = []
    for idx, idea in enumerate(ideas, 1):
        raw = raw_rows[idx - 1] if idx - 1 < len(raw_rows) and isinstance(raw_rows[idx - 1], dict) else {}
        plan = dict(raw)
        idea_id = str(idea.get("id") or idea.get("idea_id") or f"idea-current-find-{idx:03d}")
        plan.setdefault("plan_id", f"plan-{idea_id}")
        plan["idea_id"] = str(plan.get("idea_id") or idea_id)
        plan.setdefault("title", idea.get("title") or f"current Find LLM fallback plan {idx}")
        plan.setdefault("status", "waiting_for_environment_base_selection")
        plan["source"] = LLM_CURRENT_FIND_FALLBACK_SOURCE
        plan.setdefault("new_method", idea.get("new_method") or idea.get("hypothesis") or "")
        plan.setdefault("initial_experiment", idea.get("initial_experiment") or idea.get("min_experiment") or "")
        plan.setdefault("steps", ["环境阶段先由 Claude Code 选择并审计基底。", "通过 repo/data/env/protocol gate 后再执行最小实验和坏例切片。"])
        selection = plan.get("execution_selection") if isinstance(plan.get("execution_selection"), dict) else {}
        if plan.get("selected_for_execution") is True or plan.get("execute_next") is True or selection.get("selected") is True:
            plan["selected_for_execution"] = True
            plan["execute_next"] = True
            plan["execution_selection"] = {**selection, "selected": True, "selected_by": selection.get("selected_by") or "llm_fallback_after_deep_read", "source": LLM_CURRENT_FIND_FALLBACK_SOURCE}
        elif plan.get("selected_for_execution") is False or plan.get("execute_next") is False or selection.get("selected") is False:
            plan["selected_for_execution"] = False
            plan["execute_next"] = False
            plan["execution_selection"] = {**selection, "selected": False, "selected_by": selection.get("selected_by") or "not_selected_candidate_backlog", "source": LLM_CURRENT_FIND_FALLBACK_SOURCE}
        plan["llm_fallback_audit"] = {"mode": "llm_fallback", "source": LLM_CURRENT_FIND_FALLBACK_SOURCE, "status": "completed", "model": model}
        plan["guardrail"] = "Plan can feed Environment-stage validation with candidate base/repo proposals; experiment and paper execution remain Claude Code-only after gates pass."
        plans.append(plan)
    return plans


def run_llm_current_find_fallback(project: str, paths, cfg: dict[str, Any], taste_dir: Path, run_id: str, find_results: dict[str, Any], papers: list[dict[str, Any]], effective_read_limit: int, idea_count: int, find_revision: dt.datetime | None) -> int:
    generated_at = now_iso()
    result_path = paths.state / "current_find_llm_fallback_result.json"
    prompt_path = paths.state / "current_find_llm_fallback_prompt.md"
    if not llm_available(cfg):
        payload = {
            "status": "blocked_claude_unavailable_and_llm_unavailable",
            "source": LLM_CURRENT_FIND_FALLBACK_SOURCE,
            "run_id": run_id,
            "generated_at": generated_at,
            "reason": llm_disabled_reason(cfg),
            "policy": "Read/Idea/Plan may fall back to LLM only when Claude Code CLI is unavailable and the project LLM is configured. Environment, experiment, and paper stages do not use this fallback.",
        }
        save_json(result_path, payload)
        save_json(paths.state / "current_find_research_plan.json", payload)
        update_frontend_state(paths, run_id, [], [], [])
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2
    full_text_packet = load_full_text_packet_from_taste_dir(taste_dir, run_id)
    prompt = _build_llm_current_find_prompt(project, cfg, run_id, papers[:effective_read_limit], full_text_packet, idea_count)
    prompt_path.write_text(prompt, encoding="utf-8")
    llm_cfg = dict(cfg)
    llm_section = dict(cfg.get("llm") or {}) if isinstance(cfg.get("llm"), dict) else {}
    llm_section["response_format"] = "json_object"
    llm_cfg["llm"] = llm_section
    try:
        response = call_llm(prompt, llm_cfg, system_prompt="Return only valid JSON for the requested TASTE Read/Idea/Plan fallback artifacts.")
        payload = _extract_llm_json_object(str(response.get("content") or ""))
    except Exception as exc:
        response = {"error": str(exc)}
        payload = {}
    model = str((response if isinstance(response, dict) else {}).get("model") or llm_section.get("model") or "")
    raw_path = paths.state / "current_find_llm_fallback_raw_response.json"
    save_json(raw_path, response)
    if not isinstance(payload, dict) or not payload:
        blocked = {"status": "blocked_llm_current_find_fallback_parse_failed", "source": LLM_CURRENT_FIND_FALLBACK_SOURCE, "run_id": run_id, "generated_at": generated_at, "raw_response_path": str(raw_path)}
        save_json(result_path, blocked)
        save_json(paths.state / "current_find_research_plan.json", blocked)
        print(json.dumps(blocked, ensure_ascii=False, indent=2))
        return 2
    readings = _normalize_llm_fallback_readings([row for row in as_list(payload.get("readings")) if isinstance(row, dict)], papers[:effective_read_limit], full_text_packet, cfg, run_id, model)
    ideas = _normalize_llm_fallback_ideas([row for row in as_list(payload.get("ideas")) if isinstance(row, dict)], idea_count, readings, model)
    plans = _normalize_llm_fallback_plans([row for row in as_list(payload.get("plans")) if isinstance(row, dict)], ideas, model)
    ideas, plans = enrich_public_projections(ideas, plans)
    targeted_queries = [str(item).strip() for item in as_list(payload.get("targeted_search_queries")) if str(item).strip()]
    original_recommendation_rows, fallback_recommendation_rows, validation_find_results = _current_reading_validation_view(find_results, full_text_packet, effective_read_limit)
    min_required_readings = len(fallback_recommendation_rows) or min(effective_read_limit, max(1, len(_current_recommendation_identities(find_results)[1]) or len(_current_positive_identities(find_results)[1])))
    validation_ok, validation = validate_claude_readings_against_current_find(readings, validation_find_results, min_required_readings, paths, run_id)
    validation.update({
        "run_id": run_id,
        "source": LLM_CURRENT_FIND_FALLBACK_SOURCE,
        "generated_at": generated_at,
        **_current_reading_validation_metadata(original_recommendation_rows, fallback_recommendation_rows, full_text_packet, effective_read_limit),
    })
    idea_issues = _idea_rows_contract_issues(ideas, idea_count)
    content_ready = bool(validation_ok and len(readings) == min_required_readings and len(ideas) >= idea_count and not idea_issues and len(plans) >= idea_count and len(targeted_queries) >= 3)
    selection_ready = current_find_selected_execution_ready(ideas, plans) if content_ready else False
    ready = bool(content_ready and selection_ready)
    selection_fields = current_find_selection_fields(ideas, plans, source=LLM_CURRENT_FIND_FALLBACK_SOURCE, executable=ready)
    read_payload = {"run_id": run_id, "source": LLM_CURRENT_FIND_FALLBACK_SOURCE, "generated_at": generated_at, "readings": readings, "reading_validation": validation, "targeted_search_queries": targeted_queries, "full_text_packet": full_text_packet_summary(full_text_packet), **selection_fields}
    idea_payload = {"run_id": run_id, "source": LLM_CURRENT_FIND_FALLBACK_SOURCE, "generated_at": generated_at, "ideas": ideas, "targeted_search_queries": targeted_queries, **selection_fields}
    plan_payload = {"run_id": run_id, "source": LLM_CURRENT_FIND_FALLBACK_SOURCE, "generated_at": generated_at, "plans": plans, "targeted_search_queries": targeted_queries, **selection_fields}
    save_json(taste_dir / "read_results.json", read_payload)
    save_json(taste_dir / "ideas.json", idea_payload)
    save_json(taste_dir / "plans.json", plan_payload)
    write_current_find_artifact_markdowns(paths, taste_dir, run_id, readings, ideas, plans)
    fresh_plan = load_json(paths.state / "fresh_base_implementation_plan.json", {})
    execution_plan = build_execution_plan(project, cfg, run_id, readings, ideas, plans, fresh_plan if isinstance(fresh_plan, dict) else {})
    selection_issue = current_find_selected_execution_issue(ideas, plans) if content_ready else ""
    observed = current_find_takeover_observed(taste_dir, readings, ideas, plans, targeted_queries, validation, idea_count)
    if not content_ready:
        failure_type = current_find_contract_failure_type(validation, observed, idea_count=idea_count)
        status = "blocked_current_find_idea_contract_failed" if idea_issues else "blocked_llm_current_find_fallback_incomplete"
        next_required = "rerun_current_find_llm_fallback" if failure_type != "full_text_evidence_missing" else "acquire_current_find_full_text_evidence"
    elif not selection_ready:
        failure_type = selection_issue or "missing_selected_plan"
        status = "blocked_ambiguous_selected_plan" if failure_type == "ambiguous_selected_plan" else "blocked_missing_selected_plan"
        next_required = "rerun_current_find_llm_fallback_select_single_best_plan"
    else:
        failure_type = ""
        status = "selected_plan_ready"
        next_required = "environment_stage_claude_code_base_selection"
    execution_plan.update({
        "project": project,
        "source": LLM_CURRENT_FIND_FALLBACK_SOURCE,
        "run_id": run_id,
        "status": status,
        "content_ready": content_ready,
        "read_idea_plan_ready": content_ready,
        "execution_ready": ready,
        "takeover_ready": ready,
        "claude_current_find_ready": False,
        "llm_current_find_fallback_ready": content_ready,
        "failure_type": failure_type,
        "next_required_action": next_required,
        "next_required_stage": next_required,
        "base_selection_status": "waiting_for_environment_claude_code" if ready else "blocked_by_current_find_llm_fallback",
        "current_find_reading_count": len(readings),
        "current_find_idea_count": len(ideas),
        "current_find_plan_count": len(plans),
        "idea_schema_ready": not idea_issues and len(ideas) >= idea_count,
        "idea_contract_issues": idea_issues[:20],
        "reading_validation": validation,
        "targeted_search_queries": targeted_queries,
        "targeted_search_query_count": len(targeted_queries),
        "observed": {**observed, "reading_validation": validation},
        "llm_fallback": {"status": "completed", "model": model, "prompt_path": str(prompt_path), "raw_response_path": str(raw_path)},
        "guardrail": "Claude Code CLI was unavailable, so only Read/Idea/Plan used LLM fallback. Environment, experiment, paper, code execution, and claim promotion remain Claude Code-only.",
        **selection_fields,
    })
    execution_plan = _sync_current_find_plan_reading_validation(execution_plan, validation)
    save_json(paths.state / "current_find_research_plan.json", execution_plan)
    save_json(paths.state / "experiment_plan.json", execution_plan)
    save_json(paths.state / "idea_candidates.json", {"generated_at": generated_at, "project": project, "source": LLM_CURRENT_FIND_FALLBACK_SOURCE, "current_find_run_id": run_id, "ideas": ideas, **selection_fields, "summary": {"idea_count": len(ideas), "current_find_run_id": run_id, "selected_idea_id": selection_fields.get("selected_idea_id", ""), "selected_plan_id": selection_fields.get("selected_plan_id", "")}})
    save_json(paths.state / "taste_plan_bridge.json", {"source": LLM_CURRENT_FIND_FALLBACK_SOURCE, "run_id": run_id, "plans_json": plan_payload, "plan_markdown_path": str(taste_dir / "plan.md"), "plan_markdown_excerpt": (taste_dir / "plan.md").read_text(encoding="utf-8")[:12000], **selection_fields})
    paper_quality = paper_quality_from_readings(readings, cfg, dt.datetime.now(dt.timezone.utc), run_id)
    save_json(paths.state / "paper_quality.json", paper_quality)
    write_text(paths.planning / "paper_quality.md", render_paper_quality_md(paper_quality))
    update_literature_packet(paths, run_id, readings, ideas, plans)
    update_frontend_state(paths, run_id, readings, ideas, plans)
    result = {"status": status, "source": LLM_CURRENT_FIND_FALLBACK_SOURCE, "run_id": run_id, "readings": len(readings), "ideas": len(ideas), "plans": len(plans), "targeted_search_queries": len(targeted_queries), "ready": ready, "failure_type": failure_type, "prompt_path": str(prompt_path), "raw_response_path": str(raw_path)}
    save_json(result_path, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if ready else 2

def main() -> int:
    parser = argparse.ArgumentParser(description="Ensure The workflow uses the latest Find for reading, ideas, plans, and Claude Code execution planning without launching raw or duplicate Find jobs.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--read-limit", type=int, default=0, help="0 means read every entry in the current Read-stage packet derived from strong_recommendations/articles plus any eligible same-run replacements.")
    parser.add_argument("--idea-count", type=int, default=0)
    parser.add_argument("--skip-claude", action="store_true", help="Use the deterministic compatibility bridge instead of Claude Code takeover.")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--force-selection", action="store_true", help="Only run the main-Claude selected-plan step when current Find Read/Idea/Plan content is already valid.")
    args = parser.parse_args()
    paths = build_paths(args.project)
    cfg = load_project_config(args.project)
    args.idea_count = configured_max_ideas(args.project, cfg, explicit=args.idea_count, default=5)
    taste_dir = paths.planning / "finding"
    find_results = load_json(taste_dir / "find_results.json", {})
    if not isinstance(find_results, dict) or not find_results.get("run_id"):
        raise SystemExit("missing current find_results.json/run_id")
    if sanitize_find_results_non_positive_flags(find_results):
        save_json(taste_dir / "find_results.json", find_results)
        for taste_run_dir in [RUNS_DIR / str(find_results.get("run_id")), LEGACY_RUNS_DIR / str(find_results.get("run_id"))]:
            if taste_run_dir.exists():
                save_json(taste_run_dir / "find_results.json", find_results)
    run_id = str(find_results.get("run_id"))
    find_revision = current_find_revision_time(paths, find_results)
    read_results = load_json(taste_dir / "read_results.json", {})
    ideas_results = load_json(taste_dir / "ideas.json", {})
    plans_results = load_json(taste_dir / "plans.json", {})
    recommendation_count = len(_current_recommendation_identities(find_results)[1])
    positive_count = len(_current_positive_identities(find_results)[1])
    effective_read_limit = args.read_limit if args.read_limit and args.read_limit > 0 else max(1, recommendation_count or positive_count)
    papers = _current_recommendation_rows(find_results)[:effective_read_limit]
    if not papers:
        raise SystemExit("current Find has no strong/articles/read candidates to read")
    if not args.skip_claude and not args.force_selection:
        full_text_preflight = ensure_current_find_full_text_evidence_before_claude(args.project, paths, taste_dir, run_id, find_results)
        if str(full_text_preflight.get("status") or "").startswith("blocked"):
            update_frontend_state(paths, run_id, [], [], [])
            print(json.dumps(full_text_preflight, ensure_ascii=False, indent=2))
            return 2
        refreshed_find_results = load_json(taste_dir / "find_results.json", find_results)
        if isinstance(refreshed_find_results, dict) and str(refreshed_find_results.get("run_id") or "").strip() == run_id:
            find_results = refreshed_find_results
            recommendation_count = len(_current_recommendation_identities(find_results)[1])
            positive_count = len(_current_positive_identities(find_results)[1])
            effective_read_limit = args.read_limit if args.read_limit and args.read_limit > 0 else max(1, recommendation_count or positive_count)
            full_text_packet = load_current_full_text_packet(paths, run_id)
            papers = _current_reading_packet_rows(find_results, full_text_packet, effective_read_limit)
            if not papers:
                raise SystemExit("current Find has no full-text reading packet after full-text repair")
        else:
            full_text_packet = load_current_full_text_packet(paths, run_id)
            papers = _current_reading_packet_rows(find_results, full_text_packet, effective_read_limit)
            if not papers:
                raise SystemExit("current Find has no full-text reading packet after full-text repair")
        find_revision = current_find_revision_time(paths, find_results)
    if not args.skip_claude and not _current_find_claude_available(args.project, cfg):
        return run_llm_current_find_fallback(
            args.project,
            paths,
            cfg,
            taste_dir,
            run_id,
            find_results,
            papers,
            effective_read_limit,
            args.idea_count,
            find_revision,
        )
    if not args.skip_claude:
        existing_readings, existing_ideas, existing_plans = load_claude_outputs(taste_dir, run_id, find_results, effective_read_limit, paths.state, find_revision, paths, write_pending_validation=False)
        read_payload = load_json(taste_dir / "read_results.json", {})
        idea_payload = load_json(taste_dir / "ideas.json", {})
        plan_payload = load_json(taste_dir / "plans.json", {})
        current_plan = load_json(paths.state / "current_find_research_plan.json", {})
        targeted_queries = extract_targeted_search_queries(paths, read_payload if isinstance(read_payload, dict) else {}, idea_payload if isinstance(idea_payload, dict) else {}, plan_payload if isinstance(plan_payload, dict) else {}, current_plan if isinstance(current_plan, dict) else {})
        min_required_readings = min(effective_read_limit, max(1, len(papers)))
        validation_payload = load_json(paths.state / "current_find_claude_reading_validation.json", {})
        # Refresh validation before choosing a takeover path. The full-text packet can
        # advance after a previous validation file was written; stale validation must
        # not misclassify readable packet evidence as missing evidence.
        if existing_readings:
            existing_readings, existing_ideas, existing_plans, targeted_queries, validation_payload = _refresh_current_find_claude_outputs(
                paths,
                taste_dir,
                run_id,
                find_results,
                effective_read_limit,
                find_revision,
            )
        validation_ok = bool(current_reading_validation_ready(validation_payload, run_id, min_required_readings) and claude_output_payloads_are_current([validation_payload], find_revision))
        validation_requires_fresh_takeover = current_reading_validation_requires_fresh_takeover(validation_payload, run_id)
        claude_current = bool(len(existing_readings) == min_required_readings and validation_ok and len(existing_ideas) >= args.idea_count and _ideas_three_part_ready(existing_ideas, args.idea_count) and len(existing_plans) >= args.idea_count and len(targeted_queries) >= 3 and current_find_selected_execution_ready(existing_ideas, existing_plans))
        if existing_readings and not args.force and not args.force_selection:
            latest_takeover = load_json(paths.state / "current_find_claude_takeover_result.json", {})
            takeover = latest_takeover if isinstance(latest_takeover, dict) and latest_takeover else {"status": "existing_current_find_fragments_refreshed", "return_code": 0, "started_at": now_iso(), "finished_at": now_iso(), "prompt_path": ""}
            existing_readings, existing_ideas, existing_plans, targeted_queries, positive_validation = _refresh_current_find_claude_outputs(paths, taste_dir, run_id, find_results, effective_read_limit, find_revision)
            write_current_find_structured_artifacts(paths, taste_dir, run_id, existing_readings, existing_ideas, existing_plans, takeover, positive_validation)
            payload = ensure_claude_plan_state(args.project, paths, run_id, existing_readings, existing_ideas, existing_plans, takeover, idea_count=args.idea_count)
            update_literature_packet(paths, run_id, existing_readings, existing_ideas, existing_plans)
            update_frontend_state(paths, run_id, existing_readings, existing_ideas, existing_plans)
            ready_now = _current_find_contract_ready(existing_readings, existing_ideas, existing_plans, targeted_queries, positive_validation, run_id, min_required_readings, args.idea_count, find_revision)
            selection_receipt = None
            if ready_now:
                selection_receipt = sync_current_find_selection_success_receipt(
                    paths,
                    run_id,
                    takeover,
                    existing_ideas,
                    existing_plans,
                    positive_validation,
                    reason="valid_artifacts_ready",
                )
                save_json(paths.state / "current_find_claude_takeover_result.json", {
                    **takeover,
                    "status": "already_current_valid_claude_artifacts",
                    "run_id": run_id,
                    "return_code": takeover.get("return_code", 0),
                    "contract_validation_valid": True,
                    "contract_failure": None,
                    "reading_validation": positive_validation,
                    "selected_execution": (selection_receipt if isinstance(selection_receipt, dict) else {}).get("selected_execution", {}),
                    "selected_plan_id": (selection_receipt if isinstance(selection_receipt, dict) else {}).get("selected_plan_id"),
                    "selected_idea_id": (selection_receipt if isinstance(selection_receipt, dict) else {}).get("selected_idea_id"),
                    "validated_at": now_iso(),
                })
            print(json.dumps({"status": payload.get("status"), "source": payload.get("source"), "run_id": run_id, "readings": len(existing_readings), "ideas": len(existing_ideas), "plans": len(existing_plans), "targeted_search_queries": len(targeted_queries), "reading_validation": {"valid": positive_validation.get("valid"), "full_text_reading_count": positive_validation.get("full_text_reading_count"), "pending_full_text_reading_count": positive_validation.get("pending_full_text_reading_count")}, "claude_selection": selection_receipt, "validated_against_current_find": True, "public_projection": "refreshed"}, ensure_ascii=False, indent=2))
            return 0 if ready_now else 2
        if args.force_selection:
            latest_takeover = load_json(paths.state / "current_find_claude_takeover_result.json", {})
            takeover = latest_takeover if isinstance(latest_takeover, dict) and latest_takeover else {"status": "selection_only_requested", "return_code": 0, "started_at": now_iso(), "finished_at": now_iso(), "prompt_path": "", "selection_only_requested": True}
        elif not claude_current and not args.force:
            latest_takeover = load_json(paths.state / "current_find_claude_takeover_result.json", {})
            if claude_takeover_is_current(latest_takeover, find_revision) and not validation_requires_fresh_takeover:
                takeover = {**latest_takeover, "status": latest_takeover.get("status") or "already_current"}
            else:
                record_stale_claude_takeover(paths, run_id, latest_takeover, find_revision)
                takeover = run_claude_current_find_takeover(
                    args.project,
                    paths,
                    run_id,
                    effective_read_limit,
                    args.idea_count,
                    repair_validation=validation_payload if validation_requires_fresh_takeover else None,
                    attempt=2 if validation_requires_fresh_takeover else 1,
                )
                changed_run = _find_run_changed(paths, run_id)
                if changed_run:
                    _write_find_changed_blocker(paths, run_id, changed_run, takeover)
                    print(json.dumps({"status": "blocked_current_find_changed_during_claude_takeover", "previous_run_id": run_id, "run_id": changed_run, "claude_takeover": takeover}, ensure_ascii=False, indent=2))
                    return 2
                existing_readings, existing_ideas, existing_plans = load_claude_outputs(taste_dir, run_id, find_results, effective_read_limit, paths.state, find_revision, paths, write_pending_validation=False)
        elif args.force and not args.force_selection:
            existing_readings, existing_ideas, existing_plans, targeted_queries, positive_validation = _refresh_current_find_claude_outputs(
                paths,
                taste_dir,
                run_id,
                find_results,
                effective_read_limit,
                find_revision,
            )
            if _current_find_contract_ready(existing_readings, existing_ideas, existing_plans, targeted_queries, positive_validation, run_id, min_required_readings, args.idea_count, find_revision):
                latest_takeover = load_json(paths.state / "current_find_claude_takeover_result.json", {})
                takeover = latest_takeover if isinstance(latest_takeover, dict) and latest_takeover else {"status": "existing_current_find_fragments_refreshed", "return_code": 0, "started_at": now_iso(), "finished_at": now_iso(), "prompt_path": ""}
                takeover = {
                    **takeover,
                    "status": "already_current_valid_claude_artifacts",
                    "run_id": run_id,
                    "return_code": 0,
                    "finished_at": now_iso(),
                    "artifact_validated_without_force_rerun": True,
                }
                write_current_find_structured_artifacts(paths, taste_dir, run_id, existing_readings, existing_ideas, existing_plans, takeover, positive_validation)
                payload = ensure_claude_plan_state(args.project, paths, run_id, existing_readings, existing_ideas, existing_plans, takeover, idea_count=args.idea_count)
                update_literature_packet(paths, run_id, existing_readings, existing_ideas, existing_plans)
                update_frontend_state(paths, run_id, existing_readings, existing_ideas, existing_plans)
                selection_receipt = sync_current_find_selection_success_receipt(
                    paths,
                    run_id,
                    takeover,
                    existing_ideas,
                    existing_plans,
                    positive_validation,
                    reason="valid_existing_fragments_before_force_rerun",
                )
                save_json(paths.state / "current_find_claude_takeover_result.json", {
                    **takeover,
                    "contract_validation_valid": True,
                    "contract_failure": None,
                    "reading_validation": positive_validation,
                    "selected_execution": selection_receipt.get("selected_execution", {}),
                    "selected_plan_id": selection_receipt.get("selected_plan_id"),
                    "selected_idea_id": selection_receipt.get("selected_idea_id"),
                    "validated_at": now_iso(),
                })
                print(json.dumps({"status": payload.get("status"), "source": payload.get("source"), "run_id": run_id, "readings": len(existing_readings), "ideas": len(existing_ideas), "plans": len(existing_plans), "claude_takeover": takeover, "claude_selection": selection_receipt, "validated_against_current_find": True, "public_projection": "refreshed"}, ensure_ascii=False, indent=2))
                return 0
            force_contract_failure = load_json(paths.state / "current_find_claude_takeover_contract_failure.json", {})
            force_failure_type = str(force_contract_failure.get("failure_type") or "").strip() if isinstance(force_contract_failure, dict) else ""
            force_repair_from_contract = bool(
                isinstance(force_contract_failure, dict)
                and force_contract_failure.get("run_id") == run_id
                and force_failure_type in {
                    "recoverable_current_find_tool_policy_blocked",
                    "idea_contract_failed",
                    "plan_contract_failed",
                    "missing_selected_plan",
                    "ambiguous_selected_plan",
                    "selected_plan_id_missing",
                    "selected_plan_missing_matching_idea",
                }
            )
            force_repair_attempt = max(
                1,
                min(
                    CURRENT_FIND_MAX_TAKEOVER_REPAIR_ATTEMPTS,
                    (_positive_int(force_contract_failure.get("repair_attempt")) or 1) + 1,
                ),
            ) if force_repair_from_contract else (2 if validation_requires_fresh_takeover else 1)
            force_repair_validation = force_contract_failure if force_repair_from_contract else (validation_payload if validation_requires_fresh_takeover else None)
            takeover = run_claude_current_find_takeover(
                args.project,
                paths,
                run_id,
                effective_read_limit,
                args.idea_count,
                repair_validation=force_repair_validation,
                attempt=force_repair_attempt,
            )
            changed_run = _find_run_changed(paths, run_id)
            if changed_run:
                _write_find_changed_blocker(paths, run_id, changed_run, takeover)
                print(json.dumps({"status": "blocked_current_find_changed_during_claude_takeover", "previous_run_id": run_id, "run_id": changed_run, "claude_takeover": takeover}, ensure_ascii=False, indent=2))
                return 2
            existing_readings, existing_ideas, existing_plans = load_claude_outputs(taste_dir, run_id, find_results, effective_read_limit, paths.state, find_revision, paths, write_pending_validation=False)
        if 'takeover' not in locals():
            latest_takeover = load_json(paths.state / "current_find_claude_takeover_result.json", {})
            current_validation = load_json(paths.state / "current_find_claude_reading_validation.json", {})
            if claude_takeover_is_current(latest_takeover, find_revision) and not current_reading_validation_requires_fresh_takeover(current_validation, run_id):
                takeover = {**latest_takeover, "status": latest_takeover.get("status") or "already_current"}
            else:
                record_stale_claude_takeover(paths, run_id, latest_takeover, find_revision)
                takeover = {"status": "stale_or_missing_current_find_takeover", "return_code": 0, "started_at": now_iso(), "finished_at": now_iso(), "prompt_path": "", "current_find_revision_at": find_revision.isoformat() if find_revision is not None else ""}
        min_readings = min(effective_read_limit, max(1, len(_current_recommendation_identities(find_results)[1]) or len(_current_positive_identities(find_results)[1])))
        takeover_failed_without_repair = bool(
            isinstance(takeover, dict)
            and takeover.get("return_code") not in (None, 0, "0")
            and not current_find_takeover_repairable_failure(takeover)
        )
        if takeover_failed_without_repair:
            positive_validation = validation_payload if isinstance(validation_payload, dict) else {}
            valid_artifacts_ready = False
        else:
            existing_readings, existing_ideas, existing_plans, targeted_queries, positive_validation = _refresh_current_find_claude_outputs(paths, taste_dir, run_id, find_results, effective_read_limit, find_revision)
            valid_artifacts_ready = _current_find_contract_ready(existing_readings, existing_ideas, existing_plans, targeted_queries, positive_validation, run_id, min_readings, args.idea_count, find_revision)
        if valid_artifacts_ready:
            artifact_times = [
                value for value in [
                    _artifact_generated_or_modified_at(taste_dir / "read_results.json"),
                    _artifact_generated_or_modified_at(taste_dir / "ideas.json"),
                    _artifact_generated_or_modified_at(taste_dir / "plans.json"),
                    parse_iso_time(positive_validation.get("generated_at") if isinstance(positive_validation, dict) else None),
                ] if value is not None
            ]
            artifact_finished = max(artifact_times, default=dt.datetime.now(dt.timezone.utc))
            takeover = {
                "status": "already_current_valid_claude_artifacts",
                "return_code": 0,
                "started_at": find_revision.isoformat() if find_revision is not None else artifact_finished.isoformat(),
                "finished_at": artifact_finished.isoformat(),
                "prompt_path": str(paths.state / "current_find_claude_takeover_repair_prompt_attempt2.md"),
                "artifact_validated_without_repair_rerun": True,
            }
            write_current_find_structured_artifacts(paths, taste_dir, run_id, existing_readings, existing_ideas, existing_plans, takeover, positive_validation)
            payload = ensure_claude_plan_state(args.project, paths, run_id, existing_readings, existing_ideas, existing_plans, takeover, idea_count=args.idea_count)
            update_literature_packet(paths, run_id, existing_readings, existing_ideas, existing_plans)
            update_frontend_state(paths, run_id, existing_readings, existing_ideas, existing_plans)
            selection_receipt = sync_current_find_selection_success_receipt(
                paths,
                run_id,
                takeover,
                existing_ideas,
                existing_plans,
                positive_validation,
                reason="valid_artifacts_ready",
            )
            save_json(paths.state / "current_find_claude_takeover_result.json", {**takeover, "contract_validation_valid": True, "contract_failure": None, "reading_validation": positive_validation, "selected_execution": selection_receipt.get("selected_execution", {})})
            print(json.dumps({"status": payload.get("status"), "source": payload.get("source"), "run_id": run_id, "readings": len(existing_readings), "ideas": len(existing_ideas), "plans": len(existing_plans), "claude_takeover": takeover, "claude_selection": selection_receipt, "validated_against_current_find": True, "public_projection": "refreshed"}, ensure_ascii=False, indent=2))
            return 0
        takeover, existing_readings, existing_ideas, existing_plans, targeted_queries, positive_validation, changed_run = maybe_repair_current_find_takeover(
            args.project,
            paths,
            taste_dir,
            run_id,
            find_results,
            takeover,
            existing_readings,
            existing_ideas,
            existing_plans,
            targeted_queries,
            positive_validation,
            effective_read_limit,
            min_readings,
            args.idea_count,
            find_revision,
            selection_only_requested=args.force_selection,
        )
        if changed_run:
            _write_find_changed_blocker(paths, run_id, changed_run, takeover)
            print(json.dumps({"status": "blocked_current_find_changed_during_claude_takeover", "previous_run_id": run_id, "run_id": changed_run, "claude_takeover": takeover}, ensure_ascii=False, indent=2))
            return 2
        validation_ok = bool(current_reading_validation_ready(positive_validation, run_id, min_readings) and claude_output_payloads_are_current([positive_validation], find_revision))
        if _current_find_contract_ready(existing_readings, existing_ideas, existing_plans, targeted_queries, positive_validation, run_id, min_readings, args.idea_count, find_revision):
            write_current_find_structured_artifacts(paths, taste_dir, run_id, existing_readings, existing_ideas, existing_plans, takeover, positive_validation)
            payload = ensure_claude_plan_state(args.project, paths, run_id, existing_readings, existing_ideas, existing_plans, takeover, idea_count=args.idea_count)
            update_literature_packet(paths, run_id, existing_readings, existing_ideas, existing_plans)
            update_frontend_state(paths, run_id, existing_readings, existing_ideas, existing_plans)
            selection_fields = current_find_selection_fields(existing_ideas, existing_plans, source=CLAUDE_TAKEOVER_SOURCE, executable=True)
            success_takeover = {
                **takeover,
                "contract_validation_valid": True,
                "contract_failure": None,
                "reading_validation": positive_validation,
                "selected_execution": selection_fields,
                "validated_at": now_iso(),
            }
            save_json(paths.state / "current_find_claude_takeover_result.json", success_takeover)
            selection_receipt = sync_current_find_selection_success_receipt(
                paths,
                run_id,
                success_takeover,
                existing_ideas,
                existing_plans,
                positive_validation,
                reason="contract_ready_after_repair",
            )
            print(json.dumps({"status": payload.get("status"), "source": payload.get("source"), "run_id": run_id, "readings": len(existing_readings), "ideas": len(existing_ideas), "plans": len(existing_plans), "claude_takeover": success_takeover, "claude_selection": selection_receipt}, ensure_ascii=False, indent=2))
            return 0
        reading_validation = load_json(paths.state / "current_find_claude_reading_validation.json", {})
        observed = _current_find_observed_with_takeover(
            current_find_takeover_observed(taste_dir, existing_readings, existing_ideas, existing_plans, targeted_queries, reading_validation, args.idea_count),
            takeover,
            find_revision,
            taste_dir,
            reading_validation,
        )
        failure_type = current_find_contract_failure_type(reading_validation, observed, idea_count=args.idea_count)
        next_action = current_find_contract_next_required_action(reading_validation, observed, idea_count=args.idea_count)
        selection_fields = current_find_selection_fields(existing_ideas, existing_plans, source=CLAUDE_TAKEOVER_SOURCE, executable=False)
        selection_failure = failure_type in {"missing_selected_plan", "ambiguous_selected_plan", "selected_plan_id_missing", "selected_plan_missing_matching_idea"}
        idea_failure = failure_type == "idea_contract_failed"
        plan_failure = failure_type == "plan_contract_failed"
        incomplete_content_ready = bool(len(existing_readings) == min_readings and validation_ok and len(existing_ideas) >= args.idea_count and _ideas_three_part_ready(existing_ideas, args.idea_count) and len(existing_plans) >= args.idea_count and _plans_contract_ready(existing_plans, args.idea_count) and len(targeted_queries) >= 3)
        incomplete = {
            "status": "blocked_current_find_full_text_evidence_pending" if failure_type == "full_text_evidence_missing" else "blocked_current_find_idea_contract_failed" if idea_failure else "blocked_current_find_plan_contract_failed" if plan_failure else "blocked_ambiguous_selected_plan" if failure_type == "ambiguous_selected_plan" else "blocked_missing_selected_plan" if selection_failure else "blocked_claude_current_find_takeover_incomplete",
            "failure_type": failure_type,
            "next_required_action": next_action,
            "next_required_stage": "acquire_current_find_full_text_evidence" if failure_type == "full_text_evidence_missing" else next_action if (selection_failure or idea_failure or plan_failure) else "repair_current_find_claude_read_idea_plan",
            "base_selection_status": "blocked_by_current_find_full_text_evidence" if failure_type == "full_text_evidence_missing" else "blocked_by_current_find_idea_contract" if idea_failure else "blocked_by_current_find_plan_contract" if plan_failure else "blocked_by_current_find_execution_selection" if selection_failure else "blocked_by_current_find_read_idea_plan_contract",
            "content_ready": incomplete_content_ready,
            "read_idea_plan_ready": incomplete_content_ready,
            "execution_ready": False,
            "takeover_ready": False,
            "claude_current_find_ready": incomplete_content_ready,
            "selected_execution_issue": failure_type if selection_failure else "",
            "run_id": run_id,
            "required": {"readings": effective_read_limit, "minimum_positive_anchor_coverage": len(_current_positive_identities(find_results)[1]), "ideas": args.idea_count, "plans": args.idea_count, "targeted_search_queries": 3},
            "observed": {**observed, "reading_validation": reading_validation},
            "targeted_search_queries": targeted_queries,
            "claude_takeover": takeover,
            **selection_fields,
            "guardrail": "Claude Code takeover has executed, but TASTE cannot mark Read complete until every Read-stage packet paper has full-text/PDF/HTML evidence." if failure_type == "full_text_evidence_missing" else "Claude Code takeover is mandatory for current Find Read/Idea/Plan; deterministic compatibility templates are disabled unless --skip-claude is explicitly supplied. Claude must read every Read-stage packet paper and record at least three supplemental search topics.",
        }
        incomplete = _sync_current_find_plan_reading_validation(incomplete, reading_validation)
        save_json(paths.state / "current_find_research_plan.json", incomplete)
        print(json.dumps(incomplete, ensure_ascii=False, indent=2))
        return 2
    already_current = downstream_matches_current_find(
        read_results if isinstance(read_results, dict) else {},
        ideas_results if isinstance(ideas_results, dict) else {},
        plans_results if isinstance(plans_results, dict) else {},
        run_id,
        papers,
    )
    if already_current and not args.force:
        readings = as_list(read_results.get("readings"))
        readings = normalize_readings_full_text_evidence(
            [_sanitize_reading_public_fields(row) for row in readings if isinstance(row, dict)],
            load_full_text_packet_from_taste_dir(taste_dir, run_id),
        )
        positive_ids, _positive_titles = _current_positive_identities(find_results)
        readings = _enforce_current_find_claim_policy(readings, positive_ids)
        ideas = as_list(ideas_results.get("ideas"))
        plans = as_list(plans_results.get("plans"))
        ideas, plans = enrich_public_projections(ideas, plans)
        fresh_plan = load_json(paths.state / "fresh_base_implementation_plan.json", {})
        execution_plan = build_execution_plan(args.project, cfg, run_id, readings, ideas, plans, fresh_plan if isinstance(fresh_plan, dict) else {})
        downstream_source = str(read_results.get("source") or ideas_results.get("source") or plans_results.get("source") or execution_plan.get("source") or "").strip()
        if downstream_source == CLAUDE_TAKEOVER_SOURCE:
            execution_plan["source"] = CLAUDE_TAKEOVER_SOURCE
            execution_plan["normalization_source"] = "already_current_claude_takeover_preserved"
            validation_payload = load_json(paths.state / "current_find_claude_reading_validation.json", {})
            validation_ready = current_reading_validation_ready(validation_payload, run_id, len(readings)) and claude_output_payloads_are_current([validation_payload], find_revision)
            execution_plan["reading_validation"] = validation_payload if isinstance(validation_payload, dict) else {}
            idea_contract_issues = _idea_rows_contract_issues(ideas, args.idea_count)
            raw_ideas = [row for row in as_list(ideas_results.get("ideas")) if isinstance(row, dict)]
            raw_plans = [row for row in as_list(plans_results.get("plans")) if isinstance(row, dict)]
            raw_idea_contract_issues = _idea_rows_contract_issues(raw_ideas, args.idea_count)
            plan_contract_issues = _plan_rows_contract_issues(plans, args.idea_count)
            raw_plan_contract_issues = _plan_rows_contract_issues(raw_plans, args.idea_count)
            idea_schema_ready = len(ideas) >= args.idea_count and not idea_contract_issues
            plan_schema_ready = len(plans) >= args.idea_count and not plan_contract_issues
            targeted_query_count = len(extract_targeted_search_queries(paths, read_results if isinstance(read_results, dict) else {}, ideas_results if isinstance(ideas_results, dict) else {}, plans_results if isinstance(plans_results, dict) else {}, execution_plan))
            content_ready = bool(validation_ready and idea_schema_ready and plan_schema_ready and targeted_query_count >= 3)
            selection_ready = current_find_selected_execution_ready(ideas, plans) if content_ready else False
            execution_plan["content_ready"] = content_ready
            execution_plan["read_idea_plan_ready"] = content_ready
            execution_plan["execution_ready"] = bool(content_ready and selection_ready)
            execution_plan["claude_current_find_ready"] = content_ready
            execution_plan["takeover_ready"] = bool(content_ready and selection_ready)
            execution_plan["idea_schema_ready"] = idea_schema_ready
            execution_plan["plan_schema_ready"] = plan_schema_ready
            execution_plan["idea_contract_issues"] = idea_contract_issues[:20]
            execution_plan["raw_idea_contract_issues"] = raw_idea_contract_issues[:20]
            execution_plan["plan_contract_issues"] = plan_contract_issues[:20]
            execution_plan["raw_plan_contract_issues"] = raw_plan_contract_issues[:20]
            if validation_ready and (not idea_schema_ready or not plan_schema_ready):
                observed = {
                    "readings": len(readings),
                    "ideas": len(ideas),
                    "plans": len(plans),
                    "raw_artifact_idea_count": len(raw_ideas),
                    "raw_artifact_plan_count": len(raw_plans),
                    "idea_schema_ready": idea_schema_ready,
                    "raw_idea_schema_ready": len(raw_ideas) >= args.idea_count and not raw_idea_contract_issues,
                    "plan_schema_ready": plan_schema_ready,
                    "raw_plan_schema_ready": len(raw_plans) >= args.idea_count and not raw_plan_contract_issues,
                    "idea_contract_issues": idea_contract_issues[:20],
                    "raw_idea_contract_issues": raw_idea_contract_issues[:20],
                    "plan_contract_issues": plan_contract_issues[:20],
                    "raw_plan_contract_issues": raw_plan_contract_issues[:20],
                    "targeted_search_queries": targeted_query_count,
                }
                if not idea_schema_ready:
                    execution_plan["status"] = "blocked_current_find_idea_contract_failed"
                    execution_plan["failure_type"] = "idea_contract_failed"
                    execution_plan["next_required_action"] = "rerun_current_find_claude_takeover_rewrite_and_score_ideas"
                    execution_plan["next_required_stage"] = "rerun_current_find_claude_takeover_rewrite_and_score_ideas"
                    execution_plan["base_selection_status"] = "blocked_by_current_find_idea_contract"
                    blockers = [f"Claude Code takeover must rewrite and objectively score {args.idea_count} ideas before TASTE can continue current-Find planning."] + _idea_contract_issue_summary_zh(idea_contract_issues or raw_idea_contract_issues)
                else:
                    execution_plan["status"] = "blocked_current_find_plan_contract_failed"
                    execution_plan["failure_type"] = "plan_contract_failed"
                    execution_plan["next_required_action"] = "rerun_current_find_claude_takeover_rewrite_plans_without_preselected_base"
                    execution_plan["next_required_stage"] = "rerun_current_find_claude_takeover_rewrite_plans_without_preselected_base"
                    execution_plan["base_selection_status"] = "blocked_by_current_find_plan_contract"
                    blockers = ["Claude Code takeover must rewrite plans without preselecting a concrete repo/base/data path/training command."] + _plan_contract_issue_summary_zh(plan_contract_issues or raw_plan_contract_issues)
                execution_plan["observed"] = {**observed, "reading_validation": validation_payload if isinstance(validation_payload, dict) else {}}
                execution_plan["blockers"] = blockers
            if content_ready and not selection_ready:
                selection_issue = current_find_selected_execution_issue(ideas, plans)
                execution_plan["status"] = "blocked_ambiguous_selected_plan" if selection_issue == "ambiguous_selected_plan" else "blocked_missing_selected_plan"
                execution_plan["failure_type"] = selection_issue or "missing_selected_plan"
                execution_plan["next_required_action"] = current_find_contract_next_required_action(validation_payload, {"selected_execution_issue": selection_issue or "missing_selected_plan"}, idea_count=args.idea_count)
                execution_plan["next_required_stage"] = execution_plan["next_required_action"]
                execution_plan["base_selection_status"] = execution_plan["status"]
                execution_plan["selected_execution_issue"] = selection_issue or "missing_selected_plan"
            if not validation_ready:
                raw_readings = [row for row in as_list(read_results.get("readings")) if isinstance(row, dict)]
                raw_ideas = [row for row in as_list(ideas_results.get("ideas")) if isinstance(row, dict)]
                raw_plans = [row for row in as_list(plans_results.get("plans")) if isinstance(row, dict)]
                validation_blockers = [str(item) for item in as_list((validation_payload if isinstance(validation_payload, dict) else {}).get("blockers")) if str(item).strip()]
                observed = {
                    "readings": len(readings),
                    "ideas": len(ideas),
                    "plans": len(plans),
                    "contract_reading_count": len(readings),
                    "contract_idea_count": len(ideas),
                    "contract_plan_count": len(plans),
                    "raw_artifact_reading_count": len(raw_readings),
                    "raw_artifact_idea_count": len(raw_ideas),
                    "raw_artifact_plan_count": len(raw_plans),
                    "validation_actual_reading_count": _positive_int((validation_payload if isinstance(validation_payload, dict) else {}).get("actual_reading_count")),
                    "validation_full_text_reading_count": _positive_int((validation_payload if isinstance(validation_payload, dict) else {}).get("full_text_reading_count")),
                    "validation_pending_full_text_reading_count": _positive_int((validation_payload if isinstance(validation_payload, dict) else {}).get("pending_full_text_reading_count")),
                    "validation_pending_without_evidence_count": _positive_int((validation_payload if isinstance(validation_payload, dict) else {}).get("pending_without_evidence_count")),
                    "idea_schema_ready": len(ideas) >= args.idea_count and not _idea_rows_contract_issues(ideas, args.idea_count),
                    "raw_idea_schema_ready": len(raw_ideas) >= args.idea_count and not _idea_rows_contract_issues(raw_ideas, args.idea_count),
                    "plan_schema_ready": len(plans) >= args.idea_count and not _plan_rows_contract_issues(plans, args.idea_count),
                    "raw_plan_schema_ready": len(raw_plans) >= args.idea_count and not _plan_rows_contract_issues(raw_plans, args.idea_count),
                    "idea_contract_issues": _idea_rows_contract_issues(ideas, args.idea_count)[:20],
                    "raw_idea_contract_issues": _idea_rows_contract_issues(raw_ideas, args.idea_count)[:20],
                    "plan_contract_issues": _plan_rows_contract_issues(plans, args.idea_count)[:20],
                    "raw_plan_contract_issues": _plan_rows_contract_issues(raw_plans, args.idea_count)[:20],
                    "targeted_search_queries": len(extract_targeted_search_queries(paths, read_results if isinstance(read_results, dict) else {}, ideas_results if isinstance(ideas_results, dict) else {}, plans_results if isinstance(plans_results, dict) else {}, execution_plan)),
                }
                failure_type = current_find_contract_failure_type(validation_payload, observed, idea_count=args.idea_count)
                next_action = current_find_contract_next_required_action(validation_payload, observed, idea_count=args.idea_count)
                execution_plan["status"] = "blocked_current_find_full_text_evidence_pending" if failure_type == "full_text_evidence_missing" else "blocked_current_find_idea_contract_failed" if failure_type == "idea_contract_failed" else "blocked_current_find_plan_contract_failed" if failure_type == "plan_contract_failed" else "blocked_current_find_deep_read_validation_pending"
                execution_plan["failure_type"] = failure_type
                execution_plan["next_required_action"] = next_action
                execution_plan["observed"] = {**observed, "reading_validation": validation_payload if isinstance(validation_payload, dict) else {}}
                execution_plan["blockers"] = (
                    [f"Claude Code takeover must rewrite and objectively score {args.idea_count} ideas before TASTE can continue current-Find planning."] + _idea_contract_issue_summary_zh(observed.get("idea_contract_issues") or observed.get("raw_idea_contract_issues") or [])
                    if failure_type == "idea_contract_failed" else
                    ["Claude Code takeover must rewrite plans without preselecting a concrete repo/base/data path/training command."] + _plan_contract_issue_summary_zh(observed.get("plan_contract_issues") or observed.get("raw_plan_contract_issues") or [])
                    if failure_type == "plan_contract_failed" else
                    (validation_blockers or ["current-Find reading validation has not passed the full-text deep-read policy."])
                )
                execution_plan["base_selection_status"] = "blocked_by_current_find_full_text_evidence" if failure_type == "full_text_evidence_missing" else "blocked_by_current_find_idea_contract" if failure_type == "idea_contract_failed" else "blocked_by_current_find_plan_contract" if failure_type == "plan_contract_failed" else "blocked_by_current_find_reading_validation"
                execution_plan["next_required_stage"] = "acquire_current_find_full_text_evidence" if failure_type == "full_text_evidence_missing" else next_action if failure_type in {"idea_contract_failed", "plan_contract_failed"} else "repair_current_find_claude_reading_validation"
        selection_fields = {key: execution_plan.get(key) for key in CURRENT_FIND_SELECTION_FIELD_KEYS}
        if isinstance(read_results, dict):
            read_results["readings"] = readings
            read_results.update(selection_fields)
            strip_verbose_claude_takeover(read_results)
            save_json(taste_dir / "read_results.json", read_results)
        if isinstance(ideas_results, dict):
            ideas_results["ideas"] = ideas
            ideas_results.update(selection_fields)
            strip_verbose_claude_takeover(ideas_results)
            save_json(taste_dir / "ideas.json", ideas_results)
        if isinstance(plans_results, dict):
            plans_results["plans"] = plans
            plans_results.update(selection_fields)
            strip_verbose_claude_takeover(plans_results)
            save_json(taste_dir / "plans.json", plans_results)
        execution_plan = _sync_current_find_plan_reading_validation(execution_plan, execution_plan.get("reading_validation"))
        if downstream_source == CLAUDE_TAKEOVER_SOURCE and execution_plan.get("takeover_ready") is True:
            write_current_find_structured_artifacts(paths, taste_dir, run_id, readings, ideas, plans, {}, execution_plan.get("reading_validation") if isinstance(execution_plan.get("reading_validation"), dict) else {})
        save_json(paths.state / "current_find_research_plan.json", execution_plan)
        save_json(paths.state / "experiment_plan.json", execution_plan)
        if not (downstream_source == CLAUDE_TAKEOVER_SOURCE and execution_plan.get("takeover_ready") is True):
            write_current_find_artifact_markdowns(paths, taste_dir, run_id, readings, ideas, plans)
        update_literature_packet(paths, run_id, readings, ideas, plans)
        update_frontend_state(paths, run_id, readings, ideas, plans)
        print(json.dumps({"status": "already_current", "run_id": run_id, "readings": len(readings), "ideas": len(ideas), "plans": len(plans), "targeted_search_queries": execution_plan.get("targeted_search_query_count", 0), "current_find_research_plan": str(paths.state / "current_find_research_plan.json"), "refreshed_metadata": True, "validated_against_current_find": True, "public_projection": "refreshed"}, ensure_ascii=False, indent=2))
        return 0
    readings = [build_reading(row, cfg) for row in papers]
    fresh_plan = load_json(paths.state / "fresh_base_implementation_plan.json", {})
    repo = fresh_plan.get("repo", {}) if isinstance(fresh_plan, dict) and isinstance(fresh_plan.get("repo", {}), dict) else {}
    ideas = build_ideas(readings, repo, fresh_plan if isinstance(fresh_plan, dict) else {}, cfg)[: args.idea_count]
    plans = build_plans(ideas, fresh_plan if isinstance(fresh_plan, dict) else {})
    ideas, plans = enrich_public_projections(ideas, plans)
    read_payload = {"run_id": run_id, "source": "current_find_bridge_compatibility_only", "generated_at": now_iso(), "readings": readings}
    idea_payload = {"run_id": run_id, "source": "current_find_bridge_compatibility_only", "generated_at": now_iso(), "ideas": ideas, "candidate_pool": [{"title": row.get("title"), "venue": row.get("venue"), "score": row.get("score")} for row in readings], "judge_scores": [{"id": row.get("id"), "score": row.get("score"), "recommendation": row.get("recommendation")} for row in ideas], "llm": {"enabled": False, "reason": "compatibility fallback only; normal TASTE flow delegates current Find Read/Idea/Plan to Claude Code"}}
    plan_payload = {"run_id": run_id, "source": "current_find_bridge_compatibility_only", "generated_at": now_iso(), "plans": plans}
    execution_plan = build_execution_plan(args.project, cfg, run_id, readings, ideas, plans, fresh_plan if isinstance(fresh_plan, dict) else {})
    selection_fields = {key: execution_plan.get(key) for key in CURRENT_FIND_SELECTION_FIELD_KEYS}
    read_payload.update(selection_fields)
    idea_payload.update(selection_fields)
    plan_payload.update(selection_fields)
    idea_payload["ideas"] = ideas
    plan_payload["plans"] = plans
    paper_quality = paper_quality_from_readings(readings, cfg, dt.datetime.now(dt.timezone.utc), run_id)
    save_json(taste_dir / "read_results.json", read_payload)
    save_json(taste_dir / "ideas.json", idea_payload)
    save_json(taste_dir / "plans.json", plan_payload)
    write_text(taste_dir / "read.md", render_read_md(readings, run_id))
    write_text(taste_dir / "idea.md", render_idea_md(ideas, run_id, _paper_reference_index(readings)))
    write_text(taste_dir / "plan.md", render_plan_md(plans, run_id, _paper_reference_index(readings)))
    save_json(paths.state / "current_find_research_plan.json", execution_plan)
    write_text(paths.planning / "current_find_research_plan.md", render_plan_md(plans, run_id, _paper_reference_index(readings)) + "\n## Claude Code 自主循环\n\n" + "\n".join(f"- {item}" for stage in execution_plan["claude_code_autonomous_loop"] for item in ([stage.get("stage", "")] + stage.get("commands", []) if isinstance(stage, dict) else [])) + "\n")
    save_json(paths.state / "idea_candidates.json", {"generated_at": now_iso(), "project": args.project, "source": "current_find_bridge", "current_find_run_id": run_id, "ideas": ideas, **selection_fields, "summary": {"idea_count": len(ideas), "pursue_count": sum(1 for row in ideas if str(row.get("recommendation", "")).startswith("pursue")), "watch_count": sum(1 for row in ideas if str(row.get("recommendation", "")).startswith("watch")), "prune_count": sum(1 for row in ideas if str(row.get("recommendation", "")).startswith("prune")), "current_find_run_id": run_id, "selected_idea_id": selection_fields.get("selected_idea_id", ""), "selected_plan_id": selection_fields.get("selected_plan_id", "")}})
    save_json(paths.state / "taste_plan_bridge.json", {"source": "current_find_bridge", "run_id": run_id, "plans_json": plan_payload, "plan_markdown_path": str(taste_dir / "plan.md"), "plan_markdown_excerpt": (taste_dir / "plan.md").read_text(encoding="utf-8")[:12000], **selection_fields, "guardrail": "Plan is tied to current Find; downstream environment, experiment, paper, and claim stages must consume only selected_plan_id. Non-selected plans remain backlog candidates."})
    save_json(paths.state / "experiment_plan.json", execution_plan)
    save_json(paths.state / "paper_quality.json", paper_quality)
    write_text(paths.planning / "paper_quality.md", render_paper_quality_md(paper_quality))
    update_literature_packet(paths, run_id, readings, ideas, plans)
    update_frontend_state(paths, run_id, readings, ideas, plans)
    copy_to_taste_run(paths, run_id, ["read_results.json", "ideas.json", "plans.json", "read.md", "idea.md", "plan.md"])
    result = {"status": "rebuilt_current_find_downstream", "run_id": run_id, "readings": len(readings), "ideas": len(ideas), "plans": len(plans), "files": {"read_results": str(taste_dir / "read_results.json"), "ideas": str(taste_dir / "ideas.json"), "plans": str(taste_dir / "plans.json"), "current_find_research_plan": str(paths.state / "current_find_research_plan.json"), "experiment_plan": str(paths.state / "experiment_plan.json")}}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
