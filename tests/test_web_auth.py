from __future__ import annotations

import asyncio
import json
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient


class CapturingEmailSender:
    configured = True

    def __init__(self):
        self.codes: dict[str, str] = {}
        self.purposes: dict[str, str] = {}

    def send_verification_code(
        self,
        recipient: str,
        code: str,
        expires_in: int,
        purpose: str = "register",
    ) -> None:
        assert expires_in > 0
        self.codes[recipient.casefold()] = code
        self.purposes[recipient.casefold()] = purpose


def configure_api_auth(server, tmp_path, monkeypatch):
    from auto_research.web.auth import AuthStore

    sender = CapturingEmailSender()
    store = AuthStore(tmp_path / "auth.sqlite3")
    monkeypatch.setattr(server, "AUTH_STORE", store)
    monkeypatch.setattr(server, "AUTH_EMAIL_SENDER", sender)
    return store, sender


def register_account(client: TestClient, sender: CapturingEmailSender, username: str, email: str, password: str):
    code_response = client.post("/api/auth/verification-code", json={"email": email})
    assert code_response.status_code == 200, code_response.text
    response = client.post(
        "/api/auth/register",
        json={
            "username": username,
            "email": email,
            "password": password,
            "verification_code": sender.codes[email.casefold()],
        },
    )
    assert response.status_code == 200, response.text
    return response


def test_auth_store_hashes_passwords_and_expires_sessions(tmp_path):
    from auto_research.web.auth import AuthError, AuthStore

    store = AuthStore(tmp_path / "auth.sqlite3")
    verification = store.begin_email_verification("Alice@example.com", "test-client")
    user = store.register("alice", "Alice@example.com", "correct-horse", verification.code)
    assert store.authenticate("ALICE", "correct-horse") == user
    assert store.authenticate("alice@example.com", "correct-horse") == user
    assert store.authenticate("alice", "wrong-password") is None
    token = store.create_session(user)
    assert store.user_for_session(token) == user
    store.delete_session(token)
    assert store.user_for_session(token) is None

    oldest = store.create_session(user)
    middle = store.create_session(user)
    newest = store.create_session(user)
    assert store.user_for_session(oldest) is None
    assert store.user_for_session(middle) == user
    assert store.user_for_session(newest) == user

    try:
        duplicate = store.begin_email_verification("another@example.com", "test-client")
        store.register("alice", "another@example.com", "another-password", duplicate.code)
    except AuthError as exc:
        assert "已注册" in str(exc)
    else:
        raise AssertionError("duplicate usernames must be rejected")


def test_session_limit_holds_for_concurrent_logins(tmp_path):
    from auto_research.web.auth import AuthStore

    store = AuthStore(tmp_path / "auth.sqlite3")
    verification = store.begin_email_verification("concurrent@example.com", "test-client")
    user = store.register("concurrent", "concurrent@example.com", "correct-horse", verification.code)
    with ThreadPoolExecutor(max_workers=6) as executor:
        tokens = list(executor.map(lambda _: store.create_session(user), range(6)))
    assert sum(store.user_for_session(token) == user for token in tokens) == 2


def test_email_verification_rate_limit_and_email_login(tmp_path, monkeypatch):
    import auto_research.web.server as server

    _, sender = configure_api_auth(server, tmp_path, monkeypatch)
    client = TestClient(server.app)
    email = "verified@example.com"
    assert client.post(
        "/api/auth/register",
        json={"username": "verified", "email": email, "password": "password-ok", "verification_code": "123456"},
    ).status_code == 400

    assert client.post("/api/auth/verification-code", json={"email": email}).status_code == 200
    limited = client.post("/api/auth/verification-code", json={"email": email})
    assert limited.status_code == 429
    assert limited.json()["retry_after"] > 0

    code = sender.codes[email]
    wrong_code = ("0" if code[0] != "0" else "1") + code[1:]
    wrong = client.post(
        "/api/auth/register",
        json={"username": "verified", "email": email, "password": "password-ok", "verification_code": wrong_code},
    )
    assert wrong.status_code == 400
    response = client.post(
        "/api/auth/register",
        json={"username": "verified", "email": email, "password": "password-ok", "verification_code": code},
    )
    assert response.status_code == 200
    assert response.json()["user"]["email"] == email

    assert client.post("/api/auth/logout").status_code == 200
    login_response = client.post(
        "/api/auth/login",
        json={"identifier": "VERIFIED@example.com", "password": "password-ok"},
    )
    assert login_response.status_code == 200
    assert login_response.json()["user"]["username"] == "verified"


def test_email_can_only_register_one_account_case_insensitively(tmp_path):
    from auto_research.web.auth import AuthError, AuthStore

    store = AuthStore(tmp_path / "auth.sqlite3")
    verification = store.begin_email_verification("Unique@Example.com", "test-client")
    store.register("first_user", "Unique@Example.com", "password-one", verification.code)

    try:
        store.begin_email_verification("unique@example.com", "another-client")
    except AuthError as exc:
        assert "已注册" in str(exc)
    else:
        raise AssertionError("one normalized email must not register more than one account")

    with store._connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM users WHERE email_key = ?",
            ("unique@example.com",),
        ).fetchone()[0] == 1


def test_password_reset_code_is_purpose_bound_one_time_and_expires(tmp_path):
    from auto_research.web.auth import (
        AuthError,
        AuthStore,
        VERIFICATION_PURPOSE_PASSWORD_RESET,
    )

    store = AuthStore(tmp_path / "auth.sqlite3")
    registration = store.begin_email_verification("reset@example.com", "test-client")
    user = store.register("reset_user", "reset@example.com", "password-old", registration.code)
    session = store.create_session(user)

    reset = store.begin_email_verification(
        "RESET@example.com",
        "test-client",
        VERIFICATION_PURPOSE_PASSWORD_RESET,
    )
    try:
        store.register("another_user", "reset@example.com", "password-other", reset.code)
    except AuthError as exc:
        assert "先获取" in str(exc)
    else:
        raise AssertionError("a password-reset code must not be accepted for registration")

    store.reset_password("reset@example.com", "password-new", reset.code)
    assert store.authenticate("reset@example.com", "password-old") is None
    assert store.authenticate("reset@example.com", "password-new") == user
    assert store.user_for_session(session) is None
    try:
        store.reset_password("reset@example.com", "password-newer", reset.code)
    except AuthError as exc:
        assert "先获取" in str(exc)
    else:
        raise AssertionError("a used password-reset code must be invalid immediately")

    expired = store.begin_email_verification(
        "reset@example.com",
        "test-client",
        VERIFICATION_PURPOSE_PASSWORD_RESET,
    )
    with store._connect() as connection:
        connection.execute(
            "UPDATE email_verifications SET expires_at = ? WHERE email_key = ?",
            ("2000-01-01T00:00:00+00:00", "reset@example.com"),
        )
    try:
        store.reset_password("reset@example.com", "password-expired", expired.code)
    except AuthError as exc:
        assert "已过期" in str(exc)
    else:
        raise AssertionError("an expired password-reset code must be rejected")
    with store._connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM email_verifications WHERE email_key = ?",
            ("reset@example.com",),
        ).fetchone() is None


def test_password_reset_api_uses_registered_email_and_new_password(tmp_path, monkeypatch):
    import auto_research.web.server as server

    _, sender = configure_api_auth(server, tmp_path, monkeypatch)
    client = TestClient(server.app)
    register_account(client, sender, "api_reset", "api-reset@example.com", "password-old")
    assert client.post("/api/auth/logout").status_code == 200

    missing = client.post(
        "/api/auth/password-reset/verification-code",
        json={"email": "missing@example.com"},
    )
    assert missing.status_code == 400
    assert "尚未注册" in missing.json()["error"]

    code_response = client.post(
        "/api/auth/password-reset/verification-code",
        json={"email": "API-RESET@example.com"},
    )
    assert code_response.status_code == 200
    assert sender.purposes["api-reset@example.com"] == "password_reset"
    reset_response = client.post(
        "/api/auth/password-reset",
        json={
            "email": "api-reset@example.com",
            "password": "password-new",
            "verification_code": sender.codes["api-reset@example.com"],
        },
    )
    assert reset_response.status_code == 200
    assert client.post(
        "/api/auth/login",
        json={"identifier": "api_reset", "password": "password-old"},
    ).status_code == 401
    assert client.post(
        "/api/auth/login",
        json={"identifier": "api_reset", "password": "password-new"},
    ).status_code == 200


def test_verification_email_sender_loads_protected_runtime_config(tmp_path, monkeypatch):
    from auto_research.web.verification_email import VerificationEmailSender

    for key in (
        "TASTE_AUTH_SMTP_HOST",
        "TASTE_AUTH_SMTP_PORT",
        "TASTE_AUTH_SMTP_SECURITY",
        "TASTE_AUTH_SMTP_USERNAME",
        "TASTE_AUTH_SMTP_PASSWORD",
        "TASTE_AUTH_SMTP_PASSWORD_FILE",
        "TASTE_AUTH_SMTP_FROM",
        "TASTE_AUTH_SMTP_FROM_NAME",
    ):
        monkeypatch.delenv(key, raising=False)
    secret = tmp_path / "secrets" / "smtp_code"
    secret.parent.mkdir()
    secret.write_text("test-authorization-code", encoding="utf-8")
    config = tmp_path / "auth_smtp.json"
    config.write_text(json.dumps({
        "host": "smtp.example.test",
        "port": 465,
        "security": "ssl",
        "username": "taste@example.test",
        "password_file": "secrets/smtp_code",
        "from_address": "taste@example.test",
    }), encoding="utf-8")

    sender = VerificationEmailSender.from_env(config)
    assert sender.configured is True
    assert sender.host == "smtp.example.test"
    assert sender.password == "test-authorization-code"


def test_server_email_sender_reuses_authenticated_connection_across_mail_types(monkeypatch):
    import auto_research.web.verification_email as verification_email

    class FakeSMTP:
        instances = []

        def __init__(self, host, port, *, timeout, context):
            self.host = host
            self.port = port
            self.timeout = timeout
            self.context = context
            self.login_calls = []
            self.recipients = []
            self.subjects = []
            self.closed = False
            self.instances.append(self)

        def login(self, username, password):
            self.login_calls.append((username, password))

        def send_message(self, message):
            self.recipients.append(message["To"])
            self.subjects.append(message["Subject"])

        def close(self):
            self.closed = True

    monkeypatch.setattr(verification_email.smtplib, "SMTP_SSL", FakeSMTP)
    sender = verification_email.VerificationEmailSender(
        host="smtp.example.test",
        port=465,
        username="taste@example.test",
        password="authorization-code",
        from_address="taste@example.test",
    )

    sender.send_verification_code("first@example.test", "123456", 600)
    sender.send_email(
        ["second@example.test"],
        "TASTE report",
        "Report body",
        "<p>Report body</p>",
    )
    sender.send_verification_code("third@example.test", "654321", 600, "password_reset")

    assert len(FakeSMTP.instances) == 1
    assert FakeSMTP.instances[0].login_calls == [("taste@example.test", "authorization-code")]
    assert FakeSMTP.instances[0].recipients == [
        "first@example.test",
        "second@example.test",
        "third@example.test",
    ]
    assert FakeSMTP.instances[0].subjects == [
        "TASTE 注册验证码",
        "TASTE report",
        "TASTE 重置密码验证码",
    ]


def test_verification_email_sender_reconnects_once_after_stale_connection(monkeypatch):
    import auto_research.web.verification_email as verification_email

    class FakeSMTP:
        instances = []

        def __init__(self, host, port, *, timeout, context):
            self.send_count = 0
            self.closed = False
            self.instances.append(self)

        def login(self, username, password):
            pass

        def send_message(self, message):
            self.send_count += 1
            if self is self.instances[0] and self.send_count == 2:
                raise verification_email.smtplib.SMTPServerDisconnected("idle connection expired")

        def close(self):
            self.closed = True

    monkeypatch.setattr(verification_email.smtplib, "SMTP_SSL", FakeSMTP)
    sender = verification_email.VerificationEmailSender(
        host="smtp.example.test",
        port=465,
        username="taste@example.test",
        password="authorization-code",
        from_address="taste@example.test",
    )

    sender.send_verification_code("first@example.test", "123456", 600)
    sender.send_verification_code("second@example.test", "654321", 600)

    assert len(FakeSMTP.instances) == 2
    assert FakeSMTP.instances[0].closed is True
    assert FakeSMTP.instances[1].send_count == 1


def test_api_requires_login_and_filters_projects_by_account(tmp_path, monkeypatch):
    import auto_research.web.server as server
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    _, sender = configure_api_auth(server, tmp_path, monkeypatch)
    monkeypatch.setattr(server, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(server, "PROJECT_IDS_ROOT", projects_root)

    anonymous = TestClient(server.app)
    assert anonymous.get("/api/projects").status_code == 401

    alice = TestClient(server.app)
    bob = TestClient(server.app)
    alice_user = register_account(alice, sender, "alice", "alice@example.com", "password-a").json()["user"]
    bob_user = register_account(bob, sender, "bob", "bob@example.com", "password-b").json()["user"]
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
    _, sender = configure_api_auth(server, tmp_path, monkeypatch)
    monkeypatch.setattr(server, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "service-global-key-must-not-leak")
    alice = TestClient(server.app)
    bob = TestClient(server.app)
    alice_user = register_account(alice, sender, "alice2", "alice2@example.com", "password-a").json()["user"]
    bob_user = register_account(bob, sender, "bob2", "bob2@example.com", "password-b").json()["user"]

    alice_config = alice.get("/api/config").json()
    alice_config.update({
        "research_interest": "alice-only-interest",
        "provider": "openai_compatible",
        "base_url": "https://alice-llm.test/v1",
        "model": "alice-model",
        "api_key": "alice-secret",
    })
    assert alice.post("/api/config", json=alice_config).status_code == 200
    bob_config = bob.get("/api/config").json()
    assert bob_config["api_key_saved"] is False
    bob_config.update({
        "provider": "openai_compatible",
        "base_url": "https://bob-llm.test/v1",
        "model": "bob-model",
        "api_key": "bob-secret",
    })
    assert bob.post("/api/config", json=bob_config).status_code == 200
    assert alice.get("/api/config").json()["research_interest"] == "alice-only-interest"
    assert bob.get("/api/config").json()["research_interest"] != "alice-only-interest"

    alice_llm = tmp_path / "web" / ".runtime" / "accounts" / alice_user["id"] / "llm.local.json"
    bob_llm = tmp_path / "web" / ".runtime" / "accounts" / bob_user["id"] / "llm.local.json"
    assert json.loads(alice_llm.read_text(encoding="utf-8"))["api_key"] == "alice-secret"
    assert json.loads(bob_llm.read_text(encoding="utf-8"))["api_key"] == "bob-secret"
    assert stat.S_IMODE(alice_llm.stat().st_mode) == 0o600
    assert stat.S_IMODE(alice_llm.parent.stat().st_mode) == 0o700


def test_https_proxy_sets_secure_cookie_and_security_headers(tmp_path, monkeypatch):
    import auto_research.web.server as server
    _, sender = configure_api_auth(server, tmp_path, monkeypatch)
    client = TestClient(server.app, base_url="https://testserver")
    response = register_account(client, sender, "https_user", "https@example.com", "password-https")
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=lax" in cookie
    assert response.headers["strict-transport-security"] == "max-age=31536000"

    direct_http = TestClient(server.app)
    code_response = direct_http.post(
        "/api/auth/verification-code",
        headers={"X-Forwarded-Proto": "https"},
        json={"email": "http@example.com"},
    )
    assert code_response.status_code == 200
    spoofed = direct_http.post(
        "/api/auth/register",
        headers={"X-Forwarded-Proto": "https"},
        json={
            "username": "http_user",
            "email": "http@example.com",
            "password": "password-http",
            "verification_code": sender.codes["http@example.com"],
        },
    )
    assert "Secure" not in spoofed.headers["set-cookie"]
    assert "strict-transport-security" not in spoofed.headers


def test_logout_invalidates_session_and_websocket_requires_login(tmp_path, monkeypatch):
    import auto_research.web.server as server
    _, sender = configure_api_auth(server, tmp_path, monkeypatch)
    client = TestClient(server.app)
    register_account(client, sender, "logout_user", "logout@example.com", "password-logout")
    assert client.get("/api/auth/me").status_code == 200
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/me").status_code == 401

    class AnonymousWebSocket:
        cookies: dict[str, str] = {}

        def __init__(self):
            self.closed_with = None

        async def close(self, code: int):
            self.closed_with = code

    socket = AnonymousWebSocket()
    asyncio.run(server.ws_job(socket, "missing"))
    assert socket.closed_with == 4401
