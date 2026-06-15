#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


LIGHT_ACTIONS = {"status", "healthcheck"}
TERMINAL_JOB_STATUSES = {"done", "error", "cancelled", "blocked"}
REQUIRED_MOBILE_CAPABILITIES = {
    "projects",
    "jobs",
    "runtime",
    "llm_config",
    "claude_latest_response",
    "remote_artifacts",
}
SENSITIVE_RESULT_KEYS = {"api_key", "authorization", "token", "access_token", "secret", "password"}


def normalize_base_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("server URL is required")
    if not text.startswith(("http://", "https://")):
        text = "http://" + text
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"invalid server URL: {value}")
    return text.rstrip("/")


def auth_headers(token: str) -> dict[str, str]:
    token = str(token or "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def normalize_light_actions(actions: list[str] | tuple[str, ...] | None) -> list[str]:
    normalized: list[str] = []
    for raw in actions or []:
        for item in str(raw or "").replace(",", " ").split():
            action = item.strip().lower()
            if not action:
                continue
            if action not in LIGHT_ACTIONS:
                raise ValueError(f"unsupported light action: {item}; choose status or healthcheck")
            if action not in normalized:
                normalized.append(action)
    return normalized


class TASTEAPIClient:
    def __init__(self, base_url: str, token: str = "", timeout: float = 12.0):
        self.base_url = normalize_base_url(base_url)
        self.headers = auth_headers(token)
        self.timeout = timeout

    def get(self, path: str, query: dict[str, str] | None = None) -> Any:
        return self.request("GET", path, query=query)

    def post(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        return self.request("POST", path, payload=payload or {})

    def request(
        self,
        method: str,
        path: str,
        query: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        url = self._url(path, query)
        headers = dict(self.headers)
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {path} failed with HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"{method} {path} failed: {exc.reason}") from exc
        if not body:
            return {}
        try:
            return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return {"raw": body.decode("utf-8", errors="replace")}

    def _url(self, path: str, query: dict[str, str] | None = None) -> str:
        clean_path = "/" + str(path or "").lstrip("/")
        url = self.base_url + clean_path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return url


def _project_key(row: Any) -> str:
    if isinstance(row, dict):
        return str(row.get("project") or row.get("id") or row.get("name") or "").strip()
    return ""


def _project_path(project_id: str) -> str:
    return urllib.parse.quote(str(project_id or "").strip(), safe="")


def _job_id(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("job_id") or payload.get("id") or "").strip()
    return ""


def sanitize_public_result(payload: Any) -> Any:
    if isinstance(payload, dict):
        sanitized: dict[str, Any] = {}
        for key, value in payload.items():
            normalized_key = str(key or "").strip().lower().replace("-", "_")
            if normalized_key in SENSITIVE_RESULT_KEYS:
                sanitized[key] = "<redacted>"
                continue
            sanitized[key] = sanitize_public_result(value)
        return sanitized
    if isinstance(payload, list):
        return [sanitize_public_result(item) for item in payload]
    return payload


def llm_probe_diagnostic(payload: Any) -> dict[str, str]:
    probe = payload if isinstance(payload, dict) else {}
    summary = probe.get("summary") if isinstance(probe.get("summary"), dict) else {}
    base_url = str(summary.get("base_url") or "")
    host = urllib.parse.urlparse(base_url).hostname or ""
    if bool(probe.get("ok")):
        return {"category": "ok", "host": host, "message": "LLM probe succeeded."}

    error = str(probe.get("error") or "").strip()
    error_lower = error.lower()
    if any(
        marker in error_lower
        for marker in [
            "connection reset",
            "failed to connect",
            "timed out",
            "timeout",
            "network is unreachable",
            "temporary failure in name resolution",
            "nodename nor servname",
        ]
    ):
        target = host or "LLM gateway"
        return {
            "category": "network_unreachable",
            "host": host,
            "message": f"{target} is not reachable from this machine or network; check VPN, campus network, firewall, or proxy access before changing the API key.",
        }
    if "401" in error_lower or "unauthorized" in error_lower or "invalid api key" in error_lower:
        return {
            "category": "auth_failed",
            "host": host,
            "message": "The LLM gateway rejected the API key; update the saved API key and retry the probe.",
        }
    return {
        "category": "llm_probe_failed",
        "host": host,
        "message": "The LLM probe failed; inspect the sanitized error and server network path.",
    }


def validate_mobile_control_plane_meta(payload: Any) -> dict[str, Any]:
    meta = payload if isinstance(payload, dict) else {}
    version = int(meta.get("mobile_api_version") or 0)
    capabilities = {
        str(item or "").strip().lower()
        for item in (meta.get("mobile_capabilities") if isinstance(meta.get("mobile_capabilities"), list) else [])
        if str(item or "").strip()
    }
    missing = sorted(REQUIRED_MOBILE_CAPABILITIES - capabilities)
    if version < 1 or missing:
        missing_text = ", ".join(missing) if missing else "none"
        raise RuntimeError(
            "TASTE server does not advertise the mobile control-plane API; "
            f"mobile_api_version={version}, missing_capabilities={missing_text}. "
            "Update branch-app and restart scripts/start_web.sh."
        )
    return {
        "mobile_api_version": version,
        "mobile_control_plane": True,
        "mobile_capability_count": len(capabilities),
    }


def _job_wait_summary(payload: Any, *, poll_count: int) -> dict[str, Any]:
    detail = payload if isinstance(payload, dict) else {}
    progress = detail.get("progress") if isinstance(detail.get("progress"), dict) else {}
    logs = detail.get("logs") if isinstance(detail.get("logs"), list) else []
    return {
        "final_status": str(detail.get("status") or ""),
        "progress_phase": str(progress.get("phase") or ""),
        "progress_message": str(progress.get("message") or ""),
        "poll_count": poll_count,
        "log_tail": [str(line) for line in logs[-8:]],
    }


def wait_for_job(
    client: Any,
    job_id: str,
    *,
    timeout_sec: float = 60.0,
    poll_interval: float = 1.0,
) -> dict[str, Any]:
    job_id = str(job_id or "").strip()
    if not job_id:
        raise ValueError("job_id is required")
    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    poll_count = 0
    encoded_job = urllib.parse.quote(job_id, safe="")
    last_detail: Any = {}
    while True:
        poll_count += 1
        last_detail = client.get(f"/api/jobs/{encoded_job}", {"compact": "1"})
        status = str((last_detail if isinstance(last_detail, dict) else {}).get("status") or "").lower()
        if status in TERMINAL_JOB_STATUSES:
            return _job_wait_summary(last_detail, poll_count=poll_count)
        if time.monotonic() >= deadline:
            summary = _job_wait_summary(last_detail, poll_count=poll_count)
            summary["timed_out"] = True
            return summary
        if poll_interval > 0:
            time.sleep(float(poll_interval))


def run_smoke(
    client: Any,
    *,
    project_id: str,
    topic: str = "Mobile control plane smoke",
    research_interest: str | None = None,
    researcher_profile: str = "iOS mobile smoke test; prefer lightweight server-side validation.",
    target_venue: str = "",
    paper_title: str = "",
    create_project: bool = True,
    light_actions: list[str] | tuple[str, ...] | None = None,
    wait_light_actions: bool = False,
    action_wait_timeout: float = 60.0,
    action_poll_interval: float = 1.0,
    include_llm_probe: bool = False,
) -> dict[str, Any]:
    project_id = str(project_id or "").strip()
    if not project_id:
        raise ValueError("project_id is required")

    health = client.get("/health")
    config_meta = client.get("/api/config/meta")
    mobile_meta_checks = validate_mobile_control_plane_meta(config_meta)
    projects = client.get("/api/projects")
    project_rows = projects if isinstance(projects, list) else []
    created_project = False
    if project_id not in {_project_key(row) for row in project_rows}:
        if not create_project:
            raise RuntimeError(f"project does not exist and creation is disabled: {project_id}")
        client.post("/api/projects", {"id": project_id, "topic": str(topic or "")})
        created_project = True

    encoded_project = _project_path(project_id)
    profile_payload = {
        "research_interest": str(topic if research_interest is None else research_interest).strip(),
        "researcher_profile": str(researcher_profile or "").strip(),
    }
    venue = str(target_venue or "").strip()
    title = str(paper_title or "").strip()
    if venue:
        profile_payload["target_venue"] = venue
        profile_payload["venue"] = venue
    if title:
        profile_payload["title"] = title
    project_config_synced = False
    if any(str(value or "").strip() for value in profile_payload.values()):
        client.post(f"/api/projects/{encoded_project}/config", profile_payload)
        project_config_synced = True

    project = client.get(f"/api/projects/{encoded_project}", {"compact": "1"})
    runtime = client.get(f"/api/projects/{encoded_project}/runtime")
    claude_latest = client.get(
        f"/api/projects/{encoded_project}/claude/latest-response",
        {"max_chars": "16000"},
    )
    jobs = client.get(
        "/api/jobs",
        {
            "compact": "1",
            "limit": "12",
            "include_history": "1",
            "project": project_id,
        },
    )
    llm_probe = None
    if include_llm_probe:
        llm_probe = sanitize_public_result(client.post("/api/config/llm-probe"))

    action_results: list[dict[str, Any]] = []
    for action in normalize_light_actions(light_actions):
        result = client.post("/api/jobs/project", {"project": project_id, "action": action})
        job_id = _job_id(result)
        action_summary = {
            "action": action,
            "job_id": job_id,
            "status": str((result if isinstance(result, dict) else {}).get("status") or ""),
            "stage": str((result if isinstance(result, dict) else {}).get("stage") or ""),
        }
        if wait_light_actions:
            action_summary.update(
                wait_for_job(
                    client,
                    job_id,
                    timeout_sec=action_wait_timeout,
                    poll_interval=action_poll_interval,
                )
            )
        action_results.append(action_summary)

    summary = {
        "ok": True,
        "project_id": project_id,
        "created_project": created_project,
        "checks": {
            "health": bool(health is not None),
            "config_meta": bool(config_meta is not None),
            **mobile_meta_checks,
            "project_count": len(project_rows),
            "project_detail": bool(project is not None),
            "project_config": project_config_synced,
            "runtime": bool(runtime is not None),
            "claude_latest_response": bool(claude_latest is not None),
            "claude_latest_returned_chars": int((claude_latest if isinstance(claude_latest, dict) else {}).get("returned_chcount") or 0),
            "job_count": len(jobs) if isinstance(jobs, list) else 0,
        },
        "light_actions": action_results,
    }
    if include_llm_probe:
        probe_ok = bool((llm_probe if isinstance(llm_probe, dict) else {}).get("ok"))
        summary["checks"]["llm_probe"] = probe_ok
        summary["llm_probe"] = llm_probe if llm_probe is not None else {}
        summary["llm_probe_diagnostic"] = llm_probe_diagnostic(llm_probe)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Smoke-test the TASTE mobile/iOS control-plane API.")
    parser.add_argument("--server-url", default=os.environ.get("TASTE_SERVER_URL", "http://127.0.0.1:8765"))
    parser.add_argument("--token", default=os.environ.get("TASTE_SERVER_TOKEN", os.environ.get("TASTE_ACCESS_TOKEN", "")))
    parser.add_argument("--project-id", default="ios_e2e_mobile_app")
    parser.add_argument("--topic", default="Mobile control plane smoke")
    parser.add_argument("--research-interest", default=None, help="Research interest to sync into the project config before polling.")
    parser.add_argument("--researcher-profile", default="iOS mobile smoke test; prefer lightweight server-side validation.")
    parser.add_argument("--target-venue", default="")
    parser.add_argument("--paper-title", default="")
    parser.add_argument("--timeout", type=float, default=12.0)
    parser.add_argument("--no-create-project", action="store_false", dest="create_project")
    parser.add_argument(
        "--light-action",
        action="append",
        default=[],
        help="Optional lightweight project action to dispatch: status or healthcheck. Can be repeated or comma-separated.",
    )
    parser.add_argument("--wait-light-actions", action="store_true", help="Poll each lightweight action until done/error/cancelled/blocked or timeout.")
    parser.add_argument("--action-wait-timeout", type=float, default=60.0)
    parser.add_argument("--action-poll-interval", type=float, default=1.0)
    parser.add_argument("--llm-probe", action="store_true", help="Also call /api/config/llm-probe and include the sanitized result.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    client = TASTEAPIClient(args.server_url, token=args.token, timeout=args.timeout)
    try:
        summary = run_smoke(
            client,
            project_id=args.project_id,
            topic=args.topic,
            research_interest=args.research_interest,
            researcher_profile=args.researcher_profile,
            target_venue=args.target_venue,
            paper_title=args.paper_title,
            create_project=args.create_project,
            light_actions=args.light_action,
            wait_light_actions=args.wait_light_actions,
            action_wait_timeout=args.action_wait_timeout,
            action_poll_interval=args.action_poll_interval,
            include_llm_probe=args.llm_probe,
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
