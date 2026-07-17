from __future__ import annotations

import json
import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

from runtime.framework_paths import FRAMEWORK_INPUTS_DIR


ROOT = Path(os.environ.get("WORKSPACE_ROOT") or Path(__file__).resolve().parents[3]).expanduser().resolve()
FINDING_ENTRYPOINT = ROOT / "modules" / "finding" / "main.py"


def _json_tail(text: str) -> dict[str, Any]:
    for index in range(len(text) - 1, -1, -1):
        if text[index] != "{":
            continue
        try:
            payload = json.loads(text[index:].strip())
        except Exception:
            continue
        return payload if isinstance(payload, dict) else {}
    return {}


def _run_finding_json(args: list[str], *, timeout_sec: float = 30.0) -> dict[str, Any]:
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    proc = subprocess.run(
        [sys.executable, str(FINDING_ENTRYPOINT), *args],
        cwd=str(ROOT),
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout_sec,
    )
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if proc.returncode != 0:
        raise RuntimeError((combined.strip() or f"Finding CLI failed with code {proc.returncode}")[-2000:])
    payload = _json_tail(proc.stdout or combined)
    if not payload:
        raise RuntimeError("Finding CLI did not return JSON.")
    return payload


@lru_cache(maxsize=1)
def load_catalog() -> list[dict[str, Any]]:
    payload = _run_finding_json(["--action", "catalog"], timeout_sec=30.0)
    venues = payload.get("venues")
    return [dict(item) for item in venues if isinstance(item, dict)] if isinstance(venues, list) else []


def catalog_by_id() -> dict[str, dict[str, Any]]:
    catalog: dict[str, dict[str, Any]] = {}
    for venue in load_catalog():
        venue_id = str(venue.get("id") or "").strip()
        if venue_id:
            catalog[venue_id] = dict(venue)
        canonical_id = venue_id
        for alias in venue.get("aliases", []) if isinstance(venue.get("aliases"), list) else []:
            if not isinstance(alias, dict):
                continue
            alias_id = str(alias.get("id") or "").strip()
            if alias_id and alias_id not in catalog:
                catalog[alias_id] = {**venue, "id": alias_id, "canonical_id": canonical_id}
    return catalog


def venue_health(selection: dict[str, Any], *, sample_limit: int = 3, timeout_sec: float = 30.0) -> list[dict[str, Any]]:
    input_dir = FRAMEWORK_INPUTS_DIR / "finding"
    input_dir.mkdir(parents=True, exist_ok=True)
    selection_path = input_dir / f"venue_health_{os.getpid()}_{id(selection)}.json"
    selection_path.write_text(json.dumps(selection, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        payload = _run_finding_json(
            [
                "--action",
                "venue_health",
                "--selection-json",
                str(selection_path),
                "--sample-limit",
                str(max(1, int(sample_limit or 1))),
            ],
            timeout_sec=timeout_sec,
        )
    finally:
        try:
            selection_path.unlink()
        except FileNotFoundError:
            pass
    results = payload.get("results")
    return [dict(item) for item in results if isinstance(item, dict)] if isinstance(results, list) else []


def fetch_venue_sample(venue: dict[str, Any], year: int, sample_limit: int = 3) -> dict[str, Any]:
    venue_id = str((venue if isinstance(venue, dict) else {}).get("id") or "").strip()
    if not venue_id:
        return {"ok": False, "sample_count": 0, "source_adapter": "unknown", "message": "Unknown venue id.", "samples": []}
    results = venue_health(
        {
            "venue_ids": [venue_id],
            "years": [int(year)],
            "venue_years": [{"venue_id": venue_id, "year": int(year)}],
        },
        sample_limit=sample_limit,
        timeout_sec=float(os.environ.get("VENUE_HEALTH_TIMEOUT_SEC", "8") or 8) + 5.0,
    )
    return results[0] if results else {"venue_id": venue_id, "year": int(year), "ok": False, "sample_count": 0, "source_adapter": "unknown", "message": "No venue health result.", "samples": []}
