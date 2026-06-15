#!/usr/bin/env python3
from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any


DEFAULT_MAX_BUNDLE_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_SINGLE_FILE_BYTES = 10 * 1024 * 1024
DEFAULT_MOBILE_CACHE_BUDGET_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_TOTAL_PHONE_BYTES = 75 * 1024 * 1024


def _bytes_from_mb(value: float) -> int:
    return int(float(value) * 1024 * 1024)


def _mb(value: int) -> float:
    return round(int(value) / 1024 / 1024, 3)


def _iter_files(root: Path):
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path


def analyze_ios_app_footprint(
    app: Path,
    *,
    max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
    max_single_file_bytes: int = DEFAULT_MAX_SINGLE_FILE_BYTES,
    mobile_cache_budget_bytes: int = DEFAULT_MOBILE_CACHE_BUDGET_BYTES,
    max_total_phone_bytes: int = DEFAULT_MAX_TOTAL_PHONE_BYTES,
) -> dict[str, Any]:
    app = Path(app)
    blocking_items: list[str] = []
    if not app.exists() or not app.is_dir() or app.suffix != ".app":
        return {
            "ok": False,
            "app": str(app),
            "blocking_items": [f"iOS .app bundle not found: {app}"],
        }

    files: list[dict[str, Any]] = []
    bundle_bytes = 0
    for path in _iter_files(app):
        size = path.stat().st_size
        bundle_bytes += size
        files.append({
            "relative_path": str(path.relative_to(app)),
            "bytes": size,
        })

    large_files = [
        row for row in files
        if int(row["bytes"]) > int(max_single_file_bytes)
    ]
    estimated_max_phone_bytes = bundle_bytes + int(mobile_cache_budget_bytes)

    if bundle_bytes > int(max_bundle_bytes):
        blocking_items.append(
            f"App bundle exceeds mobile footprint budget: {_mb(bundle_bytes)} MB > {_mb(max_bundle_bytes)} MB."
        )
    if large_files:
        blocking_items.append(
            f"{len(large_files)} single file(s) exceed {_mb(max_single_file_bytes)} MB; keep heavy assets on the TASTE server."
        )
    if estimated_max_phone_bytes > int(max_total_phone_bytes):
        blocking_items.append(
            f"Estimated max phone storage exceeds budget: {_mb(estimated_max_phone_bytes)} MB > {_mb(max_total_phone_bytes)} MB."
        )

    largest_files = sorted(files, key=lambda row: int(row["bytes"]), reverse=True)[:12]
    return {
        "ok": not blocking_items,
        "app": str(app),
        "bundle_bytes": bundle_bytes,
        "bundle_mb": _mb(bundle_bytes),
        "max_bundle_bytes": int(max_bundle_bytes),
        "max_bundle_mb": _mb(max_bundle_bytes),
        "max_single_file_bytes": int(max_single_file_bytes),
        "mobile_cache_budget_bytes": int(mobile_cache_budget_bytes),
        "mobile_cache_budget_mb": _mb(mobile_cache_budget_bytes),
        "estimated_max_phone_bytes": estimated_max_phone_bytes,
        "estimated_max_phone_mb": _mb(estimated_max_phone_bytes),
        "max_total_phone_bytes": int(max_total_phone_bytes),
        "max_total_phone_mb": _mb(max_total_phone_bytes),
        "file_count": len(files),
        "largest_files": largest_files,
        "large_files": large_files,
        "blocking_items": blocking_items,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check the TASTE iOS app bundle against lightweight phone storage budgets.")
    parser.add_argument("--app", required=True, help="Path to TASTEApp.app from an iOS or simulator build.")
    parser.add_argument("--max-bundle-mb", type=float, default=50.0)
    parser.add_argument("--max-single-file-mb", type=float, default=10.0)
    parser.add_argument("--mobile-cache-budget-mb", type=float, default=20.0)
    parser.add_argument("--max-total-phone-mb", type=float, default=75.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = analyze_ios_app_footprint(
        Path(args.app),
        max_bundle_bytes=_bytes_from_mb(args.max_bundle_mb),
        max_single_file_bytes=_bytes_from_mb(args.max_single_file_mb),
        mobile_cache_budget_bytes=_bytes_from_mb(args.mobile_cache_budget_mb),
        max_total_phone_bytes=_bytes_from_mb(args.max_total_phone_mb),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary.get("ok") else 1


def run_cli(argv: list[str]) -> tuple[int, str]:
    output = io.StringIO()
    with redirect_stdout(output):
        exit_code = main(argv)
    return exit_code, output.getvalue()


if __name__ == "__main__":
    raise SystemExit(main())
