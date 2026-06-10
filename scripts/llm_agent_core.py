#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from llm_client import call_llm, llm_available, llm_disabled_reason


JSON_BLOCK_RE = re.compile(r'```json\s*(.*?)```', re.DOTALL | re.IGNORECASE)
PATCH_BLOCK_RE = re.compile(r'```(?:diff|patch)?\s*(.*?)```', re.DOTALL | re.IGNORECASE)


def read_text(path: Path, limit: int = 12000) -> str:
    if not path.exists():
        return ''
    return path.read_text(encoding='utf-8', errors='ignore')[:limit]


def extract_balanced_json_objects(text: str) -> list[str]:
    objects: list[str] = []
    start = -1
    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(text or ''):
        if in_string:
            if escape:
                escape = False
            elif char == '\\':
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == '{':
            if depth == 0:
                start = index
            depth += 1
        elif char == '}' and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                objects.append(text[start:index + 1])
                start = -1
    return objects


def safe_json_loads(text: str, default: Any):
    text = (text or '').strip()
    if not text:
        return default
    candidates = [text]
    for match in JSON_BLOCK_RE.findall(text):
        candidates.append(match.strip())
    candidates.extend(extract_balanced_json_objects(text))
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except Exception:
            continue
    return default


def extract_patch_text(text: str) -> str:
    text = text or ''
    matches = PATCH_BLOCK_RE.findall(text)
    if matches:
        for match in matches:
            candidate = match.strip('\n')
            if '*** Begin Patch' in candidate or candidate.startswith('--- ') or candidate.startswith('diff --git'):
                return candidate + ('\n' if not candidate.endswith('\n') else '')
    stripped = text.strip()
    if '*** Begin Patch' in stripped or stripped.startswith('--- ') or stripped.startswith('diff --git'):
        return stripped + ('\n' if not stripped.endswith('\n') else '')
    return ''


def llm_json(prompt: str, cfg: dict[str, Any], system_prompt: str = '') -> tuple[dict[str, Any], dict[str, Any]]:
    if not llm_available(cfg):
        raise RuntimeError(llm_disabled_reason(cfg))
    result = call_llm(prompt, cfg, system_prompt=system_prompt)
    parsed = safe_json_loads(result.get('content', ''), {})
    return parsed if isinstance(parsed, dict) else {}, result


def llm_text(prompt: str, cfg: dict[str, Any], system_prompt: str = '') -> dict[str, Any]:
    if not llm_available(cfg):
        raise RuntimeError(llm_disabled_reason(cfg))
    return call_llm(prompt, cfg, system_prompt=system_prompt)
