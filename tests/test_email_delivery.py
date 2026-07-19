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
        run_path,
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
    calls: list[tuple[str, str, list[str], bool, object, object]] = []

    def fake_send_run_email(request, _config, email_sender, artifact_directory, log=None, should_cancel=None):
        calls.append((request.run_id, request.artifact_scope, request.artifact_names, request.include_ranking, email_sender, artifact_directory))
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
    monkeypatch.setattr(server, "_email_artifact_directory", lambda _request, _project_hint="": "project-artifacts")

    for stage in ["find", "read", "idea", "plan"]:
        server._auto_email_after_success(stage, {"run_id": f"{stage}_auto", "project": "project"})
    response = server.api_email(EmailJobRequest(run_id="find_manual"))

    assert response == {"status": "done", "run_id": "find_manual"}
    assert calls == [
        ("find_auto", "find", ["find.md", "source_status.md"], True, sender, "project-artifacts"),
        ("read_auto", "read", ["read.md"], False, sender, "project-artifacts"),
        ("idea_auto", "idea", ["idea.md"], False, sender, "project-artifacts"),
        ("plan_auto", "plan", ["plan.md"], False, sender, "project-artifacts"),
        ("find_manual", "find", ["find.md", "source_status.md"], True, sender, "project-artifacts"),
    ]


def test_project_stage_email_uses_matching_markdown_and_ranking_only_for_find(tmp_path, monkeypatch):
    from contracts.web_models import AppConfig, EmailJobRequest
    import integrations.emailer as emailer
    import auto_research.web.server as server

    project_artifacts = tmp_path / "project" / "planning" / "finding"
    project_artifacts.mkdir(parents=True)
    (project_artifacts / "find.md").write_text("# Find-only marker", encoding="utf-8")
    (project_artifacts / "source_status.md").write_text("# Source-only marker", encoding="utf-8")
    (project_artifacts / "read.md").write_text("# Read-only marker", encoding="utf-8")
    (project_artifacts / "idea.md").write_text("# Idea-only marker", encoding="utf-8")
    (project_artifacts / "plan.md").write_text("# Plan-only marker", encoding="utf-8")
    (project_artifacts / "find_results.json").write_text(
        '{"screened_ranking":[{"title":"Ranking-only marker","fit_score":5,"score":5}]}',
        encoding="utf-8",
    )
    run_path = tmp_path / "runtime" / "find_test"
    run_path.mkdir(parents=True)
    monkeypatch.setattr(emailer, "run_dir", lambda _run_id: run_path)

    sender = CapturingServerEmailSender()
    monkeypatch.setattr(server, "AUTH_EMAIL_SENDER", sender)
    monkeypatch.setattr(server, "load_config", lambda: AppConfig(email={"receivers": ["recipient@example.test"]}))
    monkeypatch.setattr(server, "_account_owns_run", lambda _run_id: True)
    monkeypatch.setattr(server, "_email_artifact_directory", lambda _request, _project_hint="": project_artifacts)

    expected_markers = {
        "find": ["Find-only marker", "Source-only marker", "Ranking-only marker"],
        "read": ["Read-only marker"],
        "idea": ["Idea-only marker"],
        "plan": ["Plan-only marker"],
    }
    excluded_markers = set(marker for markers in expected_markers.values() for marker in markers)
    for scope, expected in expected_markers.items():
        response = server.api_email(EmailJobRequest(
            run_id="find_test",
            artifact_scope=scope,
            artifact_names=["find.md", "read.md", "idea.md", "plan.md"],
            include_ranking=True,
        ))
        assert response == {"status": "done", "run_id": "find_test"}
        html_body = sender.messages[-1]["html_body"]
        for marker in expected:
            assert marker in html_body
        for marker in excluded_markers.difference(expected):
            assert marker not in html_body
        assert "仅包含 fit_score" not in html_body


def test_project_email_directory_rejects_stale_or_unvalidated_read_artifact(tmp_path, monkeypatch):
    from contracts.web_models import EmailJobRequest
    import auto_research.web.server as server

    run_id = "find_current"
    project_root = tmp_path / "project"
    taste_dir = project_root / "planning" / "finding"
    state_dir = project_root / "state"
    taste_dir.mkdir(parents=True)
    state_dir.mkdir()
    (taste_dir / "read.md").write_text("# Current read", encoding="utf-8")
    (taste_dir / "read_results.json").write_text(
        '{"run_id":"find_current","public_final_artifact_present":true}',
        encoding="utf-8",
    )
    (state_dir / "current_find_claude_reading_validation.json").write_text(
        '{"run_id":"find_current","valid":true}',
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "_project_context_for_find_run", lambda _run_id: ("project", project_root))

    request = EmailJobRequest(run_id=run_id, artifact_scope="read")
    assert server._email_artifact_directory(request) == taste_dir

    stale_request = request.model_copy(update={"run_id": "find_stale"})
    try:
        server._email_artifact_directory(stale_request)
    except ValueError as exc:
        assert "current Find run" in str(exc)
    else:
        raise AssertionError("stale project artifact must not be emailed")

    (state_dir / "current_find_claude_reading_validation.json").write_text(
        '{"run_id":"find_current","valid":false}',
        encoding="utf-8",
    )
    try:
        server._email_artifact_directory(request)
    except ValueError as exc:
        assert "read artifact" in str(exc)
    else:
        raise AssertionError("unvalidated Read artifact must not be emailed")


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
