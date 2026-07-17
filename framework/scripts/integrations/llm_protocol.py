from __future__ import annotations


def chat_url(api_base: str) -> str:
    url = str(api_base or "").rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    if url.endswith("/responses"):
        return url[: -len("/responses")] + "/chat/completions"
    return url + "/chat/completions"


def responses_url(api_base: str) -> str:
    url = str(api_base or "").rstrip("/")
    if url.endswith("/responses"):
        return url
    if url.endswith("/chat/completions"):
        return url[: -len("/chat/completions")] + "/responses"
    return url + "/responses"


def prompt_with_json_response_hint(prompt: str) -> str:
    text = str(prompt or "")
    if "json" in text.lower():
        return text
    suffix = "Return valid JSON."
    return f"{text.rstrip()}\n\n{suffix}" if text.strip() else suffix
