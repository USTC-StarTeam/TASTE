from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .models import AppConfig, LLMRole


def _strip_json_fences(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _json_span(text: str) -> str:
    start_obj = text.find("{")
    start_arr = text.find("[")
    starts = [x for x in [start_obj, start_arr] if x >= 0]
    if not starts:
        raise ValueError("No JSON object or array found")
    start = min(starts)
    end = text.rfind("}" if text[start] == "{" else "]")
    if end < start:
        raise ValueError("JSON closing bracket not found")
    return text[start : end + 1]


def _escape_invalid_json_backslashes(text: str) -> str:
    # Some OpenAI-compatible providers return literal backslashes in Chinese
    # translations, e.g. \u not followed by four hex digits. JSON rejects
    # those strings; preserve visible text by escaping only invalid slashes.
    return re.sub(r'\\(?!["\\/bfnrt]|u[0-9a-fA-F]{4})', r'\\\\', text or "")


def _repair_json_text(text: str) -> str:
    repaired = text.strip()
    repaired = repaired.replace("\ufeff", "")
    repaired = _escape_invalid_json_backslashes(repaired)
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
    repaired = re.sub(r"}\s*\n\s*{", "},{", repaired)
    repaired = re.sub(r"([\"}\]0-9])\s*\n\s*(\"[A-Za-z_][^\"\\]*(?:\\.[^\"\\]*)*\"\s*:)", r"\1,\n\2", repaired)
    repaired = re.sub(r"([\"}\]0-9])\s+(\"[A-Za-z_][^\"\\]*(?:\\.[^\"\\]*)*\"\s*:)", r"\1, \2", repaired)
    return repaired


def _loads_json_lenient(text: str) -> Any:
    decoder = json.JSONDecoder()
    for candidate in (text, _repair_json_text(text)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            try:
                value, _end = decoder.raw_decode(candidate.strip())
                return value
            except json.JSONDecodeError:
                continue
    return json.loads(_repair_json_text(text))


def _extract_named_array(raw: str, key: str) -> list[Any]:
    text = _strip_json_fences(raw)
    match = re.search(rf'"{re.escape(key)}"\s*:\s*\[', text)
    if not match:
        return []
    start = match.end() - 1
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                snippet = text[start : index + 1]
                try:
                    value = _loads_json_lenient(snippet)
                    return value if isinstance(value, list) else []
                except Exception:
                    return extract_partial_json_array(snippet)
    return extract_partial_json_array(text[start:])


def _recover_expected_json(raw: str) -> Any:
    for key in ("evaluations", "selected", "ideas", "plans", "readings"):
        rows = _extract_named_array(raw, key)
        if rows:
            return {key: rows}
    rows = extract_partial_json_array(raw)
    if rows:
        return rows
    raise


def extract_json(raw: str) -> Any:
    text = _strip_json_fences(raw)
    try:
        span = _json_span(text)
    except Exception:
        # Many OpenAI-compatible providers occasionally truncate the final
        # closing brace/bracket while still returning a valid leading array
        # such as {"evaluations":[{...},{...}.  Recover complete objects
        # instead of dropping the whole batch to local adaptive fallback scores.
        return _recover_expected_json(text)
    try:
        return _loads_json_lenient(span)
    except Exception:
        return _recover_expected_json(span)


def extract_partial_json_array(raw: str) -> list[Any]:
    """Recover complete objects from a truncated top-level JSON array."""
    text = (raw or "").strip()
    start = text.find("[")
    if start < 0:
        return []
    items: list[Any] = []
    depth = 0
    obj_start = -1
    in_string = False
    escape = False
    for index, char in enumerate(text[start:], start):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            if depth == 0:
                obj_start = index
            depth += 1
        elif char == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and obj_start >= 0:
                    snippet = text[obj_start:index + 1]
                    try:
                        items.append(json.loads(snippet))
                    except Exception:
                        pass
                    obj_start = -1
    return items


def _chat_url(base_url: str) -> str:
    url = (base_url or "").rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    if url.endswith("/responses"):
        return url[: -len("/responses")] + "/chat/completions"
    return url + "/chat/completions"


def _responses_url(base_url: str) -> str:
    url = (base_url or "").rstrip("/")
    if url.endswith("/responses"):
        return url
    if url.endswith("/chat/completions"):
        return url[: -len("/chat/completions")] + "/responses"
    return url + "/responses"


def _content_to_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        chunks: list[str] = []
        for part in value:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict):
                for key in ["text", "content", "output_text"]:
                    inner = part.get(key)
                    if isinstance(inner, str) and inner.strip():
                        chunks.append(inner)
                        break
        return "\n".join(chunk for chunk in chunks if chunk.strip()).strip()
    if isinstance(value, dict):
        for key in ["text", "content", "output_text"]:
            inner = value.get(key)
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
    return ""


def _chat_response_debug(raw: Any) -> str:
    if not isinstance(raw, dict):
        return type(raw).__name__
    parts: list[str] = []
    choices = raw.get("choices", []) or []
    parts.append("top_keys=" + ",".join(sorted(str(key) for key in raw.keys())[:12]))
    if choices and isinstance(choices[0], dict):
        choice0 = choices[0]
        message = choice0.get("message", {}) if isinstance(choice0.get("message"), dict) else {}
        parts.append("finish_reason=" + str(choice0.get("finish_reason") or ""))
        parts.append("choice_keys=" + ",".join(sorted(str(key) for key in choice0.keys())[:12]))
        parts.append("message_keys=" + ",".join(sorted(str(key) for key in message.keys())[:12]))
    output = raw.get("output", []) or []
    if output and isinstance(output[0], dict):
        parts.append("output_keys=" + ",".join(sorted(str(key) for key in output[0].keys())[:12]))
    return "; ".join(part for part in parts if part)


def _extract_chat_text(raw: Any) -> str:
    if not isinstance(raw, dict):
        return ""
    choices = raw.get("choices", []) or []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message", {}) if isinstance(choice.get("message"), dict) else {}
        for key in ["content", "text"]:
            value = _content_to_text(message.get(key, choice.get(key, "")))
            if value:
                return value
        # Reasoning-only output usually means the provider spent the token budget before
        # emitting final JSON in message.content. Use reasoning_content only when it
        # visibly contains JSON; otherwise treat it as empty and expose diagnostics.
        value = _content_to_text(message.get("reasoning_content", choice.get("reasoning_content", "")))
        if value and ("{" in value or "[" in value):
            return value
    for item in raw.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []) or []:
            value = _content_to_text(content)
            if value:
                return value
        value = _content_to_text(item.get("content") or item.get("text"))
        if value:
            return value
    for key in ["output_text", "content", "text"]:
        value = _content_to_text(raw.get(key))
        if value:
            return value
    return ""


class LLMClient:
    def __init__(self, config: AppConfig, role: LLMRole | str | None = None):
        self.config = config
        self.role = role or "global"
        self.provider = config.provider
        self.base_url = config.base_url
        self.api_key = config.api_key
        self.model = config.model
        self.temperature = config.temperature
        if role:
            override = config.llm_roles.get(str(role))
            if override:
                self.provider = override.provider or self.provider
                self.base_url = override.base_url or self.base_url
                self.api_key = override.api_key or self.api_key
                self.model = override.model or self.model
                self.temperature = config.temperature if override.temperature is None else override.temperature
        self.api_mode = os.environ.get("LLM_API_MODE", "chat_completions")
        self.timeout_sec = int(os.environ.get("LLM_TIMEOUT_SEC", "120"))
        self.max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "2000"))
        self.retries = max(1, int(os.environ.get("LLM_RETRIES", "3")))
        self.enabled = bool(self.api_key and self.model and self.provider.lower() != "mock")

    def summary(self) -> dict:
        return {
            "role": self.role,
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "temperature": self.temperature,
            "enabled": self.enabled,
            "api_mode": self.api_mode or "chat_completions",
        }

    def chat(self, prompt: str, temperature: float | None = None, max_tokens: int | None = None) -> str:
        if not self.enabled:
            raise RuntimeError("LLM is not configured")

        api_mode = str(self.api_mode or "chat_completions").strip().lower()
        use_responses = api_mode in {"responses", "response", "openai_responses"}
        response_format = os.environ.get("LLM_RESPONSE_FORMAT", "json_object").strip().lower()
        reasoning_effort = os.environ.get("LLM_REASONING_EFFORT", "").strip().lower()
        disable_thinking = os.environ.get("LLM_DISABLE_THINKING", "0").lower() in {"1", "true", "yes", "on"}
        retry_empty_json = os.environ.get("LLM_RETRY_EMPTY_JSON_WITHOUT_RESPONSE_FORMAT", "1").lower() in {"1", "true", "yes", "on"}
        retry_unsupported_optional = os.environ.get("LLM_RETRY_UNSUPPORTED_OPTIONAL_PARAMS", "1").lower() in {"1", "true", "yes", "on"}
        retry_statuses = {408, 409, 429, 500, 502, 503, 504}

        def build_payload(*, include_response_format: bool, include_thinking_controls: bool) -> dict[str, Any]:
            if use_responses:
                payload: dict[str, Any] = {
                    "model": self.model,
                    "input": [
                        {"role": "system", "content": [{"type": "input_text", "text": "You are a strict JSON generator. Put the final answer in output_text only. Return valid JSON only, with no markdown."}]},
                        {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
                    ],
                    "temperature": self.temperature if temperature is None else temperature,
                    "max_output_tokens": int(max_tokens or self.max_tokens),
                }
                if include_response_format and response_format in {"json", "json_object"}:
                    payload["text"] = {"format": {"type": "json_object"}}
            else:
                payload = {
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": "You are a strict JSON generator. Put the final answer in message.content only. Do not put the answer in reasoning_content. Return valid JSON only, with no markdown."},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": self.temperature if temperature is None else temperature,
                    "max_tokens": int(max_tokens or self.max_tokens),
                }
                if include_response_format and response_format in {"json", "json_object"}:
                    payload["response_format"] = {"type": "json_object"}
            if reasoning_effort and reasoning_effort not in {"none", "off", "disable", "disabled", "0", "false", "no"}:
                payload["reasoning_effort"] = reasoning_effort
            if include_thinking_controls and disable_thinking:
                payload["thinking"] = {"type": "disabled"}
                payload["enable_thinking"] = False
                payload["extra_body"] = {"thinking": {"type": "disabled"}}
            return payload

        def request_once(*, include_response_format: bool, include_thinking_controls: bool) -> str:
            payload = build_payload(
                include_response_format=include_response_format,
                include_thinking_controls=include_thinking_controls,
            )
            req = urllib.request.Request(
                _responses_url(self.base_url) if use_responses else _chat_url(self.base_url),
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
                method="POST",
            )
            last_error: Exception | None = None
            for attempt in range(1, self.retries + 1):
                try:
                    with urllib.request.urlopen(req, timeout=self.timeout_sec) as response:
                        raw = json.loads(response.read().decode("utf-8", "ignore"))
                    text = _extract_chat_text(raw)
                    if text:
                        return text
                    raise RuntimeError("Chat Completions API returned no extractable text; " + _chat_response_debug(raw))
                except urllib.error.HTTPError as exc:
                    body = exc.read().decode("utf-8", "ignore")[:800]
                    last_error = RuntimeError(f"LLM HTTP {exc.code} via {self.api_mode or 'chat_completions'}: {body}")
                    if exc.code not in retry_statuses or attempt >= self.retries:
                        raise last_error from exc
                except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
                    last_error = exc
                    if attempt >= self.retries:
                        raise RuntimeError(f"LLM request failed via {self.api_mode or 'chat_completions'} after {self.retries} attempts: {exc}") from exc
                retry_text = str(last_error or "").lower()
                slow_provider = any(marker in str(self.base_url or "").lower() for marker in ["sensenova", "xiaomi", "mi.com", "bigmodel.cn"])
                rate_limited = any(marker in retry_text for marker in ["429", "rate", "rpm", "too many", "timeout", "timed out"])
                base_sleep = min(2 ** (attempt - 1), 8) + 0.1 * attempt
                if slow_provider or rate_limited:
                    base_sleep = max(base_sleep, min(12.0, 2.5 * attempt))
                time.sleep(base_sleep)
            raise RuntimeError(f"LLM request failed via {self.api_mode or 'chat_completions'}: {last_error}")

        attempts: list[tuple[bool, bool, str]] = [(True, True, "configured")]
        if response_format in {"json", "json_object"}:
            attempts.append((False, True, "without_response_format"))
        if disable_thinking:
            attempts.append((response_format in {"json", "json_object"}, False, "without_thinking_controls"))
            if response_format in {"json", "json_object"}:
                attempts.append((False, False, "without_response_format_or_thinking_controls"))

        last_error: Exception | None = None
        seen: set[tuple[bool, bool]] = set()
        for include_response_format, include_thinking_controls, label in attempts:
            key = (include_response_format, include_thinking_controls)
            if key in seen:
                continue
            seen.add(key)
            try:
                text = request_once(
                    include_response_format=include_response_format,
                    include_thinking_controls=include_thinking_controls,
                )
                if (
                    include_response_format
                    and retry_empty_json
                    and response_format in {"json", "json_object"}
                    and text.strip() == "{}"
                ):
                    last_error = RuntimeError(f"LLM returned empty JSON object with response_format during {label}")
                    continue
                return text
            except RuntimeError as exc:
                last_error = exc
                message = str(exc).lower()
                if retry_unsupported_optional and include_thinking_controls and any(token in message for token in ["unsupported parameter", "enable_thinking", "thinking"]):
                    continue
                if retry_unsupported_optional and include_response_format and any(token in message for token in ["response_format", "json_object", "unsupported parameter"]):
                    continue
                if include_response_format and response_format in {"json", "json_object"}:
                    continue
                raise
        raise RuntimeError(f"LLM request failed via {self.api_mode or 'chat_completions'}: {last_error}")

    def json_or_none(self, prompt: str, temperature: float | None = None, max_tokens: int | None = None) -> Any | None:
        try:
            return extract_json(self.chat(prompt, temperature=temperature, max_tokens=max_tokens))
        except Exception:
            return None

    def json_or_error(self, prompt: str, temperature: float | None = None, max_tokens: int | None = None) -> dict:
        raw_text = ""
        try:
            raw_text = self.chat(prompt, temperature=temperature, max_tokens=max_tokens)
            return {"ok": True, "data": extract_json(raw_text), "error": "", "raw_text": raw_text[:4000]}
        except Exception as first_exc:
            parse_error = str(first_exc)
            # Truncated JSON is common when a provider hits the completion budget.
            # Retry once with a larger budget before callers fall back to adaptive profile scores.
            if raw_text and any(token in parse_error.lower() for token in ["closing bracket", "unterminated", "expecting", "delimiter"]):
                try:
                    retry_tokens = max(self.max_tokens * 2, int(os.environ.get("LLM_PARSE_RETRY_MAX_TOKENS", "12000") or 12000))
                    raw_text = self.chat(prompt, temperature=temperature, max_tokens=retry_tokens)
                    return {"ok": True, "data": extract_json(raw_text), "error": "", "raw_text": raw_text[:4000], "parse_retry": True}
                except Exception as retry_exc:
                    return {"ok": False, "data": None, "error": f"{parse_error}; retry_failed: {retry_exc}", "raw_text": raw_text[:4000]}
            return {"ok": False, "data": None, "error": parse_error, "raw_text": raw_text[:4000]}


def clamp_workers(value: int | None, default: int = 16, maximum: int = 32) -> int:
    try:
        number = int(default if value is None else value)
    except (TypeError, ValueError):
        number = default
    return max(1, min(maximum, number))


def parallel_json(client: LLMClient, prompts: list[str], max_workers: int, temperature: float | None = None, max_tokens: int | None = None) -> list[dict]:
    if not prompts:
        return []
    workers = clamp_workers(max_workers, default=1, maximum=max_workers)
    results: list[dict] = [{"ok": False, "data": None, "error": "not started"} for _ in prompts]
    kwargs: dict[str, Any] = {}
    if temperature is not None:
        kwargs["temperature"] = temperature
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(client.json_or_error, prompt, **kwargs): index for index, prompt in enumerate(prompts)}
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    return results


_LOCAL_GENERIC_TERMS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "into", "is", "it", "its", "of", "on", "or", "that", "the", "their", "to", "with",
    "paper", "papers", "study", "studies", "using", "use", "used", "method", "methods", "model", "models", "system", "systems",
}


def _text_terms(text: str, *, min_len: int = 2) -> list[str]:
    raw_terms = re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9][a-zA-Z0-9_.-]{1,}", (text or "").lower())
    terms: list[str] = []
    for raw in raw_terms:
        term = raw.strip(".,;:!?()[]{}\"'")
        if len(term) < min_len or term in _LOCAL_GENERIC_TERMS:
            continue
        terms.append(term)
    return list(dict.fromkeys(terms))


def keyword_category(title: str, abstract: str = "") -> str:
    terms = _text_terms(f"{title} {abstract}", min_len=3)[:3]
    if not terms:
        return "Local topic"
    return "Local topic: " + " / ".join(terms)


def _interest_terms(interest: str) -> list[str]:
    return _text_terms(interest, min_len=2)


def _interest_phrases(interest: str) -> list[str]:
    phrases: list[str] = []
    for part in re.split(r"[\n,;，；。.!?、/]+", interest or ""):
        text = " ".join(str(part).lower().split())
        if len(text) >= 4:
            phrases.append(text)
    terms = [term for term in _interest_terms(interest) if re.fullmatch(r"[a-zA-Z0-9_.-]+", term)]
    for size in range(2, min(5, len(terms)) + 1):
        for index in range(0, len(terms) - size + 1):
            phrases.append(" ".join(terms[index:index + size]))
    return list(dict.fromkeys(phrase for phrase in phrases if len(phrase) >= 4))


def fallback_score(interest: str, title: str, abstract: str = "") -> float:
    haystack = f"{title} {abstract}".lower()
    tokens = _interest_terms(interest)
    if not tokens:
        return 6.0
    title_l = (title or "").lower()
    title_hits = sum(1 for token in tokens if token in title_l)
    text_hits = sum(1 for token in tokens if token in haystack)
    coverage = text_hits / max(1, len(tokens))
    phrase_hits = sum(1 for phrase in _interest_phrases(interest) if phrase in haystack)
    score = 4.0 + min(4.0, coverage * 4.0) + min(1.0, title_hits * 0.25) + min(1.0, phrase_hits * 0.35)
    return round(max(0.0, min(9.5, score)), 2)
