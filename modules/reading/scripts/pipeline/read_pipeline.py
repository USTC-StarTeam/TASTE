from __future__ import annotations

import importlib
import importlib.util
import hashlib
import os
import re
import shutil
import sys
import threading
import time
import traceback
import xml.etree.ElementTree as ET
import datetime as dt
import json
import functools
import subprocess
from concurrent import futures
from html import unescape
from pathlib import Path
from typing import Callable
from urllib.parse import quote, quote_plus
from urllib.parse import urljoin, urlparse
from urllib.parse import parse_qs, unquote

import requests

_SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_ROOT))
importlib.invalidate_caches()
_core_common_module = sys.modules.get("core.common")
if _core_common_module is not None:
    _core_common_path = Path(str(getattr(_core_common_module, "__file__", ""))).resolve(strict=False)
    if _core_common_path != (_SCRIPTS_ROOT / "core" / "common.py").resolve(strict=False):
        sys.modules.pop("core.common", None)
_core_module = sys.modules.get("core")
if _core_module is not None:
    _core_path = str((_SCRIPTS_ROOT / "core").resolve(strict=False))
    _core_package_paths = getattr(_core_module, "__path__", None)
    if _core_package_paths is None:
        sys.modules.pop("core", None)
    else:
        _core_paths = [str(Path(str(path)).resolve(strict=False)) for path in _core_package_paths]
        if _core_path not in _core_paths:
            _core_package_paths.insert(0, _core_path)
try:
    _core_common_spec = importlib.util.find_spec("core.common")
except ModuleNotFoundError:
    _core_common_spec = None
if _core_common_spec is None:
    import types

    _core_path = str((_SCRIPTS_ROOT / "core").resolve(strict=False))
    _core_package = sys.modules.get("core")
    if _core_package is None or getattr(_core_package, "__path__", None) is None:
        _core_package = types.ModuleType("core")
        sys.modules["core"] = _core_package
    _core_package_paths = [
        str(Path(str(path)).resolve(strict=False))
        for path in getattr(_core_package, "__path__", [])
    ]
    _core_package.__path__ = [_core_path, *[path for path in _core_package_paths if path != _core_path]]
    _core_common_spec = importlib.util.spec_from_file_location("core.common", _SCRIPTS_ROOT / "core" / "common.py")
    if _core_common_spec is None or _core_common_spec.loader is None:
        raise ModuleNotFoundError("core.common")
    _core_common_module = importlib.util.module_from_spec(_core_common_spec)
    sys.modules["core.common"] = _core_common_module
    _core_common_spec.loader.exec_module(_core_common_module)

from core.common import (
    best_full_text_title,
    DEFAULT_USER_AGENT,
    FULL_TEXT_MIN_CHARS as CONFIG_FULL_TEXT_MIN_CHARS,
    ServiceCooldownActive,
    config_bool,
    config_float,
    config_int,
    chinese_translation_quality_issue,
    display_paper_title,
    env_bool,
    has_substantive_chinese,
    is_placeholder_paper_title,
    jina_api_key_configured,
    jina_request_headers,
    mark_process_http_blocker,
    missing_official_access_reason,
    process_backend_slot,
    process_blocker,
    response_receipt,
    service_contact_email,
    service_cooldown_remaining,
    service_from_url,
    service_get,
    service_request_slot,
    normalized_paper_title,
    paper_author_family_tokens,
    paper_title_similarity,
    paper_title_tokens,
)
try:
    from acquisition.conference_sources import (
        official_conference_pdf_candidates,
        official_conference_title_search_specs,
    )
except Exception:
    def official_conference_pdf_candidates(paper: dict) -> list[dict]:
        return []

    def official_conference_title_search_specs(paper: dict) -> list[dict[str, str]]:
        return []
from core.common import has_unresolved_prose_latex_markup, read_json, safe_slug, scrub_reading_paths_under, write_json, write_text
from core.common import CACHE_BATCH_TEST_ROOTS, CACHE_RUN_ROOTS, OUTPUT_ROOT, create_run_dir, existing_run_dir, make_reading_paths_relative, refresh_latest_run, resolve_reading_path, ensure_inside_input, ensure_inside_output, ensure_inside_reading, run_dir, validate_run_id
try:
    from orchestration.claude_subagent import (
        article_metadata_markdown_lines,
        build_deep_read_prompt,
        build_deep_read_repair_prompt,
        build_reading_score_prompt,
        run_claude_deep_read,
    )
except ModuleNotFoundError:
    _CLAUDE_SUBAGENT_PATH = Path(__file__).resolve().parents[1] / "orchestration" / "claude_subagent.py"
    _CLAUDE_SUBAGENT_SPEC = importlib.util.spec_from_file_location("reading_claude_subagent_fallback", _CLAUDE_SUBAGENT_PATH)
    if _CLAUDE_SUBAGENT_SPEC is None or _CLAUDE_SUBAGENT_SPEC.loader is None:
        raise
    _CLAUDE_SUBAGENT = importlib.util.module_from_spec(_CLAUDE_SUBAGENT_SPEC)
    _CLAUDE_SUBAGENT_SPEC.loader.exec_module(_CLAUDE_SUBAGENT)
    article_metadata_markdown_lines = _CLAUDE_SUBAGENT.article_metadata_markdown_lines
    build_deep_read_prompt = _CLAUDE_SUBAGENT.build_deep_read_prompt
    build_deep_read_repair_prompt = _CLAUDE_SUBAGENT.build_deep_read_repair_prompt
    build_reading_score_prompt = _CLAUDE_SUBAGENT.build_reading_score_prompt
    run_claude_deep_read = _CLAUDE_SUBAGENT.run_claude_deep_read

try:
    from acquisition.semantic_scholar import semantic_scholar_pdf_candidates
except Exception:  # 兼容旧 PYTHONPATH 或测试环境；Semantic Scholar 只是可选增强源。
    def semantic_scholar_pdf_candidates(paper: dict, **_: object) -> list[dict]:
        return []

try:
    from acquisition.openreview_official import download_openreview_official_pdf, openreview_official_pdf_candidates
except Exception:
    def openreview_official_pdf_candidates(paper: dict, **_: object) -> list[dict]:
        return [{"kind": "openreview_official_pdf", "accepted": False, "reason": "openreview_official_import_failed"}]

    def download_openreview_official_pdf(candidate: dict, target: Path) -> tuple[bool, dict]:
        return False, {"accepted": False, "reason": "openreview_official_import_failed"}


FULL_TEXT_MIN_CHARS = CONFIG_FULL_TEXT_MIN_CHARS
_PDF_CACHE_INDEX: dict[str, list[dict]] | None = None
_DEEP_READ_DIR_CACHE_INDEX: dict[str, list[Path]] | None = None
ARTICLE_CACHE_ROOT = OUTPUT_ROOT.parent.parent / "cache" / "article_cache"
ARTICLE_CACHE_ARTICLES_ROOT = ARTICLE_CACHE_ROOT / "articles"
ARTICLE_CACHE_ALIASES_ROOT = ARTICLE_CACHE_ROOT / "aliases"
_ARTICLE_CACHE_LOCK = threading.Lock()
LogFn = Callable[[str], None]
CancelFn = Callable[[], bool]


def _semantic_scholar_pdf_candidates_for_reading(paper: dict, *, enabled: bool | None = None) -> list[dict]:
    try:
        return semantic_scholar_pdf_candidates(paper, enabled=enabled)
    except TypeError:
        return semantic_scholar_pdf_candidates(paper)


def _raise_if_cancelled(should_cancel: CancelFn) -> None:
    if should_cancel():
        raise ReadingCancelled("Task cancelled by user.")


class ReadingCancelled(RuntimeError):
    pass


READ_USER_AGENT = DEFAULT_USER_AGENT
ACM_BROWSER_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
OPENREVIEW_ATTACHMENT_PDF_NAMES = ("pdf", "originally_submitted_PDF")


def _backend_blocker_receipt(kind: str, query: str, backend: str) -> list[dict]:
    blocker = process_blocker(backend)
    if not blocker:
        return []
    return [{
        "kind": kind,
        "query": query,
        "accepted": False,
        "reason": "skipped_after_prior_backend_access_failure",
        "backend": backend,
        "prior_reason": blocker.get("reason"),
    }]


def _is_openreview_url(url: object) -> bool:
    host = urlparse(str(url or "")).netloc.lower()
    return host == "openreview.net" or host.endswith(".openreview.net")


def _normalize_arxiv_https_url(url: object) -> str:
    text = str(url or "").strip()
    try:
        parsed = urlparse(text)
        host = str(parsed.hostname or "").lower()
    except ValueError:
        return text
    if parsed.scheme.lower() == "http" and host in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
        return parsed._replace(scheme="https").geturl()
    return text


def _is_openreview_challenge_response(response: requests.Response) -> bool:
    parsed = urlparse(str(response.url or ""))
    if not _is_openreview_url(response.url):
        return False
    if parsed.path.startswith("/challenge"):
        return True
    content_type = str(response.headers.get("content-type") or "").lower()
    if "html" not in content_type:
        return False
    text = (response.text or "")[:3000].lower()
    return "complete the check below" in text or "verifying your browser" in text or "challenge" in parsed.query.lower()


def _is_openreview_static_pdf_url(url: object) -> bool:
    parsed = urlparse(str(url or ""))
    return _is_openreview_url(url) and bool(re.fullmatch(r"/pdf/[A-Fa-f0-9]{16,}\.pdf", parsed.path))


_OPENREVIEW_BROWSER_ACCESS_BARRIER_LOCK = threading.Lock()
_OPENREVIEW_BROWSER_ACCESS_BARRIER: dict[str, object] | None = None
_OPENREVIEW_DERIVED_AUTH_LOCK = threading.Lock()
_OPENREVIEW_DERIVED_AUTH_CACHE: tuple[dict[str, str], dict[str, object]] | None = None


def _is_openreview_browser_access_barrier(receipt: dict[str, object]) -> bool:
    reason = str(receipt.get("reason") or "")
    return reason.startswith("openreview_login_page_http_") or reason in {
        "openreview_login_page_challenge",
        "openreview_login_page_network_error",
    }


def _record_openreview_browser_access_barrier(receipt: dict[str, object]) -> None:
    if not _is_openreview_browser_access_barrier(receipt):
        return
    with _OPENREVIEW_BROWSER_ACCESS_BARRIER_LOCK:
        global _OPENREVIEW_BROWSER_ACCESS_BARRIER
        _OPENREVIEW_BROWSER_ACCESS_BARRIER = {
            "reason": receipt.get("reason"),
            "status_code": receipt.get("status_code"),
            "final_url": receipt.get("final_url"),
            "message_zh": receipt.get("message_zh"),
            "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }


def _openreview_browser_access_barrier_receipt(url: str, credential_receipt: dict[str, object]) -> dict[str, object] | None:
    with _OPENREVIEW_BROWSER_ACCESS_BARRIER_LOCK:
        cached = dict(_OPENREVIEW_BROWSER_ACCESS_BARRIER or {})
    if not cached:
        return None
    return {
        "accepted": False,
        "url": url,
        "method": "playwright_openreview_browser_login",
        "reason": "skipped_due_to_prior_openreview_login_access_blocker",
        "credentials": credential_receipt,
        "prior_openreview_login_access_blocker": cached,
        "message_zh": "同一进程已确认 OpenReview 登录入口当前不可达；保留其他 PDF/API 候选尝试，避免重复打开浏览器等待。",
    }


def _openreview_client_auth_headers() -> tuple[dict[str, str], dict[str, object]]:
    global _OPENREVIEW_DERIVED_AUTH_CACHE
    with _OPENREVIEW_DERIVED_AUTH_LOCK:
        if _OPENREVIEW_DERIVED_AUTH_CACHE is not None:
            headers, receipt = _OPENREVIEW_DERIVED_AUTH_CACHE
            return dict(headers), dict(receipt)
    username = str(os.environ.get("OPENREVIEW_USERNAME") or os.environ.get("OPENREVIEW_EMAIL") or "").strip()
    password = str(os.environ.get("OPENREVIEW_PASSWORD") or "").strip()
    receipt: dict[str, object] = {
        "configured": False,
        "source": "openreview_py_client",
        "username_set": bool(username),
        "password_set": bool(password),
        "redacted": True,
    }
    if not username or not password:
        receipt["reason"] = "missing_openreview_credentials"
        result = ({}, receipt)
        with _OPENREVIEW_DERIVED_AUTH_LOCK:
            _OPENREVIEW_DERIVED_AUTH_CACHE = result
        return dict(result[0]), dict(result[1])
    try:
        import openreview  # type: ignore
    except Exception as exc:
        receipt.update({"reason": "missing_openreview_py", "error": exc.__class__.__name__})
        result = ({}, receipt)
        with _OPENREVIEW_DERIVED_AUTH_LOCK:
            _OPENREVIEW_DERIVED_AUTH_CACHE = result
        return dict(result[0]), dict(result[1])

    attempts: list[dict[str, object]] = []
    for api_version, baseurl in [(2, "https://api2.openreview.net"), (1, "https://api.openreview.net")]:
        try:
            with service_request_slot("openreview") as gate:
                try:
                    factory = openreview.api.OpenReviewClient if api_version == 2 else openreview.Client
                    client = factory(baseurl=baseurl, username=username, password=password)
                except Exception as exc:
                    message = str(exc)
                    if "403" in message or "Forbidden" in message:
                        gate.update({"status_code": 403, "cooldown_reason": "openreview_api_login_forbidden"})
                    elif "429" in message or "Too Many Requests" in message:
                        gate.update({"status_code": 429, "cooldown_reason": "openreview_api_login_rate_limited"})
                    raise
        except ServiceCooldownActive as exc:
            attempts.append({
                "api_version": api_version,
                "baseurl": baseurl,
                "accepted": False,
                "reason": "openreview_service_cooldown_active",
                "cooldown_remaining_sec": round(exc.remaining, 3),
                "cooldown_reason": exc.reason,
            })
            break
        except Exception as exc:
            message = str(exc)[:300]
            reason = "openreview_api_login_forbidden" if ("403" in message or "Forbidden" in message) else "openreview_api_login_failed"
            attempts.append({"api_version": api_version, "baseurl": baseurl, "accepted": False, "reason": reason, "error": exc.__class__.__name__, "message": message})
            continue
        auth_value = ""
        for header_source in [getattr(client, "headers", None), getattr(getattr(client, "session", None), "headers", None), getattr(getattr(client, "requests_session", None), "headers", None)]:
            if isinstance(header_source, dict):
                auth_value = str(header_source.get("Authorization") or header_source.get("authorization") or "").strip()
                if auth_value:
                    break
        token = ""
        for attr in ["token", "access_token", "accessToken"]:
            value = str(getattr(client, attr, "") or "").strip()
            if value:
                token = value
                break
        if auth_value:
            headers = {"Authorization": auth_value}
        elif token:
            headers = {"Authorization": token if re.match(r"^(?:Bearer|Basic)\s+", token, flags=re.I) else "Bearer " + token}
        else:
            attempts.append({"api_version": api_version, "baseurl": baseurl, "accepted": False, "reason": "openreview_client_auth_token_missing"})
            continue
        receipt.update({
            "configured": True,
            "api_version": api_version,
            "baseurl": baseurl,
            "attempts": attempts,
            "reason": "openreview_client_auth_header",
        })
        result = (headers, receipt)
        with _OPENREVIEW_DERIVED_AUTH_LOCK:
            _OPENREVIEW_DERIVED_AUTH_CACHE = result
        return dict(result[0]), dict(result[1])
    receipt.update({"reason": "openreview_client_auth_unavailable", "attempts": attempts})
    result = ({}, receipt)
    with _OPENREVIEW_DERIVED_AUTH_LOCK:
        _OPENREVIEW_DERIVED_AUTH_CACHE = result
    return dict(result[0]), dict(result[1])


def _openreview_http_auth_headers() -> tuple[dict[str, str], dict[str, object]]:
    headers: dict[str, str] = {}
    receipt: dict[str, object] = {
        "configured": False,
        "cookie_configured": False,
        "authorization_configured": False,
        "derived_authorization_configured": False,
        "source_env": [],
        "policy": "OpenReview HTTP credential values are read only from process environment; receipts record only source env names and redacted configuration flags.",
    }
    cookie_sources = ["READING_OPENREVIEW_COOKIE", "OPENREVIEW_COOKIE"]
    authorization_sources = [
        "READING_OPENREVIEW_AUTHORIZATION",
        "OPENREVIEW_AUTHORIZATION",
    ]
    token_sources = [
        "READING_OPENREVIEW_TOKEN",
        "OPENREVIEW_TOKEN",
    ]
    source_env: list[str] = []
    for key in cookie_sources:
        value = str(os.environ.get(key) or "").strip()
        if value:
            headers["Cookie"] = value
            receipt["cookie_configured"] = True
            source_env.append(key)
            break
    for key in authorization_sources:
        value = str(os.environ.get(key) or "").strip()
        if value:
            headers["Authorization"] = value
            receipt["authorization_configured"] = True
            source_env.append(key)
            break
    if "Authorization" not in headers:
        for key in token_sources:
            value = str(os.environ.get(key) or "").strip()
            if value:
                headers["Authorization"] = value if re.match(r"^(?:Bearer|Basic)\s+", value, flags=re.I) else "Bearer " + value
                receipt["authorization_configured"] = True
                source_env.append(key)
                break
    if not headers:
        derived_headers, derived_receipt = _openreview_client_auth_headers()
        receipt["derived_auth"] = derived_receipt
        if derived_headers:
            headers.update(derived_headers)
            receipt["derived_authorization_configured"] = True
            source_env.extend([key for key in ["OPENREVIEW_USERNAME", "OPENREVIEW_PASSWORD"] if str(os.environ.get(key) or "").strip()])
    receipt["configured"] = bool(headers)
    receipt["source_env"] = source_env
    if headers:
        receipt["redacted"] = True
    return headers, receipt


def _openreview_request_headers(url: str, base_headers: dict[str, str]) -> tuple[dict[str, str], dict[str, object]]:
    request_headers = dict(base_headers)
    if not _is_openreview_url(url):
        return request_headers, {}
    auth_headers, auth_receipt = _openreview_http_auth_headers()
    if auth_headers:
        request_headers.update(auth_headers)
    return request_headers, auth_receipt


def _openreview_browser_login_enabled() -> bool:
    env_value = str(os.environ.get("READING_OPENREVIEW_BROWSER_LOGIN_FALLBACK") or "").strip()
    if env_value:
        return env_bool("READING_OPENREVIEW_BROWSER_LOGIN_FALLBACK", False)
    username, password, _credential_receipt = _openreview_browser_credentials_status()
    if username and password:
        return True
    return env_bool(
        "READING_OPENREVIEW_BROWSER_LOGIN_FALLBACK",
        config_bool("openreview.browser_login_pdf_fallback", False),
    )


def _openreview_browser_credentials_status() -> tuple[str, str, dict[str, object]]:
    username = str(os.environ.get("OPENREVIEW_USERNAME") or os.environ.get("OPENREVIEW_EMAIL") or "").strip()
    password = str(os.environ.get("OPENREVIEW_PASSWORD") or "").strip()
    return username, password, {
        "username_set": bool(username),
        "password_set": bool(password),
        "source_env": [
            key
            for key in ["OPENREVIEW_USERNAME", "OPENREVIEW_EMAIL", "OPENREVIEW_PASSWORD"]
            if str(os.environ.get(key) or "").strip()
        ],
        "redacted": True,
    }


def _openreview_cookie_names(cookies: list[dict[str, object]]) -> list[str]:
    names = sorted({str(cookie.get("name") or "") for cookie in cookies if str(cookie.get("name") or "")})
    return names[:20]


def _first_visible_locator(page: object, selectors: list[str]) -> object | None:
    for selector in selectors:
        try:
            matches = page.locator(selector)
            if matches.count() <= 0:
                continue
            locator = matches.first
            if locator.is_visible(timeout=1500):
                return locator
        except Exception:
            continue
    return None


def _download_openreview_pdf_with_browser_login(
    url: str,
    target: Path,
    *,
    after_direct_failure: bool = False,
) -> tuple[bool, dict[str, object]]:
    remaining = service_cooldown_remaining("openreview")
    if remaining > 0 and not after_direct_failure:
        return False, {
            "accepted": False,
            "url": url,
            "method": "playwright_openreview_browser_login",
            "reason": "skipped_due_to_openreview_service_cooldown",
            "cooldown_remaining_sec": remaining,
            "message_zh": "OpenReview 当前处于访问冷却期，本轮不再启动浏览器请求。",
        }
    try:
        with service_request_slot("openreview", allow_during_cooldown=after_direct_failure) as gate:
            downloaded, receipt = _download_openreview_pdf_with_browser_login_unlocked(url, target)
            reason = str(receipt.get("reason") or "")
            if reason.startswith("openreview_login_page_http_"):
                gate.update({"status_code": 403, "cooldown_reason": reason})
            elif reason in {"openreview_login_page_challenge", "openreview_login_page_network_error"}:
                gate.update({
                    "cooldown_sec": config_float("http.access_denied_cooldown_sec.openreview", 900.0),
                    "cooldown_reason": reason,
                })
            return downloaded, receipt
    except ServiceCooldownActive as exc:
        return False, {
            "accepted": False,
            "url": url,
            "method": "playwright_openreview_browser_login",
            "reason": "skipped_due_to_openreview_service_cooldown",
            "cooldown_remaining_sec": round(exc.remaining, 3),
            "message_zh": "OpenReview 当前处于访问冷却期，本轮不再启动浏览器请求。",
        }


def _download_openreview_pdf_with_browser_login_unlocked(url: str, target: Path) -> tuple[bool, dict[str, object]]:
    receipt: dict[str, object] = {
        "accepted": False,
        "url": url,
        "method": "playwright_openreview_browser_login",
        "policy": "Uses OpenReview username/password only from local environment, records only redacted flags and cookie names, and still requires downstream PDF text identity checks.",
    }
    if not _is_openreview_url(url):
        receipt["reason"] = "not_openreview_url"
        return False, receipt
    if not _openreview_browser_login_enabled():
        receipt["reason"] = "openreview_browser_login_fallback_disabled"
        return False, receipt
    username, password, credential_receipt = _openreview_browser_credentials_status()
    receipt["credentials"] = credential_receipt
    if not username or not password:
        receipt.update({
            "reason": "missing_openreview_browser_login_credentials",
            "message_zh": "未配置 OpenReview 用户名或密码，无法使用浏览器登录态下载 OpenReview PDF。",
        })
        return False, receipt
    cached_barrier_receipt = _openreview_browser_access_barrier_receipt(url, credential_receipt)
    if cached_barrier_receipt is not None:
        return False, cached_barrier_receipt
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        receipt.update({
            "reason": "missing_playwright",
            "error": exc.__class__.__name__,
            "message": str(exc)[:240],
            "message_zh": "当前 Python 环境缺少 Playwright，无法模拟浏览器登录 OpenReview。",
        })
        return False, receipt

    note_id = _openreview_note_id_from_url(url)
    redirect_path = f"/forum?id={quote(note_id, safe='')}" if note_id else "/"
    login_url = "https://openreview.net/login?redirect=" + quote(redirect_path, safe="")
    browser = None
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                accept_downloads=True,
                user_agent=ACM_BROWSER_USER_AGENT,
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            page = context.new_page()
            page.set_default_timeout(45000)
            login_response = page.goto(login_url, wait_until="domcontentloaded", timeout=60000)
            login_status = login_response.status if login_response is not None else None
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except PlaywrightTimeoutError:
                pass
            try:
                login_body_preview = page.locator("body").inner_text(timeout=5000)[:500]
            except Exception:
                login_body_preview = ""
            login_body_lower = login_body_preview.lower()
            if login_status in {401, 403} or "403 forbidden" in login_body_lower:
                receipt.update({
                    "reason": f"openreview_login_page_http_{login_status or 403}",
                    "login_url": login_url,
                    "final_url": page.url,
                    "status_code": login_status,
                    "message_zh": "OpenReview 登录页在当前机器返回访问限制，浏览器登录态兜底无法进入表单。",
                })
                _record_openreview_browser_access_barrier(receipt)
                return False, receipt
            if "complete the check below" in login_body_lower or "verifying your browser" in login_body_lower:
                receipt.update({
                    "reason": "openreview_login_page_challenge",
                    "login_url": login_url,
                    "final_url": page.url,
                    "status_code": login_status,
                    "message_zh": "OpenReview 登录页要求浏览器校验，当前无可自动完成的登录表单。",
                })
                _record_openreview_browser_access_barrier(receipt)
                return False, receipt
            try:
                page.wait_for_selector(
                    "input[type='email'], input[placeholder='Email'], input[type='password']",
                    timeout=10000,
                )
            except PlaywrightTimeoutError:
                pass
            email_input = _first_visible_locator(page, [
                "input[name='email']",
                "input[type='email']",
                "input#email",
                "input[placeholder='Email']",
                "input[placeholder*='Email' i]",
                "input[aria-label*='Email' i]",
            ])
            password_input = _first_visible_locator(page, [
                "input[name='password']",
                "input[type='password']",
                "input#password",
                "input[placeholder='Password']",
                "input[placeholder*='Password' i]",
                "input[aria-label*='Password' i]",
            ])
            if email_input is None or password_input is None:
                receipt.update({
                    "reason": "openreview_login_form_not_found",
                    "login_url": login_url,
                    "final_url": page.url,
                })
                return False, receipt
            email_input.fill(username)
            password_input.fill(password)
            submit = _first_visible_locator(page, [
                "button:has-text('Login to OpenReview')",
                "button:has-text('Log in')",
                "button:has-text('Login')",
                "button[type='submit']",
                "input[type='submit']",
            ])
            if submit is None:
                receipt.update({
                    "reason": "openreview_login_submit_not_found",
                    "login_url": login_url,
                    "final_url": page.url,
                })
                return False, receipt
            try:
                with page.expect_navigation(wait_until="domcontentloaded", timeout=45000):
                    submit.click()
            except PlaywrightTimeoutError:
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except PlaywrightTimeoutError:
                    pass
            cookie_names: list[str] = []
            for _ in range(40):
                cookie_names = _openreview_cookie_names(context.cookies("https://openreview.net"))
                if "openreview.accessToken" in cookie_names or "openreview.refreshToken" in cookie_names:
                    break
                time.sleep(0.5)
            cookies = context.cookies("https://openreview.net")
            cookie_names = _openreview_cookie_names(cookies)
            receipt["browser_login"] = {
                "login_url": login_url,
                "final_url": page.url,
                "cookie_names": cookie_names,
                "access_cookie_set": "openreview.accessToken" in cookie_names,
                "refresh_cookie_set": "openreview.refreshToken" in cookie_names,
            }
            if "openreview.accessToken" not in cookie_names and "openreview.refreshToken" not in cookie_names:
                receipt.update({
                    "reason": "openreview_login_no_auth_cookie",
                    "message_zh": "浏览器提交登录后没有拿到 OpenReview 登录 Cookie。",
                })
                return False, receipt
            api_response = context.request.get(url, timeout=60000, headers={"Accept": "application/pdf,*/*"})
            body = api_response.body()
            request_receipt = {
                "status_code": api_response.status,
                "content_type": api_response.headers.get("content-type", ""),
                "bytes": len(body or b""),
                "is_pdf_magic": bool(body.startswith(b"%PDF")),
            }
            receipt["context_request"] = request_receipt
            if api_response.status == 200 and body.startswith(b"%PDF"):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(body)
                receipt.update({"accepted": True, "selected": "context_request"})
                return True, receipt

            try:
                with page.expect_download(timeout=60000) as download_info:
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    except PlaywrightError as exc:
                        if "Download is starting" not in str(exc):
                            raise
                download = download_info.value
                download.save_as(str(target))
                pdf_magic = target.exists() and target.read_bytes()[:4] == b"%PDF"
                receipt["page_download"] = {
                    "suggested_filename": download.suggested_filename,
                    "bytes": target.stat().st_size if target.exists() else 0,
                    "is_pdf_magic": pdf_magic,
                }
                if pdf_magic:
                    receipt.update({"accepted": True, "selected": "page_download"})
                    return True, receipt
            except Exception as exc:
                receipt["page_download"] = {
                    "accepted": False,
                    "error": exc.__class__.__name__,
                    "message": str(exc)[:240],
                }
            receipt.setdefault("reason", "openreview_browser_login_download_not_pdf")
            return False, receipt
    except Exception as exc:
        message = str(exc)[:300]
        network_error = "net::err_" in message.lower()
        receipt.update({
            "reason": "openreview_login_page_network_error" if network_error else "openreview_browser_login_failed",
            "error": exc.__class__.__name__,
            "message": message,
        })
        if network_error:
            receipt["message_zh"] = "OpenReview 登录页在当前机器发生网络级访问失败，本进程保留其他全文来源并停止重复打开登录页。"
            _record_openreview_browser_access_barrier(receipt)
        return False, receipt
    finally:
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass


def _download_pdf_with_receipt(url: str, target: Path) -> tuple[bool, dict[str, object]]:
    url = _normalize_arxiv_https_url(url)
    receipt: dict[str, object] = {"url": url, "attempts": []}
    if not url or not url.startswith("http"):
        receipt.update({"accepted": False, "reason": "missing_pdf_url"})
        return False, receipt
    service_name = service_from_url(url)
    cooldown_remaining = service_cooldown_remaining(service_name)
    if cooldown_remaining > 0:
        receipt.update({
            "accepted": False,
            "reason": "skipped_due_to_active_challenge_cooldown",
            "service": service_name,
            "cooldown_remaining_sec": cooldown_remaining,
            "message_zh": "该服务仍处于访问冷却期；本轮跳过请求，避免继续触发站点防护。",
        })
        return False, receipt
    request_headers = {"Accept": "application/pdf,*/*"}
    if "dl.acm.org" in url.lower():
        request_headers["User-Agent"] = ACM_BROWSER_USER_AGENT
        receipt["user_agent_mode"] = "browser_like_for_acm_official_pdf"
    elif service_name == "biorxiv" and "/content/" in url.lower():
        request_headers.update({
            "User-Agent": ACM_BROWSER_USER_AGENT,
            "Accept": "application/pdf,application/octet-stream;q=0.9,text/html;q=0.8,*/*;q=0.5",
            "Accept-Language": "en-US,en;q=0.9",
        })
        receipt["user_agent_mode"] = "browser_like_for_biorxiv_official_pdf"
    request_headers, openreview_auth_receipt = _openreview_request_headers(url, request_headers)
    if openreview_auth_receipt:
        receipt["openreview_http_auth"] = openreview_auth_receipt
    max_attempts = 5 if any(host in url.lower() for host in ["openaccess.thecvf.com", "ecva.net"]) else 3
    for attempt_index in range(max_attempts):
        try:
            response = service_get(url, timeout=45, headers=request_headers)
            content_type = response.headers.get("content-type", "").lower()
            attempt = {
                "attempt": attempt_index + 1,
                "is_pdf_magic": response.content.startswith(b"%PDF"),
                **response_receipt(response),
            }
            receipt["attempts"].append(attempt)
            if response.status_code == 200 and ("pdf" in content_type or response.content.startswith(b"%PDF")):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(response.content)
                receipt.update({"accepted": True, "selected": attempt})
                return True, receipt
            if _is_openreview_challenge_response(response):
                browser_downloaded, browser_receipt = _download_openreview_pdf_with_browser_login(
                    url,
                    target,
                    after_direct_failure=True,
                )
                receipt["openreview_browser_login"] = browser_receipt
                if browser_downloaded:
                    receipt.update({"accepted": True, "selected": attempt, "reason": "openreview_browser_login_pdf"})
                    return True, receipt
                receipt.update({"accepted": False, "reason": "openreview_challenge", "selected": attempt})
                return False, receipt
            if response.status_code in {403, 404, 410}:
                headers_subset = attempt.get("headers_subset") if isinstance(attempt.get("headers_subset"), dict) else {}
                if (
                    response.status_code == 403
                    and service_name == "biorxiv"
                    and (
                        attempt.get("challenge_type") == "cloudflare"
                        or str(headers_subset.get("cf-mitigated") or "").lower() == "challenge"
                    )
                ):
                    receipt.update({"accepted": False, "reason": "biorxiv_cloudflare_challenge", "selected": attempt})
                    return False, receipt
                if response.status_code == 403 and "dl.acm.org" in url.lower() and attempt_index < 2:
                    time.sleep(2.0 + attempt_index)
                    continue
                if response.status_code == 403 and service_name == "biorxiv" and attempt_index < max_attempts - 1:
                    time.sleep(5.0 + attempt_index * 5.0)
                    continue
                if response.status_code == 403 and _is_openreview_url(url):
                    browser_downloaded, browser_receipt = _download_openreview_pdf_with_browser_login(
                        url,
                        target,
                        after_direct_failure=True,
                    )
                    receipt["openreview_browser_login"] = browser_receipt
                    if browser_downloaded:
                        receipt.update({"accepted": True, "selected": attempt, "reason": "openreview_browser_login_pdf"})
                        return True, receipt
                receipt.update({"accepted": False, "reason": f"http_{response.status_code}"})
                return False, receipt
            if response.status_code == 429:
                receipt.update({"accepted": False, "reason": "http_429_rate_limited", "retry_after": response.headers.get("retry-after") or ""})
                return False, receipt
        except ServiceCooldownActive as exc:
            receipt["attempts"].append({
                "attempt": attempt_index + 1,
                "accepted": False,
                "reason": "service_cooldown_active",
                "service": exc.service,
                "cooldown_remaining_sec": round(exc.remaining, 3),
            })
            receipt.update({
                "accepted": False,
                "reason": "skipped_due_to_active_challenge_cooldown",
                "service": exc.service,
                "cooldown_remaining_sec": round(exc.remaining, 3),
            })
            return False, receipt
        except Exception as exc:
            receipt["attempts"].append({"attempt": attempt_index + 1, "accepted": False, "error": exc.__class__.__name__, "message": str(exc)[:300]})
        time.sleep(min(8.0, 0.8 * (attempt_index + 1)))
    receipt.setdefault("accepted", False)
    receipt.setdefault("reason", "download_failed_or_not_pdf")
    return False, receipt


def _request_json(url: str, *, params: dict[str, str] | None = None, timeout: int = 30) -> tuple[dict, dict]:
    receipt: dict[str, object] = {"url": url, "params": params or {}}
    service_name = service_from_url(url)
    cooldown_remaining = service_cooldown_remaining(service_name)
    if cooldown_remaining > 0:
        receipt.update({
            "accepted": False,
            "reason": "skipped_due_to_active_challenge_cooldown",
            "service": service_name,
            "cooldown_remaining_sec": cooldown_remaining,
            "message_zh": "该服务仍处于访问冷却期；本轮跳过 JSON 请求。",
        })
        return {}, receipt
    request_headers, openreview_auth_receipt = _openreview_request_headers(url, {"Accept": "application/json"})
    if openreview_auth_receipt:
        receipt["openreview_http_auth"] = openreview_auth_receipt
    try:
        response = service_get(url, params=params or {}, timeout=timeout, headers=request_headers)
    except ServiceCooldownActive as exc:
        receipt.update({
            "accepted": False,
            "reason": "service_cooldown_active",
            "service": exc.service,
            "cooldown_remaining_sec": round(exc.remaining, 3),
        })
        return {}, receipt
    except Exception as exc:
        receipt.update({"accepted": False, "error": exc.__class__.__name__})
        return {}, receipt
    receipt.update(response_receipt(response))
    if response.status_code != 200:
        receipt["accepted"] = False
        return {}, receipt
    try:
        payload = response.json()
    except Exception as exc:
        receipt.update({"accepted": False, "error": exc.__class__.__name__})
        return {}, receipt
    receipt["accepted"] = isinstance(payload, dict)
    return payload if isinstance(payload, dict) else {}, receipt


def _request_html(url: str, *, timeout: int = 30) -> tuple[str, str, dict]:
    receipt: dict[str, object] = {"url": url}
    if not url or not url.startswith("http"):
        receipt.update({"accepted": False, "reason": "missing_url"})
        return "", "", receipt
    service_name = service_from_url(url)
    cooldown_remaining = service_cooldown_remaining(service_name)
    if cooldown_remaining > 0:
        receipt.update({
            "accepted": False,
            "reason": "skipped_due_to_active_challenge_cooldown",
            "service": service_name,
            "cooldown_remaining_sec": cooldown_remaining,
            "message_zh": "该服务仍处于访问冷却期；本轮跳过 HTML 请求。",
        })
        return url, "", receipt
    request_headers, openreview_auth_receipt = _openreview_request_headers(url, {"Accept": "text/html,application/xhtml+xml,*/*"})
    if openreview_auth_receipt:
        receipt["openreview_http_auth"] = openreview_auth_receipt
    try:
        response = service_get(url, timeout=timeout, headers=request_headers)
    except ServiceCooldownActive as exc:
        receipt.update({
            "accepted": False,
            "reason": "service_cooldown_active",
            "service": exc.service,
            "cooldown_remaining_sec": round(exc.remaining, 3),
        })
        return "", "", receipt
    except Exception as exc:
        receipt.update({"accepted": False, "error": exc.__class__.__name__})
        return "", "", receipt
    content_type = str(response.headers.get("content-type") or "").lower()
    receipt.update(response_receipt(response), content_type=content_type, resolved_url=response.url)
    if response.status_code != 200:
        receipt["accepted"] = False
        return response.url, "", receipt
    text = response.text or ""
    if "html" not in content_type and "<html" not in text[:1000].lower() and "<!doctype" not in text[:1000].lower():
        receipt.update({"accepted": False, "reason": "not_html"})
        return response.url, "", receipt
    receipt["accepted"] = True
    return response.url, text, receipt


def _copy_pdf(source: Path, target: Path) -> bool:
    try:
        source = ensure_inside_reading(source, label="PDF cache source")
        if not source.exists() or source.suffix.lower() != ".pdf":
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        return target.exists() and target.stat().st_size > 1024
    except Exception:
        return False


def _normalized_title_key(value: object) -> str:
    return " ".join(sorted(paper_title_tokens(value)))


def _content_value(value: object) -> object:
    if isinstance(value, dict) and "value" in value:
        return value.get("value")
    return value


def _same_paper_identity_ok(
    paper: dict,
    *,
    candidate_title: object = "",
    candidate_authors: object = None,
    candidate_doi: object = "",
) -> bool:
    expected_doi = _doi_from_paper(paper)
    found_doi = _doi_from_text(candidate_doi)
    if expected_doi and found_doi and expected_doi == found_doi:
        return True
    title = str(paper.get("title") or "").strip()
    if not title or not str(candidate_title or "").strip():
        return False
    similarity = paper_title_similarity(title, candidate_title)
    expected_authors = paper_author_family_tokens(paper.get("authors"))
    found_authors = paper_author_family_tokens(candidate_authors)
    if expected_authors:
        overlap = expected_authors & found_authors
        return bool(
            (similarity >= 0.82 and overlap)
            or (similarity >= 0.78 and len(overlap) >= 2)
            or (similarity >= 0.70 and len(overlap) >= 4)
        )
    return similarity >= 0.92


def _url_identity_key(value: object) -> str:
    text = str(value or "").strip()
    if not text.startswith("http"):
        return ""
    parsed = urlparse(text)
    host = (parsed.netloc or "").lower()
    path = unquote(parsed.path or "").rstrip("/")
    query = ("?" + parsed.query) if parsed.query else ""
    return f"{host}{path}{query}"


def _paper_declared_landing_urls(paper: dict) -> list[str]:
    urls: list[str] = []
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    for value in [
        paper.get("html_url"),
        paper.get("url"),
        paper.get("abs_url"),
        metadata.get("html_url"),
        metadata.get("url"),
        metadata.get("abs_url"),
        metadata.get("conference_url"),
        metadata.get("conference_landing_url"),
    ]:
        url = str(value or "").strip()
        if url.startswith("http") and url not in urls:
            urls.append(url)
    return urls


def _matches_declared_landing_url(paper: dict, *urls: object) -> bool:
    declared = {_url_identity_key(url) for url in _paper_declared_landing_urls(paper)}
    declared.discard("")
    if not declared:
        return False
    return any(_url_identity_key(url) in declared for url in urls if _url_identity_key(url))


def _best_full_text_title(paper: dict, extracted_text: str) -> str:
    return best_full_text_title(paper, extracted_text)


def _pdf_text_identity_ok(paper: dict, extracted_text: str) -> bool:
    return bool(_best_full_text_title(paper, extracted_text))


def _apply_verified_full_text_title(paper: dict, verified_title: object) -> str:
    title = display_paper_title(verified_title)
    if not title or is_placeholder_paper_title(title):
        return ""
    original_title = display_paper_title(paper.get("title"))
    metadata = paper.setdefault("metadata", {})
    if original_title and normalized_paper_title(original_title) != normalized_paper_title(title):
        metadata.setdefault("original_bibliographic_title", original_title)
        metadata["title_correction_reason"] = "verified_full_text_title"
    metadata["reading_verified_full_text_title"] = title
    paper["title"] = title
    return title


def _verify_packet_full_text_title(paper: dict, packet: dict) -> str:
    if not isinstance(packet, dict) or not packet.get("full_text_available"):
        return ""
    verified_title = str(packet.get("verified_full_text_title") or "").strip()
    if not verified_title:
        text_path = _packet_path(packet, "text_path")
        if text_path is not None and text_path.is_file():
            try:
                verified_title = _best_full_text_title(
                    paper,
                    text_path.read_text(encoding="utf-8", errors="replace"),
                )
            except OSError:
                verified_title = ""
    if not verified_title:
        return ""
    verified_title = _apply_verified_full_text_title(paper, verified_title)
    if verified_title:
        packet["verified_full_text_title"] = verified_title
        packet["title"] = verified_title
    return verified_title


def _reading_acquisition_services() -> dict[str, Callable]:
    return {
        "download_first_readable_pdf": _download_first_readable_pdf,
        "openalex_pdf_candidates": _openalex_pdf_candidates,
        "same_paper_landing_page_candidates": _same_paper_landing_page_candidates,
    }


def _openreview_pdf_url(paper: dict) -> str:
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    for value in [
        paper.get("openreview_id"),
        paper.get("openreview_note_id"),
        paper.get("openreview_forum_url"),
        paper.get("openreview_url"),
        paper.get("openreview_pdf_url"),
        paper.get("forum"),
        paper.get("note_id"),
        paper.get("paper_url"),
        paper.get("paper_pdf_url"),
        paper.get("url"),
        paper.get("html_url"),
        paper.get("abs_url"),
        paper.get("pdf_url"),
        metadata.get("openreview_id"),
        metadata.get("openreview_note_id"),
        metadata.get("openreview_forum"),
        metadata.get("openreview_forum_url"),
        metadata.get("openreview_url"),
        metadata.get("openreview_pdf_url"),
        metadata.get("forum"),
        metadata.get("note_id"),
        metadata.get("paper_url"),
        metadata.get("paper_pdf_url"),
    ]:
        text = str(value or "").strip()
        match = re.search(r"openreview\.net/(?:forum|pdf|attachment)\?id=([^&#\s]+)", text)
        if match:
            return f"https://openreview.net/pdf?id={match.group(1).strip()}"
        if re.fullmatch(r"[A-Za-z0-9_-]{8,}", text):
            return f"https://openreview.net/pdf?id={text}"
    return ""


def _openreview_anonymous_http_enabled() -> bool:
    return env_bool(
        "READING_OPENREVIEW_ALLOW_ANONYMOUS_HTTP",
        config_bool("openreview.allow_anonymous_http", True),
    )


def _openreview_note_id_from_url(value: object) -> str:
    text = str(value or "").strip()
    match = re.search(r"openreview\.net/(?:forum|pdf|attachment)\?id=([^&#\s]+)", text)
    if match:
        return match.group(1).strip()
    return ""


def _openreview_note_ids_from_text(value: object) -> set[str]:
    text = str(value or "")
    text = unescape(text).replace("\\u002F", "/").replace("\\/", "/")
    out: set[str] = set()
    for match in re.finditer(r"openreview\.net/(?:forum|pdf|attachment)\?id=([^&#\"'\s<>\\]+)", text):
        out.add(match.group(1).strip())
    return {item for item in out if item}


def _openreview_title_pdf_candidates(paper: dict, limit: int = 5) -> list[dict]:
    title = str(paper.get("title") or "").strip()
    if len(title.split()) < 3:
        return []
    candidates: list[dict] = []
    attempts: list[dict] = []
    endpoints = [
        "https://api2.openreview.net/notes",
        "https://api.openreview.net/notes",
    ]
    for endpoint in endpoints:
        payload, receipt = _request_json(endpoint, params={"content.title": title, "limit": str(limit)}, timeout=35)
        attempts.append({"kind": "openreview_title_search", "endpoint": endpoint, **receipt})
        notes = payload.get("notes") if isinstance(payload.get("notes"), list) else []
        for note in notes:
            if not isinstance(note, dict):
                continue
            content = note.get("content") if isinstance(note.get("content"), dict) else {}
            candidate_title = _content_value(content.get("title"))
            candidate_authors = _content_value(content.get("authors"))
            note_id = str(note.get("id") or "").strip()
            if not note_id or not _same_paper_identity_ok(paper, candidate_title=candidate_title, candidate_authors=candidate_authors):
                continue
            candidates.append({
                "kind": "openreview_title_verified_pdf",
                "pdf_url": f"https://openreview.net/pdf?id={note_id}",
                "openreview_id": note_id,
                "openreview_title": candidate_title,
                "openreview_endpoint": endpoint,
                "accepted": True,
            })
            return candidates
    return candidates or attempts


def _neurips_pdf_url_from_abstract(value: object) -> str:
    url = str(value or "").strip()
    match = re.search(
        r"https?://(?:papers\.nips\.cc|proceedings\.neurips\.cc)/paper_files/paper/(\d{4})/hash/([A-Za-z0-9]+)-Abstract-([^\"'<>\s/]+)\.html",
        url,
    )
    if not match:
        return ""
    year, paper_hash, track_suffix = match.group(1), match.group(2), match.group(3)
    return f"https://proceedings.neurips.cc/paper_files/paper/{year}/file/{paper_hash}-Paper-{track_suffix}.pdf"


def _pdf_links_from_html_page(url: str) -> list[dict]:
    if not url.startswith("http"):
        return []
    lowered = url.lower()
    if "papers.nips.cc" not in lowered and "proceedings.neurips.cc" not in lowered:
        return []
    try:
        response = service_get(url, timeout=30)
    except Exception as exc:
        return [{"kind": "html_pdf_link_scan", "source_url": url, "accepted": False, "error": exc.__class__.__name__}]
    if response.status_code != 200:
        return [{"kind": "html_pdf_link_scan", "source_url": url, "accepted": False, "status_code": response.status_code}]
    links = re.findall(r'href=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']', response.text, flags=re.I)
    out: list[dict] = []
    for href in links:
        absolute = urljoin(url, href)
        out.append({"kind": "html_page_pdf_link", "source_url": url, "pdf_url": absolute, "accepted": True})
    return out or [{"kind": "html_pdf_link_scan", "source_url": url, "accepted": False, "reason": "no_pdf_links"}]


def _publisher_direct_pdf_candidates(paper: dict) -> list[dict]:
    doi = _doi_from_paper(paper)
    if not doi:
        return []
    candidates: list[dict] = []
    suffix = doi.split("/", 1)[1] if "/" in doi else ""
    source_blob = " ".join(
        str(paper.get(key) or "")
        for key in ["source", "venue", "url", "abs_url", "html_url", "pdf_url"]
    ).lower()
    if doi.startswith("10.1126/"):
        candidates.append({
            "kind": "doi_direct_science_pdf",
            "pdf_url": f"https://www.science.org/doi/pdf/{doi}?download=true",
            "doi": doi,
            "accepted": True,
        })
    if doi.startswith("10.1038/") and suffix:
        candidates.append({
            "kind": "doi_direct_nature_pdf",
            "pdf_url": f"https://www.nature.com/articles/{quote(suffix, safe='')}.pdf",
            "doi": doi,
            "accepted": True,
        })
    if doi.startswith("10.1101/") or doi.startswith("10.64898/") or "biorxiv.org" in source_blob or "biorxiv" in source_blob:
        candidates.append({
            "kind": "doi_direct_biorxiv_full_pdf",
            "pdf_url": f"https://www.biorxiv.org/content/{doi}.full.pdf",
            "doi": doi,
            "accepted": True,
        })
    return candidates


def _springer_nature_api_candidates(paper: dict) -> list[dict]:
    doi = _doi_from_paper(paper)
    key = str(os.environ.get("SPRINGER_API_KEY") or os.environ.get("SPRINGER_NATURE_API_KEY") or "").strip()
    if not doi or not doi.startswith("10.1038/"):
        return []
    if not key:
        return [{"kind": "springer_nature_openaccess_api", "accepted": False, **missing_official_access_reason("springernature")}]
    params = {"q": f'doi:"{doi}"', "api_key": key, "p": "5"}
    try:
        response = service_get("https://api.springernature.com/openaccess/json", params=params, timeout=35, headers={"Accept": "application/json"}, service="springernature")
    except Exception as exc:
        return [{"kind": "springer_nature_openaccess_api", "accepted": False, "error": exc.__class__.__name__}]
    if response.status_code != 200:
        return [{"kind": "springer_nature_openaccess_api", "accepted": False, "reason": f"http_{response.status_code}", **response_receipt(response, service="springernature")}]
    try:
        payload = response.json()
    except Exception as exc:
        return [{"kind": "springer_nature_openaccess_api", "accepted": False, "error": exc.__class__.__name__, **response_receipt(response, service="springernature")}]
    candidates: list[dict] = []
    for record in payload.get("records") or []:
        if not isinstance(record, dict):
            continue
        if not _same_paper_identity_ok(paper, candidate_title=record.get("title"), candidate_doi=record.get("doi")):
            continue
        urls = record.get("url") if isinstance(record.get("url"), list) else []
        for item in urls:
            if not isinstance(item, dict):
                continue
            value = str(item.get("value") or "").strip()
            fmt = str(item.get("format") or "").lower()
            if value.startswith("http") and ("pdf" in fmt or value.lower().endswith(".pdf") or "/pdf" in value.lower()):
                candidates.append({
                    "kind": "springer_nature_openaccess_api_pdf",
                    "pdf_url": value,
                    "doi": doi,
                    "accepted": True,
                })
            elif value.startswith("http"):
                candidates.append({
                    "kind": "springer_nature_openaccess_api_landing_page",
                    "landing_page_url": value,
                    "pdf_url": "",
                    "doi": doi,
                    "accepted": True,
                })
    return candidates or [{"kind": "springer_nature_openaccess_api", "accepted": False, "reason": "no_verified_pdf_or_landing_page", **response_receipt(response, service="springernature")}]


def _crossref_pdf_candidates(paper: dict) -> list[dict]:
    doi = _doi_from_paper(paper)
    if not doi:
        return []
    url = "https://api.crossref.org/works/" + quote(doi, safe="")
    crossref_mailto = service_contact_email("crossref")
    params = {"mailto": crossref_mailto} if crossref_mailto else None
    payload, receipt = _request_json(url, params=params, timeout=35)
    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    if message and not _same_paper_identity_ok(
        paper,
        candidate_title=(message.get("title") or [""])[0] if isinstance(message.get("title"), list) else message.get("title"),
        candidate_doi=message.get("DOI"),
    ):
        return [{**receipt, "kind": "crossref_same_paper_check", "accepted": False, "reason": "identity_mismatch"}]
    candidates: list[dict] = []
    for link in message.get("link") or []:
        if not isinstance(link, dict):
            continue
        pdf_url = str(link.get("URL") or "").strip()
        content_type = str(link.get("content-type") or "").lower()
        intended_application = str(link.get("intended-application") or "").strip().lower()
        content_version = str(link.get("content-version") or "").strip().lower()
        if pdf_url.startswith("http") and intended_application == "similarity-checking":
            candidates.append({
                "kind": "crossref_same_paper_pdf_link",
                "pdf_url": pdf_url,
                "doi": doi,
                "crossref_content_type": content_type,
                "crossref_content_version": content_version,
                "crossref_intended_application": intended_application,
                "accepted": False,
                "reason": "crossref_similarity_checking_link_not_authorized_for_tdm",
            })
            continue
        if pdf_url.startswith("http") and ("pdf" in content_type or pdf_url.lower().endswith(".pdf") or "/pdf" in pdf_url.lower()):
            candidates.append({
                "kind": "crossref_same_paper_pdf_link",
                "pdf_url": pdf_url,
                "doi": doi,
                "crossref_content_type": content_type,
                "crossref_content_version": content_version,
                "crossref_intended_application": intended_application,
                "accepted": True,
            })
    return candidates or [{**receipt, "kind": "crossref_same_paper_pdf_link", "accepted": False, "reason": "no_pdf_link"}]


def _crossref_metadata_hints(paper: dict) -> dict:
    doi = _doi_from_paper(paper)
    if not doi:
        return {"accepted": False, "reason": "missing_doi"}
    url = "https://api.crossref.org/works/" + quote(doi, safe="")
    crossref_mailto = service_contact_email("crossref")
    params = {"mailto": crossref_mailto} if crossref_mailto else None
    payload, receipt = _request_json(url, params=params, timeout=35)
    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    if not message:
        return {**receipt, "kind": "crossref_same_paper_metadata", "accepted": False, "reason": "missing_crossref_message"}
    title = (message.get("title") or [""])[0] if isinstance(message.get("title"), list) else str(message.get("title") or "")
    if not _same_paper_identity_ok(paper, candidate_title=title, candidate_doi=message.get("DOI")):
        return {**receipt, "kind": "crossref_same_paper_metadata", "accepted": False, "reason": "identity_mismatch"}
    authors: list[str] = []
    for author in message.get("author") or []:
        if not isinstance(author, dict):
            continue
        given = _clean_text(author.get("given") or "", 120)
        family = _clean_text(author.get("family") or "", 120)
        name = " ".join(part for part in [given, family] if part).strip()
        if name:
            authors.append(name)
    container = " ".join(str(item or "") for item in (message.get("container-title") or []) if str(item or "").strip())
    return {
        **receipt,
        "kind": "crossref_same_paper_metadata",
        "accepted": True,
        "doi": doi,
        "title": title,
        "authors": authors,
        "container_title": container,
    }


def _meta_content_values(html: str, names: set[str]) -> list[str]:
    values: list[str] = []
    for match in re.finditer(r"<meta\b[^>]*>", html or "", flags=re.I):
        tag = match.group(0)
        name_match = re.search(r"\b(?:name|property)=(?:['\"]([^'\"]+)['\"]|([^\s>]+))", tag, flags=re.I)
        content_match = re.search(r"\bcontent=(?:['\"]([^'\"]+)['\"]|([^\s>]+))", tag, flags=re.I | re.S)
        if not name_match or not content_match:
            continue
        name = (name_match.group(1) or name_match.group(2) or "").strip().lower()
        if name in names:
            value = re.sub(r"\s+", " ", content_match.group(1) or content_match.group(2) or "").strip()
            if value:
                values.append(value)
    return values


def _html_pdf_links(html: str, base_url: str) -> list[str]:
    links: list[str] = []
    for href in re.findall(r"<a\b[^>]*\bhref=['\"]([^'\"]+)['\"][^>]*>", html or "", flags=re.I):
        absolute = urljoin(base_url, unescape(href))
        lowered = absolute.lower()
        if absolute.startswith("http") and (lowered.endswith(".pdf") or "/pdf/" in lowered or "/pdf" in lowered or "download=pdf" in lowered):
            if absolute not in links:
                links.append(absolute)
    return links


def _pdf_urls_from_text(value: str, base_url: str) -> list[str]:
    links: list[str] = []
    for match in re.finditer(r"['\"]([^'\"]+?\.pdf(?:\?[^'\"]*)?)['\"]", value or "", flags=re.I):
        absolute = urljoin(base_url, unescape(match.group(1)))
        if absolute.startswith("http") and absolute not in links:
            links.append(absolute)
    return links


def _is_likely_cv_or_resume_pdf_url(url: object) -> bool:
    parsed = urlparse(str(url or ""))
    filename = unquote(Path(parsed.path).name).lower()
    if not filename.endswith(".pdf"):
        return False
    stem = filename[:-4]
    return bool(re.search(r"(^|[-_.])(cv|resume|vitae|curriculum[-_.]?vitae)([-_.]|$)", stem))


def _is_likely_infrastructure_pdf_url(url: object) -> bool:
    parsed = urlparse(str(url or ""))
    host = parsed.netloc.lower()
    path = unquote(parsed.path).lower()
    if not path.endswith(".pdf"):
        return False
    if host in {"www.doi.org", "doi.org"} and path.startswith("/resources/"):
        return True
    return False


def _is_conference_presentation_pdf_url(url: object) -> bool:
    path = unquote(urlparse(str(url or "")).path).lower()
    filename = Path(path).name
    return bool(
        any(marker in path for marker in ["/slides/", "/posters/", "/posterpdfs/", "/slide_decks/"])
        or re.search(r"(^|[-_.])(poster|slides?|presentation)([-_.]|$)", filename)
    )


def _append_unique_url(urls: list[str], value: object, base_url: str = "") -> None:
    absolute = urljoin(base_url, unescape(str(value or "").strip()))
    parsed = urlparse(absolute)
    if parsed.scheme in {"http", "https"} and absolute not in urls:
        urls.append(absolute)


def _first_party_js_asset_urls(html: str, base_url: str, *, limit: int = 6) -> list[str]:
    base_host = urlparse(base_url).netloc.lower()
    urls: list[str] = []

    def add(value: object) -> None:
        if len(urls) >= limit:
            return
        absolute = urljoin(base_url, str(value or ""))
        parsed = urlparse(absolute)
        if not absolute.startswith("http") or parsed.netloc.lower() != base_host:
            return
        lowered = parsed.path.lower()
        if (lowered.endswith(".js") or "/assets/" in lowered) and absolute not in urls:
            urls.append(absolute)

    for src in re.findall(r"<script\b[^>]*\bsrc=['\"]([^'\"]+)['\"][^>]*>", html or "", flags=re.I):
        add(src)
    return urls


def _duckduckgo_redirect_target(href: str) -> str:
    parsed = urlparse(unescape(str(href or "")))
    qs = parse_qs(parsed.query)
    return unquote((qs.get("uddg") or [""])[0]).strip()


def _duckduckgo_challenge_reason(text: str) -> str:
    lowered = str(text or "").lower()
    if "bots use duckduckgo" in lowered or "complete the following challenge" in lowered:
        return "duckduckgo_challenge"
    return ""


def _duckduckgo_markdown_urls(text: str) -> list[str]:
    urls: list[str] = []
    for match in re.finditer(r"\[[^\]]{0,200}\]\((https?://[^)\s]+)", text or "", flags=re.I):
        url = match.group(1).rstrip(".,;")
        if url not in urls:
            urls.append(url)
    return urls


def _duckduckgo_reader_result_urls(query: str, *, limit: int = 8) -> list[dict]:
    q = str(query or "").strip()
    if not q:
        return []
    access_backend = "jina_reader_authenticated" if jina_api_key_configured() else "jina_reader_anonymous"
    backend = "duckduckgo_reader_search"
    reader_url = "https://r.jina.ai/http://duckduckgo.com/html/?q=" + quote(q, safe="")
    with process_backend_slot(backend) as blocker:
        if blocker:
            return _backend_blocker_receipt("duckduckgo_reader_search", q, backend)
        access_blocked = _backend_blocker_receipt("duckduckgo_reader_search", q, access_backend)
        if access_blocked:
            return access_blocked
        try:
            response = service_get(reader_url, timeout=35, headers=jina_request_headers())
        except Exception as exc:
            return [{"kind": "duckduckgo_reader_search", "query": q, "accepted": False, "url": reader_url, "error": exc.__class__.__name__}]
        receipt = {"kind": "duckduckgo_reader_search", "query": q, **response_receipt(response), "accepted": response.status_code == 200}
        if response.status_code != 200:
            if response.status_code in {401, 403, 429}:
                mark_process_http_blocker(access_backend, response, f"http_{response.status_code}")
                mark_process_http_blocker(backend, response, f"http_{response.status_code}")
            return [receipt]
        text = response.text or ""
        challenge_reason = _duckduckgo_challenge_reason(text)
        if challenge_reason:
            mark_process_http_blocker(backend, response, challenge_reason)
            return [{**receipt, "accepted": False, "reason": challenge_reason}]
    out: list[dict] = []
    seen: set[str] = set()
    raw_urls = [
        match.group(0).rstrip(".,;")
        for match in re.finditer(r"https?://duckduckgo\.com/l/\?uddg=[^\s)\]]+", text, flags=re.I)
    ]
    raw_urls.extend(_duckduckgo_markdown_urls(text))
    for raw_url in raw_urls:
        target = _duckduckgo_redirect_target(raw_url) if "duckduckgo.com/l/?" in raw_url.lower() else raw_url
        if not target or target in seen or not target.startswith("http"):
            continue
        target_host = urlparse(target).netloc.lower()
        if target_host.endswith("duckduckgo.com") or target_host.endswith("external-content.duckduckgo.com"):
            continue
        seen.add(target)
        out.append({"kind": "duckduckgo_reader_result_url", "query": q, "url": target, "accepted": True, "reader_url": reader_url})
        if len(out) >= limit:
            break
    return out or [{**receipt, "accepted": False, "reason": "no_duckduckgo_reader_results"}]


def _jina_search_result_urls(query: str, *, limit: int = 8) -> list[dict]:
    q = str(query or "").strip()
    if not q or not jina_api_key_configured():
        return []
    backend = "jina_search_authenticated"
    search_url = "https://s.jina.ai/" + quote(q, safe="")
    with process_backend_slot(backend) as blocker:
        if blocker:
            return _backend_blocker_receipt("jina_search", q, backend)
        try:
            response = service_get(search_url, timeout=35, headers=jina_request_headers())
        except Exception as exc:
            return [{"kind": "jina_search", "query": q, "accepted": False, "url": search_url, "error": exc.__class__.__name__}]
        receipt = {"kind": "jina_search", "query": q, **response_receipt(response), "accepted": response.status_code == 200}
        if response.status_code != 200:
            if response.status_code in {401, 403, 429}:
                mark_process_http_blocker(backend, response, f"http_{response.status_code}")
            return [receipt]
    out: list[dict] = []
    seen: set[str] = set()
    for target in _duckduckgo_markdown_urls(response.text or ""):
        host = urlparse(target).netloc.lower()
        if not host or host in {"jina.ai", "www.jina.ai", "s.jina.ai", "r.jina.ai"} or target in seen:
            continue
        seen.add(target)
        out.append({"kind": "jina_search_result_url", "query": q, "url": target, "accepted": True, "search_url": search_url})
        if len(out) >= limit:
            break
    return out or [{**receipt, "accepted": False, "reason": "no_jina_search_results"}]


def _duckduckgo_result_urls(query: str, *, limit: int = 8) -> list[dict]:
    q = str(query or "").strip()
    if not q:
        return []
    backend = "duckduckgo_direct"
    try:
        max_attempts = int(os.environ.get("READING_DDG_DIRECT_ATTEMPTS", str(config_int("search.duckduckgo_direct_attempts", 1))) or config_int("search.duckduckgo_direct_attempts", 1))
    except ValueError:
        max_attempts = config_int("search.duckduckgo_direct_attempts", 1)
    max_attempts = max(1, min(3, max_attempts))
    try:
        timeout_sec = float(os.environ.get("READING_DDG_TIMEOUT_SEC", str(config_float("search.duckduckgo_timeout_sec", 5.0))) or config_float("search.duckduckgo_timeout_sec", 5.0))
    except ValueError:
        timeout_sec = config_float("search.duckduckgo_timeout_sec", 5.0)
    timeout_sec = max(2.0, min(30.0, timeout_sec))
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    attempts: list[dict] = []
    with process_backend_slot(backend) as blocker:
        if blocker:
            return _backend_blocker_receipt("duckduckgo_search", q, backend)
        for attempt_index in range(max_attempts):
            try:
                response = service_get(
                    "https://html.duckduckgo.com/html/",
                    params={"q": q},
                    timeout=(min(3.0, timeout_sec), timeout_sec),
                    headers=headers,
                    service="web_search",
                )
            except Exception as exc:
                attempts.append({"kind": "duckduckgo_search", "query": q, "accepted": False, "error": exc.__class__.__name__})
                time.sleep(0.8 + attempt_index * 0.8)
                continue
            receipt = {
                "kind": "duckduckgo_search",
                "query": q,
                "accepted": response.status_code == 200,
                **response_receipt(response, service="web_search"),
            }
            attempts.append(receipt)
            if response.status_code != 200:
                if response.status_code in {202, 403, 429}:
                    mark_process_http_blocker(backend, response, f"http_{response.status_code}")
                time.sleep(0.8 + attempt_index * 0.8)
                continue
            html = response.text or ""
            challenge_reason = _duckduckgo_challenge_reason(html)
            if challenge_reason:
                attempts[-1] = {**receipt, "accepted": False, "reason": challenge_reason}
                mark_process_http_blocker(backend, response, challenge_reason)
                time.sleep(0.8 + attempt_index * 0.8)
                continue
            out: list[dict] = []
            seen: set[str] = set()
            for match in re.finditer(r'href=["\'](//duckduckgo\.com/l/\?uddg=[^"\']+)["\']', html, flags=re.I):
                target = _duckduckgo_redirect_target(match.group(1))
                if not target or target in seen or not target.startswith("http"):
                    continue
                seen.add(target)
                out.append({"kind": "duckduckgo_result_url", "query": q, "url": target, "accepted": True})
                if len(out) >= limit:
                    break
            if out:
                return out
            if "result__a" in html or "uddg=" in html:
                return [{**receipt, "accepted": False, "reason": "no_parseable_duckduckgo_results"}]
            time.sleep(0.8 + attempt_index * 0.8)
    return attempts or [{"kind": "duckduckgo_search", "query": q, "accepted": False, "reason": "no_duckduckgo_results"}]


def _startpage_result_urls(query: str, *, limit: int = 8) -> list[dict]:
    q = str(query or "").strip()
    if not q:
        return []
    backend = "startpage"
    try:
        timeout_sec = float(os.environ.get("READING_STARTPAGE_TIMEOUT_SEC", str(config_float("search.startpage_timeout_sec", 5.0))) or config_float("search.startpage_timeout_sec", 5.0))
    except ValueError:
        timeout_sec = config_float("search.startpage_timeout_sec", 5.0)
    timeout_sec = max(2.0, min(20.0, timeout_sec))
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    with process_backend_slot(backend) as blocker:
        if blocker:
            return _backend_blocker_receipt("startpage_search", q, backend)
        try:
            response = service_get(
                "https://www.startpage.com/sp/search",
                params={"query": q},
                timeout=(min(3.0, timeout_sec), timeout_sec),
                headers=headers,
                service="web_search",
            )
        except Exception as exc:
            return [{"kind": "startpage_search", "query": q, "accepted": False, "error": exc.__class__.__name__}]
        receipt = {"kind": "startpage_search", "query": q, "accepted": response.status_code == 200, **response_receipt(response, service="web_search")}
        if response.status_code != 200:
            if response.status_code in {202, 403, 429}:
                mark_process_http_blocker(backend, response, f"http_{response.status_code}")
            return [receipt]
        html = response.text or ""
        lowered = html.lower()
        if "enable javascript" in lowered or "detected unusual traffic" in lowered or "captcha" in lowered:
            reason = "startpage_challenge_or_automation_page"
            mark_process_http_blocker(backend, response, reason)
            return [{**receipt, "accepted": False, "reason": reason}]
    raw_urls: list[str] = []
    for match in re.finditer(r"\bhref=['\"]([^'\"]+)['\"]", html, flags=re.I):
        raw_urls.append(unescape(match.group(1)))
    for match in re.finditer(r"https?://[^\s\"'<>]+", html, flags=re.I):
        raw_urls.append(unescape(match.group(0)))
    out: list[dict] = []
    seen: set[str] = set()
    for raw_url in raw_urls:
        parsed_raw = urlparse(raw_url)
        if not parsed_raw.scheme and raw_url.startswith("//"):
            raw_url = "https:" + raw_url
            parsed_raw = urlparse(raw_url)
        target = raw_url
        if parsed_raw.netloc.lower().endswith("startpage.com"):
            qs = parse_qs(parsed_raw.query)
            for key in ["url", "uddg", "u"]:
                value = (qs.get(key) or [""])[0]
                if value.startswith("http"):
                    target = unquote(value)
                    break
        target = target.rstrip(".,;)]}")
        parsed = urlparse(target)
        host = parsed.netloc.lower()
        if not target.startswith("http") or not host or target in seen:
            continue
        if host.endswith("startpage.com") or host.endswith("ixquick.com"):
            continue
        if host in {"startmail.com", "www.startmail.com"} or host.endswith(".system1.com"):
            continue
        if host in {"openreview.ne"}:
            continue
        if host in {"fonts.googleapis.com", "fonts.gstatic.com", "www.w3.org", "w3.org"}:
            continue
        if parsed.path.lower().endswith((".css", ".js", ".ico", ".png", ".jpg", ".jpeg", ".svg", ".webp")):
            continue
        if host in {"openreview.net", "iclr.cc", "github.com", "arxiv.org"} and parsed.path in {"", "/"} and not parsed.query:
            continue
        if any(host == blocked or host.endswith("." + blocked) for blocked in [
            "twitter.com",
            "x.com",
            "reddit.com",
            "instagram.com",
            "facebook.com",
            "mastodon.social",
            "linkedin.com",
            "youtube.com",
        ]):
            continue
        seen.add(target)
        out.append({"kind": "startpage_result_url", "query": q, "url": target, "accepted": True})
        if len(out) >= limit:
            break
    return out or [{**receipt, "accepted": False, "reason": "no_startpage_results"}]


def _asset_pdf_links(html: str, base_url: str, *, max_assets: int = 8, max_bytes: int = 500_000) -> list[dict]:
    out: list[dict] = []
    seen_assets: set[str] = set()
    queue = _first_party_js_asset_urls(html, base_url, limit=max_assets)
    while queue and len(seen_assets) < max_assets:
        asset_url = queue.pop(0)
        if asset_url in seen_assets:
            continue
        seen_assets.add(asset_url)
        try:
            response = service_get(asset_url, timeout=35, headers={"Accept": "*/*"})
        except Exception as exc:
            out.append({"kind": "project_page_asset_scan", "asset_url": asset_url, "accepted": False, "error": exc.__class__.__name__})
            continue
        receipt = {"kind": "project_page_asset_scan", "asset_url": asset_url, "accepted": response.status_code == 200, **response_receipt(response)}
        if response.status_code != 200:
            out.append(receipt)
            continue
        text = response.text[:max_bytes]
        for pdf_url in _pdf_urls_from_text(text, asset_url):
            out.append({**receipt, "pdf_url": pdf_url, "accepted": True})
        for nested in re.findall(r"['\"]((?:\./)?assets/[^'\"]+?\.js)['\"]", text, flags=re.I):
            if len(queue) + len(seen_assets) >= max_assets:
                break
            parsed_asset = urlparse(asset_url)
            if nested.startswith("assets/"):
                absolute = f"{parsed_asset.scheme}://{parsed_asset.netloc}/{nested}"
            else:
                absolute = urljoin(asset_url, nested)
            if absolute not in seen_assets and absolute not in queue:
                queue.append(absolute)
        if not any(item.get("asset_url") == asset_url and item.get("pdf_url") for item in out):
            out.append({**receipt, "reason": "no_pdf_links_in_asset"})
    return out


def _anchor_links(html: str, base_url: str) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    for match in re.finditer(r"<a\b[^>]*\bhref=['\"]([^'\"]+)['\"][^>]*>(.*?)</a>", html or "", flags=re.I | re.S):
        label = re.sub(r"<[^>]+>", " ", match.group(2))
        label = re.sub(r"\s+", " ", unescape(label)).strip()
        absolute = urljoin(base_url, unescape(match.group(1)))
        if absolute.startswith("http"):
            links.append({"url": absolute, "label": label})
    return links


def _github_repo_api_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc.lower() != "github.com":
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return ""
    return f"https://api.github.com/repos/{quote(parts[0], safe='')}/{quote(parts[1], safe='')}/contents"


def _github_repo_hints(url: str, *, limit: int = 6) -> list[dict]:
    api_url = _github_repo_api_url(url)
    if not api_url:
        return []
    headers = {"Accept": "application/vnd.github+json,*/*", "X-GitHub-Api-Version": "2022-11-28"}
    github_token = str(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    if github_token:
        headers["Authorization"] = "Bearer " + github_token
    backend = "github_api_authenticated" if github_token else "github_api_anonymous"
    with process_backend_slot(backend) as blocker:
        if blocker:
            return [{
                "kind": "project_page_github_repo_scan",
                "source_url": url,
                "api_url": api_url,
                "accepted": False,
                "reason": "skipped_after_prior_backend_access_failure",
                "prior_reason": blocker.get("reason"),
            }]
        try:
            response = service_get(api_url, timeout=30, headers=headers)
        except Exception as exc:
            return [{"kind": "project_page_github_repo_scan", "source_url": url, "api_url": api_url, "accepted": False, "error": exc.__class__.__name__}]
        receipt = {"kind": "project_page_github_repo_scan", "source_url": url, "api_url": api_url, "accepted": response.status_code == 200, **response_receipt(response)}
        if response.status_code != 200:
            if response.status_code in {401, 403, 429}:
                mark_process_http_blocker(backend, response, f"http_{response.status_code}")
            return [receipt]
    try:
        payload = response.json()
    except Exception as exc:
        return [{**receipt, "accepted": False, "error": exc.__class__.__name__}]
    if not isinstance(payload, list):
        return [{**receipt, "accepted": False, "reason": "unexpected_github_contents_payload"}]
    out: list[dict] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        download_url = str(item.get("download_url") or "")
        html_url = str(item.get("html_url") or "")
        lowered = name.lower()
        if download_url.startswith("http") and lowered.endswith(".pdf"):
            out.append({**receipt, "accepted": True, "pdf_url": download_url, "repo_item": name, "html_url": html_url})
        elif download_url.startswith("http") and lowered in {"readme.md", "readme.rst", "readme.txt"}:
            try:
                readme_response = service_get(download_url, timeout=30, headers={"Accept": "text/plain,*/*"})
                text = readme_response.text[:200_000] if readme_response.status_code == 200 else ""
            except Exception as exc:
                out.append({**receipt, "accepted": False, "repo_item": name, "readme_url": download_url, "error": exc.__class__.__name__})
                continue
            for pdf_url in _pdf_urls_from_text(text, download_url):
                out.append({**receipt, "accepted": True, "pdf_url": pdf_url, "repo_item": name, "readme_url": download_url})
        if len([candidate for candidate in out if candidate.get("accepted")]) >= limit:
            break
    return out or [{**receipt, "accepted": False, "reason": "no_pdf_links_in_repo_root"}]


def _project_external_research_links(html: str, base_url: str, *, limit: int = 8) -> list[dict]:
    base_host = urlparse(base_url).netloc.lower()
    links: list[dict] = []
    seen: set[str] = set()
    for link in _anchor_links(html, base_url):
        url = link["url"]
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        lowered = url.lower()
        label_lower = link["label"].lower()
        if url in seen or not host or host == base_host:
            continue
        if "openreview.net" in host:
            continue
        is_research_link = (
            host.endswith(".edu")
            or host == "github.com"
            or "gitlab" in host
            or lowered.endswith(".pdf")
            or any(term in label_lower for term in ["paper", "code", "repository", "homepage", "project", "publication"])
        )
        if not is_research_link:
            continue
        seen.add(url)
        links.append({"kind": "project_page_external_research_link", "source_url": base_url, "url": url, "label": link["label"], "accepted": True})
        if len(links) >= limit:
            break
    return links


def _linked_research_page_pdf_candidates(html: str, base_url: str, *, source_kind: str, limit: int = 6) -> list[dict]:
    candidates: list[dict] = []
    scans: list[dict] = []
    seen_pages: set[str] = set()
    for link in _project_external_research_links(html, base_url, limit=limit):
        page_url = str(link.get("url") or "")
        if not page_url or page_url in seen_pages:
            continue
        seen_pages.add(page_url)
        if "github.com" in urlparse(page_url).netloc.lower():
            for item in _github_repo_hints(page_url):
                item = {**item, "source_project_page": base_url, "source_link": link}
                if item.get("accepted") and item.get("pdf_url"):
                    candidates.append({**item, "kind": source_kind + "_github_pdf_requires_text_identity", "requires_pdf_text_identity_check": True})
                else:
                    scans.append(item)
            continue
        try:
            response = service_get(page_url, timeout=30, headers={"Accept": "text/html,*/*"})
        except Exception as exc:
            scans.append({**link, "accepted": False, "error": exc.__class__.__name__})
            continue
        receipt = {**link, "accepted": response.status_code == 200, **response_receipt(response)}
        if response.status_code != 200:
            scans.append(receipt)
            continue
        resolved = response.url
        page_html = response.text[:300_000]
        pdf_links: list[str] = []
        for pdf_url in _html_pdf_links(page_html, resolved):
            _append_unique_url(pdf_links, pdf_url)
        for pdf_url in _pdf_urls_from_text(page_html, resolved):
            _append_unique_url(pdf_links, pdf_url)
        for pdf_url in pdf_links[:limit]:
            candidates.append({
                **receipt,
                "kind": source_kind + "_linked_page_pdf_requires_text_identity",
                "source_project_page": base_url,
                "linked_page_url": page_url,
                "pdf_url": pdf_url,
                "accepted": True,
                "requires_pdf_text_identity_check": True,
            })
        if not pdf_links:
            scans.append({**receipt, "reason": "no_pdf_links_in_linked_research_page"})
        if len(candidates) >= limit:
            break
    return candidates or scans


def _search_result_pdf_candidates(paper: dict, *, limit: int = 6) -> list[dict]:
    title = str(paper.get("title") or "").strip()
    if len(title.split()) < 3:
        return []
    candidates: list[dict] = []
    scans: list[dict] = []
    seen_candidate_pdf_urls: set[str] = set()
    seen_result_urls: set[str] = set()
    authors = paper.get("authors") if isinstance(paper.get("authors"), list) else []
    expected_openreview_ids = _iclr_openreview_ids(paper)
    crossref_hints: dict = {}
    if _is_acm_doi_input(paper):
        crossref_hints = _crossref_metadata_hints(paper)
        if crossref_hints.get("accepted") and not authors:
            authors = [str(author) for author in crossref_hints.get("authors") or [] if str(author).strip()]
    official_search_specs = official_conference_title_search_specs(paper)
    official_query_domains = {
        str(spec.get("query") or ""): str(spec.get("domain") or "").lower()
        for spec in official_search_specs
        if spec.get("query") and spec.get("domain")
    }
    search_queries: list[str] = [
        *official_query_domains,
        f"\"{title}\" PDF",
        f"\"{title}\"",
    ]
    if _doi_from_paper(paper):
        search_queries.insert(0, f"\"{title}\" {_doi_from_paper(paper)}")
    if _is_acm_doi_input(paper):
        search_queries.extend([
            f"\"{title}\" \"ACM Author-Izer\"",
            f"\"{title}\" \"Author-Izer\"",
            f"\"{_doi_from_paper(paper)}\" PDF",
        ])
    for author in authors:
        author_text = _clean_text(author, 120)
        if author_text:
            search_queries.append(f"\"{title}\" \"{author_text}\"")
    if crossref_hints.get("accepted"):
        scans.append({
            "kind": "crossref_same_paper_metadata",
            "accepted": True,
            "doi": crossref_hints.get("doi"),
            "authors": crossref_hints.get("authors"),
            "container_title": crossref_hints.get("container_title"),
        })
    deduped_queries: list[str] = []
    for query in search_queries:
        if query and query not in deduped_queries:
            deduped_queries.append(query)
    try:
        default_query_limit = config_int("search.acm_query_limit", 8) if _is_acm_doi_input(paper) else config_int("search.query_limit", 3)
        default_query_limit += len(official_search_specs)
        query_limit = int(os.environ.get("READING_SEARCH_QUERY_LIMIT", str(default_query_limit)) or default_query_limit)
    except ValueError:
        query_limit = config_int("search.acm_query_limit", 8) if _is_acm_doi_input(paper) else config_int("search.query_limit", 3)
        query_limit += len(official_search_specs)
    query_limit = max(2, min(12, query_limit))
    if len(deduped_queries) > query_limit:
        scans.append({
            "kind": "search_query_budget",
            "accepted": False,
            "reason": "search_query_limit_applied",
            "query_count": len(deduped_queries),
            "used_query_count": query_limit,
        })
        deduped_queries = deduped_queries[:query_limit]

    def append_candidate(candidate: dict) -> None:
        pdf_url = str(candidate.get("pdf_url") or "").strip()
        if not pdf_url:
            scans.append(candidate)
            return
        if _is_likely_cv_or_resume_pdf_url(pdf_url):
            scans.append({
                **candidate,
                "accepted": False,
                "reason": "likely_cv_or_resume_pdf_not_article_body",
                "message_zh": "搜索结果指向 CV/简历 PDF，不是论文正文；跳过该候选。",
            })
            return
        if _is_likely_infrastructure_pdf_url(pdf_url):
            scans.append({
                **candidate,
                "accepted": False,
                "reason": "likely_infrastructure_pdf_not_article_body",
                "message_zh": "搜索结果指向 DOI/站点政策等基础设施 PDF，不是论文正文；跳过该候选。",
            })
            return
        if _is_conference_presentation_pdf_url(pdf_url):
            scans.append({
                **candidate,
                "accepted": False,
                "reason": "conference_presentation_pdf_not_article_body",
                "message_zh": "会议搜索结果指向海报或演示文稿 PDF，不是论文正文；继续查找同篇论文全文。",
            })
            return
        candidate_openreview_id = _openreview_note_id_from_url(pdf_url)
        if expected_openreview_ids and candidate_openreview_id and candidate_openreview_id not in expected_openreview_ids:
            scans.append({
                **candidate,
                "accepted": False,
                "reason": "openreview_cross_submission_note_id_mismatch",
                "expected_openreview_ids": sorted(expected_openreview_ids),
                "candidate_openreview_id": candidate_openreview_id,
                "message_zh": "搜索结果指向另一个 OpenReview submission/note，不能替代当前固定输入论文。",
            })
            return
        if pdf_url in seen_candidate_pdf_urls:
            return
        seen_candidate_pdf_urls.add(pdf_url)
        candidates.append(candidate)

    def consume_result_items(items: list[dict], query: str) -> bool:
        candidate_count_before = len(candidates)
        official_domain = official_query_domains.get(query, "")
        for item in items:
            if not item.get("accepted"):
                scans.append(item)
                continue
            url = str(item.get("url") or "")
            if not url or url in seen_result_urls:
                continue
            lowered = url.lower()
            result_host = urlparse(url).netloc.lower()
            if official_domain and not (
                result_host == official_domain or result_host.endswith("." + official_domain)
            ):
                scans.append({
                    **item,
                    "accepted": False,
                    "reason": "official_title_search_off_domain_result",
                    "official_domain": official_domain,
                })
                continue
            seen_result_urls.add(url)
            search_metadata = {
                "official_conference_title_search": bool(official_domain),
                "official_domain": official_domain,
            }
            if "openreview.net" in lowered:
                candidate_openreview_id = _openreview_note_id_from_url(url)
                if expected_openreview_ids and candidate_openreview_id and candidate_openreview_id not in expected_openreview_ids:
                    scans.append({
                        **item,
                        "accepted": False,
                        "reason": "openreview_cross_submission_note_id_mismatch",
                        "expected_openreview_ids": sorted(expected_openreview_ids),
                        "candidate_openreview_id": candidate_openreview_id,
                        "message_zh": "搜索结果指向另一个 OpenReview submission/note，不能替代当前固定输入论文。",
                    })
                elif expected_openreview_ids and candidate_openreview_id in expected_openreview_ids and ("/pdf" in lowered or "/attachment" in lowered):
                    append_candidate({
                        "kind": "search_result_openreview_pdf_requires_text_identity",
                        "source_search_query": query,
                        "source_result_url": url,
                        "pdf_url": url,
                        "accepted": True,
                        "requires_pdf_text_identity_check": True,
                        **search_metadata,
                    })
                elif _is_openreview_static_pdf_url(url):
                    append_candidate({
                        "kind": "search_result_openreview_static_pdf_requires_text_identity",
                        "source_search_query": query,
                        "source_result_url": url,
                        "pdf_url": url,
                        "accepted": True,
                        "requires_pdf_text_identity_check": True,
                        "message_zh": "搜索结果指向 OpenReview 静态 PDF；URL 不含 note id，必须通过 PDF 正文标题/作者校验后才可使用。",
                        **search_metadata,
                    })
                continue
            if "github.com" in lowered:
                for repo_item in _github_repo_hints(url):
                    repo_item = {**repo_item, "source_search_query": query, "source_result": item}
                    if repo_item.get("accepted") and repo_item.get("pdf_url"):
                        append_candidate({
                            **repo_item,
                            "kind": "search_result_github_repo_pdf_requires_text_identity",
                            "requires_pdf_text_identity_check": True,
                            **search_metadata,
                        })
                    else:
                        scans.append(repo_item)
                continue
            if lowered.endswith(".pdf") or "/pdf" in lowered or "article/view" in lowered:
                append_candidate({
                    "kind": "search_result_pdf_requires_text_identity",
                    "source_search_query": query,
                    "source_result_url": url,
                    "pdf_url": url,
                    "accepted": True,
                    "requires_pdf_text_identity_check": True,
                    **search_metadata,
                })
                continue
            page_candidates = _publisher_page_candidates_from_url(
                paper,
                url,
                kind="search_result_page",
                scan_assets=False,
                allow_pdf_text_identity_check=True,
            )
            for candidate in page_candidates:
                candidate["source_search_query"] = query
                candidate["source_result_url"] = url
                candidate.update(search_metadata)
                if candidate.get("accepted"):
                    append_candidate(candidate)
                else:
                    scans.append(candidate)
            if len(candidates) >= limit:
                return True
        return len(candidates) > candidate_count_before

    for query in deduped_queries:
        official_query = query in official_query_domains
        direct_items = _duckduckgo_result_urls(query, limit=limit)
        direct_done = consume_result_items(direct_items, query)
        direct_blocked = any(
            not item.get("accepted") and item.get("kind") == "duckduckgo_search" and item.get("status_code") in {202, 403, 429}
            for item in direct_items
        )
        if not direct_done or direct_blocked:
            jina_done = consume_result_items(_jina_search_result_urls(query, limit=limit), query) if jina_api_key_configured() else False
            if jina_done and not official_query:
                return candidates + scans
            if not jina_done:
                reader_done = consume_result_items(_duckduckgo_reader_result_urls(query, limit=limit), query)
                if reader_done and not official_query:
                    return candidates + scans
                startpage_done = consume_result_items(_startpage_result_urls(query, limit=limit), query)
                if startpage_done and not official_query:
                    return candidates + scans
        elif direct_done and not official_query:
            return candidates + scans
    return candidates + scans if candidates else scans


def _chatpaper_openreview_cached_pdf_candidates(paper: dict, *, limit: int = 3) -> list[dict]:
    expected_openreview_ids = _iclr_openreview_ids(paper)
    title = str(paper.get("title") or "").strip()
    if not expected_openreview_ids or len(title.split()) < 3:
        return []
    search_url = "https://chatpaper.com/search?keywords=" + quote(title, safe="")
    resolved_url, html, receipt = _request_html(search_url, timeout=20)
    if not html:
        return [{
            "kind": "chatpaper_openreview_cache_search",
            "source_url": search_url,
            "accepted": False,
            **receipt,
        }]
    article_ids: list[str] = []
    for pattern in [
        r'data-doc=["\'](\d+)["\']',
        r'href=["\']/(?:zh-CN/)?paper/(\d+)(?:[?][^"\']*)?["\']',
        r'\bpaper/(\d+)\b',
    ]:
        for match in re.finditer(pattern, html, flags=re.I):
            article_id = match.group(1)
            if article_id not in article_ids:
                article_ids.append(article_id)
            if len(article_ids) >= limit:
                break
        if len(article_ids) >= limit:
            break
    if not article_ids:
        return [{
            "kind": "chatpaper_openreview_cache_search",
            "source_url": search_url,
            "resolved_url": resolved_url,
            "accepted": False,
            "reason": "no_chatpaper_article_ids",
            "fetch": receipt,
        }]

    candidates: list[dict] = []
    scans: list[dict] = []
    for article_id in article_ids[:limit]:
        page_url = f"https://chatpaper.com/paper/{article_id}"
        page_resolved_url, page_html, page_receipt = _request_html(page_url, timeout=20)
        page_note_ids = _openreview_note_ids_from_text(page_html)
        matched_note_ids = sorted(expected_openreview_ids & page_note_ids)
        base = {
            "kind": "chatpaper_openreview_cached_pdf_requires_text_identity",
            "source_url": search_url,
            "resolved_source_url": resolved_url,
            "article_page_url": page_url,
            "resolved_article_page_url": page_resolved_url,
            "chatpaper_article_id": article_id,
            "expected_openreview_ids": sorted(expected_openreview_ids),
            "candidate_openreview_ids": sorted(page_note_ids),
            "search_fetch": receipt,
            "article_page_fetch": page_receipt,
        }
        if not page_html:
            scans.append({**base, "accepted": False, "reason": "chatpaper_article_page_unavailable"})
            continue
        if not matched_note_ids:
            scans.append({
                **base,
                "accepted": False,
                "reason": "chatpaper_openreview_note_id_mismatch",
                "message_zh": "ChatPaper 页面没有匹配当前固定输入的 OpenReview note id，不能作为同篇 PDF 来源。",
            })
            continue
        candidates.append({
            **base,
            "pdf_url": f"https://chatpaper.com/api/v1/articles/download/{article_id}",
            "source_openreview_id": matched_note_ids[0],
            "accepted": True,
            "requires_pdf_text_identity_check": True,
            "message_zh": "ChatPaper 公开下载端点返回该 OpenReview note 的缓存 PDF；下载后仍必须通过正文标题/作者 identity gate。",
        })
        if len(candidates) >= limit:
            break
    return candidates or scans


def _pdf_candidate_failure_sleep_sec() -> float:
    try:
        value = float(os.environ.get("READING_PDF_FAILURE_SLEEP_SEC", str(config_float("pdf.failure_sleep_sec", 0.3))) or config_float("pdf.failure_sleep_sec", 0.3))
    except ValueError:
        value = config_float("pdf.failure_sleep_sec", 0.3)
    return max(0.0, min(5.0, value))


def _is_acm_doi_input(paper: dict) -> bool:
    doi_blob = " ".join(str(paper.get(key) or "") for key in ["url", "doi", "pdf_url", "published_doi"]).lower()
    return "10.1145/" in doi_blob or "dl.acm.org" in doi_blob


def _is_biorxiv_input(paper: dict) -> bool:
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    blob = " ".join(
        str(value or "")
        for value in [
            paper.get("source"),
            paper.get("venue"),
            paper.get("url"),
            paper.get("abs_url"),
            paper.get("html_url"),
            paper.get("pdf_url"),
            paper.get("doi"),
            paper.get("published_doi"),
            metadata.get("source"),
            metadata.get("venue"),
            metadata.get("url"),
            metadata.get("pdf_url"),
            metadata.get("doi"),
            metadata.get("published_doi"),
        ]
    ).lower()
    return "biorxiv" in blob or "biorxiv.org" in blob or "10.1101/" in blob or "10.64898/" in blob


def _iclr_mlanthology_candidates(paper: dict, *, limit: int = 3) -> list[dict]:
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    marker = " ".join(str(value or "").lower() for value in [
        paper.get("venue"),
        paper.get("source"),
        paper.get("url"),
        paper.get("html_url"),
        metadata.get("conference_channel"),
        metadata.get("conference_virtual_url"),
    ])
    if "iclr" not in marker and "/virtual/2026/poster/" not in marker:
        return []
    title_tokens = [token for token in re.findall(r"[A-Za-z0-9]+", str(paper.get("title") or "").lower()) if len(token) >= 3]
    authors = paper.get("authors") if isinstance(paper.get("authors"), list) else []
    first_author_family = ""
    if authors:
        parts = re.findall(r"[A-Za-z][A-Za-z-]+", str(authors[0] or ""))
        if parts:
            first_author_family = parts[-1].lower().replace("-", "")
    if not first_author_family or not title_tokens:
        return []
    slugs = [f"{first_author_family}2026iclr-{title_tokens[0]}"]
    candidates: list[dict] = []
    scans: list[dict] = []
    for slug in slugs[:limit]:
        page_url = f"https://mlanthology.org/iclr/2026/{slug}/"
        for candidate in _publisher_page_candidates_from_url(
            paper,
            page_url,
            kind="iclr_mlanthology_page",
            scan_assets=False,
            allow_pdf_text_identity_check=True,
            include_openreview_locators=True,
        ):
            candidate["mlanthology_slug"] = slug
            candidate["source_url"] = page_url
            if candidate.get("accepted"):
                candidates.append(candidate)
            else:
                scans.append(candidate)
    return candidates or scans


def _iclr_openreview_ids(paper: dict) -> set[str]:
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    values = [
        paper.get("openreview_id"),
        paper.get("openreview_note_id"),
        paper.get("openreview_forum_url"),
        paper.get("openreview_url"),
        paper.get("openreview_pdf_url"),
        paper.get("forum"),
        paper.get("note_id"),
        paper.get("paper_url"),
        paper.get("paper_pdf_url"),
        paper.get("url"),
        paper.get("html_url"),
        paper.get("abs_url"),
        paper.get("pdf_url"),
        metadata.get("openreview_id"),
        metadata.get("openreview_note_id"),
        metadata.get("openreview_forum"),
        metadata.get("openreview_forum_url"),
        metadata.get("openreview_url"),
        metadata.get("openreview_pdf_url"),
        metadata.get("forum"),
        metadata.get("note_id"),
        metadata.get("paper_url"),
        metadata.get("paper_pdf_url"),
    ]
    out: set[str] = set()
    for value in values:
        text = str(value or "")
        for match in re.finditer(r"openreview\.net/(?:forum|pdf|attachment)\?id=([^&#\s]+)", text):
            out.add(match.group(1).strip())
        if re.fullmatch(r"[A-Za-z0-9_-]{8,}", text.strip()):
            out.add(text.strip())
    return {item for item in out if item}


def _paper_with_discovered_openreview_id(paper: dict, note_ids: set[str]) -> dict:
    if not note_ids or _iclr_openreview_ids(paper):
        return paper
    return {**paper, "openreview_id": sorted(note_ids)[0]}


def _openreview_attachment_pdf_candidates(paper: dict) -> list[dict]:
    candidates: list[dict] = []
    for note_id in sorted(_iclr_openreview_ids(paper)):
        encoded_note_id = quote(note_id, safe="")
        for attachment_name in OPENREVIEW_ATTACHMENT_PDF_NAMES:
            encoded_name = quote(attachment_name, safe="")
            name_suffix = "" if attachment_name == "pdf" else "_" + safe_slug(attachment_name)
            for kind, endpoint in [
                ("openreview_attachment_pdf_from_note_id", "https://openreview.net/attachment"),
                ("openreview_api_attachment_pdf_from_note_id", "https://api.openreview.net/attachment"),
                ("openreview_api2_attachment_pdf_from_note_id", "https://api2.openreview.net/attachment"),
            ]:
                candidates.append({
                    "kind": kind + name_suffix,
                    "pdf_url": f"{endpoint}?id={encoded_note_id}&name={encoded_name}",
                    "openreview_id": note_id,
                    "openreview_attachment_name": attachment_name,
                    "accepted": True,
                })
    return candidates


def _openreview_direct_pdf_variant_candidates(paper: dict) -> list[dict]:
    candidates: list[dict] = []
    for note_id in sorted(_iclr_openreview_ids(paper)):
        encoded_note_id = quote(note_id, safe="")
        for kind, url in [
            ("openreview_pdf_named_from_note_id", f"https://openreview.net/pdf?id={encoded_note_id}&name=pdf"),
            ("openreview_pdf_download_from_note_id", f"https://openreview.net/pdf?id={encoded_note_id}&download=true"),
            ("openreview_api_pdf_from_note_id", f"https://api.openreview.net/pdf?id={encoded_note_id}"),
            ("openreview_api2_pdf_from_note_id", f"https://api2.openreview.net/pdf?id={encoded_note_id}"),
        ]:
            candidates.append({
                "kind": kind,
                "pdf_url": url,
                "openreview_id": note_id,
                "accepted": True,
            })
    return candidates


def _publisher_page_candidates_from_url(
    paper: dict,
    url: str,
    *,
    kind: str,
    scan_assets: bool = False,
    allow_pdf_text_identity_check: bool = False,
    include_openreview_locators: bool = False,
) -> list[dict]:
    if "openreview.net" in str(url).lower() and not _openreview_anonymous_http_enabled():
        return [{
            "kind": kind,
            "source_url": url,
            "accepted": False,
            "reason": "anonymous_openreview_http_disabled",
            "message_zh": "当前显式禁用匿名 OpenReview HTML/PDF/API 兜底；请配置官方 openreview-py 凭据，或移除 READING_OPENREVIEW_ALLOW_ANONYMOUS_HTTP=0。",
        }]
    resolved_url, html, receipt = _request_html(url, timeout=35)
    if not html:
        return [{"kind": kind, "source_url": url, "accepted": False, **receipt}]
    title_values = _meta_content_values(html, {"citation_title", "dc.title", "og:title"})
    doi_values = _meta_content_values(html, {"citation_doi", "dc.identifier", "dc.identifier.doi", "prism.doi"})
    author_values = _meta_content_values(html, {"citation_author", "dc.creator"})
    identity_ok = any(_same_paper_identity_ok(paper, candidate_title=title, candidate_authors=author_values, candidate_doi=" ".join(doi_values)) for title in title_values)
    if not identity_ok and doi_values:
        identity_ok = any(_doi_from_paper(paper) and _doi_from_paper(paper) == _doi_from_text(value) for value in doi_values)
    if not identity_ok and title_values and _matches_declared_landing_url(paper, url, resolved_url):
        identity_ok = any(paper_title_similarity(paper.get("title"), title) >= 0.97 for title in title_values)
    pdf_links = []
    for pdf_url in _meta_content_values(html, {"citation_pdf_url"}):
        if pdf_url.startswith("http"):
            pdf_links.append(pdf_url)
    pdf_links.extend(_html_pdf_links(html, resolved_url or url))
    openreview_locators: dict[str, str] = {}
    if include_openreview_locators:
        for link in _anchor_links(html, resolved_url or url):
            note_id = _openreview_note_id_from_url(link.get("url"))
            if not note_id:
                continue
            pdf_url = f"https://openreview.net/pdf?id={quote(note_id, safe='')}"
            openreview_locators[pdf_url] = str(link.get("url") or "")
            if pdf_url not in pdf_links:
                pdf_links.append(pdf_url)
    asset_scan = _asset_pdf_links(html, resolved_url or url) if scan_assets else []
    for item in asset_scan:
        pdf_url = str(item.get("pdf_url") or "")
        if item.get("accepted") and pdf_url and pdf_url not in pdf_links:
            pdf_links.append(pdf_url)
    linked_pdf_candidates = _linked_research_page_pdf_candidates(html, resolved_url or url, source_kind=kind) if scan_assets else []
    for item in linked_pdf_candidates:
        pdf_url = str(item.get("pdf_url") or "")
        if item.get("accepted") and pdf_url and pdf_url not in pdf_links:
            pdf_links.append(pdf_url)
    pdf_links = [pdf_url for pdf_url in pdf_links if not _is_likely_cv_or_resume_pdf_url(pdf_url)]
    if not identity_ok:
        if allow_pdf_text_identity_check and pdf_links:
            return [
                {
                    **receipt,
                    "kind": kind + "_pdf_link_requires_text_identity",
                    "source_url": url,
                    "resolved_url": resolved_url,
                    "pdf_url": pdf_url,
                    "accepted": True,
                    "requires_pdf_text_identity_check": True,
                    "asset_scan": asset_scan[:8],
                    "linked_research_page_scan": linked_pdf_candidates[:8],
                    "openreview_note_id": _openreview_note_id_from_url(pdf_url),
                    "source_openreview_url": openreview_locators.get(pdf_url, ""),
                }
                for pdf_url in pdf_links[:5]
            ]
        return [{
            **receipt,
            "kind": kind,
            "source_url": url,
            "resolved_url": resolved_url,
            "accepted": False,
            "reason": "identity_mismatch_or_missing_metadata",
            "candidate_titles": title_values[:3],
            "candidate_dois": doi_values[:3],
            "asset_scan": asset_scan[:8],
            "linked_research_page_scan": linked_pdf_candidates[:8],
        }]
    candidates: list[dict] = []
    for pdf_url in pdf_links:
        candidates.append({
            "kind": kind + ("_openreview_pdf_link" if pdf_url in openreview_locators else "_pdf_link"),
            "pdf_url": pdf_url,
            "landing_page_url": resolved_url or url,
            "accepted": True,
            "asset_scan": asset_scan[:8],
            "linked_research_page_scan": linked_pdf_candidates[:8],
            "openreview_note_id": _openreview_note_id_from_url(pdf_url),
            "source_openreview_url": openreview_locators.get(pdf_url, ""),
        })
    html_urls = _meta_content_values(html, {"citation_fulltext_html_url"})
    if resolved_url or html_urls:
        candidates.append({
            "kind": kind + "_verified_landing_page",
            "pdf_url": "",
            "landing_page_url": html_urls[0] if html_urls else resolved_url or url,
            "accepted": True,
        })
    return candidates or [{
        **receipt,
            "kind": kind,
            "source_url": url,
            "resolved_url": resolved_url,
            "accepted": False,
            "reason": "same_paper_page_without_pdf_link",
            "asset_scan": asset_scan[:8],
            "linked_research_page_scan": linked_pdf_candidates[:8],
    }]


def _conference_official_resource_urls(paper: dict, limit: int = 4) -> list[dict]:
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    source_urls = []
    for value in [metadata.get("conference_virtual_url"), paper.get("url"), paper.get("html_url")]:
        url = str(value or "").strip()
        if url.startswith("http") and "/virtual/" in url and url not in source_urls:
            source_urls.append(url)
    resources: list[dict] = []
    for source_url in source_urls[:2]:
        resolved_url, html, receipt = _request_html(source_url, timeout=35)
        if not html:
            resources.append({"kind": "conference_official_resource_page_scan", "source_url": source_url, "accepted": False, **receipt})
            continue
        for match in re.finditer(r"<a\b[^>]*\bhref=['\"]([^'\"]+)['\"][^>]*>(.*?)</a>", html, flags=re.I | re.S):
            href = match.group(1)
            label = re.sub(r"<[^>]+>", " ", match.group(2))
            label = re.sub(r"\s+", " ", label).strip()
            absolute = urljoin(resolved_url or source_url, href)
            lowered = absolute.lower()
            label_lower = label.lower()
            if not absolute.startswith("http"):
                continue
            if any(skip in lowered for skip in ["openreview.net", "codeofconduct", "/papers.html", "posterpdfs", "slideslive.com"]):
                continue
            if not (
                lowered.endswith(".pdf")
                or "project" in label_lower
                or "homepage" in label_lower
                or "paper" in label_lower
                or "supplement" in label_lower
                or "artifact" in label_lower
            ):
                continue
            item = {
                "kind": "conference_official_resource_page",
                "source_url": source_url,
                "resolved_source_url": resolved_url or source_url,
                "url": absolute,
                "label": label,
                "accepted": True,
                "fetch": receipt,
            }
            if absolute not in [str(existing.get("url")) for existing in resources]:
                resources.append(item)
            if len([item for item in resources if item.get("accepted")]) >= limit:
                return resources
    return resources


def _same_paper_landing_urls(paper: dict) -> list[str]:
    urls: list[str] = []
    doi = _doi_from_paper(paper)
    if doi:
        urls.append("https://doi.org/" + doi)
        if doi.startswith("10.1126/"):
            urls.append(f"https://www.science.org/doi/full/{doi}")
        if doi.startswith("10.1038/") and "/" in doi:
            urls.append("https://www.nature.com/articles/" + quote(doi.split("/", 1)[1], safe=""))
        if doi.startswith("10.1101/") or doi.startswith("10.64898/"):
            urls.append(f"https://www.biorxiv.org/content/{doi}")
    for key in ["html_url", "url", "abs_url"]:
        value = str(paper.get(key) or "").strip()
        if value and value.startswith("http") and not _is_openreview_url(value):
            urls.append(value)
    out: list[str] = []
    for url in urls:
        if url not in out:
            out.append(url)
    return out


def _publisher_page_pdf_candidates(paper: dict, limit: int = 5) -> list[dict]:
    candidates: list[dict] = []
    for url in _same_paper_landing_urls(paper)[:limit]:
        for candidate in _publisher_page_candidates_from_url(paper, url, kind="publisher_same_paper_page"):
            candidates.append(candidate)
    seen_urls = set(_same_paper_landing_urls(paper)[:limit])
    for resource in _conference_official_resource_urls(paper):
        if not resource.get("accepted"):
            candidates.append(resource)
            continue
        url = str(resource.get("url") or "")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        if url.lower().endswith(".pdf"):
            candidates.append({
                "kind": "conference_official_resource_pdf_requires_text_identity",
                "pdf_url": url,
                "conference_resource": resource,
                "accepted": True,
                "requires_pdf_text_identity_check": True,
            })
            continue
        for candidate in _publisher_page_candidates_from_url(
            paper,
            url,
            kind="conference_official_resource_page",
            scan_assets=True,
            allow_pdf_text_identity_check=True,
        ):
            candidate["conference_resource"] = resource
            candidates.append(candidate)
    return candidates


def _same_paper_landing_page_candidates(paper: dict, limit: int = 5) -> list[dict]:
    """Return verified same-paper landing-page hints for HTML/XML fallback callers."""
    candidates = list(_publisher_page_pdf_candidates(paper, limit=limit))
    candidates.extend(_springer_nature_api_candidates(paper))
    return candidates


def _arxiv_title_query(title: str) -> str:
    terms = [term for term in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", title or "") if len(term) >= 3]
    return " AND ".join(f"ti:{term}" for term in terms[:10])


def _arxiv_title_queries(title: str) -> list[str]:
    cleaned = " ".join(str(title or "").split())
    terms_query = _arxiv_title_query(cleaned)
    queries: list[str] = []
    if terms_query:
        queries.append(terms_query)
    title_head = cleaned.split(":", 1)[0].strip()
    if title_head and len(title_head.split()) >= 3:
        queries.append(f'ti:"{title_head}"')
        queries.append(f'all:"{title_head}"')
    compact = re.sub(r"[^A-Za-z0-9 /-]+", " ", cleaned).strip()
    head_terms = " ".join(compact.split()[:6])
    if head_terms and len(head_terms.split()) >= 3:
        queries.append(f'ti:"{head_terms}"')
    out: list[str] = []
    for query in queries:
        if query and query not in out:
            out.append(query)
    return out


def _arxiv_pdf_candidates(paper: dict, max_results: int = 5) -> list[dict]:
    title = str(paper.get("title") or "").strip()
    queries = _arxiv_title_queries(title)
    if not queries:
        return []
    ns = {"a": "http://www.w3.org/2005/Atom"}
    expected_authors = paper_author_family_tokens(paper.get("authors"))
    candidates: list[dict] = []
    seen_entries: set[str] = set()
    attempts: list[dict] = []
    for query in queries:
        url = "https://export.arxiv.org/api/query?search_query=" + quote_plus(query) + f"&start=0&max_results={max_results}"
        try:
            response = service_get(url, timeout=45)
            if response.status_code != 200:
                attempts.append({
                    "kind": "arxiv_title_search",
                    "url": url,
                    "query": query,
                    "accepted": False,
                    **response_receipt(response),
                    "reason": "http_429_rate_limited" if response.status_code == 429 else f"http_{response.status_code}",
                    "message_zh": "arXiv 官方 API 当前限流；已按官方要求停止继续请求，避免提高封禁风险。" if response.status_code == 429 else "",
                })
                if response.status_code == 429:
                    break
                continue
            root = ET.fromstring(response.content)
        except Exception as exc:
            attempts.append({"kind": "arxiv_title_search", "url": url, "query": query, "status_code": 0, "accepted": False, "error": exc.__class__.__name__})
            continue
        entries = root.findall("a:entry", ns)
        if not entries:
            attempts.append({"kind": "arxiv_title_search", "url": url, "query": query, "status_code": 200, "accepted": False, "reason": "no_arxiv_candidates"})
        for entry in entries:
            candidate_title = " ".join((entry.findtext("a:title", default="", namespaces=ns) or "").split())
            entry_id = entry.findtext("a:id", default="", namespaces=ns) or ""
            if entry_id in seen_entries:
                continue
            seen_entries.add(entry_id)
            candidate_authors = [node.text or "" for node in entry.findall("a:author/a:name", ns)]
            pdf_url = ""
            for link in entry.findall("a:link", ns):
                if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
                    pdf_url = link.attrib.get("href", "")
                    break
            if not pdf_url and "/abs/" in entry_id:
                pdf_url = entry_id.replace("/abs/", "/pdf/")
            similarity = paper_title_similarity(title, candidate_title)
            author_overlap = sorted(expected_authors & paper_author_family_tokens(candidate_authors))
            accepted = bool(pdf_url and (similarity >= 0.95 if not expected_authors else (similarity >= 0.82 and author_overlap) or (similarity >= 0.78 and len(author_overlap) >= 2)))
            candidates.append({
                "kind": "arxiv_title_search_candidate",
                "search_url": url,
                "query": query,
                "title": candidate_title,
                "entry_id": entry_id,
                "pdf_url": pdf_url,
                "similarity": round(similarity, 4),
                "author_overlap": author_overlap,
                "accepted": accepted,
            })
            if accepted:
                return candidates
    return candidates or attempts or [{"kind": "arxiv_title_search", "accepted": False, "reason": "no_arxiv_candidates"}]



DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.I)


def _doi_from_text(value: object) -> str:
    match = DOI_RE.search(str(value or ""))
    if not match:
        return ""
    doi = match.group(1).strip().rstrip(".,;:)]}")
    return doi.lower().removeprefix("doi:").removeprefix("https://doi.org/")


def _doi_from_paper(paper: dict) -> str:
    values = [paper.get(key) for key in ["doi", "published_doi", "url", "abs_url", "pdf_url", "input_article"]]
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    values.extend(metadata.get(key) for key in ["doi", "published_doi", "crossref_url", "url", "pdf_url"])
    for value in values:
        doi = _doi_from_text(value)
        if doi:
            return doi
    return ""


def _iter_pdf_url_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_iter_pdf_url_values(item))
        return out
    if isinstance(value, dict):
        out = []
        for key in ["url_for_pdf", "pdf_url", "url", "href"]:
            if value.get(key):
                out.append(str(value.get(key)))
        return out
    return []


def _openalex_headers() -> dict[str, str]:
    return {"Accept": "application/json"}


def _openalex_api_params(extra: dict[str, str] | None = None) -> dict[str, str]:
    params = dict(extra or {})
    api_key = str(os.environ.get("OPENALEX_API_KEY") or "").strip()
    if api_key:
        params["api_key"] = api_key
    mailto = service_contact_email("openalex")
    if mailto:
        params["mailto"] = mailto
    return params


def _openalex_work_candidates(work: dict, paper: dict, *, source_kind: str) -> list[dict]:
    title = str(paper.get("title") or "")
    work_title = str(work.get("display_name") or work.get("title") or "")
    if title and work_title and paper_title_similarity(title, work_title) < 0.72:
        return []
    candidates: list[dict] = []
    locations = []
    for key in ["best_oa_location", "primary_location"]:
        value = work.get(key)
        if isinstance(value, dict):
            locations.append(value)
    locations.extend(item for item in work.get("locations") or [] if isinstance(item, dict))
    for location in locations:
        source = location.get("source") if isinstance(location.get("source"), dict) else {}
        landing_page_url = location.get("landing_page_url")
        for url in _iter_pdf_url_values(location):
            if not str(url).startswith("http"):
                continue
            candidates.append({
                "kind": source_kind,
                "pdf_url": str(url),
                "openalex_id": work.get("id"),
                "openalex_title": work_title,
                "openalex_location": {
                    "landing_page_url": location.get("landing_page_url"),
                    "version": location.get("version"),
                    "license": location.get("license"),
                    "source": source.get("display_name") or source.get("id"),
                },
                "accepted": True,
            })
        if landing_page_url:
            candidates.append({
                "kind": source_kind + "_landing_page",
                "pdf_url": "",
                "landing_page_url": landing_page_url,
                "openalex_id": work.get("id"),
                "openalex_title": work_title,
                "openalex_location": {
                    "version": location.get("version"),
                    "license": location.get("license"),
                    "source": source.get("display_name") or source.get("id"),
                },
                "accepted": True,
            })
    return candidates


def _openalex_pdf_candidates(paper: dict, limit: int = 5) -> list[dict]:
    candidates: list[dict] = []
    failures: list[dict] = []
    doi = _doi_from_paper(paper)
    try:
        if doi:
            url = "https://api.openalex.org/works/doi:" + quote(f"https://doi.org/{doi}", safe="")
            response = service_get(url, params=_openalex_api_params(), timeout=30, headers=_openalex_headers())
            if response.status_code == 200:
                payload = response.json()
                if isinstance(payload, dict):
                    candidates.extend(_openalex_work_candidates(payload, paper, source_kind="openalex_doi_oa_pdf"))
            else:
                failures.append({"kind": "openalex_doi_oa_pdf", "accepted": False, "reason": f"http_{response.status_code}", **response_receipt(response)})
        if candidates:
            return candidates
        title = str(paper.get("title") or "").strip()
        if len(title.split()) >= 3:
            params = _openalex_api_params({"search": title, "filter": "has_pdf_url:true", "per-page": str(limit), "select": "id,display_name,doi,best_oa_location,primary_location,locations"})
            response = service_get("https://api.openalex.org/works", params=params, timeout=30, headers=_openalex_headers())
            if response.status_code == 200:
                payload = response.json()
                for work in payload.get("results") or []:
                    if isinstance(work, dict):
                        candidates.extend(_openalex_work_candidates(work, paper, source_kind="openalex_title_oa_pdf"))
            else:
                failures.append({"kind": "openalex_title_oa_pdf", "accepted": False, "reason": f"http_{response.status_code}", **response_receipt(response)})
    except Exception:
        return candidates or failures
    return candidates or failures or [{"kind": "openalex_oa_pdf", "accepted": False, "reason": "no_openalex_pdf_location"}]


def _unpaywall_pdf_candidates(paper: dict) -> list[dict]:
    doi = _doi_from_paper(paper)
    email = str(os.environ.get("UNPAYWALL_EMAIL") or "").strip()
    if not doi or not email:
        return [{"kind": "unpaywall_oa_pdf", "accepted": False, **(missing_official_access_reason("unpaywall") or {"reason": "missing_doi_or_email"})}] if doi else []
    try:
        url = "https://api.unpaywall.org/v2/" + quote(doi, safe="")
        response = service_get(url, params={"email": email}, timeout=30, headers={"Accept": "application/json"})
        if response.status_code != 200:
            return [{"kind": "unpaywall_oa_pdf", "accepted": False, "reason": f"http_{response.status_code}", **response_receipt(response)}]
        payload = response.json()
    except Exception:
        return []
    candidates: list[dict] = []
    locations = []
    if isinstance(payload.get("best_oa_location"), dict):
        locations.append(payload.get("best_oa_location"))
    locations.extend(item for item in payload.get("oa_locations") or [] if isinstance(item, dict))
    for location in locations:
        for pdf_url in _iter_pdf_url_values({"url_for_pdf": location.get("url_for_pdf"), "url": location.get("url")}):
            if str(pdf_url).startswith("http"):
                candidates.append({
                    "kind": "unpaywall_oa_pdf",
                    "pdf_url": str(pdf_url),
                    "doi": doi,
                    "oa_status": payload.get("oa_status"),
                    "host_type": location.get("host_type"),
                    "version": location.get("version"),
                    "license": location.get("license"),
                    "accepted": True,
                })
    return candidates


def _runtime_cached_pdf_candidates(paper: dict, limit: int = 4) -> list[dict]:
    if env_bool("READING_DISABLE_RUNTIME_CACHE", False):
        return []

    def add_index(cache: dict[str, list[dict]], key: str, candidate: dict) -> None:
        if key:
            cache.setdefault(key, []).append(candidate)

    def build_index() -> dict[str, list[dict]]:
        cache: dict[str, list[dict]] = {}
        roots = [*CACHE_BATCH_TEST_ROOTS, *CACHE_RUN_ROOTS]
        for root in roots:
            if not root.exists():
                continue
            for result_path in root.glob("**/read_results.json"):
                try:
                    payload = read_json(result_path, {})
                except Exception:
                    continue
                packet = payload.get("full_text_packet") if isinstance(payload.get("full_text_packet"), dict) else {}
                cached_paper = payload.get("paper") if isinstance(payload.get("paper"), dict) else {}
                cached_pdf_path_value = str(packet.get("pdf_path") or "").strip()
                if not cached_pdf_path_value:
                    continue
                cached_pdf_path = resolve_reading_path(cached_pdf_path_value)
                cached_pdf_url = str(packet.get("pdf_url") or "").strip()
                cached_doi = _doi_from_paper(cached_paper)
                if not cached_pdf_path.is_file() or cached_pdf_path.suffix.lower() != ".pdf":
                    continue
                try:
                    cached_pdf_path = ensure_inside_reading(cached_pdf_path, label="PDF cache index source")
                except Exception:
                    continue
                candidate = {
                    "kind": "reading_runtime_cached_pdf",
                    "pdf_url": cached_pdf_url or str(cached_pdf_path),
                    "cached_pdf_path": str(cached_pdf_path),
                    "cached_read_results": str(result_path),
                    "cached_full_text_chars": packet.get("full_text_chars") or packet.get("text_chars") or 0,
                    "cached_doi": cached_doi,
                    "requires_pdf_text_identity_check": True,
                    "accepted": True,
                }
                add_index(cache, "title:" + _normalized_title_key(cached_paper.get("title") or packet.get("title")), candidate)
                add_index(cache, "url:" + cached_pdf_url, candidate)
                add_index(cache, "doi:" + cached_doi.lower(), candidate)
        return cache

    global _PDF_CACHE_INDEX
    if _PDF_CACHE_INDEX is None:
        _PDF_CACHE_INDEX = build_index()
    title_key = _normalized_title_key(paper.get("title"))
    pdf_url = str(paper.get("pdf_url") or "").strip()
    doi = _doi_from_paper(paper).lower()
    if not title_key and not pdf_url and not doi:
        return []
    seen_paths: set[str] = set()
    candidates: list[dict] = []
    for cache_key in ["doi:" + doi if doi else "", "title:" + title_key if title_key else "", "url:" + pdf_url if pdf_url else ""]:
        for candidate in _PDF_CACHE_INDEX.get(cache_key, []):
            key = str(candidate.get("cached_pdf_path") or "")
            if key in seen_paths:
                continue
            seen_paths.add(key)
            item = dict(candidate)
            if doi and str(item.get("cached_doi") or "").lower() == doi:
                item["requires_pdf_text_identity_check"] = False
                item["runtime_cache_identity_basis"] = "doi_exact_match"
            candidates.append(item)
            if len(candidates) >= limit:
                return candidates
    return candidates


def _pdf_candidates_for_reading(paper: dict, *, fast_only: bool = False) -> list[dict]:
    candidates: list[dict] = []
    seen: set[str] = set()
    discovery: list[dict] = []
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    locator_keys = [
        "url", "abs_url", "html_url", "pdf_url", "doi", "published_doi", "arxiv_id",
        "openreview_id", "openreview_forum", "openreview_forum_url", "openreview_url",
        "openreview_pdf_url", "forum", "note_id", "paper_url", "paper_pdf_url",
    ]
    locator_values = [paper.get(key) for key in locator_keys]
    locator_values.extend(metadata.get(key) for key in locator_keys)
    locator_values.extend([paper.get("candidate_pdf_urls"), paper.get("pdf_urls"), paper.get("oa_pdf_urls")])
    lowered_url = " ".join(str(value or "") for value in locator_values).lower()
    has_openreview_locator = "openreview.net" in lowered_url or any(
        str(paper.get(key) or metadata.get(key) or "").strip()
        for key in ["openreview_id", "openreview_forum", "openreview_forum_url", "openreview_url", "forum", "note_id"]
    )
    has_explicit_locator = bool(
        _doi_from_paper(paper)
        or re.search(r"https?://", lowered_url)
        or str(paper.get("arxiv_id") or metadata.get("arxiv_id") or "").strip()
        or has_openreview_locator
    )
    has_metadata_only_locator = bool(
        metadata.get("title_index_only")
        or ("/virtual/" in lowered_url and "/poster/" in lowered_url)
        or ("/virtual/" in lowered_url and "/oral/" in lowered_url)
    )
    title_only_lookup = bool(
        len(str(paper.get("title") or "").split()) >= 3
        and (not has_explicit_locator or has_metadata_only_locator)
    )
    conference_title_lookup = bool(official_conference_title_search_specs(paper))
    prefer_arxiv_title_search = bool(
        str(paper.get("title") or "").strip()
        and (
            metadata.get("title_index_only")
            or ("/virtual/" in lowered_url and "/poster/" in lowered_url)
            or ("/virtual/" in lowered_url and "/oral/" in lowered_url)
        )
    )
    arxiv_title_search_attempted = False
    openreview_official_attempted = False
    semantic_scholar_attempted = False
    search_result_attempted = False
    source_lower = str(paper.get("source") or "").lower()
    venue_lower = str(paper.get("venue") or "").lower()
    metadata_channel_lower = str(metadata.get("conference_channel") or "").lower()
    is_iclr_like = (
        "iclr" in lowered_url
        or "iclr" in venue_lower
        or source_lower == "iclr"
        or metadata_channel_lower == "iclr"
    )
    force_arxiv_title_search = bool(
        str(paper.get("title") or "").strip()
        and (
            has_openreview_locator
            or is_iclr_like
            or "icml" in lowered_url
            or "icml" in str(paper.get("venue") or "").lower()
            or source_lower in {"openreview", "icml_downloads"}
        )
    )

    def add(kind: str, url: object, **extra: object) -> None:
        pdf_url = _normalize_arxiv_https_url(url)
        if not pdf_url or not (pdf_url.startswith("http") or pdf_url.startswith("openreview://")):
            return
        if pdf_url in seen:
            if extra.get("cached_pdf"):
                cached_path = str((extra.get("cached_pdf") or {}).get("cached_pdf_path") or "")
                if not any(str((candidate.get("cached_pdf") or {}).get("cached_pdf_path") or "") == cached_path for candidate in candidates):
                    candidates.append({"kind": kind, "pdf_url": pdf_url, "accepted": True, **extra})
            else:
                for index, candidate in enumerate(candidates):
                    if candidate.get("pdf_url") == pdf_url and candidate.get("cached_pdf"):
                        candidates[index] = {"kind": kind, "pdf_url": pdf_url, "accepted": True, **extra}
                        break
            return
        seen.add(pdf_url)
        candidates.append({"kind": kind, "pdf_url": pdf_url, "accepted": True, **extra})

    publisher_direct_candidates = _publisher_direct_pdf_candidates(paper)
    biorxiv_direct_first = _is_biorxiv_input(paper) and any(
        candidate.get("accepted")
        and candidate.get("pdf_url")
        and str(candidate.get("kind") or "") == "doi_direct_biorxiv_full_pdf"
        for candidate in publisher_direct_candidates
    )
    if biorxiv_direct_first:
        for candidate in publisher_direct_candidates:
            if (
                candidate.get("accepted")
                and candidate.get("pdf_url")
                and str(candidate.get("kind") or "") == "doi_direct_biorxiv_full_pdf"
            ):
                add(str(candidate.get("kind") or "publisher_direct_pdf"), candidate.get("pdf_url"), publisher_direct_match=candidate)

    for candidate in official_conference_pdf_candidates(paper):
        if candidate.get("accepted"):
            add(str(candidate.get("kind") or "conference_official_pdf"), candidate.get("pdf_url"), conference_official_match=candidate)
        else:
            discovery.append(candidate)
    raw_openreview_pdf = _openreview_pdf_url(paper)
    should_try_openreview_official = bool(
        raw_openreview_pdf
        or has_openreview_locator
        or is_iclr_like
        or "icml" in lowered_url
        or "icml" in venue_lower
        or source_lower in {"openreview", "icml_downloads", "icml_official_virtual"}
        or (title_only_lookup and not fast_only)
    )
    if should_try_openreview_official:
        openreview_official_attempted = True
        for candidate in openreview_official_pdf_candidates(paper):
            if candidate.get("accepted"):
                add(str(candidate.get("kind") or "openreview_official_pdf"), candidate.get("pdf_url"), openreview_official_match=candidate)
            else:
                discovery.append(candidate)
    add("indexed_pdf", paper.get("pdf_url"))
    add("neurips_pdf_from_abstract_url", _neurips_pdf_url_from_abstract(paper.get("url") or paper.get("abs_url")))
    for key in ["best_oa_pdf_url", "openalex_pdf_url", "oa_pdf_url", "url_for_pdf", "repository_pdf_url", "publisher_pdf_url"]:
        add(key, paper.get(key))
    for key in ["openalex_pdf_url", "pdf_url", "best_oa_pdf_url", "url_for_pdf"]:
        add("metadata_" + key, metadata.get(key))
    cached_pdf_path = str(metadata.get("reading_runtime_cached_pdf_path") or "").strip()
    if cached_pdf_path:
        cached_path = Path(cached_pdf_path)
        if cached_path.is_file() and cached_path.suffix.lower() == ".pdf":
            add("reading_runtime_cached_pdf", paper.get("pdf_url") or metadata.get("pdf_url") or cached_pdf_path, cached_pdf={"cached_pdf_path": cached_pdf_path, "cached_read_results": metadata.get("reading_runtime_cached_read_results")})
    external_ids = metadata.get("semantic_scholar_external_ids") if isinstance(metadata.get("semantic_scholar_external_ids"), dict) else {}
    arxiv_id = str(external_ids.get("ArXiv") or "").strip()
    if arxiv_id:
        add("metadata_semantic_scholar_arxiv_pdf", "https://arxiv.org/pdf/" + arxiv_id)
    if not fast_only and (prefer_arxiv_title_search or force_arxiv_title_search):
        arxiv_title_search_attempted = True
        for candidate in _arxiv_pdf_candidates(paper):
            if candidate.get("accepted"):
                add("arxiv_title_verified_pdf", candidate.get("pdf_url"), arxiv_match=candidate)
            else:
                discovery.append(candidate)
    input_page_url = str(paper.get("url") or paper.get("abs_url") or "")
    if not fast_only and not _is_openreview_url(input_page_url):
        for candidate in _pdf_links_from_html_page(input_page_url):
            if candidate.get("accepted"):
                add(str(candidate.get("kind") or "html_page_pdf_link"), candidate.get("pdf_url"), html_pdf_link=candidate)
    for key in ["pdf_urls", "oa_pdf_urls", "candidate_pdf_urls"]:
        for value in _iter_pdf_url_values(paper.get(key)):
            add(key, value)
    if raw_openreview_pdf and _openreview_anonymous_http_enabled():
        add("openreview_pdf_from_forum_url", raw_openreview_pdf)
        for candidate in _openreview_direct_pdf_variant_candidates(paper):
            if candidate.get("accepted"):
                add(str(candidate.get("kind") or "openreview_pdf_variant"), candidate.get("pdf_url"), openreview_pdf_variant_match=candidate)
            else:
                discovery.append(candidate)
        for candidate in _openreview_attachment_pdf_candidates(paper):
            if candidate.get("accepted"):
                add(str(candidate.get("kind") or "openreview_attachment_pdf"), candidate.get("pdf_url"), openreview_attachment_match=candidate)
            else:
                discovery.append(candidate)
    elif raw_openreview_pdf:
        discovery.append({
            "kind": "openreview_pdf_from_forum_url",
            "accepted": False,
            "pdf_url": raw_openreview_pdf,
            "reason": "anonymous_openreview_http_disabled",
            "message_zh": "当前显式禁用匿名 OpenReview 网页/PDF 兜底；请配置官方 openreview-py 凭据或使用 arXiv/OpenAlex 等公开镜像。",
        })
        for candidate in _openreview_direct_pdf_variant_candidates(paper):
            discovery.append({
                **candidate,
                "accepted": False,
                "reason": "anonymous_openreview_http_disabled",
                "message_zh": "当前显式禁用匿名 OpenReview PDF 变体 HTTP 兜底；请配置官方 openreview-py 凭据或使用 arXiv/OpenAlex 等公开镜像。",
            })
        for candidate in _openreview_attachment_pdf_candidates(paper):
            discovery.append({
                **candidate,
                "accepted": False,
                "reason": "anonymous_openreview_http_disabled",
                "message_zh": "当前显式禁用匿名 OpenReview attachment HTTP 兜底；请配置官方 openreview-py 凭据或使用 arXiv/OpenAlex 等公开镜像。",
            })
    for candidate in publisher_direct_candidates:
        if candidate.get("accepted"):
            add(str(candidate.get("kind") or "publisher_direct_pdf"), candidate.get("pdf_url"), publisher_direct_match=candidate)
    for candidate in _runtime_cached_pdf_candidates(paper):
        if candidate.get("accepted"):
            add(
                str(candidate.get("kind") or "reading_runtime_cached_pdf"),
                candidate.get("pdf_url"),
                cached_pdf=candidate,
                requires_pdf_text_identity_check=candidate.get("requires_pdf_text_identity_check"),
            )
    if fast_only:
        paper.pop("_same_paper_semantic_scholar_attempted", None)
        paper.pop("_same_paper_search_result_attempted", None)
        paper["_same_paper_pdf_candidate_discovery"] = discovery
        return candidates
    for candidate in _springer_nature_api_candidates(paper):
        if candidate.get("accepted") and candidate.get("pdf_url"):
            add(str(candidate.get("kind") or "springer_nature_openaccess_api_pdf"), candidate.get("pdf_url"), springer_nature_match=candidate)
        else:
            discovery.append(candidate)
    for candidate in _crossref_pdf_candidates(paper):
        if candidate.get("accepted"):
            add(str(candidate.get("kind") or "crossref_same_paper_pdf_link"), candidate.get("pdf_url"), crossref_match=candidate)
        else:
            discovery.append(candidate)
    if _is_acm_doi_input(paper):
        discovery.append({
            "kind": "publisher_same_paper_page",
            "accepted": False,
            "reason": "deferred_until_after_acm_official_pdf_attempt",
            "message_zh": "ACM DOI 输入先尝试官方 PDF；publisher landing page 扫描延后到官方 PDF 失败后，避免预扫页面触发 challenge 后跳过官方 PDF。",
        })
    elif biorxiv_direct_first:
        discovery.append({
            "kind": "publisher_same_paper_page",
            "accepted": False,
            "reason": "deferred_until_after_biorxiv_official_pdf_attempt",
            "message_zh": "bioRxiv DOI 输入先尝试官方 full PDF；publisher landing page 扫描延后到官方 PDF 非 challenge 失败后，避免预扫页面触发 Cloudflare 冷却后跳过官方 PDF。",
        })
    else:
        for candidate in _publisher_page_pdf_candidates(paper):
            if candidate.get("accepted") and candidate.get("pdf_url"):
                add(
                    str(candidate.get("kind") or "publisher_same_paper_pdf"),
                    candidate.get("pdf_url"),
                    publisher_page_match=candidate,
                    requires_pdf_text_identity_check=candidate.get("requires_pdf_text_identity_check"),
                    conference_resource=candidate.get("conference_resource"),
                )
            else:
                discovery.append(candidate)
    mlanthology_openreview_ids: set[str] = set()
    for candidate in _iclr_mlanthology_candidates(paper):
        mlanthology_note_id = str(candidate.get("openreview_note_id") or "").strip()
        if mlanthology_note_id:
            mlanthology_openreview_ids.add(mlanthology_note_id)
        if candidate.get("accepted") and candidate.get("pdf_url"):
            add(
                str(candidate.get("kind") or "iclr_mlanthology_pdf"),
                candidate.get("pdf_url"),
                mlanthology_match=candidate,
                requires_pdf_text_identity_check=candidate.get("requires_pdf_text_identity_check"),
            )
        else:
            discovery.append(candidate)
    chatpaper_paper = _paper_with_discovered_openreview_id(paper, mlanthology_openreview_ids)
    if has_openreview_locator or is_iclr_like:
        for candidate in _chatpaper_openreview_cached_pdf_candidates(chatpaper_paper):
            if candidate.get("accepted") and candidate.get("pdf_url"):
                add(
                    str(candidate.get("kind") or "chatpaper_openreview_cached_pdf"),
                    candidate.get("pdf_url"),
                    chatpaper_match=candidate,
                    requires_pdf_text_identity_check=candidate.get("requires_pdf_text_identity_check"),
                )
            else:
                discovery.append(candidate)
    has_verified_non_openreview_candidate = any(
        candidate.get("accepted")
        and "openreview" not in " ".join(
            str(candidate.get(key) or "").lower()
            for key in ["kind", "pdf_url", "landing_page_url", "source_url", "resolved_url"]
        )
        and not candidate.get("requires_pdf_text_identity_check")
        for candidate in candidates
    )
    if conference_title_lookup and not candidates and not search_result_attempted:
        search_result_attempted = True
        for candidate in _search_result_pdf_candidates(paper):
            if candidate.get("accepted") and candidate.get("pdf_url"):
                add(
                    str(candidate.get("kind") or "search_result_pdf"),
                    candidate.get("pdf_url"),
                    search_result_match=candidate,
                    requires_pdf_text_identity_check=True,
                )
            else:
                discovery.append(candidate)
    semantic_scholar_needed = bool(
        (
            (conference_title_lookup and not candidates)
            or ((has_openreview_locator or is_iclr_like) and not conference_title_lookup)
        )
        and not has_verified_non_openreview_candidate
    )
    if semantic_scholar_needed:
        semantic_scholar_attempted = True
        semantic_added_verified = False
        for candidate in _semantic_scholar_pdf_candidates_for_reading(paper):
            if candidate.get("accepted"):
                add(str(candidate.get("kind") or "semantic_scholar_open_access_pdf"), candidate.get("pdf_url"), semantic_scholar_match=candidate)
                semantic_added_verified = True
            else:
                discovery.append(candidate)
        if semantic_added_verified:
            has_verified_non_openreview_candidate = True
    if ((has_openreview_locator or is_iclr_like) and not has_verified_non_openreview_candidate and not search_result_attempted):
        search_result_attempted = True
        for candidate in _search_result_pdf_candidates(paper):
            if candidate.get("accepted") and candidate.get("pdf_url"):
                add(
                    str(candidate.get("kind") or "search_result_pdf"),
                    candidate.get("pdf_url"),
                    search_result_match=candidate,
                    requires_pdf_text_identity_check=candidate.get("requires_pdf_text_identity_check"),
                )
            else:
                discovery.append(candidate)
    if not raw_openreview_pdf and (has_openreview_locator or is_iclr_like) and _openreview_anonymous_http_enabled():
        for candidate in _openreview_title_pdf_candidates(paper):
            if candidate.get("accepted"):
                add(str(candidate.get("kind") or "openreview_title_verified_pdf"), candidate.get("pdf_url"), openreview_match=candidate)
            else:
                discovery.append(candidate)
    elif has_openreview_locator or force_arxiv_title_search:
        discovery.append({
            "kind": "openreview_title_search",
            "accepted": False,
            "reason": "anonymous_openreview_api_disabled",
            "message_zh": "默认不匿名请求 OpenReview API；官方 client、arXiv 标题验证和开放索引会优先尝试。",
        })
    doi = _doi_from_paper(paper)
    try_openalex = bool(doi or not candidates or force_arxiv_title_search or has_openreview_locator or "iclr" in str(paper.get("venue") or "").lower())
    if try_openalex:
        for candidate in _openalex_pdf_candidates(paper):
            if candidate.get("accepted"):
                add(str(candidate.get("kind") or "openalex_oa_pdf"), candidate.get("pdf_url"), openalex_match=candidate)
            else:
                discovery.append(candidate)
        for candidate in _unpaywall_pdf_candidates(paper):
            if candidate.get("accepted"):
                add("unpaywall_oa_pdf", candidate.get("pdf_url"), unpaywall_match=candidate)
            else:
                discovery.append(candidate)
    if not arxiv_title_search_attempted and (not candidates or _is_acm_doi_input(paper)):
        arxiv_title_search_attempted = True
        for candidate in _arxiv_pdf_candidates(paper):
            if candidate.get("accepted"):
                add("arxiv_title_verified_pdf", candidate.get("pdf_url"), arxiv_match=candidate)
            else:
                discovery.append(candidate)
    if title_only_lookup and not candidates and not openreview_official_attempted:
        openreview_official_attempted = True
        for candidate in openreview_official_pdf_candidates(paper):
            if candidate.get("accepted"):
                add(str(candidate.get("kind") or "openreview_official_pdf"), candidate.get("pdf_url"), openreview_official_match=candidate)
            else:
                discovery.append(candidate)
    if (title_only_lookup or conference_title_lookup) and not candidates and not search_result_attempted:
        search_result_attempted = True
        for candidate in _search_result_pdf_candidates(paper):
            if candidate.get("accepted") and candidate.get("pdf_url"):
                add(
                    str(candidate.get("kind") or "search_result_pdf"),
                    candidate.get("pdf_url"),
                    search_result_match=candidate,
                    requires_pdf_text_identity_check=True,
                )
            else:
                discovery.append(candidate)
    for candidate in _runtime_cached_pdf_candidates(paper):
        if candidate.get("accepted"):
            add(
                str(candidate.get("kind") or "reading_runtime_cached_pdf"),
                candidate.get("pdf_url"),
                cached_pdf=candidate,
                requires_pdf_text_identity_check=candidate.get("requires_pdf_text_identity_check"),
            )
    paper["_same_paper_pdf_candidate_discovery"] = discovery
    if semantic_scholar_attempted:
        paper["_same_paper_semantic_scholar_attempted"] = True
    else:
        paper.pop("_same_paper_semantic_scholar_attempted", None)
    if search_result_attempted:
        paper["_same_paper_search_result_attempted"] = True
    else:
        paper.pop("_same_paper_search_result_attempted", None)
    return candidates


def _download_first_readable_pdf(paper: dict, pdf_dir: Path, log: LogFn) -> tuple[bool, Path, str, dict]:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(paper.get("id") or paper.get("paper_id") or "paper")).strip("_") or "paper"
    attempts: list[dict] = []
    candidates = _pdf_candidates_for_reading(paper, fast_only=True)
    discovery = paper.pop("_same_paper_pdf_candidate_discovery", [])
    semantic_scholar_attempted = bool(paper.pop("_same_paper_semantic_scholar_attempted", False))
    search_result_attempted = bool(paper.pop("_same_paper_search_result_attempted", False))
    deferred_discovery = bool(candidates)
    if not candidates:
        candidates = _pdf_candidates_for_reading(paper)
        discovery.extend(paper.pop("_same_paper_pdf_candidate_discovery", []))
        semantic_scholar_attempted = bool(paper.pop("_same_paper_semantic_scholar_attempted", False))
        search_result_attempted = bool(paper.pop("_same_paper_search_result_attempted", False))
        deferred_discovery = False
    seen_candidate_urls = {str(candidate.get("pdf_url") or "").strip() for candidate in candidates if str(candidate.get("pdf_url") or "").strip()}
    search_after_acm_403_done = False
    search_after_biorxiv_official_done = False
    external_search_after_biorxiv_official_done = False
    challenged_services: set[str] = set()

    def acm_403_seen() -> bool:
        return any(
            isinstance(item, dict)
            and "dl.acm.org" in str(item.get("pdf_url") or item.get("url") or item.get("source_url") or "").lower()
            and (
                item.get("download_failure_reason") == "http_403"
                or item.get("reason") == "http_403"
                or (isinstance(item.get("download_receipt"), dict) and item["download_receipt"].get("reason") == "http_403")
            )
            for item in attempts
        )

    def _attempt_has_cloudflare_challenge(item: dict) -> bool:
        receipt = item.get("download_receipt") if isinstance(item.get("download_receipt"), dict) else {}
        selected_receipt = receipt.get("selected") if isinstance(receipt.get("selected"), dict) else {}
        headers_subset = selected_receipt.get("headers_subset") if isinstance(selected_receipt.get("headers_subset"), dict) else {}
        item_headers = item.get("headers_subset") if isinstance(item.get("headers_subset"), dict) else {}
        reason = str(item.get("download_failure_reason") or item.get("reason") or receipt.get("reason") or "")
        return (
            receipt.get("challenge_type") == "cloudflare"
            or selected_receipt.get("challenge_type") == "cloudflare"
            or str(headers_subset.get("cf-mitigated") or "").lower() == "challenge"
            or str(item_headers.get("cf-mitigated") or "").lower() == "challenge"
            or reason in {
                "skipped_due_to_active_challenge_cooldown",
                "skipped_due_to_prior_cloudflare_challenge",
            }
        )

    def biorxiv_official_pdf_attempt_failed() -> bool:
        for item in attempts:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "")
            if kind != "doi_direct_biorxiv_full_pdf":
                continue
            if item.get("downloaded") is False or item.get("download_failure_reason") or item.get("rejected_reason"):
                return True
        return False

    def biorxiv_official_pdf_failed_without_challenge() -> bool:
        seen_failure = False
        for item in attempts:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "")
            url = str(item.get("pdf_url") or item.get("url") or item.get("source_url") or "").lower()
            if kind != "doi_direct_biorxiv_full_pdf" and "biorxiv.org/content/" not in url:
                continue
            if _attempt_has_cloudflare_challenge(item):
                return False
            if item.get("downloaded") is False or item.get("download_failure_reason") or item.get("rejected_reason"):
                seen_failure = True
        return seen_failure

    def append_acm_403_search_candidates() -> None:
        nonlocal search_after_acm_403_done, search_result_attempted
        if not _is_acm_doi_input(paper) or search_after_acm_403_done or not acm_403_seen():
            return
        search_after_acm_403_done = True
        search_result_attempted = True
        for page_candidate in _publisher_page_pdf_candidates(paper):
            if page_candidate.get("accepted") and page_candidate.get("pdf_url"):
                candidate_url = str(page_candidate.get("pdf_url") or "").strip()
                if candidate_url and candidate_url not in seen_candidate_urls:
                    seen_candidate_urls.add(candidate_url)
                    candidates.append({
                        **page_candidate,
                        "kind": str(page_candidate.get("kind") or "publisher_same_paper_pdf_after_acm_403"),
                        "late_fallback_after_acm_403": True,
                    })
            else:
                discovery.append({**page_candidate, "late_fallback_after_acm_403": True})
        for search_candidate in _search_result_pdf_candidates(paper):
            if search_candidate.get("accepted") and search_candidate.get("pdf_url"):
                candidate_url = str(search_candidate.get("pdf_url") or "").strip()
                if candidate_url and candidate_url not in seen_candidate_urls:
                    seen_candidate_urls.add(candidate_url)
                    candidates.append({
                        **search_candidate,
                        "kind": str(search_candidate.get("kind") or "search_result_pdf_requires_text_identity"),
                        "requires_pdf_text_identity_check": True,
                        "late_fallback_after_acm_403": True,
                    })
            else:
                discovery.append({**search_candidate, "late_fallback_after_acm_403": True})

    def append_biorxiv_page_candidates_after_official_failure() -> None:
        nonlocal search_after_biorxiv_official_done
        if (
            not _is_biorxiv_input(paper)
            or search_after_biorxiv_official_done
            or not biorxiv_official_pdf_failed_without_challenge()
            or "biorxiv" in challenged_services
            or service_cooldown_remaining("biorxiv") > 0
        ):
            return
        search_after_biorxiv_official_done = True
        for page_candidate in _publisher_page_pdf_candidates(paper):
            if page_candidate.get("accepted") and page_candidate.get("pdf_url"):
                candidate_url = str(page_candidate.get("pdf_url") or "").strip()
                if candidate_url and candidate_url not in seen_candidate_urls:
                    seen_candidate_urls.add(candidate_url)
                    candidates.append({
                        **page_candidate,
                        "kind": str(page_candidate.get("kind") or "publisher_same_paper_pdf_after_biorxiv_official_pdf_failure"),
                        "late_fallback_after_biorxiv_official_pdf_failure": True,
                    })
            else:
                discovery.append({**page_candidate, "late_fallback_after_biorxiv_official_pdf_failure": True})

    def append_biorxiv_external_search_candidates_after_official_blocker() -> None:
        nonlocal external_search_after_biorxiv_official_done
        if (
            not _is_biorxiv_input(paper)
            or external_search_after_biorxiv_official_done
            or not biorxiv_official_pdf_attempt_failed()
        ):
            return
        external_search_after_biorxiv_official_done = True
        for search_candidate in _search_result_pdf_candidates(paper):
            if search_candidate.get("accepted") and search_candidate.get("pdf_url"):
                candidate_url = str(search_candidate.get("pdf_url") or "").strip()
                if candidate_url and candidate_url not in seen_candidate_urls:
                    seen_candidate_urls.add(candidate_url)
                    candidates.append({
                        **search_candidate,
                        "kind": str(search_candidate.get("kind") or "search_result_pdf_requires_text_identity"),
                        "requires_pdf_text_identity_check": True,
                        "late_fallback_after_biorxiv_official_blocker": True,
                    })
            else:
                discovery.append({**search_candidate, "late_fallback_after_biorxiv_official_blocker": True})

    def append_conference_title_search_candidates() -> None:
        nonlocal semantic_scholar_attempted, search_result_attempted
        if not official_conference_title_search_specs(paper):
            return
        if not search_result_attempted:
            search_result_attempted = True
            search_candidate_added = False
            for search_candidate in _search_result_pdf_candidates(paper):
                if search_candidate.get("accepted") and search_candidate.get("pdf_url"):
                    candidate_url = str(search_candidate.get("pdf_url") or "").strip()
                    if candidate_url and candidate_url not in seen_candidate_urls:
                        seen_candidate_urls.add(candidate_url)
                        candidates.append({
                            **search_candidate,
                            "kind": str(search_candidate.get("kind") or "search_result_pdf_requires_text_identity"),
                            "requires_pdf_text_identity_check": True,
                            "late_fallback_after_conference_candidates": True,
                        })
                        search_candidate_added = True
                else:
                    discovery.append({**search_candidate, "late_fallback_after_conference_candidates": True})
            if search_candidate_added:
                return
        if not semantic_scholar_attempted:
            semantic_scholar_attempted = True
            for semantic_candidate in _semantic_scholar_pdf_candidates_for_reading(paper):
                if semantic_candidate.get("accepted") and semantic_candidate.get("pdf_url"):
                    candidate_url = str(semantic_candidate.get("pdf_url") or "").strip()
                    if candidate_url and candidate_url not in seen_candidate_urls:
                        seen_candidate_urls.add(candidate_url)
                        candidates.append({
                            **semantic_candidate,
                            "requires_pdf_text_identity_check": True,
                            "late_fallback_after_conference_candidates": True,
                        })
                else:
                    discovery.append({**semantic_candidate, "late_fallback_after_conference_candidates": True})

    index = 0
    while True:
        if index >= len(candidates):
            if deferred_discovery:
                deferred_discovery = False
                for candidate in _pdf_candidates_for_reading(paper):
                    candidate_url = str(candidate.get("pdf_url") or "").strip()
                    if not candidate_url or candidate_url in seen_candidate_urls:
                        continue
                    seen_candidate_urls.add(candidate_url)
                    candidates.append(candidate)
                discovery.extend(paper.pop("_same_paper_pdf_candidate_discovery", []))
                semantic_scholar_attempted = semantic_scholar_attempted or bool(
                    paper.pop("_same_paper_semantic_scholar_attempted", False)
                )
                search_result_attempted = search_result_attempted or bool(
                    paper.pop("_same_paper_search_result_attempted", False)
                )
                if index < len(candidates):
                    continue
            append_conference_title_search_candidates()
            if index < len(candidates):
                continue
            break
        candidate = candidates[index]
        index += 1
        pdf_url = _normalize_arxiv_https_url(candidate.get("pdf_url"))
        candidate["pdf_url"] = pdf_url
        pdf_path = pdf_dir / f"{safe_id}_{index}.pdf"
        lowered_pdf_url = pdf_url.lower()
        candidate_service = "openreview" if pdf_url.startswith("openreview://") else service_from_url(pdf_url)
        if candidate_service in challenged_services:
            attempts.append({
                **dict(candidate),
                "downloaded": False,
                "pdf_path": "",
                "download_failure_reason": "skipped_due_to_prior_service_access_blocker",
                "download_receipt": {
                    "accepted": False,
                    "reason": "skipped_due_to_prior_service_access_blocker",
                    "service": candidate_service,
                    "url": pdf_url,
                    "message_zh": "同一服务在本篇前序请求已返回访问限制；本轮跳过同服务剩余候选，避免重复请求扩大限制风险。",
                },
            })
            continue
        if (
            "static-content.springer.com/esm/" in lowered_pdf_url
            or ("mediaobjects" in lowered_pdf_url and "_esm" in lowered_pdf_url)
            or ("science.org/doi/suppl/" in lowered_pdf_url or "/suppl_file/" in lowered_pdf_url)
        ):
            attempts.append({
                **dict(candidate),
                "downloaded": False,
                "download_failure_reason": "supplementary_material_pdf_not_article_body",
                "download_receipt": {
                    "accepted": False,
                    "reason": "supplementary_material_pdf_not_article_body",
                    "message_zh": "该 PDF 是补充材料/ESM，不是论文正文 PDF；不能作为精读正文。",
                },
            })
            continue
        cached_pdf = candidate.get("cached_pdf") if isinstance(candidate.get("cached_pdf"), dict) else {}
        cached_pdf_path_value = str(cached_pdf.get("cached_pdf_path") or "").strip()
        download_receipt: dict[str, object] = {}
        if cached_pdf_path_value:
            downloaded = _copy_pdf(Path(cached_pdf_path_value), pdf_path)
            download_receipt = {"accepted": downloaded, "source": "runtime_cached_pdf", "cached_pdf_path": cached_pdf_path_value}
        elif pdf_url.startswith("openreview://"):
            downloaded, download_receipt = download_openreview_official_pdf(candidate.get("openreview_official_match") if isinstance(candidate.get("openreview_official_match"), dict) else candidate, pdf_path)
            official_reason = str(download_receipt.get("reason") or "")
            official_attempts = download_receipt.get("attempts") if isinstance(download_receipt.get("attempts"), list) else []
            cooldown_reason = next(
                (
                    str(attempt.get("cooldown_reason") or "")
                    for attempt in official_attempts
                    if isinstance(attempt, dict) and attempt.get("cooldown_reason")
                ),
                "",
            )
            browser_fallback_allowed = official_reason in {
                "openreview_official_client_forbidden",
                "openreview_official_pdf_forbidden",
            } or (
                official_reason == "openreview_service_cooldown_active"
                and "forbidden" in cooldown_reason
                and "browser" not in cooldown_reason
            )
            if not downloaded and browser_fallback_allowed:
                official_match = candidate.get("openreview_official_match") if isinstance(candidate.get("openreview_official_match"), dict) else {}
                note_id = str(
                    download_receipt.get("openreview_note_id")
                    or candidate.get("openreview_note_id")
                    or official_match.get("openreview_note_id")
                    or ""
                ).strip()
                if note_id:
                    browser_downloaded, browser_receipt = _download_openreview_pdf_with_browser_login(
                        f"https://openreview.net/pdf?id={note_id}",
                        pdf_path,
                        after_direct_failure=True,
                    )
                    download_receipt["openreview_browser_login"] = browser_receipt
                    if browser_downloaded:
                        downloaded = True
                        download_receipt.update({"accepted": True, "reason": "openreview_browser_login_pdf"})
        else:
            downloaded, download_receipt = _download_pdf_with_receipt(pdf_url, pdf_path)
        attempt = dict(candidate)
        attempt.update({"downloaded": downloaded, "pdf_path": str(pdf_path) if downloaded else "", "download_receipt": download_receipt})
        if not downloaded and isinstance(download_receipt, dict):
            attempt["download_failure_reason"] = download_receipt.get("reason") or download_receipt.get("error") or "download_failed"
            attempts.append(attempt)
            selected_receipt = download_receipt.get("selected") if isinstance(download_receipt.get("selected"), dict) else {}
            headers_subset = selected_receipt.get("headers_subset") if isinstance(selected_receipt.get("headers_subset"), dict) else {}
            if (
                candidate_service != "openreview"
                and (
                    selected_receipt.get("challenge_type") == "cloudflare"
                    or str(headers_subset.get("cf-mitigated") or "").lower() == "challenge"
                )
            ):
                challenged_services.add(str(selected_receipt.get("service") or candidate_service))
            append_acm_403_search_candidates()
            append_biorxiv_page_candidates_after_official_failure()
            append_biorxiv_external_search_candidates_after_official_blocker()
            if attempt.get("download_failure_reason") in {"http_403", "openreview_challenge", "openreview_official_pdf_forbidden"}:
                time.sleep(_pdf_candidate_failure_sleep_sec())
            continue
        if downloaded:
            extracted_text = _extract_pdf_text(pdf_path, max_chars=20000)
            text_chars = len(extracted_text)
            attempt["text_chars"] = text_chars
            attempt["readable_pdf"] = text_chars >= FULL_TEXT_MIN_CHARS
            if text_chars < FULL_TEXT_MIN_CHARS:
                attempt["rejected_reason"] = "pdf_text_too_short_or_unextractable"
            if candidate.get("requires_pdf_text_identity_check"):
                identity_ok = _pdf_text_identity_ok(paper, extracted_text)
                attempt["pdf_text_identity_check"] = identity_ok
                if not identity_ok:
                    attempt["readable_pdf"] = False
                    attempt["rejected_reason"] = "pdf_text_identity_mismatch"
        attempts.append(attempt)
        if downloaded and attempt.get("readable_pdf") and int(attempt.get("text_chars") or 0) >= FULL_TEXT_MIN_CHARS:
            if candidate.get("kind") == "arxiv_title_verified_pdf":
                match = candidate.get("arxiv_match") if isinstance(candidate.get("arxiv_match"), dict) else {}
                log(f"Reading PDF acquired by arXiv title match: {paper.get('title', 'Untitled')} -> {match.get('entry_id') or pdf_url}")
            if candidate.get("requires_pdf_text_identity_check"):
                log(f"Reading PDF acquired by conference-linked page text identity: {paper.get('title', 'Untitled')} -> {pdf_url}")
            return True, pdf_path, pdf_url, {"attempts": attempts, "selected": attempt}
        append_acm_403_search_candidates()
        append_biorxiv_page_candidates_after_official_failure()
        append_biorxiv_external_search_candidates_after_official_blocker()
        time.sleep(0.2)
    return False, pdf_dir / f"{safe_id}.pdf", "", {
        "attempts": attempts,
        "selected": {},
        "candidate_discovery": discovery if isinstance(discovery, list) else [],
        "candidate_count": len(candidates),
        "policy": "All PDF candidates must be same-paper candidates by DOI, publisher metadata, OpenReview/arXiv title-author verification, or open-access index identity checks; unreadable PDFs are rejected and the next same-paper candidate is tried.",
    }


def _extract_pdf_text(path: Path, max_chars: int | None = None) -> str:
    try:
        import fitz
    except Exception:
        return ""
    try:
        doc = fitz.open(path)
        chunks = []
        for page in doc:
            chunks.append(page.get_text("text"))
        text = "\n".join(chunks)
        return text[:max_chars] if max_chars else text
    except Exception:
        return ""


def _clean_text(text: str, max_chars: int = 900) -> str:
    value = re.sub(r"-\s*\n\s*", "", str(text or ""))
    value = re.sub(r"\s*[:：]\s*no\s*[.。]?\s*$", "", value, flags=re.I)
    value = re.sub(r"\s*[:：]\s*[.。]\s*$", "", value)
    value = re.sub(r"\s*\n\s*", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:max_chars].rstrip()


_READ_PUBLIC_FORBIDDEN_REPLACEMENTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"对\s*(?:系统)?实现的直接含义", re.I), ""),
    (re.compile(r"系统实现", re.I), ""),
    (re.compile(r"\bGuardrail\b", re.I), ""),
    (re.compile(r"\binput_topic\b", re.I), "当前主题"),
    (re.compile(r"摘要级线索"), "摘要信息"),
    (re.compile(r"Strong/foundation\s+anchors?\s+may\s+guide\s+planning[^.。]*[.。]?", re.I), ""),
    (re.compile(r"\bpaper\s+claims?\b", re.I), "论文结论"),
    (re.compile(r"论文\s*claim", re.I), "论文结论"),
    (re.compile(r"\bclaim\s+promotion\b", re.I), ""),
    (re.compile(r"repo/data/env/experiment\s+gate", re.I), "实验验证"),
    (re.compile(r"只有\s*repo/data/env/experiment[^。]*。?", re.I), ""),
    (re.compile(r"该条目是当前用户可见推荐文章[^。]*。?"), ""),
    (re.compile(r"必须进入精读[^。]*。?"), ""),
]

_READ_PUBLIC_SECTION_RE = re.compile(
    r"(?:^|\n)\s*(?:#{1,6}\s*)?(?:对\s*(?:系统)?实现的直接含义|实验与证据限制|Guardrail|使用边界)\s*[:：]?\s*.*?(?=(?:\n\s*(?:#{1,6}\s*)?(?:原论文摘要|论文动机|详细方法|实验设置与结果|局限性|方法优缺点|方法机制|摘要|动机|方法|实验|局限)\b)|\Z)",
    re.I | re.S,
)

READING_CONTENT_QUALITY_POLICY_VERSION = "read_markdown_quality_v3"


def _sanitize_read_public_text(text: object, max_chars: int = 4000) -> str:
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    value = _READ_PUBLIC_SECTION_RE.sub("\n", value)
    for pattern, replacement in _READ_PUBLIC_FORBIDDEN_REPLACEMENTS:
        value = pattern.sub(replacement, value)
    value = re.sub(r"\s*[:：]\s*no\s*[.。]?\s*$", "", value, flags=re.I)
    value = re.sub(r"\s*[:：]\s*[.。]\s*$", "", value)
    value = re.sub(r"\s*\n\s*", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" -\t")
    return value[:max_chars].rstrip()


def _sanitize_read_public_value(value: object) -> object:
    if isinstance(value, str):
        return _sanitize_read_public_text(value, 8000)
    if isinstance(value, list):
        cleaned: list[object] = []
        for item in value:
            next_item = _sanitize_read_public_value(item)
            if next_item not in ("", [], {}):
                cleaned.append(next_item)
        return cleaned
    if isinstance(value, dict):
        return {str(key): _sanitize_read_public_value(item) for key, item in value.items()}
    return value


def _coerce_read_public_list(value: object) -> list[object]:
    if isinstance(value, list):
        return [
            _sanitize_read_public_value(item)
            for item in value
            if _sanitize_read_public_value(item) not in ("", [], {})
        ]
    text = _sanitize_read_public_text(value, 12000)
    if not text:
        return []
    markers = list(re.finditer(r"(?:^|[。；;:：\n]\s*)[（(]?\d+[）).、]\s*", text))
    parts: list[str] = []
    if len(markers) >= 2:
        for index, marker in enumerate(markers):
            start = marker.end()
            end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
            part = _sanitize_read_public_text(text[start:end].strip(" ；;。.\n\t"), 2000)
            if part:
                parts.append(part)
    if len(parts) < 2:
        parts = [
            _sanitize_read_public_text(part.strip(" ；;。.\n\t"), 2000)
            for part in re.split(r"[；;\n]+", text)
            if _sanitize_read_public_text(part.strip(" ；;。.\n\t"), 2000)
        ]
    return parts if len(parts) >= 2 else ([text] if text else [])


def _ensure_cjk_sentence_end(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    if not re.search(r"[\u4e00-\u9fff]", value):
        return value
    if value[-1] in "。！？.!?":
        return value
    if value[-1] in "）)】]`$" and len(value) > 1 and value[-2] in "。！？.!?":
        return value
    return value + "。"


def _ensure_public_sentence_value(value: object) -> object:
    if isinstance(value, str):
        return _ensure_cjk_sentence_end(value)
    if isinstance(value, list):
        return [_ensure_public_sentence_value(item) for item in value]
    return value


def _sanitize_reading_public_fields(reading: dict) -> dict:
    public_keys = {
        "summary", "abstract_zh", "abstract_original", "problem", "motivation_zh",
        "method", "method_details_zh", "method_family_zh", "experiments", "experiments_zh",
        "limitations", "limitations_zh", "method_advantages_zh", "method_disadvantages_zh",
        "relevance", "critique_reason", "reading_status_note_zh",
    }
    sentence_keys = {
        "summary", "abstract_zh", "problem", "motivation_zh", "method", "method_details_zh",
        "experiments", "experiments_zh", "limitations", "limitations_zh", "relevance",
        "critique_reason", "reading_status_note_zh",
    }
    for key in public_keys:
        if key in reading:
            reading[key] = _sanitize_read_public_value(reading[key])
            if key in {"method_advantages_zh", "method_disadvantages_zh"}:
                reading[key] = _coerce_read_public_list(reading[key])
            if key in sentence_keys or key in {"method_advantages_zh", "method_disadvantages_zh"}:
                reading[key] = _ensure_public_sentence_value(reading[key])
    return reading


def _source_evidence_from_packet(packet: dict) -> dict:
    acquisition = packet.get("pdf_acquisition") if isinstance(packet.get("pdf_acquisition"), dict) else {}
    selected = acquisition.get("selected") if isinstance(acquisition.get("selected"), dict) else {}
    return {
        "pdf_url": packet.get("pdf_url") or "",
        "pdf_path": packet.get("pdf_path") or "",
        "text_path": packet.get("text_path") or "",
        "pdf_downloaded": bool(packet.get("pdf_downloaded")),
        "pdf_text_chars": packet.get("full_text_chars") or packet.get("text_chars") or 0,
        "full_text_available": bool(packet.get("full_text_available")),
        "full_text_status": packet.get("full_text_status") or "",
        "full_text_evidence_kind": packet.get("full_text_evidence_kind") or packet.get("text_kind") or "",
        "true_pdf_full_text": bool(packet.get("true_pdf_full_text")),
        "selected_acquisition_kind": selected.get("kind") or "",
        "selected_acquisition_url": selected.get("pdf_url") or "",
    }


def _reading_machine_state(
    paper: dict,
    packet: dict,
    *,
    full_text_ready: bool,
    deep_read_complete: bool,
    note_zh: str,
) -> dict:
    weak = bool(
        paper.get("weak_candidate_for_critique")
        or paper.get("not_positive_support")
        or str(paper.get("evidence_tier") or "").lower()
        in {"nethreshold_for_reading", "critique_or_boundary_case", "retrieval_only", "weak_or_boundary"}
    )
    full_text_status = packet.get("full_text_status") or (
        "pdf_text_read" if full_text_ready else "pending_full_text_reading"
    )
    return {
        "paper_id": paper.get("id", "") or paper.get("paper_id", ""),
        "title": paper.get("title", "Untitled"),
        "url": paper.get("url", ""),
        "pdf_url": paper.get("resolved_pdf_url", "") or paper.get("pdf_url", "") or packet.get("pdf_url", ""),
        "venue": _clean_text(paper.get("venue", ""), 80),
        "year": _clean_text(paper.get("year", ""), 20),
        "score": paper.get("recommendation_score") or paper.get("score") or paper.get("fit_score"),
        "verdict": "contrast_or_boundary_reading" if weak else "core_reading",
        "support_role": "contrast_or_boundary_reference" if weak else "core_method_reference",
        "claim_ready_anchor": not weak,
        "recommended_for_deep_reading": True,
        "full_text_available": full_text_ready,
        "full_text_status": full_text_status,
        "pdf_text_read": full_text_status == "pdf_text_read",
        "pdf_text_chars": packet.get("full_text_chars") or packet.get("text_chars") or 0,
        "source_evidence": _source_evidence_from_packet(packet),
        "deep_read_complete": deep_read_complete,
        "reading_content_source": "article_markdown" if deep_read_complete else "machine_status_only",
        "reading_status_note_zh": note_zh,
    }


def _paper_key(paper: dict) -> str:
    for key in ["id", "paper_id", "url", "pdf_url"]:
        value = str(paper.get(key) or "").strip().lower()
        if value:
            return value
    return " ".join(str(paper.get("title") or "").lower().split())


def _dedupe_papers(papers: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for paper in papers:
        if not isinstance(paper, dict):
            continue
        key = _paper_key(paper)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(paper)
    return out


def _input_articles(payload: dict) -> list[dict]:
    for key in ["ranked_articles", "ranked_papers", "articles", "input_articles", "papers"]:
        value = payload.get(key)
        if isinstance(value, list):
            return _dedupe_papers([item for item in value if isinstance(item, dict)])
    if any(payload.get(key) for key in ["title", "article", "url", "pdf_url", "doi"]):
        return [payload]
    return []


def _research_context_from_input(payload: dict) -> dict:
    nested = payload.get("research_context")
    context = dict(nested) if isinstance(nested, dict) else {}
    aliases = {
        "research_topic": ("research_topic", "topic"),
        "research_interest": ("research_interest", "interest"),
        "researcher_profile": ("researcher_profile", "research_profile", "profile"),
    }
    for target, keys in aliases.items():
        if context.get(target) not in (None, "", [], {}):
            continue
        for key in keys:
            value = payload.get(key)
            if value not in (None, "", [], {}):
                context[target] = value
                break
    return context


def _select_ranked_input_articles(payload: dict, max_papers: int = 0) -> tuple[list[dict], list[dict], int]:
    all_input_papers = _input_articles(payload)
    configured_default = max(1, config_int("reading.default_max_papers", 50))
    try:
        selected_limit = int(max_papers or 0)
    except (TypeError, ValueError):
        selected_limit = 0
    if selected_limit <= 0:
        selected_limit = configured_default
    return all_input_papers, all_input_papers[:selected_limit], selected_limit


def _load_local_input_json(path: str) -> tuple[Path, dict]:
    if not path:
        raise SystemExit("--input-json is required for read. The caller must place the input file under .runtime/output/<run-id>/input/ first.")
    try:
        source = ensure_inside_input(Path(path), label="输入 JSON")
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    payload = read_json(source, {})
    if not isinstance(payload, dict):
        raise SystemExit(f"输入 JSON 不是对象：{source}")
    return source, payload


def _input_run_id(source: Path) -> str:
    rel = source.resolve(strict=False).relative_to(OUTPUT_ROOT.resolve(strict=False))
    return str(rel.parts[0]) if rel.parts else ""


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _paper_url(row: dict) -> str:
    for key in ["article", "url", "abs_url", "html_url", "pdf_url", "doi", "title"]:
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _normalize_local_input_paper(row: dict) -> dict:
    from acquisition.paper_sources import build_paper_record

    normalized = build_paper_record(
        article=_paper_url(row),
        title=str(row.get("title") or ""),
        authors=row.get("authors") or "",
        abstract=str(row.get("abstract") or row.get("summary") or ""),
        paper_id=str(row.get("paper_id") or row.get("id") or ""),
        pdf_url=str(row.get("pdf_url") or ""),
        url=str(row.get("url") or row.get("abs_url") or ""),
        source=str(row.get("source") or "local_input"),
    )
    paper = {**row, **normalized}
    normalized_metadata = normalized.get("metadata") if isinstance(normalized.get("metadata"), dict) else {}
    input_metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    if normalized_metadata or input_metadata:
        paper["metadata"] = {**normalized_metadata, **input_metadata}
    source_abstract_en = _article_source_abstract_en(paper)
    for mapping in [paper, paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}]:
        if mapping.get("abstract_zh") and (
            has_unresolved_prose_latex_markup(mapping.get("abstract_zh"))
            or chinese_translation_quality_issue(mapping.get("abstract_zh"), source_abstract_en)
        ):
            mapping.pop("abstract_zh", None)
    return paper


def _load_result_payload(output_path: Path, receipt: dict) -> dict:
    payload = read_json(output_path, {})
    if isinstance(payload, dict) and payload:
        return _strip_reading_content_payload(payload)
    fallback = receipt.get("result_payload") if isinstance(receipt.get("result_payload"), dict) else {}
    return _strip_reading_content_payload(fallback) if isinstance(fallback, dict) else {}


READING_CONTENT_PAYLOAD_KEYS = {
    "reading",
    "abstract_zh",
    "summary",
    "motivation_zh",
    "problem",
    "method_family_zh",
    "method_details_zh",
    "method",
    "experiments_zh",
    "experiments",
    "limitations_zh",
    "limitations",
    "method_advantages_zh",
    "method_disadvantages_zh",
    "evidence_boundary_zh",
}


def _strip_reading_content_payload(payload: dict) -> dict:
    cleaned = dict(payload)
    for key in READING_CONTENT_PAYLOAD_KEYS:
        cleaned.pop(key, None)
    return cleaned


MACHINE_PAPER_KEYS = (
    "id", "paper_id", "title", "authors", "source", "venue", "year", "url",
    "abs_url", "html_url", "pdf_url", "doi", "arxiv_id", "openreview_id",
    "score", "recommendation_score", "fit_score",
)


def _machine_read_result(result: dict) -> dict:
    cleaned = dict(result)
    paper = result.get("paper") if isinstance(result.get("paper"), dict) else {}
    if paper:
        cleaned["paper"] = {
            key: paper[key]
            for key in MACHINE_PAPER_KEYS
            if paper.get(key) not in (None, "", [], {})
        }
    claude_result = result.get("claude_result")
    if isinstance(claude_result, dict):
        cleaned["claude_result"] = _strip_reading_content_payload(claude_result)
    claude = result.get("claude")
    if isinstance(claude, dict) and isinstance(claude.get("result_payload"), dict):
        cleaned_claude = dict(claude)
        cleaned_claude["result_payload"] = _strip_reading_content_payload(claude["result_payload"])
        cleaned["claude"] = cleaned_claude
    return cleaned


def _write_read_result(path: Path, result: dict) -> None:
    write_json(path, _machine_read_result(result))


EXCLUDED_READING_METADATA_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?\s*(?:作者|Author|Authors|通讯作者|单位|机构|所属机构)\s*(?:\*\*)?\s*[:：].*$",
    re.I,
)


def _sanitize_article_markdown_text(text: str) -> str:
    lines = str(text or "").splitlines()
    cleaned: list[str] = []
    for line in lines:
        if EXCLUDED_READING_METADATA_LINE_RE.match(line):
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip() + ("\n" if cleaned else "")


def _normalize_article_markdown_metadata(text: str, paper: dict, packet: dict | None = None) -> str:
    cleaned = _sanitize_article_markdown_text(text)
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    abstract_zh = str(paper.get("abstract_zh") or metadata.get("abstract_zh") or "").strip()
    if (
        abstract_zh
        and not chinese_translation_quality_issue(abstract_zh, _article_source_abstract_en(paper))
        and not has_unresolved_prose_latex_markup(abstract_zh)
    ):
        abstract_lines = cleaned.splitlines()
        abstract_index = next((index for index, line in enumerate(abstract_lines) if line.strip() == "## 摘要"), -1)
        next_section_index = next(
            (index for index in range(abstract_index + 1, len(abstract_lines)) if abstract_lines[index].strip() == "## 动机与核心创新"),
            -1,
        )
        if abstract_index >= 0 and next_section_index > abstract_index:
            abstract_lines[abstract_index + 1:next_section_index] = ["", *abstract_zh.splitlines(), ""]
            cleaned = "\n".join(abstract_lines).strip() + "\n"
    lines = cleaned.splitlines()
    if not lines:
        return cleaned
    first = 0
    while first < len(lines) and not lines[first].strip():
        first += 1
    if first >= len(lines) or not lines[first].startswith("# "):
        return cleaned
    section_start = 0
    for index in range(first + 1, len(lines)):
        if re.match(r"^##\s+", lines[index]):
            section_start = index
            break
    if section_start <= first:
        return cleaned
    metadata_lines = article_metadata_markdown_lines(paper, packet if isinstance(packet, dict) else {})
    expected_title, _expected_abstract, _source_abstract_en = _article_quality_expectations(paper)
    current_title = display_paper_title(lines[first].lstrip("#").strip())
    canonical_title = current_title if (
        current_title
        and expected_title
        and normalized_paper_title(current_title) == normalized_paper_title(expected_title)
    ) else expected_title or current_title or "未命名论文"
    rest = lines[section_start:]
    while rest and not rest[0].strip():
        rest.pop(0)
    output = [f"# {canonical_title}", "", metadata_lines[0], metadata_lines[1], ""]
    output.extend(rest)
    return "\n".join(output).strip() + "\n"


def _deep_read_complete(receipt: dict, result_payload: dict) -> bool:
    audit = result_payload.get("deep_read_audit") if isinstance(result_payload.get("deep_read_audit"), dict) else {}
    expected_audit = receipt.get("expected_output_audit") if isinstance(receipt.get("expected_output_audit"), dict) else {}
    boundary_audit = receipt.get("nonruntime_artifact_audit") if isinstance(receipt.get("nonruntime_artifact_audit"), dict) else {}
    temp_audit = receipt.get("external_temp_artifact_audit") if isinstance(receipt.get("external_temp_artifact_audit"), dict) else {}
    article_md_declared = str(result_payload.get("article_markdown_path") or audit.get("article_markdown_path") or "").strip()
    expected_valid = expected_audit.get("exists") is True and expected_audit.get("valid_json") is True
    no_boundary_problem = int(boundary_audit.get("problem_count") or 0) == 0
    no_temp_problem = not temp_audit or (temp_audit.get("status") == "passed" and int(temp_audit.get("problem_count") or 0) == 0)
    return (
        result_payload.get("subagent_deep_read") is True
        and audit.get("subagent_used") is True
        and bool(article_md_declared)
        and audit.get("article_markdown_written") is True
        and expected_valid
        and no_boundary_problem
        and no_temp_problem
    )


_MARKDOWN_FENCED_CODE_RE = re.compile(r"```[\s\S]*?```|~~~[\s\S]*?~~~")
_MARKDOWN_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_KATEX_VALIDATOR_JS = r"""
const fs = require('fs');
const MarkdownIt = require('markdown-it');
const katex = require('katex');
const texmath = require('markdown-it-texmath');
const source = fs.readFileSync(0, 'utf8');
const renderer = new MarkdownIt({html:false, linkify:true, breaks:false, typographer:false})
  .use(texmath, {
    engine:katex,
    delimiters:['dollars', 'brackets', 'beg_end'],
    katexOptions:{throwOnError:false, strict:'ignore', trust:false, output:'html'},
  });
const formulas = [];
const visit = (tokens) => {
  for (const token of tokens || []) {
    if (String(token.type || '').startsWith('math_')) formulas.push(token);
    if (token.children) visit(token.children);
  }
};
try {
  visit(renderer.parse(source, {}));
  for (const token of formulas) {
    katex.renderToString(String(token.content || ''), {
      throwOnError:true,
      strict:'ignore',
      trust:false,
      output:'html',
      displayMode:token.type !== 'math_inline',
    });
  }
  process.stdout.write(JSON.stringify({ok:true, formula_count:formulas.length}));
} catch (error) {
  process.stdout.write(JSON.stringify({ok:false, formula_count:formulas.length, error:String(error && error.message || error)}));
}
"""


def _markdown_math_fragments(text: str) -> tuple[list[str], str]:
    scrubbed = _MARKDOWN_FENCED_CODE_RE.sub("", str(text or ""))
    scrubbed = _MARKDOWN_INLINE_CODE_RE.sub("", scrubbed)
    fragments: list[str] = []
    outside = list(scrubbed)
    opened = ""
    opened_at = -1
    index = 0
    while index < len(scrubbed):
        if scrubbed[index] == "\\":
            index += 2
            continue
        if scrubbed[index] != "$":
            if opened == "$" and scrubbed[index] == "\n":
                return fragments, "行内公式的 `$...$` 未在同一行闭合"
            index += 1
            continue
        marker = "$$" if scrubbed.startswith("$$", index) else "$"
        if opened:
            if marker != opened:
                return fragments, f"公式定界符不匹配：以 `{opened}` 开始却遇到 `{marker}`"
            fragment = scrubbed[opened_at + len(opened):index]
            if not fragment.strip():
                return fragments, f"存在空的 `{marker}...{marker}` 公式"
            fragments.append(fragment)
            for position in range(opened_at, min(len(outside), index + len(marker))):
                outside[position] = " "
            opened = ""
            opened_at = -1
        else:
            opened = marker
            opened_at = index
        index += len(marker)
    if opened:
        return fragments, f"公式定界符 `{opened}` 未闭合"

    outside_text = "".join(outside)
    bracket_pairs = {"\\(": "\\)", "\\[": "\\]"}
    bracket_opened = ""
    bracket_at = -1
    index = 0
    while index < len(outside_text):
        marker = next((candidate for candidate in ["\\(", "\\)", "\\[", "\\]"] if outside_text.startswith(candidate, index)), "")
        if not marker:
            index += 1
            continue
        if marker in bracket_pairs:
            if bracket_opened:
                return fragments, f"公式定界符嵌套或不匹配：`{bracket_opened}` 后遇到 `{marker}`"
            bracket_opened = marker
            bracket_at = index
        else:
            if not bracket_opened or bracket_pairs[bracket_opened] != marker:
                return fragments, f"公式结束定界符 `{marker}` 没有匹配的开始定界符"
            fragment = outside_text[bracket_at + 2:index]
            if not fragment.strip():
                return fragments, f"存在空的 `{bracket_opened}...{marker}` 公式"
            fragments.append(fragment)
            bracket_opened = ""
            bracket_at = -1
        index += 2
    if bracket_opened:
        return fragments, f"公式定界符 `{bracket_opened}` 未闭合"
    return fragments, ""


def _markdown_katex_syntax_error(text: str) -> str:
    fragments, structural_error = _markdown_math_fragments(text)
    if structural_error:
        return structural_error
    if not fragments:
        return ""
    node = shutil.which("node")
    if not node:
        return ""
    try:
        proc = subprocess.run(
            [node, "-e", _KATEX_VALIDATOR_JS],
            input=str(text or ""),
            text=True,
            capture_output=True,
            timeout=15,
        )
        if proc.returncode != 0:
            return ""
        payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return ""
    if payload.get("ok") is not True:
        return str(payload.get("error") or "KaTeX 解析失败")[:500]
    if int(payload.get("formula_count") or 0) != len(fragments):
        return "网页 Markdown 解析器未识别全部公式定界符"
    return ""


_REQUIRED_ARTICLE_SECTIONS = (
    "摘要",
    "动机与核心创新",
    "方法",
    "实验结果",
    "优缺点总结",
)


def _article_source_abstract_en(paper: dict | None) -> str:
    paper = paper if isinstance(paper, dict) else {}
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    return str(
        paper.get("abstract_en")
        or paper.get("abstract")
        or metadata.get("abstract_en")
        or metadata.get("abstract")
        or ""
    ).strip()


def _article_quality_expectations(paper: dict | None) -> tuple[str, str, str]:
    paper = paper if isinstance(paper, dict) else {}
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    title = display_paper_title(metadata.get("reading_verified_full_text_title") or paper.get("title"))
    source_abstract_en = _article_source_abstract_en(paper)
    abstract_zh = str(paper.get("abstract_zh") or metadata.get("abstract_zh") or "").strip()
    if abstract_zh and (
        chinese_translation_quality_issue(abstract_zh, source_abstract_en)
        or has_unresolved_prose_latex_markup(abstract_zh)
    ):
        abstract_zh = ""
    return title, abstract_zh, source_abstract_en


def _article_section_bodies(text: str) -> tuple[list[str], dict[str, str]]:
    matches = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", str(text or "")))
    headings: list[str] = []
    bodies: dict[str, str] = {}
    for index, match in enumerate(matches):
        heading = match.group(1).strip()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        headings.append(heading)
        bodies.setdefault(heading, text[match.end():end].strip())
    return headings, bodies


def _normalized_article_title(value: str) -> str:
    return normalized_paper_title(value)


@functools.lru_cache(maxsize=1024)
def _article_markdown_quality_findings(
    text: str,
    expected_title: str = "",
    expected_abstract_zh: str = "",
    source_abstract_en: str = "",
) -> tuple[tuple[str, str], ...]:
    findings: list[tuple[str, str]] = []
    if has_unresolved_prose_latex_markup(text):
        findings.append(("unresolved_prose_latex_markup", "公式外仍残留 LaTeX 排版命令"))
    katex_error = _markdown_katex_syntax_error(text)
    if katex_error:
        findings.append(("invalid_katex_syntax", f"网页 KaTeX 语法校验失败：{katex_error}"))
    abstract = re.search(r"(?ms)^##\s+摘要\s*$\s*(.*?)(?=^##\s+|\Z)", text)
    abstract_quality_issue = ""
    if not abstract:
        findings.append(("missing_abstract_section", "缺少 `## 摘要` 栏目"))
    else:
        abstract_quality_issue = chinese_translation_quality_issue(abstract.group(1), source_abstract_en)
    if abstract and abstract_quality_issue == "missing_substantive_chinese":
        findings.append(("abstract_missing_chinese", "`## 摘要` 未包含合格的中文摘要"))
    elif abstract and abstract_quality_issue in {"copied_english_source", "long_english_prose"}:
        findings.append((
            "abstract_contains_english_original",
            "`## 摘要` 含英文原文句群；摘要只能保留完整中文翻译，专有名词、模型名、数据集名和缩写除外",
        ))
    elif abstract and expected_abstract_zh and abstract.group(1).strip() != expected_abstract_zh.strip():
        findings.append(("fixed_abstract_modified", "`## 摘要` 未逐字保留 Framework 提供的固定中文摘要"))

    h1_matches = re.findall(r"(?m)^#(?!#)\s+(.+?)\s*$", text)
    actual_title = h1_matches[0].strip() if len(h1_matches) == 1 else ""
    if len(h1_matches) != 1:
        findings.append(("article_title_mismatch", "单篇产物必须且只能有一个一级论文标题"))
    elif is_placeholder_paper_title(actual_title):
        findings.append(("article_title_placeholder", "一级标题仍是占位文字，必须恢复为已核验的论文标题"))
    elif expected_title and _normalized_article_title(actual_title) != _normalized_article_title(expected_title):
        findings.append(("article_title_mismatch", f"一级标题与已核验论文标题 `{expected_title}` 不一致"))

    headings, section_bodies = _article_section_bodies(text)
    if headings != list(_REQUIRED_ARTICLE_SECTIONS):
        findings.append((
            "invalid_section_structure",
            "二级栏目必须按固定顺序且各出现一次：" + "、".join(_REQUIRED_ARTICLE_SECTIONS),
        ))
    else:
        non_chinese_sections = [
            heading for heading in _REQUIRED_ARTICLE_SECTIONS
            if not has_substantive_chinese(section_bodies.get(heading, ""))
        ]
        if non_chinese_sections:
            findings.append((
                "section_missing_chinese",
                "以下栏目不是合格的中文论述：" + "、".join(non_chinese_sections),
            ))
    return tuple(findings)


def _article_markdown_quality_issue(text: str, paper: dict | None = None) -> str:
    expected_title, expected_abstract_zh, source_abstract_en = _article_quality_expectations(paper)
    findings = _article_markdown_quality_findings(
        str(text or ""), expected_title, expected_abstract_zh, source_abstract_en
    )
    return findings[0][0] if findings else ""


def _article_markdown_quality_reason(text: str, paper: dict | None = None) -> str:
    expected_title, expected_abstract_zh, source_abstract_en = _article_quality_expectations(paper)
    findings = _article_markdown_quality_findings(
        str(text or ""), expected_title, expected_abstract_zh, source_abstract_en
    )
    return "；".join(reason for _issue, reason in findings)


def _article_markdown_ready(path: Path, result_payload: dict, paper: dict | None = None) -> bool:
    audit = result_payload.get("deep_read_audit") if isinstance(result_payload.get("deep_read_audit"), dict) else {}
    declared = str(result_payload.get("article_markdown_path") or audit.get("article_markdown_path") or "").strip()
    if not declared:
        return False
    declared_ok = Path(declared).name == path.name or declared == path.name or declared.endswith("/" + path.name)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return (
        declared_ok
        and bool(text.strip())
        and text.lstrip().startswith("#")
        and not _article_markdown_quality_issue(text, paper)
    )


def _write_single_read_md_if_needed(path: Path, result: dict, *, complete: bool) -> None:
    # The subagent owns scientific content; Python only normalizes fixed metadata.
    if not complete:
        return
    if not path.exists() or not path.read_text(encoding="utf-8", errors="replace").strip():
        raise RuntimeError(f"subagent completed without writing article Markdown: {path}")
    original = path.read_text(encoding="utf-8", errors="replace")
    paper = result.get("paper") if isinstance(result.get("paper"), dict) else {}
    packet = result.get("full_text_packet") if isinstance(result.get("full_text_packet"), dict) else {}
    cleaned = _normalize_article_markdown_metadata(original, paper, packet)
    if cleaned != original:
        path.write_text(cleaned, encoding="utf-8")


def _reuse_existing_deep_read_enabled() -> bool:
    return env_bool("READING_REUSE_EXISTING_DEEP_READ_RESULTS", True)


def _article_cache_enabled() -> bool:
    if os.environ.get("PYTEST_CURRENT_TEST") and not env_bool("READING_ENABLE_ARTICLE_CACHE_DURING_TESTS", False):
        return False
    return (
        not env_bool("READING_DISABLE_RUNTIME_CACHE", False)
        and not env_bool("READING_DISABLE_ARTICLE_CACHE", False)
    )


def _article_full_text_cache_enabled() -> bool:
    return (
        _article_cache_enabled()
        and not env_bool("READING_DISABLE_RUNTIME_FULL_TEXT_CACHE", False)
    )


def _ensure_article_cache_dirs() -> tuple[Path, Path]:
    root = ensure_inside_reading(ARTICLE_CACHE_ROOT, label="文章缓存目录")
    articles = ensure_inside_reading(ARTICLE_CACHE_ARTICLES_ROOT, label="文章缓存目录")
    aliases = ensure_inside_reading(ARTICLE_CACHE_ALIASES_ROOT, label="文章缓存索引目录")
    root.mkdir(parents=True, exist_ok=True)
    articles.mkdir(parents=True, exist_ok=True)
    aliases.mkdir(parents=True, exist_ok=True)
    return articles, aliases


def _article_cache_hash(value: str) -> str:
    return hashlib.sha256(str(value or "").strip().lower().encode("utf-8")).hexdigest()[:32]


def _article_cache_alias_path(alias: str) -> Path:
    _, aliases = _ensure_article_cache_dirs()
    digest = _article_cache_hash(alias)
    return aliases / digest[:2] / f"{digest}.json"


def _article_cache_dir_for_alias(alias: str) -> Path:
    articles, _ = _ensure_article_cache_dirs()
    return articles / _article_cache_hash(alias)


def _arxiv_identity_from_text(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    patterns = [
        r"arxiv\.org/(?:abs|pdf)/([A-Za-z-]+/\d{7}(?:v\d+)?|\d{4}\.\d{4,5}(?:v\d+)?)",
        r"\barxiv\s*[:/]\s*([A-Za-z-]+/\d{7}(?:v\d+)?|\d{4}\.\d{4,5}(?:v\d+)?)",
        r"^([A-Za-z-]+/\d{7}(?:v\d+)?|\d{4}\.\d{4,5}(?:v\d+)?)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            return re.sub(r"v\d+$", "", match.group(1).strip(), flags=re.I).lower()
    return ""


def _article_cache_aliases(paper: dict, packet: dict | None = None) -> list[str]:
    packet = packet if isinstance(packet, dict) else {}
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    aliases: list[str] = []

    def add(prefix: str, value: object) -> None:
        text = str(value or "").strip()
        if not text:
            return
        alias = f"{prefix}:{text.lower()}"
        if alias not in aliases:
            aliases.append(alias)

    doi = _doi_from_paper(paper) or _doi_from_text(packet.get("doi"))
    add("doi", doi)

    for value in [
        paper.get("paper_id"),
        paper.get("id"),
        metadata.get("paper_id"),
        metadata.get("id"),
    ]:
        text = str(value or "").strip()
        source = str(paper.get("source") or metadata.get("source") or "").strip().lower()
        if text and source:
            add("paperid", f"{source}:{text}")

    for value in [
        paper.get("arxiv_id"),
        metadata.get("arxiv_id"),
        paper.get("url"),
        paper.get("abs_url"),
        paper.get("pdf_url"),
        packet.get("pdf_url"),
    ]:
        add("arxiv", _arxiv_identity_from_text(value))

    for value in [
        paper.get("openreview_id"),
        paper.get("openreview_note_id"),
        paper.get("openreview_forum_url"),
        paper.get("openreview_url"),
        paper.get("openreview_pdf_url"),
        paper.get("url"),
        paper.get("pdf_url"),
        metadata.get("openreview_id"),
        metadata.get("openreview_note_id"),
        metadata.get("openreview_forum_url"),
        metadata.get("openreview_url"),
        metadata.get("openreview_pdf_url"),
        packet.get("pdf_url"),
    ]:
        text = str(value or "").strip()
        note_id = _openreview_note_id_from_url(text)
        if not note_id and re.fullmatch(r"[A-Za-z0-9_-]{8,}", text):
            note_id = text
        add("openreview", note_id)
        for found in _openreview_note_ids_from_text(text):
            add("openreview", found)

    for key in ["url", "abs_url", "html_url"]:
        add("url", _url_identity_key(paper.get(key) or metadata.get(key) or ""))
    for value in [paper.get("pdf_url"), metadata.get("pdf_url"), packet.get("pdf_url")]:
        add("pdf", _url_identity_key(value))

    title_key = _normalized_title_key(paper.get("title") or packet.get("title"))
    add("title", title_key)
    return aliases


def _article_cache_manifest(cache_dir: Path) -> dict:
    try:
        cache_dir = ensure_inside_reading(cache_dir, label="文章缓存目录")
    except Exception:
        return {}
    return read_json(cache_dir / "manifest.json", {})


def _article_cache_same_paper_ok(cache_dir: Path, paper: dict) -> bool:
    manifest = _article_cache_manifest(cache_dir)
    cached_paper = manifest.get("paper") if isinstance(manifest.get("paper"), dict) else {}
    if not cached_paper:
        cached_paper = read_json(cache_dir / "paper.json", {})
    if not isinstance(cached_paper, dict) or not cached_paper:
        return True
    wanted_aliases = set(_article_cache_aliases(paper))
    cached_aliases = {
        str(alias)
        for alias in (manifest.get("aliases") if isinstance(manifest.get("aliases"), list) else [])
        if str(alias).strip()
    }
    if not cached_aliases:
        cached_aliases = set(_article_cache_aliases(cached_paper))
    exact_prefixes = ("doi:", "arxiv:", "openreview:", "url:", "pdf:", "paperid:")
    if any(alias in cached_aliases for alias in wanted_aliases if alias.startswith(exact_prefixes)):
        return True
    wanted_title = next((alias for alias in wanted_aliases if alias.startswith("title:")), "")
    cached_title = next((alias for alias in cached_aliases if alias.startswith("title:")), "")
    if wanted_title and wanted_title == cached_title:
        return True
    return _same_paper_identity_ok(
        paper,
        candidate_title=cached_paper.get("title") or "",
        candidate_authors=cached_paper.get("authors"),
        candidate_doi=cached_paper.get("doi") or cached_paper.get("published_doi") or "",
    )


def _locate_article_cache_dir(paper: dict, packet: dict | None = None) -> Path | None:
    if not _article_cache_enabled():
        return None
    aliases = _article_cache_aliases(paper, packet)
    if not aliases:
        return None
    articles_root, alias_root = _ensure_article_cache_dirs()
    candidates: list[Path] = []
    seen: set[str] = set()
    for alias in aliases:
        alias_path = alias_root / _article_cache_hash(alias)[:2] / f"{_article_cache_hash(alias)}.json"
        payload = read_json(alias_path, {})
        cache_dir_value = str(payload.get("cache_dir") or "").strip() if isinstance(payload, dict) else ""
        if cache_dir_value:
            try:
                cache_dir = resolve_reading_path(cache_dir_value)
            except Exception:
                cache_dir = Path(cache_dir_value)
            key = str(cache_dir.resolve(strict=False))
            if key not in seen:
                seen.add(key)
                candidates.append(cache_dir)
        direct = _article_cache_dir_for_alias(alias)
        key = str(direct.resolve(strict=False))
        if key not in seen:
            seen.add(key)
            candidates.append(direct)
    for cache_dir in candidates:
        try:
            cache_dir = ensure_inside_reading(cache_dir, label="文章缓存目录")
        except Exception:
            continue
        if cache_dir.is_dir() and _article_cache_same_paper_ok(cache_dir, paper):
            return cache_dir
    for manifest_path in articles_root.glob("*/manifest.json"):
        cache_dir = manifest_path.parent
        key = str(cache_dir.resolve(strict=False))
        if key in seen:
            continue
        if _article_cache_same_paper_ok(cache_dir, paper):
            return cache_dir
    return None


def _target_article_cache_dir(paper: dict, packet: dict | None = None) -> tuple[Path, list[str]]:
    aliases = _article_cache_aliases(paper, packet)
    if not aliases:
        fallback = "title:" + (_normalized_title_key(paper.get("title")) or safe_slug(paper.get("paper_id") or "paper"))
        aliases = [fallback]
    existing = _locate_article_cache_dir(paper, packet)
    cache_dir = existing if existing is not None else _article_cache_dir_for_alias(aliases[0])
    return ensure_inside_reading(cache_dir, label="文章缓存目录"), aliases


def _write_article_cache_aliases(cache_dir: Path, aliases: list[str]) -> None:
    for alias in aliases:
        if not alias:
            continue
        write_json(_article_cache_alias_path(alias), {
            "alias": alias,
            "cache_dir": _rel_reading_path(cache_dir),
            "updated_at": _now_iso(),
        })


def _copy_article_cache_file(source: Path, target: Path) -> bool:
    try:
        source = ensure_inside_reading(source, label="文章缓存源文件")
        target = ensure_inside_reading(target, label="文章缓存目标文件")
    except Exception:
        return False
    if not source.is_file():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(source, target)
        return target.is_file() and target.stat().st_size > 0
    except Exception:
        return False


def _file_sha256(path: Path | None) -> str:
    if path is None or not path.is_file():
        return ""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def _content_fingerprints(*, text_path: Path | None, pdf_path: Path | None) -> dict[str, str]:
    full_text_sha256 = _file_sha256(text_path)
    pdf_sha256 = _file_sha256(pdf_path)
    if not full_text_sha256 and not pdf_sha256:
        return {}
    revision_source = f"full_text_sha256={full_text_sha256}\npdf_sha256={pdf_sha256}\n"
    return {
        "full_text_sha256": full_text_sha256,
        "pdf_sha256": pdf_sha256,
        "content_revision": hashlib.sha256(revision_source.encode("ascii")).hexdigest(),
    }


def _article_cache_content_fingerprints(cache_dir: Path) -> dict[str, str]:
    return _content_fingerprints(
        text_path=cache_dir / "extracted" / "full_text.txt",
        pdf_path=cache_dir / "downloads" / "article.pdf",
    )


def _invalidate_article_read_artifacts(cache_dir: Path) -> bool:
    removed = False
    for relative_path in ["read.md", "read_results.json", "outputs", "prompts", "claude"]:
        path = cache_dir / relative_path
        try:
            if path.is_dir():
                shutil.rmtree(path)
                removed = True
            elif path.exists():
                path.unlink()
                removed = True
        except OSError:
            continue
    return removed


def _packet_acquisition_source_kind(packet: dict) -> str:
    for section_key in ["pdf_acquisition", "html_acquisition", "pmc_xml_acquisition"]:
        section = packet.get(section_key) if isinstance(packet.get(section_key), dict) else {}
        selected = section.get("selected") if isinstance(section.get("selected"), dict) else {}
        kind = str(selected.get("kind") or "").strip()
        if kind:
            return kind
    return str(packet.get("full_text_evidence_kind") or packet.get("text_kind") or "").strip()


def _packet_from_full_text_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {}
    packets = payload.get("papers") if isinstance(payload.get("papers"), list) else []
    if packets and isinstance(packets[0], dict):
        return dict(packets[0])
    for key in ["packet", "full_text_packet"]:
        value = payload.get(key)
        if isinstance(value, dict):
            return dict(value)
    if "full_text_available" in payload:
        return dict(payload)
    return {}


def _packet_path(packet: dict, key: str) -> Path | None:
    value = str(packet.get(key) or "").strip()
    if not value:
        return None
    try:
        return resolve_reading_path(value)
    except Exception:
        return None


def _publish_article_full_text_cache(item_dir: Path, paper: dict, packet: dict) -> dict:
    if not _article_full_text_cache_enabled() or not isinstance(packet, dict) or not packet:
        return {}
    if packet.get("article_cache_hit") is True:
        cache_value = str(packet.get("article_cache_dir") or "").strip()
        try:
            cache_dir = ensure_inside_reading(resolve_reading_path(cache_value), label="文章缓存目录")
        except Exception:
            cache_dir = None
        if cache_dir is not None and cache_dir.is_dir() and _article_cache_same_paper_ok(cache_dir, paper):
            fingerprints = _article_cache_content_fingerprints(cache_dir)
            packet.update(fingerprints)
            with _ARTICLE_CACHE_LOCK:
                manifest = _article_cache_manifest(cache_dir)
                manifest.update({
                    "updated_at": _now_iso(),
                    "cache_scope": "reading_article_cache",
                    "has_full_text": (cache_dir / "extracted" / "full_text.txt").is_file(),
                    "has_pdf": (cache_dir / "downloads" / "article.pdf").is_file(),
                    "has_read_md": (cache_dir / "read.md").is_file(),
                    "full_text_content_revision": fingerprints.get("content_revision", ""),
                    "full_text_sha256": fingerprints.get("full_text_sha256", ""),
                    "pdf_sha256": fingerprints.get("pdf_sha256", ""),
                    "full_text_source_kind": _packet_acquisition_source_kind(packet),
                    "full_text_pdf_url": str(packet.get("pdf_url") or ""),
                })
                write_json(cache_dir / "manifest.json", manifest)
            return {
                "cache_dir": _rel_reading_path(cache_dir),
                "copied_text": False,
                "copied_pdf": False,
                "cache_hit": True,
                **fingerprints,
            }
    text_source = _packet_path(packet, "text_path")
    pdf_source = _packet_path(packet, "pdf_path")
    if (text_source is None or not text_source.is_file()) and (pdf_source is None or not pdf_source.is_file()):
        return {}
    cache_dir, aliases = _target_article_cache_dir(paper, packet)
    with _ARTICLE_CACHE_LOCK:
        cache_dir.mkdir(parents=True, exist_ok=True)
        previous_fingerprints = _article_cache_content_fingerprints(cache_dir)
        manifest = _article_cache_manifest(cache_dir)
        had_read_md = (cache_dir / "read.md").is_file()
        cached_packet = dict(packet)
        copied_text = False
        copied_pdf = False
        if text_source is not None and text_source.is_file():
            text_target = cache_dir / "extracted" / "full_text.txt"
            copied_text = _copy_article_cache_file(text_source, text_target)
            if copied_text:
                cached_packet["text_path"] = _rel_reading_path(text_target)
        if pdf_source is not None and pdf_source.is_file():
            suffix = pdf_source.suffix if pdf_source.suffix.lower() == ".pdf" else ".pdf"
            pdf_target = cache_dir / "downloads" / ("article" + suffix)
            copied_pdf = _copy_article_cache_file(pdf_source, pdf_target)
            if copied_pdf:
                cached_packet["pdf_path"] = _rel_reading_path(pdf_target)
        current_fingerprints = _article_cache_content_fingerprints(cache_dir)
        packet.update(current_fingerprints)
        cached_packet.update(current_fingerprints)
        previous_revision = str(previous_fingerprints.get("content_revision") or "")
        current_revision = str(current_fingerprints.get("content_revision") or "")
        bound_read_revision = str(manifest.get("read_content_revision") or "")
        content_changed = bool(current_revision and (previous_revision or had_read_md) and current_revision != previous_revision)
        read_binding_mismatch = bool(bound_read_revision and current_revision and bound_read_revision != current_revision)
        read_cache_invalidated = False
        if had_read_md and (content_changed or read_binding_mismatch):
            read_cache_invalidated = _invalidate_article_read_artifacts(cache_dir)
        cached_packet["article_cache_dir"] = _rel_reading_path(cache_dir)
        cached_packet["article_cache_published_at"] = _now_iso()
        write_json(cache_dir / "paper.json", paper)
        write_json(cache_dir / "full_text_packet.json", {
            "paper": paper,
            "papers": [cached_packet],
            "generated_at": _now_iso(),
            "cache_scope": "reading_article_cache",
        })
        manifest.update({
            "paper": paper,
            "aliases": aliases,
            "updated_at": _now_iso(),
            "cache_scope": "reading_article_cache",
            "has_full_text": copied_text or bool(manifest.get("has_full_text")),
            "has_pdf": copied_pdf or bool(manifest.get("has_pdf")),
            "has_read_md": (cache_dir / "read.md").is_file(),
            "full_text_content_revision": current_revision,
            "full_text_sha256": current_fingerprints.get("full_text_sha256", ""),
            "pdf_sha256": current_fingerprints.get("pdf_sha256", ""),
            "full_text_source_kind": _packet_acquisition_source_kind(packet),
            "full_text_pdf_url": str(packet.get("pdf_url") or ""),
        })
        if read_cache_invalidated:
            manifest.update({
                "read_content_revision": "",
                "read_invalidated_at": _now_iso(),
                "read_invalidation_reason": "full_text_content_replaced",
            })
        write_json(cache_dir / "manifest.json", manifest)
        _write_article_cache_aliases(cache_dir, aliases)
    return {
        "cache_dir": _rel_reading_path(cache_dir),
        "copied_text": copied_text,
        "copied_pdf": copied_pdf,
        "content_changed": content_changed,
        "read_cache_invalidated": read_cache_invalidated,
        **current_fingerprints,
    }


def _restore_article_full_text_cache(paper: dict, item_dir: Path) -> dict:
    if not _article_full_text_cache_enabled():
        return {}
    cache_dir = _locate_article_cache_dir(paper)
    if cache_dir is None:
        return {}
    payload = read_json(cache_dir / "full_text_packet.json", {})
    packet = _packet_from_full_text_payload(payload)
    if not packet:
        return {}
    text_source = _packet_path(packet, "text_path")
    pdf_source = _packet_path(packet, "pdf_path")
    restored = dict(packet)
    copied_text = False
    copied_pdf = False
    if text_source is not None and text_source.is_file():
        text_target = item_dir / "extracted" / "full_text.txt"
        copied_text = _copy_article_cache_file(text_source, text_target)
        if copied_text:
            restored["text_path"] = _rel_reading_path(text_target)
    if pdf_source is not None and pdf_source.is_file():
        suffix = pdf_source.suffix if pdf_source.suffix.lower() == ".pdf" else ".pdf"
        pdf_target = item_dir / "downloads" / ("article" + suffix)
        copied_pdf = _copy_article_cache_file(pdf_source, pdf_target)
        if copied_pdf:
            restored["pdf_path"] = _rel_reading_path(pdf_target)
    if not copied_text and not copied_pdf:
        return {}
    verified_title = str(restored.get("verified_full_text_title") or "").strip()
    if not verified_title and copied_text:
        try:
            verified_title = _best_full_text_title(paper, text_target.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            verified_title = ""
    if verified_title:
        restored["verified_full_text_title"] = _apply_verified_full_text_title(paper, verified_title)
        restored["title"] = paper.get("title") or restored.get("title") or ""
    restored.update(_article_cache_content_fingerprints(cache_dir))
    restored["article_cache_hit"] = True
    restored["article_cache_dir"] = _rel_reading_path(cache_dir)
    restored["reused_article_full_text_cache"] = True
    return restored


def _copy_article_support_file(cache_dir: Path, item_dir: Path, relative_path: str) -> bool:
    return _copy_article_cache_file(cache_dir / relative_path, item_dir / relative_path)


def _publish_article_read_cache(item_dir: Path, paper: dict, result: dict) -> dict:
    if not _article_cache_enabled() or not isinstance(result, dict):
        return {}
    article_md_path = item_dir / "read.md"
    if not article_md_path.is_file() or not article_md_path.read_text(encoding="utf-8", errors="replace").strip():
        return {}
    packet = result.get("full_text_packet") if isinstance(result.get("full_text_packet"), dict) else {}
    _publish_article_full_text_cache(item_dir, paper, packet)
    cache_dir, aliases = _target_article_cache_dir(paper, packet)
    with _ARTICLE_CACHE_LOCK:
        cache_dir.mkdir(parents=True, exist_ok=True)
        content_fingerprints = _article_cache_content_fingerprints(cache_dir)
        packet.update(content_fingerprints)
        cleaned = _normalize_article_markdown_metadata(article_md_path.read_text(encoding="utf-8", errors="replace"), paper, packet)
        if _article_markdown_quality_issue(cleaned, paper):
            return {}
        if cleaned != article_md_path.read_text(encoding="utf-8", errors="replace"):
            article_md_path.write_text(cleaned, encoding="utf-8")
        (cache_dir / "read.md").write_text(cleaned, encoding="utf-8")
        for relative_path in [
            "read_results.json",
            "outputs/reading_result.json",
            "prompts/deep_read_prompt.md",
            "claude/claude_receipt.json",
            "paper.json",
        ]:
            _copy_article_support_file(item_dir, cache_dir, relative_path)
        manifest = _article_cache_manifest(cache_dir)
        manifest.update({
            "paper": paper,
            "aliases": aliases,
            "updated_at": _now_iso(),
            "cache_scope": "reading_article_cache",
            "has_read_md": True,
            "has_full_text": bool(manifest.get("has_full_text")) or (cache_dir / "extracted" / "full_text.txt").is_file(),
            "has_pdf": bool(manifest.get("has_pdf")) or any((cache_dir / "downloads").glob("*.pdf")),
            "full_text_content_revision": content_fingerprints.get("content_revision", ""),
            "full_text_sha256": content_fingerprints.get("full_text_sha256", ""),
            "pdf_sha256": content_fingerprints.get("pdf_sha256", ""),
            "read_content_revision": content_fingerprints.get("content_revision", ""),
            "read_quality_policy_version": READING_CONTENT_QUALITY_POLICY_VERSION,
            "read_invalidated_at": "",
            "read_invalidation_reason": "",
        })
        write_json(cache_dir / "manifest.json", manifest)
        _write_article_cache_aliases(cache_dir, aliases)
    return {"cache_dir": _rel_reading_path(cache_dir), "read_md": _rel_reading_path(cache_dir / "read.md")}


def _cached_read_result_skeleton(
    *,
    item_dir: Path,
    paper: dict,
    packet: dict,
    run_id: str,
    paper_index: int,
    cache_dir: Path,
) -> dict:
    full_text_ready = bool(packet.get("full_text_available")) and int(packet.get("full_text_chars") or packet.get("text_chars") or 0) >= FULL_TEXT_MIN_CHARS
    reading = _reading_machine_state(
        paper,
        packet,
        full_text_ready=full_text_ready,
        deep_read_complete=True,
        note_zh="已复用 Reading 文章缓存中的单篇 read.md。",
    )
    _sanitize_reading_public_fields(reading)
    article_md = item_dir / "read.md"
    return {
        "run_id": run_id,
        "paper_index": paper_index,
        "status": "complete",
        "generated_at": _now_iso(),
        "paper": paper,
        "full_text_packet": packet,
        "claude": {"status": "reused_article_read_cache", "run_executed": False},
        "claude_result": {
            "status": "reused_article_read_cache",
            "source": "reading_article_cache",
            "subagent_deep_read": True,
            "article_markdown_path": str(article_md),
            "deep_read_audit": {
                "subagent_used": True,
                "article_markdown_written": True,
                "article_markdown_path": str(article_md),
                "source": "reading_article_cache",
            },
        },
        "reading": reading,
        "validation": {
            "full_text_ready": full_text_ready,
            "deep_read_complete": True,
            "replacement_policy": "forbidden",
            "same_paper_repair_policy": "allowed_pdf_html_xml_for_same_input_paper_only",
            "phase": "reused_article_read_cache",
            "full_text_content_revision": str(packet.get("content_revision") or ""),
            "read_content_revision": str(packet.get("content_revision") or ""),
        },
        "artifacts": {
            "paper": str(item_dir / "paper.json"),
            "full_text_packet": str(item_dir / "full_text_packet.json"),
            "prompt": str(item_dir / "prompts" / "deep_read_prompt.md"),
            "read_md": str(article_md),
            "article_markdown": str(article_md),
            "read_results": str(item_dir / "read_results.json"),
        },
        "article_cache": {
            "hit": True,
            "cache_dir": _rel_reading_path(cache_dir),
        },
    }


def _merge_cached_paper_hints(paper: dict, cached_paper: dict) -> dict:
    if not isinstance(cached_paper, dict) or not cached_paper:
        return dict(paper)
    input_paper = dict(paper)
    merged = {**cached_paper, **input_paper}
    for key in ["authors", "abstract", "url", "abs_url", "html_url", "pdf_url", "doi", "venue", "year"]:
        if input_paper.get(key) in (None, "", []):
            merged[key] = cached_paper.get(key)
    if str(input_paper.get("source") or "").strip().lower() in {"", "input", "local_input", "standalone_input"}:
        merged["source"] = cached_paper.get("source") or input_paper.get("source") or "input"
    cached_metadata = cached_paper.get("metadata") if isinstance(cached_paper.get("metadata"), dict) else {}
    input_metadata = input_paper.get("metadata") if isinstance(input_paper.get("metadata"), dict) else {}
    if cached_metadata or input_metadata:
        merged["metadata"] = {**cached_metadata, **input_metadata}
    return merged


def _restore_article_read_cache(item_dir: Path, paper: dict, *, run_id: str, paper_index: int) -> dict:
    if not _article_cache_enabled():
        return {}
    cache_dir = _locate_article_cache_dir(paper)
    if cache_dir is None or not (cache_dir / "read.md").is_file():
        return {}
    if not _article_cache_same_paper_ok(cache_dir, paper):
        return {}
    manifest = _article_cache_manifest(cache_dir)
    paper = _merge_cached_paper_hints(paper, read_json(cache_dir / "paper.json", {}))
    cached_read_text = (cache_dir / "read.md").read_text(encoding="utf-8", errors="replace")
    initial_quality_issue = _article_markdown_quality_issue(cached_read_text, paper)
    if initial_quality_issue and initial_quality_issue != "article_title_mismatch":
        with _ARTICLE_CACHE_LOCK:
            _invalidate_article_read_artifacts(cache_dir)
            manifest = _article_cache_manifest(cache_dir)
            manifest.update({
                "updated_at": _now_iso(),
                "has_read_md": False,
                "read_content_revision": "",
                "read_quality_policy_version": READING_CONTENT_QUALITY_POLICY_VERSION,
                "read_invalidated_at": _now_iso(),
                "read_invalidation_reason": "read_content_quality:" + initial_quality_issue,
            })
            write_json(cache_dir / "manifest.json", manifest)
        return {}
    content_fingerprints = _article_cache_content_fingerprints(cache_dir)
    current_revision = str(content_fingerprints.get("content_revision") or "")
    read_revision = str(manifest.get("read_content_revision") or "")
    if read_revision and (not current_revision or read_revision != current_revision):
        with _ARTICLE_CACHE_LOCK:
            _invalidate_article_read_artifacts(cache_dir)
            manifest = _article_cache_manifest(cache_dir)
            manifest.update({
                "updated_at": _now_iso(),
                "has_read_md": False,
                "read_content_revision": "",
                "read_invalidated_at": _now_iso(),
                "read_invalidation_reason": "read_full_text_fingerprint_mismatch",
            })
            write_json(cache_dir / "manifest.json", manifest)
        return {}
    restored_packet = _restore_article_full_text_cache(paper, item_dir)
    restored_text_path = _packet_path(restored_packet, "text_path") if restored_packet else None
    if restored_text_path is None or not restored_text_path.is_file() or restored_text_path.stat().st_size < FULL_TEXT_MIN_CHARS:
        return {}
    quality_issue = _article_markdown_quality_issue(cached_read_text, paper)
    if quality_issue:
        with _ARTICLE_CACHE_LOCK:
            _invalidate_article_read_artifacts(cache_dir)
            manifest = _article_cache_manifest(cache_dir)
            manifest.update({
                "updated_at": _now_iso(),
                "has_read_md": False,
                "read_content_revision": "",
                "read_quality_policy_version": READING_CONTENT_QUALITY_POLICY_VERSION,
                "read_invalidated_at": _now_iso(),
                "read_invalidation_reason": "read_content_quality:" + quality_issue,
            })
            write_json(cache_dir / "manifest.json", manifest)
        return {}
    item_dir.mkdir(parents=True, exist_ok=True)
    write_json(item_dir / "paper.json", paper)
    if not _copy_article_cache_file(cache_dir / "read.md", item_dir / "read.md"):
        return {}
    write_json(item_dir / "full_text_packet.json", {
        "run_id": run_id,
        "paper_index": paper_index,
        "papers": [restored_packet],
        "generated_at": _now_iso(),
    })
    article_text = (item_dir / "read.md").read_text(encoding="utf-8", errors="replace")
    normalized_article_text = _normalize_article_markdown_metadata(article_text, paper, restored_packet)
    if normalized_article_text != article_text:
        (item_dir / "read.md").write_text(normalized_article_text, encoding="utf-8")
    cached_article_text = (cache_dir / "read.md").read_text(encoding="utf-8", errors="replace")
    manifest_updates = {
        "cache_scope": "reading_article_cache",
        "has_read_md": True,
        "has_full_text": (cache_dir / "extracted" / "full_text.txt").is_file(),
        "has_pdf": (cache_dir / "downloads" / "article.pdf").is_file(),
        "full_text_content_revision": current_revision,
        "full_text_sha256": str(content_fingerprints.get("full_text_sha256") or ""),
        "pdf_sha256": str(content_fingerprints.get("pdf_sha256") or ""),
        "read_content_revision": current_revision,
        "read_quality_policy_version": READING_CONTENT_QUALITY_POLICY_VERSION,
        "full_text_source_kind": _packet_acquisition_source_kind(restored_packet) or str(manifest.get("full_text_source_kind") or ""),
        "full_text_pdf_url": str(restored_packet.get("pdf_url") or manifest.get("full_text_pdf_url") or ""),
        "read_invalidated_at": "",
        "read_invalidation_reason": "",
    }
    manifest_needs_update = any(manifest.get(key) != value for key, value in manifest_updates.items())
    if normalized_article_text != cached_article_text or manifest_needs_update:
        with _ARTICLE_CACHE_LOCK:
            if normalized_article_text != cached_article_text:
                (cache_dir / "read.md").write_text(normalized_article_text, encoding="utf-8")
            manifest = _article_cache_manifest(cache_dir)
            manifest.update(manifest_updates)
            manifest["updated_at"] = _now_iso()
            write_json(cache_dir / "manifest.json", manifest)
    for relative_path in [
        "outputs/reading_result.json",
        "prompts/deep_read_prompt.md",
    ]:
        _copy_article_support_file(cache_dir, item_dir, relative_path)
    result = _cached_read_result_skeleton(
        item_dir=item_dir,
        paper=paper,
        packet=restored_packet or {},
        run_id=run_id,
        paper_index=paper_index,
        cache_dir=cache_dir,
    )
    write_json(item_dir / "claude" / "claude_receipt.json", result["claude"])
    _write_read_result(item_dir / "read_results.json", result)
    return result


def _existing_full_text_packet_for_deep_read(item_dir: Path, paper: dict, *, allow_existing: bool = False) -> dict:
    if not allow_existing and not env_bool("READING_REUSE_EXISTING_FULL_TEXT_PACKET", True):
        return {}
    payload = read_json(item_dir / "full_text_packet.json", {})
    packets = payload.get("papers") if isinstance(payload, dict) and isinstance(payload.get("papers"), list) else []
    packet = packets[0] if packets and isinstance(packets[0], dict) else {}
    if not packet:
        return {}
    try:
        chars = int(packet.get("full_text_chars") or packet.get("text_chars") or 0)
    except Exception:
        chars = 0
    text_path_value = str(packet.get("text_path") or "").strip()
    if packet.get("full_text_available") is not True or chars < FULL_TEXT_MIN_CHARS or not text_path_value:
        return {}
    try:
        text_path = resolve_reading_path(text_path_value)
    except Exception:
        text_path = Path(text_path_value)
    if not text_path.exists() or text_path.stat().st_size < FULL_TEXT_MIN_CHARS:
        return {}
    if not _same_paper_identity_ok(
        paper,
        candidate_title=packet.get("title") or paper.get("title"),
        candidate_authors=packet.get("authors") or paper.get("authors"),
        candidate_doi=packet.get("doi") or paper.get("doi"),
    ):
        return {}
    reused = dict(packet)
    verified_title = str(reused.get("verified_full_text_title") or "").strip()
    if not verified_title:
        try:
            verified_title = _best_full_text_title(paper, text_path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            verified_title = ""
    if verified_title:
        reused["verified_full_text_title"] = _apply_verified_full_text_title(paper, verified_title)
        reused["title"] = paper.get("title") or reused.get("title") or ""
    reused["reused_existing_full_text_packet_for_deep_read"] = True
    reused["reuse_policy"] = "Only reused because the same Reading run already has verified same-paper full-text evidence and this pass is rerunning Claude/subagent deep-read synthesis."
    return reused


def _deep_read_cache_keys_for_paper(paper: dict) -> list[str]:
    keys: list[str] = []
    doi = _doi_from_paper(paper).lower()
    title_key = _normalized_title_key(paper.get("title"))
    for key in [
        "doi:" + doi if doi else "",
        "title:" + title_key if title_key else "",
        "url:" + _url_identity_key(paper.get("url") or paper.get("abs_url") or ""),
        "pdf:" + _url_identity_key(paper.get("pdf_url") or ""),
    ]:
        if key and key not in keys:
            keys.append(key)
    return keys


def _deep_read_cache_index() -> dict[str, list[Path]]:
    global _DEEP_READ_DIR_CACHE_INDEX
    if _DEEP_READ_DIR_CACHE_INDEX is not None:
        return _DEEP_READ_DIR_CACHE_INDEX
    cache: dict[str, list[Path]] = {}
    roots = [*CACHE_BATCH_TEST_ROOTS, *CACHE_RUN_ROOTS]
    for root in roots:
        if not root.exists():
            continue
        for result_path in root.glob("**/read_results.json"):
            try:
                result_path = ensure_inside_output(result_path, label="deep-read cache result")
            except Exception:
                continue
            result = read_json(result_path, {})
            if not isinstance(result, dict) or not result:
                continue
            validation = result.get("validation") if isinstance(result.get("validation"), dict) else {}
            if validation.get("deep_read_complete") is not True:
                continue
            cached_paper = result.get("paper") if isinstance(result.get("paper"), dict) else {}
            if not cached_paper:
                continue
            artifacts = result.get("artifacts") if isinstance(result.get("artifacts"), dict) else {}
            article_md_value = str(artifacts.get("article_markdown") or artifacts.get("read_md") or "").strip()
            if not article_md_value:
                continue
            try:
                article_md_path = ensure_inside_output(resolve_reading_path(article_md_value), label="deep-read cache article Markdown")
            except Exception:
                continue
            if not article_md_path.is_file():
                continue
            article_text = article_md_path.read_text(encoding="utf-8", errors="replace")
            if not article_text.strip():
                continue
            item_dir = result_path.parent
            for key in _deep_read_cache_keys_for_paper(cached_paper):
                cache.setdefault(key, []).append(item_dir)
    _DEEP_READ_DIR_CACHE_INDEX = cache
    return cache


def _copy_reused_deep_read_dir(source_dir: Path, target_dir: Path) -> bool:
    try:
        source = ensure_inside_output(source_dir.resolve(strict=False), label="复用单篇 run 目录")
        target = ensure_inside_output(target_dir.resolve(strict=False), label="当前单篇 run 目录")
    except Exception:
        return False
    if source == target:
        return True
    if not (source / "read_results.json").is_file():
        return False
    tmp_target = target.with_name(target.name + ".reuse_tmp")
    try:
        if tmp_target.exists():
            shutil.rmtree(tmp_target)
        shutil.copytree(source, tmp_target, symlinks=False)
        if target.exists():
            shutil.rmtree(target)
        tmp_target.replace(target)
        return True
    except Exception:
        try:
            if tmp_target.exists():
                shutil.rmtree(tmp_target)
        except Exception:
            pass
        return False


def _rel_reading_path(path: Path) -> str:
    try:
        return make_reading_paths_relative(str(path))
    except Exception:
        return str(path)


def _rewrite_reused_read_result_paths(result: dict, item_dir: Path, *, run_id: str, paper_index: int, source_dir: Path) -> dict:
    reused = dict(result)
    reused["run_id"] = run_id
    reused["paper_index"] = paper_index
    reused["reused_existing_deep_read_result_for_repair"] = True
    reused["reused_deep_read_run_dir"] = _rel_reading_path(source_dir)
    reused["reuse_policy"] = "Reused by copying the complete per-paper run directory into the current run; force=true bypasses this reuse and regenerates the subagent result."
    artifacts = dict(reused.get("artifacts") if isinstance(reused.get("artifacts"), dict) else {})
    artifacts.update({
        "paper": _rel_reading_path(item_dir / "paper.json"),
        "full_text_packet": _rel_reading_path(item_dir / "full_text_packet.json"),
        "prompt": _rel_reading_path(item_dir / "prompts" / "deep_read_prompt.md"),
        "read_md": _rel_reading_path(item_dir / "read.md"),
        "article_markdown": _rel_reading_path(item_dir / "read.md"),
        "read_results": _rel_reading_path(item_dir / "read_results.json"),
    })
    reused["artifacts"] = artifacts
    return reused


def _complete_read_result_from_item_dir(item_dir: Path, paper: dict, *, run_id: str, paper_index: int, source_dir: Path) -> dict:
    result = read_json(item_dir / "read_results.json", {})
    if not _reuse_existing_deep_read_enabled():
        return {}
    if not isinstance(result, dict) or not result:
        return {}
    validation = result.get("validation") if isinstance(result.get("validation"), dict) else {}
    if validation.get("deep_read_complete") is not True:
        return {}
    claude = result.get("claude") if isinstance(result.get("claude"), dict) else {}
    temp_audit = claude.get("external_temp_artifact_audit") if isinstance(claude.get("external_temp_artifact_audit"), dict) else {}
    boundary_audit = claude.get("nonruntime_artifact_audit") if isinstance(claude.get("nonruntime_artifact_audit"), dict) else {}
    if int(temp_audit.get("problem_count") or 0) > 0 or int(boundary_audit.get("problem_count") or 0) > 0:
        return {}
    artifacts = result.get("artifacts") if isinstance(result.get("artifacts"), dict) else {}
    article_md_value = str(artifacts.get("article_markdown") or artifacts.get("read_md") or "").strip()
    if not article_md_value:
        return {}
    try:
        article_md_path = ensure_inside_output(resolve_reading_path(article_md_value), label="单篇 read.md")
    except Exception:
        return {}
    claude_result = result.get("claude_result") if isinstance(result.get("claude_result"), dict) else {}
    if not _article_markdown_ready(article_md_path, claude_result, paper):
        return {}
    existing_paper = result.get("paper") if isinstance(result.get("paper"), dict) else {}
    if not _same_paper_identity_ok(
        paper,
        candidate_title=existing_paper.get("title") or paper.get("title"),
        candidate_authors=existing_paper.get("authors") or paper.get("authors"),
        candidate_doi=existing_paper.get("doi") or paper.get("doi"),
    ):
        return {}
    reused = _rewrite_reused_read_result_paths(result, item_dir, run_id=run_id, paper_index=paper_index, source_dir=source_dir)
    _write_read_result(item_dir / "read_results.json", reused)
    return reused


def _existing_complete_read_result_for_repair(item_dir: Path, paper: dict, *, run_id: str, paper_index: int) -> dict:
    if not _reuse_existing_deep_read_enabled():
        return {}
    current = _complete_read_result_from_item_dir(item_dir, paper, run_id=run_id, paper_index=paper_index, source_dir=item_dir)
    if current:
        _publish_article_read_cache(item_dir, paper, current)
        return current
    article_cached = _restore_article_read_cache(item_dir, paper, run_id=run_id, paper_index=paper_index)
    if article_cached:
        return article_cached
    cache_dir = _locate_article_cache_dir(paper)
    if cache_dir is not None:
        manifest = _article_cache_manifest(cache_dir)
        if (
            (cache_dir / "read.md").is_file()
            or (cache_dir / "full_text_packet.json").is_file()
            or bool(manifest.get("has_full_text"))
            or bool(manifest.get("has_pdf"))
        ):
            return {}
    seen: set[Path] = {item_dir.resolve(strict=False)}
    for key in _deep_read_cache_keys_for_paper(paper):
        for source_dir in _deep_read_cache_index().get(key, []):
            resolved_source = source_dir.resolve(strict=False)
            if resolved_source in seen:
                continue
            seen.add(resolved_source)
            source_result = read_json(source_dir / "read_results.json", {})
            source_paper = source_result.get("paper") if isinstance(source_result.get("paper"), dict) else {}
            if not _same_paper_identity_ok(
                paper,
                candidate_title=source_paper.get("title") or paper.get("title"),
                candidate_authors=source_paper.get("authors") or paper.get("authors"),
                candidate_doi=source_paper.get("doi") or paper.get("doi"),
            ):
                continue
            if not _copy_reused_deep_read_dir(source_dir, item_dir):
                continue
            reused = _complete_read_result_from_item_dir(item_dir, paper, run_id=run_id, paper_index=paper_index, source_dir=source_dir)
            if reused:
                _publish_article_read_cache(item_dir, paper, reused)
                return reused
    return {}


def _process_local_read_paper(
    *,
    run_id: str,
    directory: Path,
    index: int,
    row: dict,
    claude_mode: str,
    timeout_sec: int,
    log: LogFn,
    force_deep_read: bool = False,
) -> dict:
    from acquisition.paper_sources import acquire_full_text

    paper = _normalize_local_input_paper(row)
    item_dir = directory / "papers" / f"{index:03d}_{safe_slug(paper.get('paper_id') or paper.get('title') or 'paper')}"
    item_dir.mkdir(parents=True, exist_ok=True)
    existing_complete = {} if force_deep_read else _existing_complete_read_result_for_repair(item_dir, paper, run_id=run_id, paper_index=index)
    if existing_complete:
        log(f"复用单篇精读缓存 {index}: {paper.get('title') or paper.get('paper_id')}")
        return existing_complete
    article_md_path = item_dir / "read.md"
    try:
        article_md_path.unlink(missing_ok=True)
    except OSError:
        pass
    write_json(item_dir / "paper.json", paper)
    packet = _existing_full_text_packet_for_deep_read(item_dir, paper, allow_existing=True)
    if packet:
        log(f"复用已验证全文 {index}: {paper.get('title') or paper.get('paper_id')}")
    if not packet:
        packet = _restore_article_full_text_cache(paper, item_dir)
        if packet:
            log(f"复用文章缓存全文/PDF {index}: {paper.get('title') or paper.get('paper_id')}")
    if not packet:
        packet = acquire_full_text(paper, item_dir, log=log, services=_reading_acquisition_services())
    _verify_packet_full_text_title(paper, packet)
    write_json(item_dir / "paper.json", paper)
    cache_publication = _publish_article_full_text_cache(item_dir, paper, packet)
    write_json(item_dir / "full_text_packet.json", {"run_id": run_id, "paper_index": index, "papers": [packet], "generated_at": _now_iso()})
    if not force_deep_read and cache_publication.get("read_cache_invalidated") is not True:
        refreshed_read = _restore_article_read_cache(item_dir, paper, run_id=run_id, paper_index=index)
        if refreshed_read:
            log(f"已更新全文并复用单篇精读缓存 {index}: {paper.get('title') or paper.get('paper_id')}")
            return refreshed_read
    output_path = item_dir / "outputs" / "reading_result.json"
    prompt_path = item_dir / "prompts" / "deep_read_prompt.md"
    article_md_path = item_dir / "read.md"
    prompt = build_deep_read_prompt(
        paper=paper,
        packet=packet,
        run_path=item_dir,
        output_path=output_path,
        article_md_path=article_md_path,
    )
    write_text(prompt_path, prompt)
    full_text_ready = bool(packet.get("full_text_available")) and int(packet.get("full_text_chars") or packet.get("text_chars") or 0) >= FULL_TEXT_MIN_CHARS
    mode = claude_mode if full_text_ready else "prepare"
    if mode != "prepare":
        try:
            article_md_path.unlink(missing_ok=True)
        except OSError:
            pass
    receipt = run_claude_deep_read(
        prompt_path=prompt_path,
        run_path=item_dir,
        expected_output_path=output_path,
        timeout_sec=timeout_sec,
        mode=mode,
    )
    result_payload = _load_result_payload(output_path, receipt)
    article_markdown_ready = _article_markdown_ready(article_md_path, result_payload, paper)
    complete = (
        _deep_read_complete(receipt, result_payload)
        and article_markdown_ready
    )
    status = "complete" if complete else "prepared_full_text_for_main_claude_subagent" if full_text_ready else "blocked_full_text_unavailable"
    if mode == "run" and not complete:
        status = str(receipt.get("status") or status)
    note_zh = (
        "已完成 Claude/subagent 精读，用户正文只保存在单篇 read.md。"
        if complete
        else "已取得可精读正文证据，但当前未完成 Claude/subagent 精读或未直接写出单篇 read.md，不能报告为精读完成。"
        if full_text_ready
        else "未取得足够全文证据，不能进入 Reading subagent 精读。"
    )
    merged_reading = _reading_machine_state(
        paper,
        packet,
        full_text_ready=full_text_ready,
        deep_read_complete=complete,
        note_zh=note_zh,
    )
    _sanitize_reading_public_fields(merged_reading)
    result = {
        "run_id": run_id,
        "paper_index": index,
        "status": status,
        "generated_at": _now_iso(),
        "paper": paper,
        "full_text_packet": packet,
        "claude": receipt,
        "claude_result": result_payload,
        "reading": merged_reading,
        "validation": {
            "full_text_ready": full_text_ready,
            "deep_read_complete": complete,
            "replacement_policy": "forbidden",
            "same_paper_repair_policy": "allowed_pdf_html_xml_for_same_input_paper_only",
        },
        "artifacts": {
            "paper": str(item_dir / "paper.json"),
            "full_text_packet": str(item_dir / "full_text_packet.json"),
            "prompt": str(prompt_path),
            "read_md": str(item_dir / "read.md"),
            "article_markdown": str(article_md_path),
            "read_results": str(item_dir / "read_results.json"),
        },
    }
    _write_read_result(item_dir / "read_results.json", result)
    _write_single_read_md_if_needed(article_md_path, result, complete=complete)
    if complete:
        _publish_article_read_cache(item_dir, paper, result)
    return result


def _prepare_local_read_paper(
    *,
    run_id: str,
    directory: Path,
    index: int,
    row: dict,
    log: LogFn,
    force_deep_read: bool = False,
) -> dict:
    from acquisition.paper_sources import acquire_full_text

    paper = _normalize_local_input_paper(row)
    item_dir = directory / "papers" / f"{index:03d}_{safe_slug(paper.get('paper_id') or paper.get('title') or 'paper')}"
    item_dir.mkdir(parents=True, exist_ok=True)
    existing_complete = {} if force_deep_read else _existing_complete_read_result_for_repair(item_dir, paper, run_id=run_id, paper_index=index)
    if existing_complete:
        log(f"复用单篇精读缓存 {index}: {paper.get('title') or paper.get('paper_id')}")
        return existing_complete
    try:
        (item_dir / "read.md").unlink(missing_ok=True)
    except OSError:
        pass
    write_json(item_dir / "paper.json", paper)
    packet = _existing_full_text_packet_for_deep_read(item_dir, paper, allow_existing=True)
    if packet:
        log(f"复用已验证全文 {index}: {paper.get('title') or paper.get('paper_id')}")
    if not packet:
        packet = _restore_article_full_text_cache(paper, item_dir)
        if packet:
            log(f"复用文章缓存全文/PDF {index}: {paper.get('title') or paper.get('paper_id')}")
    if not packet:
        packet = acquire_full_text(paper, item_dir, log=log, services=_reading_acquisition_services())
    _verify_packet_full_text_title(paper, packet)
    write_json(item_dir / "paper.json", paper)
    cache_publication = _publish_article_full_text_cache(item_dir, paper, packet)
    write_json(item_dir / "full_text_packet.json", {"run_id": run_id, "paper_index": index, "papers": [packet], "generated_at": _now_iso()})
    if not force_deep_read and cache_publication.get("read_cache_invalidated") is not True:
        refreshed_read = _restore_article_read_cache(item_dir, paper, run_id=run_id, paper_index=index)
        if refreshed_read:
            log(f"已更新全文并复用单篇精读缓存 {index}: {paper.get('title') or paper.get('paper_id')}")
            return refreshed_read
    full_text_ready = bool(packet.get("full_text_available")) and int(packet.get("full_text_chars") or packet.get("text_chars") or 0) >= FULL_TEXT_MIN_CHARS
    if full_text_ready:
        status = "prepared_full_text_for_reading_subagent"
        note_zh = "已取得可精读正文证据，等待并发 Reading subagent 精读。"
    else:
        status = "blocked_full_text_unavailable"
        note_zh = "未取得足够全文证据，不能进入 Reading subagent 精读。"
    fallback_reading = _reading_machine_state(
        paper,
        packet,
        full_text_ready=full_text_ready,
        deep_read_complete=False,
        note_zh=note_zh,
    )
    _sanitize_reading_public_fields(fallback_reading)
    result = {
        "run_id": run_id,
        "paper_index": index,
        "status": status,
        "generated_at": _now_iso(),
        "paper": paper,
        "full_text_packet": packet,
        "claude": {"status": "not_started_until_all_full_text_prepared", "run_executed": False},
        "claude_result": {},
        "reading": fallback_reading,
        "validation": {
            "full_text_ready": full_text_ready,
            "deep_read_complete": False,
            "replacement_policy": "forbidden",
            "same_paper_repair_policy": "allowed_pdf_html_xml_for_same_input_paper_only",
            "phase": "full_text_prepared_before_reading_subagents",
        },
        "artifacts": {
            "paper": str(item_dir / "paper.json"),
            "full_text_packet": str(item_dir / "full_text_packet.json"),
            "prompt": str(item_dir / "prompts" / "deep_read_prompt.md"),
            "read_md": str(item_dir / "read.md"),
            "read_results": str(item_dir / "read_results.json"),
        },
    }
    _write_read_result(item_dir / "read_results.json", result)
    return result


def _read_exception_payload(phase: str, exc: BaseException) -> dict:
    return {
        "phase": phase,
        "error_type": exc.__class__.__name__,
        "error_message": str(exc)[:1000],
        "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__, limit=12))[:8000],
    }


def _failed_prepare_item(
    *,
    run_id: str,
    directory: Path,
    index: int,
    row: dict,
    exc: BaseException,
) -> dict:
    try:
        paper = _normalize_local_input_paper(row)
    except Exception:
        paper = dict(row) if isinstance(row, dict) else {"title": "Untitled"}
    item_dir = directory / "papers" / f"{index:03d}_{safe_slug(paper.get('paper_id') or paper.get('title') or 'paper')}"
    item_dir.mkdir(parents=True, exist_ok=True)
    error = _read_exception_payload("full_text_acquisition", exc)
    packet = {
        "run_id": run_id,
        "paper_index": index,
        "paper_id": paper.get("paper_id") or paper.get("id") or f"paper_{index:03d}",
        "title": paper.get("title") or "Untitled",
        "url": paper.get("url") or "",
        "pdf_url": paper.get("pdf_url") or "",
        "full_text_available": False,
        "full_text_status": "full_text_acquisition_error",
        "full_text_chars": 0,
        "error": error,
    }
    fallback_reading = _reading_machine_state(
        paper,
        packet,
        full_text_ready=False,
        deep_read_complete=False,
        note_zh="Read 爬取全文时发生错误；错误只记录在运行日志和 read_results.json 中。",
    )
    _sanitize_reading_public_fields(fallback_reading)
    result = {
        "run_id": run_id,
        "paper_index": index,
        "status": "error_full_text_acquisition",
        "generated_at": _now_iso(),
        "paper": paper,
        "full_text_packet": packet,
        "read_error": error,
        "claude": {"status": "not_started_full_text_acquisition_failed", "run_executed": False},
        "claude_result": {},
        "reading": fallback_reading,
        "validation": {
            "full_text_ready": False,
            "deep_read_complete": False,
            "replacement_policy": "forbidden",
            "same_paper_repair_policy": "allowed_pdf_html_xml_for_same_input_paper_only",
            "phase": "full_text_acquisition_error_recorded",
            "error_logged": True,
        },
        "artifacts": {
            "paper": str(item_dir / "paper.json"),
            "full_text_packet": str(item_dir / "full_text_packet.json"),
            "prompt": str(item_dir / "prompts" / "deep_read_prompt.md"),
            "read_md": str(item_dir / "read.md"),
            "read_results": str(item_dir / "read_results.json"),
        },
    }
    write_json(item_dir / "paper.json", paper)
    write_json(item_dir / "full_text_packet.json", {"run_id": run_id, "paper_index": index, "papers": [packet], "generated_at": _now_iso()})
    _write_read_result(item_dir / "read_results.json", result)
    return result


def _failed_reading_subagent_item(prepared: dict, exc: BaseException) -> dict:
    error = _read_exception_payload("reading_subagent", exc)
    artifacts = prepared.get("artifacts") if isinstance(prepared.get("artifacts"), dict) else {}
    read_results_value = str(artifacts.get("read_results") or "").strip()
    read_results_path = resolve_reading_path(read_results_value) if read_results_value else run_dir(str(prepared.get("run_id") or "read_run")) / "papers" / f"{int(prepared.get('paper_index') or 0):03d}_paper" / "read_results.json"
    item_dir = read_results_path.parent
    item_dir.mkdir(parents=True, exist_ok=True)
    reading = dict(prepared.get("reading")) if isinstance(prepared.get("reading"), dict) else {}
    reading["reading_status_note_zh"] = "Reading subagent 精读时发生错误；错误只记录在运行日志和 read_results.json 中。"
    _sanitize_reading_public_fields(reading)
    result = {
        **prepared,
        "status": "error_reading_subagent",
        "generated_at": _now_iso(),
        "read_error": error,
        "claude": {
            **(prepared.get("claude") if isinstance(prepared.get("claude"), dict) else {}),
            "status": "reading_subagent_error",
            "run_executed": True,
            "error": error,
        },
        "reading": reading,
        "validation": {
            **(prepared.get("validation") if isinstance(prepared.get("validation"), dict) else {}),
            "deep_read_complete": False,
            "phase": "reading_subagent_error_recorded",
            "error_logged": True,
        },
        "artifacts": {
            **artifacts,
            "read_results": str(read_results_path),
        },
    }
    _write_read_result(read_results_path, result)
    return result


def _normalize_article_markdown_file(path: Path, paper: dict, packet: dict) -> str:
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    normalized = _normalize_article_markdown_metadata(text, paper, packet)
    if normalized != text:
        path.write_text(normalized, encoding="utf-8")
    return normalized


def _run_content_quality_repair_once(
    *,
    paper: dict,
    packet: dict,
    item_dir: Path,
    article_md_path: Path,
    output_path: Path,
    receipt: dict,
    result_payload: dict,
    mode: str,
    timeout_sec: int,
    log: LogFn,
    index: int,
) -> dict:
    article_text = article_md_path.read_text(encoding="utf-8", errors="replace") if article_md_path.is_file() else ""
    quality_issue = _article_markdown_quality_issue(article_text, paper) if article_text else ""
    quality_reason = _article_markdown_quality_reason(article_text, paper) if quality_issue else ""
    if mode == "prepare" or not quality_issue:
        if article_text:
            _normalize_article_markdown_file(article_md_path, paper, packet)
        return {
            "receipt": receipt,
            "result_payload": result_payload,
            "quality_issue": quality_issue,
            "quality_reason": quality_reason,
            "quality_retry": {},
        }

    repair_prompt_path = item_dir / "prompts" / "content_quality_repair_prompt.md"
    repair_prompt = build_deep_read_repair_prompt(
        paper=paper,
        run_path=item_dir,
        output_path=output_path,
        article_md_path=article_md_path,
        quality_issue=quality_issue,
        quality_reason=quality_reason,
    )
    write_text(repair_prompt_path, repair_prompt)
    log(f"Retrying reading subagent {index} with a new repair Claude: content_quality_issue={quality_issue} - {paper.get('title') or paper.get('paper_id')}")
    try:
        retry_receipt = run_claude_deep_read(
            prompt_path=repair_prompt_path,
            run_path=item_dir,
            expected_output_path=output_path,
            timeout_sec=timeout_sec,
            mode=mode,
            receipt_dir_name="claude_content_quality_retry",
        )
        retry_payload = _load_result_payload(output_path, retry_receipt)
        repaired_text = article_md_path.read_text(encoding="utf-8", errors="replace") if article_md_path.is_file() else ""
        final_issue = _article_markdown_quality_issue(repaired_text, paper) if repaired_text else quality_issue
        final_reason = _article_markdown_quality_reason(repaired_text, paper) if repaired_text and final_issue else quality_reason
        if repaired_text and not final_issue:
            _normalize_article_markdown_file(article_md_path, paper, packet)
        quality_retry = {
            "attempted": True,
            "attempt_count": 1,
            "new_claude_process": True,
            "repair_prompt": _rel_reading_path(repair_prompt_path),
            "initial_issue": quality_issue,
            "initial_reason": quality_reason,
            "final_issue": final_issue,
            "final_reason": final_reason,
            "resolved": not final_issue,
        }
        receipt = {**retry_receipt, "content_quality_retry": quality_retry}
        result_payload = retry_payload
        quality_issue = final_issue
        quality_reason = final_reason
    except Exception as exc:
        quality_retry = {
            "attempted": True,
            "attempt_count": 1,
            "new_claude_process": True,
            "repair_prompt": _rel_reading_path(repair_prompt_path),
            "initial_issue": quality_issue,
            "initial_reason": quality_reason,
            "final_issue": quality_issue,
            "final_reason": quality_reason,
            "resolved": False,
            "error": _read_exception_payload("reading_content_quality_retry", exc),
        }
        receipt = {**receipt, "content_quality_retry": quality_retry}
    log(f"Finished reading content-quality retry {index}: {quality_issue or 'resolved'}")
    return {
        "receipt": receipt,
        "result_payload": result_payload,
        "quality_issue": quality_issue,
        "quality_reason": quality_reason,
        "quality_retry": quality_retry,
    }


def _run_reading_subagent_for_prepared_paper(
    *,
    prepared: dict,
    claude_mode: str,
    timeout_sec: int,
    log: LogFn = print,
) -> dict:
    if prepared.get("validation", {}).get("deep_read_complete") is True:
        return prepared
    if prepared.get("validation", {}).get("full_text_ready") is not True:
        return prepared
    paper = prepared.get("paper") if isinstance(prepared.get("paper"), dict) else {}
    packet = prepared.get("full_text_packet") if isinstance(prepared.get("full_text_packet"), dict) else {}
    run_id = str(prepared.get("run_id") or "")
    index = int(prepared.get("paper_index") or 0)
    artifacts = prepared.get("artifacts") if isinstance(prepared.get("artifacts"), dict) else {}
    read_results_value = str(artifacts.get("read_results") or "").strip()
    if not read_results_value:
        result = {
            **prepared,
            "status": "blocked_read_results_artifact_missing",
            "generated_at": _now_iso(),
            "validation": {
                **(prepared.get("validation") if isinstance(prepared.get("validation"), dict) else {}),
                "deep_read_complete": False,
                "phase": "reading_subagent_blocked_missing_read_results_artifact",
            },
        }
        return result
    try:
        read_results_path = resolve_reading_path(read_results_value)
    except Exception:
        result = {
            **prepared,
            "status": "blocked_read_results_artifact_outside_reading_runtime",
            "generated_at": _now_iso(),
            "validation": {
                **(prepared.get("validation") if isinstance(prepared.get("validation"), dict) else {}),
                "deep_read_complete": False,
                "phase": "reading_subagent_blocked_invalid_read_results_artifact",
            },
        }
        return result
    item_dir = read_results_path.parent
    output_path = item_dir / "outputs" / "reading_result.json"
    prompt_path = item_dir / "prompts" / "deep_read_prompt.md"
    article_md_path = item_dir / "read.md"
    prompt = build_deep_read_prompt(
        paper=paper,
        packet=packet,
        run_path=item_dir,
        output_path=output_path,
        article_md_path=article_md_path,
    )
    write_text(prompt_path, prompt)
    mode = claude_mode if claude_mode in {"run", "auto"} else "prepare"
    if mode != "prepare":
        try:
            article_md_path.unlink(missing_ok=True)
        except OSError:
            pass
    receipt = run_claude_deep_read(
        prompt_path=prompt_path,
        run_path=item_dir,
        expected_output_path=output_path,
        timeout_sec=timeout_sec,
        mode=mode,
    )
    result_payload = _load_result_payload(output_path, receipt)
    quality = _run_content_quality_repair_once(
        paper=paper,
        packet=packet,
        item_dir=item_dir,
        article_md_path=article_md_path,
        output_path=output_path,
        receipt=receipt,
        result_payload=result_payload,
        mode=mode,
        timeout_sec=timeout_sec,
        log=log,
        index=index,
    )
    receipt = quality["receipt"]
    result_payload = quality["result_payload"]
    quality_issue = str(quality["quality_issue"] or "")
    quality_reason = str(quality["quality_reason"] or "")
    quality_retry = quality["quality_retry"]
    article_markdown_ready = _article_markdown_ready(article_md_path, result_payload, paper)
    complete = (
        _deep_read_complete(receipt, result_payload)
        and article_markdown_ready
    )
    status = "complete" if complete else quality_issue or str(receipt.get("status") or "prepared_full_text_for_main_claude_subagent")
    note_zh = (
        "已完成 Claude/subagent 精读，用户正文只保存在单篇 read.md。"
        if complete
        else f"精读产物内容质量未通过：{quality_issue}。"
        if quality_issue
        else "已取得可精读正文证据，但当前未完成 Claude/subagent 精读或未直接写出单篇 read.md，不能报告为精读完成。"
    )
    merged_reading = _reading_machine_state(
        paper,
        packet,
        full_text_ready=True,
        deep_read_complete=complete,
        note_zh=note_zh,
    )
    _sanitize_reading_public_fields(merged_reading)
    result = {
        **prepared,
        "status": status,
        "generated_at": _now_iso(),
        "claude": receipt,
        "claude_result": result_payload,
        "reading": merged_reading,
        "validation": {
            **(prepared.get("validation") if isinstance(prepared.get("validation"), dict) else {}),
            "full_text_ready": True,
            "deep_read_complete": complete,
            "phase": "reading_subagent_content_quality_failed" if quality_issue else "reading_subagent_completed_after_full_text_collection",
            "content_quality_issue": quality_issue,
            "content_quality_reason": quality_reason,
            "content_quality_retry": quality_retry,
        },
        "artifacts": {
            **artifacts,
            "prompt": str(prompt_path),
            "read_md": str(article_md_path),
            "article_markdown": str(article_md_path),
            "read_results": str(item_dir / "read_results.json"),
        },
    }
    _write_read_result(item_dir / "read_results.json", result)
    _write_single_read_md_if_needed(article_md_path, result, complete=complete)
    if complete:
        _publish_article_read_cache(item_dir, paper, result)
    return result


def _load_subagent_result(output_path: Path, receipt: dict) -> dict:
    payload = read_json(output_path, {})
    if isinstance(payload, dict) and payload:
        return _strip_reading_content_payload(payload)
    fallback = receipt.get("result_payload") if isinstance(receipt.get("result_payload"), dict) else {}
    return _strip_reading_content_payload(fallback) if isinstance(fallback, dict) else {}


def _article_markdown_path_for_completed_item(item: dict) -> Path | None:
    validation = item.get("validation") if isinstance(item.get("validation"), dict) else {}
    if validation.get("deep_read_complete") is not True:
        return None
    artifacts = item.get("artifacts") if isinstance(item.get("artifacts"), dict) else {}
    claude_result = item.get("claude_result") if isinstance(item.get("claude_result"), dict) else {}
    for key in ["article_markdown", "read_md"]:
        value = str(artifacts.get(key) or "").strip()
        if not value:
            continue
        try:
            path = ensure_inside_output(resolve_reading_path(value), label="单篇 read.md")
        except Exception:
            continue
        paper = item.get("paper") if isinstance(item.get("paper"), dict) else {}
        if _article_markdown_ready(path, claude_result, paper):
            return path
    return None


def _article_title_for_log(item: dict, fallback: str = "Untitled") -> str:
    paper = item.get("paper") if isinstance(item.get("paper"), dict) else {}
    reading = item.get("reading") if isinstance(item.get("reading"), dict) else {}
    return str(reading.get("title") or paper.get("title") or paper.get("paper_id") or fallback).strip() or fallback


def _score_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if not 0.0 <= score <= 10.0:
        return None
    return round(score, 1)


def _reading_scoring_receipt_gate(receipt: dict) -> dict[str, object]:
    status = str(receipt.get("status") or "").strip()
    expected_audit = receipt.get("expected_output_audit") if isinstance(receipt.get("expected_output_audit"), dict) else {}
    nonruntime_audit = receipt.get("nonruntime_artifact_audit") if isinstance(receipt.get("nonruntime_artifact_audit"), dict) else {}
    external_temp_audit = receipt.get("external_temp_artifact_audit") if isinstance(receipt.get("external_temp_artifact_audit"), dict) else {}
    checks = {
        "status_acceptable": status in {"complete", "completed", "claude_completed"},
        "run_executed": receipt.get("run_executed") is True,
        "return_code_zero": receipt.get("return_code") == 0,
        "expected_output_valid": expected_audit.get("exists") is True and expected_audit.get("valid_json") is True,
        "nonruntime_artifact_audit_passed": (
            int(nonruntime_audit.get("problem_count") or 0) == 0
            and nonruntime_audit.get("status") == "passed"
        ),
        "external_temp_artifact_audit_passed": (
            int(external_temp_audit.get("problem_count") or 0) == 0
            and external_temp_audit.get("status") == "passed"
        ),
    }
    return {
        "accepted": all(checks.values()),
        "status": status,
        "checks": checks,
    }


def _normalize_reading_scores(payload: dict, items: list[dict]) -> dict[int, dict[str, float]]:
    valid_indices = {
        int(item.get("paper_index") or index)
        for index, item in enumerate(items, 1)
        if item.get("validation", {}).get("deep_read_complete") is True
    }
    rows = payload.get("scores") if isinstance(payload, dict) else []
    normalized: dict[int, dict[str, float]] = {}
    if not isinstance(rows, list):
        return normalized
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            paper_index = int(row.get("paper_index"))
        except (TypeError, ValueError):
            continue
        match_score = _score_number(row.get("match_score"))
        transferability_score = _score_number(row.get("transferability_score"))
        if paper_index not in valid_indices or match_score is None or transferability_score is None:
            continue
        normalized[paper_index] = {
            "match_score": match_score,
            "transferability_score": transferability_score,
            "average_score": round((match_score + transferability_score) / 2.0, 2),
        }
    return normalized


def _apply_reading_scores_and_rank(
    items: list[dict],
    scores: dict[int, dict[str, float]],
    *,
    rerank: bool = True,
) -> list[dict]:
    indexed_items: list[tuple[int, dict]] = []
    for original_order, item in enumerate(items, 1):
        paper_index = int(item.get("paper_index") or original_order)
        reading = item.get("reading") if isinstance(item.get("reading"), dict) else None
        item.pop("match_score", None)
        item.pop("transferability_score", None)
        item.pop("average_score", None)
        if reading is not None:
            reading.pop("match_score", None)
            reading.pop("transferability_score", None)
            reading.pop("average_score", None)
        score = scores.get(paper_index)
        if score:
            item.update(score)
            if reading is not None:
                reading.update(score)
        indexed_items.append((original_order, item))
    if rerank:
        indexed_items.sort(key=lambda entry: (
            entry[1].get("average_score") is None,
            -float(entry[1].get("average_score") or 0.0),
            entry[0],
        ))
    ranked = [item for _, item in indexed_items]
    for final_rank, item in enumerate(ranked, 1):
        item["final_read_rank"] = final_rank
        reading = item.get("reading") if isinstance(item.get("reading"), dict) else None
        if reading is not None:
            reading["final_read_rank"] = final_rank
    return ranked


def _run_final_reading_scoring(
    *,
    directory: Path,
    items: list[dict],
    research_context: dict,
    claude_mode: str,
    timeout_sec: int,
    log: LogFn,
) -> tuple[list[dict], dict]:
    candidates: list[dict] = []
    for index, item in enumerate(items, 1):
        path = _article_markdown_path_for_completed_item(item)
        if path is None:
            continue
        candidates.append({
            "paper_index": int(item.get("paper_index") or index),
            "title": _article_title_for_log(item),
            "article_markdown_path": path.resolve(strict=False).relative_to(directory.resolve(strict=False)).as_posix(),
        })
    if claude_mode == "prepare" or not candidates:
        ranked = _apply_reading_scores_and_rank(items, {})
        return ranked, {
            "status": "not_started_prepare_mode" if claude_mode == "prepare" else "skipped_no_completed_reading_artifacts",
            "attempted": False,
            "expected_article_count": len(candidates),
            "scored_article_count": 0,
        }
    if not any(value not in (None, "", [], {}) for value in research_context.values()):
        ranked = _apply_reading_scores_and_rank(items, {})
        return ranked, {
            "status": "skipped_missing_research_context",
            "attempted": False,
            "required": True,
            "expected_article_count": len(candidates),
            "scored_article_count": 0,
        }

    scoring_dir = directory / "scoring"
    scoring_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = scoring_dir / "prompt.md"
    output_path = directory / "outputs" / "reading_scores.json"
    write_text(prompt_path, build_reading_score_prompt(
        research_context=research_context,
        articles=candidates,
        run_path=directory,
        output_path=output_path,
    ))
    log(f"Final Reading scoring phase: {len(candidates)} completed reading artifacts")
    try:
        receipt = run_claude_deep_read(
            prompt_path=prompt_path,
            run_path=directory,
            expected_output_path=output_path,
            timeout_sec=timeout_sec,
            mode=claude_mode,
            receipt_dir_name="claude_scoring",
        )
    except Exception as exc:
        ranked = _apply_reading_scores_and_rank(items, {}, rerank=False)
        error = _read_exception_payload("final_scoring", exc)
        write_json(output_path, {
            "status": "complete_with_warnings",
            "scores": [],
            "expected_article_count": len(candidates),
            "scored_article_count": 0,
            "error": error,
        })
        log(f"Warning: Final Reading scoring failed: {exc.__class__.__name__}: {str(exc)[:300]}")
        return ranked, {
            "status": "complete_with_warnings",
            "attempted": True,
            "expected_article_count": len(candidates),
            "scored_article_count": 0,
            "score_artifact": str(output_path),
            "ranking_policy": "preserve_input_ranking_when_scoring_fails",
            "receipt_gate": {"accepted": False, "reason": "scoring_exception"},
            "error": error,
        }
    raw_payload = read_json(output_path, {})
    if not isinstance(raw_payload, dict) or not raw_payload:
        raw_payload = receipt.get("result_payload") if isinstance(receipt.get("result_payload"), dict) else {}
    receipt_gate = _reading_scoring_receipt_gate(receipt)
    scores = _normalize_reading_scores(raw_payload, items) if receipt_gate.get("accepted") is True else {}
    candidate_indices = {int(candidate["paper_index"]) for candidate in candidates}
    scores = {paper_index: score for paper_index, score in scores.items() if paper_index in candidate_indices}
    scoring_complete = receipt_gate.get("accepted") is True and set(scores) == candidate_indices
    ranked = _apply_reading_scores_and_rank(items, scores, rerank=scoring_complete)
    canonical_scores = [
        {"paper_index": paper_index, **score}
        for paper_index, score in sorted(scores.items())
    ]
    scoring_status = "complete" if scoring_complete else "complete_with_warnings"
    ranking_policy = (
        "descending_average_of_match_score_and_transferability_score"
        if scoring_complete
        else "preserve_input_ranking_when_scoring_incomplete_or_untrusted"
    )
    write_json(output_path, {
        "status": scoring_status,
        "scores": canonical_scores,
        "expected_article_count": len(candidates),
        "scored_article_count": len(scores),
        "ranking_policy": ranking_policy,
        "receipt_gate": receipt_gate,
    })
    log(f"Final Reading scoring complete: scored={len(scores)}/{len(candidates)}")
    return ranked, {
        "status": scoring_status,
        "attempted": True,
        "expected_article_count": len(candidates),
        "scored_article_count": len(scores),
        "score_artifact": str(output_path),
        "ranking_policy": ranking_policy,
        "receipt_gate": receipt_gate,
        "claude": receipt,
    }


def _demote_article_markdown(text: str, *, index: int, title: str, item: dict | None = None) -> str:
    body = _sanitize_article_markdown_text(str(text or "")).strip()
    lines = body.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    article_title = title
    if lines and lines[0].startswith("# "):
        lines.pop(0)
    while lines and not lines[0].strip():
        lines.pop(0)
    output = [f"## {index}. {article_title}", ""]
    item = item if isinstance(item, dict) else {}
    match_score = _score_number(item.get("match_score"))
    transferability_score = _score_number(item.get("transferability_score"))
    if match_score is not None and transferability_score is not None:
        output.extend([
            f"- **匹配度：** {match_score:g}/10",
            f"- **可借鉴性：** {transferability_score:g}/10",
        ])
    for line in lines:
        if line.startswith("#"):
            hashes = len(line) - len(line.lstrip("#"))
            rest = line[hashes:].lstrip()
            new_level = min(6, hashes + 1)
            output.append("#" * new_level + " " + rest)
        else:
            output.append(line)
    return "\n".join(output).strip()


def _aggregate_read_md_by_concatenation(
    *,
    read_md_path: Path,
    article_md_paths: list[Path],
    items: list[dict],
) -> dict:
    sections: list[str] = ["# 论文精读", ""]
    completed_items = [
        item for item in items if item.get("validation", {}).get("deep_read_complete") is True
    ]
    item_by_path: dict[str, dict] = {}
    for item in completed_items:
        path = _article_markdown_path_for_completed_item(item)
        if path is not None:
            item_by_path[str(path.resolve(strict=False))] = item
    for index, path in enumerate(article_md_paths, 1):
        text = path.read_text(encoding="utf-8", errors="replace")
        item = item_by_path.get(str(path.resolve(strict=False)), {})
        title = _article_title_for_log(item, fallback=path.parent.name)
        article_body = _demote_article_markdown(text, index=index, title=title, item=item)
        if article_body:
            sections.append(article_body)
            sections.append("")
    read_md_path.parent.mkdir(parents=True, exist_ok=True)
    read_md_path.write_text("\n".join(sections).rstrip() + "\n", encoding="utf-8")
    return {
        "status": "completed",
        "valid": True,
        "mode": "deterministic_concat_single_subagent_markdown",
        "subagent_used": False,
        "checked_article_count": len(article_md_paths),
        "written_read_md_path": str(read_md_path),
        "per_paper_sections_preserved": True,
        "notes": "Final read.md was assembled only by concatenating completed per-paper subagent Markdown.",
    }


def _run_warning_detail(item: dict, index: int) -> dict:
    validation = item.get("validation") if isinstance(item.get("validation"), dict) else {}
    packet = item.get("full_text_packet") if isinstance(item.get("full_text_packet"), dict) else {}
    reading = item.get("reading") if isinstance(item.get("reading"), dict) else {}
    error = item.get("read_error") if isinstance(item.get("read_error"), dict) else packet.get("error") if isinstance(packet.get("error"), dict) else {}
    claude = item.get("claude") if isinstance(item.get("claude"), dict) else {}
    expected_audit = claude.get("expected_output_audit") if isinstance(claude.get("expected_output_audit"), dict) else {}
    if not error and expected_audit and expected_audit.get("valid_json") is False:
        error = {
            "error_type": str(expected_audit.get("error") or "invalid_expected_output_json"),
            "error_message": str(expected_audit.get("message") or ""),
        }
    full_text_ready = validation.get("full_text_ready") is True
    deep_read_complete = validation.get("deep_read_complete") is True
    content_quality_issue = str(validation.get("content_quality_issue") or "")
    if not full_text_ready:
        phase = "full_text_acquisition"
        message = "未取得同篇全文证据"
    elif content_quality_issue:
        phase = "reading_content_quality"
        message = f"精读产物内容质量未通过：{content_quality_issue}"
    elif not deep_read_complete:
        phase = "reading_subagent"
        message = "未完成 Reading subagent 精读"
    else:
        phase = str(validation.get("phase") or "read")
        message = str(reading.get("reading_status_note_zh") or item.get("status") or "")
    return {
        "index": int(item.get("paper_index") or index),
        "title": _article_title_for_log(item),
        "phase": phase,
        "status": content_quality_issue or str(item.get("status") or ""),
        "message": message,
        "content_quality_issue": content_quality_issue,
        "full_text_ready": full_text_ready,
        "deep_read_complete": deep_read_complete,
        "full_text_status": str(reading.get("full_text_status") or packet.get("full_text_status") or ""),
        "error_type": str(error.get("error_type") or error.get("error") or ""),
        "error_message": str(error.get("error_message") or error.get("message") or "")[:1000],
    }


def _final_read_md_structure_audit(read_md_text: str, expected_article_count: int) -> dict[str, object]:
    h2_matches = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", read_md_text))
    h2_titles = [match.group(1).strip() for match in h2_matches]
    per_paper_text = read_md_text
    per_paper_section_count = len(re.findall(r"(?m)^##\s+\d+\.\s+", per_paper_text))
    required_heading_groups = {
        "摘要": ["摘要", "速览", "原论文摘要"],
        "动机与核心创新": ["动机与核心创新", "动机和核心创新", "动机及核心创新"],
        "方法": ["方法", "机制", "详细方法", "方法机制", "训练策略"],
        "实验结果": ["实验结果", "实验与证据", "实验设置与结果", "实验", "结果"],
        "优缺点总结": ["优缺点总结", "优缺点", "方法优缺点", "优点", "不足", "局限", "局限性", "风险边界", "证据边界"],
    }

    def _heading_group_count(aliases: list[str]) -> int:
        pattern = "|".join(re.escape(alias) for alias in aliases)
        return len(re.findall(rf"(?m)^###\s+.*(?:{pattern})", per_paper_text))

    heading_counts = {
        heading: _heading_group_count(aliases)
        for heading, aliases in required_heading_groups.items()
    }
    expected_count = max(0, int(expected_article_count or 0))
    required_preserved = (
        expected_count == 0
        or (
            per_paper_section_count >= expected_count
            and all(heading_counts.get(group, 0) >= expected_count for group in required_heading_groups)
        )
    )
    preserved = required_preserved
    valid = (
        read_md_text.lstrip().startswith("# 论文精读")
        and preserved
    )
    issues: list[str] = []
    if not read_md_text.lstrip().startswith("# 论文精读"):
        issues.append("missing_top_level_reading_title")
    if expected_count and per_paper_section_count < expected_count:
        issues.append(f"per_paper_section_count {per_paper_section_count} < expected {expected_count}")
    for heading in required_heading_groups:
        count = heading_counts.get(heading, 0)
        if expected_count and count < expected_count:
            issues.append(f"required_heading_{heading}_count {count} < expected {expected_count}")
    return {
        "valid": valid,
        "issues": issues,
        "h2_titles": h2_titles,
        "per_paper_section_count": per_paper_section_count,
        "expected_article_count": expected_count,
        "required_heading_counts": heading_counts,
    }


def _aggregate_read_md_from_article_markdown(
    *,
    run_path: Path,
    items: list[dict],
    read_md_path: Path,
) -> dict:
    article_md_paths = [
        path
        for item in items
        for path in [_article_markdown_path_for_completed_item(item)]
        if path is not None
    ]
    warning_items = [
        {
            "index": int(item.get("paper_index") or index),
            "title": _article_title_for_log(item),
            "status": str(item.get("status") or ""),
        }
        for index, item in enumerate(items, 1)
        if item.get("validation", {}).get("deep_read_complete") is not True
    ]
    output_path = run_path / "outputs" / "read_md_aggregation.json"
    mode = "deterministic_concat_single_subagent_markdown"
    result_payload = {
        "status": "completed",
        "valid": True,
        "subagent_read_md_aggregation": False,
        "read_md_path": str(read_md_path),
        "article_markdown_paths": [str(path) for path in article_md_paths],
        "read_markdown_aggregation": _aggregate_read_md_by_concatenation(
            read_md_path=read_md_path,
            article_md_paths=article_md_paths,
            items=items,
        ),
    }
    structure_audit = _final_read_md_structure_audit(
        read_md_path.read_text(encoding="utf-8", errors="replace"),
        len(article_md_paths),
    )
    valid = (
        bool(article_md_paths)
        and structure_audit.get("valid") is True
    )
    result_payload["valid"] = valid
    result_payload["read_markdown_aggregation"]["valid"] = valid
    result_payload["read_markdown_aggregation"]["structure_audit"] = structure_audit
    write_json(output_path, result_payload)
    receipt: dict[str, object] = {
        "status": "completed" if valid else "blocked_read_md_structure_invalid",
        "run_executed": False,
        "mode": mode,
        "expected_output_path": str(output_path),
        "payload_source": mode,
        "result_payload": result_payload,
    }
    warnings: list[str] = []
    if warning_items:
        warnings.append(f"{len(warning_items)} 篇论文未进入最终 read.md；仅在任务日志和 read_results.json 中记录机器状态。")
    if valid is True:
        status = "passed_with_warnings" if warning_items else "passed"
    else:
        status = str(receipt.get("status") or "blocked_read_md_structure_invalid")
    if valid is not True:
        status = str(receipt.get("status") or status or "blocked_read_md_aggregation_failed")
        warnings.append("最终 read.md 拼接后未通过结构验收；已保留当前拼接产物，结构问题只记录到任务日志和 read_results.json。")
    return {
        "status": status,
        "valid": valid,
        "mode": mode,
        "expected_output_path": str(output_path),
        "article_markdown_paths": [str(path) for path in article_md_paths],
        "complete_article_markdown_count": len(article_md_paths),
        "expected_article_markdown_count": len(items),
        "warning_items": warning_items,
        "warnings": warnings,
        "structure_audit": structure_audit,
        "receipt": receipt,
        "result": result_payload,
        "policy": "Final read.md is deterministic per-paper Markdown concatenation only. Python only assembles completed subagent-written article Markdown and does not synthesize scientific summaries.",
    }


_COOLDOWN_REQUEUE_REASONS = {
    "http_429_rate_limited",
    "openreview_service_cooldown_active",
    "service_cooldown_active",
    "skipped_due_to_active_challenge_cooldown",
    "skipped_due_to_openreview_service_cooldown",
    "skipped_due_to_prior_cloudflare_challenge",
    "skipped_due_to_prior_service_access_blocker",
}


def _nested_route_items(value: object):
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _nested_route_items(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _nested_route_items(nested)


def _cooldown_requeue_services(item: dict) -> list[str]:
    validation = item.get("validation") if isinstance(item.get("validation"), dict) else {}
    if validation.get("full_text_ready") is True:
        return []
    packet = item.get("full_text_packet") if isinstance(item.get("full_text_packet"), dict) else {}
    blocker = packet.get("blocked_full_text_reason") if isinstance(packet.get("blocked_full_text_reason"), dict) else {}
    retryable = blocker.get("retryable_after_cooldown") is True
    services = {
        str(service).strip()
        for service in blocker.get("cooldown_services", [])
        if str(service).strip()
    } if isinstance(blocker.get("cooldown_services"), list) else set()
    for route in _nested_route_items(packet):
        reasons = {
            str(route.get(key) or "").strip()
            for key in ("reason", "download_failure_reason")
            if str(route.get(key) or "").strip()
        }
        route_retryable = bool(
            reasons & _COOLDOWN_REQUEUE_REASONS
            or int(route.get("status_code") or 0) == 429
            or route.get("challenge_type") == "cloudflare"
        )
        if not route_retryable:
            continue
        retryable = True
        service = str(route.get("service") or "").strip()
        url = str(route.get("url") or route.get("source_url") or route.get("pdf_url") or "").strip()
        if not service and url.startswith("openreview://"):
            service = "openreview"
        if not service and url:
            service = service_from_url(url)
        if service:
            services.add(service)
    if retryable and not services:
        paper = item.get("paper") if isinstance(item.get("paper"), dict) else {}
        for key in ("pdf_url", "url", "abs_url", "html_url"):
            url = str(paper.get(key) or "").strip()
            if not url:
                continue
            services.add("openreview" if url.startswith("openreview://") else service_from_url(url))
            break
    return sorted(services) if retryable else []


def _wait_for_cooldown_requeue(
    services: set[str],
    *,
    log: LogFn,
    should_cancel: CancelFn,
) -> float:
    started = time.monotonic()
    announced = False
    while services:
        _raise_if_cancelled(should_cancel)
        remaining_by_service = {
            service: service_cooldown_remaining(service)
            for service in sorted(services)
        }
        longest = max(remaining_by_service.values(), default=0.0)
        if longest <= 0:
            break
        if not announced:
            summary = ", ".join(f"{service}={remaining:.1f}s" for service, remaining in remaining_by_service.items())
            log(f"Cooldown recovery wait before batch requeue: {summary}")
            announced = True
        time.sleep(min(1.0, max(0.05, longest)))
    return round(time.monotonic() - started, 3)


def run_read(
    *,
    run_id: str,
    input_json: str,
    claude_mode: str = "prepare",
    timeout_sec: int = 1800,
    max_papers: int = 0,
    max_workers: int = 1,
    force_deep_read: bool = False,
    log: LogFn = print,
    should_cancel: CancelFn = lambda: False,
) -> dict:
    source_path, input_payload = _load_local_input_json(input_json)
    source_run_id = _input_run_id(source_path)
    try:
        validate_run_id(source_run_id)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    requested_run_id = str(run_id or "").strip()
    if requested_run_id and requested_run_id != source_run_id:
        raise SystemExit(
            "Reading input and output run must match: "
            f"--input-json is under .runtime/output/{source_run_id}/input but --run-id resolves to {requested_run_id}."
        )
    all_input_papers, papers, selected_limit = _select_ranked_input_articles(input_payload, max_papers)
    if not papers:
        raise SystemExit("输入 JSON 中没有 ranked_articles/ranked_papers/articles/input_articles/papers。")
    research_context = _research_context_from_input(input_payload)
    directory = existing_run_dir(source_run_id)
    input_dir = directory / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    write_json(input_dir / "input.json", input_payload)
    write_json(input_dir / "input_receipt.json", {
        "source_input": str(source_path),
        "local_snapshot": str(input_dir / "input.json"),
        "available_ranked_article_count": len(all_input_papers),
        "input_article_count": len(papers),
        "selected_max_papers": selected_limit,
        "policy": "Input must already be inside this directory; Reading consumes the first N papers in caller-provided final ranking order and does not copy from external paths.",
    })
    worker_count = max(1, min(len(papers), int(max_workers or 1)))
    prepared_by_index: dict[int, dict] = {}
    log(f"Full-text acquisition phase: {len(papers)} papers, {worker_count} workers")
    if worker_count <= 1:
        for index, row in enumerate(papers, 1):
            _raise_if_cancelled(should_cancel)
            title = row.get('title') or row.get('url') or row.get('pdf_url') or row.get('doi')
            log(f"Acquiring full text {index}/{len(papers)}: {title}")
            try:
                result = _prepare_local_read_paper(
                    run_id=directory.name,
                    directory=directory,
                    index=index,
                    row=row,
                    log=log,
                    force_deep_read=force_deep_read,
                )
            except ReadingCancelled:
                raise
            except Exception as exc:
                log(f"Error: full-text acquisition failed {index}/{len(papers)}: {exc.__class__.__name__}: {str(exc)[:300]} - {title}")
                result = _failed_prepare_item(
                    run_id=directory.name,
                    directory=directory,
                    index=index,
                    row=row,
                    exc=exc,
                )
            prepared_by_index[index] = result
            log(
                f"Finished full-text acquisition {index}/{len(papers)}: "
                f"{result.get('status')} / full_text={result.get('validation', {}).get('full_text_ready')} "
                f"- {title}"
            )
    else:
        with futures.ThreadPoolExecutor(max_workers=worker_count) as pool:
            pending: dict[futures.Future[dict], tuple[int, dict]] = {}
            for index, row in enumerate(papers, 1):
                _raise_if_cancelled(should_cancel)
                future = pool.submit(
                    _prepare_local_read_paper,
                    run_id=directory.name,
                    directory=directory,
                    index=index,
                    row=row,
                    log=log,
                    force_deep_read=force_deep_read,
                )
                pending[future] = (index, row)
            for future in futures.as_completed(pending):
                index, row = pending[future]
                _raise_if_cancelled(should_cancel)
                try:
                    result = future.result()
                except ReadingCancelled:
                    raise
                except Exception as exc:
                    log(f"Error: full-text acquisition failed {index}/{len(papers)}: {exc.__class__.__name__}: {str(exc)[:300]} - {row.get('title') or row.get('url') or row.get('pdf_url') or row.get('doi')}")
                    result = _failed_prepare_item(
                        run_id=directory.name,
                        directory=directory,
                        index=index,
                        row=row,
                        exc=exc,
                    )
                prepared_by_index[index] = result
                log(
                    f"Finished full-text acquisition {index}/{len(papers)}: "
                    f"{result.get('status')} / full_text={result.get('validation', {}).get('full_text_ready')} "
                    f"- {row.get('title') or row.get('url') or row.get('pdf_url') or row.get('doi')}"
                )
    cooldown_requeue_by_index = {
        index: _cooldown_requeue_services(prepared_by_index[index])
        for index in range(1, len(papers) + 1)
    }
    cooldown_requeue_by_index = {
        index: services
        for index, services in cooldown_requeue_by_index.items()
        if services
    }
    cooldown_requeue_summary = {
        "status": "not_needed",
        "attempted_paper_count": 0,
        "recovered_full_text_count": 0,
        "worker_count": 0,
        "services": [],
        "waited_sec": 0.0,
    }
    if cooldown_requeue_by_index:
        retry_services = {
            service
            for services in cooldown_requeue_by_index.values()
            for service in services
        }
        log(
            "Cooldown recovery phase: "
            f"requeueing {len(cooldown_requeue_by_index)} papers with 1 recovery worker"
        )
        recovered_count = 0
        waited_sec = 0.0
        for index, services in cooldown_requeue_by_index.items():
            _raise_if_cancelled(should_cancel)
            # A prior recovery request may have opened a fresh shared cooldown.
            # Wait again before each queued paper so its one retry is a real request,
            # not another immediate cooldown skip caused by the preceding paper.
            waited_sec += _wait_for_cooldown_requeue(
                set(services),
                log=log,
                should_cancel=should_cancel,
            )
            row = papers[index - 1]
            initial = prepared_by_index[index]
            initial_packet = initial.get("full_text_packet") if isinstance(initial.get("full_text_packet"), dict) else {}
            initial_blocker = initial_packet.get("blocked_full_text_reason") if isinstance(initial_packet.get("blocked_full_text_reason"), dict) else {}
            try:
                result = _prepare_local_read_paper(
                    run_id=directory.name,
                    directory=directory,
                    index=index,
                    row=row,
                    log=log,
                    force_deep_read=force_deep_read,
                )
            except ReadingCancelled:
                raise
            except Exception as exc:
                log(f"Error: cooldown recovery failed {index}/{len(papers)}: {exc.__class__.__name__}: {str(exc)[:300]}")
                result = _failed_prepare_item(
                    run_id=directory.name,
                    directory=directory,
                    index=index,
                    row=row,
                    exc=exc,
                )
            result_validation = result.get("validation") if isinstance(result.get("validation"), dict) else {}
            result_validation["cooldown_requeue"] = {
                "attempted": True,
                "attempt": 1,
                "services": services,
                "initial_status": str(initial.get("status") or ""),
                "initial_blocked_reason_code": str(initial_blocker.get("code") or ""),
            }
            result["validation"] = result_validation
            artifacts = result.get("artifacts") if isinstance(result.get("artifacts"), dict) else {}
            read_results_path = str(artifacts.get("read_results") or "").strip()
            if read_results_path:
                _write_read_result(resolve_reading_path(read_results_path), result)
            prepared_by_index[index] = result
            if result_validation.get("full_text_ready") is True:
                recovered_count += 1
            log(
                f"Finished cooldown recovery {index}/{len(papers)}: "
                f"{result.get('status')} / full_text={result_validation.get('full_text_ready')}"
            )
        cooldown_requeue_summary = {
            "status": "complete",
            "attempted_paper_count": len(cooldown_requeue_by_index),
            "recovered_full_text_count": recovered_count,
            "worker_count": 1,
            "services": sorted(retry_services),
            "waited_sec": round(waited_sec, 3),
        }
    prepared_items = [prepared_by_index[index] for index in range(1, len(papers) + 1)]
    read_candidates = [
        item for item in prepared_items
        if item.get("validation", {}).get("full_text_ready") is True
        and item.get("validation", {}).get("deep_read_complete") is not True
    ]
    items_by_index: dict[int, dict] = {
        int(item.get("paper_index") or index): item
        for index, item in enumerate(prepared_items, 1)
    }
    reading_worker_count = max(1, min(len(read_candidates), worker_count)) if read_candidates else 0
    if read_candidates:
        log(f"Reading subagent phase: {len(read_candidates)} papers, {reading_worker_count} workers")
    if read_candidates and reading_worker_count <= 1:
        for item in read_candidates:
            _raise_if_cancelled(should_cancel)
            index = int(item.get("paper_index") or 0)
            paper = item.get("paper") if isinstance(item.get("paper"), dict) else {}
            title = paper.get('title') or paper.get('url') or paper.get('pdf_url') or paper.get('doi')
            log(f"Starting reading subagent {index}/{len(papers)}: {title}")
            try:
                result = _run_reading_subagent_for_prepared_paper(
                    prepared=item,
                    claude_mode=claude_mode,
                    timeout_sec=timeout_sec,
                    log=log,
                )
            except ReadingCancelled:
                raise
            except Exception as exc:
                log(f"Error: reading subagent failed {index}/{len(papers)}: {exc.__class__.__name__}: {str(exc)[:300]} - {title}")
                result = _failed_reading_subagent_item(item, exc)
            items_by_index[index] = result
            log(
                f"Finished reading subagent {index}/{len(papers)}: "
                f"{result.get('status')} / deep_read={result.get('validation', {}).get('deep_read_complete')} "
                f"- {title}"
            )
    elif read_candidates:
        with futures.ThreadPoolExecutor(max_workers=reading_worker_count) as pool:
            pending_read: dict[futures.Future[dict], dict] = {}
            for item in read_candidates:
                _raise_if_cancelled(should_cancel)
                future = pool.submit(
                    _run_reading_subagent_for_prepared_paper,
                    prepared=item,
                    claude_mode=claude_mode,
                    timeout_sec=timeout_sec,
                    log=log,
                )
                pending_read[future] = item
            for future in futures.as_completed(pending_read):
                _raise_if_cancelled(should_cancel)
                source_item = pending_read[future]
                source_paper = source_item.get("paper") if isinstance(source_item.get("paper"), dict) else {}
                title = source_paper.get('title') or source_paper.get('url') or source_paper.get('pdf_url') or source_paper.get('doi')
                try:
                    result = future.result()
                except ReadingCancelled:
                    raise
                except Exception as exc:
                    source_index = int(source_item.get("paper_index") or 0)
                    log(f"Error: reading subagent failed {source_index}/{len(papers)}: {exc.__class__.__name__}: {str(exc)[:300]} - {title}")
                    result = _failed_reading_subagent_item(source_item, exc)
                index = int(result.get("paper_index") or source_item.get("paper_index") or 0)
                items_by_index[index] = result
                log(
                    f"Finished reading subagent {index}/{len(papers)}: "
                    f"{result.get('status')} / deep_read={result.get('validation', {}).get('deep_read_complete')} "
                    f"- {title}"
                )
    items = [items_by_index[index] for index in range(1, len(papers) + 1)]
    complete_count = sum(1 for item in items if item.get("validation", {}).get("deep_read_complete"))
    ready_count = sum(1 for item in items if item.get("validation", {}).get("full_text_ready"))
    _raise_if_cancelled(should_cancel)
    items, reading_scoring = _run_final_reading_scoring(
        directory=directory,
        items=items,
        research_context=research_context,
        claude_mode=claude_mode,
        timeout_sec=timeout_sec,
        log=log,
    )
    scoring_warning = bool(
        claude_mode != "prepare"
        and complete_count > 0
        and reading_scoring.get("status") != "complete"
    )
    status = "complete" if complete_count == len(items) and not scoring_warning else "complete_with_warnings"
    warning_count = max(0, len(items) - complete_count) + int(scoring_warning)
    pending_full_text_count = max(0, len(items) - ready_count)
    pending_deep_read_count = max(0, ready_count - complete_count)
    warning_items = [
        _run_warning_detail(item, index)
        for index, item in enumerate(items, 1)
        if item.get("validation", {}).get("deep_read_complete") is not True
    ]
    content_quality_failures = [
        str(item.get("validation", {}).get("content_quality_issue") or "")
        for item in items
        if str(item.get("validation", {}).get("content_quality_issue") or "")
    ]
    content_quality_issues = list(dict.fromkeys(content_quality_failures))
    if scoring_warning:
        warning_items.append({
            "index": 0,
            "title": "final Reading scoring",
            "phase": "final_scoring",
            "status": str(reading_scoring.get("status") or "complete_with_warnings"),
            "message": "统一评分未覆盖全部已完成精读；未伪造缺失分数，最终产物保留原输入排名。",
            "expected_article_count": int(reading_scoring.get("expected_article_count") or 0),
            "scored_article_count": int(reading_scoring.get("scored_article_count") or 0),
        })
    error_items = [
        detail
        for detail in warning_items
        if detail.get("error_type") or str(detail.get("status") or "").startswith("error_")
    ]
    if warning_count:
        log(
            "Warning: Reading will continue with a clean read.md built from completed papers only; "
            f"full_text_ready={ready_count}/{len(items)}, deep_read_complete={complete_count}/{len(items)}, "
            f"pending_full_text={pending_full_text_count}, pending_deep_read={pending_deep_read_count}."
        )
    full_text_entries: list[dict] = [
        item["full_text_packet"]
        for item in items
        if isinstance(item.get("full_text_packet"), dict)
    ]
    full_text_dir = directory / "full_text_reading"
    write_json(full_text_dir / "full_text_packet.json", {
        "run_id": directory.name,
        "generated_at": _now_iso(),
        "papers": full_text_entries,
        "policy": "Read processes exactly the local input articles; same-paper PDF/HTML/XML full-text fallback is allowed, article replacement is forbidden.",
    })
    payload = {
        "run_id": directory.name,
        "status": status,
        "generated_at": _now_iso(),
        "input_json": str(source_path),
        "public_final_artifact": str(directory / "read.md"),
        "machine_support_artifacts": [
            str(directory / "read_results.json"),
            str(full_text_dir / "full_text_packet.json"),
            *([str(reading_scoring.get("score_artifact"))] if reading_scoring.get("score_artifact") else []),
        ],
        "available_ranked_article_count": len(all_input_papers),
        "selected_max_papers": selected_limit,
        "input_article_count": len(items),
        "full_text_ready_count": ready_count,
        "deep_read_complete_count": complete_count,
        "blocked_count": pending_full_text_count,
        "warning_count": warning_count,
        "warning_items": warning_items,
        "error_items": error_items,
        "pending_full_text_count": pending_full_text_count,
        "pending_deep_read_count": pending_deep_read_count,
        "content_quality_issue_count": len(content_quality_failures),
        "content_quality_issues": content_quality_issues,
        "continuation_policy": "Read emits final read.md from completed per-paper Markdown whenever possible. Incomplete papers and concatenation issues are recorded only as task-log/machine-state warnings.",
        "worker_count": worker_count,
        "cooldown_requeue": cooldown_requeue_summary,
        "reading_subagent_worker_count": reading_worker_count,
        "reading_scoring": reading_scoring,
        "execution_phases": [
            "full_text_acquisition_for_all_inputs",
            *(["cooldown_expiry_batch_requeue"] if cooldown_requeue_summary["attempted_paper_count"] else []),
            "parallel_reading_subagents_after_full_text_collection",
            *(["claude_scoring_over_all_completed_reading_artifacts"] if reading_scoring.get("attempted") is True else []),
            "final_read_md_deterministic_concatenation",
        ],
        "strict_input_contract": True,
        "replacement_policy": "forbidden",
        "items": [_machine_read_result(item) for item in items],
    }
    if claude_mode == "prepare":
        status = "prepared_all_full_text_pending_claude" if ready_count == len(items) else "prepared_with_full_text_warnings"
        payload.update({
            "status": status,
            "read_markdown_aggregation": {
                "status": "not_started_prepare_mode",
                "valid": None,
                "mode": "prepare",
                "article_markdown_paths": [],
                "complete_article_markdown_count": 0,
                "expected_article_markdown_count": len(items),
                "policy": "Final read.md is assembled only from completed per-paper subagent Markdown; no final method summary table is generated.",
            },
            "public_final_artifact_present": False,
        })
        try:
            (directory / "read.md").unlink(missing_ok=True)
        except OSError:
            pass
        write_json(directory / "read_results.json", payload)
        scrub_reading_paths_under(directory)
        latest_run = refresh_latest_run(directory)
        return make_reading_paths_relative({
            "status": status,
            "run_id": directory.name,
            "run_dir": str(directory),
            "latest_run": str(latest_run),
            "available_ranked_article_count": len(all_input_papers),
            "selected_max_papers": selected_limit,
            "input_article_count": len(items),
            "full_text_ready_count": ready_count,
            "deep_read_complete_count": complete_count,
            "worker_count": worker_count,
            "cooldown_requeue": cooldown_requeue_summary,
            "read_md": str(directory / "read.md"),
            "public_final_artifact_present": False,
            "latest_public_final_artifact_present": False,
            "read_results": str(directory / "read_results.json"),
            "reading_scoring": reading_scoring,
            "read_markdown_aggregation": payload.get("read_markdown_aggregation"),
        })
    read_md_path = directory / "read.md"
    log(f"Final read.md phase: assembling {len(items)} papers into read.md from per-paper Markdown")
    try:
        read_md_aggregation = _aggregate_read_md_from_article_markdown(
            run_path=directory,
            items=items,
            read_md_path=read_md_path,
        )
    except Exception as exc:
        log(f"Error: Final read.md concatenation failed: {exc.__class__.__name__}: {str(exc)[:300]}")
        article_md_paths = [
            path
            for item in items
            for path in [_article_markdown_path_for_completed_item(item)]
            if path is not None
        ]
        read_md_aggregation = {
            "status": "error_read_md_aggregation_recorded",
            "valid": False,
            "mode": "deterministic_concat_failed",
            "article_markdown_paths": [str(path) for path in article_md_paths],
            "complete_article_markdown_count": len(article_md_paths),
            "expected_article_markdown_count": len(items),
            "warning_items": warning_items,
            "warnings": ["最终 read.md 拼接阶段发生错误；错误只记录在日志和 read_results.json。"],
            "error": _read_exception_payload("read_md_aggregation", exc),
            "policy": "Final read.md is assembled from completed per-paper Markdown. Python does not synthesize per-paper reading text.",
        }
        error_items.append({
            "index": 0,
            "title": "final read.md concatenation",
            "phase": "read_md_aggregation",
            "status": "error_read_md_aggregation_recorded",
            "message": "最终 read.md 拼接阶段发生错误",
            "full_text_ready": False,
            "deep_read_complete": False,
            "full_text_status": "",
            "error_type": exc.__class__.__name__,
            "error_message": str(exc)[:1000],
        })
        payload["error_items"] = error_items
        payload["warning_count"] = max(int(payload.get("warning_count") or 0), len(warning_items) + 1)
        payload["error_count"] = len(error_items)
        payload["status"] = "complete_with_warnings"
        status = "complete_with_warnings"
    payload["read_markdown_aggregation"] = read_md_aggregation
    log(f"Final read.md assembly complete: valid={read_md_aggregation.get('valid')}")
    for warning in read_md_aggregation.get("warnings") or []:
        log(f"Warning: {warning}")
    if read_md_aggregation.get("valid") is not True:
        payload["status"] = "complete_with_warnings"
        status = payload["status"]
        log("Warning: Final read.md assembly returned warnings; continuing without blocking downstream workflow.")
    else:
        payload["status"] = status
    payload["public_final_artifact_present"] = bool(
        read_md_path.exists()
        and read_md_path.read_text(encoding="utf-8", errors="replace").strip()
    )
    write_json(directory / "read_results.json", payload)
    scrub_reading_paths_under(directory)
    latest_run = refresh_latest_run(directory)
    latest_read_md = Path(str(latest_run)) / "read.md"
    latest_public_final_artifact_present = bool(latest_read_md.exists() and latest_read_md.read_text(encoding="utf-8", errors="replace").strip())
    if payload["public_final_artifact_present"] and not latest_public_final_artifact_present:
        raise RuntimeError(f"latest_run did not receive final read.md: {latest_read_md}")
    return make_reading_paths_relative({
        "status": status,
        "run_id": directory.name,
        "run_dir": str(directory),
        "latest_run": str(latest_run),
        "available_ranked_article_count": len(all_input_papers),
        "selected_max_papers": selected_limit,
        "input_article_count": len(items),
        "full_text_ready_count": ready_count,
        "deep_read_complete_count": complete_count,
        "worker_count": worker_count,
        "read_md": str(directory / "read.md"),
        "public_final_artifact_present": payload["public_final_artifact_present"],
        "latest_public_final_artifact_present": latest_public_final_artifact_present,
        "read_results": str(directory / "read_results.json"),
        "reading_scoring": reading_scoring,
        "read_markdown_aggregation": payload.get("read_markdown_aggregation"),
    })


def _standalone_run_id(article: str, title: str = "", explicit: str = "") -> str:
    if explicit:
        return validate_run_id(explicit)
    return create_run_dir().name


def _load_standalone_input_json(path: str) -> tuple[Path | None, dict]:
    if not path:
        return None, {}
    try:
        source = ensure_inside_input(Path(path), label="standalone 输入 JSON")
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    payload = read_json(source, {})
    if not isinstance(payload, dict):
        raise SystemExit(f"输入 JSON 不是对象：{source}")
    return source, payload


def _load_standalone_claude_result(result_path: Path, claude_receipt: dict) -> dict:
    file_payload = read_json(result_path, {})
    if isinstance(file_payload, dict) and file_payload:
        return _strip_reading_content_payload(file_payload)
    payload = claude_receipt.get("result_payload") if isinstance(claude_receipt, dict) else {}
    return _strip_reading_content_payload(payload) if isinstance(payload, dict) else {}


def _standalone_final_status(packet: dict, claude_receipt: dict, claude_result: dict) -> str:
    if int(packet.get("full_text_chars") or packet.get("text_chars") or 0) < FULL_TEXT_MIN_CHARS:
        return "blocked_full_text_unavailable"
    if _deep_read_complete(claude_receipt, claude_result):
        return "complete"
    status = str(claude_receipt.get("status") or "").strip()
    if status.startswith(("prepared_", "blocked_")):
        return status
    if claude_receipt.get("run_executed"):
        return "blocked_claude_result_missing_or_invalid"
    return "prepared_for_claude_subagent"


def run_standalone_deep_read(args: object) -> dict:
    from acquisition.paper_sources import acquire_full_text

    input_source, input_payload = _load_standalone_input_json(args.input_json)
    input_rows = _input_articles(input_payload)
    if len(input_rows) > 1:
        raise SystemExit("单篇 deep-read 的 --input-json 最多只能包含一篇论文；多篇论文请使用 read action。")
    input_paper = dict(input_rows[0]) if input_rows else dict(input_payload)
    article = args.article or str(input_paper.get("article") or input_paper.get("url") or input_paper.get("pdf_url") or "")
    title = args.title or str(input_paper.get("title") or "")
    if not article and not title:
        raise SystemExit("必须提供 --title、--article，或在 --input-json 中提供 title/article/url/pdf_url。")
    article = article or title
    explicit_run_id = args.run_id or ("" if input_source is not None else str(input_payload.get("run_id") or ""))
    if input_source is not None and not explicit_run_id:
        explicit_run_id = _input_run_id(input_source)
    if input_source is not None:
        source_run_id = _input_run_id(input_source)
        try:
            validate_run_id(source_run_id)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        if explicit_run_id and explicit_run_id != source_run_id:
            raise SystemExit(
                "Standalone Reading input and output run must match: "
                f"--input-json is under .runtime/output/{source_run_id}/input but output run is {explicit_run_id}."
            )
        current_run_dir = existing_run_dir(source_run_id)
    elif explicit_run_id:
        try:
            current_run_dir = run_dir(validate_run_id(explicit_run_id))
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    else:
        current_run_dir = create_run_dir()
    if input_source is not None and current_run_dir.name != _input_run_id(input_source):
        raise SystemExit(
            "Standalone Reading input and output run must match: "
            f"--input-json is under .runtime/output/{_input_run_id(input_source)}/input but output run is {current_run_dir.name}."
        )
    if args.input_json:
        source_input = input_source
        local_source = current_run_dir / "input" / "source_input.json"
        local_source.parent.mkdir(parents=True, exist_ok=True)
        if source_input and source_input.exists() and source_input.resolve(strict=False) != local_source.resolve(strict=False):
            shutil.copy2(source_input, local_source)
    write_json(current_run_dir / "input.json", {"cli": vars(args), "input_json": input_payload})

    paper_input = dict(input_paper)
    overrides = {
        "article": article,
        "title": title,
        "authors": args.authors,
        "abstract": args.abstract,
        "paper_id": args.paper_id,
        "pdf_url": args.pdf_url,
        "url": args.url,
        "source": args.source,
    }
    for key, value in overrides.items():
        if value not in (None, "", []):
            paper_input[key] = value
    paper_input.setdefault("source", "standalone_input")
    paper = _normalize_local_input_paper(paper_input)
    write_json(current_run_dir / "paper.json", paper)
    force_deep_read = bool(getattr(args, "force", False))
    cached_read = {} if force_deep_read else _restore_article_read_cache(
        current_run_dir,
        paper,
        run_id=current_run_dir.name,
        paper_index=1,
    )
    if cached_read:
        cached_read.update({
            "source": "Reading standalone deep_read",
            "run_dir": str(current_run_dir),
            "public_final_artifact": str(current_run_dir / "read.md"),
            "machine_support_artifacts": [str(current_run_dir / "read_results.json")],
            "public_final_artifact_present": True,
        })
        _write_read_result(current_run_dir / "read_results.json", cached_read)
        scrub_reading_paths_under(current_run_dir)
        latest_run = refresh_latest_run(current_run_dir)
        cached_read["latest_run"] = str(latest_run)
        cached_read["latest_public_final_artifact_present"] = (latest_run / "read.md").is_file()
        return cached_read
    packet_entry = _restore_article_full_text_cache(paper, current_run_dir)
    if not packet_entry:
        packet_entry = acquire_full_text(paper, current_run_dir, services=_reading_acquisition_services())
        _verify_packet_full_text_title(paper, packet_entry)
        _publish_article_full_text_cache(current_run_dir, paper, packet_entry)
    else:
        _verify_packet_full_text_title(paper, packet_entry)
    write_json(current_run_dir / "paper.json", paper)
    write_json(current_run_dir / "full_text_packet.json", {
        "run_id": current_run_dir.name,
        "source": "Reading standalone deep_read",
        "papers": [packet_entry],
        "policy": "独立精读流水线只在 .runtime/output 下保存下载、抽取、提示和结果。",
    })

    output_path = current_run_dir / "outputs" / "reading_result.json"
    prompt_path = current_run_dir / "prompts" / "deep_read_prompt.md"
    read_md_path = current_run_dir / "read.md"
    prompt = build_deep_read_prompt(
        paper=paper,
        packet=packet_entry,
        run_path=current_run_dir,
        output_path=output_path,
        article_md_path=read_md_path,
    )
    write_text(prompt_path, prompt)
    claude_mode = args.claude_mode
    if int(packet_entry.get("full_text_chars") or packet_entry.get("text_chars") or 0) < FULL_TEXT_MIN_CHARS and claude_mode == "auto":
        claude_mode = "prepare"
    if claude_mode != "prepare":
        try:
            read_md_path.unlink(missing_ok=True)
        except OSError:
            pass
    claude_receipt = run_claude_deep_read(
        prompt_path=prompt_path,
        run_path=current_run_dir,
        expected_output_path=output_path,
        timeout_sec=args.timeout_sec,
        mode=claude_mode,
    )
    claude_result = _load_standalone_claude_result(output_path, claude_receipt)
    quality = _run_content_quality_repair_once(
        paper=paper,
        packet=packet_entry,
        item_dir=current_run_dir,
        article_md_path=read_md_path,
        output_path=output_path,
        receipt=claude_receipt,
        result_payload=claude_result,
        mode=claude_mode,
        timeout_sec=args.timeout_sec,
        log=lambda _message: None,
        index=1,
    )
    claude_receipt = quality["receipt"]
    claude_result = quality["result_payload"]
    quality_issue = str(quality["quality_issue"] or "")
    status = _standalone_final_status(packet_entry, claude_receipt, claude_result)
    article_markdown_ready = _article_markdown_ready(read_md_path, claude_result, paper)
    if quality_issue:
        status = quality_issue
    if status == "complete" and not article_markdown_ready:
        status = "blocked_article_markdown_missing_for_standalone_deep_read"
    result_payload = {
        "run_id": current_run_dir.name,
        "status": status,
        "source": "Reading standalone deep_read",
        "generated_at": _now_iso(),
        "run_dir": str(current_run_dir),
        "paper": paper,
        "full_text_packet": packet_entry,
        "claude": claude_receipt,
        "claude_result": claude_result,
        "validation": {
            "content_quality_issue": quality_issue,
            "content_quality_reason": str(quality["quality_reason"] or ""),
            "content_quality_retry": quality["quality_retry"],
        },
        "public_final_artifact": str(current_run_dir / "read.md"),
        "machine_support_artifacts": [str(current_run_dir / "read_results.json")],
    }
    if status != "complete" or not article_markdown_ready:
        try:
            read_md_path.unlink(missing_ok=True)
        except OSError:
            pass
    result_payload["public_final_artifact_present"] = bool(read_md_path.exists() and read_md_path.read_text(encoding="utf-8", errors="replace").strip())
    if result_payload["public_final_artifact_present"]:
        _publish_article_read_cache(current_run_dir, paper, result_payload)
    result_payload = _machine_read_result(result_payload)
    write_json(current_run_dir / "read_results.json", result_payload)
    scrub_reading_paths_under(current_run_dir)
    latest_run = refresh_latest_run(current_run_dir)
    latest_read_md = Path(str(latest_run)) / "read.md"
    latest_public_final_artifact_present = bool(latest_read_md.exists() and latest_read_md.read_text(encoding="utf-8", errors="replace").strip())
    if result_payload["public_final_artifact_present"] and not latest_public_final_artifact_present:
        raise RuntimeError(f"latest_run did not receive final read.md: {latest_read_md}")
    result_payload["latest_run"] = str(latest_run)
    result_payload["latest_public_final_artifact_present"] = latest_public_final_artifact_present
    return result_payload
