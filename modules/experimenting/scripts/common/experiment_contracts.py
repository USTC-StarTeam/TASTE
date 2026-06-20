#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Iterable

SUPPORTIVE_CLAIM_VERDICTS = {"supported", "partial", "promising"}
WEAK_CLAIM_VERDICTS = {"unsupported", "weakening"}
PRUNE_RECOMMENDATIONS = {"compare_then_prune_or_pause", "pause_or_prune"}


def load_json(path: Path, default=None):
    if default is None:
        default = {}
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def experiment_rows(payload) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("experiments"), list):
        return [row for row in payload.get("experiments", []) if isinstance(row, dict)]
    return []


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def parse_float(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else None


def metric_higher_is_better(metric_name: str) -> bool:
    lowered = (metric_name or "").lower()
    worse_if_high = ("loss", "error", "wer", "cer", "rmse", "mae", "latency", "time", "perplexity")
    return not any(token in lowered for token in worse_if_high)


def completed_status(row: dict) -> bool:
    return str(row.get("status", "")).lower() in {"completed", "success"}


def real_dataset_row(row: dict, ready_real_datasets: Iterable[str] | None = None) -> bool:
    dataset = str(row.get("dataset", "")).strip()
    if not dataset or dataset.startswith("synthetic"):
        return False
    ready = {str(item).strip() for item in ready_real_datasets or [] if str(item).strip()}
    return (not ready) or dataset in ready


ROLE_FIELDS = ("comparison_role", "method_role", "claim_role", "experiment_role", "role")
CONTROL_ROLES = {"baseline", "control", "ablation", "reference", "reproduction", "comparator"}
CANDIDATE_ROLES = {"candidate", "proposed", "variant", "intervention", "treatment", "ours"}
GENERIC_CONTROL_TOKENS = ("baseline", "control", "ablation", "reference", "reproduction")
GENERIC_CANDIDATE_TOKENS = (
    "proposed", "candidate", "variant", "ours", "intervention", "treatment",
    "rerank", "reranking", "finetune", "fusion", "conditioning",
)
NON_PROMOTABLE_PROMOTION_STATUSES = {
    "candidate_observation_only",
    "observation_only",
    "exploratory_only",
    "not_promotable",
    "do_not_promote",
    "blocked",
    "hold",
    "hold_markdown_only",
}
INCOMPARABLE_COMPARISON_STATUSES = {
    "not_comparable",
    "incomparable",
    "comparison_blocked",
    "baseline_missing",
    "baseline_incomplete",
    "baseline_crashed",
    "no_comparable_baseline",
}
FAILED_BASELINE_STATUSES = {
    "crashed",
    "crashed_or_incomplete",
    "failed",
    "incomplete",
    "incomplete_audit",
    "error",
    "timeout",
    "killed",
    "not_completed",
    "no_complete_baseline",
}
NON_PROMOTABLE_EVIDENCE_STATUSES = {
    "candidate_observation_only",
    "observation_only",
    "not_audit_ready",
    "incomplete_audit",
    "incomplete_running",
    "running",
    "pending",
    "blocked",
    "not_promotable",
}


def _truthy(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _policy_lists(role_policy: dict | None) -> tuple[set[str], set[str], dict[str, str]]:
    policy = role_policy or {}
    control_values = set(policy.get("control_methods") or policy.get("baseline_methods") or [])
    candidate_values = set(policy.get("candidate_methods") or policy.get("proposed_methods") or [])
    controls = {normalize_method_token(str(item)) for item in control_values if str(item).strip()}
    candidates = {normalize_method_token(str(item)) for item in candidate_values if str(item).strip()}
    explicit_roles: dict[str, str] = {}
    for key in ("method_roles", "role_by_method", "method_role_by_slug"):
        mapping = policy.get(key) if isinstance(policy.get(key), dict) else {}
        for name, role in mapping.items():
            token = normalize_method_token(str(name))
            if token:
                explicit_roles[token] = normalize_role(role)
    return controls, candidates, explicit_roles


def normalize_role(value) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")


def row_method_tokens(row_or_name) -> set[str]:
    if isinstance(row_or_name, dict):
        values = [row_or_name.get(key, "") for key in ("method", "method_slug", "experiment_id", "name")]
    else:
        values = [row_or_name]
    tokens: set[str] = set()
    for value in values:
        if value is None:
            continue
        raw = str(value).strip()
        norm = normalize_method_token(raw)
        if raw:
            tokens.add(raw)
        if norm:
            tokens.add(norm)
    return tokens


def row_method_role(row_or_name, role_policy: dict | None = None) -> str:
    """Classify experiment role without topic-specific name assumptions.

    New experiment rows should carry comparison_role/method_role metadata.
    Project configs may define compatibility mappings for old rows. Only the
    final fallback uses generic research-method words such as baseline/control
    or proposed/candidate; topic tags, method names, and dataset names are never
    treated as universal role signals here.
    """
    if isinstance(row_or_name, dict):
        for flag, role in (("is_baseline", "baseline"), ("is_control", "control"), ("is_candidate", "candidate")):
            if _truthy(row_or_name.get(flag)):
                return role
        for field in ROLE_FIELDS:
            role = normalize_role(row_or_name.get(field))
            if role:
                return role

    tokens = {normalize_method_token(token) for token in row_method_tokens(row_or_name)}
    controls, candidates, explicit_roles = _policy_lists(role_policy)
    for token in tokens:
        if token in explicit_roles:
            return explicit_roles[token]
    if tokens & controls:
        return "control"
    if tokens & candidates:
        return "candidate"

    fallback_enabled = True
    if isinstance(role_policy, dict) and "generic_name_fallback" in role_policy:
        fallback_enabled = _truthy(role_policy.get("generic_name_fallback"))
    if fallback_enabled:
        joined = " ".join(tokens)
        if any(token in joined for token in GENERIC_CONTROL_TOKENS):
            return "control"
        if any(token in joined for token in GENERIC_CANDIDATE_TOKENS):
            return "candidate"
    return "unknown"


def method_is_baseline_or_control(row_or_name, role_policy: dict | None = None) -> bool:
    return row_method_role(row_or_name, role_policy) in CONTROL_ROLES


def method_is_candidate(row_or_name, role_policy: dict | None = None) -> bool:
    return row_method_role(row_or_name, role_policy) in CANDIDATE_ROLES


def row_method_name(row: dict) -> str:
    return str(row.get("method") or row.get("method_slug") or row.get("experiment_id") or row.get("name") or "")


def row_metric(row: dict) -> tuple[str, float | None]:
    metric_name = str(row.get("metric_name") or row.get("metric") or "").strip()
    value = parse_float(row.get("metric_value"))
    if value is None:
        value = parse_float(row.get("final_metric_value"))
    if value is None and metric_name and isinstance(row.get("metrics"), dict):
        value = parse_float(row.get("metrics", {}).get(metric_name))
    return metric_name, value


def better_metric(candidate: float, control: float, metric_name: str, margin: float) -> bool:
    if metric_higher_is_better(metric_name):
        return candidate > control * (1.0 + margin)
    return candidate < control * (1.0 - margin)


def _row_time_key(row: dict) -> str:
    for key in ("finished_at", "completed_at", "timestamp", "generated_at", "updated_at", "started_at"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def best_metric_row(rows: Iterable[dict], metric_name: str) -> dict | None:
    scored = []
    for row in rows:
        row_metric_name, value = row_metric(row)
        if value is None:
            continue
        if metric_name and row_metric_name and row_metric_name != metric_name:
            continue
        scored.append((float(value), row))
    if not scored:
        return None
    if metric_higher_is_better(metric_name):
        best_value = max(value for value, _ in scored)
    else:
        best_value = min(value for value, _ in scored)
    tied = [row for value, row in scored if value == best_value]
    return sorted(tied, key=_row_time_key, reverse=True)[0]



PROMOTABLE_CLAIM_STATUSES = {"supported", "partially_supported", "partial", "promising"}
LLM_SEMANTIC_MARKERS = (
    "llm", "large language", "language model", "openai-compatible", "openai",
    "text embedding", "api embedding", "semantic embedding", "semantic conditioning",
    "tenc_llm", "tencllm", "finetune_llm", "sem_emb_path",
)
CLUSTER_ONLY_SEMANTIC_MARKERS = (
    "minibatchkmeans", "kmeans", "k-means", "cluster centroid", "cluster-centroid",
    "cluster centroids", "semantic cluster", "clustering-based", "cluster-based",
    "semantic-buffer", "semantic buffer",
)
REAL_LLM_EMBEDDING_MARKERS = (
    "openai-compatible", "openai", "api text embedding", "api embedding", "text embedding",
    "item text", "catalog text", "metadata text", "sentence-transformer", "sentence transformer",
    "large language model embedding", "llm text embedding", "real llm embedding",
)
PROJECT_LLM_REQUIREMENT_MARKERS = (
    "llm", "large language", "language model", "大模型", "语言模型",
    "text embedding", "文本嵌入", "语义", "semantic",
)
NEGATED_REAL_LLM_EVIDENCE_MARKERS = (
    "not real llm", "not real source text", "not llm", "not llm-based", "not llm based",
    "no item text", "no source text", "no item title/description",
    "anonymous integer item ids", "only anonymous",
    "pseudo source text", "pseudo-text", "pseudotext", "pseudo description",
    "synthetic description", "generated description", "generated item description",
    "generate item descriptions", "descriptions from co-occurrence", "frequently interacted",
    "co-occurrence", "cooccurrence", "co occurrence", "interaction-derived",
    "interaction derived", "collaborative pseudo", "id-derived", "id derived",
    "不是 llm", "不是真实 llm", "非 llm", "没有文本", "无文本",
    "伪文本", "共现", "交互派生", "匿名 id",
)
PSEUDO_TEXT_DERIVATION_MARKERS = (
    "pseudo source text", "pseudo-text", "pseudotext",
    "pseudo description", "synthetic description", "generated description",
    "generated item description", "generate item descriptions",
    "descriptions from co-occurrence", "frequently interacted",
    "co-occurrence", "cooccurrence", "co occurrence", "interaction-derived",
    "interaction derived", "collaborative pseudo", "id-derived", "id derived",
    "伪文本", "共现", "交互派生",
)
SOURCE_UNAVAILABLE_EVIDENCE_PATTERNS = (
    r"real[_\s-]*[a-z0-9_\s-]{0,40}metadata[\"']?\s*[:=]\s*(?:false|0|no|none|null)",
    r"source[_\s-]*(?:text|metadata)[\"']?\s*[:=]\s*(?:false|0|no|none|null)",
    r"(?:text|metadata)[_\s-]*(?:available|present|ready)[\"']?\s*[:=]\s*(?:false|0|no|none|null)",
    r"(?:anonymous|integer|id[-_\s]*only).{0,40}(?:items?|records?|identifiers?)",
)
PASS_LIKE_EMBEDDING_STATUSES = {
    "pass", "passed", "success", "completed", "ready", "probe_passed", "smoke_passed",
    "real_llm_embedding_passed", "llm_embedding_probe_passed",
}


def _lower_blob(*values) -> str:
    return " ".join(str(value or "") for value in values).lower()


def _row_text(row: dict) -> str:
    fields = [
        "experiment_id", "name", "method", "method_slug", "claim_verdict", "novelty_note",
        "counterexample_outcome", "notes", "summary", "artifact_semantics",
        "semantic_embedding_evidence", "embedding_source", "semantic_embedding_source",
        "llm_embedding_source", "sem_emb_path", "baseline_status", "comparison_status",
        "comparability_status", "promotion_status", "evidence_status", "audit_status", "log_path",
        "embedding_method", "inference_method", "model_checkpoint", "semantic_embeddings",
    ]
    blob = _lower_blob(*(row.get(field) for field in fields))
    audit_path = str(row.get("audit_path") or "").strip()
    if audit_path:
        try:
            path = Path(audit_path)
            if path.exists() and path.is_file() and path.suffix.lower() == ".json":
                payload = json.loads(path.read_text(encoding="utf-8"))
                blob = _lower_blob(blob, json.dumps(payload, ensure_ascii=False)[:50000])
        except Exception:
            pass
    return blob


def _claim_status_promotable(status) -> bool:
    lowered = str(status or "").strip().lower()
    return lowered in PROMOTABLE_CLAIM_STATUSES


def _row_id(row: dict) -> str:
    return str(row.get("experiment_id") or row.get("name") or row.get("id") or "unknown")


def _status_token(value) -> str:
    return normalize_role(value)


def _json_file_payload(path: Path) -> dict:
    try:
        if path.exists() and path.is_file() and path.suffix.lower() == ".json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}
    return {}


def _existing_artifact_file(row: dict, *names: str) -> str:
    candidates: list[Path] = []
    for key in ("artifact_path", "artifact_dir", "output_dir"):
        value = str(row.get(key) or "").strip()
        if value:
            root = Path(value)
            candidates.extend(root / name for name in names)
    audit_path = str(row.get("audit_path") or "").strip()
    if audit_path:
        parent = Path(audit_path).parent
        candidates.extend(parent / name for name in names)
        audit_payload = _json_file_payload(Path(audit_path))
        outputs = audit_payload.get("outputs") if isinstance(audit_payload.get("outputs"), dict) else {}
        for key in ("bad_case_slices", "bad_cases", "counterexample_outcomes", "counterexamples"):
            value = str(outputs.get(key) or "").strip()
            if value:
                candidates.append(Path(value))
    for candidate in candidates:
        try:
            if candidate.exists() and candidate.is_file() and candidate.stat().st_size > 2:
                return str(candidate)
        except Exception:
            continue
    return ""


def _row_has_bad_case_evidence(row: dict) -> bool:
    if row.get("bad_case_path") or row.get("bad_case_slices") or row.get("bad_cases"):
        return True
    audit_path = str(row.get("audit_path") or "").strip()
    audit_payload = _json_file_payload(Path(audit_path)) if audit_path else {}
    if audit_payload.get("has_bad_case_slices") or audit_payload.get("bad_case_slices") or audit_payload.get("bad_cases"):
        return True
    return bool(_existing_artifact_file(row, "bad_case_slices.json", "bad_cases.json", "bad_case_analysis.json"))


def _row_has_counterexample_evidence(row: dict) -> bool:
    if row.get("counterexample_outcome") or row.get("counterexample_outcomes") or row.get("counterexamples"):
        return True
    audit_path = str(row.get("audit_path") or "").strip()
    audit_payload = _json_file_payload(Path(audit_path)) if audit_path else {}
    if audit_payload.get("has_counterexample_outcomes") or audit_payload.get("counterexample_outcome") or audit_payload.get("counterexample_outcomes"):
        return True
    return bool(_existing_artifact_file(row, "counterexample_outcomes.json", "counterexamples.json", "counterexample_analysis.json"))


def row_promotion_blockers(row: dict) -> list[str]:
    """Return structured reasons why a row cannot support claim/paper promotion."""
    if not isinstance(row, dict):
        return ["invalid_experiment_row"]
    blockers: list[str] = []
    status = _status_token(row.get("status"))
    baseline_status = _status_token(row.get("baseline_status"))
    comparison_status = _status_token(row.get("comparison_status") or row.get("comparability_status"))
    promotion_status = _status_token(row.get("promotion_status"))
    evidence_status = _status_token(row.get("evidence_status") or row.get("audit_status"))
    if status and status not in {"completed", "success", "pass", "passed"}:
        blockers.append(f"status={status}")
    if baseline_status in FAILED_BASELINE_STATUSES:
        blockers.append(f"baseline_status={baseline_status}")
    if comparison_status in INCOMPARABLE_COMPARISON_STATUSES:
        blockers.append(f"comparison_status={comparison_status}")
    if promotion_status in NON_PROMOTABLE_PROMOTION_STATUSES:
        blockers.append(f"promotion_status={promotion_status}")
    if evidence_status in NON_PROMOTABLE_EVIDENCE_STATUSES:
        blockers.append(f"evidence_status={evidence_status}")
    text = _row_text(row)
    if "candidate_observation_only" in text or "observation only" in text:
        blockers.append("text_marks_candidate_observation_only")
    if "not comparable" in text or "not_comparable" in text or "incomparable" in text:
        blockers.append("text_marks_not_comparable")
    if "baseline" in text and any(marker in text for marker in ("crashed", "crash", "incomplete", "typeerror")):
        blockers.append("text_marks_failed_or_incomplete_baseline")
    if any(marker in text for marker in ("inference-only", "inference only", "post-hoc", "posthoc")):
        blockers.append("inference_only_or_posthoc_method")
    if _has_any(text, PSEUDO_TEXT_DERIVATION_MARKERS) or _has_any(text, NEGATED_REAL_LLM_EVIDENCE_MARKERS) or _has_unavailable_source_evidence(text):
        blockers.append("pseudo_or_interaction_derived_text_evidence")
    if not _row_has_bad_case_evidence(row):
        blockers.append("missing_bad_case_slices")
    if not _row_has_counterexample_evidence(row):
        blockers.append("missing_counterexample_outcome")
    if not row.get("claim_verdict"):
        blockers.append("missing_claim_verdict")
    return blockers


def row_promotable_for_claims(row: dict) -> bool:
    return (
        isinstance(row, dict)
        and completed_status(row)
        and bool(row.get("audit_ready"))
        and not row_promotion_blockers(row)
    )


def _has_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _has_unavailable_source_evidence(text: str) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in SOURCE_UNAVAILABLE_EVIDENCE_PATTERNS)


def _row_has_cluster_only_semantic_proxy(row: dict) -> bool:
    text = _row_text(row)
    return _has_any(text, LLM_SEMANTIC_MARKERS) and _has_any(text, CLUSTER_ONLY_SEMANTIC_MARKERS)


def _row_has_real_llm_text_evidence(row: dict) -> bool:
    text = _row_text(row)
    if _has_any(text, NEGATED_REAL_LLM_EVIDENCE_MARKERS) or _has_unavailable_source_evidence(text) or _has_any(text, CLUSTER_ONLY_SEMANTIC_MARKERS):
        return False
    return _has_any(text, REAL_LLM_EMBEDDING_MARKERS)


def _safe_json_load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def _pass_like_payload(data: dict) -> bool:
    if not isinstance(data, dict):
        return False
    status = str(data.get("status") or data.get("decision") or data.get("result") or "").strip().lower()
    if status in PASS_LIKE_EMBEDDING_STATUSES:
        return True
    return bool(data.get("success") or data.get("ok") or data.get("probe_success") or data.get("smoke_success"))


def _project_requires_llm_semantics(paths) -> bool:
    cfg = _safe_json_load(getattr(paths, "config", Path("")), {})
    if not isinstance(cfg, dict):
        return False
    fields = [
        cfg.get("topic"),
        cfg.get("title"),
        cfg.get("user_prompt"),
        cfg.get("research_interest"),
        cfg.get("researcher_profile"),
    ]
    for key in ("queries", "task_keywords"):
        value = cfg.get(key)
        if isinstance(value, list):
            fields.extend(value)
    return _has_any(_lower_blob(*fields), PROJECT_LLM_REQUIREMENT_MARKERS)


def _looks_like_real_llm_embedding_payload(data: dict) -> bool:
    if not isinstance(data, dict) or not _pass_like_payload(data):
        return False
    blob = json.dumps(data, ensure_ascii=False).lower()
    has_real_marker = _has_any(blob, REAL_LLM_EMBEDDING_MARKERS)
    negated_or_pseudo = _has_any(blob, NEGATED_REAL_LLM_EVIDENCE_MARKERS) or _has_unavailable_source_evidence(blob)
    cluster_only = _has_any(blob, CLUSTER_ONLY_SEMANTIC_MARKERS) and not any(
        marker in blob for marker in ("openai-compatible", "openai", "api text embedding", "llm text embedding", "real llm embedding")
    )
    return has_real_marker and not cluster_only and not negated_or_pseudo


def _artifact_json_files(path_text: str) -> list[Path]:
    if not path_text:
        return []
    path = Path(path_text)
    if path.is_file() and path.suffix.lower() == ".json":
        return [path]
    if not path.is_dir():
        return []
    try:
        return sorted([child for child in path.iterdir() if child.is_file() and child.suffix.lower() == ".json"])
    except Exception:
        return []


def _embedding_generator_scripts(active_repo_path: str) -> list[Path]:
    if not active_repo_path:
        return []
    root = Path(active_repo_path)
    if not root.exists():
        return []
    scripts: list[Path] = []
    patterns = ("*embedding*.py", "*semantic*.py", "*llm*.py")
    try:
        for pattern in patterns:
            scripts.extend(path for path in root.glob(pattern) if path.is_file())
    except Exception:
        return []
    seen: set[str] = set()
    unique: list[Path] = []
    for script in scripts:
        key = str(script)
        if key not in seen:
            unique.append(script)
            seen.add(key)
    return unique


def _known_embedding_probe_paths(paths, llm_rows: list[dict]) -> list[Path]:
    names = [
        "real_llm_embedding_probe.json",
        "real_llm_embedding_smoke.json",
        "llm_embedding_probe.json",
        "llm_embedding_smoke.json",
        "semantic_embedding_audit.json",
        "api_embedding_probe.json",
        "api_embedding_smoke.json",
    ]
    out = [paths.state / name for name in names]
    try:
        out.extend(sorted(paths.state.glob("*embedding*probe*.json")))
        out.extend(sorted(paths.state.glob("*embedding*smoke*.json")))
        out.extend(sorted(paths.state.glob("*semantic*audit*.json")))
    except Exception:
        pass
    for row in llm_rows:
        for json_path in _artifact_json_files(str(row.get("artifact_path") or "")):
            lower = json_path.name.lower()
            if any(token in lower for token in ("embedding", "semantic", "audit", "probe", "smoke", "metrics")):
                out.append(json_path)
    seen = set()
    unique = []
    for item in out:
        key = str(item)
        if key not in seen:
            unique.append(item)
            seen.add(key)
    return unique


def llm_semantic_promotion_guard(paths, experiments, active_repo_path: str = "", claim_ledger: dict | None = None) -> dict:
    """Block paper/claim promotion when LLM semantic evidence is only cluster-derived.

    Cluster centroids can be useful engineering probes, but they are not LLM/API
    text embeddings. This guard keeps TASTE from turning a cluster-only candidate
    into a paper-ready LLM semantic claim before a real text-embedding probe and
    artifact-local audit exist.
    """
    rows = experiment_rows(experiments)
    active_norm = str(active_repo_path or "").rstrip("/")
    if active_norm:
        rows = [row for row in rows if str(row.get("repo_path") or "").rstrip("/") == active_norm]
    completed = [row for row in rows if isinstance(row, dict) and completed_status(row)]
    llm_rows = [row for row in completed if _has_any(_row_text(row), LLM_SEMANTIC_MARKERS)]
    cluster_only_rows = [row for row in llm_rows if _row_has_cluster_only_semantic_proxy(row)]
    missing_artifact_audit = []
    for row in llm_rows:
        text = _row_text(row)
        if not (_has_any(text, CLUSTER_ONLY_SEMANTIC_MARKERS) or "llm_candidate" in text or "finetune_llm" in text):
            continue
        artifact_path = str(row.get("artifact_path") or "")
        if artifact_path and Path(artifact_path).is_dir() and not _artifact_json_files(artifact_path):
            missing_artifact_audit.append(str(row.get("experiment_id") or row.get("name") or artifact_path))

    probe_paths = _known_embedding_probe_paths(paths, llm_rows)
    real_probe_hits = []
    for probe_path in probe_paths:
        payload = _safe_json_load(probe_path, {})
        if _looks_like_real_llm_embedding_payload(payload):
            real_probe_hits.append(str(probe_path))
    has_real_llm_embedding_evidence = bool(real_probe_hits)

    embedding_generator_scripts = _embedding_generator_scripts(active_norm)
    generator_script = Path(active_norm) / "generate_semantic_embeddings.py" if active_norm else Path("")
    generator_cluster_only_scripts: list[str] = []
    generator_pseudo_text_scripts: list[str] = []
    generator_present = bool(embedding_generator_scripts) or (generator_script.exists() if active_norm else False)
    api_embedding_markers = ("openai-compatible", "openai", "requests.post", "chat.completions", "embeddings.create", "api text embedding")
    for script in embedding_generator_scripts or ([generator_script] if generator_script.exists() else []):
        try:
            src = script.read_text(encoding="utf-8", errors="ignore").lower()
        except Exception:
            continue
        if _has_any(src, CLUSTER_ONLY_SEMANTIC_MARKERS) and not any(marker in src for marker in api_embedding_markers):
            generator_cluster_only_scripts.append(str(script))
        if _has_any(src, PSEUDO_TEXT_DERIVATION_MARKERS):
            generator_pseudo_text_scripts.append(str(script))
    generator_cluster_only = bool(generator_cluster_only_scripts)

    queue = _safe_json_load(paths.state / "guidance_queue.json", [])
    items = queue if isinstance(queue, list) else queue.get("items", []) if isinstance(queue, dict) else []
    queued_embedding_guidance = [
        str(item.get("id") or "")
        for item in items
        if isinstance(item, dict)
        and str(item.get("status") or "").lower() not in {"consumed", "done", "completed"}
        and _has_any(str(item.get("message") or "").lower(), ("generate_semantic_embeddings.py", "minibatchkmeans", "api embedding", "llm embedding"))
    ]

    claims = claim_ledger.get("claims", []) if isinstance(claim_ledger, dict) and isinstance(claim_ledger.get("claims"), list) else []
    project_requires_llm_semantics = _project_requires_llm_semantics(paths)
    rows_by_id = {}
    for row in rows:
        for key in ("experiment_id", "name", "id"):
            value = str(row.get(key) or "").strip()
            if value:
                rows_by_id[value] = row
    non_promotable_claims = []
    llm_claims_supported_by_cluster = []
    promotable_llm_claims = []
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        claim_id = str(claim.get("claim_type") or claim.get("id") or "claim")
        status = str(claim.get("status") or "")
        if status and not _claim_status_promotable(status):
            non_promotable_claims.append(f"{claim_id}:{status}")
        claim_text = _lower_blob(claim.get("text"), claim.get("claim"), claim.get("summary"), claim.get("caveats"))
        if (
            _claim_status_promotable(status)
            and _has_any(claim_text, LLM_SEMANTIC_MARKERS)
            and not _has_any(claim_text, NEGATED_REAL_LLM_EVIDENCE_MARKERS)
        ):
            promotable_llm_claims.append(claim_id)
        support_ids = [str(item or "").strip() for item in (claim.get("supporting_runs") or claim.get("supported_by") or []) if str(item or "").strip()]
        supported_by_cluster = [rid for rid in support_ids if _row_has_cluster_only_semantic_proxy(rows_by_id.get(rid, {}))]
        if _has_any(claim_text, ("llm semantic", "llm-conditioned", "llm conditioning", "large language", "text embedding")) and supported_by_cluster:
            llm_claims_supported_by_cluster.append(f"{claim_id}:{','.join(supported_by_cluster[:4])}")

    issues = []
    # Infrastructure checks (experiment rows, generator script) only block when
    # there are actual LLM claims backed by cluster-only evidence. If claims have
    # been pruned/removed, remaining infrastructure is non-promoted research artifact.
    has_active_cluster_claims = bool(llm_claims_supported_by_cluster) or bool(non_promotable_claims)
    if project_requires_llm_semantics and not has_real_llm_embedding_evidence:
        if not promotable_llm_claims:
            issues.append(
                "Current project topic requires LLM/large-language semantic fusion, but the current claim ledger has no promotable LLM/text-semantic claim; pruning unsupported LLM claims does not clear the evidence gate."
            )
        if llm_rows:
            issues.append("No passed artifact-local real LLM/API text-embedding probe was found for the current selected-base route.")
        else:
            issues.append("No completed LLM/text-conditioned candidate run exists for the current selected-base route.")
        if cluster_only_rows:
            ids = [_row_id(row) for row in cluster_only_rows[:6]]
            issues.append("Current LLM/semantic candidate evidence is cluster-derived only and is not promotable as LLM evidence; runs=" + ", ".join(ids))
        if generator_cluster_only:
            issues.append("Embedding-related code paths still use or consume cluster/ID-derived semantic buffers rather than real item text/API embeddings: " + ", ".join(generator_cluster_only_scripts[:6]))
        if generator_pseudo_text_scripts:
            issues.append("Embedding generator scripts derive descriptions from interactions/co-occurrence or other pseudo-text, not real item metadata: " + ", ".join(generator_pseudo_text_scripts[:6]))
    if llm_rows and not has_real_llm_embedding_evidence and has_active_cluster_claims:
        issues.append("No passed artifact-local real LLM/API text-embedding probe was found for the current selected-base route.")
    if cluster_only_rows and not has_real_llm_embedding_evidence and has_active_cluster_claims:
        ids = [str(row.get("experiment_id") or row.get("name") or "unknown") for row in cluster_only_rows[:6]]
        issues.append("Current LLM/semantic candidate evidence is cluster-derived only; runs=" + ", ".join(ids))
    if missing_artifact_audit and not has_real_llm_embedding_evidence and has_active_cluster_claims:
        issues.append("LLM/semantic candidate artifact directories lack artifact-local JSON audit payloads: " + ", ".join(missing_artifact_audit[:6]))
    if generator_cluster_only and not has_real_llm_embedding_evidence and has_active_cluster_claims:
        issues.append("Embedding-related code paths still use or consume cluster/ID-derived embeddings rather than real text/API embeddings: " + ", ".join(generator_cluster_only_scripts[:6]))
    if generator_pseudo_text_scripts and not has_real_llm_embedding_evidence:
        issues.append("Embedding generator scripts use interaction-derived pseudo-text/co-occurrence descriptions; these cannot satisfy real LLM/text evidence: " + ", ".join(generator_pseudo_text_scripts[:6]))
    if queued_embedding_guidance and not has_real_llm_embedding_evidence and has_active_cluster_claims:
        issues.append("Pending human supervision safety checkpoint about real LLM embedding has not been consumed: " + ", ".join(queued_embedding_guidance[:6]))
    if llm_claims_supported_by_cluster and not has_real_llm_embedding_evidence:
        issues.append("Claim ledger contains LLM semantic claims backed by cluster-only runs: " + ", ".join(llm_claims_supported_by_cluster[:6]))
    if non_promotable_claims:
        issues.append("Claim ledger contains non-promotable current claims: " + ", ".join(non_promotable_claims[:8]))

    return {
        "status": "pass" if not issues else "blocked",
        "has_real_llm_embedding_evidence": has_real_llm_embedding_evidence,
        "real_llm_embedding_evidence": real_probe_hits,
        "llm_candidate_runs": [str(row.get("experiment_id") or row.get("name") or "unknown") for row in llm_rows],
        "cluster_only_runs": [str(row.get("experiment_id") or row.get("name") or "unknown") for row in cluster_only_rows],
        "missing_artifact_audit_runs": missing_artifact_audit,
        "generator_script": str(generator_script) if generator_script.exists() else "",
        "generator_scripts": [str(script) for script in embedding_generator_scripts],
        "generator_cluster_only": generator_cluster_only,
        "cluster_or_id_derived_embedding_code_paths": generator_cluster_only_scripts,
        "generator_pseudo_text_scripts": generator_pseudo_text_scripts,
        "queued_embedding_guidance": queued_embedding_guidance,
        "project_requires_llm_semantics": project_requires_llm_semantics,
        "promotable_llm_claims": promotable_llm_claims,
        "llm_claims_supported_by_cluster": llm_claims_supported_by_cluster,
        "non_promotable_claims": non_promotable_claims,
        "issues": issues,
    }


def scientific_progress_gate(
    experiments: list[dict],
    *,
    ready_real_datasets: Iterable[str] | None = None,
    active_repo_path: str = "",
    active_dataset: str = "",
    margin: float = 0.005,
    method_role_policy: dict | None = None,
) -> dict:
    """Require real-data, audit-ready candidate progress before paper promotion."""
    active_dataset_key = str(active_dataset or "").strip().lower()
    real_rows = [
        row for row in experiments
        if isinstance(row, dict)
        and completed_status(row)
        and real_dataset_row(row, ready_real_datasets)
        and (not active_repo_path or str(row.get("repo_path") or "") == active_repo_path)
        and (not active_dataset_key or str(row.get("dataset") or "").strip().lower() == active_dataset_key)
        and row_metric(row)[1] is not None
    ]
    audit_ready_real = [row for row in real_rows if row.get("audit_ready")]
    candidate_rows = [row for row in real_rows if method_is_candidate(row, method_role_policy)]
    candidate_audit_ready = [row for row in candidate_rows if row.get("audit_ready")]
    non_audit_ready_candidate_rows = [row for row in candidate_rows if not row.get("audit_ready")]
    status_non_promotable_candidate_rows = [row for row in candidate_audit_ready if row_promotion_blockers(row)]
    cluster_non_promotable_candidate_rows = [row for row in candidate_audit_ready if _row_has_cluster_only_semantic_proxy(row) and not _row_has_real_llm_text_evidence(row)]
    non_promotable_candidate_rows = []
    for row in non_audit_ready_candidate_rows + status_non_promotable_candidate_rows + cluster_non_promotable_candidate_rows:
        if row not in non_promotable_candidate_rows:
            non_promotable_candidate_rows.append(row)
    promotable_candidate_audit_ready = [row for row in candidate_audit_ready if row not in non_promotable_candidate_rows]
    control_rows = [row for row in real_rows if method_is_baseline_or_control(row, method_role_policy)]
    excluded_control_rows = [row for row in control_rows if row_promotion_blockers(row)]
    control_audit_ready = [row for row in control_rows if row.get("audit_ready") and row not in excluded_control_rows]

    metrics = sorted({row_metric(row)[0] for row in real_rows if row_metric(row)[0]})
    metric_name = metrics[0] if len(metrics) == 1 else (metrics[0] if metrics else "")
    best_candidate = best_metric_row(promotable_candidate_audit_ready, metric_name)
    best_control = best_metric_row(control_audit_ready, metric_name)
    best_audit_ready_control = best_metric_row(control_audit_ready, metric_name)
    blockers: list[str] = []

    if not real_rows:
        blockers.append("No completed real-data metric rows exist for the active repo.")
    if not candidate_audit_ready:
        if candidate_rows:
            blockers.append("No audit-ready real-data candidate/proposed-method run exists; non-audit-ready candidates: " + ", ".join(_row_id(row) for row in non_audit_ready_candidate_rows[:6]))
        else:
            blockers.append("No audit-ready real-data candidate/proposed-method run exists.")
    elif not promotable_candidate_audit_ready:
        blockers.append("No audit-ready promotable candidate/proposed-method run exists; non-promotable candidates: " + ", ".join(_row_id(row) for row in non_promotable_candidate_rows[:6]))
    elif status_non_promotable_candidate_rows:
        blockers.append(
            "Some candidate/proposed-method runs are excluded by evidence-contract status: "
            + "; ".join(f"{_row_id(row)} ({', '.join(row_promotion_blockers(row)[:3])})" for row in status_non_promotable_candidate_rows[:6])
        )
    if not control_rows:
        blockers.append("No comparable real-data baseline/control metric exists.")
    if control_rows and not best_audit_ready_control:
        blockers.append("No audit-ready comparable baseline/control run exists.")
    if excluded_control_rows:
        blockers.append(
            "Some baseline/control rows are excluded by evidence-contract status: "
            + "; ".join(f"{_row_id(row)} ({', '.join(row_promotion_blockers(row)[:3])})" for row in excluded_control_rows[:6])
        )
    if len(metrics) > 1:
        blockers.append(f"Real-data comparison mixes metrics ({', '.join(metrics)}); The workflow must compare on the same metric before paper promotion.")

    candidate_value = row_metric(best_candidate)[1] if best_candidate else None
    control_value = row_metric(best_control)[1] if best_control else None
    comparison_pass = False
    if best_candidate and best_control and candidate_value is not None and control_value is not None:
        comparison_pass = better_metric(candidate_value, control_value, metric_name, margin)
        if not comparison_pass:
            blockers.append(
                "Best audit-ready candidate does not beat the best comparable baseline/control "
                f"by the required margin {margin:.3g} on {metric_name or 'metric'} "
                f"(candidate={candidate_value}, control={control_value})."
            )

    return {
        "status": "pass" if not blockers and comparison_pass else "blocked",
        "margin": margin,
        "metric_name": metric_name,
        "ready_real_datasets": sorted({str(item) for item in ready_real_datasets or [] if str(item).strip()}),
        "real_metric_runs": len(real_rows),
        "audit_ready_real_metric_runs": len(audit_ready_real),
        "candidate_real_runs": len(candidate_rows),
        "candidate_audit_ready_runs": len(candidate_audit_ready),
        "promotable_candidate_audit_ready_runs": len(promotable_candidate_audit_ready),
        "non_promotable_candidate_runs": [_row_id(row) for row in non_promotable_candidate_rows],
        "non_promotable_candidate_reasons": {
            _row_id(row): ((["audit_ready_missing"] if not row.get("audit_ready") else []) + row_promotion_blockers(row))
            for row in non_promotable_candidate_rows
        },
        "control_real_runs": len(control_rows),
        "control_audit_ready_runs": len(control_audit_ready),
        "excluded_control_reasons": {_row_id(row): row_promotion_blockers(row) for row in excluded_control_rows},
        "method_role_policy_source": "experiment row metadata + project config + generic fallback",
        "unknown_role_runs": len([row for row in real_rows if row_method_role(row, method_role_policy) == "unknown"]),
        "best_candidate": summarize_experiment_row(best_candidate),
        "best_control": summarize_experiment_row(best_control),
        "best_audit_ready_control": summarize_experiment_row(best_audit_ready_control),
        "comparison_pass": comparison_pass,
        "blockers": blockers,
    }


def _reference_control_row(reference: dict) -> dict:
    metric_name = str(reference.get("metric_name") or reference.get("metric") or "ndcg_at_10")
    metric_value = reference.get("metric_value")
    metrics = reference.get("metrics") if isinstance(reference.get("metrics"), dict) else {}
    if metric_value is None and metric_name:
        metric_value = metrics.get(metric_name)
    row = {
        "experiment_id": reference.get("experiment_id") or reference.get("name") or "selected_base_reference_full",
        "name": reference.get("experiment_id") or reference.get("name") or "selected_base_reference_full",
        "method": reference.get("method") or "selected_base_reference",
        "method_slug": reference.get("method") or "selected_base_reference",
        "dataset": reference.get("dataset") or "",
        "metric_name": metric_name,
        "metric_value": metric_value,
        "metrics": {metric_name: metric_value} if metric_name and metric_value is not None else {},
        "comparison_role": "reference",
        "status": "completed",
        "audit_ready": bool(reference.get("audit_ready", True)),
        "artifact_path": reference.get("artifact_path") or reference.get("artifact_dir") or "",
        "audit_path": reference.get("audit_path") or reference.get("artifact_audit_path") or "",
        "artifact_audit_path": reference.get("artifact_audit_path") or reference.get("audit_path") or "",
        "repo_path": reference.get("repo_path") or "",
        "repo_name": reference.get("repo_name") or "",
        "mode": reference.get("mode") or "",
    }
    return {key: value for key, value in row.items() if value is not None and value != ""}


def _drop_control_missing_blockers(blockers: list) -> list:
    prefixes = (
        "No comparable real-data baseline/control metric exists.",
        "No audit-ready comparable baseline/control run exists.",
        "Some baseline/control rows are excluded by evidence-contract status:",
    )
    return [item for item in blockers if not any(str(item).startswith(prefix) for prefix in prefixes)]


def _reference_same_control(control: dict, reference: dict) -> bool:
    ref_id = str(reference.get("experiment_id") or reference.get("name") or "").strip()
    ctrl_id = str(control.get("experiment_id") or control.get("name") or "").strip()
    return bool(ref_id and ctrl_id and ref_id == ctrl_id) or (
        str(control.get("method") or "") == "selected_base_reference"
        and str(control.get("dataset") or "") == str(reference.get("dataset") or "")
        and control.get("metric_value") == reference.get("metric_value")
    )


def align_reference_best_control(progress_gate: dict, reproduction_gate: dict) -> dict:
    """Use the authoritative reference gate reproduction as selected-base control.

    The experiment registry may not contain a separate control row for the
    wrapper-managed selected-base reproduction. When the reference gate has
    already passed, scientific-progress reporting should still compare future
    candidates against that authoritative reproduction instead of claiming that
    no comparable control exists.
    """
    if not isinstance(progress_gate, dict) or not isinstance(reproduction_gate, dict):
        return progress_gate
    if str(reproduction_gate.get("status") or "").lower() != "pass":
        return progress_gate
    reference = reproduction_gate.get("best_reproduction") if isinstance(reproduction_gate.get("best_reproduction"), dict) else {}
    if not reference:
        return progress_gate
    reference_control = _reference_control_row(reference)
    if not reference_control.get("metric_value"):
        return progress_gate

    matched_existing = False
    for key in ["best_control", "best_audit_ready_control"]:
        control = progress_gate.get(key) if isinstance(progress_gate.get(key), dict) else {}
        if control and _reference_same_control(control, reference):
            matched_existing = True
            control.update({field: value for field, value in reference_control.items() if value is not None and value != ""})
            progress_gate[key] = control

    if not matched_existing:
        progress_gate["best_control"] = dict(reference_control)
        progress_gate["best_audit_ready_control"] = dict(reference_control)
        for count_key in ("control_real_runs", "control_audit_ready_runs"):
            try:
                current = int(progress_gate.get(count_key) or 0)
            except (TypeError, ValueError):
                current = 0
            progress_gate[count_key] = max(current, 1)
        blockers = progress_gate.get("blockers") if isinstance(progress_gate.get("blockers"), list) else []
        progress_gate["blockers"] = _drop_control_missing_blockers(blockers)
        progress_gate["excluded_control_reasons"] = {}

    candidate = progress_gate.get("best_candidate") if isinstance(progress_gate.get("best_candidate"), dict) else {}
    metric_name, candidate_value = row_metric(candidate) if candidate else ("", None)
    control_metric = str(reference_control.get("metric_name") or metric_name or "")
    control_value = parse_float(reference_control.get("metric_value"))
    if candidate and candidate_value is not None and control_value is not None:
        margin = parse_float(progress_gate.get("margin")) or 0.0
        comparison_pass = better_metric(candidate_value, control_value, metric_name or control_metric, margin)
        progress_gate["comparison_pass"] = comparison_pass
        blockers = progress_gate.get("blockers") if isinstance(progress_gate.get("blockers"), list) else []
        comparison_blocker_prefix = "Best audit-ready candidate does not beat the best comparable baseline/control"
        blockers = [item for item in blockers if not str(item).startswith(comparison_blocker_prefix)]
        if not comparison_pass:
            blockers.append(
                "Best audit-ready candidate does not beat the best comparable baseline/control "
                f"by the required margin {margin:.3g} on {(metric_name or control_metric) or 'metric'} "
                f"(candidate={candidate_value}, control={control_value})."
            )
        progress_gate["blockers"] = blockers
        progress_gate["status"] = "pass" if not blockers and comparison_pass else "blocked"
    elif progress_gate.get("status") == "pass":
        progress_gate["status"] = "blocked"
    return progress_gate


def summarize_experiment_row(row: dict | None) -> dict:
    if not isinstance(row, dict):
        return {}
    keys = [
        "timestamp",
        "experiment_id",
        "name",
        "method",
        "dataset",
        "metric_name",
        "metric_value",
        "final_metric_value",
        "audit_ready",
        "baseline_status",
        "comparison_status",
        "promotion_status",
        "evidence_status",
        "artifact_path",
        "audit_path",
    ]
    return {key: row.get(key) for key in keys if row.get(key) not in {None, ""}}


def normalize_method_token(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", (value or "").lower()).strip("_")
    parts = [part for part in text.split("_") if part]
    filtered = [part for part in parts if part not in {"trial", "exec", "run"}]
    return "_".join(filtered) or text


def row_matches_method(row: dict, method: dict) -> bool:
    row_tokens = set()
    for key in ("method", "method_slug", "experiment_id", "name"):
        value = row.get(key, "")
        if value:
            row_tokens.add(str(value))
            row_tokens.add(normalize_method_token(str(value)))
    method_tokens = set()
    for key in ("method", "method_slug"):
        value = method.get(key, "")
        if value:
            method_tokens.add(str(value))
            method_tokens.add(normalize_method_token(str(value)))
    for trial in method.get("trials", []):
        exp_id = trial.get("experiment_id", "")
        if exp_id:
            method_tokens.add(exp_id)
            method_tokens.add(normalize_method_token(exp_id))
    return bool(row_tokens & method_tokens)


def extract_bad_case_summary(path: str) -> dict:
    if not path:
        return {}
    target = Path(path)
    if not target.exists():
        return {"exists": False, "path": path, "count": 0, "examples": [], "slices": []}

    payload = {"exists": True, "path": str(target), "count": 0, "examples": [], "slices": []}
    try:
        suffix = target.suffix.lower()
        rows = []
        if suffix == ".json":
            data = json.loads(target.read_text(encoding="utf-8"))
            rows = data if isinstance(data, list) else data.get("items", []) if isinstance(data, dict) else []
        elif suffix in {".jsonl", ".ndjson"}:
            rows = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]
        elif suffix == ".csv":
            with target.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
        else:
            lines = [line for line in target.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip()]
            payload["count"] = len(lines)
            payload["examples"] = lines[:3]
            return payload
        payload["count"] = len(rows)
        payload["examples"] = rows[:3]
        slices = []
        for row in rows:
            if isinstance(row, dict):
                for key in ("slice", "bucket", "group", "error_type"):
                    if row.get(key):
                        slices.append(str(row[key]))
                        break
        payload["slices"] = sorted(set(slices))[:20]
    except Exception as exc:
        payload["parse_error"] = str(exc)
    return payload


def validate_audit_payload(data: dict) -> list[str]:
    issues = []
    if not isinstance(data, dict):
        return ["audit payload is not a JSON object"]
    metrics = data.get("metrics")
    if not isinstance(metrics, dict) or not metrics:
        issues.append("missing non-empty metrics object")
    if not str(data.get("claim_verdict", "")).strip():
        issues.append("missing claim_verdict")
    if not str(data.get("counterexample_outcome", "")).strip():
        issues.append("missing counterexample_outcome")
    if not str(data.get("novelty_note", "")).strip():
        issues.append("missing novelty_note")
    bad_cases = data.get("bad_cases")
    if isinstance(bad_cases, list):
        # Accept legacy/list-style bad-case payloads but require the runner to also pass a concrete bad_case_path.
        data["bad_cases"] = {"items": bad_cases, "count": len(bad_cases), "path": data.get("bad_case_path", "")}
        bad_cases = data["bad_cases"]
    if not isinstance(bad_cases, dict):
        issues.append("missing bad_cases object")
    else:
        if not bad_cases.get("path") and not bad_cases.get("items"):
            issues.append("bad_cases.path missing")
    return issues


def load_audit_payload(audit_path: Path) -> tuple[dict, list[str]]:
    if not audit_path.exists():
        return {}, [f"missing audit file: {audit_path}"]
    try:
        data = json.loads(audit_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {}, [f"audit json parse error: {exc}"]
    if isinstance(data, dict) and "bad_cases" not in data:
        sibling = audit_path.with_name("bad_cases.json")
        summary = extract_bad_case_summary(str(sibling)) if sibling.exists() else {}
        if summary:
            data["bad_cases"] = summary
            data["bad_case_path"] = str(sibling)
            try:
                save_json(audit_path, data)
            except Exception:
                pass
    return data, validate_audit_payload(data)


def claim_strength_score(rows: Iterable[dict]) -> float:
    mapping = {
        "supported": 1.0,
        "partial": 0.5,
        "promising": 0.4,
        "weakening": -0.6,
        "unsupported": -1.0,
    }
    scores = [mapping.get(str(row.get("claim_verdict", "")).strip().lower(), 0.0) for row in rows if row.get("claim_verdict")]
    return 0.0 if not scores else round(sum(scores) / len(scores), 4)


def novelty_score(rows: Iterable[dict]) -> float:
    pos = ("meaningfully novel", "meaningful novelty", "distinct contribution", "non-trivial delta", "strong novelty")
    neg = (
        "no novelty", "no novelty or improvement", "novelty claim is not supported",
        "not support final", "smoke reproduction only", "incremental", "minor",
        "weak", "unclear", "noise", "trivial"
    )
    values = []
    for row in rows:
        note = str(row.get("novelty_note", "")).lower()
        if not note:
            continue
        if any(token in note for token in neg):
            values.append(-1.0)
        elif any(token in note for token in pos):
            values.append(1.0)
        else:
            values.append(0.0)
    return 0.0 if not values else round(sum(values) / len(values) / 1.0, 4)


def counterexample_score(rows: Iterable[dict]) -> float:
    pos = ("passed", "holds", "survived", "robust", "favorable")
    neg = ("failed", "break", "broke", "collapsed", "counterexample")
    values = []
    for row in rows:
        outcome = str(row.get("counterexample_outcome", "")).lower()
        if not outcome:
            continue
        if any(token in outcome for token in pos):
            values.append(1.0)
        elif any(token in outcome for token in neg):
            values.append(-1.0)
        else:
            values.append(0.0)
    return 0.0 if not values else round(sum(values) / len(values), 4)


def supportive_claim_count(rows: Iterable[dict]) -> int:
    return sum(1 for row in rows if str(row.get("claim_verdict", "")).strip().lower() in SUPPORTIVE_CLAIM_VERDICTS)


def evidence_gate(method_rows: list[dict], recommendation: str = "") -> dict:
    bad_case_slices = sorted({slice_name for row in method_rows for slice_name in row.get("bad_case_slices", []) or []})
    claim_score = claim_strength_score(method_rows)
    novelty = novelty_score(method_rows)
    counter = counterexample_score(method_rows)
    supportive = supportive_claim_count(method_rows)
    deepen_ready = supportive > 0 and bool(bad_case_slices) and (novelty > 0 or counter >= 0.5) and recommendation not in PRUNE_RECOMMENDATIONS
    return {
        "claim_strength_score": claim_score,
        "novelty_score": novelty,
        "counterexample_score": counter,
        "bad_case_slice_count": len(bad_case_slices),
        "supportive_claim_runs": supportive,
        "deepen_ready": deepen_ready,
    }
