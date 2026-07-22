from __future__ import annotations

import fcntl
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

import requests

from .framework_paths import FRAMEWORK_LOCKS_DIR


_CRAWL_SERVICE_LOCKS_LOCK = threading.Lock()
_CRAWL_SERVICE_LOCKS: dict[str, threading.Lock] = {}


class CrawlServiceCooldownActive(RuntimeError):
    def __init__(self, service: str, remaining: float, reason: str = "") -> None:
        self.service = service
        self.remaining = max(0.0, remaining)
        self.reason = reason
        super().__init__(f"{service} access cooldown active for {self.remaining:.1f}s")


def _crawl_service_name(service: str) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "_", str(service or "generic").lower()) or "generic"


def _crawl_service_state(handle) -> dict:
    handle.seek(0)
    try:
        payload = json.load(handle)
    except Exception:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _write_crawl_service_state(handle, state: dict) -> None:
    handle.seek(0)
    handle.truncate()
    json.dump(state, handle, ensure_ascii=True, sort_keys=True)
    handle.flush()


def _crawl_service_root(state_root: Path | None = None) -> Path:
    return Path(state_root) if state_root is not None else FRAMEWORK_LOCKS_DIR / "crawl_services"


@contextmanager
def crawl_service_slot(
    service: str,
    *,
    min_interval_sec: float = 0.0,
    allow_during_cooldown: bool = False,
    state_root: Path | None = None,
) -> Iterator[dict]:
    """Serialize one external crawl service across threads and processes."""
    service_name = _crawl_service_name(service)
    with _CRAWL_SERVICE_LOCKS_LOCK:
        process_lock = _CRAWL_SERVICE_LOCKS.setdefault(service_name, threading.Lock())
    with process_lock:
        root = _crawl_service_root(state_root)
        root.mkdir(parents=True, exist_ok=True)
        state_path = root / f"{service_name}.lock"
        with state_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                state = _crawl_service_state(handle)
                now = time.time()
                cooldown_until = float(state.get("cooldown_until") or 0.0)
                if cooldown_until > now and not allow_during_cooldown:
                    raise CrawlServiceCooldownActive(
                        service_name,
                        cooldown_until - now,
                        str(state.get("cooldown_reason") or ""),
                    )
                wait = max(0.0, float(min_interval_sec or 0.0) - (now - float(state.get("last_finished_at") or 0.0)))
                if wait:
                    time.sleep(wait)
                gate: dict = {"service": service_name, "waited_sec": round(wait, 3)}
                try:
                    yield gate
                finally:
                    finished_at = time.time()
                    state["last_finished_at"] = finished_at
                    state["pid"] = os.getpid()
                    cooldown = max(0.0, float(gate.get("cooldown_sec") or 0.0))
                    if cooldown > 0:
                        state["cooldown_until"] = max(
                            float(state.get("cooldown_until") or 0.0),
                            finished_at + cooldown,
                        )
                        state["cooldown_reason"] = str(gate.get("cooldown_reason") or "")
                    _write_crawl_service_state(handle, state)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def crawl_service_cooldown_remaining(service: str, *, state_root: Path | None = None) -> float:
    service_name = _crawl_service_name(service)
    with _CRAWL_SERVICE_LOCKS_LOCK:
        process_lock = _CRAWL_SERVICE_LOCKS.setdefault(service_name, threading.Lock())
    with process_lock:
        state_path = _crawl_service_root(state_root) / f"{service_name}.lock"
        if not state_path.exists():
            return 0.0
        with state_path.open("r", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                remaining = float(_crawl_service_state(handle).get("cooldown_until") or 0.0) - time.time()
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return round(max(0.0, remaining), 3)


_CRAWL_MIN_INTERVAL_DEFAULTS = {
    "arxiv": 3.1, "biorxiv": 3.0, "science": 3.0, "openreview": 1.0,
    "iclr": 3.0, "icml": 3.0, "crossref": 0.34, "openalex": 0.05,
    "semanticscholar": 1.0, "europepmc": 0.25, "springernature": 0.7,
    "unpaywall": 0.25, "acm": 2.0, "dblp": 1.0, "chatpaper": 10.0,
    "github": 1.0, "generic": 0.05,
}
_CRAWL_RATE_LIMIT_DEFAULTS = {
    "arxiv": 6.0, "biorxiv": 15.0, "science": 15.0, "openreview": 10.0,
    "iclr": 15.0, "icml": 15.0, "crossref": 2.0, "openalex": 2.0,
    "semanticscholar": 5.0, "europepmc": 2.0, "springernature": 5.0,
    "unpaywall": 2.0, "acm": 15.0, "dblp": 10.0, "chatpaper": 10.0,
    "github": 60.0, "generic": 10.0,
}
_CRAWL_CHALLENGE_DEFAULTS = {
    "biorxiv": 60.0, "science": 30.0, "openreview": 30.0,
    "iclr": 60.0, "icml": 60.0, "acm": 30.0, "generic": 10.0,
}
_CRAWL_ACCESS_DENIED_DEFAULTS = {"openreview": 30.0, "iclr": 30.0, "icml": 30.0}


def crawl_service_from_url(url: str) -> str:
    host = urlparse(str(url or "")).netloc.lower()
    rules = (
        ("arxiv.org", "arxiv"), ("biorxiv.org", "biorxiv"),
        ("medrxiv.org", "biorxiv"), ("science.org", "science"),
        ("openreview.net", "openreview"), ("iclr.cc", "iclr"),
        ("icml.cc", "icml"), ("crossref.org", "crossref"),
        ("openalex.org", "openalex"), ("semanticscholar.org", "semanticscholar"),
        ("europepmc.org", "europepmc"), ("ebi.ac.uk", "europepmc"),
        ("springernature.com", "springernature"), ("springer.com", "springernature"),
        ("nature.com", "springernature"), ("unpaywall.org", "unpaywall"),
        ("dl.acm.org", "acm"), ("chatpaper.com", "chatpaper"),
        ("dblp.org", "dblp"),
        ("dblp.uni-trier.de", "dblp"), ("dblp.dagstuhl.de", "dblp"),
        ("github.com", "github"),
        ("githubusercontent.com", "github"),
    )
    for suffix, service in rules:
        if host == suffix or host.endswith("." + suffix):
            return service
    normalized_host = re.sub(r"[^a-z0-9_.-]+", "_", host).strip("_.-")
    return f"host_{normalized_host}" if normalized_host else "generic"


def _crawl_policy_value(prefix: str, service: str, defaults: dict[str, float]) -> float:
    default = defaults.get(service, defaults["generic"])
    env_service = re.sub(r"[^A-Z0-9]+", "_", service.upper()).strip("_") or "GENERIC"
    raw = os.environ.get(f"FINDING_{env_service}_{prefix}", "")
    try:
        return max(0.0, float(raw)) if str(raw).strip() else default
    except ValueError:
        return default


def _crawl_retry_after_seconds(response: requests.Response, service: str) -> float:
    headers = getattr(response, "headers", {}) or {}
    raw = str(headers.get("retry-after") or "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            try:
                return max(0.0, parsedate_to_datetime(raw).timestamp() - time.time())
            except Exception:
                pass
    reset = str(headers.get("x-ratelimit-reset") or "").strip()
    try:
        reset_value = float(reset)
    except ValueError:
        return 0.0
    return max(0.0, reset_value if service == "openalex" else reset_value - time.time())


def crawl_service_get(url: str, **kwargs: Any) -> requests.Response:
    """GET one crawl source through the shared per-service cross-process gate."""
    service = crawl_service_from_url(url)
    min_interval = _crawl_policy_value("MIN_INTERVAL_SEC", service, _CRAWL_MIN_INTERVAL_DEFAULTS)
    with crawl_service_slot(service, min_interval_sec=min_interval) as gate:
        response = requests.get(url, **kwargs)
        status_code = int(getattr(response, "status_code", 0) or 0)
        headers = getattr(response, "headers", {}) or {}
        cooldown = 0.0
        reason = ""
        if status_code == 429:
            cooldown = _crawl_retry_after_seconds(response, service) or _crawl_policy_value(
                "RATE_LIMIT_COOLDOWN_SEC", service, _CRAWL_RATE_LIMIT_DEFAULTS
            )
            reason = "http_429"
        elif status_code == 403 and service == "github" and str(headers.get("x-ratelimit-remaining") or "").strip() == "0":
            cooldown = _crawl_retry_after_seconds(response, service) or 60.0
            reason = "github_rate_limit_exhausted"
        elif status_code == 403 and service in _CRAWL_ACCESS_DENIED_DEFAULTS:
            cooldown = _crawl_policy_value(
                "ACCESS_DENIED_COOLDOWN_SEC", service, {**_CRAWL_ACCESS_DENIED_DEFAULTS, "generic": 30.0}
            )
            reason = "http_403"
        if str(headers.get("cf-mitigated") or "").lower() == "challenge":
            cooldown = max(cooldown, _crawl_policy_value("CHALLENGE_COOLDOWN_SEC", service, _CRAWL_CHALLENGE_DEFAULTS))
            reason = reason or "cloudflare_challenge"
        if cooldown > 0:
            gate["cooldown_sec"] = cooldown
            gate["cooldown_reason"] = reason
    return response


@contextmanager
def project_workflow_lease(*, workflow: str, project: str) -> Iterator[None]:
    """Serialize one project's artifact workflow across server processes."""
    workflow = str(workflow or "").strip()
    project = str(project or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", workflow):
        raise ValueError(f"Invalid workflow lock name: {workflow}")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", project):
        raise ValueError(f"Invalid project name for {workflow} lock: {project}")
    FRAMEWORK_LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = FRAMEWORK_LOCKS_DIR / f"{workflow}_{project}.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
