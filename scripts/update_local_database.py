#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from project_paths import ROOT, build_paths, conda_executable
from auto_research.paths import LOCAL_DATABASE_DIR

WORKSPACE_ROOT = ROOT / "modules" / "taste"
DEFAULT_ENV = "taste"
LOCAL_DATABASE = LOCAL_DATABASE_DIR

DRIVER = r'''
from __future__ import annotations
import json
import shutil
import sys
from pathlib import Path
from datetime import datetime, timezone

taste_root = Path({taste_root_json})
sys.path.insert(0, str(taste_root))

from auto_research.auto_update.json_builder.build_category_summary import write_summary_file
from auto_research.auto_update.json_builder.build_iclr_openreview_cache import build_iclr_year
from auto_research.auto_update.json_builder.build_neurips_openreview_cache import build_neurips_year

output_root = Path({output_root_json})
years = {years_json}
venues = {venues_json}
page_size = {page_size}
timeout = {timeout}
retries = {retries}
max_pages = {max_pages}
allow_empty = {allow_empty}

builders = {{
    "iclr": build_iclr_year,
    "openreview_iclr": build_iclr_year,
    "neurips": build_neurips_year,
    "openreview_neurips": build_neurips_year,
}}
results = []
for venue in venues:
    builder = builders.get(str(venue).lower())
    if not builder:
        results.append({{"venue": venue, "status": "skipped", "reason": "unsupported venue builder"}})
        continue
    for year in years:
        target = None
        tmp_root = output_root / ".tmp_update" / str(venue).lower() / str(year)
        if tmp_root.exists():
            shutil.rmtree(tmp_root)
        tmp_output = tmp_root / "local_database"
        try:
            target = builder(year=year, output_root=tmp_output, page_size=page_size, timeout=timeout, retries=retries, max_pages=max_pages)
            data = json.loads(target.read_text(encoding="utf-8"))
            count = int(data.get("paper_count") or len(data.get("papers", []) if isinstance(data.get("papers"), list) else []))
            if count <= 0 and not allow_empty:
                results.append({{"venue": venue, "year": year, "status": "kept_existing", "reason": "fresh fetch returned zero papers", "paper_count": count}})
                continue
            final_dir = output_root / data.get("venue_id", str(venue).lower()) / str(year)
            final_dir.mkdir(parents=True, exist_ok=True)
            final_papers = final_dir / "papers.json"
            final_papers.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            summary = write_summary_file(final_papers)
            results.append({{"venue": venue, "year": year, "status": "updated", "paper_count": count, "papers_path": str(final_papers), "category_summary_path": str(summary)}})
        except Exception as exc:
            results.append({{"venue": venue, "year": year, "status": "failed", "error": type(exc).__name__ + ": " + str(exc)}})
        finally:
            if tmp_root.exists():
                shutil.rmtree(tmp_root)
print(json.dumps({{"generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "results": results}}, ensure_ascii=False))
'''


def parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.replace(";", ",").split(",") if item.strip()]


def parse_years(value: str) -> list[int]:
    years = []
    for item in parse_csv(value):
        if item.isdigit():
            years.append(int(item))
    return years


def load_paper_count(path: Path) -> int:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return int(data.get("paper_count") or len(data.get("papers", []) if isinstance(data.get("papers"), list) else []))
    except Exception:
        return 0


def current_index_snapshot(root: Path = LOCAL_DATABASE) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for papers_path in sorted(root.glob("*/*/papers.json")):
        summary_path = papers_path.with_name("category_summary.json")
        rows.append({
            "venue_id": papers_path.parent.parent.name,
            "year": papers_path.parent.name,
            "papers_path": str(papers_path),
            "category_summary_path": str(summary_path),
            "paper_count": load_paper_count(papers_path),
            "category_summary_exists": summary_path.exists(),
        })
    return rows


def run_driver(args: argparse.Namespace, paths) -> dict[str, Any]:
    conda = conda_executable()
    if not conda:
        raise SystemExit("conda not found; cannot update TASTE local database")
    tmp_dir = paths.root / "tmp" / "finding"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    driver = tmp_dir / "update_local_database_driver.py"
    driver.write_text(DRIVER.format(
        taste_root_json=json.dumps(str(WORKSPACE_ROOT)),
        output_root_json=json.dumps(str(LOCAL_DATABASE)),
        years_json=json.dumps(args.years),
        venues_json=json.dumps(args.venues),
        page_size=max(1, args.page_size),
        timeout=max(1, args.request_timeout_sec),
        retries=max(0, args.retries),
        max_pages=max(1, args.max_pages),
        allow_empty="True" if args.allow_empty else "False",
    ), encoding="utf-8")
    log_path = paths.logs / "taste_local_database_update.log"
    proc = subprocess.run([conda, "run", "-n", args.env_name, "python", str(driver)], cwd=ROOT, text=True, capture_output=True, timeout=args.timeout_sec)
    log_path.write_text((proc.stdout or "") + "\n--- STDERR ---\n" + (proc.stderr or ""), encoding="utf-8")
    try:
        driver.unlink()
    except FileNotFoundError:
        pass
    if proc.returncode != 0:
        return {"status": "failed", "return_code": proc.returncode, "log_path": str(log_path), "stderr_tail": (proc.stderr or "")[-2000:]}
    lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
    payload = json.loads(lines[-1]) if lines else {"results": []}
    payload.update({"status": "updated", "return_code": 0, "log_path": str(log_path)})
    return payload


def write_state(paths, payload: dict[str, Any], before: list[dict[str, Any]], after: list[dict[str, Any]]) -> None:
    payload = dict(payload)
    payload.setdefault("generated_at", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    payload["project"] = paths.name
    payload["taste_root"] = str(WORKSPACE_ROOT)
    payload["local_database"] = str(LOCAL_DATABASE)
    payload["before"] = before
    payload["after"] = after
    paths.state.mkdir(parents=True, exist_ok=True)
    paths.reports.mkdir(parents=True, exist_ok=True)
    (paths.state / "taste_local_database_update.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# TASTE Local Database Update\n\n", f"- status: {payload.get('status')}\n", f"- generated_at: {payload.get('generated_at')}\n", f"- local_database: {LOCAL_DATABASE}\n"]
    if payload.get("log_path"):
        lines.append(f"- log_path: {payload.get('log_path')}\n")
    lines.append("\n## Results\n")
    for row in payload.get("results", []):
        lines.append(f"- {row.get('venue')} {row.get('year')}: {row.get('status')} papers={row.get('paper_count', '')} {row.get('reason', row.get('error', ''))}\n")
    lines.append("\n## Current Index Snapshot\n")
    for row in after:
        lines.append(f"- {row['venue_id']} {row['year']}: papers={row['paper_count']} summary={'yes' if row['category_summary_exists'] else 'no'}\n")
    (paths.reports / "taste_local_database_update.md").write_text("".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh integrated TASTE local venue indexes used by TASTE literature survey.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--env-name", default=DEFAULT_ENV)
    parser.add_argument("--venues", default="iclr,neurips", help="Comma-separated supported builders: iclr, neurips.")
    parser.add_argument("--years", default="2026,2025", help="Comma-separated years.")
    parser.add_argument("--timeout-sec", type=int, default=int(os.environ.get("DB_UPDATE_TIMEOUT_SEC", "1800")))
    parser.add_argument("--request-timeout-sec", type=int, default=30)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--allow-empty", action="store_true", help="Allow a zero-paper fetch to overwrite an existing cache. Default protects existing useful caches.")
    parser.add_argument("--if-missing", action="store_true", help="Only fetch venue/year indexes whose papers.json or category_summary.json is missing or empty.")
    args = parser.parse_args()
    args.venues = parse_csv(args.venues)
    args.years = parse_years(args.years)
    if not args.venues or not args.years:
        raise SystemExit("at least one venue and one year are required")
    paths = build_paths(args.project)
    before = current_index_snapshot()
    if args.if_missing:
        existing = {(row["venue_id"], int(row["year"])): row for row in before if str(row.get("year", "")).isdigit()}
        venue_map = {"iclr": "openreview_iclr", "openreview_iclr": "openreview_iclr", "neurips": "openreview_neurips", "openreview_neurips": "openreview_neurips"}
        needed: dict[str, list[int]] = {}
        for venue in args.venues:
            venue_id = venue_map.get(venue.lower(), venue.lower())
            for year in args.years:
                row = existing.get((venue_id, year))
                if not row or int(row.get("paper_count") or 0) <= 0 or not row.get("category_summary_exists"):
                    needed.setdefault(venue, []).append(year)
        if not needed:
            payload = {"status": "skipped", "reason": "all requested indexes already exist with nonzero paper counts", "results": []}
            after = current_index_snapshot()
            write_state(paths, payload, before, after)
            print(json.dumps(payload, ensure_ascii=False))
            return 0
        # Flatten to a conservative fetch set; builder runtime is still venue/year specific.
        args.venues = list(needed.keys())
        args.years = sorted({year for years in needed.values() for year in years})
    payload = run_driver(args, paths)
    after = current_index_snapshot()
    write_state(paths, payload, before, after)
    print(json.dumps({"status": payload.get("status"), "results": payload.get("results", []), "after_count": len(after)}, ensure_ascii=False))
    return 0 if payload.get("status") in {"updated", "skipped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
