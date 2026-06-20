from __future__ import annotations

import importlib

import repair.repair_current_find_full_text_evidence as _repair_impl

_repair_impl = importlib.reload(_repair_impl)
from repair.repair_current_find_full_text_evidence import *  # noqa: F401,F403,E402


_PROXY_NAMES = {
    "arxiv_search_candidates",
    "fetch_url",
    "main",
    "record_unavailable_full_text_evidence_blocker",
    "repair_current_find_full_text_evidence",
    "try_acquire_for_paper",
}
_ORIGINAL_IMPL = {name: getattr(_repair_impl, name) for name in _PROXY_NAMES if hasattr(_repair_impl, name)}
_SYNC_NAMES = [
    "FULL_TEXT_FETCH_ATTEMPTS",
    "FULL_TEXT_FETCH_RETRY_BASE_DELAY_SEC",
    "FULL_TEXT_FETCH_RETRY_MAX_DELAY_SEC",
    "REQUEST_TIMEOUT_SEC",
    "build_paths",
    "fetch_url",
    "record_unavailable_full_text_evidence_blocker",
    "requests",
    "time",
    "try_acquire_for_paper",
]


def _sync_compat_monkeypatches() -> None:
    for name in _SYNC_NAMES:
        if name not in globals():
            continue
        current = globals()[name]
        if name in _PROXY_NAMES and getattr(current, "__module__", "") == __name__:
            current = _ORIGINAL_IMPL.get(name, current)
        setattr(_repair_impl, name, current)


def _restore_impl_functions() -> None:
    for name, original in _ORIGINAL_IMPL.items():
        setattr(_repair_impl, name, original)


def fetch_url(*args, **kwargs):
    _sync_compat_monkeypatches()
    try:
        return _repair_impl.fetch_url(*args, **kwargs)
    finally:
        _restore_impl_functions()


def arxiv_search_candidates(*args, **kwargs):
    _sync_compat_monkeypatches()
    try:
        return _repair_impl.arxiv_search_candidates(*args, **kwargs)
    finally:
        _restore_impl_functions()


def try_acquire_for_paper(*args, **kwargs):
    _sync_compat_monkeypatches()
    try:
        return _repair_impl.try_acquire_for_paper(*args, **kwargs)
    finally:
        _restore_impl_functions()


def record_unavailable_full_text_evidence_blocker(*args, **kwargs):
    _sync_compat_monkeypatches()
    try:
        return _repair_impl.record_unavailable_full_text_evidence_blocker(*args, **kwargs)
    finally:
        _restore_impl_functions()


def repair_current_find_full_text_evidence(*args, **kwargs):
    _sync_compat_monkeypatches()
    try:
        return _repair_impl.repair_current_find_full_text_evidence(*args, **kwargs)
    finally:
        _restore_impl_functions()


def main(*args, **kwargs):
    _sync_compat_monkeypatches()
    try:
        return _repair_impl.main(*args, **kwargs)
    finally:
        _restore_impl_functions()


if __name__ == "__main__":
    raise SystemExit(main())
