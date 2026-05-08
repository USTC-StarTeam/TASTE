from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from openai import OpenAI

from .models import AppConfig, LLMRole


def extract_json(raw: str) -> Any:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start_obj = text.find("{")
    start_arr = text.find("[")
    starts = [x for x in [start_obj, start_arr] if x >= 0]
    if not starts:
        raise ValueError("No JSON object or array found")
    start = min(starts)
    end = text.rfind("}" if text[start] == "{" else "]")
    if end < start:
        raise ValueError("JSON closing bracket not found")
    return json.loads(text[start : end + 1])


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
        self.enabled = bool(self.api_key and self.model and self.provider.lower() != "mock")
        self.client = None
        if self.enabled:
            self.client = OpenAI(api_key=self.api_key, base_url=self.base_url or None)

    def summary(self) -> dict:
        return {
            "role": self.role,
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "temperature": self.temperature,
            "enabled": self.enabled,
        }

    def chat(self, prompt: str, temperature: float | None = None) -> str:
        if not self.enabled or self.client is None:
            raise RuntimeError("LLM is not configured")
        result = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=self.temperature if temperature is None else temperature,
        )
        return result.choices[0].message.content or ""

    def json_or_none(self, prompt: str) -> Any | None:
        try:
            return extract_json(self.chat(prompt))
        except Exception:
            return None

    def json_or_error(self, prompt: str) -> dict:
        try:
            return {"ok": True, "data": extract_json(self.chat(prompt)), "error": ""}
        except Exception as exc:
            return {"ok": False, "data": None, "error": str(exc)}


def clamp_workers(value: int | None, default: int = 16, maximum: int = 32) -> int:
    try:
        number = int(default if value is None else value)
    except (TypeError, ValueError):
        number = default
    return max(1, min(maximum, number))


def parallel_json(client: LLMClient, prompts: list[str], max_workers: int) -> list[dict]:
    if not prompts:
        return []
    workers = clamp_workers(max_workers, default=1, maximum=max_workers)
    results: list[dict] = [{"ok": False, "data": None, "error": "not started"} for _ in prompts]
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(client.json_or_error, prompt): index for index, prompt in enumerate(prompts)}
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    return results


def keyword_category(title: str, abstract: str = "") -> str:
    text = f"{title} {abstract}".lower()
    rules = [
        ("Agent / Tool Use", ["agent", "tool", "web", "gui", "planner"]),
        ("LLM / Foundation Models", ["llm", "language model", "pretrain", "instruction", "alignment"]),
        ("Multimodal / Vision-Language", ["vision-language", "multimodal", "image", "video", "vlm"]),
        ("Safety / Trustworthy AI", ["safety", "privacy", "robust", "fairness", "hallucination"]),
        ("RAG / Retrieval", ["retrieval", "rag", "knowledge graph", "search"]),
        ("Systems / Efficiency", ["efficient", "quantization", "serving", "inference", "system"]),
        ("Robotics / Embodied AI", ["robot", "embodied", "navigation", "manipulation"]),
    ]
    for label, keys in rules:
        if any(key in text for key in keys):
            return label
    return "General AI / Machine Learning"


def _interest_terms(interest: str) -> list[str]:
    raw_terms = re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9][a-zA-Z0-9_.-]{2,}", interest.lower())
    synonyms = {
        "生成式": ["generative"],
        "生成式ai": ["generative ai"],
        "科学发现": ["scientific discovery", "science discovery"],
        "材料": ["material", "materials"],
        "材料物理": ["materials physics", "material physics"],
        "物理": ["physics"],
        "大模型": ["large language model", "llm"],
        "语言模型": ["language model", "llm"],
        "智能体": ["agent", "agents"],
        "检索": ["retrieval", "rag"],
    }
    expanded: list[str] = []
    for term in raw_terms:
        expanded.append(term)
        expanded.extend(synonyms.get(term, []))
    return list(dict.fromkeys(term for term in expanded if len(term) >= 2))


def fallback_score(interest: str, title: str, abstract: str = "") -> float:
    haystack = f"{title} {abstract}".lower()
    tokens = _interest_terms(interest)
    if not tokens:
        return 6.0
    hits = sum(1 for token in tokens if token in haystack)
    return round(min(9.5, 5.0 + hits * 0.8), 2)
