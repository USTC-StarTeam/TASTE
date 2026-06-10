#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path

from project_paths import build_paths, load_project_config

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)


def git_available(repo: Path) -> bool:
    return (repo / ".git").exists() and run(["git", "status", "--short"], repo).returncode == 0


def git_snapshot(repo: Path) -> dict:
    if not git_available(repo):
        return {"available": False, "reason": "not-a-git-repo"}
    head = run(["git", "rev-parse", "--verify", "HEAD"], repo)
    diff = run(["git", "diff", "--binary"], repo)
    status = run(["git", "status", "--short"], repo)
    return {
        "available": True,
        "head": head.stdout.strip() if head.returncode == 0 else "",
        "dirty_before": bool(status.stdout.strip()),
        "status_before": status.stdout,
        "diff_before": diff.stdout,
    }


def restore_snapshot(repo: Path, snapshot: dict) -> dict:
    if not snapshot.get("available"):
        return {"restored": False, "reason": snapshot.get("reason", "snapshot-unavailable")}
    # Preserve untracked evidence/config artifacts. Only revert tracked files touched by LLM patches.
    reset = run(["git", "reset", "--hard", snapshot.get("head") or "HEAD"], repo)
    if snapshot.get("diff_before"):
        patch = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=repo, input=snapshot["diff_before"], text=True, capture_output=True)
    else:
        patch = subprocess.CompletedProcess(["git", "apply"], 0, "", "")
    return {
        "restored": reset.returncode == 0 and patch.returncode == 0,
        "reset_rc": reset.returncode,
        "patch_rc": patch.returncode,
        "preserved_untracked": True,
        "stderr": (reset.stderr + patch.stderr)[-2000:],
    }


def execute_validation(project: str, repo: Path, command: str, env_name: str) -> subprocess.CompletedProcess[str]:
    if env_name:
        quoted_repo = shlex.quote(str(repo))
        exec_cmd = [str(SCRIPTS / "run_in_conda.sh"), project, "--env-name", env_name, "bash", "-lc", f"cd {quoted_repo} && {command}"]
        return run(exec_cmd, ROOT)
    return run(["bash", "-lc", command], repo)


def write_feedback(paths, method: str, out: dict) -> Path:
    feedback = paths.planning / f"coding_feedback_{method}.md"
    lines = [
        f"# Coding Feedback: {method}\n\n",
        f"- success: {out.get('success')}\n",
        f"- final_stage: {out.get('final_stage')}\n",
        f"- final_return_code: {out.get('final_return_code')}\n",
        f"- rounds: {len(out.get('rounds', []))}\n",
        "\n## Round Trace\n",
    ]
    for row in out.get("rounds", []):
        lines.append(f"- round {row.get('round')} | stage={row.get('stage')} | rc={row.get('return_code')} | patch={row.get('patch_path', '')}\n")
    if out.get("latest_failure"):
        lines.extend(["\n## Latest Failure Excerpt\n\n", "```text\n", str(out.get("latest_failure", ""))[-5000:], "\n```\n"])
    lines.extend([
        "\n## Planner Guidance\n",
        "- Treat this file as feedback into the next planning/reflection step.\n",
        "- If failures are validation or audit-contract related, repair evidence export before tuning.\n",
        "- If two consecutive LLM patches fail at apply/validation, diversify the method rather than repeatedly patching the same idea.\n",
    ])
    feedback.write_text("".join(lines), encoding="utf-8")
    return feedback


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--method", required=True)
    parser.add_argument("--repo-path", required=True)
    parser.add_argument("--command", required=True)
    parser.add_argument("--env-name", default="")
    parser.add_argument("--request", default="")
    parser.add_argument("--mode", choices=["implement", "repair"], default="repair")
    parser.add_argument("--trial-json", default="")
    parser.add_argument("--max-rounds", type=int, default=2)
    parser.add_argument("--rollback-on-failure", action="store_true", default=True)
    args = parser.parse_args()

    _cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    repo = Path(args.repo_path).resolve()
    if not repo.exists():
        raise SystemExit(f"missing repo path: {repo}")

    snapshot = git_snapshot(repo)
    rounds: list[dict] = []
    latest_failure = ""
    success = False
    final_stage = "not_started"
    final_return_code = 1
    applied_patches: list[str] = []
    rollback_result: dict = {}

    for round_idx in range(1, args.max_rounds + 1):
        engineer = run([
            sys.executable, str(SCRIPTS / "run_llm_engineer.py"),
            "--project", args.project,
            "--method", args.method,
            "--repo-path", str(repo),
            "--request", args.request,
            "--failure-summary", latest_failure,
            "--mode", args.mode,
            "--trial-json", args.trial_json,
        ], ROOT)
        if engineer.returncode != 0:
            rounds.append({"round": round_idx, "stage": "engineer", "return_code": engineer.returncode, "stdout": engineer.stdout[-3000:], "stderr": engineer.stderr[-3000:]})
            final_stage = "engineer"
            final_return_code = engineer.returncode
            break
        try:
            engineer_payload = json.loads(engineer.stdout.strip().splitlines()[-1])
        except Exception:
            rounds.append({"round": round_idx, "stage": "engineer-parse", "return_code": engineer.returncode, "stdout": engineer.stdout[-3000:], "stderr": engineer.stderr[-3000:]})
            final_stage = "engineer-parse"
            final_return_code = 2
            break
        patch_path = engineer_payload.get("patch_path", "")
        apply = run([sys.executable, str(SCRIPTS / "apply_llm_patch.py"), "--repo-path", str(repo), "--patch-path", patch_path], ROOT)
        if apply.returncode != 0:
            latest_failure = (apply.stdout + "\n" + apply.stderr)[-5000:]
            rounds.append({"round": round_idx, "stage": "apply", "return_code": apply.returncode, "stdout": apply.stdout[-3000:], "stderr": apply.stderr[-3000:], "patch_path": patch_path})
            final_stage = "apply"
            final_return_code = apply.returncode
            continue
        applied_patches.append(patch_path)

        proc = execute_validation(args.project, repo, args.command, args.env_name)
        rounds.append({"round": round_idx, "stage": "execute", "return_code": proc.returncode, "stdout": proc.stdout[-4000:], "stderr": proc.stderr[-4000:], "patch_path": patch_path})
        final_stage = "execute"
        final_return_code = proc.returncode
        if proc.returncode == 0:
            success = True
            break
        latest_failure = (proc.stdout + "\n" + proc.stderr)[-5000:]

    if not success and args.rollback_on_failure and applied_patches:
        rollback_result = restore_snapshot(repo, snapshot)

    out = {
        "project": args.project,
        "method": args.method,
        "mode": args.mode,
        "repo_path": str(repo),
        "trial_json": args.trial_json,
        "max_rounds": args.max_rounds,
        "rounds": rounds,
        "success": success,
        "final_stage": final_stage,
        "final_return_code": final_return_code,
        "latest_failure": latest_failure,
        "git_snapshot": {k: v for k, v in snapshot.items() if k != "diff_before"},
        "applied_patches": applied_patches,
        "rollback_on_failure": bool(args.rollback_on_failure),
        "rollback_result": rollback_result,
    }
    out_path = paths.state / f"llm_repair_loop_{args.method}.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    feedback_path = write_feedback(paths, args.method, out)
    out["planner_feedback_path"] = str(feedback_path)
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(out_path)
    raise SystemExit(0 if success else 1)


if __name__ == "__main__":
    main()
