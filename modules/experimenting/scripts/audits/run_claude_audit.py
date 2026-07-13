#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

CURRENT = Path(__file__).resolve()
SCRIPTS = CURRENT.parents[1]
MODULE_ROOT = CURRENT.parents[2]
ROOT = CURRENT.parents[4]

COMMON = SCRIPTS / "common"
if str(COMMON) not in sys.path:
    sys.path.insert(0, str(COMMON))

SKILL_PATH = MODULE_ROOT / "skills" / "experiment-audit-adjudication" / "SKILL.md"
PROMPT_PATH = MODULE_ROOT / "prompts" / "audit-adjudication.md"
KIND_PROMPTS = {
    "experiment_iteration": MODULE_ROOT / "prompts" / "experiment-iteration-audit.md",
    "runtime_integrity": MODULE_ROOT / "prompts" / "runtime-integrity-audit.md",
    "reference_reproduction": MODULE_ROOT / "prompts" / "reference-reproduction-audit.md",
    "claim_progress": MODULE_ROOT / "prompts" / "claim-progress-audit.md",
    "experiment_recording": MODULE_ROOT / "prompts" / "experiment-recording.md",
}
DEFAULT_OUTPUT_ROOT = MODULE_ROOT / ".runtime" / "standalone_audit"

AUDIT_KINDS = ("full_cycle", "experiment_iteration", "runtime_integrity", "reference_reproduction", "claim_progress", "experiment_recording")


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_text(path: Path, limit: int = 200000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit] if path.exists() else ""
    except Exception:
        return ""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch a fresh Claude Code audit adjudication over Experimenting evidence.")
    parser.add_argument("--project", required=True, help="TASTE project name under projects/.")
    parser.add_argument("--venue", default="")
    parser.add_argument("--audit-kind", default="full_cycle", choices=AUDIT_KINDS)
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--permission-mode", default="bypassPermissions", choices=["acceptEdits", "auto", "bypassPermissions", "default", "dontAsk", "plan"])
    parser.add_argument("--model", default="")
    parser.add_argument("--claude-timeout-sec", type=int, default=3600)
    parser.add_argument("--extra-context", action="append", default=[])
    parser.add_argument("--skip-deterministic-refresh", action="store_true")
    parser.add_argument("--skip-claude", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def project_dir(project: str) -> Path | None:
    name = str(project or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        return None
    projects = (ROOT / "projects").resolve()
    path = (projects / name).resolve()
    try:
        path.relative_to(projects)
    except ValueError:
        return None
    return path if path.is_dir() and path.name == name else None


def _status_of(payload: Any) -> str:
    return str(payload.get("status") or payload.get("decision") or "missing") if isinstance(payload, dict) else "missing"


def refresh_deterministic_audits(project: str, audit_kind: str, *, skip: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "requested": bool(project and not skip),
        "skipped": bool(skip or not project),
        "project": project,
        "audit_kind": audit_kind,
        "refreshed_at": now_iso(),
        "results": {},
        "errors": [],
        "policy": "No scripted audit adjudication is run here. This wrapper may refresh machine facts such as watchdog output; Claude Code performs the audit verdict from prompts.",
    }
    if skip or not project:
        return payload

    if audit_kind in {"full_cycle", "runtime_integrity", "claim_progress"}:
        watchdog = SCRIPTS / "execution" / "experiment_run_watchdog.py"
        env = os.environ.copy()
        env["EXPERIMENTING_PUBLIC_ENTRYPOINT_ACTIVE"] = "1"
        try:
            proc = subprocess.run(
                [sys.executable, str(watchdog), "--project", project],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=90,
            )
            result = _json_from_text(proc.stdout)
            payload["results"]["watchdog"] = {
                "return_code": proc.returncode,
                "status": _status_of(result),
                "stdout_tail": proc.stdout[-1200:],
                "stderr_tail": proc.stderr[-1200:],
            }
        except Exception as exc:
            payload["errors"].append({"stage": "watchdog", "error": f"{type(exc).__name__}: {exc}"})
    return payload


def _project_rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return path.name


def _copy_pack_file(src: Path, target: Path, *, limit_bytes: int) -> dict[str, Any]:
    item = {"source": str(src), "pack_path": str(target), "size": 0, "copied": False, "truncated": False}
    try:
        size = src.stat().st_size
        item["size"] = size
        target.parent.mkdir(parents=True, exist_ok=True)
        if size <= limit_bytes:
            shutil.copy2(src, target)
            item["copied"] = True
            return item
        text = src.read_text(encoding="utf-8", errors="replace")[:limit_bytes]
        truncated = target.with_name(target.name + ".truncated.txt")
        truncated.write_text(text, encoding="utf-8")
        item["pack_path"] = str(truncated)
        item["copied"] = True
        item["truncated"] = True
        return item
    except Exception as exc:
        item["error"] = f"{type(exc).__name__}: {exc}"
        return item


def _recent_text_files(root: Path, limit: int = 12) -> list[Path]:
    if not root.exists():
        return []
    suffixes = {".json", ".md", ".txt", ".log", ".csv"}
    files = [path for path in root.glob("**/*") if path.is_file() and path.suffix.lower() in suffixes]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:limit]


def collect_audit_pack(project: str, audit_kind: str, output_root: Path, deterministic_refresh: dict[str, Any]) -> dict[str, Any]:
    pack_dir = output_root / "input" / "audit_pack"
    files_dir = pack_dir / "files"
    pdir = project_dir(project)
    manifest: dict[str, Any] = {
        "project": project,
        "project_dir": str(pdir or ""),
        "audit_kind": audit_kind,
        "generated_at": now_iso(),
        "deterministic_refresh": deterministic_refresh,
        "included": [],
        "missing": [],
    }
    if not pdir:
        manifest["missing"].append(f"projects/{project}" if project else "project argument")
        save_json(pack_dir / "pack_manifest.json", manifest)
        return manifest

    rels = [
        "state/current_find_research_plan.json",
        "state/taste_plan_bridge.json",
        "state/experiment_plan.json",
        "state/experiment_registry.json",
        "state/experiment_record_table.json",
        "state/experiment_run_manifest.json",
        "state/experiment_run_watchdog.json",
        "state/experiment_iteration_audit.json",
        "state/experiment_runtime_integrity.json",
        "state/reference_reproduction_gate.json",
        "state/scientific_progress_gate.json",
        "state/paper_evidence_gate.json",
        "state/submission_readiness_gate.json",
        "state/blocker_action_plan.json",
        "state/next_actions.json",
        "reports/experiment_iteration_audit.md",
        "reports/experiment_runtime_integrity.md",
        "reports/reference_reproduction_gate.md",
        "reports/iteration_reflection.md",
        "planning/next_actions.md",
        "planning/finding_frontend.md",
        "experiments/experiment_records.csv",
        "experiments/实验记录.md",
        "records/experiment_records.csv",
        "records/实验记录.md",
    ]
    candidates = [pdir / rel for rel in rels]
    candidates.extend(_recent_text_files(pdir / "logs"))
    candidates.extend(_recent_text_files(pdir / "artifacts"))

    seen: set[Path] = set()
    for src in candidates:
        src = src.resolve(strict=False)
        if src in seen:
            continue
        seen.add(src)
        if not src.exists() or not src.is_file():
            manifest["missing"].append(_project_rel(src, pdir))
            continue
        rel = _project_rel(src, pdir)
        target = files_dir / rel
        manifest["included"].append(_copy_pack_file(src, target, limit_bytes=300000))

    save_json(pack_dir / "pack_manifest.json", manifest)
    return manifest


def read_extra_context(paths: list[str], limit: int = 24000) -> str:
    sections: list[str] = []
    for raw in paths:
        path = Path(raw).expanduser().resolve()
        if path.exists() and path.is_file():
            sections.append(f"## {path}\n{read_text(path, limit)}")
        else:
            sections.append(f"## missing extra context\n{path}")
    return "\n\n".join(sections)


def build_claude_prompt(args: argparse.Namespace, output_root: Path, pack_manifest: dict[str, Any]) -> str:
    skill = read_text(SKILL_PATH)
    prompt_parts = [read_text(PROMPT_PATH)]
    if args.audit_kind == "full_cycle":
        prompt_parts.extend(read_text(path) for path in KIND_PROMPTS.values())
    else:
        prompt_parts.append(read_text(KIND_PROMPTS.get(args.audit_kind, Path())))
    prompt_template = "\n\n".join(part for part in prompt_parts if part.strip())
    extra = read_extra_context(args.extra_context)
    adjudication_path = output_root / "audit_adjudication.json"
    task_label = "实验记录维护" if args.audit_kind == "experiment_recording" else "审计裁决"
    required_json = output_root / ("recording_result.json" if args.audit_kind == "experiment_recording" else "audit_adjudication.json")
    return f"""
你是 Experimenting {task_label} Claude Code 会话。你必须只做本任务要求的证据读取、记录维护和结构化输出。

执行契约：
- 审计类型必须是: {args.audit_kind}
- 项目必须是: {args.project or "standalone"}
- 输出目录必须是: {output_root}
- 结构化结果必须写入: {required_json}
- 写入范围必须只有输出目录。
- 读取入口必须先看: {output_root / "input" / "audit_pack" / "pack_manifest.json"}
- 每个 finding 必须引用本地 evidence_paths。
- deterministic gate 为 blocked/running/missing 时，最终 `status` 必须是 blocked/running，除非 finding 证明该 gate 已过期并给出刷新动作。
- 论文/claim promotion 只有在 reference reproduction、runtime integrity、experiment iteration、metrics/logs、bad-case/counterexample evidence 都通过时才可为 true。
- experiment_recording 任务必须维护 registry/CSV/Markdown/experiment_record，并写 `recording_result.json`。

审计包清单：
```json
{json.dumps(pack_manifest, ensure_ascii=False, indent=2)[:30000]}
```

本地 skill：
{skill}

本地 prompt：
{prompt_template}

额外上下文：
{extra or "无"}
""".strip() + "\n"


def _json_from_text(text: str) -> dict[str, Any]:
    if not text.strip():
        return {}
    candidates = [text]
    try:
        top = json.loads(text)
    except Exception:
        top = None
    if isinstance(top, dict):
        if top.get("status") and top.get("findings") is not None:
            return top
        for key in ["result", "summary", "message", "text"]:
            value = top.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value)
    for candidate in candidates:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start >= 0 and end > start:
            try:
                payload = json.loads(candidate[start:end + 1])
            except Exception:
                continue
            if isinstance(payload, dict):
                return payload
    return {}


def _command_display(cmd: list[str]) -> str:
    return " ".join(shlex.quote(str(part)) for part in cmd)


def run_claude(args: argparse.Namespace, prompt: str, output_root: Path) -> tuple[int, dict[str, Any]]:
    output_root.mkdir(parents=True, exist_ok=True)
    prompt_path = output_root / "claude_audit_prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    output_path = output_root / ("recording_result.json" if args.audit_kind == "experiment_recording" else "audit_adjudication.json")
    result_path = output_root / "claude_audit_result.json"
    log_path = output_root / "claude_audit_stdout.log"

    if args.dry_run or args.skip_claude:
        status = "dry_run" if args.dry_run else "skipped"
        adjudication = {
            "status": "blocked",
            "audit_kind": args.audit_kind,
            "summary": f"Claude {args.audit_kind} task was {status}; no evidence-grounded output was produced.",
            "decision": "maintain_records" if args.audit_kind == "experiment_recording" else "block_paper",
            "claim_promotion_allowed": False,
            "findings": [
                {
                    "severity": "block",
                    "claim": f"audit adjudication {status}",
                    "evidence_paths": [str(prompt_path)],
                    "required_next_action": "Run the same Experimenting action without dry-run/skip-claude to produce evidence-grounded output.",
                }
            ],
            "gate_alignment": {
                "experiment_iteration": "missing",
                "runtime_integrity": "missing",
                "reference_reproduction": "missing",
            },
            "next_action": "Run the same Experimenting action with Claude enabled.",
        }
        save_json(output_path, adjudication)
        result = {
            "status": status,
            "audit_kind": args.audit_kind,
            "project": args.project,
            "prompt_path": str(prompt_path),
            "output_path": str(output_path),
            "output_loaded": True,
            "output_status": adjudication["status"],
            "generated_at": now_iso(),
        }
        save_json(result_path, result)
        log_path.write_text(f"{status}=true\n", encoding="utf-8")
        return 0, result

    claude = shutil.which("claude")
    if not claude:
        result = {
            "status": "blocked_claude_missing",
            "audit_kind": args.audit_kind,
            "project": args.project,
            "prompt_path": str(prompt_path),
            "output_path": str(output_path),
            "generated_at": now_iso(),
            "message": "Claude Code CLI is not available in the current conda taste runtime.",
        }
        save_json(result_path, result)
        log_path.write_text("claude_missing=true\n", encoding="utf-8")
        return 2, result

    cmd = [
        claude,
        "-p",
        "--permission-mode",
        args.permission_mode,
        "--add-dir",
        str(output_root),
        "--output-format",
        "json",
    ]
    pdir = project_dir(args.project)
    if pdir:
        cmd.extend(["--add-dir", str(pdir)])
    if args.model:
        cmd.extend(["--model", args.model])
    cmd.append(prompt)

    started = now_iso()
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["EXPERIMENTING_AUDIT_OUTPUT_DIR"] = str(output_root)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(pdir or output_root),
            env=env,
            text=True,
            capture_output=True,
            timeout=max(60, args.claude_timeout_sec),
        )
        timed_out = False
        stdout = proc.stdout
        stderr = proc.stderr
        return_code = int(proc.returncode)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return_code = 124

    log_path.write_text(
        "\n".join([
            f"# started_at: {started}",
            f"# finished_at: {now_iso()}",
            f"# command: {_command_display(cmd[:-1] + ['<prompt>'])}",
            f"# return_code: {return_code}",
            f"# timed_out: {timed_out}",
            "",
            "## stdout",
            stdout,
            "",
            "## stderr",
            stderr,
            "",
        ]),
        encoding="utf-8",
    )
    adjudication = load_json(output_path, {})
    if not isinstance(adjudication, dict) or not adjudication:
        adjudication = _json_from_text(stdout)
        if adjudication:
            save_json(output_path, adjudication)

    result = {
        "status": "completed" if return_code == 0 else "failed",
        "audit_kind": args.audit_kind,
        "project": args.project,
        "return_code": return_code,
        "timed_out": timed_out,
        "prompt_path": str(prompt_path),
        "log_path": str(log_path),
        "output_path": str(output_path),
        "output_loaded": bool(adjudication),
        "output_status": str(adjudication.get("status") or "") if isinstance(adjudication, dict) else "",
        "generated_at": now_iso(),
    }
    if adjudication:
        result["adjudication"] = adjudication
    elif return_code == 0:
        result["status"] = "blocked_invalid_claude_audit_output"
    save_json(result_path, result)

    if return_code != 0:
        return 2, result
    if not adjudication:
        return 2, result
    status = str(adjudication.get("status") or "").lower()
    return (0 if status in {"pass", "warn"} else 2), result


def main() -> int:
    args = parse_args()
    output_root = Path(args.output_root).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    save_json(output_root / "audit_invocation.json", vars(args))
    deterministic = refresh_deterministic_audits(
        args.project,
        args.audit_kind,
        skip=args.skip_deterministic_refresh or args.dry_run,
    )
    save_json(output_root / "deterministic_audit_refresh.json", deterministic)
    pack_manifest = collect_audit_pack(args.project, args.audit_kind, output_root, deterministic)
    prompt = build_claude_prompt(args, output_root, pack_manifest)
    rc, result = run_claude(args, prompt, output_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return rc


if __name__ == "__main__":
    import sys as _sys
    from pathlib import Path as _Path

    _common = _Path(__file__).resolve().parents[1] / "common"
    if str(_common) not in _sys.path:
        _sys.path.insert(0, str(_common))
    from entrypoint_guard import ensure_main_entrypoint

    ensure_main_entrypoint()
    raise SystemExit(main())
