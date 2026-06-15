#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import ipaddress
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


DEFAULT_PROJECT_ID = "ios_e2e_mobile_app"
DEFAULT_PROFILE = "TASTE iPhone"


class CommandRunner:
    def run(self, args: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=check,
        )


def normalize_server_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("server URL is required")
    if not text.startswith(("http://", "https://")):
        text = "http://" + text
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"invalid server URL: {value}")
    return text.rstrip("/")


def is_physical_phone_reachable_url(value: str) -> bool:
    parsed = urlparse(normalize_server_url(value))
    host = str(parsed.hostname or "").strip().lower()
    if not host:
        return False
    if host == "localhost":
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (address.is_loopback or address.is_unspecified)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_mobile_connection_link_module() -> Any:
    script = _repo_root() / "scripts" / "mobile_connection_link.py"
    spec = importlib.util.spec_from_file_location("mobile_connection_link", script)
    module = importlib.util.module_from_spec(spec)
    if spec.loader is None:
        raise RuntimeError(f"could not load {script}")
    spec.loader.exec_module(module)
    return module


def build_connect_page_url(*, server_url: str, profile: str, kind: str, project_id: str) -> str:
    link_module = _load_mobile_connection_link_module()
    return str(link_module.build_connect_page_url(
        server_url=server_url,
        profile=profile,
        kind=kind,
        project=project_id,
    ))


def _iter_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _iter_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_dicts(item)


def _nested_text(mapping: dict[str, Any], *paths: str) -> str:
    for path in paths:
        current: Any = mapping
        for key in path.split("."):
            if not isinstance(current, dict) or key not in current:
                current = None
                break
            current = current.get(key)
        text = str(current or "").strip()
        if text:
            return text
    return ""


def _available_device(mapping: dict[str, Any]) -> bool:
    if mapping.get("isAvailable") is False:
        return False
    status_text = " ".join(
        _nested_text(mapping, key)
        for key in (
            "state",
            "status",
            "availability",
            "connectionProperties.pairingState",
        )
    ).lower()
    unavailable_markers = {"unavailable", "unpaired", "disabled", "offline"}
    return not any(marker in status_text for marker in unavailable_markers)


def _physical_ios_device_summary(mapping: dict[str, Any]) -> dict[str, str] | None:
    core_device_identifier = _nested_text(mapping, "identifier")
    hardware_udid = _nested_text(mapping, "hardwareProperties.udid", "udid", "UDID")
    identifier = core_device_identifier or hardware_udid
    if not identifier:
        return None

    text = " ".join(
        _nested_text(mapping, key)
        for key in (
            "name",
            "deviceType",
            "kind",
            "platform",
            "modelName",
            "deviceProperties.name",
            "deviceProperties.deviceType",
            "deviceProperties.platform",
            "deviceProperties.productType",
            "hardwareProperties.deviceType",
            "hardwareProperties.modelName",
        )
    ).lower()
    if "simulator" in text or "watch" in text:
        return None
    if "iphone" not in text and "ipad" not in text and "ios" not in text and "ipados" not in text:
        return None
    if not _available_device(mapping):
        return None

    name = _nested_text(mapping, "name", "deviceProperties.name", "hardwareProperties.marketingName") or "iOS Device"
    device_type = _nested_text(mapping, "deviceType", "deviceProperties.deviceType", "hardwareProperties.deviceType") or "iOS"
    state = _nested_text(mapping, "state", "status", "availability", "connectionProperties.pairingState") or "available"
    transport = _nested_text(mapping, "connectionProperties.transportType", "connectionProperties.connectionType") or "unknown"
    xcode_destination_id = hardware_udid or identifier
    return {
        "identifier": identifier,
        "udid": hardware_udid or identifier,
        "xcode_destination_id": xcode_destination_id,
        "name": name,
        "device_type": device_type,
        "state": state,
        "transport": transport,
    }


def parse_physical_ios_devices(payload: Any) -> list[dict[str, str]]:
    devices: list[dict[str, str]] = []
    seen: set[str] = set()
    result = payload.get("result") if isinstance(payload, dict) else {}
    device_rows = result.get("devices") if isinstance(result, dict) else None
    candidates = device_rows if isinstance(device_rows, list) else list(_iter_dicts(payload))
    for item in candidates:
        if not isinstance(item, dict):
            continue
        summary = _physical_ios_device_summary(item)
        if not summary:
            continue
        identifier = summary["identifier"]
        if identifier in seen:
            continue
        seen.add(identifier)
        devices.append(summary)
    return devices


def list_physical_ios_devices(runner: Any) -> list[dict[str, str]]:
    result = runner.run(["xcrun", "devicectl", "list", "devices", "--json-output", "-"])
    payload = json.loads(result.stdout or "{}")
    return parse_physical_ios_devices(payload)


def run_mobile_api_smoke(
    runner: Any,
    *,
    root: Path,
    server_url: str,
    token: str = "",
    project_id: str = DEFAULT_PROJECT_ID,
) -> dict[str, Any]:
    args = [
        sys.executable,
        "scripts/mobile_api_smoke.py",
        "--server-url",
        server_url,
        "--project-id",
        project_id,
    ]
    if str(token or "").strip():
        args.extend(["--token", str(token).strip()])
    result = runner.run(args, cwd=root)
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        payload = {"ok": False, "raw": result.stdout}
    return payload if isinstance(payload, dict) else {"ok": False, "raw": payload}


def _mobile_api_ready(api_smoke: dict[str, Any]) -> bool:
    checks = api_smoke.get("checks") if isinstance(api_smoke.get("checks"), dict) else {}
    return bool(api_smoke.get("ok")) and bool(checks.get("mobile_control_plane")) and int(checks.get("mobile_api_version") or 0) >= 1


def _redact_token(value: Any, token: str) -> Any:
    clean_token = str(token or "").strip()
    if not clean_token:
        return value
    if isinstance(value, dict):
        return {key: _redact_token(item, clean_token) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_token(item, clean_token) for item in value]
    if isinstance(value, str):
        return value.replace(clean_token, "REDACTED")
    return value


def run_ios_device_preflight(
    runner: Any,
    *,
    root: Path,
    server_url: str,
    token: str = "",
    project_id: str = DEFAULT_PROJECT_ID,
    profile: str = DEFAULT_PROFILE,
    kind: str = "server",
    skip_api_smoke: bool = False,
) -> dict[str, Any]:
    root = Path(root)
    normalized_url = normalize_server_url(server_url)
    blocking_items: list[str] = []

    server_url_reachable = is_physical_phone_reachable_url(normalized_url)
    if not server_url_reachable:
        blocking_items.append(
            "127.0.0.1/localhost only reaches the physical iPhone itself; use the computer LAN IP, VPN, or authenticated tunnel URL."
        )

    device_error = ""
    try:
        devices = list_physical_ios_devices(runner)
    except Exception as exc:
        devices = []
        device_error = str(exc)
        blocking_items.append(f"Could not list physical iOS devices with xcrun devicectl: {device_error}")

    if not devices and not device_error:
        blocking_items.append("Connect a trusted physical iPhone, unlock it, and allow this computer before running the preflight.")

    api_smoke: dict[str, Any]
    if skip_api_smoke:
        api_smoke = {"skipped": True}
    else:
        try:
            api_smoke = run_mobile_api_smoke(
                runner,
                root=root,
                server_url=normalized_url,
                token=token,
                project_id=project_id,
            )
        except Exception as exc:
            api_smoke = {"ok": False, "error": str(exc)}
        api_smoke = _redact_token(api_smoke, token)
        if not _mobile_api_ready(api_smoke):
            blocking_items.append(
                "Mobile API smoke failed or the server does not advertise mobile_api_version >= 1 with mobile_control_plane."
            )

    connect_page_url = build_connect_page_url(
        server_url=normalized_url,
        profile=profile,
        kind=kind,
        project_id=project_id,
    )

    return {
        "ok": not blocking_items,
        "server_url": normalized_url,
        "server_url_reachable_for_phone": server_url_reachable,
        "physical_device_available": bool(devices),
        "device_count": len(devices),
        "devices": devices,
        "api_smoke": api_smoke,
        "connect_page_url": connect_page_url,
        "server_token_provided": bool(str(token or "").strip()),
        "blocking_items": blocking_items,
    }


def build_parser() -> argparse.ArgumentParser:
    root = _repo_root()
    parser = argparse.ArgumentParser(description="Preflight-check a physical iPhone path for the TASTE iOS app.")
    parser.add_argument("--root", default=str(root))
    parser.add_argument("--server-url", default=os.environ.get("TASTE_SERVER_URL", ""), help="TASTE URL reachable from the iPhone; avoid 127.0.0.1/localhost for physical devices.")
    parser.add_argument("--token", default=os.environ.get("TASTE_SERVER_TOKEN", os.environ.get("TASTE_ACCESS_TOKEN", "")))
    parser.add_argument("--project-id", default=DEFAULT_PROJECT_ID)
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--kind", choices=["computer", "server", "cloud"], default="server")
    parser.add_argument("--skip-api-smoke", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = run_ios_device_preflight(
            CommandRunner(),
            root=Path(args.root),
            server_url=args.server_url,
            token=args.token,
            project_id=args.project_id,
            profile=args.profile,
            kind=args.kind,
            skip_api_smoke=args.skip_api_smoke,
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
