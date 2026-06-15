import importlib.util
from pathlib import Path
from urllib.parse import parse_qs, urlparse


def load_mobile_connection_link_module():
    root = Path(__file__).resolve().parents[3]
    script = root / "scripts" / "mobile_connection_link.py"
    spec = importlib.util.spec_from_file_location("mobile_connection_link", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_build_connection_link_encodes_profile_project_and_optional_token():
    module = load_mobile_connection_link_module()

    link = module.build_connection_link(
        server_url=" http://192.168.1.20:8765/ ",
        profile="Lab Mac",
        kind="computer",
        project="demo project",
        token="server-token",
    )

    parsed = urlparse(link)
    query = parse_qs(parsed.query)

    assert parsed.scheme == "taste"
    assert parsed.netloc == "connect"
    assert query["server_url"] == ["http://192.168.1.20:8765"]
    assert query["profile"] == ["Lab Mac"]
    assert query["kind"] == ["computer"]
    assert query["project"] == ["demo project"]
    assert query["token"] == ["server-token"]
    assert " " not in link


def test_auto_server_url_uses_detected_lan_ip_and_json_redacts_token(monkeypatch):
    module = load_mobile_connection_link_module()
    monkeypatch.setattr(module, "detect_lan_ip", lambda: "192.168.1.42")

    server_url = module.resolve_server_url("auto", port=8765)
    payload = module.build_connection_payload(
        server_url=server_url,
        profile="Lab Mac",
        kind="computer",
        project="demo",
        token="server-token",
    )

    assert server_url == "http://192.168.1.42:8765"
    assert payload["link"].startswith("taste://connect?")
    assert "token=server-token" in payload["link"]
    assert payload["redacted_link"].startswith("taste://connect?")
    assert "server-token" not in payload["redacted_link"]
    assert "token=REDACTED" in payload["redacted_link"]
    assert payload["server_url"] == "http://192.168.1.42:8765"
    assert payload["includes_token"] is True


def test_connection_payload_includes_token_free_connect_page_url():
    module = load_mobile_connection_link_module()

    payload = module.build_connection_payload(
        server_url="http://192.168.1.42:8765",
        profile="Lab Mac",
        kind="computer",
        project="demo",
        token="server-token",
    )

    connect_page_url = payload["connect_page_url"]
    parsed = urlparse(connect_page_url)
    query = parse_qs(parsed.query)

    assert connect_page_url.startswith("http://192.168.1.42:8765/mobile/connect?")
    assert parsed.scheme == "http"
    assert parsed.netloc == "192.168.1.42:8765"
    assert parsed.path == "/mobile/connect"
    assert query["server_url"] == ["http://192.168.1.42:8765"]
    assert query["profile"] == ["Lab Mac"]
    assert query["kind"] == ["computer"]
    assert query["project"] == ["demo"]
    assert "server-token" not in connect_page_url
    assert "token=" not in connect_page_url


def test_connection_payload_includes_inline_qr_svg_for_phone_scanning():
    module = load_mobile_connection_link_module()

    payload = module.build_connection_payload(
        server_url="http://192.168.1.42:8765",
        profile="Lab Mac",
        kind="computer",
        project="demo",
    )

    qr_svg = payload["qr_svg"]
    qr_data_url = payload["qr_svg_data_url"]

    assert str(qr_svg).startswith("<svg")
    assert "viewBox=" in qr_svg
    assert "taste://connect" not in qr_svg
    assert str(qr_data_url).startswith("data:image/svg+xml;base64,")
