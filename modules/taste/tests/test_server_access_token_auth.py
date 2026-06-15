from fastapi.testclient import TestClient

from auto_research.web import server


def test_server_access_token_is_optional_when_unconfigured(monkeypatch):
    monkeypatch.delenv("TASTE_SERVER_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("TASTE_SERVER_TOKEN", raising=False)

    with TestClient(server.app) as client:
        response = client.get("/health")

    assert response.status_code == 200


def test_server_access_token_protects_mobile_control_plane(monkeypatch):
    monkeypatch.setenv("TASTE_SERVER_ACCESS_TOKEN", "server-secret")
    monkeypatch.delenv("TASTE_SERVER_TOKEN", raising=False)

    with TestClient(server.app) as client:
        missing = client.get("/health")
        wrong = client.get("/api/config/meta", headers={"Authorization": "Bearer wrong-secret"})
        ok = client.get("/api/config/meta", headers={"Authorization": "Bearer server-secret"})

    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert missing.json()["error"] == "server_access_token_required"
    assert "server-secret" not in missing.text
    assert wrong.status_code == 401
    assert ok.status_code == 200


def test_config_meta_advertises_mobile_control_plane_capabilities(monkeypatch):
    monkeypatch.delenv("TASTE_SERVER_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("TASTE_SERVER_TOKEN", raising=False)

    with TestClient(server.app) as client:
        response = client.get("/api/config/meta")

    assert response.status_code == 200
    payload = response.json()
    assert payload["mobile_api_version"] >= 1
    assert "projects" in payload["mobile_capabilities"]
    assert "jobs" in payload["mobile_capabilities"]
    assert "runtime" in payload["mobile_capabilities"]
    assert "llm_config" in payload["mobile_capabilities"]
    assert "claude_latest_response" in payload["mobile_capabilities"]
    assert "remote_artifacts" in payload["mobile_capabilities"]
