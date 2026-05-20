from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from auto_research.paths import PROJECT_ROOT


DEFAULT_DATABASE_ROOT = PROJECT_ROOT / "auto_research" / "local_database"


def _category_name(paper: dict[str, Any]) -> str:
    for key in ("primary_area", "category", "track"):
        value = str(paper.get(key) or "").strip()
        if value:
            return value
    return "(uncategorized)"


def _clean_sample(value: object, limit: int = 180) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit].rstrip()


def build_category_summary(data: dict[str, Any], sample_size: int = 5) -> list[dict[str, Any]]:
    papers = data.get("papers", [])
    if not isinstance(papers, list):
        return []

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for paper in papers:
        if isinstance(paper, dict):
            grouped[_category_name(paper)].append(paper)

    summary: list[dict[str, Any]] = []
    for category, items in grouped.items():
        sample_titles = []
        for item in items[:sample_size]:
            title = _clean_sample(item.get("title"))
            if title:
                sample_titles.append(title)
        keywords: list[str] = []
        seen_keywords: set[str] = set()
        for item in items:
            for keyword in item.get("keywords") or []:
                keyword_text = _clean_sample(keyword, limit=80)
                key = keyword_text.lower()
                if keyword_text and key not in seen_keywords:
                    seen_keywords.add(key)
                    keywords.append(keyword_text)
                if len(keywords) >= 20:
                    break
            if len(keywords) >= 20:
                break
        summary.append({
            "name": category,
            "count": len(items),
            "sample_titles": sample_titles,
            "sample_keywords": keywords,
        })

    return sorted(summary, key=lambda item: (-int(item["count"]), str(item["name"]).lower()))


def _summary_path_for(path: Path) -> Path:
    if path.name == "papers.json":
        return path.with_name("category_summary.json")
    return path.parent / path.stem / "category_summary.json"


def write_summary_file(path: Path, sample_size: int = 5) -> Path:
    data = json.loads(path.read_text(encoding="utf-8"))
    summary = build_category_summary(data, sample_size=sample_size)
    payload = {
        "schema_version": data.get("schema_version", 1),
        "venue_id": data.get("venue_id", ""),
        "venue": data.get("venue", ""),
        "full_name": data.get("full_name", ""),
        "year": data.get("year", ""),
        "source": data.get("source", ""),
        "source_adapter": data.get("source_adapter", ""),
        "paper_count": data.get("paper_count", len(data.get("papers", []) if isinstance(data.get("papers"), list) else [])),
        "category_count": len(summary),
        "category_counts": {item["name"]: item["count"] for item in summary},
        "category_summary": summary,
        "built_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_file": str(path),
    }
    target = _summary_path_for(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def iter_json_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    paths = [
        path
        for pattern in ("*/papers.json", "*/*/papers.json")
        for path in root.glob(pattern)
        if path.is_file()
    ]
    return sorted(dict.fromkeys(paths))


def main() -> None:
    parser = argparse.ArgumentParser(description="Build neutral category_summary.json files for local venue/year paper JSON files.")
    parser.add_argument("--root", default=str(DEFAULT_DATABASE_ROOT), help="Local database root or a single JSON file.")
    parser.add_argument("--sample-size", type=int, default=5, help="Number of sample titles to keep per category.")
    args = parser.parse_args()

    paths = iter_json_files(Path(args.root))
    if not paths:
        raise SystemExit(f"No JSON files found under {args.root}")

    for path in paths:
        target = write_summary_file(path, sample_size=max(1, args.sample_size))
        print(f"Wrote {target}")


if __name__ == "__main__":
    main()
