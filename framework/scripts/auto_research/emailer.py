from __future__ import annotations

import html
import re
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Callable

from auto_research.models import AppConfig, EmailJobRequest
from auto_research.storage import read_json, run_dir, write_json


LogFn = Callable[[str], None]
CancelFn = Callable[[], bool]


DEFAULT_EMAIL_ARTIFACTS = ["find.md", "read.md", "idea.md", "plan.md", "biorxiv.md", "nature.md", "science.md", "hf.md", "github.md", "source_status.md"]


def _escape(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def _inline_markdown(text: str) -> str:
    escaped = _escape(text)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"`(.+?)`", r"<code>\1</code>", escaped)
    escaped = re.sub(
        r"\[([^\]]+)\]\((https?://[^)]+)\)",
        r'<a href="\2" target="_blank" rel="noreferrer">\1</a>',
        escaped,
    )
    url_pattern = r"(?<![\"'=])(https?://[^\s<]+)"
    return re.sub(url_pattern, r'<a href="\1" target="_blank" rel="noreferrer">\1</a>', escaped)


def markdown_to_html(markdown: str) -> str:
    lines = markdown.splitlines()
    output: list[str] = []
    in_list = False

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            output.append("</ul>")
            in_list = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            close_list()
            continue
        heading = re.match(r"^(#{1,4})\s+(.+)$", stripped)
        if heading:
            close_list()
            level = len(heading.group(1))
            output.append(f"<h{level}>{_inline_markdown(heading.group(2))}</h{level}>")
            continue
        bullet = re.match(r"^[-*]\s+(.+)$", stripped)
        if bullet:
            if not in_list:
                output.append("<ul>")
                in_list = True
            output.append(f"<li>{_inline_markdown(bullet.group(1))}</li>")
            continue
        close_list()
        output.append(f"<p>{_inline_markdown(stripped)}</p>")
    close_list()
    return "\n".join(output)


def _ranking_html(directory: Path) -> str:
    data = read_json(directory / "find_results.json", {})
    ranking = data.get("screened_ranking")
    if not isinstance(ranking, list):
        ranking = [
            item for item in data.get("evaluated_candidates", [])
            if float(item.get("fit_score") or 0) > 6
        ]
        ranking.sort(key=lambda row: float(row.get("score") or 0), reverse=True)
    if not ranking:
        return ""
    rows = []
    for index, item in enumerate(ranking, 1):
        directions = item.get("hit_directions", [])
        if isinstance(directions, list):
            directions_text = ", ".join(str(direction) for direction in directions)
        else:
            directions_text = str(directions or "")
        title = _escape(item.get("title", "Untitled"))
        url = str(item.get("url") or item.get("pdf_url") or "")
        title_html = f'<a href="{_escape(url)}">{title}</a>' if url else title
        rows.append(
            "<tr>"
            f"<td>{index}</td>"
            f"<td>{title_html}</td>"
            f"<td>{_escape(item.get('venue'))} {_escape(item.get('year'))}</td>"
            f"<td>{_escape(item.get('fit_score'))}</td>"
            f"<td>{_escape(item.get('diversity_score'))}</td>"
            f"<td>{_escape(item.get('score'))}</td>"
            f"<td>{_escape(directions_text)}</td>"
            f"<td>{_escape(item.get('fit_explanation') or item.get('reason'))}</td>"
            "</tr>"
        )
    return (
        "<section>"
        "<h2>筛选后完整排名 / Full Screened Ranking</h2>"
        "<p>仅包含 fit_score &gt; 6 的候选，并按最终 score 降序排列。</p>"
        "<table><thead><tr>"
        "<th>#</th><th>Title</th><th>Venue/Year</th><th>Fit</th><th>Diversity</th><th>Score</th><th>Hit Directions</th><th>Explanation</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></section>"
    )


def _email_shell(title: str, body: str) -> str:
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: Arial, sans-serif; color: #18212f; line-height: 1.55; background: #f5f7fa; margin: 0; padding: 24px; }}
    .wrap {{ max-width: 980px; margin: 0 auto; background: #ffffff; border: 1px solid #d7dee7; border-radius: 10px; padding: 24px; }}
    h1, h2, h3 {{ color: #174848; line-height: 1.25; }}
    section {{ border-top: 1px solid #e0e6ed; padding-top: 16px; margin-top: 18px; }}
    code {{ background: #eef2f5; border-radius: 4px; padding: 1px 4px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ border: 1px solid #d7dee7; padding: 7px; text-align: left; vertical-align: top; }}
    th {{ background: #eef2f5; }}
    a {{ color: #1e5a5a; }}
    .path {{ color: #607083; font-size: 12px; word-break: break-all; }}
  </style>
</head>
<body><div class="wrap"><h1>{_escape(title)}</h1>{body}</div></body>
</html>"""


def build_run_email_html(request: EmailJobRequest) -> str:
    directory = run_dir(request.run_id)
    artifact_names = request.artifact_names or DEFAULT_EMAIL_ARTIFACTS
    sections: list[str] = []
    if request.include_ranking:
        ranking = _ranking_html(directory)
        if ranking:
            sections.append(ranking)
    for name in artifact_names:
        path = directory / name
        if not path.exists() or path.suffix.lower() != ".md":
            continue
        content = path.read_text(encoding="utf-8")
        sections.append(
            "<section>"
            f"<h2>{_escape(name)}</h2>"
            f'<p class="path">{_escape(str(path))}</p>'
            f"{markdown_to_html(content)}"
            "</section>"
        )
    if not sections:
        sections.append("<p>No Markdown artifacts were available for this run.</p>")
    subject = request.subject or f"Report: {request.run_id}"
    return _email_shell(subject, "\n".join(sections))


def _resolve_receivers(request: EmailJobRequest, config: AppConfig) -> list[str]:
    receivers = request.receivers or config.email.receivers
    return [item.strip() for item in receivers if item.strip()]


def send_run_email(
    request: EmailJobRequest,
    config: AppConfig,
    log: LogFn = print,
    should_cancel: CancelFn = lambda: False,
) -> dict:
    if should_cancel():
        raise RuntimeError("Email job cancelled before SMTP send.")
    email_config = config.email
    receivers = _resolve_receivers(request, config)
    if not email_config.smtp_server or not email_config.sender or not email_config.smtp_password:
        raise ValueError("SMTP server, sender, and password are required.")
    if not receivers:
        raise ValueError("At least one email receiver is required.")

    subject = request.subject or f"Report: {request.run_id}"
    html_body = build_run_email_html(request)
    message = MIMEText(html_body, "html", "utf-8")
    message["Subject"] = subject
    message["From"] = email_config.sender
    message["To"] = ", ".join(receivers)

    port = int(email_config.smtp_port or 465)
    log(f"Sending rendered HTML email to {len(receivers)} receiver(s) via {email_config.smtp_server}:{port}")
    try:
        if port == 465:
            with smtplib.SMTP_SSL(email_config.smtp_server, port, timeout=30) as server:
                server.login(email_config.sender, email_config.smtp_password)
                server.sendmail(email_config.sender, receivers, message.as_string())
        else:
            with smtplib.SMTP(email_config.smtp_server, port, timeout=30) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(email_config.sender, email_config.smtp_password)
                server.sendmail(email_config.sender, receivers, message.as_string())
    except smtplib.SMTPException as exc:
        raise RuntimeError(f"SMTP send failed: {exc}") from exc

    sent_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    report = {
        "run_id": request.run_id,
        "sent_at": sent_at,
        "subject": subject,
        "receivers": receivers,
        "artifact_names": request.artifact_names or DEFAULT_EMAIL_ARTIFACTS,
        "include_ranking": request.include_ranking,
        "smtp_server": email_config.smtp_server,
        "smtp_port": port,
        "sender": email_config.sender,
    }
    write_json(run_dir(request.run_id) / "email_report.json", report)
    log("Email sent successfully.")
    return report
