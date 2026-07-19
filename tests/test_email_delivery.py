from __future__ import annotations

from types import SimpleNamespace


class CapturingServerEmailSender:
    configured = True
    host = "smtp.system.test"
    port = 465
    from_address = "taste@system.test"

    def __init__(self):
        self.messages: list[dict] = []

    def send_email(self, recipients, subject, text_body, html_body=None):
        self.messages.append({
            "recipients": recipients,
            "subject": subject,
            "text_body": text_body,
            "html_body": html_body,
        })


def test_run_email_uses_system_sender_and_only_user_receivers(tmp_path, monkeypatch):
    from contracts.web_models import AppConfig, EmailJobRequest
    import integrations.emailer as emailer

    run_path = tmp_path / "find_test"
    run_path.mkdir()
    (run_path / "find.md").write_text("# Result\n\nDelivered.", encoding="utf-8")
    monkeypatch.setattr(emailer, "run_dir", lambda _run_id: run_path)

    config = AppConfig(email={
        "receivers": ["configured@example.test"],
        "smtp_server": "",
        "sender": "",
        "smtp_password": "",
    })
    sender = CapturingServerEmailSender()
    report = emailer.send_run_email(
        EmailJobRequest(run_id="find_test", subject="Finished"),
        config,
        sender,
        log=lambda _message: None,
    )

    assert sender.messages[0]["recipients"] == ["configured@example.test"]
    assert sender.messages[0]["subject"] == "Finished"
    assert "Delivered." in sender.messages[0]["html_body"]
    assert report["smtp_server"] == "smtp.system.test"
    assert report["sender"] == "taste@system.test"
    assert (run_path / "email_report.json").is_file()


def test_auto_and_manual_run_email_share_auth_smtp_sender(monkeypatch):
    from contracts.web_models import AppConfig, EmailJobRequest
    import auto_research.web.server as server

    config = AppConfig(email={
        "receivers": ["recipient@example.test"],
        "auto_send_enabled": True,
        "auto_send_stages": ["find", "read", "idea", "plan"],
    })
    sender = CapturingServerEmailSender()
    calls: list[tuple[str, object]] = []

    def fake_send_run_email(request, _config, email_sender, log=None, should_cancel=None):
        calls.append((request.run_id, email_sender))
        return {"run_id": request.run_id}

    def fake_start_job(stage, fn):
        assert stage == "email"
        result = fn(lambda _message: None, lambda: False, lambda *_args: None)
        return SimpleNamespace(as_dict=lambda: {"stage": stage, "result": result})

    monkeypatch.setattr(server, "AUTH_EMAIL_SENDER", sender)
    monkeypatch.setattr(server, "load_config", lambda: config)
    monkeypatch.setattr(server, "send_run_email", fake_send_run_email)
    monkeypatch.setattr(server, "start_job", fake_start_job)
    monkeypatch.setattr(server, "_account_owns_run", lambda _run_id: True)

    server._auto_email_after_success("find", {"run_id": "find_auto"})
    response = server.api_email(EmailJobRequest(run_id="find_manual"))

    assert response == {"status": "done", "run_id": "find_manual"}
    assert calls == [("find_auto", sender), ("find_manual", sender)]


def test_email_jobs_are_hidden_auxiliary_operations_not_paper_tasks():
    import auto_research.web.server as server

    job = server.JobState("email_current", "email")
    payload = job.as_dict()
    assert server._public_taste_stage("email") == "email"
    assert payload["stage"] == "email"
    assert payload["internal"] is True
    assert payload["display"] == "hidden"

    legacy = server.JobState.from_dict({
        "job_id": "email_legacy",
        "stage": "paper",
        "status": "done",
        "logs": [],
        "result": {"run_id": "find_legacy"},
    })
    assert legacy.stage == "email"
    assert legacy.internal is True
    assert legacy.display == "hidden"
