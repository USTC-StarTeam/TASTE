from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

STAGE_NAME = "finding"
DISPLAY_NAME = "Finding"
RESPONSIBILITY = "Collect candidate papers/tools from explicit sources and rank recommendations for later reading."
REQUIRED_EXTERNAL_INPUTS = ("config_json", "selection_json")
ARTIFACTS_IN = ("config.json", "selection.json")
PUBLIC_FINAL_ARTIFACT = "find.md"
ARTIFACTS_OUT = ("find_results.json", PUBLIC_FINAL_ARTIFACT, "source_status.md", "find_progress.json")
MACHINE_SUPPORT_ARTIFACTS = ("find_results.json", "source_status.md", "find_progress.json")
PRIVATE_BACKEND_ROOTS = (
    "scripts/core",
    "scripts/flow",
    "scripts/sources.py",
    "scripts/cache",
)

MODULE_ROOT = Path(__file__).resolve().parent
MODULE_RUNTIME_DIR = MODULE_ROOT / ".runtime"
MODULE_CONFIG_DIR = MODULE_ROOT / "config"
LOCAL_LLM_CONFIG_PATH = MODULE_CONFIG_DIR / "llm.local.json"
SCRIPTS = MODULE_ROOT / "scripts"
SCRIPT_IMPORT_DIRS = (
    SCRIPTS / "core",
    SCRIPTS / "flow",
    SCRIPTS / "cache",
    SCRIPTS,
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


def _contract_payload() -> dict[str, Any]:
    payload = contract()
    payload["entrypoint"] = f"modules/{STAGE_NAME}/main.py"
    payload["scripts_are_private_backend"] = True
    return payload


def _configure_private_runtime_env(env: dict[str, str] | None = None) -> None:
    target = env if env is not None else os.environ
    if not str(target.get("FINDING_RUNTIME_DIR") or "").strip():
        target["FINDING_RUNTIME_DIR"] = str(MODULE_RUNTIME_DIR)


def _ensure_runtime_imports() -> None:
    _configure_private_runtime_env()
    for path in reversed(SCRIPT_IMPORT_DIRS):
        scripts_path = str(path)
        if scripts_path not in sys.path:
            sys.path.insert(0, scripts_path)


def _private_import(module_name: str):
    _ensure_runtime_imports()
    candidates: list[str] = []
    if __package__:
        candidates.append(f"{__package__}.scripts.{module_name}")
    candidates.append(module_name)
    last_error: ModuleNotFoundError | None = None
    for candidate in candidates:
        try:
            return importlib.import_module(candidate)
        except ModuleNotFoundError as exc:
            if exc.name == candidate or candidate.startswith(f"{exc.name}."):
                last_error = exc
                continue
            raise
    if last_error:
        raise last_error
    raise ModuleNotFoundError(module_name)


def _action_module(action: str) -> str:
    normalized = _normalize_action(action)
    if "." in normalized:
        return normalized
    return ACTION_MODULES.get(normalized, normalized)


def _load_json(path: str, default):
    return json.loads(Path(path).read_text(encoding="utf-8")) if path else default


def _local_llm_config_path() -> Path:
    override = os.environ.get("FINDING_LLM_CONFIG", "").strip()
    return Path(override).expanduser() if override else LOCAL_LLM_CONFIG_PATH


def _load_local_llm_config() -> dict[str, Any]:
    path = _local_llm_config_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _missing_config_value(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _with_local_llm_config(payload: dict[str, Any]) -> dict[str, Any]:
    data = dict(payload)
    allowed_keys = {"provider", "base_url", "model", "api_key", "temperature"}
    for key, value in _load_local_llm_config().items():
        if key in allowed_keys and not _missing_config_value(value) and _missing_config_value(data.get(key)):
            data[key] = value
    return data


def _with_llm_env_defaults(payload: dict[str, Any]) -> dict[str, Any]:
    data = _with_local_llm_config(payload)
    env_defaults = {
        "provider": os.environ.get("LLM_PROVIDER", ""),
        "base_url": os.environ.get("LLM_API_BASE") or os.environ.get("OPENAI_API_BASE", ""),
        "api_key": os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY", ""),
        "model": os.environ.get("LLM_MODEL", ""),
    }
    for key, value in env_defaults.items():
        if value and _missing_config_value(data.get(key)):
            data[key] = value
    if "LLM_TEMPERATURE" in os.environ and _missing_config_value(data.get("temperature")):
        try:
            data["temperature"] = float(os.environ["LLM_TEMPERATURE"])
        except ValueError:
            pass
    return data


def _display_path(path: str | Path) -> str:
    candidate = Path(path).expanduser()
    try:
        return candidate.resolve().relative_to(MODULE_ROOT).as_posix()
    except (OSError, ValueError):
        try:
            return candidate.relative_to(MODULE_ROOT).as_posix()
        except ValueError:
            return candidate.as_posix()


def _normalize_action(action: str) -> str:
    return str(action or "").strip().replace("-", "_")


def _run_action_module(script_stem: str, args: Sequence[str]) -> int:
    module_name = _action_module(script_stem)
    try:
        module = _private_import(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name or module_name.startswith(f"{exc.name}."):
            raise SystemExit(f"Unknown Finding action: {script_stem}") from exc
        raise
    runner = getattr(module, "run_cli", None)
    if not callable(runner):
        raise SystemExit(f"Unknown Finding action: {script_stem}")
    return int(runner(list(args)))


DIRECT_ACTIONS = {"", "find", "pipeline", "find_pipeline"}
ACTION_ALIASES = {
    "category_summary": "build_category_summary",
    "venue_metadata_cache": "build_venue_metadata_cache",
    "priority_venue_metadata_audit": "audit_priority_venue_metadata",
    "audit_priority_venue_metadata": "audit_priority_venue_metadata",
    "openreview_cache": "build_openreview_cache",
    "local_database": "update_local_database",
}
ACTION_MODULES = {
    "audit_priority_venue_metadata": "cache.audit_priority_venue_metadata",
    "build_category_summary": "cache.build_category_summary",
    "build_openreview_cache": "cache.build_openreview_cache",
    "build_venue_metadata_cache": "cache.build_venue_metadata_cache",
    "update_local_database": "cache.update_local_database",
}


def _result_summary(result: Any, run_dir: str) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"status": "unknown", "run_dir": run_dir}
    source_status = result.get("source_status")
    if not isinstance(source_status, list):
        source_status = []
    return {
        "status": result.get("status") or "complete",
        "run_dir": run_dir,
        "recommendation_target_count": result.get("recommendation_target_count"),
        "recommendation_actual_count": result.get("recommendation_actual_count"),
        "recommendation_shortfall": result.get("recommendation_shortfall"),
        "source_count": len(source_status),
        "limited_sources": [
            str(row.get("source") or "")
            for row in source_status
            if isinstance(row, dict) and row.get("limited")
        ],
    }


def _run_refresh_source_health(args: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="Refresh Finding venue source-health files without rerunning scoring.")
    parser.add_argument("--run-dir", required=True, help="Finding run directory to refresh in place.")
    parser.add_argument("--selection-json", default="", help="Optional VenueSelection-compatible source selection JSON.")
    ns = parser.parse_args(list(args))
    source_selection = _private_import("finding_runtime.source_selection")
    find_pipeline = _private_import("flow.pipeline")

    selection = source_selection.normalize_source_selection(_load_json(ns.selection_json, {})) if ns.selection_json else None
    result = find_pipeline.refresh_find_source_health(Path(ns.run_dir), selection=selection)
    print(json.dumps({"result": result}, ensure_ascii=False, indent=2))
    return 0


def _venue_health_pairs(selection: dict[str, Any]) -> list[tuple[str, int]]:
    pairs: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for item in selection.get("venue_years") or []:
        if not isinstance(item, dict):
            continue
        venue_id = str(item.get("venue_id") or item.get("venue") or item.get("id") or "").strip()
        raw_years = item.get("years") if isinstance(item.get("years"), list) else [item.get("year")]
        if not venue_id:
            continue
        for raw_year in raw_years:
            try:
                year = int(raw_year)
            except (TypeError, ValueError):
                continue
            key = (venue_id, year)
            if key in seen:
                continue
            seen.add(key)
            pairs.append(key)
    if pairs:
        return pairs
    venue_ids = selection.get("venue_ids") if isinstance(selection.get("venue_ids"), list) else []
    years = selection.get("years") if isinstance(selection.get("years"), list) else []
    for venue_id_raw in venue_ids:
        venue_id = str(venue_id_raw or "").strip()
        if not venue_id:
            continue
        for raw_year in years:
            try:
                year = int(raw_year)
            except (TypeError, ValueError):
                continue
            key = (venue_id, year)
            if key in seen:
                continue
            seen.add(key)
            pairs.append(key)
    return pairs


def _run_catalog(_args: Sequence[str]) -> int:
    support = _private_import("flow.support")
    catalog = support.catalog_by_id()
    venues = sorted(
        [dict(item) for item in catalog.values() if isinstance(item, dict)],
        key=lambda item: (
            str(item.get("source") or ""),
            str(item.get("field") or ""),
            str(item.get("type") or ""),
            str(item.get("rank") or ""),
            str(item.get("name") or ""),
            str(item.get("id") or ""),
        ),
    )
    print(json.dumps({"venues": venues}, ensure_ascii=False, indent=2))
    return 0


def _run_venue_health(args: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="Check Finding venue/year availability and return sample metadata.")
    parser.add_argument("--selection-json", default="", help="VenueSelection-compatible source selection JSON.")
    parser.add_argument("--sample-limit", type=int, default=3)
    ns = parser.parse_args(list(args))
    source_selection = _private_import("finding_runtime.source_selection")
    support = _private_import("flow.support")
    selection_payload = _load_json(ns.selection_json, source_selection.default_source_selection())
    selection = source_selection.normalize_source_selection(selection_payload)
    sample_limit = max(1, int(ns.sample_limit or 1))
    catalog = support.catalog_by_id()
    results: list[dict[str, Any]] = []
    for venue_id, year in _venue_health_pairs(selection):
        venue = catalog.get(venue_id)
        if not venue:
            results.append({
                "venue_id": venue_id,
                "year": year,
                "ok": False,
                "sample_count": 0,
                "source_adapter": "unknown",
                "message": "Unknown venue id.",
                "samples": [],
            })
            continue
        try:
            result = support.fetch_venue_sample(venue, year, sample_limit)
        except Exception as exc:
            result = {
                "venue_id": venue_id,
                "year": year,
                "ok": False,
                "sample_count": 0,
                "source_adapter": "error",
                "message": str(exc) or "Venue health check failed.",
                "samples": [],
            }
        if isinstance(result, dict):
            result.setdefault("venue_id", venue_id)
            result.setdefault("year", year)
            results.append(result)
    print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
    return 0


def _run_find(args: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="Run Finding.")
    parser.add_argument("--config-json", default="", help="AppConfig-compatible JSON with LLM/profile fields.")
    parser.add_argument("--selection-json", default="", help="VenueSelection-compatible source selection JSON.")
    ns = parser.parse_args(list(args))
    models = _private_import("finding_runtime.models")
    source_selection = _private_import("finding_runtime.source_selection")
    find_pipeline = _private_import("flow.pipeline")

    config_payload = _load_json(ns.config_json, {})
    if not isinstance(config_payload, dict):
        config_payload = {}
    config = models.AppConfig(**_with_llm_env_defaults(config_payload))
    applied_runtime_tuning = models.apply_runtime_tuning_env(config)
    selection_payload = _load_json(ns.selection_json, source_selection.default_source_selection())
    selection = models.VenueSelection(**source_selection.normalize_source_selection(selection_payload))
    log_stream = io.StringIO()
    with contextlib.redirect_stdout(log_stream):
        result = find_pipeline.run_find(models.FindRequest(config=config, selection=selection))
    logs = log_stream.getvalue()
    if logs:
        print(logs, end="", file=sys.stderr)
    run_id = str(result.get("run_id") or "") if isinstance(result, dict) else ""
    run_dir = ""
    if run_id:
        storage = _private_import("finding_runtime.storage")

        run_dir = _display_path(storage.run_dir(run_id))
    print(json.dumps({"run_id": run_id, "run_dir": run_dir, "runtime_tuning_keys": sorted(applied_runtime_tuning), "summary": _result_summary(result, run_dir)}, ensure_ascii=False, indent=2))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Finding CLI.", add_help=True)
    parser.add_argument("--action", default="find", help="Backend action. Default: find.")
    parser.add_argument("--contract", action="store_true")
    argv_list = list(sys.argv[1:] if argv is None else argv)
    if "--contract" in argv_list:
        print(json.dumps(_contract_payload(), ensure_ascii=False, indent=2))
        return 0
    action = "find"
    for index, item in enumerate(argv_list):
        if item == "--action" and index + 1 < len(argv_list):
            action = _normalize_action(argv_list[index + 1])
            break
        if item.startswith("--action="):
            action = _normalize_action(item.split("=", 1)[1])
            break
    if action not in DIRECT_ACTIONS and not any(item in {"-h", "--help"} for item in argv_list[:1]):
        parser = argparse.ArgumentParser(description="Finding CLI.", add_help=False)
        parser.add_argument("--action", default="find", help="Backend action. Default: find.")
    ns, rest = parser.parse_known_args(argv_list)
    action = _normalize_action(ns.action)
    if action in DIRECT_ACTIONS:
        return _run_find(rest)
    if action in {"refresh_source_health", "source_health", "refresh_venue_source_health"}:
        return _run_refresh_source_health(rest)
    if action in {"catalog", "venue_catalog"}:
        return _run_catalog(rest)
    if action in {"venue_health", "check_venue_health"}:
        return _run_venue_health(rest)
    return _run_action_module(ACTION_ALIASES.get(action, action), rest)


if __name__ == "__main__":
    raise SystemExit(main())
