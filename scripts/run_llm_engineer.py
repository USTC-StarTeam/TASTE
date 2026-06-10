#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from llm_agent_core import extract_patch_text, llm_json, llm_text, read_text
from llm_client import llm_available, llm_disabled_reason
from project_paths import build_paths, load_project_config

ROOT = Path(__file__).resolve().parents[1]
CONTEXT_CANDIDATES = [
    "README.md",
    "pyproject.toml",
    "setup.py",
    "requirements.txt",
    "environment.yml",
    "environment.yaml",
    "configs/default.yaml",
    "configs/config.yaml",
]


def load_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def gather_repo_context(repo_path: Path, focus_files: list[str], limit: int = 6) -> list[dict[str, str]]:
    contexts: list[dict[str, str]] = []
    seen = set()

    def add_file(target: Path, rel_hint: str = "") -> None:
        if not target.exists() or not target.is_file():
            return
        key = str(target.resolve())
        if key in seen:
            return
        seen.add(key)
        rel = rel_hint or str(target.relative_to(repo_path))
        contexts.append({"path": rel, "content": read_text(target, 16000)})

    for rel in focus_files:
        if not rel:
            continue
        target = repo_path / rel
        add_file(target, rel)
        if len(contexts) >= limit:
            return contexts

    for rel in CONTEXT_CANDIDATES:
        add_file(repo_path / rel, rel)
        if len(contexts) >= limit:
            return contexts

    for pattern in ("*.py", "*.sh", "*.yaml", "*.yml"):
        for candidate in sorted(repo_path.rglob(pattern)):
            if ".git/" in str(candidate):
                continue
            add_file(candidate)
            if len(contexts) >= limit:
                return contexts
    return contexts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--method", required=True)
    parser.add_argument("--repo-path", required=True)
    parser.add_argument("--request", default="")
    parser.add_argument("--failure-summary", default="")
    parser.add_argument("--mode", choices=["implement", "repair"], default="repair")
    parser.add_argument("--trial-json", default="")
    parser.add_argument("--max-files", type=int, default=6)
    args = parser.parse_args()

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    repo = Path(args.repo_path).resolve()
    out_dir = paths.state / "llm_engineer"
    out_dir.mkdir(parents=True, exist_ok=True)
    result_path = out_dir / f"{args.method}_result.json"
    result = {
        "project": args.project,
        "method": args.method,
        "repo_path": str(repo),
        "mode": args.mode,
        "trial_json": args.trial_json,
        "status": "error",
        "patch_nonempty": False,
    }

    try:
        if not repo.exists():
            raise RuntimeError(f"missing repo path: {repo}")
        if not llm_available(cfg):
            raise RuntimeError(llm_disabled_reason(cfg))

        next_actions = load_json(paths.state / "next_actions.json", {"method_summaries": []})
        method_info = next((row for row in next_actions.get("method_summaries", []) if row.get("method") == args.method), {})
        failure_info = load_json(paths.state / f"failure_analysis_{args.method}.json", {})
        experiment_log = read_text(paths.experiments / "experiment_log.md", 12000)
        trial_context = {}
        if args.trial_json:
            trial_path = Path(args.trial_json)
            if trial_path.exists():
                try:
                    trial_context = json.loads(trial_path.read_text(encoding="utf-8"))
                except Exception:
                    trial_context = {"raw": read_text(trial_path, 12000)}

        planning_prompt = {
            "project": args.project,
            "method": args.method,
            "mode": args.mode,
            "request": args.request,
            "failure_summary": args.failure_summary,
            "method_info": method_info,
            "failure_info": failure_info,
            "trial_context": trial_context,
        }
        plan_prompt = (
            "You are an LLM-only engineering planner for autonomous AI research. "
            "Return strict JSON with keys summary, focus_files, edit_goals, risks, validation_steps. "
            "Focus on the smallest code changes that can improve the specified method or repair implementation issues.\n\n"
            + json.dumps(planning_prompt, ensure_ascii=False, indent=2)
        )
        plan_json, plan_raw = llm_json(plan_prompt, cfg, system_prompt="Return strict JSON only.")
        focus_files = plan_json.get("focus_files", []) if isinstance(plan_json.get("focus_files", []), list) else []
        repo_context = gather_repo_context(repo, [str(x) for x in focus_files], limit=max(1, args.max_files))

        patch_prompt = {
            "project": args.project,
            "method": args.method,
            "engineering_plan": plan_json,
            "mode": args.mode,
            "failure_summary": args.failure_summary,
            "experiment_log_excerpt": experiment_log,
            "trial_context": trial_context,
            "repo_context": repo_context,
        }
        patch_request = (
            "You are an LLM-only engineer. Produce a unified diff patch only. "
            "Output no prose, no markdown commentary, and no apply_patch blocks. "
            "Use repository-relative paths that patch(1) can apply from the repo root.\n\n"
            + json.dumps(patch_prompt, ensure_ascii=False, indent=2)
        )
        patch_raw = llm_text(patch_request, cfg, system_prompt="Output only a unified diff patch.")
        patch_text = extract_patch_text(patch_raw.get("content", ""))
        if not patch_text.strip():
            raise RuntimeError("empty-patch-from-llm")

        plan_path = out_dir / f"{args.method}_plan.json"
        raw_plan_path = out_dir / f"{args.method}_plan_raw.json"
        patch_path = out_dir / f"{args.method}.patch"
        raw_patch_path = out_dir / f"{args.method}_patch_raw.json"
        plan_path.write_text(json.dumps(plan_json, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        raw_plan_path.write_text(json.dumps(plan_raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        patch_path.write_text(patch_text, encoding="utf-8")
        raw_patch_path.write_text(json.dumps(patch_raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        result.update({
            "status": "success",
            "plan_path": str(plan_path),
            "patch_path": str(patch_path),
            "patch_nonempty": True,
            "focus_files": focus_files,
        })
    except Exception as exc:
        result["error"] = str(exc)

    result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    if result.get("status") != "success":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
