from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient


def test_auth_store_hashes_passwords_and_expires_sessions(tmp_path):
    from auto_research.web.auth import AuthError, AuthStore

    store = AuthStore(tmp_path / "auth.sqlite3")
    user = store.register("alice", "correct-horse")
    assert store.authenticate("ALICE", "correct-horse") == user
    assert store.authenticate("alice", "wrong-password") is None
    token = store.create_session(user)
    assert store.user_for_session(token) == user
    store.delete_session(token)
    assert store.user_for_session(token) is None

    try:
        store.register("alice", "another-password")
    except AuthError as exc:
        assert "已注册" in str(exc)
    else:
        raise AssertionError("duplicate usernames must be rejected")


def test_api_requires_login_and_filters_projects_by_account(tmp_path, monkeypatch):
    import auto_research.web.server as server
    from auto_research.web.auth import AuthStore

    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    monkeypatch.setattr(server, "AUTH_STORE", AuthStore(tmp_path / "auth.sqlite3"))
    monkeypatch.setattr(server, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(server, "PROJECT_IDS_ROOT", projects_root)

    anonymous = TestClient(server.app)
    assert anonymous.get("/api/projects").status_code == 401

    alice = TestClient(server.app)
    bob = TestClient(server.app)
    alice_user = alice.post("/api/auth/register", json={"username": "alice", "password": "password-a"}).json()["user"]
    bob_user = bob.post("/api/auth/register", json={"username": "bob", "password": "password-b"}).json()["user"]
    alice_project = f"u_{alice_user['id']}__demo"
    bob_project = f"u_{bob_user['id']}__demo"
    for project in (alice_project, bob_project):
        root = projects_root / project
        root.mkdir()
        (root / "project.json").write_text(json.dumps({"name": project, "topic": project}), encoding="utf-8")

    monkeypatch.setattr(server, "list_projects", lambda: [
        {"id": alice_project, "name": alice_project, "topic": "alice topic", "path": str(projects_root / alice_project)},
        {"id": bob_project, "name": bob_project, "topic": "bob topic", "path": str(projects_root / bob_project)},
    ])
    monkeypatch.setattr(server, "project_summary", lambda project, compact=True: {
        "project": project,
        "path": str(projects_root / project),
        "config": {"name": project},
        "state": {},
        "artifacts": [],
    })

    assert [row["id"] for row in alice.get("/api/projects").json()] == ["demo"]
    assert [row["id"] for row in bob.get("/api/projects").json()] == ["demo"]
    assert alice.get(f"/api/projects/{bob_project}").status_code == 404
    assert bob.get(f"/api/projects/{alice_project}").status_code == 404
    assert alice.get("/api/projects/demo").json()["project"] == "demo"


def test_account_configuration_is_isolated(tmp_path, monkeypatch):
    import auto_research.web.server as server
    from auto_research.web.auth import AuthStore

    monkeypatch.setattr(server, "AUTH_STORE", AuthStore(tmp_path / "auth.sqlite3"))
    monkeypatch.setattr(server, "WORKSPACE_ROOT", tmp_path)
    alice = TestClient(server.app)
    bob = TestClient(server.app)
    assert alice.post("/api/auth/register", json={"username": "alice2", "password": "password-a"}).status_code == 200
    assert bob.post("/api/auth/register", json={"username": "bob2", "password": "password-b"}).status_code == 200

    alice_config = alice.get("/api/config").json()
    alice_config["research_interest"] = "alice-only-interest"
    assert alice.post("/api/config", json=alice_config).status_code == 200
    assert alice.get("/api/config").json()["research_interest"] == "alice-only-interest"
    assert bob.get("/api/config").json()["research_interest"] != "alice-only-interest"


def test_https_proxy_sets_secure_cookie_and_security_headers(tmp_path, monkeypatch):
    import auto_research.web.server as server
    from auto_research.web.auth import AuthStore

    monkeypatch.setattr(server, "AUTH_STORE", AuthStore(tmp_path / "auth.sqlite3"))
    client = TestClient(server.app)
    response = client.post(
        "/api/auth/register",
        headers={"X-Forwarded-Proto": "https"},
        json={"username": "https_user", "password": "password-https"},
    )
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=lax" in cookie
    assert response.headers["strict-transport-security"] == "max-age=31536000"
