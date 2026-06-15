#!/usr/bin/env python3
"""Generate a TASTE iOS connection deep link."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_ROOT = REPO_ROOT / "modules" / "taste"
if str(MODULE_ROOT) not in sys.path:
    sys.path.insert(0, str(MODULE_ROOT))

from auto_research.web.mobile_qr import qr_svg, qr_svg_data_url


SENSITIVE_QUERY_KEYS = {"token", "server_access_token", "access_token"}


def normalized_server_url(value: str) -> str:
    text = str(value or "").strip()
    while len(text) > 1 and text.endswith("/"):
        text = text[:-1]
    if not (text.startswith("http://") or text.startswith("https://")):
        raise ValueError("server URL must start with http:// or https://")
    return text


def detect_lan_ip() -> str:
    """Return the best non-loopback IPv4 address for an iPhone on the LAN."""
    candidates: list[str] = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.settimeout(0.2)
        probe.connect(("8.8.8.8", 80))
        candidates.append(str(probe.getsockname()[0]))
    except OSError:
        pass
    finally:
        probe.close()

    try:
        _hostname, _aliases, addresses = socket.gethostbyname_ex(socket.gethostname())
        candidates.extend(addresses)
    except OSError:
        pass

    for address in candidates:
        text = str(address or "").strip()
        if text and "." in text and not text.startswith("127."):
            return text
    raise ValueError("could not detect a LAN IP address; pass --server-url http://<ip>:<port>")


def resolve_server_url(value: str, *, port: int = 8765) -> str:
    text = str(value or "").strip()
    if text.lower() in {"auto", "lan", "local-network"}:
        return normalized_server_url(f"http://{detect_lan_ip()}:{int(port)}")
    return normalized_server_url(text)


def build_connection_link(
    *,
    server_url: str,
    profile: str = "",
    kind: str = "server",
    project: str = "",
    token: str = "",
) -> str:
    kind = str(kind or "server").strip().lower()
    if kind not in {"computer", "server", "cloud"}:
        raise ValueError("kind must be one of: computer, server, cloud")

    query = {
        "server_url": normalized_server_url(server_url),
        "kind": kind,
    }
    profile = str(profile or "").strip()
    project = str(project or "").strip()
    token = str(token or "").strip()
    if profile:
        query["profile"] = profile
    if project:
        query["project"] = project
    if token:
        query["token"] = token
    return f"taste://connect?{urlencode(query)}"


def build_connect_page_url(
    *,
    server_url: str,
    profile: str = "",
    kind: str = "server",
    project: str = "",
) -> str:
    kind = str(kind or "server").strip().lower()
    if kind not in {"computer", "server", "cloud"}:
        raise ValueError("kind must be one of: computer, server, cloud")

    base_url = normalized_server_url(server_url)
    query = {
        "server_url": base_url,
        "kind": kind,
    }
    profile = str(profile or "").strip()
    project = str(project or "").strip()
    if profile:
        query["profile"] = profile
    if project:
        query["project"] = project
    return f"{base_url}/mobile/connect?{urlencode(query)}"


def redacted_connection_link(link: str) -> str:
    parts = urlsplit(str(link or ""))
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        query.append((key, "REDACTED" if key in SENSITIVE_QUERY_KEYS and value else value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def build_connection_payload(
    *,
    server_url: str,
    profile: str = "",
    kind: str = "server",
    project: str = "",
    token: str = "",
) -> dict[str, object]:
    link = build_connection_link(
        server_url=server_url,
        profile=profile,
        kind=kind,
        project=project,
        token=token,
    )
    qr = qr_svg(link)
    return {
        "link": link,
        "redacted_link": redacted_connection_link(link),
        "qr_svg": qr,
        "qr_svg_data_url": qr_svg_data_url(link),
        "connect_page_url": build_connect_page_url(
            server_url=server_url,
            profile=profile,
            kind=kind,
            project=project,
        ),
        "server_url": normalized_server_url(server_url),
        "includes_token": bool(str(token or "").strip()),
        "warning": "Anyone with this link can use the included server token." if str(token or "").strip() else "",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a TASTE iOS taste://connect deep link.")
    parser.add_argument("--server-url", required=True, help="TASTE server URL reachable from the iPhone, or 'auto' to use this machine's LAN IP.")
    parser.add_argument("--port", type=int, default=int(os.environ.get("WEB_PORT", "8765") or 8765), help="Port used when --server-url is auto.")
    parser.add_argument("--profile", default="TASTE Server", help="Connection profile name shown in the iOS app.")
    parser.add_argument("--kind", choices=["computer", "server", "cloud"], default="server")
    parser.add_argument("--project", default="", help="Optional project ID to select after import.")
    parser.add_argument("--token", default=os.environ.get("TASTE_SERVER_TOKEN", os.environ.get("TASTE_ACCESS_TOKEN", "")))
    parser.add_argument("--json", action="store_true", help="Print a JSON object instead of the link only.")
    args = parser.parse_args(argv)

    server_url = resolve_server_url(args.server_url, port=args.port)
    payload = build_connection_payload(
        server_url=server_url,
        profile=args.profile,
        kind=args.kind,
        project=args.project,
        token=args.token,
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(str(payload["link"]))
        if payload["includes_token"]:
            print(str(payload["warning"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
