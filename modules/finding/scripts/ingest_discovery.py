#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from literature_policy import is_not_positive_literature_signal, now_utc, paper_sort_key, score_paper
from project_paths import ROOT, build_paths, load_project_config

import sys
from taste_pythonpath import ensure_taste_pythonpath
ensure_taste_pythonpath(ROOT)
from auto_research.source_selection import canonical_source_selection, paper_source_allowed


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def render_markdown(item: dict[str, object]) -> str:
    authors = ", ".join(item.get("authors", []))
    categories = ", ".join(item.get("categories", []))
    venue_candidates = ", ".join(item.get("venue_candidates", []))
    return (
        f"# {item['title']}\n\n"
        f"- source: {item.get('source', '')}\n"
        f"- paper_id: `{item['paper_id']}`\n"
        f"- authors: {authors}\n"
        f"- published: {item.get('published', '')}\n"
        f"- recency_bucket: {item.get('recency_bucket', '')}\n"
        f"- paper_age_days: {item.get('paper_age_days', '')}\n"
        f"- selection_bucket: {item.get('selection_bucket', '')}\n"
        f"- discovery_priority_score: {item.get('discovery_priority_score', '')}\n"
        f"- venue_candidates: {venue_candidates}\n"
        f"- categories: {categories}\n"
        f"- abs: {item.get('abs_url', '')}\n"
        f"- pdf: {item.get('pdf_url', '')}\n"
        f"- citations: {item.get('citations', '')}\n\n"
        "## Abstract\n\n"
        f"{item.get('summary', '')}\n"
    )


def merge_paper_metadata(existing: dict[str, object], incoming: dict[str, object]) -> dict[str, object]:
    merged = dict(existing)
    for key, value in incoming.items():
        if value not in (None, '', [], {}):
            merged[key] = value
    if is_not_positive_literature_signal(existing) or is_not_positive_literature_signal(incoming):
        merged["not_positive_support"] = True
        merged["weak_candidate_for_critique"] = True
        merged["selection_bucket"] = "deprioritized"
        merged["high_quality_recent"] = False
        merged["discovery_priority_score"] = min(float(merged.get("discovery_priority_score") or 0), 0.0)
        merged["idea_worthiness_score"] = min(float(merged.get("idea_worthiness_score") or 0), 0.0)
        merged["guardrail"] = (
            "finding inspected this as a weak or boundary candidate; keep it visible "
            "for critique/search expansion only, not as a positive idea anchor or paper claim support."
        )
    return merged


def latest_discoveries(discover_dir: Path, include_history: bool = False) -> list[Path]:
    files = sorted(discover_dir.glob("*.json"), key=lambda path: path.stat().st_mtime if path.exists() else 0)
    if include_history:
        return files
    return files[-1:] if files else []


def normalize_paper_id(value: str) -> str:
    return str(value).replace("/", "_")


def cleanup_empty_ingest_artifacts(paths) -> None:
    # Avoid stale paper-derived wiki pages contaminating a stricter no-qualified round.
    for folder in [paths.raw_papers, paths.wiki_papers, paths.wiki_concepts, paths.wiki_entities]:
        if folder.exists():
            for child in folder.iterdir():
                if child.is_file():
                    child.unlink()
                elif child.is_dir():
                    import shutil
                    shutil.rmtree(child)


def ranked_candidates(cfg: dict[str, object], files: list[Path], selection: dict[str, object] | None = None) -> tuple[list[dict[str, object]], dict[str, object]]:
    dedup: dict[str, dict[str, object]] = {}
    reference_time = now_utc()
    for src in files:
        payload = load_json(src)
        items = payload.get("items", []) if isinstance(payload, dict) else []
        for raw_item in items:
            if not isinstance(raw_item, dict):
                continue
            if selection is not None and not paper_source_allowed(raw_item, selection):
                continue
            item = dict(raw_item)
            item["paper_id"] = normalize_paper_id(str(item.get("paper_id", "unknown")))
            item.setdefault("query", payload.get("query", ""))
            item.setdefault("source_file", str(src))
            item.setdefault("generated_at", payload.get("generated_at", ""))
            item.update(score_paper(item, cfg, reference_time=reference_time))
            if is_not_positive_literature_signal(item):
                item["not_positive_support"] = True
                item["weak_candidate_for_critique"] = True
                item["selection_bucket"] = "deprioritized"
                item["high_quality_recent"] = False
                item["discovery_priority_score"] = min(float(item.get("discovery_priority_score") or 0), 0.0)
                item["idea_worthiness_score"] = min(float(item.get("idea_worthiness_score") or 0), 0.0)
            existing = dedup.get(item["paper_id"])
            if existing:
                merged = merge_paper_metadata(existing, item)
                merged.update(score_paper(merged, cfg, reference_time=reference_time))
                dedup[item["paper_id"]] = merged
            elif not existing or paper_sort_key(item) < paper_sort_key(existing):
                dedup[item["paper_id"]] = item
    ranked = sorted(dedup.values(), key=paper_sort_key)
    summary = {
        "reference_time": reference_time.isoformat(),
        "candidate_count": len(ranked),
        "recent_high_priority_count": sum(1 for row in ranked if row.get('selection_bucket') == 'recent_high_priority'),
        "recent_candidate_count": sum(1 for row in ranked if row.get('selection_bucket') == 'recent_candidate'),
        "older_foundational_count": sum(1 for row in ranked if row.get('selection_bucket') == 'older_foundational'),
        "deprioritized_count": sum(1 for row in ranked if row.get('selection_bucket') == 'deprioritized'),
    }
    return ranked, summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--discovery-file")
    parser.add_argument("--include-history", action="store_true", help="Also rank older discovery files; default is latest discovery file only to avoid stale demoted literature resurfacing.")
    parser.add_argument("--limit", type=int, default=3)
    args = parser.parse_args()

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    ingested_ids = load_json(paths.state / "ingested_ids.json")
    files = [Path(args.discovery_file)] if args.discovery_file else latest_discoveries(paths.discover, include_history=args.include_history)
    selection = canonical_source_selection(project_config_path=paths.config)
    ranked, summary = ranked_candidates(cfg, list(reversed(files)), selection)
    count = 0
    ingested_rows: list[dict[str, object]] = []
    already_ingested_rows: list[dict[str, object]] = []

    max_foundational = int(cfg.get('literature', {}).get('max_foundational_older_papers', 3)) if isinstance(cfg.get('literature', {}), dict) else 3
    used_foundational = 0

    allowed_buckets = {'recent_high_priority', 'recent_candidate', 'older_foundational'}
    eligible_count = 0
    for item in ranked:
        paper_id = str(item["paper_id"])
        if item.get('selection_bucket') not in allowed_buckets:
            continue
        eligible_count += 1
        if paper_id in ingested_ids:
            paper_dir = paths.raw_papers / paper_id
            metadata_path = paper_dir / "metadata.json"
            if metadata_path.exists():
                existing = load_json(metadata_path)
                if isinstance(existing, dict):
                    merged = merge_paper_metadata(existing, item)
                    save_json(metadata_path, merged)
                    if (paper_dir / "source.md").exists():
                        (paper_dir / "source.md").write_text(render_markdown(merged), encoding="utf-8")
            already_ingested_rows.append({
                "paper_id": paper_id,
                "title": item.get("title", ""),
                "selection_bucket": item.get("selection_bucket", ""),
                "discovery_priority_score": item.get("discovery_priority_score", 0),
                "metadata_refreshed": metadata_path.exists(),
            })
            continue
        if item.get('selection_bucket') == 'older_foundational':
            if used_foundational >= max_foundational:
                continue
            used_foundational += 1
        paper_dir = paths.raw_papers / paper_id
        paper_dir.mkdir(parents=True, exist_ok=True)
        save_json(paper_dir / "metadata.json", item)
        (paper_dir / "source.md").write_text(render_markdown(item), encoding="utf-8")
        ingested_ids.append(paper_id)
        ingested_rows.append({
            "paper_id": paper_id,
            "title": item.get("title", ""),
            "selection_bucket": item.get("selection_bucket", ""),
            "recency_bucket": item.get("recency_bucket", ""),
            "paper_age_days": item.get("paper_age_days"),
            "discovery_priority_score": item.get("discovery_priority_score", 0),
            "venue_candidates": item.get("venue_candidates", []),
        })
        count += 1
        if count >= args.limit:
            break

    save_json(paths.state / "ingested_ids.json", ingested_ids)
    no_qualified = eligible_count == 0
    no_new = eligible_count > 0 and count == 0
    if no_qualified:
        cleanup_empty_ingest_artifacts(paths)
    save_json(paths.state / "ingest_ranking.json", {
        "summary": {**summary, "eligible_count": eligible_count, "already_ingested_count": len(already_ingested_rows), "new_ingested_count": count},
        "ingested": ingested_rows,
        "already_ingested": already_ingested_rows[:25],
        "top_candidates": ranked[: min(len(ranked), 25)],
        "eligible_bucket_policy": sorted(allowed_buckets),
        "no_qualified_papers": no_qualified,
        "no_new_papers": no_new,
        "no_qualified_reason": "All discovered papers were deprioritized by recency/venue/topic/actionability policy." if no_qualified else "",
        "no_new_reason": "Qualified papers were already ingested in previous rounds; keeping existing wiki/raw artifacts." if no_new else "",
    })
    suffix = f" eligible={eligible_count} already={len(already_ingested_rows)}"
    print(f"ingested={count}{suffix}")


if __name__ == "__main__":
    main()
