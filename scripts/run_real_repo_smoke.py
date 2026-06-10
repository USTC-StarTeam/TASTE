#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from pipeline_guard import guard_fresh_base_blocker_entry

try:
    import yaml
except Exception:  # pragma: no cover - dependency is installed in repo env by bootstrap_repo_env.py
    yaml = None


def save_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_yaml(path: Path) -> dict:
    if yaml is None:
        raise RuntimeError("pyyaml is required inside the repo conda environment")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return data if isinstance(data, dict) else {}


def dump_yaml(path: Path, data: dict) -> None:
    if yaml is None:
        raise RuntimeError("pyyaml is required inside the repo conda environment")
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, sort_keys=False, allow_unicode=True)


def bounded_int(value, fallback: int, upper: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = fallback
    return max(1, min(parsed, upper))


def choose_free_gpu() -> int:
    try:
        proc = subprocess.run([
            "nvidia-smi",
            "--query-gpu=index,memory.free",
            "--format=csv,noheader,nounits",
        ], text=True, capture_output=True, timeout=10)
    except Exception:
        return 0
    if proc.returncode != 0:
        return 0
    best_index = 0
    best_free = -1
    for line in proc.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            index = int(parts[0])
            free_mem = int(float(parts[1]))
        except Exception:
            continue
        if free_mem > best_free:
            best_index = index
            best_free = free_mem
    return best_index


def strategy_for_method(method_slug: str) -> dict:
    method = (method_slug or "repo_real_reproduction_smoke").lower()
    if "hparam" in method or "sanity" in method:
        return {
            "strategy": "low_resource_hparam_sanity",
            "batch_size": 64,
            "test_batch_size": 128,
            "emb_dim": 16,
            "ui_n_layers": 1,
            "social_n_layers": 1,
            "condition_n_layers": 1,
            "N": 2,
            "M": 2,
            "lr": 0.0002,
        }
    if "cycle3" in method:
        return {
            "strategy": "cycle3_llm_probe",
            "batch_size": 1024,
            "test_batch_size": 512,
            "emb_dim": 128,
            "ui_n_layers": 2,
            "social_n_layers": 2,
            "condition_n_layers": 2,
            "N": 5,
            "M": 5,
        }
    if "metric" in method or "audit" in method:
        return {
            "strategy": "metric_audit_probe",
            "batch_size": 128,
            "test_batch_size": 64,
            "emb_dim": 32,
            "ui_n_layers": 1,
            "social_n_layers": 1,
            "condition_n_layers": 1,
            "N": 3,
            "M": 3,
            "test_mode": 1,
        }
    return {
        "strategy": "low_resource_reproduction",
        "batch_size": 128,
        "test_batch_size": 128,
        "emb_dim": 32,
        "ui_n_layers": 1,
        "social_n_layers": 1,
        "condition_n_layers": 1,
        "N": 3,
        "M": 3,
    }


def prepare_short_config(repo: Path, dataset: str, epochs: int, seed: int, strategy: dict) -> tuple[Path, bytes, dict, dict]:
    config_path = repo / "configures" / f"{dataset}.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"missing repo config: {config_path}")
    original_bytes = config_path.read_bytes()
    original = load_yaml(config_path)
    patched = dict(original)
    patched["dataset"] = dataset
    patched["seed"] = seed
    patched["epoches"] = bounded_int(patched.get("epoches", epochs), epochs, epochs)
    for key in ["batch_size", "test_batch_size", "emb_dim", "ui_n_layers", "social_n_layers", "condition_n_layers", "N", "M"]:
        if key in strategy:
            patched[key] = bounded_int(patched.get(key, strategy[key]), int(strategy[key]), int(strategy[key]))
    for key in ["lr", "test_mode"]:
        if key in strategy:
            patched[key] = strategy[key]
    patched.setdefault("topks", "[10,5]")
    dump_yaml(config_path, patched)
    return config_path, original_bytes, original, patched


def parse_number_list(text: str) -> list[float]:
    return [float(x) for x in re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", text or "")]


def parse_metrics(output: str) -> dict:
    metrics: dict[str, float] = {}
    # Prefer the last printed evaluation block, because the repo may evaluate multiple times.
    ndcg_matches = re.findall(r"ndcg:\s*\t?\s*\[([^\]]+)\]", output, flags=re.IGNORECASE)
    recall_matches = re.findall(r"recall:\s*\t?\s*\[([^\]]+)\]", output, flags=re.IGNORECASE)
    precision_matches = re.findall(r"precision:\s*\t?\s*\[([^\]]+)\]", output, flags=re.IGNORECASE)
    if ndcg_matches:
        vals = parse_number_list(ndcg_matches[-1])
        if vals:
            metrics["ndcg_at_10"] = vals[0]
            if len(vals) > 1:
                metrics["ndcg_at_5"] = vals[1]
    if recall_matches:
        vals = parse_number_list(recall_matches[-1])
        if vals:
            metrics["recall_at_10"] = vals[0]
            if len(vals) > 1:
                metrics["recall_at_5"] = vals[1]
    if precision_matches:
        vals = parse_number_list(precision_matches[-1])
        if vals:
            metrics["precision_at_10"] = vals[0]
            if len(vals) > 1:
                metrics["precision_at_5"] = vals[1]
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a short, honest real-dataset smoke reproduction for a selected repo and emit TASTE audit artifacts.")
    parser.add_argument("--repo-path", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--epochs", type=int, default=int(os.environ.get("REAL_SMOKE_EPOCHS", "1")))
    parser.add_argument("--timeout-sec", type=int, default=int(os.environ.get("REAL_SMOKE_TIMEOUT_SEC", "600")))
    parser.add_argument("--entry-command", default="")
    parser.add_argument("--core", default="auto")
    parser.add_argument("--method-slug", default=os.environ.get("METHOD_SLUG", "repo_real_reproduction_smoke"))
    args = parser.parse_args()

    project = os.environ.get("PROJECT_ID", "")
    if not project:
        raise SystemExit("PROJECT_ID is required for run_real_repo_smoke")
    venue = os.environ.get("VENUE", "")
    guard_rc = guard_fresh_base_blocker_entry(project, venue, Path(__file__).name, safe_unblock=False)
    if guard_rc is not None:
        return int(guard_rc)

    repo = Path(args.repo_path).resolve()
    artifact = Path(args.artifact_dir).resolve()
    artifact.mkdir(parents=True, exist_ok=True)
    # Some research repos assume local output directories exist; create only generic sinks.
    (repo / "log").mkdir(parents=True, exist_ok=True)
    started = time.time()
    core = choose_free_gpu() if str(args.core).lower() == "auto" else int(args.core)
    command = args.entry_command.strip() or f"{shlex.quote(sys.executable)} main.py --dataset {shlex.quote(args.dataset)} --core {core}"
    method_slug = args.method_slug or os.environ.get("METHOD_SLUG", "repo_real_reproduction_smoke")
    strategy = strategy_for_method(method_slug)
    config_path = None
    original_bytes = b""
    patched_config = {}
    proc = subprocess.CompletedProcess(command, 1, "", "not-run")
    config_error = ""
    try:
        config_path, original_bytes, _original_config, patched_config = prepare_short_config(repo, args.dataset, args.epochs, args.seed, strategy)
        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        proc = subprocess.run(["bash", "-lc", command], cwd=repo, env=env, text=True, capture_output=True, timeout=args.timeout_sec)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "ignore")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", "ignore")
        proc = subprocess.CompletedProcess(command, 124, stdout, stderr + f"\nTIMEOUT after {args.timeout_sec}s")
    except Exception as exc:
        config_error = str(exc)
        proc = subprocess.CompletedProcess(command, 1, "", config_error)
    finally:
        if config_path is not None and original_bytes:
            config_path.write_bytes(original_bytes)

    duration = time.time() - started
    combined = (proc.stdout or "") + "\n--- STDERR ---\n" + (proc.stderr or "")
    (artifact / "stdout_stderr_real_repo.log").write_text(combined, encoding="utf-8")
    metrics = parse_metrics(combined)
    if metrics:
        metrics["runtime_sec"] = round(duration, 3)
    if proc.returncode == 0:
        bad_items = []
        if metrics.get("ndcg_at_10", 0.0) <= 0.001:
            bad_items.append({
                "slice": "aggregate_low_ndcg_at_10",
                "evidence": f"ndcg_at_10={metrics.get('ndcg_at_10')} on the real repo/data short reproduction.",
                "action": "Treat this as a weak baseline signal; require instrumentation before claiming improvement.",
            })
        if metrics.get("recall_at_10", 0.0) <= 0.001:
            bad_items.append({
                "slice": "aggregate_low_recall_at_10",
                "evidence": f"recall_at_10={metrics.get('recall_at_10')} on the real repo/data short reproduction.",
                "action": "Inspect ranking/evaluation setup and run a controlled baseline comparison.",
            })
        bad_items.append({
            "slice": "per_user_bad_cases_unavailable",
            "evidence": "The selected repo command completed but did not export per-user ranking failures; this run is aggregate reproduction evidence only.",
            "action": "Require a later instrumentation run before making slice-level improvement claims.",
        })
    else:
        bad_items = [{
            "slice": "runtime_failure",
            "evidence": (proc.stderr or proc.stdout or config_error)[-1200:],
            "action": "Debug repo execution before claiming real-dataset evidence.",
        }]
    bad_payload = {
        "path": str(artifact / "bad_cases.json"),
        "count": len(bad_items),
        "items": bad_items,
        "slices": sorted({str(row.get("slice", "unknown")) for row in bad_items}),
    }
    audit = {
        "metrics": metrics,
        "claim_verdict": "partial" if metrics and proc.returncode == 0 else "unsupported",
        "novelty_note": "Real-dataset repo smoke reproduction only; no novelty or improvement claim is supported by this artifact.",
        "counterexample_outcome": "Short reproduction produced metrics but no per-user bad cases; strong claims remain blocked." if metrics else "Not tested in this short reproduction; absence of per-user bad cases blocks strong claims.",
        "bad_cases": bad_payload,
        "bad_case_slices": bad_payload["slices"],
        "dataset": args.dataset,
        "method_slug": method_slug,
        "strategy": strategy.get("strategy", ""),
        "command": command,
        "return_code": proc.returncode,
        "selected_core": core,
        "strategy_config": strategy,
        "patched_config": patched_config,
    }
    save_json(artifact / "metrics.json", metrics)
    save_json(artifact / "bad_cases.json", bad_items)
    save_json(artifact / "audit.json", audit)
    print(json.dumps({"return_code": proc.returncode, "method_slug": method_slug, "strategy": strategy.get("strategy", ""), "metrics": metrics, "artifact_dir": str(artifact)}, ensure_ascii=False))
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
