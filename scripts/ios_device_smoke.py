#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


BUNDLE_ID = "org.ustcstarteam.taste.mobile"
SCHEME = "TASTEApp"
DEFAULT_PROJECT_ID = "ios_e2e_mobile_app"
DEFAULT_PROFILE = "TASTE iPhone"
DEFAULT_PROVISIONING_PROFILES_DIR = Path.home() / "Library" / "MobileDevice" / "Provisioning Profiles"
XCODE_PROVISIONING_PROFILES_DIR = Path.home() / "Library" / "Developer" / "Xcode" / "UserData" / "Provisioning Profiles"
DEFAULT_PROVISIONING_PROFILE_DIRS = [DEFAULT_PROVISIONING_PROFILES_DIR, XCODE_PROVISIONING_PROFILES_DIR]


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


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_script_module(name: str, script_name: str) -> Any:
    script = _repo_root() / "scripts" / script_name
    spec = importlib.util.spec_from_file_location(name, script)
    module = importlib.util.module_from_spec(spec)
    if spec.loader is None:
        raise RuntimeError(f"could not load {script}")
    spec.loader.exec_module(module)
    return module


def redacted_connection_link(link: str) -> str:
    if not link:
        return ""
    parts = urlsplit(str(link))
    sensitive_keys = {"token", "server_access_token", "access_token"}
    query = [
        (key, "REDACTED" if key.lower() in sensitive_keys and value else value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _redact_text(value: Any, secrets: list[str] | tuple[str, ...] | None = None) -> str:
    text = str(value or "")
    for secret in secrets or []:
        clean = str(secret or "").strip()
        if clean:
            text = text.replace(clean, "REDACTED")
    return text


def _tail_text(value: Any, *, max_lines: int = 24, secrets: list[str] | tuple[str, ...] | None = None) -> str:
    lines = _redact_text(value, secrets).splitlines()
    return "\n".join(lines[-max_lines:])


def _signing_hint(text: str) -> str:
    lower = text.lower()
    if "no account for team" in lower or "no profiles for" in lower or "provisioning" in lower:
        return (
            "iOS device signing/provisioning is not ready. Open Xcode Settings > Accounts, "
            "sign in with the Apple ID for this team, then rerun with --allow-provisioning-updates; "
            "or pass --development-team <TEAMID> and a bundle id that has an iOS Development profile."
        )
    if "requires a development team" in lower:
        return "Pass --development-team <TEAMID> or install an Apple Development identity so the script can infer one."
    return ""


def command_error_payload(exc: subprocess.CalledProcessError, *, secrets: list[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
    stdout = getattr(exc, "stdout", None)
    if stdout is None:
        stdout = getattr(exc, "output", "")
    stderr = getattr(exc, "stderr", "")
    combined = "\n".join([str(stdout or ""), str(stderr or "")])
    command = [_redact_text(part, secrets) for part in (exc.cmd if isinstance(exc.cmd, list) else [str(exc.cmd)])]
    payload = {
        "ok": False,
        "error": f"Command failed with exit {exc.returncode}",
        "command": command,
        "stdout_tail": _tail_text(stdout, secrets=secrets),
        "stderr_tail": _tail_text(stderr, secrets=secrets),
    }
    hint = _signing_hint(combined)
    if hint:
        payload["signing_hint"] = hint
    return payload


def exit_code_for_summary(summary: dict[str, Any]) -> int:
    return 0 if bool(summary.get("ok")) else 1


def _build_connection_link(
    *,
    server_url: str,
    profile: str,
    kind: str,
    project_id: str,
    token: str,
) -> str:
    link_module = _load_script_module("mobile_connection_link", "mobile_connection_link.py")
    return str(link_module.build_connection_link(
        server_url=server_url,
        profile=profile,
        kind=kind,
        project=project_id,
        token=token,
    ))


def _select_device(devices: list[dict[str, str]], selector: str = "") -> dict[str, str]:
    if not devices:
        raise RuntimeError("No trusted physical iPhone is available for device smoke.")
    selector = str(selector or "").strip()
    if not selector:
        return devices[0]
    for device in devices:
        values = {
            str(device.get("identifier") or ""),
            str(device.get("udid") or ""),
            str(device.get("xcode_destination_id") or ""),
            str(device.get("name") or ""),
        }
        if selector in values:
            return device
    raise RuntimeError(f"physical iPhone not found for selector: {selector}")


def _app_path(derived_data: Path) -> Path:
    return Path(derived_data) / "Build" / "Products" / "Debug-iphoneos" / "TASTEApp.app"


def infer_development_team_from_identities(output: str) -> str:
    for line in str(output or "").splitlines():
        if "Apple Development:" not in line:
            continue
        match = re.search(r"\(([A-Z0-9]{10})\)", line)
        if match:
            return match.group(1)
    return ""


def infer_code_signing_development_team(runner: Any) -> str:
    try:
        result = runner.run(["security", "find-identity", "-v", "-p", "codesigning"], check=False)
    except Exception:
        return ""
    return infer_development_team_from_identities(getattr(result, "stdout", ""))


def infer_xcode_development_team_from_defaults(output: str) -> str:
    match = re.search(r"teamID\s*=\s*([A-Z0-9]{10})", str(output or ""))
    return match.group(1) if match else ""


def infer_xcode_development_team(runner: Any) -> str:
    try:
        result = runner.run(["defaults", "read", "com.apple.dt.Xcode", "IDEProvisioningTeamByIdentifier"], check=False)
    except Exception:
        return ""
    return infer_xcode_development_team_from_defaults(getattr(result, "stdout", ""))


def resolve_development_team(runner: Any, explicit_team: str = "") -> dict[str, Any]:
    explicit = str(explicit_team or "").strip()
    if explicit:
        return {"team": explicit, "source": "explicit", "identity_available": True}
    identity_team = infer_code_signing_development_team(runner)
    if identity_team:
        return {"team": identity_team, "source": "code_signing_identity", "identity_available": True}
    xcode_team = infer_xcode_development_team(runner)
    if xcode_team:
        return {"team": xcode_team, "source": "xcode_defaults", "identity_available": False}
    return {"team": "", "source": "", "identity_available": False}


def _decode_mobileprovision(runner: Any, path: Path) -> dict[str, Any]:
    result = runner.run(["security", "cms", "-D", "-i", str(path)], check=False)
    raw = getattr(result, "stdout", "") or ""
    if not raw:
        return {}
    try:
        payload = plistlib.loads(raw.encode("utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _profile_team_identifier(payload: dict[str, Any]) -> str:
    team_ids = payload.get("TeamIdentifier")
    if isinstance(team_ids, list) and team_ids:
        return str(team_ids[0] or "").strip()
    return str(payload.get("TeamIdentifier") or "").strip()


def _profile_bundle_pattern(payload: dict[str, Any]) -> str:
    entitlements = payload.get("Entitlements") if isinstance(payload.get("Entitlements"), dict) else {}
    application_identifier = str(entitlements.get("application-identifier") or "").strip()
    if "." not in application_identifier:
        return ""
    return application_identifier.split(".", 1)[1]


def _bundle_matches_profile(bundle_id: str, profile_pattern: str) -> bool:
    bundle_id = str(bundle_id or "").strip()
    profile_pattern = str(profile_pattern or "").strip()
    if not bundle_id or not profile_pattern:
        return False
    if profile_pattern == bundle_id:
        return True
    if profile_pattern.endswith(".*"):
        return bundle_id.startswith(profile_pattern[:-1])
    return profile_pattern == "*"


def _provisioning_profile_dirs(paths: Any) -> list[Path]:
    if isinstance(paths, (list, tuple, set)):
        return [Path(path) for path in paths]
    text = str(paths or "").strip()
    if os.pathsep in text:
        return [Path(path) for path in text.split(os.pathsep) if path]
    return [Path(text)]


def collect_signing_readiness(
    runner: Any,
    *,
    bundle_id: str,
    development_team: str = "",
    profiles_dir: Any = DEFAULT_PROVISIONING_PROFILE_DIRS,
) -> dict[str, Any]:
    bundle_id = str(bundle_id or BUNDLE_ID).strip() or BUNDLE_ID
    team_resolution = resolve_development_team(runner, development_team)
    resolved_team = str(team_resolution.get("team") or "")
    identity_available = bool(team_resolution.get("identity_available"))
    profile_dirs = _provisioning_profile_dirs(profiles_dir)

    matching_profiles: list[dict[str, str]] = []
    profile_count = 0
    for profile_dir in profile_dirs:
        for profile_path in sorted(profile_dir.glob("*.mobileprovision")) if profile_dir.exists() else []:
            profile_count += 1
            payload = _decode_mobileprovision(runner, profile_path)
            team = _profile_team_identifier(payload)
            pattern = _profile_bundle_pattern(payload)
            if resolved_team and team and team != resolved_team:
                continue
            if not _bundle_matches_profile(bundle_id, pattern):
                continue
            entitlements = payload.get("Entitlements") if isinstance(payload.get("Entitlements"), dict) else {}
            matching_profiles.append({
                "name": str(payload.get("Name") or profile_path.stem),
                "path": str(profile_path),
                "team": team,
                "bundle_pattern": pattern,
                "development": str(bool(entitlements.get("get-task-allow"))).lower(),
            })

    blocking_items: list[str] = []
    if not identity_available:
        blocking_items.append("No valid Apple Development signing identity is available in Keychain.")
    if not matching_profiles:
        blocking_items.append(
            f"No iOS Development provisioning profile matches bundle id {bundle_id}. "
            "Open Xcode Settings > Accounts, sign in, or pass --bundle-id for a profile you own."
        )

    return {
        "ready": not blocking_items,
        "bundle_id": bundle_id,
        "development_team": resolved_team,
        "development_team_source": str(team_resolution.get("source") or ""),
        "identity_available": identity_available,
        "profile_dirs": [str(path) for path in profile_dirs],
        "profile_count": profile_count,
        "matching_profile_count": len(matching_profiles),
        "matching_profiles": matching_profiles,
        "blocking_items": blocking_items,
    }


def run_ios_device_smoke(
    runner: Any,
    *,
    root: Path,
    app_root: Path,
    derived_data: Path,
    server_url: str,
    token: str = "",
    project_id: str = DEFAULT_PROJECT_ID,
    profile: str = DEFAULT_PROFILE,
    kind: str = "server",
    device: str = "",
    connection_link: str = "",
    bundle_id: str = BUNDLE_ID,
    development_team: str = "",
    infer_team: bool = True,
    allow_provisioning_updates: bool = False,
    signing_preflight_only: bool = False,
    provisioning_profiles_dir: Path = DEFAULT_PROVISIONING_PROFILES_DIR,
    skip_api_smoke: bool = False,
) -> dict[str, Any]:
    root = Path(root)
    app_root = Path(app_root)
    derived_data = Path(derived_data)
    derived_data.mkdir(parents=True, exist_ok=True)

    preflight_module = _load_script_module("ios_device_preflight", "ios_device_preflight.py")
    preflight = preflight_module.run_ios_device_preflight(
        runner,
        root=root,
        server_url=server_url,
        token=token,
        project_id=project_id,
        profile=profile,
        kind=kind,
        skip_api_smoke=skip_api_smoke,
    )
    if preflight.get("blocking_items"):
        raise RuntimeError("iOS physical device preflight failed: " + "; ".join(str(item) for item in preflight["blocking_items"]))

    selected_device = _select_device(preflight.get("devices") if isinstance(preflight.get("devices"), list) else [], device)
    xcode_destination_id = str(selected_device.get("xcode_destination_id") or selected_device.get("udid") or selected_device.get("identifier") or "")
    devicectl_device_id = str(selected_device.get("identifier") or selected_device.get("udid") or xcode_destination_id)
    if not xcode_destination_id or not devicectl_device_id:
        raise RuntimeError("physical iPhone did not provide usable Xcode/devicectl identifiers")

    requested_development_team = str(development_team or "").strip()
    resolved_development_team = requested_development_team
    resolved_bundle_id = str(bundle_id or BUNDLE_ID).strip() or BUNDLE_ID
    signing_readiness = collect_signing_readiness(
        runner,
        bundle_id=resolved_bundle_id,
        development_team=requested_development_team if infer_team else resolved_development_team,
        profiles_dir=Path(provisioning_profiles_dir),
    )
    if signing_preflight_only:
        return {
            "ok": bool(signing_readiness.get("ready")),
            "bundle_id": resolved_bundle_id,
            "device": selected_device,
            "preflight": preflight,
            "development_team": signing_readiness.get("development_team", resolved_development_team),
            "signing_readiness": signing_readiness,
        }
    resolved_development_team = str(signing_readiness.get("development_team") or resolved_development_team)

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
        "iphoneos",
        "-destination",
        f"id={xcode_destination_id}",
        "-derivedDataPath",
        str(derived_data),
    ]
    if allow_provisioning_updates:
        build_args.append("-allowProvisioningUpdates")
    build_args.append("build")
    if resolved_development_team:
        build_args.append(f"DEVELOPMENT_TEAM={resolved_development_team}")
    if resolved_bundle_id != BUNDLE_ID:
        build_args.append(f"PRODUCT_BUNDLE_IDENTIFIER={resolved_bundle_id}")
    build_args.append("CODE_SIGNING_ALLOWED=YES")
    runner.run(build_args, cwd=app_root)

    app = _app_path(derived_data)
    runner.run(["xcrun", "devicectl", "device", "install", "app", "--device", devicectl_device_id, str(app)])

    payload_url = str(connection_link or "").strip()
    if not payload_url:
        payload_url = _build_connection_link(
            server_url=server_url,
            profile=profile,
            kind=kind,
            project_id=project_id,
            token=token,
        )
    launch_args = [
        "xcrun",
        "devicectl",
        "device",
        "process",
        "launch",
        "--device",
        devicectl_device_id,
        "--terminate-existing",
    ]
    if payload_url:
        launch_args.extend(["--payload-url", payload_url])
    launch_args.append(resolved_bundle_id)
    runner.run(launch_args)

    return {
        "ok": True,
        "bundle_id": resolved_bundle_id,
        "device": selected_device,
        "app": str(app),
        "preflight": preflight,
        "installed": True,
        "launched": True,
        "development_team": resolved_development_team,
        "signing_readiness": signing_readiness,
        "connection_link_dispatched": bool(payload_url),
        "connection_link": redacted_connection_link(payload_url),
    }


def build_parser() -> argparse.ArgumentParser:
    root = _repo_root()
    parser = argparse.ArgumentParser(description="Build, install, launch, and connection-link smoke-test the TASTE iOS app on a physical iPhone.")
    parser.add_argument("--root", default=str(root))
    parser.add_argument("--app-root", default=str(root / "apps" / "ios" / "TASTEApp"))
    parser.add_argument("--derived-data", default=str(Path(tempfile.gettempdir()) / "taste-ios-device-smoke-derived-data"))
    parser.add_argument("--server-url", default=os.environ.get("TASTE_SERVER_URL", ""), help="TASTE URL reachable from the iPhone.")
    parser.add_argument("--token", default=os.environ.get("TASTE_SERVER_TOKEN", os.environ.get("TASTE_ACCESS_TOKEN", "")))
    parser.add_argument("--project-id", default=DEFAULT_PROJECT_ID)
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--kind", choices=["computer", "server", "cloud"], default="server")
    parser.add_argument("--device", default="", help="Optional device name, CoreDevice identifier, UDID, or Xcode destination id.")
    parser.add_argument("--connection-link", default="", help="Optional taste://connect link to pass as the launch payload URL. Defaults to a generated link.")
    parser.add_argument("--bundle-id", default=os.environ.get("TASTE_IOS_BUNDLE_ID", BUNDLE_ID), help="Bundle identifier to sign and launch; override for a personal/team provisioning profile.")
    parser.add_argument("--development-team", default=os.environ.get("DEVELOPMENT_TEAM", os.environ.get("APPLE_DEVELOPMENT_TEAM", "")))
    parser.add_argument("--no-infer-development-team", action="store_true", help="Do not infer DEVELOPMENT_TEAM from local Apple Development signing identities.")
    parser.add_argument("--signing-preflight-only", action="store_true", help="Only report physical-device signing readiness; do not build or install.")
    parser.add_argument(
        "--provisioning-profiles-dir",
        default=os.pathsep.join(str(path) for path in DEFAULT_PROVISIONING_PROFILE_DIRS),
        help="Provisioning profile directory, or multiple directories separated by the OS path separator.",
    )
    parser.add_argument("--allow-provisioning-updates", action="store_true")
    parser.add_argument("--skip-api-smoke", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = run_ios_device_smoke(
            CommandRunner(),
            root=Path(args.root),
            app_root=Path(args.app_root),
            derived_data=Path(args.derived_data),
            server_url=args.server_url,
            token=args.token,
            project_id=args.project_id,
            profile=args.profile,
            kind=args.kind,
            device=args.device,
            connection_link=args.connection_link,
            bundle_id=args.bundle_id,
            development_team=args.development_team,
            infer_team=not args.no_infer_development_team,
            allow_provisioning_updates=args.allow_provisioning_updates,
            signing_preflight_only=args.signing_preflight_only,
            provisioning_profiles_dir=Path(args.provisioning_profiles_dir),
            skip_api_smoke=args.skip_api_smoke,
        )
    except subprocess.CalledProcessError as exc:
        print(
            json.dumps(command_error_payload(exc, secrets=[args.token, args.connection_link]), ensure_ascii=False, indent=2),
            file=sys.stderr,
        )
        return int(exc.returncode or 1)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return exit_code_for_summary(summary)


if __name__ == "__main__":
    raise SystemExit(main())
