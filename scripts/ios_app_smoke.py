#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


BUNDLE_ID = "org.ustcstarteam.taste.mobile"
SCHEME = "TASTEApp"
DEFAULT_DEVICE_NAME = "iPhone 17"


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


def _device_runtime_is_ios(runtime: str) -> bool:
    return ".iOS-" in runtime or runtime.endswith(".iOS")


def find_device_udid(runner: Any, device_name: str) -> tuple[str, str]:
    result = runner.run(["xcrun", "simctl", "list", "devices", "available", "--json"])
    payload = json.loads(result.stdout or "{}")
    for runtime, devices in (payload.get("devices") or {}).items():
        if not _device_runtime_is_ios(runtime):
            continue
        for device in devices or []:
            if device.get("name") == device_name and device.get("isAvailable", True):
                return str(device.get("udid") or ""), str(device.get("state") or "")
    raise RuntimeError(f"available iOS simulator not found: {device_name}")


def ensure_device_booted(runner: Any, udid: str, state: str) -> None:
    if state != "Booted":
        runner.run(["xcrun", "simctl", "boot", udid])
    runner.run(["xcrun", "simctl", "bootstatus", udid, "-b"])


def ensure_device_cleanly_booted_for_launch_argument(runner: Any, udid: str, state: str) -> None:
    if state == "Booted":
        runner.run(["xcrun", "simctl", "shutdown", udid], check=False)
        state = "Shutdown"
    ensure_device_booted(runner, udid, state)


def redacted_connection_link(link: str) -> str:
    if not link:
        return ""
    parts = urlsplit(link)
    sensitive_keys = {"token", "server_access_token", "access_token"}
    query = [
        (key, "REDACTED" if key.lower() in sensitive_keys and value else value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def run_ios_app_smoke(
    runner: Any,
    *,
    root: Path,
    app_root: Path,
    derived_data: Path,
    device_name: str = DEFAULT_DEVICE_NAME,
    screenshot: Path | None = None,
    server_url: str = "http://127.0.0.1:8765",
    token: str = "",
    project_id: str = "ios_e2e_mobile_app",
    connection_link: str = "",
    connection_link_dispatch: str = "openurl",
    light_actions: list[str] | tuple[str, ...] | None = None,
    wait_light_actions: bool = False,
    action_wait_timeout: float = 60.0,
    action_poll_interval: float = 1.0,
    skip_api_smoke: bool = False,
) -> dict[str, Any]:
    root = Path(root)
    app_root = Path(app_root)
    derived_data = Path(derived_data)
    screenshot = Path(screenshot or (root / "runtime" / "ios-app-smoke.png"))
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    derived_data.mkdir(parents=True, exist_ok=True)

    project = app_root / "TASTEApp.xcodeproj"
    build_args = [
        "xcodebuild",
        "-project",
        str(project),
        "-scheme",
        SCHEME,
        "-configuration",
        "Debug",
        "-sdk",
        "iphonesimulator",
        "-destination",
        f"platform=iOS Simulator,name={device_name}",
        "-derivedDataPath",
        str(derived_data),
        "build",
        "CODE_SIGNING_ALLOWED=NO",
    ]
    runner.run(build_args, cwd=app_root)

    udid, state = find_device_udid(runner, device_name)
    if not udid:
        raise RuntimeError(f"simulator {device_name} did not report a UDID")

    app = derived_data / "Build" / "Products" / "Debug-iphonesimulator" / "TASTEApp.app"
    dispatch = str(connection_link_dispatch or "openurl").strip().lower()
    if dispatch not in {"openurl", "launch_argument"}:
        raise ValueError("connection_link_dispatch must be openurl or launch_argument")
    if connection_link and dispatch == "launch_argument":
        ensure_device_cleanly_booted_for_launch_argument(runner, udid, state)
    else:
        ensure_device_booted(runner, udid, state)
    runner.run(["xcrun", "simctl", "install", udid, str(app)])
    runner.run(["xcrun", "simctl", "terminate", udid, BUNDLE_ID], check=False)
    launch_args = ["xcrun", "simctl", "launch", udid, BUNDLE_ID]
    if connection_link and dispatch == "launch_argument":
        launch_args.extend(["--taste-connection-link", connection_link])
    runner.run(launch_args)
    time.sleep(2)
    if connection_link and dispatch == "openurl":
        runner.run(["xcrun", "simctl", "openurl", udid, connection_link])
        time.sleep(2)
    runner.run(["xcrun", "simctl", "io", udid, "screenshot", str(screenshot)])

    api_summary: dict[str, Any] | None = None
    if not skip_api_smoke:
        api_args = [
            sys.executable,
            "scripts/mobile_api_smoke.py",
            "--server-url",
            server_url,
            "--project-id",
            project_id,
        ]
        if token:
            api_args.extend(["--token", token])
        for action in light_actions or []:
            api_args.extend(["--light-action", str(action)])
        if wait_light_actions:
            api_args.append("--wait-light-actions")
            api_args.extend(["--action-wait-timeout", f"{action_wait_timeout:g}"])
            api_args.extend(["--action-poll-interval", f"{action_poll_interval:g}"])
        api_result = runner.run(api_args, cwd=root)
        try:
            api_summary = json.loads(api_result.stdout or "{}")
        except json.JSONDecodeError:
            api_summary = {"ok": False, "raw": api_result.stdout}

    return {
        "ok": True,
        "device_name": device_name,
        "device_udid": udid,
        "bundle_id": BUNDLE_ID,
        "app": str(app),
        "screenshot": str(screenshot),
        "connection_link_dispatched": bool(connection_link),
        "connection_link_dispatch": dispatch if connection_link else "",
        "connection_link": redacted_connection_link(connection_link),
        "api_smoke": api_summary,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build, install, launch, and screenshot the TASTE iOS app on a simulator.")
    root = Path(__file__).resolve().parents[1]
    parser.add_argument("--root", default=str(root))
    parser.add_argument("--app-root", default=str(root / "apps" / "ios" / "TASTEApp"))
    parser.add_argument("--derived-data", default=str(Path(tempfile.gettempdir()) / "taste-ios-smoke-derived-data"))
    parser.add_argument("--device-name", default=DEFAULT_DEVICE_NAME)
    parser.add_argument("--screenshot", default=str(root / "runtime" / "ios-app-smoke.png"))
    parser.add_argument("--server-url", default=os.environ.get("TASTE_SERVER_URL", "http://127.0.0.1:8765"))
    parser.add_argument("--token", default=os.environ.get("TASTE_SERVER_TOKEN", os.environ.get("TASTE_ACCESS_TOKEN", "")))
    parser.add_argument("--project-id", default="ios_e2e_mobile_app")
    parser.add_argument("--connection-link", default="", help="Optional taste://connect link to import in the simulator.")
    parser.add_argument(
        "--connection-link-dispatch",
        choices=["openurl", "launch_argument"],
        default="openurl",
        help="Use openurl to exercise iOS custom-link dispatch, or launch_argument to bypass the iOS confirmation prompt for automation.",
    )
    parser.add_argument("--light-action", action="append", default=[], help="Optional lightweight project action for the API smoke: status or healthcheck.")
    parser.add_argument("--wait-light-actions", action="store_true", help="Ask the API smoke to poll lightweight actions until a terminal job status.")
    parser.add_argument("--action-wait-timeout", type=float, default=60.0)
    parser.add_argument("--action-poll-interval", type=float, default=1.0)
    parser.add_argument("--skip-api-smoke", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = run_ios_app_smoke(
            CommandRunner(),
            root=Path(args.root),
            app_root=Path(args.app_root),
            derived_data=Path(args.derived_data),
            device_name=args.device_name,
            screenshot=Path(args.screenshot),
            server_url=args.server_url,
            token=args.token,
            project_id=args.project_id,
            connection_link=args.connection_link,
            connection_link_dispatch=args.connection_link_dispatch,
            light_actions=args.light_action,
            wait_light_actions=args.wait_light_actions,
            action_wait_timeout=args.action_wait_timeout,
            action_poll_interval=args.action_poll_interval,
            skip_api_smoke=args.skip_api_smoke,
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
