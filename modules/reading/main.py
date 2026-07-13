from __future__ import annotations

import argparse
import ast
import datetime as dt
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

STAGE_NAME = "reading"
DISPLAY_NAME = "Reading"
RESPONSIBILITY = "Acquire verified same-paper full text from generic local paper inputs and synthesize reading notes; a title is sufficient, optional locators accelerate resolution, and article replacement is forbidden."
REQUIRED_EXTERNAL_INPUTS = ("local_input_json", "claude_or_prepare_mode")
ARTIFACTS_IN = ("local input JSON under this directory",)
PUBLIC_FINAL_ARTIFACT = "read.md"
ARTIFACTS_OUT = (PUBLIC_FINAL_ARTIFACT, "read_results.json", "full_text_reading/full_text_packet.json")
MACHINE_SUPPORT_ARTIFACTS = ("read_results.json", "full_text_reading/full_text_packet.json")
PRIVATE_BACKEND_ROOTS = (
    "scripts/pipeline/read_pipeline.py",
    "scripts/core/common.py",
    "scripts/acquisition/paper_sources.py",
    "scripts/acquisition/conference_sources.py",
    "scripts/acquisition/openreview_official.py",
    "scripts/acquisition/semantic_scholar.py",
    "scripts/orchestration/claude_subagent.py",
)


def contract() -> dict[str, Any]:
    return {
        "stage": STAGE_NAME,
        "display_name": DISPLAY_NAME,
        "responsibility": RESPONSIBILITY,
        "required_external_inputs": list(REQUIRED_EXTERNAL_INPUTS),
        "artifacts_in": list(ARTIFACTS_IN),
        "artifacts_out": list(ARTIFACTS_OUT),
        "public_final_artifact": PUBLIC_FINAL_ARTIFACT,
        "machine_support_artifacts": list(MACHINE_SUPPORT_ARTIFACTS),
        "private_backend_roots": list(PRIVATE_BACKEND_ROOTS),
    }


READING_ROOT = Path(__file__).resolve().parent
SCRIPTS = READING_ROOT / "scripts"
DEFAULT_READ_WORKERS = 8
MAX_READ_WORKER_CAP = 16


def _read_worker_cap(default: int = MAX_READ_WORKER_CAP) -> int:
    raw = str(os.environ.get("READING_READ_WORKER_CAP") or "").strip()
    if raw:
        try:
            return max(1, min(MAX_READ_WORKER_CAP, int(raw)))
        except ValueError:
            pass
    return max(1, min(MAX_READ_WORKER_CAP, int(default or DEFAULT_READ_WORKERS)))


def _python_env() -> dict[str, str]:
    env = os.environ.copy()
    reading_entries: list[str] = [
        str(READING_ROOT),
        str(SCRIPTS),
    ]
    if SCRIPTS.is_dir():
        reading_entries.extend(
            str(path)
            for path in sorted(SCRIPTS.rglob("*"))
            if path.is_dir() and not path.name.startswith("__")
        )
    existing = [part for part in env.get("PYTHONPATH", "").split(os.pathsep) if part]
    seen: set[str] = set()
    merged: list[str] = []
    for item in [*reading_entries, *existing]:
        if item and item not in seen:
            seen.add(item)
            merged.append(item)
    env["PYTHONPATH"] = os.pathsep.join(merged)
    return env


def _ensure_runtime_imports() -> None:
    for entry in reversed(_python_env()["PYTHONPATH"].split(os.pathsep)):
        if not entry:
            continue
        while entry in sys.path:
            sys.path.remove(entry)
        sys.path.insert(0, entry)


def _contract_payload() -> dict:
    payload = contract()
    payload["entrypoint"] = "main.py"
    payload["scripts_are_private_backend"] = True
    return payload


def _normalize_action(action: str) -> str:
    return str(action or "").strip().replace("-", "_")


def _module_path(path: Path) -> str:
    return ".".join(path.relative_to(SCRIPTS).with_suffix("").parts)


def _imports(tree: ast.AST) -> list[str]:
    values: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            values.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * int(node.level or 0) + (node.module or "")
            for alias in node.names:
                values.add(prefix + (("." + alias.name) if prefix and alias.name != "*" else alias.name))
    return sorted(values)


def _script_record(path: Path) -> dict[str, Any]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    functions = [node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    classes = [node.name for node in tree.body if isinstance(node, ast.ClassDef)]
    parts = path.relative_to(SCRIPTS).parts
    category = parts[0] if len(parts) > 1 else "core"
    return {
        "path": path.relative_to(READING_ROOT).as_posix(),
        "module": _module_path(path),
        "category": category,
        "role": "implementation",
        "line_count": len(source.splitlines()),
        "function_count": len(functions),
        "functions": functions,
        "class_count": len(classes),
        "classes": classes,
        "imports": _imports(tree),
    }


def _build_manifest() -> dict[str, Any]:
    scripts = [
        _script_record(path)
        for path in sorted(SCRIPTS.rglob("*.py"))
        if "__pycache__" not in path.parts
    ]
    top_level_files = sorted(path.name for path in SCRIPTS.glob("*.py"))
    duplicate_modules: dict[str, list[str]] = {}
    for record in scripts:
        duplicate_modules.setdefault(str(record["module"]).rsplit(".", 1)[-1], []).append(str(record["path"]))
    conflicting_duplicate_modules = {
        name: paths for name, paths in duplicate_modules.items() if len(paths) > 1 and name != "__init__"
    }
    count_limit = 7
    status = "pass" if not conflicting_duplicate_modules and len(scripts) <= count_limit else "fail"
    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "module": "reading",
        "purpose": "论文全文获取、精读准备和 Reading 正式执行脚本清单。",
        "boundary": "scripts 只保留 Reading 内部全文获取、精读和 Claude prompt 运行代码；测试/批测脚本不得放入模块 scripts 目录。配置在 config/，运行输入和产物在 .runtime/output/<UTC精确时间run-id>/；latest_run 只是人工审查副本，程序不得从中同步。",
        "public_entrypoint": "main.py",
        "script_count": len(scripts),
        "script_count_limit": count_limit,
        "scripts": scripts,
        "audit": {
            "top_level_python_files": top_level_files,
            "conflicting_duplicate_basenames": conflicting_duplicate_modules,
            "script_count_within_limit": len(scripts) <= count_limit,
            "status": status,
        },
    }


def _load_private_script_module(relative_path: str, module_name: str) -> Any:
    _ensure_runtime_imports()
    module_path = (SCRIPTS / relative_path).resolve(strict=False)
    try:
        module_path.relative_to(SCRIPTS.resolve(strict=False))
    except ValueError as exc:
        raise ModuleNotFoundError(f"Reading private module path escapes scripts root: {relative_path}") from exc
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ModuleNotFoundError(f"Cannot load Reading private module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _private_core_module(stem: str) -> Any:
    _ensure_runtime_imports()
    module_name = f"core.{stem}"
    expected_path = (SCRIPTS / "core" / f"{stem}.py").resolve(strict=False)
    loaded = sys.modules.get(module_name)
    if loaded is not None:
        loaded_path = Path(str(getattr(loaded, "__file__", ""))).resolve(strict=False)
        if loaded_path == expected_path:
            return loaded
    try:
        spec = importlib.util.find_spec(module_name)
        if spec is not None and spec.loader is not None and spec.origin:
            module_path = Path(spec.origin).resolve(strict=False)
            module_path.relative_to(SCRIPTS.resolve(strict=False))
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            return module
    except Exception:
        pass
    return _load_private_script_module(f"core/{stem}.py", f"reading_private_core_{stem}")


def _common_module() -> Any:
    return _private_core_module("common")


def _read_pipeline_module() -> Any:
    _ensure_runtime_imports()
    try:
        from pipeline import read_pipeline

        module_path = Path(str(getattr(read_pipeline, "__file__", ""))).resolve(strict=False)
        module_path.relative_to(SCRIPTS.resolve(strict=False))
        return read_pipeline
    except Exception:
        return _load_private_script_module("pipeline/read_pipeline.py", "reading_private_read_pipeline")


def _private_pipeline_module(stem: str) -> Any:
    _ensure_runtime_imports()
    try:
        module_name = f"pipeline.{stem}"
        spec = importlib.util.find_spec(module_name)
        if spec is not None and spec.loader is not None and spec.origin:
            module_path = Path(spec.origin).resolve(strict=False)
            module_path.relative_to(SCRIPTS.resolve(strict=False))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    except Exception:
        pass
    return _load_private_script_module(f"pipeline/{stem}.py", f"reading_private_{stem}")


DIRECT_ACTIONS = {"", "read", "pipeline", "read_pipeline"}
STANDALONE_DEEP_READ_ACTIONS = {"deep_read", "deep_read_paper", "standalone", "standalone_deep_read"}
MANIFEST_ACTIONS = {"manifest", "script_manifest", "audit_scripts", "generate_script_manifest"}

def _run_standalone_deep_read(args: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="Run Reading single-paper deep-read through the public main.py entrypoint.")
    parser.add_argument("--article", default="", help="可选论文 URL、arXiv 链接/编号、PDF URL 或 DOI；也可只提供标题。")
    parser.add_argument("--input-json", default="", help="可选本地输入 JSON。")
    parser.add_argument("--run-id", default="", help="可选运行 ID；产物写入 .runtime/output/<run-id>。")
    parser.add_argument("--paper-id", default="")
    parser.add_argument("--title", default="")
    parser.add_argument("--authors", default="")
    parser.add_argument("--abstract", default="")
    parser.add_argument("--url", default="")
    parser.add_argument("--pdf-url", default="")
    parser.add_argument("--source", default="")
    parser.add_argument("--claude-mode", choices=["auto", "run", "prepare"], default="auto")
    parser.add_argument("--timeout-sec", type=int, default=1800)
    parser.add_argument("--force", action="store_true", help="Regenerate reading Markdown while still allowing same-paper full-text cache reuse.")
    parsed = parser.parse_args(list(args))
    read_pipeline = _read_pipeline_module()
    result = read_pipeline.run_standalone_deep_read(parsed)
    payload = {
        "status": result.get("status"),
        "run_id": result.get("run_id"),
        "run_dir": result.get("run_dir"),
        "latest_run": result.get("latest_run"),
        "read_md": result.get("public_final_artifact"),
        "read_results": str(Path(str(result.get("run_dir") or "")) / "read_results.json") if result.get("run_dir") else "",
    }
    try:
        payload = read_pipeline.make_reading_paths_relative(payload)
    except Exception:
        pass
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in {"complete", "prepared_for_claude_subagent", "prepared_for_main_claude_subagent"} else 2


def _run_manifest(args: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="Generate or audit Reading script_manifest.json.")
    parser.add_argument("--check", action="store_true", help="Do not write; fail if the existing manifest differs.")
    parsed = parser.parse_args(list(args))
    manifest_path = READING_ROOT / "script_manifest.json"
    payload = _build_manifest()
    if parsed.check:
        existing = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
        if existing.get("scripts") != payload.get("scripts") or existing.get("audit") != payload.get("audit"):
            print(json.dumps({"status": "fail", "reason": "script_manifest_out_of_date"}, ensure_ascii=False, indent=2))
            return 2
        print(json.dumps({"status": payload["audit"]["status"], "script_count": payload["script_count"]}, ensure_ascii=False, indent=2))
        return 0 if payload["audit"]["status"] == "pass" else 2
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": payload["audit"]["status"], "script_count": payload["script_count"], "manifest": str(manifest_path)}, ensure_ascii=False, indent=2))
    return 0 if payload["audit"]["status"] == "pass" else 2


def _run_read(args: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="Read exactly the local input articles through the public main.py entrypoint.")
    parser.add_argument("--input-json", required=True, help="Input JSON inside this directory, with articles/input_articles/papers.")
    parser.add_argument("--run-id", default="", help="Output run id under .runtime/output.")
    parser.add_argument("--max-papers", type=int, default=0, help="Optional local truncation for smoke tests only; 0 means all input articles.")
    parser.add_argument("--claude-mode", choices=["prepare", "run", "auto"], default="prepare")
    parser.add_argument("--timeout-sec", type=int, default=1800)
    parser.add_argument("--read-workers", type=int, default=0, help="Parallel paper workers; 0 uses READING_READ_WORKERS or the Reading default.")
    parser.add_argument("--force", action="store_true", help="Regenerate reading subagent Markdown instead of reusing complete per-paper results.")
    parsed = parser.parse_args(list(args))
    env_workers = str(os.environ.get("READING_READ_WORKERS") or "").strip()
    try:
        read_workers = int(parsed.read_workers or env_workers or 0)
    except ValueError:
        read_workers = 0
    if read_workers <= 0:
        read_workers = DEFAULT_READ_WORKERS if parsed.claude_mode != "prepare" else 1
    read_workers = max(1, min(_read_worker_cap(), read_workers))
    result = _read_pipeline_module().run_read(
        run_id=parsed.run_id,
        input_json=parsed.input_json,
        claude_mode=parsed.claude_mode,
        timeout_sec=parsed.timeout_sec,
        max_papers=parsed.max_papers,
        max_workers=read_workers,
        force_deep_read=bool(parsed.force),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in {"complete", "prepared_all_full_text_pending_claude"} else 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reading module public backend entrypoint.", add_help=True)
    parser.usage = "%(prog)s [action] [--action ACTION] [module args ...]"
    parser.add_argument("--action", default="", help="Backend action. Default: read.")
    parser.add_argument("--contract", action="store_true")
    ns, rest = parser.parse_known_args(argv)
    if ns.contract:
        print(json.dumps(_contract_payload(), ensure_ascii=False, indent=2))
        return 0
    action_arg = ""
    if not ns.action and rest and not rest[0].startswith("-"):
        action_arg = rest.pop(0)
    action = _normalize_action(ns.action or action_arg or "read")
    if action in DIRECT_ACTIONS:
        return _run_read(rest)
    if action in STANDALONE_DEEP_READ_ACTIONS:
        return _run_standalone_deep_read(rest)
    if action in MANIFEST_ACTIONS:
        return _run_manifest(rest)
    raise SystemExit(f"Unknown {STAGE_NAME} module action: {action}")


if __name__ == "__main__":
    raise SystemExit(main())
