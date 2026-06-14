#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from project_paths import build_paths


METRIC_NAMES = (
    "hr_at_5", "hr_at_10", "hr_at_20", "hr_at_50",
    "ndcg_at_1", "ndcg_at_3", "ndcg_at_5", "ndcg_at_10", "ndcg_at_20", "ndcg_at_50",
    "recall_at_1", "recall_at_3", "recall_at_5", "recall_at_10", "recall_at_20", "recall_at_50",
    "mrr_at_5", "mrr_at_10", "mrr_at_20", "mrr_at_50",
)


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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except Exception:
        return ""
    return digest.hexdigest()


def process_alive(pid: Any) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    return value > 0 and Path(f"/proc/{value}").exists()


def proc_cmdline(pid: Any) -> str:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return ""
    try:
        raw = Path(f"/proc/{value}/cmdline").read_bytes()
    except Exception:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()


def command_arg(command: list[Any], *names: str) -> str:
    tokens = [str(item) for item in command if str(item)]
    wanted = set(names)
    for index, token in enumerate(tokens):
        if token in wanted and index + 1 < len(tokens):
            return tokens[index + 1]
        for name in wanted:
            if token.startswith(name + "="):
                return token.split("=", 1)[1]
    return ""


def nested_value(data: dict[str, Any], dotted: str) -> Any:
    current: Any = data
    for part in dotted.split("."):
        if not isinstance(current, dict):
            return ""
        current = current.get(part)
    return current


def nonempty_nested(data: dict[str, Any], dotted: str) -> bool:
    value = nested_value(data, dotted)
    if isinstance(value, (list, tuple, dict)):
        return bool(value)
    return bool(str(value or "").strip())


def contract_schema_version(contract: dict[str, Any]) -> int:
    raw = contract.get("contract_schema_version", contract.get("schema_version", 0)) if isinstance(contract, dict) else 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def parse_epoch(line: str) -> int | None:
    patterns = (
        r"\bepoch\s+(\d+)\b",
        r"[\x27\"]epoch[\x27\"]\s*:\s*(\d+)",
    )
    for pattern in patterns:
        match = re.search(pattern, line, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def parse_metric_values(line: str) -> list[float]:
    values = re.findall(r"(?<![A-Za-z@])-?\d+(?:\.\d+)?(?:e[-+]?\d+)?", line, flags=re.IGNORECASE)
    out: list[float] = []
    for value in values:
        try:
            out.append(float(value))
        except ValueError:
            pass
    return out


def metric_key_from_header(label: str) -> str:
    match = re.match(r"\s*(HR|NDCG|MRR|Recall)@(\d+)\s*$", str(label or ""), flags=re.IGNORECASE)
    if not match:
        return ""
    return f"{match.group(1).lower()}_at_{match.group(2)}"


def parse_metric_table_after(lines: list[str], marker_index: int, current_epoch: int | None) -> dict[str, Any]:
    row: dict[str, Any] = {}
    line_index = marker_index
    end = min(marker_index + 24, len(lines))
    index = marker_index + 1
    while index < end:
        header = lines[index].strip()
        labels = re.findall(r"\b(?:HR|NDCG|MRR|Recall)@\d+\b", header, flags=re.IGNORECASE)
        if not labels:
            index += 1
            continue
        value_index = index + 1
        while value_index < end and not parse_metric_values(lines[value_index]):
            value_index += 1
        if value_index >= end:
            break
        values = parse_metric_values(lines[value_index])
        for label, value in zip(labels, values):
            key = metric_key_from_header(label)
            if key:
                row[key] = value
        line_index = value_index
        index = value_index + 1
    if row:
        row["epoch"] = current_epoch
        row["line_index"] = line_index
    return row


def parse_test_metrics(log_text: str) -> list[dict[str, Any]]:
    lines = log_text.splitlines()
    current_epoch: int | None = None
    tests: list[dict[str, Any]] = []
    seen_markers: set[int] = set()
    for index, line in enumerate(lines):
        epoch = parse_epoch(line)
        if epoch is not None:
            current_epoch = epoch
        marker = line.strip().upper()
        is_marker = (
            "TEST PHRASE" in marker
            or "TEST RESULTS" in marker
            or re.match(r"^-+\s*TEST\s*-+$", marker) is not None
        )
        if not is_marker or index in seen_markers:
            continue
        seen_markers.add(index)
        table_row = parse_metric_table_after(lines, index, current_epoch)
        if table_row.get("ndcg_at_10") is not None and table_row.get("ndcg_at_20") is not None:
            tests.append(table_row)
            continue
        for offset in range(index + 1, min(index + 12, len(lines))):
            candidate = lines[offset].strip()
            if not candidate or not candidate[0].isdigit() or "." not in candidate:
                continue
            values = parse_metric_values(lines[offset])
            if len(values) < 6:
                continue
            names = ("hr_at_10", "ndcg_at_10", "hr_at_20", "ndcg_at_20", "hr_at_50", "ndcg_at_50")
            row = {name: values[pos] for pos, name in enumerate(names)}
            row["epoch"] = current_epoch
            row["line_index"] = offset
            tests.append(row)
            break
    return tests


def parse_prefixed_metric_dict_logs(log_text: str) -> list[dict[str, Any]]:
    """Parse prefixed logger metric dictionaries from final evaluate() output.

    Training/validation metric dictionaries include loss_ keys, so they are
    deliberately ignored here. Completed candidate import still requires
    ndcg_at_10 and ndcg_at_20 plus separate completion evidence.
    """
    rows: list[dict[str, Any]] = []
    current_epoch: int | None = None
    value_re = re.compile(
        r"['\"](?:[A-Za-z0-9]+_)?(?P<metric>ndcg|recall)@(?P<cutoff>\d+)['\"]\s*:\s*(?:tensor\()?\s*(?P<value>-?\d+(?:\.\d+)?(?:e[-+]?\d+)?)",
        flags=re.IGNORECASE,
    )
    for index, line in enumerate(log_text.splitlines()):
        epoch = parse_epoch(line)
        if epoch is not None:
            current_epoch = epoch
        if "ndcg@" not in line.lower() or "loss_" in line:
            continue
        row: dict[str, Any] = {}
        for match in value_re.finditer(line):
            key = f"{match.group('metric').lower()}_at_{match.group('cutoff')}"
            try:
                row[key] = float(match.group("value"))
            except ValueError:
                pass
        if isinstance(row.get("ndcg_at_10"), (int, float)) and isinstance(row.get("ndcg_at_20"), (int, float)):
            row["epoch"] = current_epoch
            row["line_index"] = index
            rows.append(row)
    return rows


def select_best(rows: list[dict[str, Any]], metric: str) -> dict[str, Any]:
    usable = [row for row in rows if isinstance(row.get(metric), (int, float))]
    if not usable:
        return {}
    return max(usable, key=lambda row: float(row.get(metric, 0.0)))


def planned_epochs(config: dict[str, Any], artifact_dir: Path) -> int | None:
    _ = artifact_dir
    candidates: list[Any] = []
    if isinstance(config, dict):
        candidates.extend(config.get(key) for key in ("epoch", "epochs", "planned_epochs"))
        cfg = config.get("config")
        if isinstance(cfg, dict):
            candidates.extend(cfg.get(key) for key in ("epoch", "epochs", "planned_epochs"))
    for value in candidates:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return None


def last_epoch_seen(tests: list[dict[str, Any]], log_text: str) -> int | None:
    epochs = [row.get("epoch") for row in tests if isinstance(row.get("epoch"), int)]
    # Training logs can advance several epochs after the latest TEST block.
    # Completion detection must use the latest observed training epoch, not only
    # the latest evaluation epoch, otherwise finished runs can be missed.
    for line in log_text.splitlines():
        epoch = parse_epoch(line)
        if epoch is not None:
            epochs.append(epoch)
    return max(epochs) if epochs else None


def structured_completion_epochs(log_text: str) -> int | None:
    for line in log_text.splitlines():
        lowered = line.lower()
        if "completed" not in lowered or "status" not in lowered:
            continue
        for key in ("executed_epochs", "planned_epochs", "epochs", "epoch"):
            match = re.search(rf"[\"']{key}[\"']\s*:\s*(\d+)", line, flags=re.IGNORECASE)
            if match:
                try:
                    value = int(match.group(1))
                except ValueError:
                    continue
                if value > 0:
                    return value
    return None


def looks_like_training_command(text: Any) -> bool:
    lowered = str(text or "").lower()
    if not lowered or "python" not in lowered:
        return False
    if "exp_text_init" in lowered:
        return True
    if re.search(r"(?:^|\s)(?:\S*/)?main\.py\b", lowered) and re.search(r"(?:^|\s)--data(?:=|\s+)", lowered):
        return True
    return bool(re.search(r"(?:^|\s)(?:\S*/)?(?:finetune|train)[\w.-]*\.py\b", lowered))


def launcher_contract_matches_process(contract: dict[str, Any], cmdline: str) -> bool:
    if not isinstance(contract, dict) or not str(cmdline or "").strip():
        return False
    command = contract.get("command") if isinstance(contract.get("command"), list) else []
    command_display = str(contract.get("command_display") or "")
    candidates: set[str] = set()
    for item in command:
        token = str(item or "").strip()
        if not token or token.startswith("-"):
            continue
        name = Path(token).name
        if name.endswith(".py") or name.endswith(".sh") or "runner" in name.lower():
            candidates.add(name)
    for token in re.findall(r"[^\s]+", command_display):
        name = Path(token.strip("'\"")).name
        if name.endswith(".py") or name.endswith(".sh") or "runner" in name.lower():
            candidates.add(name)
    return any(candidate and candidate in cmdline for candidate in candidates)


def live_process_for_artifact(artifact_dir: Path, config: dict[str, Any]) -> bool:
    names = {artifact_dir.name}
    for sidecar in (artifact_dir / "run_contract.json", artifact_dir / "launcher.pid.json"):
        contract = load_json(sidecar, {})
        if not isinstance(contract, dict):
            continue
        pid = contract.get("pid")
        if process_alive(pid):
            cmdline = proc_cmdline(pid)
            if looks_like_training_command(cmdline) or launcher_contract_matches_process(contract, cmdline):
                return True
    if isinstance(config, dict):
        cfg = config.get("config") if isinstance(config.get("config"), dict) else {}
        for value in (config.get("descri"), config.get("description"), cfg.get("descri"), cfg.get("description")):
            if value:
                names.add(str(value))
    try:
        proc = subprocess.run(["ps", "-eo", "pid=,cmd="], text=True, capture_output=True, timeout=5)
    except Exception:
        return False
    for line in proc.stdout.splitlines():
        cmd = line.strip()
        if not looks_like_training_command(cmd):
            continue
        if str(artifact_dir) in cmd or any((f"--descri {name}" in cmd or name in cmd) for name in names if name):
            return True
    return False


def completion_evidence(artifact_dir: Path, config: dict[str, Any], log_text: str, tests: list[dict[str, Any]]) -> dict[str, Any]:
    explicit = bool(
        "Training complete" in log_text
        or re.search(r"\bDone\.\s*Best epoch\b", log_text, flags=re.IGNORECASE)
        or re.search(r"\bEarly stopping at epoch\s+\d+", log_text, flags=re.IGNORECASE)
        or re.search(r"[\"']status[\"']\s*:\s*[\"']completed[\"']", log_text, flags=re.IGNORECASE)
        or re.search(r"\bstatus\s*=\s*completed\b", log_text, flags=re.IGNORECASE)
    )
    failed = bool("Traceback (most recent call last)" in log_text or re.search(r"\b(?:Error|Exception|NameError|TypeError|RuntimeError):", log_text))
    configured_planned = planned_epochs(config, artifact_dir)
    structured_planned = structured_completion_epochs(log_text)
    last_epoch = last_epoch_seen(tests, log_text)
    planned = configured_planned or structured_planned or ((last_epoch + 1) if explicit and last_epoch is not None else None)
    if configured_planned:
        planned_source = "artifact_config"
    elif structured_planned:
        planned_source = "structured_completion_marker"
    elif planned:
        planned_source = "last_epoch_from_explicit_completion"
    else:
        planned_source = "missing"
    alive = live_process_for_artifact(artifact_dir, config)
    epoch_complete = bool(planned and last_epoch is not None and last_epoch >= planned - 1)
    completed = bool((explicit or epoch_complete) and not alive and not failed)
    if failed:
        reason = "training log contains a Python exception or traceback"
    elif explicit and not alive:
        reason = "explicit completion/early-stopping marker and matching training process is no longer alive"
    elif epoch_complete and not alive:
        reason = "planned final epoch observed and matching training process is no longer alive"
    elif alive:
        reason = "matching training process is still alive"
    elif planned:
        reason = f"final planned epoch not observed yet: last_epoch={last_epoch}, planned_epochs={planned}"
    else:
        reason = "no explicit completion marker and planned epoch count is unknown"
    return {
        "completed": completed,
        "failed": failed,
        "reason": reason,
        "explicit_training_complete_marker": explicit,
        "planned_epochs": planned,
        "planned_epochs_source": planned_source,
        "last_epoch_seen": last_epoch,
        "matching_process_alive": alive,
    }


def reference_baseline(paths) -> dict[str, Any]:
    reference_gate = load_json(paths.state / "reference_reproduction_gate.json", {})
    progress_gate = load_json(paths.state / "scientific_progress_gate.json", {})
    candidates = []
    if isinstance(reference_gate, dict):
        for key in ("best_reproduction", "best_control", "best_reference"):
            value = reference_gate.get(key)
            if isinstance(value, dict):
                candidates.append(value)
    if isinstance(progress_gate, dict):
        value = progress_gate.get("best_control")
        if isinstance(value, dict):
            candidates.append(value)
    for row in candidates:
        metric_name = str(row.get("metric_name") or row.get("metric") or "ndcg_at_10")
        value = row.get("metric_value")
        if value is None and isinstance(row.get("metrics"), dict):
            value = row.get("metrics", {}).get(metric_name) or row.get("metrics", {}).get("ndcg_at_10")
        try:
            metric_value = float(value)
        except (TypeError, ValueError):
            continue
        return {
            "experiment_id": row.get("experiment_id") or row.get("run_id") or row.get("name") or "selected_base_reference_full",
            "metric_name": metric_name,
            "metric_value": metric_value,
            "dataset": row.get("dataset") or "",
            "artifact_path": row.get("artifact_path") or row.get("artifact_dir") or "",
            "audit_path": row.get("audit_path") or row.get("artifact_audit_path") or "",
        }
    return {}


def current_repo(paths, audit_payload: dict[str, Any]) -> dict[str, str]:
    active = load_json(paths.state / "active_repo.json", {})
    selected = audit_payload.get("selected_base") if isinstance(audit_payload.get("selected_base"), dict) else {}
    return {
        "repo": str(active.get("repo") or active.get("url") or selected.get("url") or ""),
        "repo_name": str(active.get("name") or selected.get("name") or active.get("repo") or selected.get("url") or "current_selected_repo"),
        "repo_path": str(active.get("repo_path") or active.get("local_path") or selected.get("repo_path") or ""),
        "selected_base_title": str(active.get("selected_base_title") or active.get("title") or selected.get("literature_base_title") or selected.get("title") or audit_payload.get("paper_title") or "current selected base"),
    }


def normalize_registry(payload: Any) -> tuple[list[dict[str, Any]], bool, dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)], False, {}
    if isinstance(payload, dict):
        rows = payload.get("experiments") if isinstance(payload.get("experiments"), list) else payload.get("runs")
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)], True, dict(payload)
    return [], False, {}


def upsert_registry(paths, row: dict[str, Any]) -> bool:
    registry_path = paths.state / "experiment_registry.json"
    raw = load_json(registry_path, [])
    rows, wrapped, wrapper = normalize_registry(raw)
    target_id = str(row.get("experiment_id") or row.get("name") or "")
    changed = False
    next_rows: list[dict[str, Any]] = []
    replaced = False
    for existing in rows:
        existing_id = str(existing.get("experiment_id") or existing.get("name") or existing.get("id") or "")
        if existing_id == target_id:
            if not replaced:
                if existing != row:
                    changed = True
                next_rows.append(row)
                replaced = True
            else:
                changed = True
            continue
        next_rows.append(existing)
    if not replaced:
        next_rows.append(row)
        changed = True
    if wrapped:
        wrapper["experiments"] = next_rows
        wrapper["updated_at"] = now_iso()
        save_json(registry_path, wrapper)
    else:
        save_json(registry_path, next_rows)
    return changed


def artifact_candidates(paths, explicit: str = "") -> list[Path]:
    if explicit:
        return [Path(explicit).expanduser().resolve()]
    root = paths.root / "artifacts"
    if not root.exists():
        return []
    markers = {"stdout_stderr.log", "output.log", "run_contract.json", "experiment.json"}
    out: list[Path] = []
    seen: set[str] = set()
    for marker in markers:
        for file_path in root.glob(f"**/{marker}"):
            artifact_dir = file_path.parent
            if not artifact_dir.is_dir():
                continue
            if (artifact_dir / "CONTAMINATED_DO_NOT_IMPORT.txt").exists() or (artifact_dir / "FAILED_DO_NOT_IMPORT.txt").exists():
                continue
            key = str(artifact_dir.resolve())
            if key in seen:
                continue
            out.append(artifact_dir.resolve())
            seen.add(key)
    return sorted(out, key=lambda item: item.stat().st_mtime if item.exists() else 0)


def command_script_name(command: list[Any]) -> str:
    for item in command:
        text = str(item or "")
        if text.endswith(".py"):
            return Path(text).name
    return ""


def slugify_method_name(value: str, fallback: str = "diffusion_recommender") -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
    return slug or fallback


def method_from_command(command: list[Any], metadata_method: str = "") -> str:
    if metadata_method:
        return metadata_method
    script = command_script_name(command)
    model_type = command_arg(command, "--model_type", "--model-type")
    explicit_method = command_arg(command, "--method", "--method_slug", "--method-slug")
    if explicit_method:
        return explicit_method
    if script == "train_llm_cond_diff.py":
        return "llm_cond_discrete_diff"
    if script == "train_nullspace_discrete_diff.py":
        return "nullspace_discrete_diff"
    if script == "train_diffusion.py":
        return slugify_method_name(model_type)
    if script == "train_wrapper.py":
        if model_type:
            return f"{slugify_method_name(model_type, fallback='wrapper')}_sasrec"
        return "train_wrapper"
    if script:
        return Path(script).stem
    return ""


def config_from_contract(contract: dict[str, Any], command: list[Any]) -> dict[str, Any]:
    metadata = contract.get("experiment_metadata") if isinstance(contract.get("experiment_metadata"), dict) else {}
    extra = metadata.get("extra") if isinstance(metadata.get("extra"), dict) else {}
    cfg = dict(extra)
    dataset = str(metadata.get("dataset") or extra.get("dataset") or command_arg(command, "--data", "--dataset") or "").strip()
    method = method_from_command(command, str(metadata.get("method") or extra.get("method") or "").strip())
    role = str(metadata.get("role") or extra.get("role") or "candidate").strip() or "candidate"
    epoch = command_arg(command, "--epoch", "--epochs")
    if epoch:
        cfg.setdefault("epoch", epoch)
        cfg.setdefault("planned_epochs", epoch)
    model_type = command_arg(command, "--model_type", "--model-type")
    if model_type:
        cfg.setdefault("model_type", model_type)
    if dataset:
        cfg.setdefault("dataset", dataset)
    if method:
        cfg.setdefault("method", method)
    config: dict[str, Any] = {
        "method": method,
        "dataset": dataset,
        "comparison_role": role,
        "config": cfg,
        "config_source": "run_contract.command_audit" if (dataset or method) and not (metadata.get("dataset") and metadata.get("method")) else "run_contract.experiment_metadata",
    }
    if contract.get("command_display"):
        config["command"] = str(contract.get("command_display"))
    return config


def _text(value: Any) -> str:
    return str(value or "").strip()


def _lower_text(value: Any) -> str:
    return _text(value).lower()


def prior_audit_is_nonpromotable(audit: Any) -> bool:
    if not isinstance(audit, dict) or not audit:
        return False
    positive_markers = ("supports_claim", "claim_supported", "paper_evidence", "submission_ready")
    values = [
        _lower_text(audit.get("claim_verdict")),
        _lower_text(audit.get("promotion_status")),
        _lower_text(audit.get("evidence_status")),
        _lower_text(audit.get("audit_status")),
    ]
    if any(any(marker in value for marker in positive_markers) for value in values):
        return False
    negative_markers = (
        "unsupported",
        "not_supported",
        "not promotable",
        "candidate_observation_only",
        "negative_observation",
        "historical_record_only",
        "failed",
    )
    return any(any(marker in value for marker in negative_markers) for value in values)


def config_from_prior_audit(audit: dict[str, Any]) -> dict[str, Any]:
    assessment = audit.get("artifact_contract_assessment") if isinstance(audit.get("artifact_contract_assessment"), dict) else {}
    completion = audit.get("completion_evidence") if isinstance(audit.get("completion_evidence"), dict) else {}
    raw_config = audit.get("experiment_config") if isinstance(audit.get("experiment_config"), dict) else {}
    raw_cfg = raw_config.get("config") if isinstance(raw_config.get("config"), dict) else {}
    cfg = dict(raw_cfg)
    if completion.get("planned_epochs"):
        cfg.setdefault("planned_epochs", completion.get("planned_epochs"))
        cfg.setdefault("epoch", completion.get("planned_epochs"))
    return {
        "method": _text(audit.get("method") or audit.get("method_slug") or assessment.get("method")),
        "dataset": _text(audit.get("dataset") or assessment.get("dataset")),
        "comparison_role": _text(audit.get("comparison_role") or raw_config.get("comparison_role") or "candidate") or "candidate",
        "config": cfg,
        "command": _text(audit.get("command") or raw_config.get("command")),
        "started_at": _text(audit.get("started_at") or raw_config.get("started_at")),
        "config_source": "prior_artifact_local_audit.nonpromotable",
    }


def artifact_schema_blocker(artifact_dir: Path, experiment_path: Path, contract: dict[str, Any], config: dict[str, Any], prior_audit: Any) -> str:
    metadata = contract.get("experiment_metadata") if isinstance(contract.get("experiment_metadata"), dict) else {}
    method = str(config.get("method") or metadata.get("method") or "").strip()
    dataset = str(config.get("dataset") or metadata.get("dataset") or "").strip()
    if experiment_path.exists():
        missing = []
        if not method:
            missing.append("method")
        if not dataset:
            missing.append("dataset")
        if missing:
            return "artifact schema incomplete: missing " + ", ".join(missing) + " in experiment.json or run_contract.experiment_metadata"
        return ""
    contract_has_new_schema = bool(contract.get("environment_contract") or contract.get("expected_outputs") or metadata)
    if not contract_has_new_schema:
        if prior_audit_is_nonpromotable(prior_audit):
            missing = []
            if not method:
                missing.append("method")
            if not dataset:
                missing.append("dataset")
            if missing:
                return "legacy non-promotable artifact-local audit missing " + ", ".join(missing) + "; do not infer method/dataset from directory names"
            return ""
        if (artifact_dir / "audit.json").exists():
            return "legacy artifact-local audit is not explicitly non-promotable; missing experiment.json cannot be imported"
        return "legacy artifact lacks experiment.json and has no prior non-promotable artifact-local audit; do not infer method/dataset from directory names"
    missing = []
    if not method:
        missing.append("method")
    if not dataset:
        missing.append("dataset")
    if missing:
        return "artifact schema incomplete: missing " + ", ".join(missing) + " in experiment.json or run_contract.experiment_metadata"
    return ""


def command_schema_conflict(contract: dict[str, Any], config: dict[str, Any]) -> str:
    command = contract.get("command") if isinstance(contract.get("command"), list) else []
    if not command:
        return ""
    command_dataset = command_arg(command, "--data")
    artifact_dataset = str(config.get("dataset") or "").strip()
    if command_dataset and artifact_dataset and command_dataset.strip().lower() != artifact_dataset.lower():
        return f"artifact dataset {artifact_dataset!r} conflicts with launcher command --data {command_dataset!r}; do not infer or silently overwrite artifact-local schema"
    command_epoch = command_arg(command, "--epoch", "--epochs")
    cfg = config.get("config") if isinstance(config.get("config"), dict) else {}
    artifact_epoch = config.get("planned_epochs") or config.get("epoch") or cfg.get("planned_epochs") or cfg.get("epoch") or cfg.get("epochs")
    if command_epoch and artifact_epoch:
        try:
            command_value = int(command_epoch)
            artifact_value = int(artifact_epoch)
        except (TypeError, ValueError):
            return ""
        if command_value != artifact_value:
            return f"artifact planned_epochs {artifact_value!r} conflicts with launcher command epoch {command_value!r}; do not infer or silently overwrite artifact-local schema"
    return ""


def _command_artifact_dir_from_contract(contract: dict[str, Any]) -> str:
    command = contract.get("command") if isinstance(contract, dict) else []
    if not isinstance(command, list):
        return ""
    for index, item in enumerate(command):
        value = str(item or "")
        if value in {"--artifact_dir", "--artifact-dir"} and index + 1 < len(command):
            return str(command[index + 1] or "")
        if value.startswith("--artifact_dir=") or value.startswith("--artifact-dir="):
            return value.split("=", 1)[1]
    return ""


def _split_launcher_companion(paths, artifact_dir: Path) -> dict[str, str]:
    root = paths.root / "artifacts"
    try:
        candidates = sorted(root.glob(f"{artifact_dir.name}_20*/run_contract.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    except Exception:
        candidates = []
    for contract_path in candidates:
        contract = load_json(contract_path, {})
        command_artifact = _command_artifact_dir_from_contract(contract if isinstance(contract, dict) else {})
        if command_artifact and Path(command_artifact).resolve() == artifact_dir.resolve():
            log_path = contract_path.parent / "stdout_stderr.log"
            if log_path.exists():
                return {"log_path": str(log_path), "contract_path": str(contract_path), "launcher_artifact_dir": str(contract_path.parent)}
    return {}


def artifact_contract_assessment(artifact_dir: Path, log_path: Path, config: dict[str, Any], contract: dict[str, Any], completion: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "status": "pass" if passed else "block", "detail": detail})

    command = contract.get("command") if isinstance(contract.get("command"), list) else []
    dataset = str(config.get("dataset") or "").strip()
    method = str(config.get("method") or "").strip()
    planned = completion.get("planned_epochs")
    log_hash = sha256_file(log_path)
    schema_version = contract_schema_version(contract if isinstance(contract, dict) else {})
    expected_outputs = contract.get("expected_outputs") if isinstance(contract.get("expected_outputs"), dict) else {}
    launcher = str(contract.get("launcher") or "")
    stdout_expected = str(expected_outputs.get("stdout") or "")
    stdout_recorded = str(contract.get("stdout_path") or "")

    add("run_contract_present", isinstance(contract, dict) and bool(contract), str(artifact_dir / "run_contract.json"))
    add("log_present_and_hashed", log_path.exists() and bool(log_hash), str(log_path))
    add("command_recorded", bool(command), str(contract.get("command_display") or ""))
    add("dataset_recorded", bool(dataset), dataset or "missing dataset in artifact-local experiment schema")
    add("method_recorded", bool(method), method or "missing method in artifact-local experiment schema")
    add("planned_epochs_recorded", bool(planned), str(planned or "missing planned_epochs in artifact-local schema"))
    add("completion_evidence_recorded", bool(completion.get("completed") and completion.get("last_epoch_seen") is not None), str(completion))
    if schema_version >= 2:
        required_fields = contract.get("artifact_contract_required_fields") if isinstance(contract.get("artifact_contract_required_fields"), list) else []
        missing_required = []
        for field in required_fields:
            field_text = str(field)
            if nonempty_nested(contract, field_text):
                continue
            if field_text == "experiment_metadata.method" and method:
                continue
            if field_text == "experiment_metadata.dataset" and dataset:
                continue
            missing_required.append(field_text)
        add("contract_schema_version_v2", True, str(schema_version))
        add("artifact_required_fields_complete", not missing_required, ", ".join(missing_required) if missing_required else "complete_or_command_verified")
        add("launcher_recorded", launcher.endswith("/modules/experimenting/scripts/launch_experiment_run.py"), launcher or "missing launcher")
        add("stdout_path_matches_contract", bool(stdout_recorded and stdout_expected and Path(stdout_recorded).resolve() == log_path.resolve() and Path(stdout_expected).resolve() == log_path.resolve()), f"stdout_path={stdout_recorded}; expected_outputs.stdout={stdout_expected}; actual={log_path}")
        add("project_python_recorded", bool(nonempty_nested(contract, "environment_contract.required_python_executable") and contract.get("python_executable")), str(contract.get("python_executable") or ""))

    blockers = [row for row in checks if row.get("status") != "pass"]
    return {
        "status": "pass" if not blockers else "incomplete_audit",
        "audit_ready": not blockers,
        "checks": checks,
        "blockers": blockers,
        "dataset": dataset,
        "method": method,
        "log_sha256": log_hash,
        "contract_path": str(artifact_dir / "run_contract.json") if isinstance(contract, dict) and contract else "",
    }



def failure_summary_from_log(log_text: str) -> str:
    lines = [line.strip() for line in str(log_text or "").splitlines() if line.strip()]
    traceback_index = next((idx for idx, line in enumerate(lines) if "Traceback (most recent call last)" in line), -1)
    if traceback_index >= 0:
        return " | ".join(lines[traceback_index:traceback_index + 10])[-1200:]
    for line in reversed(lines[-100:]):
        if re.search(r"\b(?:Error|Exception|NameError|TypeError|RuntimeError):", line):
            return line[-1200:]
    return "training failed before parseable TEST metrics"


def import_failed_artifact(
    paths,
    artifact_dir: Path,
    *,
    log_path: Path,
    config: dict[str, Any],
    contract: dict[str, Any],
    split_companion: dict[str, str],
    log_text: str,
    reason: str,
    dry_run: bool,
) -> dict[str, Any]:
    method = str(config.get("method") or "").strip() if isinstance(config, dict) else ""
    dataset = str(config.get("dataset") or "").strip() if isinstance(config, dict) else ""
    if not method or not dataset:
        return {"artifact_dir": str(artifact_dir), "status": "skipped", "reason": "failed artifact lacks command-verified method/dataset schema"}
    reference_audit = load_json(paths.state / "fresh_base_reference_full_reproduction_audit.json", {})
    repo = current_repo(paths, reference_audit if isinstance(reference_audit, dict) else {})
    now = now_iso()
    started_at = str(config.get("started_at") or contract.get("started_at") or contract.get("created_at") or "")
    if not started_at:
        started_at = dt.datetime.fromtimestamp(artifact_dir.stat().st_ctime, tz=dt.timezone.utc).isoformat()
    finished_at = dt.datetime.fromtimestamp(log_path.stat().st_mtime, tz=dt.timezone.utc).isoformat()
    failure_text = failure_summary_from_log(log_text)
    audit_path = artifact_dir / "failure_audit.json"
    metrics_path = artifact_dir / "metrics.json"
    completion = {
        "completed": False,
        "failed": True,
        "reason": reason,
        "last_epoch_seen": last_epoch_seen([], log_text),
        "matching_process_alive": live_process_for_artifact(artifact_dir, config),
    }
    contract_assessment = artifact_contract_assessment(artifact_dir, log_path, config, contract, completion)
    row = {
        "timestamp": finished_at,
        "started_at": started_at,
        "updated_at": now,
        "finished_at": finished_at,
        "experiment_id": artifact_dir.name,
        "name": artifact_dir.name,
        "repo": repo["repo"],
        "repo_name": repo["repo_name"],
        "repo_path": repo["repo_path"],
        "selected_base_title": repo["selected_base_title"],
        "dataset": dataset,
        "method": method,
        "method_slug": method,
        "comparison_role": str(config.get("comparison_role") or "candidate"),
        "status": "failed",
        "metric_name": "",
        "metric_value": "",
        "metrics": {},
        "result": "failed_before_parseable_test_metrics",
        "promotion_status": "failed_not_promotable",
        "evidence_status": "failed_not_promotable",
        "claim_verdict": "unsupported",
        "audit_ready": False,
        "audit_path": "",
        "failure_audit_path": str(audit_path),
        "artifact_path": str(artifact_dir),
        "log_path": str(log_path),
        "config_path": str(artifact_dir / "run_contract.json") if (artifact_dir / "run_contract.json").exists() else "",
        "config_source": str(config.get("config_source") or "run_contract.command_audit"),
        "artifact_contract_status": contract_assessment.get("status"),
        "artifact_contract_blockers": contract_assessment.get("blockers", []),
        "command": str(config.get("command") or contract.get("command_display") or ""),
        "progress_epoch": completion.get("last_epoch_seen"),
        "planned_epochs": planned_epochs(config, artifact_dir),
        "process_alive": completion.get("matching_process_alive"),
        "human_goal": "记录当前选中基底下真实启动但失败的实验，防止历史运行被隐藏或误当作未执行。",
        "notes": "failed_not_promotable; deterministic import from run_contract/stdout_stderr; no paper claim is allowed.",
        "counterexample_outcome": f"实验失败，不能作为候选效果证据；失败原因：{failure_text}",
        "next_action": "先由 project agent 修复启动命令、保存目录或训练脚本错误，再通过 the launcher 重新运行；不得用该失败 run 支撑论文 claim。",
    }
    audit = {
        "project": paths.root.name,
        "experiment_id": artifact_dir.name,
        "status": "failed",
        "audit_status": "failed_not_audit_ready",
        "audit_ready": False,
        "artifact_dir": str(artifact_dir),
        "log_path": str(log_path),
        "log_sha256": sha256_file(log_path),
        "launcher_split_companion": split_companion,
        "config_source": row["config_source"],
        "artifact_contract_assessment": contract_assessment,
        "parsed_at": now,
        "metrics": {},
        "completion_evidence": completion,
        "failure_reason": reason,
        "failure_summary": failure_text,
        "promotion_status": "failed_not_promotable",
        "evidence_status": "failed_not_promotable",
        "claim_verdict": "unsupported",
        "guardrail": "Failed experiments are visible audit records only and cannot support paper claims.",
    }
    if dry_run:
        return {
            "artifact_dir": str(artifact_dir),
            "status": "would_import_failed",
            "changed": False,
            "experiment_id": artifact_dir.name,
            "failure_reason": reason,
            "failure_summary": failure_text,
            "artifact_contract_assessment": contract_assessment,
        }
    save_json(audit_path, audit)
    save_json(metrics_path, {})
    contract_path = artifact_dir / "run_contract.json"
    if contract_path.exists() and isinstance(contract, dict):
        next_contract = dict(contract)
        next_contract.update({
            "status": "failed",
            "updated_at": now,
            "process_alive": False,
            "completion_evidence": completion,
            "artifact_contract_assessment": contract_assessment,
            "audit_ready": False,
            "failure_audit_path": str(audit_path),
            "metrics_path": str(metrics_path),
            "registry_imported_at": now,
            "registry_import_status": "imported_failed",
            "finished_at": finished_at,
            "return_code": next_contract.get("return_code", 1),
        })
        save_json(contract_path, next_contract)
    changed = upsert_registry(paths, row)
    return {
        "artifact_dir": str(artifact_dir),
        "status": "imported_failed",
        "row_status": "failed",
        "changed": changed,
        "experiment_id": artifact_dir.name,
        "failure_audit_path": str(audit_path),
    }

def finalize_artifact_contract(
    artifact_dir: Path,
    *,
    completed: bool,
    running: bool,
    audit_ready: bool,
    finished_at: str,
    audit_path: Path,
    metrics_path: Path,
    bad_cases_path: Path,
    completion: dict[str, Any],
    contract_assessment: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Synchronize launcher contract after deterministic artifact import.

    The launcher writes a running contract before the detached worker exits. The
    importer is the deterministic finalization point: it has the log hash,
    completion evidence, and artifact-local audit status. Keep this schema local
    to the artifact so Web/API and watchdog do not rely on stale running fields.
    """
    contract_path = artifact_dir / "run_contract.json"
    contract = load_json(contract_path, {})
    if not isinstance(contract, dict) or not contract:
        return {"status": "missing_contract"}
    now = now_iso()
    metadata = contract.get("experiment_metadata") if isinstance(contract.get("experiment_metadata"), dict) else {}
    metadata = dict(metadata)
    if isinstance(config, dict):
        method = str(config.get("method") or "").strip()
        dataset = str(config.get("dataset") or "").strip()
        role = str(config.get("comparison_role") or config.get("role") or "").strip()
        if method and not str(metadata.get("method") or "").strip():
            metadata["method"] = method
        if dataset and not str(metadata.get("dataset") or "").strip():
            metadata["dataset"] = dataset
        if role and not str(metadata.get("role") or "").strip():
            metadata["role"] = role
    contract["experiment_metadata"] = metadata
    status = "completed" if completed else "running" if running else "incomplete"
    contract.update({
        "status": status,
        "updated_at": now,
        "process_alive": bool(running),
        "completion_evidence": completion,
        "artifact_contract_assessment": contract_assessment,
        "audit_ready": bool(audit_ready),
        "audit_path": str(audit_path) if audit_ready else "",
        "metrics_path": str(metrics_path) if metrics_path.exists() else "",
        "bad_case_path": str(bad_cases_path) if audit_ready and bad_cases_path.exists() else "",
        "registry_imported_at": now,
        "registry_import_status": "imported_completed" if completed else "imported_running" if running else "imported_incomplete",
    })
    if completed:
        contract.setdefault("started_at", contract.get("created_at") or "")
        contract["finished_at"] = finished_at
        contract["finalized_at"] = now
        contract["return_code"] = contract.get("return_code", 0)
    save_json(contract_path, contract)

    lock_path = artifact_dir / "run.lock"
    lock_released = False
    if completed and lock_path.exists() and not completion.get("matching_process_alive"):
        try:
            lock_path.unlink()
            lock_released = True
        except Exception:
            lock_released = False
    return {
        "status": status,
        "contract_path": str(contract_path),
        "lock_released": lock_released,
        "lock_path": str(lock_path),
    }


def import_artifact(paths, artifact_dir: Path, *, require_completed: bool = True, dry_run: bool = False) -> dict[str, Any]:
    artifact_dir = artifact_dir.expanduser().resolve()
    artifact_text = str(artifact_dir)
    if "/fresh_base_reference_reproduction/" in artifact_text or artifact_dir.name.startswith("selected_base_reference_"):
        return {"artifact_dir": artifact_text, "status": "skipped", "reason": "reference reproduction artifacts are managed by reference_reproduction_gate, not generic candidate import"}
    local_contract = load_json(artifact_dir / "run_contract.json", {})
    command_artifact = _command_artifact_dir_from_contract(local_contract if isinstance(local_contract, dict) else {})
    if command_artifact and Path(command_artifact).expanduser().resolve() != artifact_dir and Path(command_artifact).exists():
        return {
            "artifact_dir": str(artifact_dir),
            "status": "skipped",
            "reason": "launcher stdout/contract companion for split command artifact; import the command artifact_dir instead",
            "command_artifact_dir": str(Path(command_artifact).expanduser().resolve()),
        }
    experiment_path = artifact_dir / "experiment.json"
    log_path = artifact_dir / "output.log"
    split_companion: dict[str, str] = {}
    if not log_path.exists():
        log_path = artifact_dir / "stdout_stderr.log"
    if not log_path.exists():
        split_companion = _split_launcher_companion(paths, artifact_dir)
        if split_companion.get("log_path"):
            log_path = Path(split_companion["log_path"])
    if not log_path.exists():
        return {"artifact_dir": str(artifact_dir), "status": "skipped", "reason": "missing output.log or stdout_stderr.log"}

    contamination_marker = artifact_dir / "CONTAMINATED_DO_NOT_IMPORT.txt"
    if contamination_marker.exists():
        return {
            "artifact_dir": str(artifact_dir),
            "status": "skipped",
            "reason": "artifact marked contaminated; do not import",
            "contamination_marker": str(contamination_marker),
        }
    failure_marker = artifact_dir / "FAILED_DO_NOT_IMPORT.txt"
    if failure_marker.exists():
        return {
            "artifact_dir": str(artifact_dir),
            "status": "skipped",
            "reason": "artifact marked failed; do not import",
            "failure_marker": str(failure_marker),
        }

    raw_log = log_path.read_bytes()
    if b"\x00" in raw_log:
        return {"artifact_dir": str(artifact_dir), "status": "skipped", "reason": "log contains NUL bytes; artifact is not audit-clean"}
    log_text = raw_log.decode("utf-8", errors="replace")
    contract = load_json(artifact_dir / "run_contract.json", {})
    command = contract.get("command") if isinstance(contract, dict) and isinstance(contract.get("command"), list) else []
    prior_audit = load_json(artifact_dir / "audit.json", {})
    contract_metadata = contract.get("experiment_metadata") if isinstance(contract, dict) and isinstance(contract.get("experiment_metadata"), dict) else {}
    if experiment_path.exists():
        config_source = "experiment.json"
        config = load_json(experiment_path, {})
    elif not contract_metadata and prior_audit_is_nonpromotable(prior_audit):
        config_source = "prior_artifact_local_audit.nonpromotable"
        config = config_from_prior_audit(prior_audit if isinstance(prior_audit, dict) else {})
    else:
        config_source = "run_contract.experiment_metadata"
        config = config_from_contract(contract if isinstance(contract, dict) else {}, command)
    if not isinstance(config, dict):
        config_source = "invalid_experiment_schema"
        config = {}
    if isinstance(contract, dict):
        contract_metadata = contract.get("experiment_metadata") if isinstance(contract.get("experiment_metadata"), dict) else {}
        if contract_metadata.get("method") and not config.get("method"):
            config["method"] = str(contract_metadata.get("method"))
        if contract_metadata.get("dataset") and not config.get("dataset"):
            config["dataset"] = str(contract_metadata.get("dataset"))
    schema_blocker = artifact_schema_blocker(artifact_dir, experiment_path, contract if isinstance(contract, dict) else {}, config if isinstance(config, dict) else {}, prior_audit)
    if schema_blocker:
        return {
            "artifact_dir": str(artifact_dir),
            "status": "skipped",
            "reason": schema_blocker,
            "schema_status": "blocked",
            "required_schema": ["experiment.json", "run_contract.experiment_metadata.method", "run_contract.experiment_metadata.dataset"],
        }
    if isinstance(contract, dict):
        command = contract.get("command") if isinstance(contract.get("command"), list) else []
        cfg = config.get("config") if isinstance(config.get("config"), dict) else {}
        config["config"] = cfg
        if command:
            command_display = str(contract.get("command_display") or " ".join(str(item) for item in command))
            config.setdefault("command", command_display)
            command_conflict = command_schema_conflict(contract, config)
            if command_conflict:
                return {
                    "artifact_dir": str(artifact_dir),
                    "status": "skipped",
                    "reason": command_conflict,
                    "schema_status": "blocked",
                }
        if contract.get("started_at"):
            config.setdefault("started_at", contract.get("started_at"))
    tests = parse_test_metrics(log_text)
    if not tests:
        tests = parse_prefixed_metric_dict_logs(log_text)
    metrics_json = load_json(artifact_dir / "metrics.json", {})
    if not tests and isinstance(metrics_json, dict) and isinstance(metrics_json.get("all_evals"), list):
        for index, row in enumerate(metrics_json.get("all_evals") or []):
            if not isinstance(row, dict):
                continue
            parsed = {name: row.get(name) for name in METRIC_NAMES if isinstance(row.get(name), (int, float))}
            if isinstance(parsed.get("ndcg_at_10"), (int, float)) and isinstance(parsed.get("ndcg_at_20"), (int, float)):
                parsed["epoch"] = row.get("epoch")
                parsed["line_index"] = -1 - index
                tests.append(parsed)
    if not tests:
        if "Traceback (most recent call last)" in log_text or re.search(r"\b(?:Error|Exception|NameError|TypeError|RuntimeError):", log_text):
            return import_failed_artifact(
                paths,
                artifact_dir,
                log_path=log_path,
                config=config if isinstance(config, dict) else {},
                contract=contract if isinstance(contract, dict) else {},
                split_companion=split_companion,
                log_text=log_text,
                reason="training failed before parseable TEST metrics",
                dry_run=dry_run,
            )
        return {"artifact_dir": str(artifact_dir), "status": "skipped", "reason": "no TEST metrics parsed"}
    completion = completion_evidence(artifact_dir, config if isinstance(config, dict) else {}, log_text, tests)
    completed = bool(completion.get("completed"))
    if require_completed and not completed:
        if completion.get("failed"):
            return import_failed_artifact(
                paths,
                artifact_dir,
                log_path=log_path,
                config=config if isinstance(config, dict) else {},
                contract=contract if isinstance(contract, dict) else {},
                split_companion=split_companion,
                log_text=log_text,
                reason=str(completion.get("reason") or "training failed"),
                dry_run=dry_run,
            )
        return {
            "artifact_dir": str(artifact_dir),
            "status": "skipped",
            "reason": completion.get("reason") or "training log is not complete",
            "completion_evidence": completion,
        }


    running = bool(not completed and completion.get("matching_process_alive"))
    row_status = "completed" if completed else "running" if running else "incomplete"
    contract_assessment = artifact_contract_assessment(artifact_dir, log_path, config if isinstance(config, dict) else {}, contract if isinstance(contract, dict) else {}, completion)
    artifact_audit_ready = bool(completed and contract_assessment.get("audit_ready"))
    if completed and not artifact_audit_ready:
        return {
            "artifact_dir": str(artifact_dir),
            "status": "skipped",
            "reason": "completed artifact contract is incomplete; do not import completed results into registry until artifact-local audit is ready",
            "schema_status": "blocked",
            "completion_evidence": completion,
            "artifact_contract_assessment": contract_assessment,
        }
    audit_status = "audit_ready_negative_observation" if artifact_audit_ready else "running_not_audit_ready" if running else "incomplete_audit"

    best_by_ndcg20 = select_best(tests, "ndcg_at_20") or tests[-1]
    best_by_ndcg10 = select_best(tests, "ndcg_at_10") or tests[-1]
    final_test = tests[-1]
    reference = reference_baseline(paths)
    reference_value = reference.get("metric_value")
    selected_metric_value = float(best_by_ndcg20.get("ndcg_at_10") or best_by_ndcg10.get("ndcg_at_10") or final_test.get("ndcg_at_10"))
    try:
        reference_numeric = float(reference_value) if reference_value is not None else None
    except (TypeError, ValueError):
        reference_numeric = None
    reference_epsilon = 1e-6
    reference_delta = selected_metric_value - reference_numeric if reference_numeric is not None else None
    beats_reference = bool(reference_delta is not None and reference_delta > reference_epsilon)

    reference_audit = load_json(paths.state / "fresh_base_reference_full_reproduction_audit.json", {})
    repo = current_repo(paths, reference_audit if isinstance(reference_audit, dict) else {})
    experiment_id = artifact_dir.name
    started_at = str(config.get("started_at") or "") if isinstance(config, dict) else ""
    if not started_at:
        started_at = dt.datetime.fromtimestamp(artifact_dir.stat().st_ctime, tz=dt.timezone.utc).isoformat()
    finished_at = dt.datetime.fromtimestamp(log_path.stat().st_mtime, tz=dt.timezone.utc).isoformat()
    updated_at = now_iso()
    metrics = {
        "metric_selection": "best_by_ndcg_at_20_checkpoint",
        "selected_epoch": best_by_ndcg20.get("epoch"),
        "ndcg_at_10": selected_metric_value,
        "ndcg_at_20": float(best_by_ndcg20.get("ndcg_at_20")),
        "best_ndcg_at_10": float(best_by_ndcg10.get("ndcg_at_10")),
        "best_ndcg_at_10_epoch": best_by_ndcg10.get("epoch"),
        "best_ndcg_at_20": float(best_by_ndcg20.get("ndcg_at_20")),
        "best_ndcg_at_20_epoch": best_by_ndcg20.get("epoch"),
        "final_ndcg_at_10": float(final_test.get("ndcg_at_10")),
        "final_ndcg_at_20": float(final_test.get("ndcg_at_20")),
        "final_epoch": final_test.get("epoch"),
        "test_evaluation_count": len(tests),
    }
    audit_path = artifact_dir / "audit.json"
    running_status_path = artifact_dir / "running_status.json"
    metrics_path = artifact_dir / "metrics.json"
    bad_cases_path = artifact_dir / "bad_cases.json"
    reference_float = reference_numeric if reference_numeric is not None else 0.0
    bad_items = [
        {
            "slice": "aggregate_not_above_reference",
            "evidence": f"best_candidate_ndcg_at_10={selected_metric_value:.6f}; selected_base_reference_ndcg_at_10={reference_float:.6f}; delta={(reference_delta if reference_delta is not None else 0.0):.6f}",
            "action": "Do not promote this candidate; use it as negative evidence for protocol or method redesign.",
        },
        {
            "slice": "final_checkpoint_weakness",
            "evidence": f"final_epoch={final_test.get('epoch')}; final_ndcg_at_10={float(final_test.get('ndcg_at_10')):.6f}; final_ndcg_at_20={float(final_test.get('ndcg_at_20')):.6f}",
            "action": "Inspect training protocol and dataset fit before launching project-target follow-up runs.",
        },
    ]
    bad_case_summary = {
        "path": str(bad_cases_path),
        "count": len(bad_items),
        "items": bad_items,
        "slices": [item["slice"] for item in bad_items],
    }
    if beats_reference:
        counterexample_text = (
            f"候选最好 NDCG@10={selected_metric_value:.6f}，仅以当前解析指标看高于参考复现 NDCG@10={reference_float:.6f}；"
            "仍需确定性证据门控和多指标复核，不能直接支撑论文 claim。"
        )
    else:
        counterexample_text = (
            f"候选最好 NDCG@10={selected_metric_value:.6f}，未高于当前选中基底参考复现 NDCG@10={reference_float:.6f}；"
            "该实验只能作为负向/候选观察，不能支撑论文 claim。"
        )
    audit = {
        "project": paths.root.name,
        "experiment_id": experiment_id,
        "status": row_status,
        "audit_status": audit_status,
        "audit_ready": artifact_audit_ready,
        "artifact_dir": str(artifact_dir),
        "experiment_config_path": str(experiment_path) if experiment_path.exists() else "",
        "log_path": str(log_path),
        "log_sha256": sha256_file(log_path),
        "launcher_split_companion": split_companion,
        "config_source": config_source,
        "artifact_contract_assessment": contract_assessment,
        "parsed_at": updated_at,
        "metrics": metrics,
        "all_test_evaluations": tests,
        "reference_control": reference,
        "completion_evidence": completion,
        "comparison_status": "above_selected_base_reference_requires_gate_review" if beats_reference else "not_above_selected_base_reference" if reference_numeric is not None else "requires_manual_review",
        "promotion_status": "candidate_observation_only",
        "evidence_status": "candidate_observation_only",
        "claim_verdict": "unsupported",
        "novelty_note": "Current-route candidate observation; it is useful for pruning/protocol diagnosis but not for a paper contribution.",
        "counterexample_outcome": counterexample_text,
        "bad_cases": bad_case_summary,
        "bad_case_path": str(bad_cases_path),
        "bad_case_slices": bad_case_summary["slices"],
        "guardrail": "Running/incomplete candidates are progress records only; completed candidates remain audited observations unless deterministic evidence gates promote them. No paper claim is allowed from a running record.",
    }

    method = str(config.get("method") or "") if isinstance(config, dict) else ""
    dataset = str(config.get("dataset") or "") if isinstance(config, dict) else ""
    if not method:
        return {"artifact_dir": str(artifact_dir), "status": "skipped", "reason": "missing method after artifact schema validation", "schema_status": "blocked"}
    if not dataset:
        return {"artifact_dir": str(artifact_dir), "status": "skipped", "reason": "missing dataset after artifact schema validation", "schema_status": "blocked"}
    cfg = config.get("config") if isinstance(config, dict) and isinstance(config.get("config"), dict) else {}
    sem_source = str(cfg.get("sem_emb_path") or "")
    cluster_or_proxy = "cluster" in method.lower() or "cluster" in sem_source.lower()
    human_goal = "检验当前选中基底下的候选实验是否能超过当前参考复现。"
    notes = "candidate_observation_only; selected-base full reference reproduction remains the comparison control; no improvement claim is allowed."
    if cluster_or_proxy:
        human_goal = "检验当前选中基底下的 proxy/cluster 语义嵌入候选是否超过当前参考复现；该嵌入源未证明为真实 LLM/API 文本证据。"
        notes = "candidate_observation_only; proxy-derived candidate evidence is not promotable project-target evidence; no improvement claim is allowed."
    row = {
        "timestamp": updated_at if running else finished_at,
        "started_at": started_at,
        "updated_at": updated_at,
        "finished_at": "" if running else finished_at,
        "experiment_id": experiment_id,
        "name": experiment_id,
        "repo": repo["repo"],
        "repo_name": repo["repo_name"],
        "repo_path": repo["repo_path"],
        "selected_base_title": repo["selected_base_title"],
        "dataset": dataset,
        "method": method,
        "method_slug": method,
        "comparison_role": "candidate",
        "status": row_status,
        "metric_name": "ndcg_at_10",
        "metric_value": selected_metric_value,
        "metrics": metrics,
        "result": selected_metric_value,
        "baseline_status": "completed_selected_base_reference",
        "baseline_experiment_id": reference.get("experiment_id") or "selected_base_reference_full",
        "baseline_ndcg_at_10": reference.get("metric_value"),
        "comparison_status": audit["comparison_status"],
        "promotion_status": audit["promotion_status"],
        "evidence_status": audit["evidence_status"],
        "claim_verdict": audit["claim_verdict"],
        "audit_ready": artifact_audit_ready,
        "audit_path": str(audit_path) if artifact_audit_ready else "",
        "status_path": str(running_status_path) if running else "",
        "artifact_path": str(artifact_dir),
        "log_path": str(log_path),
        "config_path": str(experiment_path) if experiment_path.exists() else str(artifact_dir / "run_contract.json") if (artifact_dir / "run_contract.json").exists() else "",
        "config_source": config_source,
        "artifact_contract_status": contract_assessment.get("status"),
        "artifact_contract_blockers": contract_assessment.get("blockers", []),
        "command": str(config.get("command") or "") if isinstance(config, dict) else "",
        "progress_epoch": completion.get("last_epoch_seen"),
        "planned_epochs": completion.get("planned_epochs"),
        "process_alive": completion.get("matching_process_alive"),
        "semantic_embedding_source": sem_source,
        "semantic_embedding_evidence": "cluster_or_proxy_only_not_real_llm_text_evidence" if cluster_or_proxy else "requires_artifact_audit",
        "artifact_semantics": "cluster_or_proxy_semantic_candidate" if cluster_or_proxy else "semantic_conditioning_candidate",
        "pretrained_checkpoint": str(cfg.get("pretrained_ckpt") or ""),
        "human_goal": human_goal,
        "notes": notes,
        "counterexample_outcome": counterexample_text,
        "bad_case_path": str(bad_cases_path) if artifact_audit_ready else "",
        "bad_case_slices": bad_case_summary["slices"] if artifact_audit_ready else [],
        "bad_case_summary": bad_case_summary if artifact_audit_ready else {},
        "next_action": "等待当前训练完成；完成后由 project agent 写 artifact-local audit、登记最终指标，并刷新 scientific_progress/paper_evidence/submission_readiness。" if running else "由 project agent 继续在当前选中基底下设计新的真实文本语义增强候选实验，或写明可审计的剪枝理由。",
    }
    if dry_run:
        return {
            "artifact_dir": str(artifact_dir),
            "status": "would_import",
            "changed": False,
            "experiment_id": experiment_id,
            "metric_value": selected_metric_value,
            "baseline": reference.get("metric_value"),
            "audit_path": str(audit_path) if artifact_audit_ready else "",
            "config_source": config_source,
            "completion_evidence": completion,
            "artifact_contract_assessment": contract_assessment,
        }
    if artifact_audit_ready:
        save_json(bad_cases_path, bad_items)
        save_json(audit_path, audit)
    else:
        save_json(running_status_path, audit)
    save_json(metrics_path, metrics)
    finalization = finalize_artifact_contract(
        artifact_dir,
        completed=completed,
        running=running,
        audit_ready=artifact_audit_ready,
        finished_at=finished_at,
        audit_path=audit_path,
        metrics_path=metrics_path,
        bad_cases_path=bad_cases_path,
        completion=completion,
        contract_assessment=contract_assessment,
        config=config if isinstance(config, dict) else {},
    )
    changed = upsert_registry(paths, row)
    return {
        "artifact_dir": str(artifact_dir),
        "status": "imported",
        "row_status": row_status,
        "changed": changed,
        "experiment_id": experiment_id,
        "metric_value": selected_metric_value,
        "baseline": reference.get("metric_value"),
        "audit_path": str(audit_path),
        "finalization": finalization,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Import current-route experiment artifacts into the TASTE experiment registry.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--artifact-dir", default="")
    parser.add_argument("--scan-completed", action="store_true")
    parser.add_argument("--allow-incomplete", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Parse artifacts and report importability without writing audit, metrics, or registry files.")
    args = parser.parse_args()
    paths = build_paths(args.project)
    results = [import_artifact(paths, path, require_completed=not args.allow_incomplete, dry_run=args.dry_run) for path in artifact_candidates(paths, args.artifact_dir)]
    imported = [row for row in results if row.get("status") in {"imported", "imported_failed"}]
    would_import = [row for row in results if row.get("status") in {"would_import", "would_import_failed"}]
    print(json.dumps({"project": args.project, "dry_run": bool(args.dry_run), "imported": len(imported), "would_import": len(would_import), "results": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
