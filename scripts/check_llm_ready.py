#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from llm_client import call_llm, get_llm_config, llm_available, llm_disabled_reason
from project_paths import build_paths, load_project_config


def main() -> None:
    parser = argparse.ArgumentParser(description="Check whether the generic OpenAI-compatible LLM API is configured and callable.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--live", action="store_true", help="Send a tiny API request instead of only checking configuration.")
    args = parser.parse_args()

    cfg = load_project_config(args.project)
    paths = build_paths(args.project)
    settings = get_llm_config(cfg)
    safe = {k: v for k, v in settings.items() if k != "api_key"}
    safe["api_key_present"] = bool(settings.get("api_key"))
    result = {
        "project": args.project,
        "configured": llm_available(cfg),
        "disabled_reason": "" if llm_available(cfg) else llm_disabled_reason(cfg),
        "settings": safe,
        "live_checked": bool(args.live),
        "live_ok": False,
        "live_error": "",
    }
    if args.live and result["configured"]:
        try:
            response = call_llm("Return exactly: ok", cfg, system_prompt="You are a readiness checker. Reply with ok only.")
            content = str(response.get("content", "")).strip().lower()
            result["live_ok"] = "ok" in content[:20]
            result["model"] = response.get("model", "")
        except Exception as exc:
            result["live_error"] = str(exc)
    out = paths.reports / "llm_readiness.json"
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md = paths.reports / "llm_readiness.md"
    lines = ["# LLM Readiness\n\n"]
    lines.append(f"- configured: {result['configured']}\n")
    lines.append(f"- disabled_reason: {result['disabled_reason'] or 'none'}\n")
    lines.append(f"- provider: {safe.get('provider', '')}\n")
    lines.append(f"- api_mode: {safe.get('api_mode', '')}\n")
    lines.append(f"- api_base: {safe.get('api_base', '')}\n")
    lines.append(f"- model: {safe.get('model', '')}\n")
    lines.append(f"- api_key_env: {safe.get('api_key_env', '')}\n")
    lines.append(f"- api_key_present: {safe.get('api_key_present')}\n")
    lines.append(f"- live_checked: {result['live_checked']}\n")
    lines.append(f"- live_ok: {result['live_ok']}\n")
    if result.get("live_error"):
        lines.append(f"- live_error: {result['live_error']}\n")
    lines.append("\n## Minimum Configuration\n")
    lines.append("- Set project `llm.api_base`, `llm.model`, and the configured API key env var, or export `LLM_API_BASE`, `LLM_MODEL`, and the key env var.\n")
    lines.append(f"- Then run `python3 scripts/check_llm_ready.py --project {args.project} --live`.\n")
    md.write_text("".join(lines), encoding="utf-8")
    print(md)
    if not result["configured"] or (args.live and not result["live_ok"]):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
