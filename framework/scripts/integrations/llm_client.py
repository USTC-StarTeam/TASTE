#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from integrations.llm_protocol import chat_url as _chat_url
from integrations.llm_protocol import prompt_with_json_response_hint as _prompt_with_json_response_hint
from integrations.llm_protocol import responses_url as _responses_url


def get_llm_config(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg or {}
    llm = cfg.get('llm', {}) if isinstance(cfg, dict) else {}
    runtime = cfg.get('runtime', {}) if isinstance(cfg, dict) and isinstance(cfg.get('runtime', {}), dict) else {}
    env_overrides = runtime.get('env_overrides', {}) if isinstance(runtime.get('env_overrides', {}), dict) else {}

    def env_value(name: str, default: Any = '') -> Any:
        value = os.environ.get(name)
        if value not in (None, ''):
            return value
        value = env_overrides.get(name)
        if value not in (None, ''):
            return value
        return default

    # Project config is authoritative for TASTE runs. Shell LLM_* variables and
    # saved runtime env overrides are startup/default fallbacks for empty saved fields.
    provider = str(llm.get('provider') or env_value('LLM_PROVIDER') or 'none')
    api_base = str(llm.get('api_base') or env_value('LLM_API_BASE') or '')
    model = str(llm.get('model') or env_value('LLM_MODEL') or '')
    api_key_env = str(llm.get('api_key_env') or env_value('LLM_API_KEY_ENV') or 'OPENAI_API_KEY')
    api_key = str(llm.get('api_key') or env_value('LLM_API_KEY') or env_value(api_key_env) or '')
    timeout_sec = int(llm.get('timeout_sec') or env_value('LLM_TIMEOUT_SEC') or 120)
    max_tokens = int(llm.get('max_tokens') or env_value('LLM_MAX_TOKENS') or 2000)
    response_format = str(llm.get('response_format') or env_value('LLM_RESPONSE_FORMAT') or '')
    api_mode = str(llm.get('api_mode') or env_value('LLM_API_MODE') or 'chat_completions')
    temperature = float(llm.get('temperature') if llm.get('temperature') is not None else env_value('LLM_TEMPERATURE', 0.2) or 0.2)
    enabled = str(llm.get('enabled', env_value('LLM_ENABLED', True))).lower() not in {'0', 'false', 'no'}
    return {
        'enabled': enabled,
        'provider': provider,
        'api_base': api_base,
        'api_mode': api_mode,
        'model': model,
        'api_key_env': api_key_env,
        'api_key': api_key,
        'timeout_sec': timeout_sec,
        'max_tokens': max_tokens,
        'temperature': temperature,
        'response_format': response_format,
    }


def llm_available(cfg: dict[str, Any] | None = None) -> bool:
    settings = get_llm_config(cfg)
    if not settings['enabled']:
        return False
    return bool(settings['provider'] not in {'', 'none'} and settings['api_base'] and settings['model'] and settings['api_key'])


def llm_disabled_reason(cfg: dict[str, Any] | None = None) -> str:
    settings = get_llm_config(cfg)
    if not settings['enabled']:
        return 'llm-disabled'
    if settings['provider'] in {'', 'none'}:
        return 'llm-provider-missing'
    if not settings['api_base']:
        return 'llm-api-base-missing'
    if not settings['model']:
        return 'llm-model-missing'
    if not settings['api_key']:
        return f"llm-api-key-missing:{settings['api_key_env']}"
    return 'llm-not-available'


def _wants_json_response(settings: dict[str, Any]) -> bool:
    return settings.get('response_format') in {'json_object', 'json'}


def _extract_response_text(raw: Any) -> tuple[str, str, list[str]]:
    if not isinstance(raw, dict):
        return '', '', []
    if isinstance(raw.get('output_text'), str) and raw['output_text'].strip():
        return raw['output_text'], str(raw.get('status', '')), ['output_text']
    chunks: list[str] = []
    keys: list[str] = []
    for item in raw.get('output', []) or []:
        if not isinstance(item, dict):
            continue
        keys.extend(str(k) for k in item.keys())
        for content in item.get('content', []) or []:
            if not isinstance(content, dict):
                continue
            keys.extend(str(k) for k in content.keys())
            if content.get('type') in {'output_text', 'text'} and isinstance(content.get('text'), str):
                chunks.append(content['text'])
            elif isinstance(content.get('text'), str):
                chunks.append(content['text'])
    if chunks:
        return '\n'.join(chunks), str(raw.get('status', '')), sorted(set(keys))
    # Compatibility fallback for providers that expose Chat Completions shape.
    choices = raw.get('choices', []) or []
    if choices and isinstance(choices[0], dict):
        choice0 = choices[0]
        message = choice0.get('message', {}) if isinstance(choice0.get('message'), dict) else {}
        for key in ['content', 'reasoning_content', 'text']:
            value = message.get(key, choice0.get(key, ''))
            if isinstance(value, str) and value.strip():
                return value, str(choice0.get('finish_reason', '')), sorted(set(message.keys()))
    return '', str(raw.get('status', '')), sorted(set(keys))


def _build_responses_payload(prompt: str, settings: dict[str, Any], system_prompt: str = '') -> dict[str, Any]:
    wants_json_response = _wants_json_response(settings)
    request_prompt = _prompt_with_json_response_hint(prompt) if wants_json_response else prompt
    payload: dict[str, Any] = {
        'model': settings['model'],
        'input': request_prompt if not system_prompt else [
            {'role': 'system', 'content': [{'type': 'input_text', 'text': system_prompt}]},
            {'role': 'user', 'content': [{'type': 'input_text', 'text': request_prompt}]},
        ],
        'temperature': settings['temperature'],
        'max_output_tokens': settings['max_tokens'],
    }
    if wants_json_response:
        payload['text'] = {'format': {'type': 'json_object'}}
    return payload


def _build_chat_payload(prompt: str, settings: dict[str, Any], system_prompt: str = '') -> dict[str, Any]:
    wants_json_response = _wants_json_response(settings)
    request_prompt = _prompt_with_json_response_hint(prompt) if wants_json_response else prompt
    payload = {
        'model': settings['model'],
        'messages': ([{'role': 'system', 'content': system_prompt}] if system_prompt else []) + [{'role': 'user', 'content': request_prompt}],
        'temperature': settings['temperature'],
        'max_tokens': settings['max_tokens'],
    }
    if wants_json_response:
        payload['response_format'] = {'type': 'json_object'}
    return payload


def call_llm(prompt: str, cfg: dict[str, Any] | None = None, system_prompt: str = '') -> dict[str, Any]:
    settings = get_llm_config(cfg)
    if not llm_available(cfg):
        raise RuntimeError(llm_disabled_reason(cfg))

    api_mode = str(settings.get('api_mode') or 'chat_completions').lower()
    use_chat = api_mode in {'chat', 'chat_completions', 'chat.completions'}
    url = _chat_url(settings['api_base']) if use_chat else _responses_url(settings['api_base'])
    payload = _build_chat_payload(prompt, settings, system_prompt) if use_chat else _build_responses_payload(prompt, settings, system_prompt)
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Content-Type': 'application/json',
            'Authorization': f"Bearer {settings['api_key']}",
        },
        method='POST',
    )
    attempts = max(1, int(os.environ.get('LLM_RETRIES', '4')))
    retry_statuses = {408, 409, 429, 500, 502, 503, 504}
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=settings['timeout_sec']) as response:
                raw = json.loads(response.read().decode('utf-8', 'ignore'))
            break
        except urllib.error.HTTPError as exc:
            last_error = exc
            body = exc.read().decode('utf-8', 'ignore')[:800]
            # Some OpenAI-compatible gateways lag on Responses. Make the incompatibility visible,
            # but allow an explicit opt-in fallback only when LLM_ALLOW_CHAT_FALLBACK=1.
            if (not use_chat) and exc.code in {400, 404, 405} and os.environ.get('LLM_ALLOW_CHAT_FALLBACK') == '1':
                settings = dict(settings)
                settings['api_mode'] = 'chat_completions'
                return call_llm(prompt, {'llm': settings}, system_prompt=system_prompt)
            if exc.code not in retry_statuses or attempt >= attempts:
                raise RuntimeError(f'LLM HTTP {exc.code} via {"chat" if use_chat else "responses"}: {body}') from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt >= attempts:
                raise RuntimeError(f'LLM request failed via {"chat" if use_chat else "responses"} after {attempts} attempts: {exc}') from exc
        time.sleep(min(2 ** (attempt - 1), 8) + 0.1 * attempt)
    else:
        raise RuntimeError(f'LLM request failed: {last_error}')
    content, finish_reason, message_keys = _extract_response_text(raw)
    return {
        'provider': settings['provider'],
        'model': settings['model'],
        'api_mode': 'chat_completions' if use_chat else 'responses',
        'content': content,
        'finish_reason': finish_reason,
        'message_keys': message_keys,
        'raw': raw,
    }
