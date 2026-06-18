#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _taste_root() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "framework").is_dir() and (parent / "modules").is_dir() and (parent / "web").is_dir():
            return parent
    return current.parents[3]


_TASTE_ROOT = _taste_root()
for _entry in [_TASTE_ROOT / "framework" / "scripts", _TASTE_ROOT / "modules" / "experimenting" / "scripts"]:
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from project_paths import ROOT, build_paths, project_experiment_python_from_config
from reference_reproduction_state import audit_state_path, post_reference_reproduction_refresh, write_mode_audit


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


def update_json(path: Path, updates: dict[str, Any]) -> None:
    payload = load_json(path, {})
    if not isinstance(payload, dict):
        payload = {}
    payload.update(updates)
    save_json(path, payload)


def project_local_adapter(project: str) -> Path:
    return ROOT / "projects" / project / "scripts" / "adapters" / Path(__file__).name


def dispatch_project_local_adapter(project: str, argv: list[str]) -> int | None:
    adapter = project_local_adapter(project)
    if not adapter.exists():
        return None
    proc = subprocess.run([sys.executable, str(adapter), *argv], cwd=ROOT)
    return int(proc.returncode)


def sha256_file(path: Path) -> str:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except Exception:
        return ""


def selected_pretrain_checkpoint(repo: Path, dataset: str) -> Path:
    if not dataset:
        return Path("")
    return repo / "pretrain" / f"{dataset}_consistency_1.pth"


def write_torch_load_compat(artifact_dir: Path, repo: Path, dataset: str, mode: str, adapter: str) -> dict[str, Any]:
    """Create a one-run torch.load compatibility shim for trusted selected-base checkpoints.

    PyTorch 2.6+ changed torch.load's default to weights_only=True. Some official
    reference checkpoints in older repositories store full model objects and must be
    loaded with weights_only=False. The shim is scoped to one exact checkpoint path
    and only injected into this subprocess through PYTHONPATH.
    """
    checkpoint = selected_pretrain_checkpoint(repo, dataset)
    payload: dict[str, Any] = {
        "enabled": False,
        "kind": "torch_load_selected_checkpoint_compatibility",
        "scope": "disabled",
        "checkpoint_path": str(checkpoint) if checkpoint else "",
        "checkpoint_sha256": sha256_file(checkpoint) if checkpoint and checkpoint.exists() else "",
    }
    if mode != "full" or adapter != "selected_base_official_two_stage" or not checkpoint.exists():
        payload["reason"] = "compatibility shim is only needed for full selected-base two-stage checkpoints"
        return payload
    compat_dir = artifact_dir / "compat"
    compat_dir.mkdir(parents=True, exist_ok=True)
    sitecustomize = compat_dir / "sitecustomize.py"
    sitecustomize.write_text(
        """
from __future__ import annotations

import os
from pathlib import Path

try:
    import torch

    _ORIGINAL_TORCH_LOAD = torch.load
    _ALLOWED_CHECKPOINT = os.environ.get("SELECTED_BASE_TRUSTED_TORCHLOAD_CHECKPOINT", "").strip()
    _ALLOWED_RESOLVED = Path(_ALLOWED_CHECKPOINT).resolve() if _ALLOWED_CHECKPOINT else None

    def _selected_base_torch_load(file, *args, **kwargs):
        try:
            resolved = Path(file).resolve() if isinstance(file, (str, os.PathLike)) else None
        except Exception:
            resolved = None
        if _ALLOWED_RESOLVED is not None and resolved == _ALLOWED_RESOLVED and "weights_only" not in kwargs:
            kwargs["weights_only"] = False
        return _ORIGINAL_TORCH_LOAD(file, *args, **kwargs)

    torch.load = _selected_base_torch_load
except Exception:
    pass
""".lstrip(),
        encoding="utf-8",
    )
    payload.update(
        {
            "enabled": True,
            "scope": "single subprocess via artifact-local sitecustomize; only the exact selected-base pretrain checkpoint is loaded with weights_only=False",
            "reason": "PyTorch 2.6+ defaults torch.load to weights_only=True, while this official selected-base checkpoint stores a full model object needed by finetune.py.",
            "sitecustomize_path": str(sitecustomize),
            "sitecustomize_sha256": sha256_file(sitecustomize),
        }
    )
    return payload


def project_python(project: str) -> str:
    paths = build_paths(project)
    cfg = load_json(paths.config, {})
    return project_experiment_python_from_config(cfg if isinstance(cfg, dict) else {}, fallback_to_current=True)


def selected_repo(project: str) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    paths = build_paths(project)
    impl = load_json(paths.state / "fresh_base_implementation_plan.json", {})
    env = load_json(paths.state / "evidence_ready_repo_selection.json", {})
    repo = impl.get("repo", {}) if isinstance(impl, dict) and isinstance(impl.get("repo"), dict) else {}
    selected = env.get("selected", {}) if isinstance(env, dict) and isinstance(env.get("selected"), dict) else {}
    for value in [repo.get("repo_path"), repo.get("local_path"), repo.get("path"), selected.get("repo_path"), selected.get("local_path")]:
        candidate = str(value or "").strip()
        if candidate:
            return Path(candidate).resolve(), impl if isinstance(impl, dict) else {}, env if isinstance(env, dict) else {}
    return Path(""), impl if isinstance(impl, dict) else {}, env if isinstance(env, dict) else {}


def analysis_quickstart_command(repo: Path, mode: str) -> list[str]:
    analysis_dir = repo / "analysis"
    data_dir = repo / "data"
    if not analysis_dir.exists() or not data_dir.exists():
        return []
    scripts = sorted(path for path in analysis_dir.glob("*.py") if path.is_file())
    datasets = sorted(path for path in data_dir.glob("*.jsonl") if path.is_file())
    if not scripts or not datasets:
        return []
    outdir = "outputs/reference_full" if mode == "full" else "outputs/reference_bounded"
    n_boot = "2000" if mode == "full" else "20"
    return [str(scripts[0].relative_to(repo)), "--input", str(datasets[0].relative_to(repo)), "--outdir", outdir, "--n_boot", n_boot]


def is_sasrec_recommendation_repo(repo: Path) -> bool:
    return all(
        (repo / rel).exists()
        for rel in [
            "run.py",
            "configs/basemodel.yaml",
            "configs/sasrec.yaml",
            "data/dataset.py",
            "model/basemodel.py",
            "model/sasrec.py",
            "utils/arguments.py",
            "utils/utils.py",
        ]
    )


def write_sasrec_bounded_runner(artifact_dir: Path, dataset: str, epoch: int) -> Path:
    runner = artifact_dir / "sasrec_bounded_reference_runner.py"
    runner.write_text(
        f'''
from __future__ import annotations

import json
import os
import sys
import traceback

sys.path.insert(0, os.getcwd())
os.environ.setdefault("WANDB_MODE", "disabled")
os.environ.setdefault("WANDB_DISABLED", "true")
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")

try:
    import torch
    from utils.arguments import get_default_parser
    from utils.utils import load_config, prepare_datasets, prepare_model

    parser = get_default_parser()
    args = vars(parser.parse_args(["-m", "SASRec", "-d", {dataset!r}, "--trainfile", "", "--device", "0", "--mode", "recommendation"]))
    config = load_config(args)
    config["train"]["device"] = "cpu"
    config["train"]["epochs"] = max(1, int({int(max(1, epoch))!r}))
    config["train"]["batch_size"] = 2
    config["eval"]["batch_size"] = 2
    config["model"]["dropout_rate"] = 0.0
    config["num_layer"] = 1
    config["neg_num"] = 8

    train_dataset, val_dataset, test_dataset = prepare_datasets(config)
    original_train_len = len(train_dataset)
    bounded_examples = min(8, original_train_len)
    train_dataset.set_data_index(torch.arange(bounded_examples))
    model = prepare_model(config, [train_dataset, val_dataset, test_dataset])
    model._init_model(train_dataset)
    batch = next(iter(train_dataset.get_loader(batch_size=2, shuffle=False)))
    batch = {{key: value.to(model.device) for key, value in batch.items()}}
    batch["neg_item"] = model._neg_sampling(batch)
    model.optimizer.zero_grad()
    loss = model.training_step(batch, 0)
    loss.backward()
    model.optimizer.step()

    payload = {{
        "status": "ok",
        "mode": "sasrec_bounded_single_batch_reference",
        "dataset": {dataset!r},
        "original_train_len": original_train_len,
        "bounded_examples": bounded_examples,
        "loss": float(loss.detach().cpu()),
        "batch_keys": sorted(batch.keys()),
        "paper_level": False,
        "note": "One official SASRec-style batch with the repository loader/model/loss; bounded audit only, not paper-level reproduction.",
    }}
    print(f"Epoch 0 Train loss: {{payload['loss']:.6f}}")
    print(json.dumps(payload, ensure_ascii=False))
except Exception as exc:
    print(json.dumps({{"status": "failed", "error": str(exc), "traceback": traceback.format_exc()[-4000:]}}, ensure_ascii=False))
    raise
'''.lstrip(),
        encoding="utf-8",
    )
    return runner



def write_sasrec_full_runner(artifact_dir: Path, dataset: str, epoch: int) -> Path:
    runner = artifact_dir / "sasrec_full_reference_runner.py"
    runner.write_text(
        f'''
from __future__ import annotations

import json
import os
import sys
import traceback

sys.path.insert(0, os.getcwd())
os.environ.setdefault("WANDB_MODE", "disabled")
os.environ.setdefault("WANDB_DISABLED", "true")

try:
    from utils.arguments import get_default_parser
    from utils.utils import load_config, setup_environment
    import quickstart

    parser = get_default_parser()
    args = vars(parser.parse_args(["-m", "SASRec", "-d", {dataset!r}, "--trainfile", "", "--device", "0", "--mode", "recommendation"]))
    config = load_config(args)
    requested_epochs = max(1, int({int(max(1, epoch))!r}))
    original_epochs = int(config.get("train", {{}}).get("epochs", 0) or 0)
    config.setdefault("train", {{}})["epochs"] = requested_epochs
    setup_environment(config["train"])
    print("[sasrec-reference-runner] " + json.dumps({{
        "status": "starting",
        "mode": "sasrec_full_dataset_reference",
        "dataset": {dataset!r},
        "model": "SASRec",
        "official_entrypoint": "quickstart.run_recommender",
        "original_yaml_epochs": original_epochs,
        "executed_epochs": requested_epochs,
        "epoch_override_scope": "artifact-local runner; third-party repo files are not modified",
        "paper_level_scope": "full dataset/reference command with audited epoch cap",
    }}, ensure_ascii=False))
    quickstart.run_recommender(config)
    print("[sasrec-reference-runner] " + json.dumps({{
        "status": "completed",
        "mode": "sasrec_full_dataset_reference",
        "dataset": {dataset!r},
        "executed_epochs": requested_epochs,
    }}, ensure_ascii=False))
except Exception as exc:
    print("[sasrec-reference-runner] " + json.dumps({{
        "status": "failed",
        "error": str(exc),
        "traceback": traceback.format_exc()[-4000:],
    }}, ensure_ascii=False))
    raise
'''.lstrip(),
        encoding="utf-8",
    )
    return runner

def repo_adapter(repo: Path) -> str:
    if (repo / "main.py").exists() and (repo / "finetune.py").exists() and (repo / "run.sh").exists():
        return "selected_base_official_two_stage"
    if is_sasrec_recommendation_repo(repo):
        return "sasrec_official"
    if any((repo / name).exists() for name in ["train.py", "single_train.py"]):
        return "official_training_entrypoint"
    if analysis_quickstart_command(repo, "bounded"):
        return "analysis_data_quickstart"
    return "unknown"


def ready_dataset(impl: dict[str, Any], protocol: dict[str, Any]) -> str:
    candidates = []
    if isinstance(protocol, dict):
        candidates.append(protocol.get("selected_dataset"))
    if isinstance(impl, dict) and isinstance(impl.get("ready_datasets"), list):
        candidates.extend(impl.get("ready_datasets") or [])
    for value in candidates:
        candidate = str(value or "").strip()
        if candidate:
            return candidate
    return ""


def official_command(repo: Path, dataset: str, mode: str, epoch: int) -> list[str]:
    analysis_command = analysis_quickstart_command(repo, mode)
    if analysis_command:
        return analysis_command
    if is_sasrec_recommendation_repo(repo):
        return ["run.py", "-m", "SASRec", "-d", dataset, "--trainfile", "", "--mode", "recommendation", "--device", "0"]
    run_sh = repo / "run.sh"
    preferred: list[str] = []
    if run_sh.exists():
        for raw in run_sh.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "python" not in line:
                continue
            if dataset and f"--data {dataset}" not in line and f"--data={dataset}" not in line:
                continue
            if "main.py" not in line and "finetune.py" not in line:
                continue
            preferred.append(line)
    chosen = ""
    if mode == "bounded":
        chosen = next((line for line in preferred if "main.py" in line), preferred[0] if preferred else "")
    else:
        chosen = next((line for line in preferred if "finetune.py" in line), preferred[-1] if preferred else "")
    script = "finetune.py" if mode == "full" and (repo / "finetune.py").exists() else "main.py"
    if chosen:
        tokens = chosen.replace("nohup ", "").split()
        try:
            py_index = next(i for i, token in enumerate(tokens) if token.endswith("python") or token.endswith("python3") or token == "python")
            args = tokens[py_index + 1:]
        except StopIteration:
            args = [script]
        cleaned: list[str] = []
        skip_next = False
        stop_tokens = {">>", ">", "2>&1", "&"}
        for token in args:
            if skip_next:
                skip_next = False
                continue
            if token in stop_tokens:
                break
            if token == "--cuda":
                cleaned.extend([token, "0"])
                skip_next = True
                continue
            if token == "--epoch":
                cleaned.extend([token, str(epoch)])
                skip_next = True
                continue
            cleaned.append(token)
        if "--epoch" not in cleaned:
            cleaned.extend(["--epoch", str(epoch)])
        if "--eval" not in cleaned:
            cleaned.extend(["--eval", "1"])
        if "--batch_size" not in cleaned and mode == "bounded":
            cleaned.extend(["--batch_size", "64"])
        return cleaned
    return [script, "--data", dataset, "--epoch", str(epoch), "--eval", "1", "--cuda", "0", "--batch_size", "64"]


def parse_epoch_progress(line: str, total_epoch: int, mode: str) -> dict[str, Any]:
    match = re.search(r"\bEpoch\s+(\d+)\b", line) or re.search(r"\bTraining\s+(\d+)\s*:", line)
    if not match or total_epoch <= 0:
        return {}
    epoch_index = int(match.group(1))
    current = min(epoch_index + 1, total_epoch)
    progress: dict[str, Any] = {
        "phase": "reference-reproduction",
        "mode": mode,
        "current": current,
        "total": total_epoch,
        "percent": int(round((current / total_epoch) * 100)),
        "current_epoch": epoch_index,
        "message": f"{mode} reference reproduction epoch {current}/{total_epoch}",
    }
    loss_match = re.search(r"Train loss:\s*([0-9.]+)", line)
    if loss_match:
        progress["last_train_loss"] = float(loss_match.group(1))
    return progress


def final_reference_progress(last_progress: dict[str, Any], total_epoch: int, mode: str, rc: int, timed_out: bool) -> dict[str, Any]:
    progress = dict(last_progress) if isinstance(last_progress, dict) else {}
    if total_epoch > 0 and rc == 0 and not timed_out:
        progress.update({
            "phase": "reference-reproduction",
            "mode": mode,
            "current": total_epoch,
            "total": total_epoch,
            "percent": 100,
            "current_epoch": total_epoch - 1,
            "message": f"{mode} reference reproduction completed",
        })
    elif progress:
        progress.update({
            "message": f"{mode} reference reproduction stopped before completion",
            "timed_out": bool(timed_out),
            "return_code": rc,
        })
    return progress


def parse_metrics(stdout: str) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    loaded_match = re.search(r"Loaded\s+(\d+)\s+traces", stdout)
    if loaded_match:
        metrics["trace_count"] = int(loaded_match.group(1))
    trajectory_match = re.search(r"Trajectory rows:\s*(\d+)", stdout)
    if trajectory_match:
        metrics["trajectory_rows"] = int(trajectory_match.group(1))
    saved_match = re.search(r"All figures saved to\s+(.+)", stdout)
    if saved_match:
        metrics["analysis_outputs_saved"] = 1
    header: list[str] = []
    for line in stdout.splitlines():
        if all(token in line for token in ["HR@10", "NDCG@10", "HR@20", "NDCG@20"]):
            header = [part.strip().lower().replace("@", "_at_") for part in line.split()]
            continue
        if header:
            vals = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", line)]
            if len(vals) >= len(header):
                for key, value in zip(header, vals):
                    metrics[key] = value
                    best_key = f"best_seen_{key}"
                    metrics[best_key] = max(float(metrics.get(best_key, value)), value)
                header = []
    for line in stdout.splitlines():
        epoch_match = re.search(r"['\"]epoch['\"]\s*:\s*(\d+)", line)
        if epoch_match:
            metrics["last_epoch"] = int(epoch_match.group(1))
        for raw_key, raw_value in re.findall(r"['\"]([^'\"]+)['\"]\s*:\s*tensor\((-?\d+(?:\.\d+)?)", line):
            key = raw_key.strip().lower().replace("@", "_at_").replace(" ", "_")
            value = float(raw_value)
            metrics[key] = value
            ranking_metric = key.startswith(("ndcg_at_", "recall_at_", "hr_at_")) or any(marker in key for marker in ("_ndcg_at_", "_recall_at_", "_hr_at_"))
            if ranking_metric:
                best_key = f"best_seen_{key}"
                metrics[best_key] = max(float(metrics.get(best_key, value)), value)
            if key.startswith("train_loss"):
                metrics["last_train_loss"] = value
    losses = [float(x) for x in re.findall(r"Train loss:\s*([0-9.]+)", stdout)]
    if losses:
        metrics["last_train_loss"] = losses[-1]
    return metrics


def append_registry(paths, payload: dict[str, Any]) -> None:
    registry_path = paths.state / "experiment_registry.json"
    rows = load_json(registry_path, [])
    if not isinstance(rows, list):
        rows = rows.get("experiments", []) if isinstance(rows, dict) else []
    selected = payload.get("selected_base", {}) if isinstance(payload.get("selected_base"), dict) else {}
    row = {
        "timestamp": payload.get("finished_at", ""),
        "started_at": payload.get("started_at", ""),
        "finished_at": payload.get("finished_at", ""),
        "experiment_id": payload.get("experiment_id", ""),
        "name": payload.get("experiment_id", ""),
        "repo": selected.get("name") or selected.get("repo") or payload.get("repo_name") or "selected_base",
        "repo_path": payload.get("repo_path", ""),
        "dataset": payload.get("dataset", ""),
        "benchmark": "selected_base_official_reference",
        "method": payload.get("method", "selected_base_reference"),
        "method_slug": payload.get("method", "selected_base_reference"),
        "method_role": "reference",
        "comparison_role": "reference",
        "status": "completed" if payload.get("return_code") == 0 else "failed",
        "metric_name": "ndcg_at_10" if payload.get("metrics", {}).get("ndcg_at_10") is not None else "",
        "metric_value": payload.get("metrics", {}).get("ndcg_at_10"),
        "metrics": payload.get("metrics", {}),
        "result": "bounded_reference_audit_not_paper_level" if payload.get("mode") == "bounded" else "reference_reproduction_run",
        "notes": payload.get("notes", ""),
        "artifact_path": payload.get("artifact_dir", ""),
        "audit_path": payload.get("state_audit_path") or str(audit_state_path(paths, str(payload.get("mode") or "bounded"))),
        "env_name": payload.get("env_name", ""),
        "command": " ".join(str(x) for x in payload.get("command", [])),
        "return_code": payload.get("return_code"),
        "duration_sec": payload.get("duration_sec"),
        "audit_ready": bool(payload.get("audit_ready")),
        "claim_verdict": "weak" if payload.get("mode") == "bounded" else "pending",
        "novelty_note": "Selected-base reference reproduction wrapper; bounded mode is not paper-level evidence.",
        "config_summary": json.dumps(payload.get("config", {}), ensure_ascii=False)[:1000],
    }
    identity = row["experiment_id"]
    rows = [old for old in rows if not (isinstance(old, dict) and old.get("experiment_id") == identity)]
    rows.append(row)
    save_json(registry_path, rows)


def write_report(paths, payload: dict[str, Any]) -> None:
    lines = ["# Selected Base Reference Reproduction Audit\n\n"]
    for key in ["status", "decision", "experiment_id", "mode", "adapter", "dataset", "duration_sec", "return_code"]:
        lines.append(f"- {key}: {payload.get(key, '')}\n")
    lines.append(f"- audit_ready: {payload.get('audit_ready')}\n")
    lines.append(f"- command: `{' '.join(str(x) for x in payload.get('command', []))}`\n")
    lines.append(f"- artifact_dir: {payload.get('artifact_dir', '')}\n")
    lines.append(f"- metrics: {payload.get('metrics', {})}\n")
    lines.append("\n## Policy\n")
    lines.append("- Bounded mode captures command/config/log/hash/runtime evidence but does not satisfy paper-level reference reproduction.\n")
    lines.append("- Full mode is paper-level reference reproduction only after the downstream reference gate audits the exact logs and metrics.\n")
    mode = str(payload.get("mode") or "unknown")
    report_path = paths.reports / f"fresh_base_reference_{mode}_reproduction_audit.md"
    report_path.write_text("".join(lines), encoding="utf-8")
    if mode == "full":
        (paths.reports / "fresh_base_reference_reproduction_audit.md").write_text("".join(lines), encoding="utf-8")


def project_target_venue(project: str, explicit: str = "") -> str:
    venue = str(explicit or "").strip()
    if venue:
        return venue
    cfg = load_json(build_paths(project).config, {})
    if isinstance(cfg, dict):
        for row in [cfg, cfg.get("writing") if isinstance(cfg.get("writing"), dict) else {}, cfg.get("paper") if isinstance(cfg.get("paper"), dict) else {}]:
            if not isinstance(row, dict):
                continue
            for key in ["target_venue", "venue", "venue_slug"]:
                value = str(row.get(key) or "").strip()
                if value:
                    return value.upper() if value.lower() in {"iclr", "icml", "neurips", "nips", "kdd", "sigkdd"} else value
    return ""


def build(project: str, mode: str, epoch: int, timeout_sec: int, execute: bool, venue: str = "") -> dict[str, Any]:
    paths = build_paths(project)
    repo, impl, env = selected_repo(project)
    selected = env.get("selected", {}) if isinstance(env, dict) and isinstance(env.get("selected"), dict) else {}
    protocol = load_json(paths.state / "fresh_base_reference_protocol_probe.json", {})
    smoke = load_json(paths.state / "fresh_base_reference_smoke.json", {})
    adapter = repo_adapter(repo)
    failures: list[str] = []
    if not repo.exists():
        failures.append("current selected-base repository is missing")
    if adapter == "unknown":
        failures.append("current selected-base repository has no recognized runnable adapter")
    if not isinstance(protocol, dict) or protocol.get("status") != "reference_protocol_probe_passed":
        failures.append("selected-base reference protocol probe has not passed")
    if not isinstance(smoke, dict) or smoke.get("status") != "reference_smoke_passed":
        failures.append("selected-base bounded smoke has not passed")
    dataset = ready_dataset(impl, protocol)
    if not dataset:
        failures.append("no ready selected-base dataset recorded")
    safe_epoch = max(1, min(epoch, 1 if mode == "bounded" else 30))
    py = project_python(project)
    args = official_command(repo, dataset, mode, safe_epoch)
    command = [py, "-u", *args]
    stamp = str(os.environ.get("SELECTED_BASE_REFERENCE_STAMP") or "").strip() or dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
    exp_id = f"selected_base_reference_{mode}_{dataset or 'dataset'}_{safe_epoch}epoch_{stamp}"
    artifact_dir = paths.artifacts / "fresh_base_reference_reproduction" / exp_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    if adapter == "sasrec_official" and mode == "bounded":
        args = [str(write_sasrec_bounded_runner(artifact_dir, dataset, safe_epoch))]
        command = [py, "-u", *args]
    elif adapter == "sasrec_official" and mode == "full":
        args = [str(write_sasrec_full_runner(artifact_dir, dataset, safe_epoch))]
        command = [py, "-u", *args]
    stdout_path = artifact_dir / "stdout_stderr.log"
    artifact_audit_path = artifact_dir / "audit.json"
    state_audit_path = audit_state_path(paths, mode)
    torch_load_compat = write_torch_load_compat(artifact_dir, repo, dataset, mode, adapter)
    env_vars = dict(os.environ)
    env_vars.setdefault("PYTHONUNBUFFERED", "1")
    env_vars.setdefault("CUDA_VISIBLE_DEVICES", "0")
    if torch_load_compat.get("enabled"):
        compat_path = str(Path(str(torch_load_compat.get("sitecustomize_path", ""))).parent)
        old_pythonpath = env_vars.get("PYTHONPATH", "")
        env_vars["PYTHONPATH"] = compat_path + (os.pathsep + old_pythonpath if old_pythonpath else "")
        env_vars["SELECTED_BASE_TRUSTED_TORCHLOAD_CHECKPOINT"] = str(selected_pretrain_checkpoint(repo, dataset).resolve())
    started = now_iso()
    state_job_path = paths.state / "fresh_base_reference_full_reproduction_job.json"
    if mode == "full":
        update_json(
            state_job_path,
            {
                "project": project,
                "updated_at": now_iso(),
                "status": "running" if execute and not failures else "blocked",
                "decision": "full_reference_reproduction_running" if execute and not failures else "reference_reproduction_audit_failed",
                "repo_path": str(repo),
                "artifact_dir": str(artifact_dir),
                "stdout_path": str(stdout_path),
                "experiment_id": exp_id,
                "dataset": dataset,
                "command": command,
                "wrapper_pid": os.getpid(),
                "method": "selected_base_reference",
                "epoch": safe_epoch,
                "guardrail": "Wrapper-managed selected-base reference reproduction; no paper writing, claim promotion, second Find, pair_compare, or legacy main-route fallback.",
            },
        )
    rc = 2
    timed_out = False
    stdout = ""
    duration = 0.0
    last_progress: dict[str, Any] = {}
    if not failures and execute:
        t0 = time.monotonic()
        stdout_parts: list[str] = []
        with stdout_path.open("w", encoding="utf-8", errors="replace") as out_handle:
            out_handle.write("[wrapper] command=" + " ".join(str(x) for x in command) + "\n")
            out_handle.write(f"[wrapper] cwd={repo}\n")
            out_handle.write(f"[wrapper] started_at={started}\n")
            out_handle.flush()
            try:
                proc = subprocess.Popen(
                    command,
                    cwd=repo,
                    env=env_vars,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=1,
                )
                if mode == "full":
                    update_json(state_job_path, {"updated_at": now_iso(), "child_pid": proc.pid, "stdout_path": str(stdout_path), "artifact_dir": str(artifact_dir)})
                deadline = time.monotonic() + timeout_sec
                assert proc.stdout is not None
                while True:
                    line = proc.stdout.readline()
                    if line:
                        stdout_parts.append(line)
                        out_handle.write(line)
                        out_handle.flush()
                        progress = parse_epoch_progress(line, safe_epoch, mode)
                        if mode == "full" and progress:
                            last_progress = progress
                            update_json(
                                state_job_path,
                                {
                                    "updated_at": now_iso(),
                                    "progress": progress,
                                    "current_epoch": progress.get("current_epoch"),
                                    "last_output_line": line.strip()[-500:],
                                },
                            )
                    if proc.poll() is not None:
                        rest = proc.stdout.read() or ""
                        if rest:
                            stdout_parts.append(rest)
                            out_handle.write(rest)
                            out_handle.flush()
                        break
                    if time.monotonic() > deadline:
                        timed_out = True
                        rc = 124
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        timeout_line = f"\n[wrapper] TIMEOUT after {timeout_sec}s\n"
                        stdout_parts.append(timeout_line)
                        out_handle.write(timeout_line)
                        out_handle.flush()
                        break
                if not timed_out:
                    rc = int(proc.returncode if proc.returncode is not None else 0)
            except Exception as exc:
                rc = 2
                err_line = f"\n[wrapper] ERROR: {type(exc).__name__}: {exc}\n"
                stdout_parts.append(err_line)
                out_handle.write(err_line)
                out_handle.flush()
        stdout = "".join(stdout_parts)
        duration = time.monotonic() - t0
    else:
        stdout = "SKIPPED: " + ("; ".join(failures) if failures else "execute flag not set")
        stdout_path.write_text(stdout, encoding="utf-8", errors="replace")
    metrics = parse_metrics(stdout)
    final_progress = final_reference_progress(last_progress, safe_epoch, mode, rc, timed_out)
    entrypoint = repo / (args[0] if args else "")
    hashes = {
        "entrypoint_sha256": sha256_file(entrypoint),
        "stdout_sha256": sha256_file(stdout_path),
    }
    if torch_load_compat.get("checkpoint_sha256"):
        hashes["selected_base_checkpoint_sha256"] = torch_load_compat.get("checkpoint_sha256", "")
    data_root = repo / "data" / dataset if dataset else Path("")
    for name in ["data_statis.df", "train_data.df", "val_data.df", "test_data.df"]:
        hashes[name.replace(".", "_") + "_sha256"] = sha256_file(data_root / name) if dataset else ""
    status = "completed_bounded_audit" if rc == 0 and mode == "bounded" else "completed_reference_reproduction" if rc == 0 else "blocked_reference_reproduction_audit"
    decision = "reference_reproduction_audit_required" if mode == "bounded" and rc == 0 else "ready_for_reference_gate_audit" if mode == "full" and rc == 0 else "reference_reproduction_audit_failed"
    payload = {
        "project": project,
        "generated_at": now_iso(),
        "started_at": started,
        "finished_at": now_iso(),
        "status": status,
        "decision": decision,
        "experiment_id": exp_id,
        "mode": mode,
        "adapter": adapter,
        "method": "selected_base_reference",
        "dataset": dataset,
        "repo_path": str(repo),
        "repo_name": selected.get("name") or selected.get("repo") or "",
        "selected_base": selected,
        "python_executable": py,
        "env_name": selected.get("env_name") or selected.get("conda_env") or "",
        "command": command,
        "return_code": rc,
        "timed_out": timed_out,
        "duration_sec": duration,
        "artifact_dir": str(artifact_dir),
        "stdout_path": str(stdout_path),
        "state_audit_path": str(state_audit_path),
        "artifact_audit_path": str(artifact_audit_path),
        "metrics": metrics,
        "config": {"mode": mode, "epoch": safe_epoch, "timeout_sec": timeout_sec, "execute": execute, "paper_level": bool(mode == "full" and rc == 0 and not timed_out), "adapter": adapter, "torch_load_compat": torch_load_compat},
        "hashes": hashes,
        "failures": failures,
        "audit_ready": bool(rc == 0 and not timed_out and execute),
        "paper_level_reproduction_passed": bool(mode == "full" and rc == 0 and not timed_out),
        "notes": "Bounded selected-base reference reproduction audit. This is not paper-level evidence unless mode=full and downstream gates pass." if mode == "bounded" else "Full selected-base reference reproduction run; downstream gates must audit metrics before experiments proceed.",
        "venue": project_target_venue(project, venue),
        "guardrail": "Wrapper-managed selected-base command only; no paper writing, claim promotion, second Find, pair_compare, or legacy main-route fallback.",
    }
    write_mode_audit(paths, payload, artifact_audit_path)
    if mode == "full":
        update_json(
            paths.state / "fresh_base_reference_full_reproduction_job.json",
            {
                "updated_at": now_iso(),
                "finished_at": payload.get("finished_at", ""),
                "status": "completed" if rc == 0 and not timed_out else "blocked",
                "decision": decision,
                "return_code": rc,
                "timed_out": timed_out,
                "duration_sec": duration,
                "artifact_dir": str(artifact_dir),
                "stdout_path": str(stdout_path),
                "audit_path": payload.get("state_audit_path") or str(audit_state_path(paths, str(payload.get("mode") or "bounded"))),
                "paper_level_reproduction_passed": bool(payload.get("paper_level_reproduction_passed")),
                "metrics": metrics,
                "progress": final_progress,
            },
        )
        post_refresh = post_reference_reproduction_refresh(paths, project, project_target_venue(project, venue), trigger="environment_selected_base_reference_wrapper")
        payload["post_reference_refresh"] = post_refresh
        write_mode_audit(paths, payload, artifact_audit_path)
        update_json(paths.state / "fresh_base_reference_full_reproduction_job.json", {"updated_at": now_iso(), "post_reference_refresh": post_refresh})
    write_report(paths, payload)
    append_registry(paths, payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Audited selected-base reference reproduction wrapper.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--mode", default="bounded", choices=["bounded", "full"])
    parser.add_argument("--epoch", type=int, default=1)
    parser.add_argument("--timeout-sec", type=int, default=900)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--venue", default="")
    args = parser.parse_args()
    project_rc = dispatch_project_local_adapter(args.project, sys.argv[1:])
    if project_rc is not None:
        return project_rc
    payload = build(args.project, args.mode, args.epoch, max(30, args.timeout_sec), args.execute, args.venue)
    print(json.dumps({"status": payload.get("status"), "decision": payload.get("decision"), "experiment_id": payload.get("experiment_id"), "return_code": payload.get("return_code"), "metrics": payload.get("metrics"), "failures": payload.get("failures")}, ensure_ascii=False))
    return 0 if payload.get("return_code") == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
