from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from cache.build_category_summary import write_summary_file
from cache.build_openreview_cache import build_openreview_year
from cache.build_openreview_cache import venue_spec
from cache.build_openreview_cache import VENUE_ALIASES
from finding_runtime.paths import LOCAL_DATABASE_DIR, write_json_cache
from finding_runtime.paths import display_path


DEFAULT_OUTPUT_ROOT = LOCAL_DATABASE_DIR


def parse_csv(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").replace(";", ",").split(",") if item.strip()]


def parse_years(value: str) -> list[int]:
    years: list[int] = []
    for item in parse_csv(value):
        try:
            year = int(item)
        except ValueError:
            continue
        if 2000 <= year <= 2100 and year not in years:
            years.append(year)
    return years


def default_years_for_builder(builder: str) -> list[int]:
    try:
        return list(venue_spec(builder).get("default_years") or [])
    except Exception:
        return []


def load_paper_count(path: Path) -> int:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        papers = data.get("papers", []) if isinstance(data, dict) else []
        return int(data.get("paper_count") or len(papers if isinstance(papers, list) else []))
    except Exception:
        return 0


def current_index_snapshot(root: Path = DEFAULT_OUTPUT_ROOT) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for papers_path in sorted(root.glob("*/*/papers.json")):
        summary_path = papers_path.with_name("category_summary.json")
        rows.append({
            "venue_id": papers_path.parent.parent.name,
            "year": papers_path.parent.name,
            "papers_path": display_path(papers_path),
            "category_summary_path": display_path(summary_path),
            "paper_count": load_paper_count(papers_path),
            "category_summary_exists": summary_path.exists(),
        })
    return rows


def _builder_venue(value: str) -> str:
    key = str(value or "").strip().lower()
    if key in {"all", "all_openreview", "openreview_all"}:
        return key
    if key not in VENUE_ALIASES:
        raise ValueError(f"unsupported venue builder {value!r}; supported: {', '.join(sorted(VENUE_ALIASES))}")
    return VENUE_ALIASES[key]


def refresh_database(args: argparse.Namespace) -> dict[str, Any]:
    output_root = Path(args.output_root).expanduser()
    output_root.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    requested_venues: list[str] = []
    for venue in args.venues:
        try:
            builder = _builder_venue(venue)
        except ValueError as exc:
            results.append({"venue": venue, "status": "skipped", "reason": str(exc)})
            continue
        if builder in {"all", "all_openreview", "openreview_all"}:
            requested_venues.extend(sorted(set(VENUE_ALIASES.values())))
        else:
            requested_venues.append(builder)
    for builder in list(dict.fromkeys(requested_venues)):
        years = args.years or default_years_for_builder(builder)
        for year in years:
            spec = venue_spec(builder)
            final_venue_id = str(spec.get("venue_id") or f"openreview_{builder}")
            final_dir = output_root / final_venue_id / str(year)
            final_papers = final_dir / "papers.json"
            if args.if_missing and final_papers.exists() and load_paper_count(final_papers) > 0 and final_papers.with_name("category_summary.json").exists():
                results.append({"venue": builder, "year": year, "status": "skipped_existing", "papers_path": display_path(final_papers), "paper_count": load_paper_count(final_papers)})
                continue
            tmp_root = output_root / ".tmp_update" / final_venue_id / str(year)
            if tmp_root.exists():
                shutil.rmtree(tmp_root)
            tmp_output = tmp_root / "local_database"
            try:
                target = build_openreview_year(
                    venue=builder,
                    year=year,
                    output_root=tmp_output,
                    page_size=max(1, args.page_size),
                    timeout=max(1, args.request_timeout_sec),
                    retries=max(0, args.retries),
                    max_pages=max(1, args.max_pages),
                )
                data = json.loads(target.read_text(encoding="utf-8"))
                count = int(data.get("paper_count") or len(data.get("papers", []) if isinstance(data.get("papers"), list) else []))
                if count <= 0 and final_papers.exists() and not args.allow_empty:
                    results.append({"venue": builder, "year": year, "status": "kept_existing", "reason": "fresh fetch returned zero papers", "paper_count": count, "papers_path": display_path(final_papers)})
                    continue
                final_dir.mkdir(parents=True, exist_ok=True)
                write_json_cache(final_papers, data)
                summary = write_summary_file(final_papers)
                results.append({"venue": builder, "year": year, "status": "updated", "paper_count": count, "papers_path": display_path(final_papers), "category_summary_path": display_path(summary)})
            except Exception as exc:
                results.append({"venue": builder, "year": year, "status": "failed", "error": type(exc).__name__ + ": " + str(exc)})
            finally:
                if tmp_root.exists():
                    shutil.rmtree(tmp_root)
    return {
        "status": "updated" if any(row.get("status") == "updated" for row in results) else "skipped",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "local_database": display_path(output_root),
        "results": results,
        "snapshot": current_index_snapshot(output_root),
    }


def run_cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh Finding local OpenReview venue indexes in the module runtime cache.")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT), help="Local database root. Defaults to .runtime/cache/local_database.")
    parser.add_argument("--venues", default="iclr,neurips,icml", help="Comma-separated venue builders from OPENREVIEW_VENUE_PATTERNS, or all_openreview. Defaults to stable known builders: iclr, neurips, icml.")
    parser.add_argument("--years", default="", help="Comma-separated years. When omitted, each venue uses its default stable/current year set.")
    parser.add_argument("--request-timeout-sec", type=int, default=30)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--allow-empty", action="store_true", help="Allow a zero-paper fetch to overwrite an existing cache.")
    parser.add_argument("--if-missing", action="store_true", help="Only fetch venue/year indexes whose papers.json or category_summary.json is missing or empty.")
    args = parser.parse_args(list(argv) if argv is not None else None)
    args.venues = parse_csv(args.venues)
    args.years = parse_years(args.years)
    if not args.venues:
        raise SystemExit("at least one venue is required")
    payload = refresh_database(args)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if all(row.get("status") != "failed" for row in payload.get("results", [])) else 1
