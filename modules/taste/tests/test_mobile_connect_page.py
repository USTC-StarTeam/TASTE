from fastapi.testclient import TestClient

from auto_research.web import server


def test_mobile_connect_page_builds_link_without_exposing_server_token(monkeypatch):
    monkeypatch.setenv("TASTE_SERVER_ACCESS_TOKEN", "server-secret")
    monkeypatch.delenv("TASTE_SERVER_TOKEN", raising=False)

    with TestClient(server.app) as client:
        response = client.get(
            "/mobile/connect",
            params={
                "server_url": "http://192.168.1.42:8765",
                "profile": "Lab Mac",
                "kind": "computer",
                "project": "ios_e2e_mobile_app",
            },
        )

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert response.headers["cache-control"] == "no-store"
    assert "TASTE Mobile Connect" in response.text
    assert "http://192.168.1.42:8765" in response.text
    assert "ios_e2e_mobile_app" in response.text
    assert "taste://connect" in response.text
    assert 'id="connection-qr"' in response.text
    assert "data:image/svg+xml;base64," in response.text
    assert "Scan this QR" in response.text
    assert "Server access token" in response.text
    assert "server-secret" not in response.text


def test_mobile_connect_qr_endpoint_returns_inline_svg_without_cache(monkeypatch):
    monkeypatch.setenv("TASTE_SERVER_ACCESS_TOKEN", "server-secret")

    link = "taste://connect?server_url=http%3A%2F%2F192.168.1.42%3A8765&kind=computer&profile=Lab+Mac"
    with TestClient(server.app) as client:
        response = client.post("/mobile/connect/qr", json={"link": link})

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["qr_svg"].startswith("<svg")
    assert payload["qr_svg_data_url"].startswith("data:image/svg+xml;base64,")
    assert "server-secret" not in payload["qr_svg"]
    assert "server-secret" not in payload["qr_svg_data_url"]
