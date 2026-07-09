#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import signal
import select
import subprocess
import time
import sys
from pathlib import Path

from project_paths import ROOT, build_paths, conda_executable, management_python

from taste_pythonpath import ensure_taste_pythonpath
ensure_taste_pythonpath(ROOT)
from auto_research.source_selection import canonical_source_selection, normalize_source_selection
from project_paths import build_paths as _build_project_paths

DEFAULT_ENV = os.environ.get("FIND_ENV_NAME") or os.environ.get("CONDA_ENV_NAME", "")
DEFAULT_CORE_VENUE_IDS = ["openreview_iclr_2026", "openreview_neurips", "dblp_icml", "dblp_kdd"]
DEFAULT_LOCAL_LLM_CONFIG_PATH = ROOT / "modules" / "finding" / "config" / "llm.local.json"


def _local_llm_config_path() -> Path:
    raw = os.environ.get("FINDING_LLM_CONFIG", "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_LOCAL_LLM_CONFIG_PATH


def _load_local_llm_config() -> dict:
    path = _local_llm_config_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _env_or_local_api_key(env: dict[str, str] | None = None) -> str:
    source = env if env is not None else os.environ
    local_llm = _load_local_llm_config()
    api_key_env = source.get("LLM_API_KEY_ENV") or "OPENAI_API_KEY"
    return (
        (source.get(api_key_env, "") if api_key_env else "")
        or source.get("LLM_API_KEY", "")
        or str(local_llm.get("api_key") or "")
    )

DRIVER_TEMPLATE = r'''
from __future__ import annotations
import json
import os
import shutil
import signal
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

root = Path({root_json})
framework_scripts = root / "framework" / "scripts"
if str(framework_scripts) not in sys.path:
    sys.path.insert(0, str(framework_scripts))
from taste_pythonpath import ensure_taste_pythonpath
ensure_taste_pythonpath(root)
os.environ["WORKFLOW_RUNTIME_DIR"] = os.environ.get("FINDING_RUNTIME_DIR") or str(root / "modules" / "finding" / ".runtime")

from project_paths import build_paths, load_project_config
from sync_outputs import adopt_taste_find_run

DEFAULT_CORE_VENUE_IDS = {core_venue_ids_json}
project = {project_json}
max_papers = {max_papers}
max_ideas = {max_ideas}
repair_rounds = {repair_rounds}
include_arxiv = {include_arxiv}
include_huggingface = {include_huggingface}
include_github = {include_github}
use_venues = {use_venues}
source_selection = {source_selection_json}
api_mode = {api_mode_json}
paths = build_paths(project)
internal_output_dir_raw = os.environ.get("TASTE_INTERNAL_FIND_OUTPUT_DIR", "").strip()
internal_output_dir = Path(internal_output_dir_raw).expanduser() if internal_output_dir_raw else None
publish_outputs = internal_output_dir is None
finding_module = root / "modules" / "finding"
finding_entrypoint = finding_module / "main.py"
module_find_config_path = finding_module / "config" / "find.config.json"
project_find_config_path = paths.root / "config" / "finding.json"
def read_json_file(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {{}}
    return payload if isinstance(payload, dict) else {{}}

def write_json_file(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

def split_find_config_payload(payload):
    if not isinstance(payload, dict):
        return {{}}, {{}}
    if "config" in payload or "selection" in payload:
        config_payload = payload.get("config") if isinstance(payload.get("config"), dict) else {{}}
        selection_payload = payload.get("selection") if isinstance(payload.get("selection"), dict) else {{}}
        config_payload = dict(config_payload)
    else:
        config_payload = dict(payload)
        selection_payload = {{}}
    embedded_selection = config_payload.pop("default_find_selection", None)
    if isinstance(embedded_selection, dict) and not selection_payload:
        selection_payload = embedded_selection
    return config_payload, dict(selection_payload)

def ensure_project_find_config():
    if not project_find_config_path.exists():
        payload = read_json_file(module_find_config_path)
        if not payload:
            payload = {{"schema_version": 1, "config": {{}}, "selection": dict(source_selection)}}
        write_json_file(project_find_config_path, payload)
    return project_find_config_path

project_find_config_source = ensure_project_find_config()
project_find_config_payload = read_json_file(project_find_config_source)
finding_cfg, configured_selection = split_find_config_payload(project_find_config_payload)
legacy_finding_cfg = {{}}

def local_llm_config_path():
    raw = os.environ.get("FINDING_LLM_CONFIG", "").strip()
    return Path(raw).expanduser() if raw else finding_module / "config" / "llm.local.json"

local_llm_path = local_llm_config_path()
if not os.environ.get("FINDING_LLM_CONFIG", "").strip() and local_llm_path.exists():
    os.environ["FINDING_LLM_CONFIG"] = str(local_llm_path)
local_llm = read_json_file(local_llm_path)
input_dir_raw = os.environ.get("TASTE_FIND_INPUT_DIR", "").strip()
input_dir = Path(input_dir_raw).expanduser() if input_dir_raw else paths.root / "tmp" / "finding" / "input"
input_dir.mkdir(parents=True, exist_ok=True)
cfg = load_project_config(project)
legacy_finding_cfg = cfg.get("finding", {{}}) if isinstance(cfg.get("finding", {{}}), dict) else {{}}
for _key, _value in legacy_finding_cfg.items():
    if _key not in finding_cfg and _key not in {{"api_key", "email", "llm_roles", "provider", "base_url", "model", "temperature", "default_find_selection"}}:
        finding_cfg[_key] = _value
api_key_env = os.environ.get("LLM_API_KEY_ENV") or "OPENAI_API_KEY"
api_key = (os.environ.get(api_key_env, "") if api_key_env else "") or os.environ.get("LLM_API_KEY", "") or local_llm.get("api_key", "")
api_base = os.environ.get("LLM_API_BASE") or local_llm.get("base_url") or "https://api.openai.com/v1"
model = os.environ.get("LLM_MODEL") or local_llm.get("model") or "mock-model"
provider = os.environ.get("LLM_PROVIDER") or local_llm.get("provider") or "mock"
if not (api_base and model and api_key):
    provider = "mock"

def read_text(path, limit=5000):
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore")[:limit]
    except Exception:
        return ""

def env_int(name, default):
    raw = os.environ.get(name, "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else int(default)

next_actions = read_text(paths.planning / "next_actions.md", 4000)
evolution = read_text(paths.reports / "evolution_memory.md", 4000)
reflection = read_text(paths.reports / "iteration_reflection.md", 4000)
last_taste = read_text(paths.planning / "finding_frontend.md", 3000)
research_goal = "\n".join([str(cfg.get("topic", "")), str(cfg.get("user_prompt", "")), ", ".join(cfg.get("queries", []))])
configured_topic = str(finding_cfg.get("research_topic") or cfg.get("topic") or research_goal).strip()
feedback_profile = f"""
Research goal:
{{research_goal}}

The workflow should prioritize papers and ideas that directly help the current research loop. Strong preference:
- papers matching the project topic, configured queries, and current research plan
- recent high-quality papers with runnable code, datasets, and clear evaluation protocols
- ideas that can become executable plans with baselines, bad-case slicing, counterexamples, and prune/deepen rules
- literature signals that connect to local repo/data/env audits instead of standalone claims

Avoid generic adjacent papers unless they provide a transferable mechanism for the configured project topic.

TASTE next actions:
{{next_actions}}

TASTE evolution memory:
{{evolution}}

TASTE iteration reflection:
{{reflection}}

Previous TASTE frontend summary:
{{last_taste}}
"""[:18000]

topic_queries = []
for item in cfg.get("queries", []):
    if isinstance(item, str) and item.strip():
        topic_queries.append(item.strip())
extra_queries = []
for raw in os.environ.get("EXTRA_QUERIES", "").splitlines():
    raw = raw.strip()
    if not raw:
        continue
    try:
        decoded = json.loads(raw)
    except Exception:
        decoded = raw
    if isinstance(decoded, list):
        extra_queries.extend(str(item).strip() for item in decoded if str(item).strip())
    elif isinstance(decoded, str) and decoded.strip():
        extra_queries.extend(item.strip() for item in decoded.replace(";", "\n").split("\n") if item.strip())
if os.environ.get("EXTRA_QUERY", "").strip():
    extra_queries.extend(item.strip() for item in os.environ.get("EXTRA_QUERY", "").replace(";", "\n").split("\n") if item.strip())
for item in extra_queries:
    if item and item not in topic_queries:
        topic_queries.append(item)
if not topic_queries:
    topic = str(configured_topic or finding_cfg.get("research_interest") or cfg.get("research_interest") or research_goal).strip()
    if topic:
        topic_queries.extend([
            f"{{topic}} reproducible code dataset",
            f"{{topic}} recent benchmark method",
            f"{{topic}} executable research idea",
        ])

deep_survey = {deep_survey}
fast_mode = {fast_mode}
DEFAULT_ARXIV_WINDOW_DAYS = 180
literature_cfg = cfg.get("literature", {{}}) if isinstance(cfg.get("literature", {{}}), dict) else {{}}
for _key, _value in finding_cfg.items():
    if _key not in literature_cfg and _key not in {{"api_key", "email", "llm_roles"}}:
        literature_cfg[_key] = _value

def config_positive_int(name, default):
    try:
        value = int(literature_cfg.get(name) or 0)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else int(default)

arxiv_default_window_days = DEFAULT_ARXIV_WINDOW_DAYS if deep_survey else literature_cfg.get("arxiv_window_days", DEFAULT_ARXIV_WINDOW_DAYS) or DEFAULT_ARXIV_WINDOW_DAYS
arxiv_window_days = env_int("WINDOW_DAYS", arxiv_default_window_days)
max_fetch_count = config_positive_int("max_fetch_papers", max(120 if deep_survey else 80, max_papers * 6))
venue_scan_limit = env_int("VENUE_TITLE_SCAN_LIMIT", config_positive_int("venue_title_scan_limit", 12000 if deep_survey else 3000))
find_recall_count = env_int("FIND_RECALL_COUNT", config_positive_int("find_recall_count", 3000 if deep_survey else 200))
detail_fetch_count = env_int("DETAIL_FETCH_COUNT", config_positive_int("detail_fetch_count", 800 if deep_survey else 50))
arxiv_max_queries = env_int("ARXIV_MAX_QUERIES", config_positive_int("arxiv_max_queries", 3))
arxiv_per_query_limit = env_int("ARXIV_PER_QUERY_LIMIT", config_positive_int("arxiv_per_query_limit", 100 if deep_survey else 50))
arxiv_timeout_sec = env_int("ARXIV_TIMEOUT_SEC", config_positive_int("arxiv_timeout_sec", 45 if deep_survey else 15))
arxiv_candidate_limit = env_int("ARXIV_LLM_CANDIDATE_LIMIT", config_positive_int("arxiv_llm_candidate_limit", 0))
arxiv_per_category = env_int("ARXIV_LLM_CANDIDATES_PER_CATEGORY", config_positive_int("arxiv_llm_candidates_per_category", 0))
biorxiv_candidate_limit = env_int("BIORXIV_LLM_CANDIDATE_LIMIT", config_positive_int("biorxiv_llm_candidate_limit", 0))
biorxiv_per_category = env_int("BIORXIV_LLM_CANDIDATES_PER_CATEGORY", config_positive_int("biorxiv_llm_candidates_per_category", 0))
nature_candidate_limit = env_int("NATURE_CANDIDATE_LIMIT", config_positive_int("nature_candidate_limit", 200))
science_candidate_limit = env_int("SCIENCE_CANDIDATE_LIMIT", config_positive_int("science_candidate_limit", 200))
abstract_scoring_max_workers = env_int("ABSTRACT_SCORING_MAX_WORKERS", config_positive_int("abstract_scoring_max_workers", 6 if deep_survey else 4))
abstract_scoring_batch_size = env_int("ABSTRACT_SCORING_BATCH_SIZE", config_positive_int("abstract_scoring_batch_size", 10 if deep_survey else 6))
abstract_scoring_timeout_sec = env_int("ABSTRACT_SCORING_TIMEOUT_SEC", config_positive_int("abstract_scoring_timeout_sec", 180))

runtime_tuning = dict(literature_cfg.get("runtime_tuning") or {{}}) if isinstance(literature_cfg.get("runtime_tuning"), dict) else {{}}
def runtime_default(name, default=None):
    raw = os.environ.get(name, "")
    if str(raw).strip():
        runtime_tuning[name] = raw
    elif name not in runtime_tuning and default is not None:
        runtime_tuning[name] = default

runtime_keys = [
    "ARXIV_FULL_SCAN",
    "ARXIV_MAX_QUERIES",
    "ARXIV_PER_QUERY_LIMIT",
    "ARXIV_TIMEOUT_SEC",
    "MIN_TITLE_CANDIDATES",
    "MIN_DETAIL_CANDIDATES",
    "ABSTRACT_SCORING_BATCH_SIZE",
    "ABSTRACT_SCORING_MAX_BATCH_SIZE",
    "ABSTRACT_SCORING_MAX_TOKENS",
    "SINGLE_ABSTRACT_SCORING_MAX_TOKENS",
    "ABSTRACT_SCORING_LLM_RETRIES",
    "ABSTRACT_SCORING_WALL_TIMEOUT_SEC",
    "ABSTRACT_SCORING_MAX_WORKERS",
    "ABSTRACT_SCORING_WORKER_CAP",
    "ABSTRACT_SCORING_TIMEOUT_SEC",
    "OMITTED_ITEM_RETRY_ATTEMPTS",
    "USE_LLM_TITLE_FILTER",
    "LARGE_TITLE_POOL_THRESHOLD",
]
for key in runtime_keys:
    runtime_default(key)
runtime_default("ABSTRACT_SCORING_BATCH_SIZE", str(abstract_scoring_batch_size))
runtime_default("ABSTRACT_SCORING_MAX_BATCH_SIZE", str(max(1, abstract_scoring_batch_size)))
runtime_default("ABSTRACT_SCORING_MAX_WORKERS", str(abstract_scoring_max_workers))
runtime_default("ABSTRACT_SCORING_WORKER_CAP", str(max(1, abstract_scoring_max_workers)))
runtime_default("ABSTRACT_SCORING_TIMEOUT_SEC", str(abstract_scoring_timeout_sec))
if deep_survey:
    runtime_default("VENUE_TITLE_SCAN_LIMIT", str(venue_scan_limit))
    runtime_default("FIND_RECALL_COUNT", str(find_recall_count))
    runtime_default("DETAIL_FETCH_COUNT", str(detail_fetch_count))
    runtime_default("ARXIV_FULL_SCAN", "1")
    runtime_default("ARXIV_MAX_QUERIES", "3")
    runtime_default("ARXIV_PER_QUERY_LIMIT", "100")
    runtime_default("ARXIV_TIMEOUT_SEC", "45")
    runtime_default("MIN_TITLE_CANDIDATES", "240")
    runtime_default("MIN_DETAIL_CANDIDATES", "80")
    runtime_default("ABSTRACT_SCORING_BATCH_SIZE", "10")
    runtime_default("ABSTRACT_SCORING_MAX_BATCH_SIZE", "10")
    runtime_default("ABSTRACT_SCORING_MAX_TOKENS", "12000")
    runtime_default("SINGLE_ABSTRACT_SCORING_MAX_TOKENS", "3000")
    runtime_default("ABSTRACT_SCORING_LLM_RETRIES", "2")
    runtime_default("ABSTRACT_SCORING_WALL_TIMEOUT_SEC", "180")
    runtime_default("ABSTRACT_SCORING_MAX_WORKERS", "6")
    runtime_default("ABSTRACT_SCORING_WORKER_CAP", "6")
    runtime_default("ABSTRACT_SCORING_TIMEOUT_SEC", "180")
    runtime_default("OMITTED_ITEM_RETRY_ATTEMPTS", "2")
    runtime_default("LARGE_TITLE_POOL_THRESHOLD", "800")
if os.environ.get("DISABLE_LLM_TITLE_FILTER", "0").lower() in {{"1", "true", "yes", "on"}}:
    runtime_tuning["USE_LLM_TITLE_FILTER"] = "0"
elif os.environ.get("FORCE_LLM_TITLE_FILTER", "0").lower() in {{"1", "true", "yes", "on"}}:
    runtime_tuning["USE_LLM_TITLE_FILTER"] = "1"
elif deep_survey:
    runtime_default("USE_LLM_TITLE_FILTER", "1")

year = date.today().year
venue_ids = list(source_selection.get("venue_ids") or [])
if not use_venues:
    venue_ids = []
years = []
for item in source_selection.get("years") or [year]:
    try:
        years.append(int(item))
    except Exception:
        pass
if not years:
    years = [year]

project_interest = str(finding_cfg.get("research_interest") or cfg.get("research_interest") or cfg.get("user_prompt") or research_goal).strip()
project_profile = str(finding_cfg.get("researcher_profile") or cfg.get("researcher_profile") or "").strip()
if project_profile:
    researcher_profile = (project_profile + "\n\n" + feedback_profile)[:18000]
else:
    researcher_profile = feedback_profile

config_payload = {{
    "research_topic": configured_topic,
    "research_interest": project_interest or configured_topic,
    "researcher_profile": researcher_profile,
    "provider": provider,
    "base_url": api_base,
    "api_key": "",
    "model": model,
    "temperature": float(os.environ.get("LLM_TEMPERATURE") or local_llm.get("temperature", 0.2) or 0.2),
    "max_fetch_papers": max_fetch_count,
    "max_recommended_papers": max_papers,
    "max_ideas": max_ideas,
    "venue_title_scan_limit": venue_scan_limit,
    "find_recall_count": find_recall_count,
    "detail_fetch_count": detail_fetch_count,
    "arxiv_max_queries": arxiv_max_queries,
    "arxiv_per_query_limit": arxiv_per_query_limit,
    "arxiv_timeout_sec": arxiv_timeout_sec,
    "arxiv_llm_candidate_limit": arxiv_candidate_limit,
    "arxiv_llm_candidates_per_category": arxiv_per_category,
    "biorxiv_llm_candidate_limit": biorxiv_candidate_limit,
    "biorxiv_llm_candidates_per_category": biorxiv_per_category,
    "llm_concurrency": env_int("LLM_CONCURRENCY", config_positive_int("llm_concurrency", 4 if fast_mode else 8 if deep_survey else 6)),
    "idea_parallel_workers": env_int("IDEA_WORKERS", config_positive_int("idea_parallel_workers", 2 if deep_survey else 1)),
    "abstract_scoring_max_workers": abstract_scoring_max_workers,
    "abstract_scoring_batch_size": abstract_scoring_batch_size,
    "abstract_scoring_timeout_sec": abstract_scoring_timeout_sec,
    "arxiv_categories": literature_cfg.get("arxiv_categories") if isinstance(literature_cfg.get("arxiv_categories"), list) else ["cs.IR", "cs.LG", "cs.AI"],
    "arxiv_queries": topic_queries,
    "github_languages": literature_cfg.get("github_languages") if isinstance(literature_cfg.get("github_languages"), list) else ["python", "all"],
    "github_since": str(literature_cfg.get("github_since") or "monthly"),
    "arxiv_start_date": str(literature_cfg.get("arxiv_start_date") or (date.today() - timedelta(days=arxiv_window_days)).isoformat()),
    "arxiv_end_date": str(literature_cfg.get("arxiv_end_date") or date.today().isoformat()),
    "biorxiv_categories": literature_cfg.get("biorxiv_categories") if isinstance(literature_cfg.get("biorxiv_categories"), list) else ["bioinformatics"],
    "biorxiv_start_date": str(literature_cfg.get("biorxiv_start_date") or ""),
    "biorxiv_end_date": str(literature_cfg.get("biorxiv_end_date") or ""),
    "nature_journals": literature_cfg.get("nature_journals") if isinstance(literature_cfg.get("nature_journals"), list) else ["nature", "natmachintell", "natcomputsci", "nmeth", "ncomms"],
    "nature_article_types": literature_cfg.get("nature_article_types") if isinstance(literature_cfg.get("nature_article_types"), list) else ["article"],
    "nature_start_date": str(literature_cfg.get("nature_start_date") or ""),
    "nature_end_date": str(literature_cfg.get("nature_end_date") or ""),
    "nature_candidate_limit": nature_candidate_limit,
    "science_journals": literature_cfg.get("science_journals") if isinstance(literature_cfg.get("science_journals"), list) else ["science", "sciadv"],
    "science_article_types": literature_cfg.get("science_article_types") if isinstance(literature_cfg.get("science_article_types"), list) else ["Research Article"],
    "science_start_date": str(literature_cfg.get("science_start_date") or ""),
    "science_end_date": str(literature_cfg.get("science_end_date") or ""),
    "science_candidate_limit": science_candidate_limit,
    "runtime_tuning": runtime_tuning,
}}
selection_payload = dict(source_selection)
selection_payload.update({{
    "venue_ids": venue_ids,
    "years": years,
    "venue_years": [
        pair for pair in source_selection.get("venue_years", [])
        if isinstance(pair, dict) and str(pair.get("venue_id") or "") in set(venue_ids)
    ] if venue_ids else [],
    "include_arxiv": bool(include_arxiv),
    "include_huggingface": bool(include_huggingface),
    "include_github": bool(include_github),
    "include_biorxiv": bool(source_selection.get("include_biorxiv")),
    "include_nature": bool(source_selection.get("include_nature")),
    "include_science": bool(source_selection.get("include_science")),
}})
if venue_ids and not selection_payload.get("venue_years"):
    selection_payload["venue_years"] = [
        {{"venue_id": venue_id, "year": int(year_value)}}
        for venue_id in venue_ids
        for year_value in years
    ]

input_payload = {{
    "research_topic": configured_topic,
    "research_interest": project_interest or configured_topic,
    "researcher_profile": researcher_profile,
    "arxiv_queries": topic_queries,
}}
find_input_fields = {{"research_topic", "research_interest", "researcher_profile", "arxiv_queries"}}
find_llm_fields = {{"provider", "base_url", "api_key", "model", "temperature", "llm_roles"}}
find_config_payload = {{
    key: value
    for key, value in config_payload.items()
    if key not in find_input_fields and key not in find_llm_fields and key not in {{"default_find_selection", "email"}}
}}
combined_find_config = {{
    "schema_version": 1,
    "config": find_config_payload,
    "selection": selection_payload,
}}
write_json_file(project_find_config_path, combined_find_config)

find_config_path = input_dir / "find.config.json"
input_path = input_dir / "input.json"
config_path = input_dir / "config.json"
selection_path = input_dir / "selection.json"
write_json_file(find_config_path, combined_find_config)
write_json_file(input_path, input_payload)
config_path.write_text(json.dumps(config_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
selection_path.write_text(json.dumps(selection_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

def _extract_json_tail(text):
    for index in range(len(text) - 1, -1, -1):
        if text[index] != "{{":
            continue
        candidate = text[index:].strip()
        try:
            return json.loads(candidate)
        except Exception:
            continue
    return {{}}

find_cmd = [
    sys.executable,
    str(finding_entrypoint),
    "--action",
    "find",
    "--config-json",
    str(find_config_path),
    "--input-json",
    str(input_path),
]
print("[framework] Finding public CLI input: " + str(find_config_path) + " / " + str(input_path), flush=True)
proc = subprocess.Popen(find_cmd, cwd=str(root), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1)
find_output = []
assert proc.stdout is not None
for line in proc.stdout:
    find_output.append(line)
    print(line, end="", flush=True)
returncode = proc.wait()
combined_output = "".join(find_output)
if returncode != 0:
    raise SystemExit(returncode)
cli_payload = _extract_json_tail(combined_output)
run_id = str(cli_payload.get("run_id") or "")
run_dir_text = str(cli_payload.get("run_dir") or "")
if not run_id or not run_dir_text:
    raise RuntimeError("Finding CLI did not return run_id/run_dir")
directory = Path(run_dir_text)
if not directory.is_absolute():
    directory = finding_module / directory
if not (directory / "find_results.json").exists():
    raise RuntimeError("Finding CLI completed but find_results.json is missing: " + str(directory))
result = json.loads((directory / "find_results.json").read_text(encoding="utf-8"))
out_dir = internal_output_dir if internal_output_dir is not None else paths.planning / "finding"
out_dir.mkdir(parents=True, exist_ok=True)

STANDARD_FIND_ARTIFACTS = [
    "find.md", "source_status.md", "hf.md", "github.md",
    "find_results.json", "find_progress.json", "manifest.json", "selection.json",
    "venue_health_report.json", "category_scan_report.json", "title_filter_report.json",
    "arxiv_raw.json", "arxiv_prefiltered.json", "biorxiv.md", "biorxiv_raw.json",
    "biorxiv_prefiltered.json", "nature.md", "nature_raw.json", "nature_prefiltered.json",
    "science.md", "science_raw.json", "science_prefiltered.json",
]

def _copy_find_artifacts(target_dir):
    copied = []
    target_dir.mkdir(parents=True, exist_ok=True)
    for name in STANDARD_FIND_ARTIFACTS:
        source = directory / name
        if source.exists():
            shutil.copyfile(source, target_dir / name)
            copied.append(name)
    return copied

if publish_outputs:
    adopt_taste_find_run(paths, {{"taste_run_id": run_id, "taste_run_dir": str(directory)}}, run_id)
else:
    _copy_find_artifacts(out_dir)

def _safe_int(value, default=0):
    try:
        if value is None or value == "":
            return default
        return int(float(str(value).strip()))
    except Exception:
        return default


def _survey_stats_from_find(find_result):
    category_scan_rows = find_result.get("category_scan_report", []) if isinstance(find_result, dict) else []
    title_filter_rows = find_result.get("title_filter_report", []) if isinstance(find_result, dict) else []
    source_rows = find_result.get("source_status", []) if isinstance(find_result, dict) else []
    venue_rows = find_result.get("venue_health_report", []) if isinstance(find_result, dict) else []
    arxiv_row = next((row for row in source_rows if isinstance(row, dict) and row.get("source") == "arxiv"), {{}})
    raw_count = len(find_result.get("raw_title_index", [])) if isinstance(find_result, dict) else 0
    if not raw_count:
        raw_count = sum(_safe_int((row if isinstance(row, dict) else {{}}).get("corpus_count") or (row if isinstance(row, dict) else {{}}).get("sample_count") or (row if isinstance(row, dict) else {{}}).get("raw_title_index_count"), 0) for row in venue_rows)
    if not raw_count:
        raw_count = sum(_safe_int((row if isinstance(row, dict) else {{}}).get("raw_title_index_count"), 0) for row in source_rows)
    evaluated = find_result.get("evaluated_candidates", []) if isinstance(find_result, dict) else []
    llm_scored = sum(1 for row in evaluated if isinstance(row, dict) and str(row.get("reason_source") or "") == "llm abstract evaluation")
    return {{
        "deep_survey": deep_survey,
        "raw_title_index_papers": raw_count,
        "venue_total_papers_available": raw_count,
        "venue_corpus_audited_papers": raw_count,
        "category_corpus_audited_papers": sum(_safe_int(row.get("corpus_audit_papers") or row.get("total_papers"), 0) for row in category_scan_rows if isinstance(row, dict)),
        "venue_category_selected_papers": sum(_safe_int(row.get("selected_category_papers"), 0) for row in category_scan_rows if isinstance(row, dict)),
        "venue_title_filter_input_papers": sum(_safe_int(row.get("title_filter_input_papers"), 0) for row in title_filter_rows if isinstance(row, dict)),
        "venue_final_title_candidates": sum(_safe_int(row.get("final_title_candidates"), 0) for row in title_filter_rows if isinstance(row, dict)),
        "venue_detail_fetched_candidates": len(evaluated),
        "venue_evaluated_candidates": len(evaluated),
        "llm_scored_candidates": llm_scored or len(evaluated),
        "full_venue_corpus_audit": bool(raw_count),
        "llm_scoring_policy": "Full venue corpus is audited; category/title-screened candidates are batch-scored by LLM for efficiency.",
        "venue_read_candidates": len(find_result.get("strong_recommendations", []) or find_result.get("articles", [])) if isinstance(find_result, dict) else 0,
        "strong_recommendations": len(find_result.get("strong_recommendations", []) or find_result.get("articles", [])) if isinstance(find_result, dict) else 0,
        "category_scan_reports": len(category_scan_rows),
        "title_filter_reports": len(title_filter_rows),
        "arxiv_raw_count": len(find_result.get("arxiv_raw", [])) if isinstance(find_result, dict) else 0,
        "arxiv_prefiltered_count": len(find_result.get("arxiv_prefiltered", [])) if isinstance(find_result, dict) else 0,
        "arxiv_pages_fetched": arxiv_row.get("pages_fetched", 0) if isinstance(arxiv_row, dict) else 0,
        "arxiv_full_scan": arxiv_row.get("full_scan", False) if isinstance(arxiv_row, dict) else False,
        "arxiv_deduped_count": arxiv_row.get("deduped_count", 0) if isinstance(arxiv_row, dict) else 0,
    }}

def _write_frontend_state(stage, find_result, read_result=None, idea_result=None, plan_result=None):
    stats = _survey_stats_from_find(find_result)
    payload = {{
        "project": project,
        "repo_root": str(root),
        "taste_run_id": run_id,
        "taste_run_dir": str(directory),
        "output_dir": str(out_dir),
        "internal_literature_survey": not publish_outputs,
        "web_visible": publish_outputs,
        "provider": provider,
        "base_url": api_base,
        "model": model,
        "llm_enabled": provider != "mock",
        "api_mode": api_mode,
        "stage": stage,
        "status": stage,
        "max_papers": max_papers,
        "max_ideas": max_ideas,
        "repair_rounds": repair_rounds,
        "include_arxiv": include_arxiv,
        "include_huggingface": include_huggingface,
        "include_github": include_github,
        "survey_stats": stats,
        "venue_ids": venue_ids,
        "years": years,
        "targeted_queries": extra_queries,
        "topic_queries": topic_queries,
        "arxiv_window_days": arxiv_window_days,
        "survey_policy": {{
            "scope": "configured venue channels with full-corpus audit, category prefilter, and screened-candidate LLM scoring",
            "title_scan_limit": venue_scan_limit,
            "title_scan_limit_meaning": "safety cap per venue/year; local databases smaller than this are scanned fully",
            "find_recall_count": find_recall_count,
            "find_recall_count_meaning": "high-recall title candidates kept after local ranking for downstream scoring",
            "detail_fetch_count": detail_fetch_count,
            "detail_fetch_count_meaning": "top high-recall candidates whose abstracts/details are fetched and strictly scored",
            "llm_title_filter_policy": "large title pools use local high-recall ranking; LLM judges abstracts/details downstream",
            "arxiv_window_days": arxiv_window_days,
        }},
        "counts": {{
            "strong_recommendations": len(find_result.get("strong_recommendations", []) or find_result.get("articles", [])) if isinstance(find_result, dict) else 0,
            "evaluated_candidates": len(find_result.get("evaluated_candidates", [])) if isinstance(find_result, dict) else 0,
            "huggingface": len(find_result.get("huggingface", [])) if isinstance(find_result, dict) else 0,
            "github": len(find_result.get("github", [])) if isinstance(find_result, dict) else 0,
            "readings": len((read_result or {{}}).get("readings", [])),
            "ideas": len((idea_result or {{}}).get("ideas", [])),
            "plans": len((plan_result or {{}}).get("plans", [])),
        }},
    }}
    state_path = paths.state / "finding_frontend.json" if publish_outputs else out_dir / "finding_frontend.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    summary = ["# Find Frontend\n\n"]
    for key in ["taste_run_id", "stage", "provider", "api_mode", "model", "output_dir"]:
        summary.append("- " + key + ": " + str(payload.get(key, "")) + "\n")
    if extra_queries:
        summary.append("- targeted_queries: " + json.dumps(extra_queries, ensure_ascii=False) + "\n")
    for key, value in payload["counts"].items():
        summary.append("- " + key + ": " + str(value) + "\n")
    summary.append("\n## Survey Coverage\n")
    for key in ["deep_survey", "venue_total_papers_available", "venue_corpus_audited_papers", "venue_category_selected_papers", "venue_title_filter_input_papers", "venue_final_title_candidates", "venue_detail_fetched_candidates", "llm_scored_candidates", "venue_read_candidates", "arxiv_raw_count", "arxiv_prefiltered_count", "arxiv_pages_fetched"]:
        summary.append("- " + key + ": " + str(stats.get(key)) + "\n")
    summary.append("\n## Survey Policy\n")
    summary.append("- scope: configured venue channels with full-corpus audit; category signals are prefilters, not a requirement to LLM-score every crawled paper\n")
    summary.append(f"- title_scan_limit: {{venue_scan_limit}}; safety cap, not a target sample size\n")
    summary.append(f"- find_recall_count: {{find_recall_count}}; high-recall title candidates kept for downstream scoring\n")
    summary.append(f"- detail_fetch_count: {{detail_fetch_count}}; candidates fetched/scored with details\n")
    summary.append("- large title pools: local high-recall ranking first; LLM strict judging at abstract/detail stage\n")
    summary.append("\n## Usage In TASTE\n")
    summary.append("- Find-stage artifacts are written immediately after Find so Claude/experiments can use them while Read/Idea/Plan continues.\n")
    summary.append("- Treat candidates not promoted by evidence gates as literature signals only, not paper-claim evidence.\n")
    frontend_md_path = paths.planning / "finding_frontend.md" if publish_outputs else out_dir / "finding_frontend.md"
    frontend_md_path.parent.mkdir(parents=True, exist_ok=True)
    frontend_md_path.write_text("".join(summary), encoding="utf-8")
    return payload

_write_frontend_state("find_completed", result)

def _taste_article_from_item(row, source):
    title = str(row.get("title") or "").strip()
    if not title:
        return None
    return {{
        "id": str(row.get("id") or row.get("paper_id") or row.get("entry_id") or title)[:120],
        "source": source,
        "title": title,
        "authors": ", ".join(row.get("authors", [])) if isinstance(row.get("authors"), list) else str(row.get("authors", "")),
        "abstract": str(row.get("abstract") or row.get("summary") or row.get("tldr") or row.get("reason") or "")[:6000],
        "url": row.get("url") or row.get("abs_url") or row.get("entry_id") or "",
        "pdf_url": row.get("pdf_url") or "",
        "venue": row.get("venue") or row.get("source") or "TASTE-cache",
        "year": int(str(row.get("year") or row.get("published", "") or date.today().year)[:4]) if str(row.get("year") or row.get("published", "") or date.today().year)[:4].isdigit() else date.today().year,
        "category": row.get("category") or ", ".join(row.get("categories", [])) if isinstance(row.get("categories", []), list) else str(row.get("category", "")),
        "classification_source": row.get("classification_source") or "fallback",
        "fit_score": row.get("fit_score") or row.get("discovery_priority_score") or 7.0,
        "diversity_score": row.get("diversity_score") or 6.0,
        "score": row.get("score") or row.get("discovery_priority_score") or 7.0,
        "reason": row.get("reason") or row.get("taste_reason") or "Recovered from TASTE discovery cache because live TASTE sources were unavailable.",
        "reason_source": row.get("reason_source") or "TASTE discovery cache recovery",
    }}

def _load_backup_articles(limit):
    candidates = []
    # Prefer prior successful TASTE runs because they already match TASTE's schema.
    runs_root = root / "runtime" / "runs"
    for find_path in sorted(runs_root.glob("find_*/find_results.json"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True):
        if run_id in str(find_path):
            continue
        try:
            payload = json.loads(find_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for row in payload.get("articles", []):
            item = _taste_article_from_item(row, "taste_history")
            if item:
                candidates.append(item)
        if len(candidates) >= limit:
            break
    # Then use TASTE's broader discovery cache.
    for path in sorted(paths.discover.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for row in payload.get("items", []) if isinstance(payload, dict) else []:
            item = _taste_article_from_item(row, "discovery_cache")
            if item:
                candidates.append(item)
        if len(candidates) >= limit * 4:
            break
    seen = set()
    unique = []
    for item in candidates:
        key = item["title"].lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
        if len(unique) >= limit:
            break
    return unique

if not result.get("strong_recommendations") and not result.get("articles") and not (
    result.get("screened_ranking")
    or result.get("evaluated_candidates")
    or result.get("title_candidates")
    or result.get("retrieval_candidates")
):
    if os.environ.get("ALLOW_STALE_CACHE_RECOVERY", "0").lower() in {{"1", "true", "yes", "on"}}:
        backup_articles = _load_backup_articles(max_papers)
        if backup_articles:
            print(f"Live sources produced no recommendations; recovered {{len(backup_articles)}} recommendations from TASTE cache", flush=True)
            result["strong_recommendations"] = backup_articles
            result.setdefault("source_status", []).append({{"source": "cache_recovery", "ok": True, "limited": True, "count": len(backup_articles), "message": "Recovered articles from TASTE cache because live sources were empty or rate-limited."}})
            (directory / "find_results.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + chr(10), encoding="utf-8")
            article_lines = ["# Recommended Articles", ""]
            for index, item in enumerate(backup_articles, 1):
                article_lines.extend([
                    f"## {{index}}. {{item.get('title', 'Untitled')}}",
                    "",
                    f"- Source: {{item.get('source', '')}}",
                    f"- Venue: {{item.get('venue', '')}}",
                    f"- URL: {{item.get('url', '')}}",
                    "",
                    str(item.get("abstract") or item.get("reason") or "").strip(),
                    "",
                ])
            recovered_article = chr(10).join(article_lines)
            (directory / "find.md").write_text(recovered_article, encoding="utf-8")
            status_lines = ["# Source Status", "", "## cache_recovery", "", f"- **Status**: ok", f"- **Count**: {{len(backup_articles)}}", "- **Message**: Recovered articles from TASTE cache because live sources were empty or rate-limited.", ""]
            (directory / "source_status.md").write_text(chr(10).join(status_lines), encoding="utf-8")
    else:
        raise RuntimeError("Fresh Find produced no usable candidates; stale TASTE cache recovery is disabled. Fix sources/scoring before continuing.")

if publish_outputs:
    adopt_taste_find_run(paths, {{"taste_run_id": run_id, "taste_run_dir": str(directory)}}, run_id)
else:
    _copy_find_artifacts(out_dir)

payload = _write_frontend_state("find_completed", result)
print(json.dumps(payload, ensure_ascii=False))
'''


def driver_python_command(args: argparse.Namespace, cfg: dict, driver: Path) -> list[str]:
    env_name = str(getattr(args, "env_name", "") or "").strip()
    if env_name:
        conda = conda_executable(cfg)
        if not conda:
            raise RuntimeError(f"conda not found for --env-name {env_name}; use MANAGEMENT_PYTHON or clear --env-name")
        return [conda, "run", "--no-capture-output", "-n", env_name, "python", str(driver)]

    runtime = cfg.get("runtime", {}) if isinstance(cfg.get("runtime", {}), dict) else {}
    for candidate in [
        os.environ.get("MANAGEMENT_PYTHON", ""),
        runtime.get("management_python"),
        runtime.get("python_executable"),
        cfg.get("python_executable"),
        management_python(),
        sys.executable,
    ]:
        value = str(candidate or "").strip()
        if value and Path(value).expanduser().exists():
            return [str(Path(value).expanduser()), str(driver)]
    return [sys.executable, str(driver)]


def run(cmd: list[str], cwd: Path = ROOT, env: dict[str, str] | None = None, timeout_sec: int = 900, live_log_path: Path | None = None) -> subprocess.CompletedProcess[str]:
    proc = subprocess.Popen(cmd, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, start_new_session=True, bufsize=1)
    started = time.monotonic()
    lines: list[str] = []
    last_heartbeat = 0.0
    if live_log_path:
        live_log_path.parent.mkdir(parents=True, exist_ok=True)
        live_log_path.write_text(f"[frontend] started pid={proc.pid} timeout_sec={timeout_sec}\n", encoding="utf-8")
    assert proc.stdout is not None
    try:
        while True:
            if timeout_sec and time.monotonic() - started > timeout_sec:
                raise subprocess.TimeoutExpired(cmd, timeout_sec, output="".join(lines), stderr="")
            return_code = proc.poll()
            ready, _, _ = select.select([proc.stdout], [], [], 0.2)
            if ready:
                line = proc.stdout.readline()
                if line:
                    clean = redact(line)
                    lines.append(clean)
                    print(clean, end="", flush=True)
                    if live_log_path:
                        with live_log_path.open("a", encoding="utf-8") as handle:
                            handle.write(clean)
                    continue
            if return_code is not None:
                remainder = proc.stdout.read() or ""
                if remainder:
                    clean = redact(remainder)
                    lines.append(clean)
                    print(clean, end="", flush=True)
                    if live_log_path:
                        with live_log_path.open("a", encoding="utf-8") as handle:
                            handle.write(clean)
                return subprocess.CompletedProcess(cmd, return_code, "".join(lines), "")
            if live_log_path and time.monotonic() - last_heartbeat > 30:
                # Keep liveness in process/state metadata. Repeating idle heartbeats
                # drown out the real Find channel/LLM scoring steps in the UI log.
                last_heartbeat = time.monotonic()
            time.sleep(0.2)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                proc.kill()
            proc.wait()
        exc.output = "".join(lines)
        exc.stderr = ""
        raise exc


def read_focus_queries(path: str) -> list[str]:
    if not path:
        return []
    focus_path = Path(path)
    if not focus_path.exists():
        return []
    text = focus_path.read_text(encoding="utf-8", errors="ignore")
    values: list[str] = []
    if focus_path.suffix.lower() == ".json":
        try:
            payload = json.loads(text)
        except Exception:
            payload = None
        if isinstance(payload, dict):
            for key in ["queries", "followup_queries", "suggested_followup_queries", "targets"]:
                for item in payload.get(key, []) if isinstance(payload.get(key, []), list) else []:
                    if isinstance(item, str) and item.strip():
                        values.append(item.strip())
                    elif isinstance(item, dict):
                        title = str(item.get("title") or item.get("query") or item.get("name") or "").strip()
                        if title:
                            values.append(title)
        elif isinstance(payload, list):
            for item in payload:
                if isinstance(item, str) and item.strip():
                    values.append(item.strip())
                elif isinstance(item, dict):
                    title = str(item.get("title") or item.get("query") or item.get("name") or "").strip()
                    if title:
                        values.append(title)
    else:
        values.extend(line.strip(" -\t") for line in text.splitlines() if line.strip(" -\t"))
    return values


def merge_extra_queries(args: argparse.Namespace) -> list[str]:
    values = list(args.query or []) + read_focus_queries(args.focus_file)
    seen = set()
    out: list[str] = []
    for value in values:
        text = " ".join(str(value or "").split())
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
    existing: list[str] = []
    raw = os.environ.get("EXTRA_QUERIES", "").strip()
    if raw:
        try:
            decoded = json.loads(raw)
        except Exception:
            decoded = raw
        if isinstance(decoded, list):
            existing.extend(str(item).strip() for item in decoded if str(item).strip())
        elif isinstance(decoded, str):
            existing.extend(item.strip() for item in decoded.replace(";", "\n").split("\n") if item.strip())
    merged = []
    seen = set()
    for value in existing + out:
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            merged.append(value)
    if merged:
        os.environ["EXTRA_QUERIES"] = json.dumps(merged, ensure_ascii=False)
    return merged


def write_driver(path: Path, project: str, max_papers: int, max_ideas: int, repair_rounds: int, include_arxiv: bool, include_huggingface: bool, include_github: bool, use_venues: bool, source_selection: dict[str, Any], *, deep_survey: bool = False, fast_mode: bool = False) -> None:
    code = DRIVER_TEMPLATE.format(
        root_json=json.dumps(str(ROOT)),
        taste_root_json=json.dumps(str(ROOT)),
        project_json=json.dumps(project),
        max_papers=max_papers,
        max_ideas=max_ideas,
        repair_rounds=repair_rounds,
        include_arxiv=include_arxiv,
        include_huggingface=include_huggingface,
        include_github=include_github,
        use_venues=use_venues,
        source_selection_json=repr(source_selection),
        api_mode_json=json.dumps(os.environ.get("LLM_API_MODE", "chat_completions")),
        core_venue_ids_json=json.dumps(DEFAULT_CORE_VENUE_IDS),
        deep_survey=bool(deep_survey),
        fast_mode=bool(fast_mode),
    )
    path.write_text(code, encoding="utf-8")


def redact(text: str) -> str:
    for key in ["OPENAI_API_KEY", "LLM_API_KEY"]:
        value = os.environ.get(key, "")
        if value:
            text = text.replace(value, "<redacted>")
    local_key = str(_load_local_llm_config().get("api_key") or "")
    if local_key:
        text = text.replace(local_key, "<redacted>")
    return text


def _load_project_config(project: str) -> dict:
    try:
        return __import__("project_paths").load_project_config(project)
    except Exception:
        return {}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else int(default)


def _effective_source_selection(args: argparse.Namespace) -> dict[str, Any]:
    project_path = _build_project_paths(args.project).config
    selection = canonical_source_selection(project_config_path=project_path)
    if getattr(args, "skip_venues", False):
        selection["venue_ids"] = []
    if getattr(args, "skip_arxiv", False):
        selection["include_arxiv"] = False
    if getattr(args, "skip_huggingface", False):
        selection["include_huggingface"] = False
    if getattr(args, "skip_github", False):
        selection["include_github"] = False
    return normalize_source_selection(selection)


def _taste_signature(args: argparse.Namespace, extra_queries: list[str]) -> dict:
    cfg = _load_project_config(args.project)
    literature_cfg = cfg.get("literature", {}) if isinstance(cfg.get("literature", {}), dict) else {}
    deep = bool(args.deep_survey)
    wide = bool(args.wide_survey)
    selection = _effective_source_selection(args)
    venues = list(selection.get("venue_ids") or [])
    years = [int(item) for item in selection.get("years") or [dt.date.today().year]]
    window_days = _env_int("WINDOW_DAYS", 180 if deep else int(literature_cfg.get("primary_window_days", 90) or 90))
    topic_queries: list[str] = []
    for item in cfg.get("queries", []) or []:
        if isinstance(item, str) and item.strip():
            topic_queries.append(" ".join(item.split()))
    for item in extra_queries:
        if item and item not in topic_queries:
            topic_queries.append(item)
    local_llm = _load_local_llm_config()
    api_key_env = os.environ.get("LLM_API_KEY_ENV") or "OPENAI_API_KEY"
    api_key = (os.environ.get(api_key_env, "") if api_key_env else "") or os.environ.get("LLM_API_KEY", "") or str(local_llm.get("api_key") or "")
    api_base = os.environ.get("LLM_API_BASE") or local_llm.get("base_url") or ""
    model = os.environ.get("LLM_MODEL") or local_llm.get("model") or "mock-model"
    provider = os.environ.get("LLM_PROVIDER") or local_llm.get("provider") or "mock"
    payload = {
        "schema": 1,
        "scoring_policy_version": "quality_bonus_v4_selective_venue",
        "project": args.project,
        "topic": str(cfg.get("topic", "")),
        "user_prompt": str(cfg.get("user_prompt", "")),
        "queries": topic_queries,
        "deep_survey": deep,
        "wide_survey": wide,
        "venues": venues,
        "years": years,
        "include_arxiv": bool(selection.get("include_arxiv")),
        "include_huggingface": bool(selection.get("include_huggingface")),
        "include_github": bool(selection.get("include_github")),
        "arxiv_window_days": window_days,
        "venue_title_scan_limit": _env_int("VENUE_TITLE_SCAN_LIMIT", 12000 if deep else 3000),
        "find_recall_count": _env_int("FIND_RECALL_COUNT", 3000 if deep else 200),
        "detail_fetch_count": _env_int("DETAIL_FETCH_COUNT", 800 if deep else 50),
        "abstract_scoring_max_workers": _env_int("ABSTRACT_SCORING_MAX_WORKERS", 6 if deep else 4),
        "abstract_scoring_batch_size": _env_int("ABSTRACT_SCORING_BATCH_SIZE", 10 if deep else 6),
        "max_papers": int(args.max_papers),
        "max_ideas": int(args.max_ideas),
        "provider": provider,
        "model": model,
        "api_mode": os.environ.get("LLM_API_MODE", "chat_completions"),
        "api_base": api_base,
        "api_key_env": api_key_env,
        "llm_config_source": "modules/finding/config/llm.local.json_or_env",
        "llm_key_available": bool(api_key),
        "llm_title_filter": "forced" if os.environ.get("FORCE_LLM_TITLE_FILTER", "0").lower() in {"1", "true", "yes", "on"} else "disabled_for_large_pools",
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload["signature"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return payload


def _copy_cached_taste_outputs(project: str, run_dir_path: Path) -> dict:
    paths = build_paths(project)
    out_dir = paths.planning / "finding"
    out_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for name in [
        "find.md", "source_status.md", "hf.md", "github.md",
        "find_results.json", "find_progress.json", "manifest.json", "selection.json",
        "venue_health_report.json", "category_scan_report.json", "title_filter_report.json",
        "arxiv_raw.json", "arxiv_prefiltered.json", "biorxiv_raw.json", "biorxiv_prefiltered.json",
        "nature_raw.json", "nature_prefiltered.json", "science_raw.json", "science_prefiltered.json",
    ]:
        source = run_dir_path / name
        if source.exists():
            shutil.copyfile(source, out_dir / name)
            copied.append(name)
    return {"output_dir": str(out_dir), "copied": copied}


def maybe_reuse_taste_run(args: argparse.Namespace, extra_queries: list[str]) -> dict | None:
    if os.environ.get("ALLOW_FIND_RUN_REUSE", "0").lower() not in {"1", "true", "yes", "on"}:
        return None
    if os.environ.get("FORCE_REFRESH", "0").lower() in {"1", "true", "yes", "on"}:
        return None
    paths = build_paths(args.project)
    reuse_ttl_hours = _env_int("REUSE_TTL_HOURS", 72)
    expected = _taste_signature(args, extra_queries)
    state_path = paths.state / "taste_reuse_signature.json"
    state = {}
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    except Exception:
        state = {}
    run_dir_text = str(state.get("run_dir") or "")
    run_dir_path = Path(run_dir_text)
    generated_at = str(state.get("generated_at") or "")
    age_ok = True
    if generated_at:
        try:
            created = dt.datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
            age_ok = (dt.datetime.now(dt.timezone.utc) - created).total_seconds() <= reuse_ttl_hours * 3600
        except Exception:
            age_ok = False
    required = ["find_results.json", "find.md"]
    if (
        state.get("signature") == expected.get("signature")
        and age_ok
        and run_dir_path.exists()
        and all((run_dir_path / name).exists() for name in required)
    ):
        copied = _copy_cached_taste_outputs(args.project, run_dir_path)
        payload = {
            "project": args.project,
            "status": "reused",
            "stage": "reused_cached_taste_run",
            "taste_run_id": state.get("run_id"),
            "taste_run_dir": str(run_dir_path),
            "signature": expected,
            "reuse_ttl_hours": reuse_ttl_hours,
            "copied": copied.get("copied", []),
            "reason": "Same literature channels, direction, years/window, budgets, and LLM config; reused TASTE intermediate artifacts instead of rerunning discovery.",
        }
        (paths.state / "finding_frontend.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        (paths.planning / "finding_frontend.md").write_text(
            "# Find Frontend\n\n"
            "- status: reused\n"
            f"- taste_run_id: {payload['taste_run_id']}\n"
            f"- taste_run_dir: {payload['taste_run_dir']}\n"
            "- reason: same survey signature; reused cached TASTE artifacts.\n"
            "\nTargeted or different-direction literature refreshes change the signature and will run TASTE again.\n",
            encoding="utf-8",
        )
        print("Reused cached TASTE run " + str(payload["taste_run_id"]), flush=True)
        print(json.dumps(payload, ensure_ascii=False), flush=True)
        return payload
    return None


def save_taste_reuse_signature(project: str, run_id: str, run_dir_path: Path, args: argparse.Namespace, extra_queries: list[str]) -> None:
    paths = build_paths(project)
    signature = _taste_signature(args, extra_queries)
    payload = {
        **signature,
        "run_id": run_id,
        "run_dir": str(run_dir_path),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "required_files": ["find_results.json", "find.md"],
    }
    (paths.state / "taste_reuse_signature.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")




def latest_taste_run_hint() -> dict:
    runtime_root = Path(os.environ.get("FINDING_RUNTIME_DIR") or ROOT / "modules" / "finding" / ".runtime").expanduser()
    runs = sorted((runtime_root / "runs").glob("find_*"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    if not runs:
        return {}
    latest = runs[0]
    files = sorted(child.name for child in latest.iterdir() if child.is_file())
    return {"latest_run_dir": str(latest), "latest_run_files": files}





def write_find_timeout_state(
    project: str,
    *,
    status: str,
    reason: str,
    log_path: Path,
    timeout_sec: int,
    elapsed_sec: float,
    run_dir: Path | None = None,
    output_dir: Path | None = None,
) -> dict:
    paths = build_paths(project)
    out_dir = output_dir or paths.planning / "finding"
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "project": project,
        "status": status,
        "stage": status,
        "timeout_sec": timeout_sec,
        "elapsed_sec": round(elapsed_sec, 3),
        "reason": reason,
        "log_path": str(log_path),
        "taste_run_dir": str(run_dir) if run_dir else "",
        "output_dir": str(out_dir),
        "guardrail": "Find timeout records must not create fallback papers, readings, ideas, or plans. Rerun Find or rebuild downstream via reading --action current_find_research_plan only after real Find artifacts exist.",
    }
    state_path = paths.state / "finding_frontend.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_lines = [
        "# Find Frontend",
        "",
        f"- status: {status}",
        f"- timeout_sec: {timeout_sec}",
        f"- elapsed_sec: {elapsed_sec:.3f}",
        f"- output_dir: {out_dir}",
        f"- log_path: {log_path}",
        "",
        "No fallback scientific artifacts were generated. Use real `find_results.json` plus `reading --action current_find_research_plan` before downstream stages.",
    ]
    (paths.planning / "finding_frontend.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    (out_dir / "finding_frontend_timeout.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return payload

def write_timeout_state(project: str, timeout_sec: int, elapsed_sec: float, log_path: Path) -> None:
    paths = build_paths(project)
    payload = {
        "project": project,
        "status": "timeout",
        "timeout_sec": timeout_sec,
        "elapsed_sec": round(elapsed_sec, 3),
        "log_path": str(log_path),
        "recommendation": "Retry Find with narrower budgets or repair source/LLM access. Do not synthesize fallback papers, ideas, or plans.",
        "partial_run_hint": latest_taste_run_hint(),
    }
    (paths.state / "finding_frontend.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (paths.planning / "finding_frontend.md").write_text(
        "# Find Frontend\n\n"
        "- status: timeout\n"
        f"- timeout_sec: {timeout_sec}\n"
        f"- elapsed_sec: {elapsed_sec:.3f}\n"
        "\nFind did not finish inside the configured budget. Do not create fallback literature artifacts; retry with narrower feedback or repair the failing source/LLM path.\n",
        encoding="utf-8",
    )

def main() -> int:
    parser = argparse.ArgumentParser(description="Run the configured Find route and sync real Find artifacts into the project.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--env-name", default=DEFAULT_ENV)
    parser.add_argument("--max-papers", type=int, default=20)
    parser.add_argument("--max-ideas", type=int, default=6)
    parser.add_argument("--repair-rounds", type=int, default=2)
    parser.add_argument("--skip-arxiv", action="store_true")
    parser.add_argument("--skip-huggingface", action="store_true")
    parser.add_argument("--skip-github", action="store_true")
    parser.add_argument("--skip-venues", action="store_true", help="Skip slow venue title-index sources and use arXiv/HF/GitHub only.")
    parser.add_argument("--timeout-sec", type=int, default=int(os.environ.get("TIMEOUT_SEC", "3600")))
    parser.add_argument("--fast-mode", action="store_true", help="Use conservative budgets and skip slower external sources so initialization cannot dominate the loop.")
    parser.add_argument("--deep-survey", action="store_true", help="Use TASTE focused deep survey mode: full venue-corpus audit, category prefiltering, and screened-candidate LLM scoring.")
    parser.add_argument("--wide-survey", action="store_true", help="Allow broader venue/year scope. Default follows the project canonical source selection.")
    parser.add_argument("--query", action="append", default=[], help="Targeted query supplied by project agent; appended to project literature queries.")
    parser.add_argument("--focus-file", default="", help="Optional JSON/Markdown/TXT file with targeted queries or paper titles.")
    parser.add_argument("--internal-output-dir", default="", help="Run Find into this internal directory without publishing to the web-facing project artifacts.")
    args = parser.parse_args()
    if os.environ.get("DISABLE_NEW_FIND", "0").lower() in {"1", "true", "yes", "on"}:
        paths = build_paths(args.project)
        existing_find = paths.planning / "finding" / "find_results.json"
        payload = {
            "project": args.project,
            "status": "existing_find_reused_record_only",
            "stage": "existing_find_reused_record_only",
            "existing_find_results": str(existing_find),
            "existing_find_available": existing_find.exists(),
            "guardrail": "Record-only/existing-literature mode was explicitly requested; this invocation reuses canonical project literature artifacts instead of launching TASTE.",
        }
        try:
            data = json.loads(existing_find.read_text(encoding="utf-8")) if existing_find.exists() else {}
            if isinstance(data, dict):
                payload["taste_run_id"] = data.get("run_id", "")
        except Exception as exc:
            payload["existing_find_error"] = str(exc)[:300]
        (paths.state / "finding_frontend.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        (paths.planning / "finding_frontend.md").write_text(
            "# Find Frontend\n\n"
            "- status: existing_find_reused_record_only\n"
            f"- existing_find_results: {existing_find}\n"
            "- guardrail: existing-literature mode was explicitly requested; no TASTE run was launched by this invocation.\n",
            encoding="utf-8",
        )
        print(json.dumps(payload, ensure_ascii=False), flush=True)
        return 0
    extra_queries = merge_extra_queries(args)
    internal_output_dir = Path(args.internal_output_dir).expanduser() if str(args.internal_output_dir or "").strip() else None
    if internal_output_dir is not None:
        internal_output_dir.mkdir(parents=True, exist_ok=True)

    if args.deep_survey:
        if os.environ.get("REFRESH_LOCAL_DB", "0").lower() in {"1", "true", "yes", "on"}:
            refresh_cmd = [
                sys.executable,
                str(ROOT / "modules" / "finding" / "main.py"),
                "--action",
                "local_database",
                "--if-missing",
            ]
            years = os.environ.get("YEARS", "").strip()
            if years:
                refresh_cmd.extend(["--years", years])
            venues = os.environ.get("LOCAL_DB_VENUES", "").strip()
            if venues:
                refresh_cmd.extend(["--venues", venues])
            subprocess.run(refresh_cmd, cwd=ROOT, text=True, capture_output=True, timeout=min(args.timeout_sec, int(os.environ.get("DB_UPDATE_TIMEOUT_SEC", "1800")) + 60))

    if args.fast_mode:
        args.max_papers = min(args.max_papers, 3)
        args.max_ideas = min(args.max_ideas, 2)
        args.repair_rounds = min(args.repair_rounds, 1)
        args.skip_huggingface = True
        args.skip_github = True
        args.skip_venues = True

    if not (ROOT / "framework" / "scripts" / "auto_research").exists():
        print(f"missing framework root: {ROOT / 'framework' / 'scripts' / 'auto_research'}", file=sys.stderr)
        return 2
    source_selection = _effective_source_selection(args)
    reuse_payload = None if internal_output_dir is not None else maybe_reuse_taste_run(args, extra_queries)
    if reuse_payload:
        return 0
    paths = build_paths(args.project)
    cfg = _load_project_config(args.project)
    run_token = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S_%f") + f"_{os.getpid()}"
    tmp_dir = paths.root / "tmp" / "finding" / run_token
    tmp_dir.mkdir(parents=True, exist_ok=True)
    driver = tmp_dir / "run_driver.py"
    write_driver(
        driver,
        args.project,
        args.max_papers,
        args.max_ideas,
        args.repair_rounds,
        bool(source_selection.get("include_arxiv")),
        bool(source_selection.get("include_huggingface")),
        bool(source_selection.get("include_github")),
        bool(source_selection.get("venue_ids")),
        source_selection,
        deep_survey=bool(args.deep_survey),
        fast_mode=bool(args.fast_mode),
    )
    if extra_queries:
        targeted_path = (internal_output_dir / "taste_targeted_queries.json") if internal_output_dir is not None else paths.state / "taste_targeted_queries.json"
        try:
            existing_targeted = json.loads(targeted_path.read_text(encoding="utf-8")) if targeted_path.exists() else {}
        except Exception:
            existing_targeted = {}
        if not isinstance(existing_targeted, dict):
            existing_targeted = {}
        existing_targeted.update({"project": args.project, "queries": extra_queries, "updated_by": "run_frontend"})
        targeted_path.write_text(json.dumps(existing_targeted, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log_path = (internal_output_dir / "finding_frontend.log") if internal_output_dir is not None else paths.logs / "finding_frontend.log"
    start = time.time()
    run_env = os.environ.copy()
    run_env["WORKFLOW_RUNTIME_DIR"] = run_env.get("FINDING_RUNTIME_DIR") or str(ROOT / "modules" / "finding" / ".runtime")
    run_env["TASTE_FIND_INPUT_DIR"] = str(tmp_dir / "input")
    if internal_output_dir is not None:
        run_env["TASTE_INTERNAL_FIND_OUTPUT_DIR"] = str(internal_output_dir)
    local_llm_path = _local_llm_config_path()
    if not run_env.get("FINDING_LLM_CONFIG") and local_llm_path.exists():
        run_env["FINDING_LLM_CONFIG"] = str(local_llm_path)
    local_llm = _load_local_llm_config()
    api_key_env = run_env.get("LLM_API_KEY_ENV") or "OPENAI_API_KEY"
    api_key = (run_env.get(api_key_env, "") if api_key_env else "") or run_env.get("LLM_API_KEY", "") or str(local_llm.get("api_key") or "")
    if api_key_env:
        run_env["LLM_API_KEY_ENV"] = str(api_key_env)
    for env_key, local_key in [("LLM_API_BASE", "base_url"), ("LLM_MODEL", "model"), ("LLM_PROVIDER", "provider")]:
        if not run_env.get(env_key) and local_llm.get(local_key):
            run_env[env_key] = str(local_llm.get(local_key))
    try:
        driver_cmd = driver_python_command(args, cfg, driver)
    except RuntimeError as exc:
        log_path.write_text(str(exc) + "\n", encoding="utf-8")
        print(str(exc), file=sys.stderr)
        return 2
    try:
        proc = run(driver_cmd, cwd=ROOT, env=run_env, timeout_sec=args.timeout_sec, live_log_path=log_path)
    except subprocess.TimeoutExpired as exc:
        elapsed = time.time() - start
        stdout = redact(exc.stdout or "") if isinstance(exc.stdout, str) else ""
        stderr = redact(exc.stderr or "") if isinstance(exc.stderr, str) else ""
        log_path.write_text(stdout + "\n--- STDERR ---\n" + stderr + f"\n--- TIMEOUT ---\ntimeout_sec={args.timeout_sec}\nelapsed_sec={elapsed:.3f}\n", encoding="utf-8")
        try:
            driver.unlink()
        except FileNotFoundError:
            pass
        fallback_reason = f"Find timed out after {args.timeout_sec}s before producing a complete usable result."
        if internal_output_dir is not None:
            timeout_payload = {
                "project": args.project,
                "status": "internal_find_timeout",
                "stage": "internal_find_timeout",
                "timeout_sec": args.timeout_sec,
                "elapsed_sec": round(elapsed, 3),
                "output_dir": str(internal_output_dir),
                "reason": fallback_reason,
                "web_visible": False,
                "internal_literature_survey": True,
                "log_path": str(log_path),
            }
            (internal_output_dir / "finding_frontend.json").write_text(json.dumps(timeout_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            (internal_output_dir / "finding_frontend.md").write_text("# Internal literature survey\n\n- status: internal_find_timeout\n- web_visible: False\n", encoding="utf-8")
            print(json.dumps(timeout_payload, ensure_ascii=False))
            return 124
        run_hint = latest_taste_run_hint()
        latest_dir = Path(run_hint.get("latest_run_dir", "")) if run_hint else Path("")
        if latest_dir.exists() and (latest_dir / "find_results.json").exists():
            out_dir = paths.planning / "finding"
            out_dir.mkdir(parents=True, exist_ok=True)
            for name in ["find.md", "find_results.json", "selection.json", "source_status.md", "venue_health_report.json", "category_scan_report.json", "title_filter_report.json", "arxiv_raw.json", "arxiv_prefiltered.json", "hf.md", "github.md"]:
                source = latest_dir / name
                if source.exists():
                    shutil.copyfile(source, out_dir / name)
            payload = write_find_timeout_state(
                args.project,
                status="find_artifacts_copied_after_timeout",
                reason=fallback_reason,
                log_path=log_path,
                timeout_sec=args.timeout_sec,
                elapsed_sec=elapsed,
                run_dir=latest_dir,
                output_dir=out_dir,
            )
            print(json.dumps(payload, ensure_ascii=False))
            print(f"native frontend timed out after {args.timeout_sec}s; copied real Find artifacts only.", file=sys.stderr)
            return 0
        else:
            payload = write_find_timeout_state(
                args.project,
                status="blocked_find_timeout_no_usable_artifacts",
                reason=fallback_reason,
                log_path=log_path,
                timeout_sec=args.timeout_sec,
                elapsed_sec=elapsed,
                run_dir=None,
                output_dir=paths.planning / "finding",
            )
            print(json.dumps(payload, ensure_ascii=False))
            print(f"native frontend timed out after {args.timeout_sec}s before usable Find; no fallback artifacts were written.", file=sys.stderr)
            return 124
    log_path.write_text(redact(proc.stdout) + "\n--- STDERR ---\n" + redact(proc.stderr), encoding="utf-8")
    try:
        driver.unlink()
    except FileNotFoundError:
        pass
    try:
        hint = latest_taste_run_hint()
        run_dir = Path(str(hint.get("latest_run_dir") or ""))
        if os.environ.get("ALLOW_FIND_RUN_REUSE", "0").lower() in {"1", "true", "yes", "on"} and internal_output_dir is None and proc.returncode == 0 and run_dir.exists() and (run_dir / "find_results.json").exists():
            run_id = run_dir.name
            save_taste_reuse_signature(args.project, run_id, run_dir, args, extra_queries)
    except Exception as exc:
        print(f"TASTE reuse signature save failed: {exc}", file=sys.stderr)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
