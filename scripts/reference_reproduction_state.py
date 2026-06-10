#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _repo_path_from_mapping(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    for key in ["repo_path", "active_repo_path", "local_path", "path", "current_selected_repo_path"]:
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _selected_base_viability_repo_path(paths) -> str:
    """Return the still-active selected-base repo when a candidate switch conflicts.

    Base-switch artifacts are proposal/execution receipts for alternative routes.
    They must not override a wrapper-managed selected-base route when
    selected_base_viability_gate and the selected-base reference audit agree on
    the current repo.
    """
    gate = load_json(paths.state / "selected_base_viability_gate.json", {})
    if not isinstance(gate, dict):
        return ""
    status = str(gate.get("status") or "").lower()
    decision = str(gate.get("decision") or "").lower()
    if status != "blocked" or decision not in {"base_switch_gate_required", "continue_experiment_evidence_repair"}:
        return ""
    repo_path = _repo_path_from_mapping(gate)
    if not repo_path:
        return ""

    impl = load_json(paths.state / "fresh_base_implementation_plan.json", {})
    impl_repo = impl.get("repo") if isinstance(impl, dict) and isinstance(impl.get("repo"), dict) else {}
    guard = load_json(paths.state / "selected_base_route_guard.json", {})
    trusted = guard.get("trusted_audit") if isinstance(guard, dict) and isinstance(guard.get("trusted_audit"), dict) else {}
    legacy_audit = load_json(paths.state / "fresh_base_reference_reproduction_audit.json", {})
    legacy_selected = legacy_audit.get("selected_base") if isinstance(legacy_audit, dict) and isinstance(legacy_audit.get("selected_base"), dict) else {}
    aligned_paths = {
        value
        for value in [
            _repo_path_from_mapping(impl_repo),
            _repo_path_from_mapping(trusted),
            _repo_path_from_mapping(legacy_audit) or _repo_path_from_mapping(legacy_selected),
        ]
        if value
    }
    if aligned_paths and repo_path not in aligned_paths:
        return ""
    return repo_path


def current_impl_repo_path(paths) -> str:
    """Return the authoritative current selected-base repo path.

    The implementation plan can contain stale/proposal routes. A selected-base
    viability gate with matching reference evidence is authoritative over
    deterministic candidate-switch receipts.
    """
    viability_repo = _selected_base_viability_repo_path(paths)
    if viability_repo:
        return viability_repo

    selection = load_json(paths.state / "evidence_ready_repo_selection.json", {})
    if isinstance(selection, dict):
        selected = selection.get("selected") if isinstance(selection.get("selected"), dict) else {}
        gate = str(selection.get("selection_gate") or selected.get("selection_gate") or "")
        if gate.startswith(("accepted_by_claude", "accepted_by_deterministic_base_switch_gate")):
            for key in ["repo_path", "local_path", "path"]:
                value = str(selected.get(key) or "").strip()
                if value:
                    return value
    active = load_json(paths.state / "active_repo.json", {})
    if isinstance(active, dict):
        gate = str(active.get("selection_gate") or "")
        if gate.startswith(("accepted_by_claude", "accepted_by_deterministic_base_switch_gate")):
            for key in ["repo_path", "local_path", "path"]:
                value = str(active.get(key) or "").strip()
                if value:
                    return value
    impl = load_json(paths.state / "fresh_base_implementation_plan.json", {})
    repo = impl.get("repo", {}) if isinstance(impl, dict) and isinstance(impl.get("repo"), dict) else {}
    return str(repo.get("repo_path") or repo.get("local_path") or repo.get("path") or "").strip()


def artifact_matches_current_repo(paths, payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    current_repo = current_impl_repo_path(paths)
    if not current_repo:
        return False
    for key in ["repo_path", "active_repo_path", "local_path", "path"]:
        value = str(payload.get(key) or "").strip()
        if value and value != current_repo:
            return False
    return True


def audit_state_path(paths, mode: str) -> Path:
    mode = str(mode or "").strip().lower()
    if mode == "full":
        return paths.state / "fresh_base_reference_full_reproduction_audit.json"
    if mode == "bounded":
        return paths.state / "fresh_base_reference_bounded_reproduction_audit.json"
    return paths.state / f"fresh_base_reference_{mode or 'unknown'}_reproduction_audit.json"


def legacy_audit_state_path(paths) -> Path:
    return paths.state / "fresh_base_reference_reproduction_audit.json"


def audit_index_path(paths) -> Path:
    return paths.state / "fresh_base_reference_reproduction_index.json"


def _audit_metrics(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    for key in ["metrics", "test_metrics", "eval_metrics", "results"]:
        value = payload.get(key)
        if isinstance(value, dict) and value:
            return value
    return {}


def _looks_like_legacy_full_pass_audit(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("mode") not in (None, "", "full"):
        return False
    status = str(payload.get("status") or "").lower()
    route_scope = str(payload.get("route_scope") or "").lower()
    has_full_status = any(
        token in status
        for token in [
            "completed_reference_reproduction",
            "reference_reproduction_passed",
            "full_reference_reproduction_passed",
            "completed_full_reference_reproduction",
        ]
    )
    has_reference_scope = "reference" in route_scope or "reproduction" in status
    return bool(
        payload.get("return_code") == 0
        and payload.get("audit_ready")
        and payload.get("paper_level_reproduction_passed")
        and _audit_metrics(payload)
        and (payload.get("mode") == "full" or has_full_status or has_reference_scope)
    )


def normalize_full_audit_payload(paths, payload: dict[str, Any], audit_path: Path | None = None) -> dict[str, Any]:
    metrics = _audit_metrics(payload)
    normalized = dict(payload)
    normalized["mode"] = "full"
    normalized["metrics"] = metrics
    normalized.setdefault("repo_path", current_impl_repo_path(paths))
    normalized.setdefault("experiment_id", normalized.get("name") or normalized.get("candidate") or "selected_base_full_reference_reproduction")
    if normalized.get("artifact_audit_path") in (None, ""):
        normalized["artifact_audit_path"] = str(normalized.get("artifact_audit") or "")
    if audit_path:
        normalized.setdefault("source_audit_path", str(audit_path))
    return normalized


def is_full_pass_audit(payload: Any) -> bool:
    return bool(
        isinstance(payload, dict)
        and payload.get("mode") == "full"
        and payload.get("return_code") == 0
        and payload.get("audit_ready")
        and payload.get("paper_level_reproduction_passed")
        and _audit_metrics(payload)
    )


def is_importable_full_pass_audit(payload: Any) -> bool:
    return is_full_pass_audit(payload) or _looks_like_legacy_full_pass_audit(payload)


def is_bounded_pass_audit(payload: Any) -> bool:
    return bool(
        isinstance(payload, dict)
        and payload.get("mode") == "bounded"
        and payload.get("return_code") == 0
        and payload.get("audit_ready")
    )


def _dedupe_paths(paths_: list[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for path in paths_:
        try:
            key = str(path.resolve())
        except Exception:
            key = str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def audit_candidate_paths(paths, mode: str | None = None) -> list[Path]:
    mode = str(mode or "").strip().lower()
    candidates: list[Path] = []
    if mode in {"full", ""}:
        candidates.append(audit_state_path(paths, "full"))
    if mode in {"bounded", ""}:
        candidates.append(audit_state_path(paths, "bounded"))
    candidates.append(legacy_audit_state_path(paths))
    for path in sorted(paths.state.glob("*_reference_reproduction_audit.json")):
        candidates.append(path)
    index = load_json(audit_index_path(paths), {})
    if isinstance(index, dict):
        keys = [f"latest_{mode}"] if mode else ["latest_full", "latest_bounded"]
        for key in keys:
            row = index.get(key)
            if isinstance(row, dict):
                for field in ["state_audit_path", "artifact_audit_path"]:
                    value = str(row.get(field) or "").strip()
                    if value:
                        candidates.append(Path(value))
        rows = index.get("entries", []) if isinstance(index.get("entries"), list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if mode and str(row.get("mode") or "") != mode:
                continue
            for field in ["state_audit_path", "artifact_audit_path"]:
                value = str(row.get(field) or "").strip()
                if value:
                    candidates.append(Path(value))
    artifact_root = paths.artifacts / "fresh_base_reference_reproduction"
    if artifact_root.exists():
        artifacts = sorted(
            artifact_root.glob("selected_base_reference_*/audit.json"),
            key=lambda x: x.stat().st_mtime if x.exists() else 0,
            reverse=True,
        )
        candidates.extend(artifacts)
    return _dedupe_paths(candidates)


def iter_reference_audits(paths, mode: str | None = None):
    mode = str(mode or "").strip().lower()
    for path in audit_candidate_paths(paths, mode):
        payload = load_json(path, {})
        if not isinstance(payload, dict) or not payload:
            continue
        if not artifact_matches_current_repo(paths, payload):
            continue
        payload_mode = str(payload.get("mode") or "").strip().lower()
        if mode == "full" and is_importable_full_pass_audit(payload):
            yield path, normalize_full_audit_payload(paths, payload, path)
            continue
        if mode and payload_mode != mode:
            continue
        yield path, payload


def latest_reference_audit(paths, mode: str | None = None) -> tuple[Path | None, dict[str, Any]]:
    for path, payload in iter_reference_audits(paths, mode):
        return path, payload
    return None, {}


def upsert_audit_index(paths, payload: dict[str, Any], state_audit_path: Path, artifact_audit_path: Path | None = None) -> None:
    index_path = audit_index_path(paths)
    index = load_json(index_path, {})
    if not isinstance(index, dict):
        index = {}
    entries = index.get("entries", []) if isinstance(index.get("entries"), list) else []
    entries = [old for old in entries if artifact_matches_current_repo(paths, old)]
    entry = {
        "mode": payload.get("mode", ""),
        "experiment_id": payload.get("experiment_id", ""),
        "status": payload.get("status", ""),
        "decision": payload.get("decision", ""),
        "return_code": payload.get("return_code"),
        "audit_ready": bool(payload.get("audit_ready")),
        "paper_level_reproduction_passed": bool(payload.get("paper_level_reproduction_passed")),
        "repo_path": payload.get("repo_path", ""),
        "dataset": payload.get("dataset", ""),
        "artifact_dir": payload.get("artifact_dir", ""),
        "state_audit_path": str(state_audit_path),
        "artifact_audit_path": str(artifact_audit_path or payload.get("artifact_audit_path") or ""),
        "stdout_path": payload.get("stdout_path", ""),
        "finished_at": payload.get("finished_at", ""),
        "generated_at": payload.get("generated_at", ""),
    }
    identity = (entry["mode"], entry["experiment_id"], entry["state_audit_path"])
    entries = [old for old in entries if isinstance(old, dict) and (old.get("mode"), old.get("experiment_id"), old.get("state_audit_path")) != identity]
    entries.append(entry)
    index.update({
        "project": payload.get("project", ""),
        "updated_at": now_iso(),
        "entries": entries[-80:],
        f"latest_{payload.get('mode', 'unknown')}": entry,
    })
    save_json(index_path, index)


def write_mode_audit(paths, payload: dict[str, Any], artifact_audit_path: Path) -> Path:
    mode = str(payload.get("mode") or "unknown")
    state_path = audit_state_path(paths, mode)
    payload = {**payload, "state_audit_path": str(state_path), "artifact_audit_path": str(artifact_audit_path)}
    save_json(state_path, payload)
    save_json(artifact_audit_path, payload)
    legacy_path = legacy_audit_state_path(paths)
    legacy_existing = load_json(legacy_path, {})
    legacy_is_full_pass = is_full_pass_audit(legacy_existing)
    if mode == "full" or not legacy_is_full_pass:
        save_json(legacy_path, {**payload, "compatibility_pointer": str(state_path)})
    upsert_audit_index(paths, payload, state_path, artifact_audit_path)
    return state_path


def import_full_audit_if_verified(paths, audit_path: Path | None, payload: dict[str, Any]) -> tuple[Path | None, dict[str, Any]]:
    if not audit_path or not isinstance(payload, dict) or not artifact_matches_current_repo(paths, payload):
        return audit_path, payload
    if not is_importable_full_pass_audit(payload):
        return audit_path, payload
    normalized = normalize_full_audit_payload(paths, payload, audit_path)
    if not is_full_pass_audit(normalized):
        return audit_path, payload
    state_path = audit_state_path(paths, "full")
    artifact_pointer = Path(str(normalized.get("artifact_audit_path") or audit_path))
    try:
        same_path = audit_path.resolve() == state_path.resolve()
    except Exception:
        same_path = str(audit_path) == str(state_path)
    if same_path:
        normalized = {**normalized, "state_audit_path": str(state_path), "artifact_audit_path": str(artifact_pointer)}
        save_json(state_path, normalized)
        upsert_audit_index(paths, normalized, state_path, artifact_pointer)
        return state_path, normalized
    imported = {
        **normalized,
        "state_audit_path": str(state_path),
        "artifact_audit_path": str(artifact_pointer),
        "imported_from_state": str(audit_path) if paths.state in audit_path.parents else "",
        "imported_from_artifact": str(artifact_pointer),
        "import_verification": "verified_existing_full_audit_json: return_code=0, audit_ready=true, paper_level_reproduction_passed=true, metrics present, repo_path matches current selected-base",
        "imported_at": now_iso(),
    }
    save_json(state_path, imported)
    upsert_audit_index(paths, imported, state_path, artifact_pointer)
    return state_path, imported


def full_reference_audit_passed(paths) -> bool:
    path, payload = latest_reference_audit(paths, "full")
    _, payload = import_full_audit_if_verified(paths, path, payload)
    return is_full_pass_audit(payload)


def bounded_reference_audit_recorded(paths) -> bool:
    _, payload = latest_reference_audit(paths, "bounded")
    return is_bounded_pass_audit(payload)


def reference_full_job_state(paths) -> dict[str, Any]:
    for path in sorted(paths.state.glob("*_reference_full_reproduction_job.json")):
        state = load_json(path, {})
        if isinstance(state, dict) and artifact_matches_current_repo(paths, state):
            return state
    return {}
