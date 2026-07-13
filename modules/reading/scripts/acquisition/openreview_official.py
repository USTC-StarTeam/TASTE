from __future__ import annotations

import importlib
import importlib.util
import hashlib
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any

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

from core.common import ServiceCooldownActive, config_bool, config_float, env_bool, service_request_slot


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)) or default)
    except Exception:
        return default


OPENREVIEW_CLIENT_CALL_TIMEOUT_SEC = _env_float(
    "READING_OPENREVIEW_CLIENT_CALL_TIMEOUT_SEC",
    config_float("http.openreview_client_call_timeout_sec", 15.0),
)

_CLIENT_CACHE_LOCK = threading.Lock()
_CLIENT_CACHE: dict[tuple[int, str, str, str, int], tuple[Any, dict[str, Any]]] = {}


def _call_client_method(method: Any, *args: Any, **kwargs: Any) -> Any:
    return method(*args, **kwargs)


def _configure_client_timeout(client: Any) -> None:
    session = getattr(client, "session", None)
    request = getattr(session, "request", None)
    if session is None or not callable(request):
        return
    timeout = max(5.0, float(OPENREVIEW_CLIENT_CALL_TIMEOUT_SEC or 15.0))

    def request_with_timeout(method: str, url: str, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", timeout)
        return request(method, url, **kwargs)

    session.request = request_with_timeout


def _guarded_openreview_call(call: Any) -> Any:
    with service_request_slot("openreview") as gate:
        try:
            return call()
        except BaseException as exc:
            message = str(exc)
            if "403" in message or "Forbidden" in message:
                gate.update({"status_code": 403, "cooldown_reason": "openreview_client_forbidden"})
            elif "429" in message or "Too Many Requests" in message:
                gate.update({"status_code": 429, "cooldown_reason": "openreview_client_rate_limited"})
            raise


def _openreview_ids_from_paper(paper: dict[str, Any]) -> list[str]:
    values: list[Any] = []
    metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    for key in [
        "openreview_id",
        "openreview_forum",
        "openreview_forum_url",
        "openreview_url",
        "openreview_pdf_url",
        "forum",
        "note_id",
        "paper_url",
        "paper_pdf_url",
        "url",
        "html_url",
        "abs_url",
        "pdf_url",
    ]:
        values.append(paper.get(key))
        values.append(metadata.get(key))
    out: list[str] = []
    for value in values:
        text = str(value or "")
        for pattern in [
            r"openreview\.net/(?:forum|pdf)\?id=([^&#\s]+)",
            r"openreview\.net/attachment\?id=([^&#\s]+)",
            r"\bopenreview:([A-Za-z0-9_-]{6,})\b",
        ]:
            for match in re.finditer(pattern, text):
                note_id = match.group(1).strip()
                if note_id and note_id not in out:
                    out.append(note_id)
        direct = text.strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{8,}", direct) and direct not in out:
            out.append(direct)
    return out


def _credential_status() -> dict[str, Any]:
    username = str(os.environ.get("OPENREVIEW_USERNAME") or os.environ.get("OPENREVIEW_EMAIL") or "").strip()
    password = str(os.environ.get("OPENREVIEW_PASSWORD") or "").strip()
    if not username or not password:
        return {
            "accepted": False,
            "reason": "missing_openreview_credentials",
            "message_zh": "未设置 OPENREVIEW_USERNAME/OPENREVIEW_PASSWORD；若已安装 openreview-py，系统会以匿名 official client 低频尝试，并记录 403/challenge。",
        }
    return {"accepted": True, "username_set": True, "password_set": True}


def _anonymous_official_client_enabled() -> bool:
    return env_bool(
        "READING_OPENREVIEW_ALLOW_ANONYMOUS_OFFICIAL_CLIENT",
        config_bool("openreview.allow_anonymous_official_client", True),
    )


def _package_status() -> dict[str, Any]:
    if importlib.util.find_spec("openreview") is None:
        return {
            "accepted": False,
            "reason": "missing_openreview_py",
            "message_zh": "未安装官方 openreview-py，无法使用官方 client 下载 OpenReview PDF。",
            "install_hint": "python -m pip install openreview-py",
        }
    return {"accepted": True, "package": "openreview-py"}


def _official_access_status() -> dict[str, Any]:
    package_status = _package_status()
    credential_status = _credential_status()
    anonymous_enabled = _anonymous_official_client_enabled()
    missing_reasons = [
        str(item.get("reason"))
        for item in [package_status, credential_status]
        if isinstance(item, dict) and item.get("reason")
    ]
    return {
        "accepted": package_status.get("accepted") is True and (credential_status.get("accepted") is True or anonymous_enabled),
        "package_status": package_status,
        "credential_status": credential_status,
        "anonymous_official_client_enabled": anonymous_enabled,
        "missing_reasons": missing_reasons,
    }


def _client(api_version: int) -> tuple[Any, dict[str, Any]]:
    access_status = _official_access_status()
    package_status = access_status.get("package_status") if isinstance(access_status.get("package_status"), dict) else {}
    credential_status = access_status.get("credential_status") if isinstance(access_status.get("credential_status"), dict) else {}
    if package_status.get("accepted") is not True:
        missing_reasons = access_status.get("missing_reasons") if isinstance(access_status.get("missing_reasons"), list) else []
        primary_reason = str(missing_reasons[0]) if missing_reasons else "openreview_official_access_not_configured"
        return None, {
            "accepted": False,
            "api_version": api_version,
            "reason": primary_reason,
            **access_status,
            "message_zh": "OpenReview 官方 client 需要同时安装 openreview-py 并设置 OPENREVIEW_USERNAME/OPENREVIEW_PASSWORD。",
        }
    if credential_status.get("accepted") is not True and not access_status.get("anonymous_official_client_enabled"):
        return None, {
            "accepted": False,
            "api_version": api_version,
            "reason": str(credential_status.get("reason") or "missing_openreview_credentials"),
            **access_status,
            "message_zh": "当前显式禁用匿名 OpenReview official client；请设置 OPENREVIEW_USERNAME/OPENREVIEW_PASSWORD。",
        }
    try:
        import openreview  # type: ignore
    except Exception as exc:
        return None, {
            "accepted": False,
            "reason": "missing_openreview_py",
            "error": exc.__class__.__name__,
            **access_status,
            "message_zh": "未安装官方 openreview-py，无法使用官方 client 下载 OpenReview PDF。",
        }
    username = str(os.environ.get("OPENREVIEW_USERNAME") or os.environ.get("OPENREVIEW_EMAIL") or "").strip()
    password = str(os.environ.get("OPENREVIEW_PASSWORD") or "").strip()
    baseurl = "https://api2.openreview.net" if api_version == 2 else "https://api.openreview.net"
    auth_mode = "authenticated" if credential_status.get("accepted") is True else "anonymous"
    try:
        factory = openreview.api.OpenReviewClient if api_version == 2 else openreview.Client
        credential_fingerprint = hashlib.sha256(
            (username + "\0" + password).encode("utf-8", errors="replace")
        ).hexdigest() if auth_mode == "authenticated" else "anonymous"
        cache_key = (api_version, baseurl, auth_mode, credential_fingerprint, id(factory))
        with _CLIENT_CACHE_LOCK:
            cached = _CLIENT_CACHE.get(cache_key)
            if cached is not None:
                client, cached_receipt = cached
                return client, {**cached_receipt, "client_reused": True}
            client = factory(baseurl=baseurl, token="e30.e30.")
            client.token = None
            if isinstance(getattr(client, "headers", None), dict):
                client.headers.pop("Authorization", None)
            _configure_client_timeout(client)
            if auth_mode == "authenticated":
                _guarded_openreview_call(lambda: _call_client_method(client.login_user, username, password))
            receipt = {
                "accepted": True,
                "api_version": api_version,
                "baseurl": baseurl,
                "auth_mode": auth_mode,
                "client_reused": False,
                **access_status,
            }
            _CLIENT_CACHE[cache_key] = (client, receipt)
            return client, dict(receipt)
    except ServiceCooldownActive as exc:
        return None, {
            "accepted": False,
            "api_version": api_version,
            "baseurl": baseurl,
            "reason": "openreview_service_cooldown_active",
            "cooldown_remaining_sec": round(exc.remaining, 3),
            "cooldown_reason": exc.reason,
            "message_zh": "OpenReview 当前处于访问冷却期，本轮不再请求同站点。",
        }
    except Exception as exc:
        message = str(exc)[:500]
        reason = "openreview_official_client_forbidden" if ("403" in message or "Forbidden" in message) else "openreview_official_client_init_failed"
        return None, {
            "accepted": False,
            "api_version": api_version,
            "baseurl": baseurl,
            "reason": reason,
            "error": exc.__class__.__name__,
            "message": message,
            "message_zh": "官方 OpenReview client 初始化失败。",
        }


def _content_value(value: Any) -> Any:
    if isinstance(value, dict) and "value" in value:
        return value.get("value")
    return value


def _note_field(note: Any, field: str) -> Any:
    content = getattr(note, "content", None)
    if not isinstance(content, dict):
        return ""
    return _content_value(content.get(field))


def _note_title(note: Any) -> str:
    return str(_note_field(note, "title") or "").strip()


def _note_authors(note: Any) -> Any:
    return _note_field(note, "authors") or _note_field(note, "authorids") or []


def _note_id(note: Any) -> str:
    return str(getattr(note, "id", "") or getattr(note, "forum", "") or "").strip()


def _title_tokens(value: object) -> set[str]:
    stop = {"a", "an", "and", "for", "in", "of", "on", "the", "to", "towards", "toward", "with"}
    normalized = re.sub(r"[\u2010-\u2015]", "-", str(value or ""))
    return {token.lower() for token in re.findall(r"[A-Za-z0-9]+", normalized) if len(token) >= 2 and token.lower() not in stop}


def _title_similarity(left: object, right: object) -> float:
    left_tokens = _title_tokens(left)
    right_tokens = _title_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))


def _author_family_tokens(value: object) -> set[str]:
    values = value if isinstance(value, list) else re.split(r"[,;]", str(value or ""))
    out: set[str] = set()
    for name in values:
        parts = [part.lower() for part in re.findall(r"[A-Za-z][A-Za-z-]+", str(name or ""))]
        if parts:
            out.add(parts[-1])
    return out


def _same_title_author(paper: dict[str, Any], note: Any) -> bool:
    title = str(paper.get("title") or "").strip()
    note_title = _note_title(note)
    if not title or not note_title:
        return False
    similarity = _title_similarity(title, note_title)
    expected_authors = _author_family_tokens(paper.get("authors"))
    found_authors = _author_family_tokens(_note_authors(note))
    if expected_authors:
        overlap = expected_authors & found_authors
        return bool((similarity >= 0.82 and overlap) or (similarity >= 0.78 and len(overlap) >= 2))
    return similarity >= 0.92


def _notes_by_title(client: Any, title: str, limit: int) -> tuple[list[Any], dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    for method_name in ["get_all_notes", "get_notes"]:
        method = getattr(client, method_name, None)
        if method is None:
            continue
        for kwargs in [
            {"content": {"title": title}},
            {"content": {"title": {"value": title}}},
            {"select": "id,forum,content", "content": {"title": title}},
        ]:
            try:
                notes = _guarded_openreview_call(lambda: _call_client_method(method, **kwargs))
                if isinstance(notes, list):
                    attempts.append({"method": method_name, "kwargs": sorted(kwargs.keys()), "status": "ok", "note_count": len(notes)})
                    return notes[:limit], {"accepted": True, "attempts": attempts}
            except ServiceCooldownActive as exc:
                attempts.append({
                    "method": method_name,
                    "accepted": False,
                    "reason": "openreview_service_cooldown_active",
                    "cooldown_remaining_sec": round(exc.remaining, 3),
                })
                return [], {"accepted": False, "reason": "openreview_service_cooldown_active", "attempts": attempts}
            except TypeError as exc:
                attempts.append({"method": method_name, "kwargs": sorted(kwargs.keys()), "accepted": False, "error": exc.__class__.__name__, "message": str(exc)[:300]})
            except Exception as exc:
                attempts.append({"method": method_name, "kwargs": sorted(kwargs.keys()), "accepted": False, "error": exc.__class__.__name__, "message": str(exc)[:500]})
                if "403" in str(exc) or "Forbidden" in str(exc):
                    return [], {"accepted": False, "reason": "openreview_official_title_search_forbidden", "attempts": attempts}
    return [], {"accepted": False, "reason": "openreview_official_title_search_failed", "attempts": attempts}


def openreview_official_pdf_candidates(paper: dict[str, Any], *, limit: int = 5) -> list[dict[str, Any]]:
    note_ids = _openreview_ids_from_paper(paper)
    out: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    for note_id in note_ids:
        out.append({
            "kind": "openreview_official_note_pdf",
            "pdf_url": f"openreview://{note_id}/pdf",
            "openreview_note_id": note_id,
            "accepted": True,
            "official_client_required": True,
        })
    if out:
        return out
    title = str(paper.get("title") or "").strip()
    if len(title.split()) < 3:
        return [{"kind": "openreview_official_title_search", "accepted": False, "reason": "missing_searchable_title"}]
    for api_version in [2, 1]:
        client, receipt = _client(api_version)
        attempts.append({"kind": "openreview_official_client_init", **receipt})
        if client is None:
            continue
        notes, search_receipt = _notes_by_title(client, title, limit)
        attempts.append({"kind": "openreview_official_title_search", "api_version": api_version, **search_receipt})
        for note in notes:
            note_id = _note_id(note)
            if not note_id:
                continue
            accepted = _same_title_author(paper, note)
            item = {
                "kind": "openreview_official_title_verified_pdf",
                "pdf_url": f"openreview://{note_id}/pdf",
                "openreview_note_id": note_id,
                "openreview_title": _note_title(note),
                "api_version": api_version,
                "accepted": accepted,
                "official_client_required": True,
            }
            if accepted:
                out.append(item)
                return out
            attempts.append(item)
    return out or attempts or [{"kind": "openreview_official_title_search", "accepted": False, "reason": "no_openreview_match"}]


def download_openreview_official_pdf(candidate: dict[str, Any], target: Path) -> tuple[bool, dict[str, Any]]:
    note_id = str(candidate.get("openreview_note_id") or "").strip()
    if not note_id:
        return False, {"accepted": False, "reason": "missing_openreview_note_id"}
    attempts: list[dict[str, Any]] = []
    for api_version in [int(candidate.get("api_version") or 2), 1]:
        client, receipt = _client(api_version)
        attempts.append({"kind": "openreview_official_client_init", **receipt})
        if client is None:
            continue
        for method_name, args, kwargs in [
            ("get_pdf", (note_id,), {}),
            ("get_attachment", ("pdf",), {"id": note_id}),
        ]:
            method = getattr(client, method_name, None)
            if method is None:
                continue
            try:
                content = _guarded_openreview_call(lambda: _call_client_method(method, *args, **kwargs))
                if isinstance(content, str):
                    content = content.encode("utf-8", errors="replace")
                is_pdf = isinstance(content, (bytes, bytearray)) and bytes(content).startswith(b"%PDF")
                attempt = {"kind": method_name, "api_version": api_version, "accepted": is_pdf, "bytes": len(content or b"")}
                attempts.append(attempt)
                if is_pdf:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(bytes(content))
                    return True, {"accepted": True, "openreview_note_id": note_id, "attempts": attempts, "selected": attempt}
            except ServiceCooldownActive as exc:
                attempts.append({
                    "kind": method_name,
                    "api_version": api_version,
                    "accepted": False,
                    "reason": "openreview_service_cooldown_active",
                    "cooldown_remaining_sec": round(exc.remaining, 3),
                })
                return False, {
                    "accepted": False,
                    "reason": "openreview_service_cooldown_active",
                    "openreview_note_id": note_id,
                    "attempts": attempts,
                }
            except Exception as exc:
                attempts.append({
                    "kind": method_name,
                    "api_version": api_version,
                    "accepted": False,
                    "error": exc.__class__.__name__,
                    "message": str(exc)[:500],
                })
                if "403" in str(exc) or "Forbidden" in str(exc):
                    return False, {"accepted": False, "reason": "openreview_official_pdf_forbidden", "openreview_note_id": note_id, "attempts": attempts}
    for reason in [
        "openreview_official_pdf_forbidden",
        "openreview_official_client_forbidden",
        "openreview_service_cooldown_active",
        "missing_openreview_py",
        "missing_openreview_credentials",
    ]:
        if any(attempt.get("reason") == reason for attempt in attempts):
            return False, {"accepted": False, "reason": reason, "openreview_note_id": note_id, "attempts": attempts}
    return False, {"accepted": False, "reason": "openreview_official_pdf_unavailable", "openreview_note_id": note_id, "attempts": attempts}
