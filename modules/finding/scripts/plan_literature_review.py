#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from literature_policy import build_literature_policy, dedupe_keep_order, now_utc
from project_paths import ROOT, build_paths, load_project_config

import sys
from taste_pythonpath import ensure_taste_pythonpath
ensure_taste_pythonpath(ROOT)
from auto_research.source_selection import canonical_source_selection

AI_CORE = ["ICLR", "NeurIPS", "ICML"]
CORE_VENUE_IDS = ["openreview_iclr_2026", "openreview_neurips", "dblp_icml", "dblp_kdd"]
CORE_VENUE_NAMES = ["ICLR", "NeurIPS", "ICML", "KDD"]
DEFAULT_SECONDARY_VENUES = ["AAAI", "IJCAI", "AISTATS", "COLM"]
AI_SECONDARY = list(DEFAULT_SECONDARY_VENUES)

VENUE_IDS = {
    "NeurIPS": "openreview_neurips",
    "ICLR": "openreview_iclr_2026",
    "ICML": "dblp_icml",
    "KDD": "dblp_kdd",
    "SIGIR": "dblp_sigir",
    "WWW": "dblp_www",
    "TheWebConf": "dblp_www",
    "WSDM": "dblp_wsdm",
    "CIKM": "dblp_cikm",
    "AAAI": "dblp_aaai",
    "IJCAI": "dblp_ijcai",
    "ACL": "dblp_acl",
    "EMNLP": "dblp_emnlp",
    "COLM": "dblp_colm",
    "ACMMM": "dblp_acmmm",
}

CUSTOM_VENUES = [
    {"id": "dblp_icml", "source": "dblp", "name": "ICML", "full_name": "International Conference on Machine Learning", "type": "conference", "rank": "high-level", "field": "Artificial Intelligence / Machine Learning", "field_key": "AI", "address": "https://dblp.uni-trier.de/db/conf/icml/", "years": [], "classification_source": "topic_policy"},
    {"id": "dblp_kdd", "source": "dblp", "name": "KDD", "full_name": "ACM SIGKDD Conference on Knowledge Discovery and Data Mining", "type": "conference", "rank": "high-level", "field": "Data Mining", "field_key": "DM_CS", "address": "https://dblp.uni-trier.de/db/conf/kdd/", "years": [], "classification_source": "topic_policy"},
    {"id": "dblp_sigir", "source": "dblp", "name": "SIGIR", "full_name": "ACM SIGIR Conference on Research and Development in Information Retrieval", "type": "conference", "rank": "high-level", "field": "Information Retrieval", "field_key": "DM_CS", "address": "https://dblp.uni-trier.de/db/conf/sigir/", "years": [], "classification_source": "topic_policy"},
    {"id": "dblp_www", "source": "dblp", "name": "WWW", "full_name": "The Web Conference", "type": "conference", "rank": "high-level", "field": "Web / IR", "field_key": "DM_CS", "address": "https://dblp.uni-trier.de/db/conf/www/", "years": [], "classification_source": "topic_policy"},
    {"id": "dblp_wsdm", "source": "dblp", "name": "WSDM", "full_name": "ACM International Conference on Web Search and Data Mining", "type": "conference", "rank": "high-level", "field": "Web Search / Data Mining", "field_key": "DM_CS", "address": "https://dblp.uni-trier.de/db/conf/wsdm/", "years": [], "classification_source": "topic_policy"},
    {"id": "dblp_cikm", "source": "dblp", "name": "CIKM", "full_name": "ACM International Conference on Information and Knowledge Management", "type": "conference", "rank": "strong", "field": "Information Retrieval / Knowledge Management", "field_key": "DM_CS", "address": "https://dblp.uni-trier.de/db/conf/cikm/", "years": [], "classification_source": "topic_policy"},
    {"id": "dblp_acmmm", "source": "dblp", "name": "ACMMM", "full_name": "ACM International Conference on Multimedia", "type": "conference", "rank": "high-level", "field": "Multimedia", "field_key": "CGAndMT", "address": "https://dblp.uni-trier.de/db/conf/mm/", "years": [], "classification_source": "topic_policy"},
    {"id": "dblp_colm", "source": "dblp", "name": "COLM", "full_name": "Conference on Language Modeling", "type": "conference", "rank": "high-level", "field": "Language Modeling", "field_key": "AI", "address": "https://dblp.uni-trier.de/db/conf/colm/", "years": [], "classification_source": "topic_policy"},
]


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def tokens(text: str) -> set[str]:
    return {t.lower() for t in re.findall(r"[a-zA-Z][a-zA-Z0-9_+-]{2,}|[\u4e00-\u9fff]+", text or "")}


def has_any(blob: str, needles: list[str]) -> bool:
    low = blob.lower()
    return any(n.lower() in low for n in needles)


def infer_direction(cfg: dict[str, Any]) -> dict[str, Any]:
    blob = " ".join([str(cfg.get("topic", "")), str(cfg.get("user_prompt", "")), " ".join(cfg.get("queries", []) or [])])
    return {"topic_terms": sorted(tokens(blob))[:40]}



def build_queries(cfg: dict[str, Any], direction: dict[str, Any]) -> list[str]:
    base = list(cfg.get("queries", []) or [])
    for key in ("literature_queries", "search_queries"):
        if isinstance(cfg.get(key), list):
            base.extend(cfg.get(key) or [])
    topic = str(cfg.get("topic", "") or "").strip()
    if topic:
        base.append(topic)
    interest = str(cfg.get("research_interest", "") or "").strip()
    if interest:
        base.extend(part.strip() for part in re.split(r"[;；]", interest) if part.strip())
    return dedupe_keep_order([str(query).strip() for query in base if str(query).strip()])[:10]

def plan_venues(
    cfg: dict[str, Any],
    direction: dict[str, Any],
    *,
    core_only: bool = True,
) -> tuple[list[str], list[str], list[str], list[str]]:
    preferred = list(AI_CORE)
    secondary = list(AI_SECONDARY)
    journals: list[str] = []
    # Domain-specific venue expansion belongs in the project config.
    # The framework seeds broad venues and then merges configured venues/journals.
    policy = build_literature_policy(cfg)
    preferred.extend(policy.get("preferred_venues", []))
    secondary.extend(policy.get("secondary_venues", []))
    journals.extend(policy.get("preferred_journals", []))
    preferred = dedupe_keep_order(preferred)
    secondary = [v for v in dedupe_keep_order(secondary) if v not in preferred]
    journals = dedupe_keep_order(journals)
    venue_ids = dedupe_keep_order([VENUE_IDS[v] for v in preferred + secondary if v in VENUE_IDS])
    if core_only:
        preferred = [name for name in CORE_VENUE_NAMES if name in preferred or name in VENUE_IDS]
        secondary = []
        venue_ids = list(CORE_VENUE_IDS)
    return preferred, secondary, journals, venue_ids


def update_project_config(paths, cfg: dict[str, Any], preferred: list[str], secondary: list[str], journals: list[str], queries: list[str]) -> None:
    cfg.setdefault("literature", {})
    cfg["literature"]["preferred_venues"] = preferred
    cfg["literature"]["secondary_venues"] = secondary
    cfg["literature"]["preferred_journals"] = journals
    cfg["literature"].setdefault("primary_window_days", 180)
    cfg["literature"].setdefault("secondary_window_days", 365)
    cfg["literature"].setdefault("deprioritize_older_than_days", 730)
    cfg["queries"] = queries
    cfg.setdefault("discovery", {}).setdefault("semantic_scholar", {})["enabled"] = True
    selection = canonical_source_selection()
    enabled = ["manual", "semantic_scholar"]
    if selection.get("include_arxiv"):
        enabled.append("arxiv")
    if selection.get("include_github"):
        enabled.append("github")
    cfg.setdefault("discovery", {})["enabled_sources"] = enabled
    save_json(paths.config, cfg)


def write_taste_custom_venues(years: list[int]) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    candidate_paths = [
        repo_root / "modules" / "finding" / "data" / "custom_venues.json",
        repo_root / "external" / "TASTE" / "auto_research" / "data" / "custom_venues.json",
    ]
    rows = []
    for row in CUSTOM_VENUES:
        copy = dict(row)
        copy["years"] = years
        rows.append(copy)
    for path in candidate_paths:
        if path.parent.exists():
            save_json(path, rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Create an adaptive recent-literature review plan from the project topic.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--primary-window-days", type=int, default=90)
    parser.add_argument("--secondary-window-days", type=int, default=120)
    parser.add_argument("--wide-venue-survey", action="store_true", help="Allow broader multi-venue planning; default is latest-year core-five survey.")
    parser.add_argument("--max-queries", type=int, default=8)
    args = parser.parse_args()

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    reference_time = now_utc()
    primary_start = reference_time - dt.timedelta(days=args.primary_window_days)
    secondary_start = reference_time - dt.timedelta(days=args.secondary_window_days)
    years = [reference_time.year]
    direction = infer_direction(cfg)
    queries = build_queries(cfg, direction)[: args.max_queries]
    preferred, secondary, journals, venue_ids = plan_venues(cfg, direction, core_only=not args.wide_venue_survey)
    update_project_config(paths, cfg, preferred, secondary, journals, queries)
    write_taste_custom_venues(years)

    plan = {
        "project": args.project,
        "generated_at_utc": reference_time.isoformat(),
        "research_topic": cfg.get("topic", ""),
        "direction_signals": direction,
        "time_windows": {
            "primary_recent": {"days": args.primary_window_days, "start_utc": primary_start.isoformat(), "end_utc": reference_time.isoformat(), "purpose": "highest priority for fresh high-quality papers"},
            "secondary_recent": {"days": args.secondary_window_days, "start_utc": secondary_start.isoformat(), "end_utc": reference_time.isoformat(), "purpose": "important recent SOTA and strong transferable work"},
            "older_foundational": {"max_age_days": 1825, "purpose": "only keep if highly cited/foundational or still an unbeaten baseline"},
        },
        "venue_strategy": {
            "core_ai_top_venues": AI_CORE,
            "topic_preferred_venues": preferred,
            "topic_secondary_venues": secondary,
            "topic_journals": journals,
            "taste_venue_ids": venue_ids,
        },
        "queries": queries,
        "source_strategy": {
            "arxiv": "search planned queries in a strict recent window; keep only high-score fresh preprints to prevent arXiv from dominating conference survey",
            "semantic_scholar": "search every planned query for citation, venue, OA PDF, TLDR, and publication-date signals",
            "github": "search planned code-oriented queries; score by topic fit, stars/forks, recent activity, license/install/entrypoint cues",
            "taste": "use topic-selected venue ids plus arXiv/GitHub/HuggingFace when enabled; feed TASTE reflection back into researcher_profile",
        },
        "selection_rules": [
            "Prefer the latest-year core-five venue scan first; widen only after TASTE records evidence that the narrow survey is insufficient.",
            "Prefer primary window papers first, then secondary window papers.",
            "Prioritize the configured venue set for the current project; domain-specific venues must come from project config, not framework keyword inference.",
            "Keep arXiv only when topic fit, recency, borrowability, and citation/repo signals make it useful for idea generation.",
            "Keep older papers only as foundations, baselines, or still-unbeaten SOTA anchors; do not let stale work dominate initialization.",
            "A paper should feed an idea only if novelty delta, claim strength, counterexamples, bad-case implications, and implementation feasibility can be inspected.",
        ],
    }
    save_json(paths.state / "literature_review_plan.json", plan)
    md = ["# Adaptive Literature Review Plan\n\n"]
    md.append(f"- generated_at_utc: {plan['generated_at_utc']}\n")
    md.append(f"- topic: {plan['research_topic']}\n")
    md.append("- survey_scope: latest-year core-five venues by default; use --wide-venue-survey only with recorded evidence need\n")
    md.append(f"- years: {', '.join(str(y) for y in years)}\n")
    md.append(f"- primary_recent_window: {primary_start.date()} to {reference_time.date()} UTC ({args.primary_window_days} days)\n")
    md.append(f"- secondary_recent_window: {secondary_start.date()} to {reference_time.date()} UTC ({args.secondary_window_days} days)\n")
    md.append("\n## Direction Signals\n")
    for k, v in direction.items():
        md.append(f"- {k}: {v}\n")
    md.append("\n## Venue Focus\n")
    md.append("- core_ai_top_venues: " + ", ".join(AI_CORE) + "\n")
    md.append("- topic_preferred_venues: " + ", ".join(preferred) + "\n")
    md.append("- topic_secondary_venues: " + ", ".join(secondary) + "\n")
    md.append("- topic_journals: " + ", ".join(journals) + "\n")
    md.append("- taste_venue_ids: " + ", ".join(venue_ids) + "\n")
    md.append("\n## Queries\n")
    for q in queries:
        md.append(f"- {q}\n")
    md.append("\n## Selection Rules\n")
    for rule in plan["selection_rules"]:
        md.append(f"- {rule}\n")
    (paths.planning / "literature_review_plan.md").write_text("".join(md), encoding="utf-8")
    print(paths.planning / "literature_review_plan.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
