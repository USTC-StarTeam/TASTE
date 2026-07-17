from __future__ import annotations

import json
import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path


class VerificationEmailError(RuntimeError):
    pass


@dataclass(frozen=True)
class VerificationEmailSender:
    host: str
    port: int
    username: str
    password: str
    from_address: str
    from_name: str = "TASTE"
    security: str = "ssl"

    @classmethod
    def from_env(cls, config_path: Path | None = None) -> VerificationEmailSender:
        config: dict[str, object] = {}
        if config_path is not None and config_path.is_file():
            try:
                loaded = json.loads(config_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    config = loaded
            except (OSError, ValueError):
                config = {}

        def setting(env_name: str, config_name: str, default: object = "") -> str:
            if env_name in os.environ:
                return os.environ[env_name]
            return str(config.get(config_name, default) or "")

        host = setting("TASTE_AUTH_SMTP_HOST", "host").strip()
        username = setting("TASTE_AUTH_SMTP_USERNAME", "username").strip()
        password = setting("TASTE_AUTH_SMTP_PASSWORD", "password")
        password_file_value = setting("TASTE_AUTH_SMTP_PASSWORD_FILE", "password_file").strip()
        if not password and password_file_value:
            password_file = Path(password_file_value).expanduser()
            if not password_file.is_absolute() and config_path is not None:
                password_file = config_path.parent / password_file
            try:
                password = password_file.read_text(encoding="utf-8").strip()
            except OSError:
                password = ""
        from_address = setting("TASTE_AUTH_SMTP_FROM", "from_address").strip() or username
        security_value = setting("TASTE_AUTH_SMTP_SECURITY", "security").strip().lower()
        try:
            port = int(setting("TASTE_AUTH_SMTP_PORT", "port") or (465 if security_value != "starttls" else 587))
        except ValueError:
            port = 465
        security = security_value or ("ssl" if port == 465 else "starttls")
        return cls(
            host=host,
            port=port,
            username=username,
            password=password,
            from_address=from_address,
            from_name=setting("TASTE_AUTH_SMTP_FROM_NAME", "from_name", "TASTE").strip() or "TASTE",
            security=security,
        )

    @property
    def configured(self) -> bool:
        return bool(
            self.host
            and self.from_address
            and self.security in {"ssl", "starttls", "plain"}
            and 0 < self.port <= 65535
            and (not self.username or self.password)
        )

    def send_verification_code(self, recipient: str, code: str, expires_in: int) -> None:
        if not self.configured:
            raise VerificationEmailError("服务器尚未配置注册邮件服务，请联系管理员。")

        minutes = max(1, expires_in // 60)
        message = EmailMessage()
        message["Subject"] = "TASTE 注册验证码"
        message["From"] = formataddr((self.from_name, self.from_address))
        message["To"] = recipient
        message.set_content(
            f"你的 TASTE 注册验证码是：{code}\n\n验证码 {minutes} 分钟内有效。若非本人操作，请忽略此邮件。"
        )
        message.add_alternative(
            """
            <div style="font-family:system-ui,-apple-system,sans-serif;color:#172033;line-height:1.6">
              <h2 style="margin:0 0 16px">TASTE 注册验证码</h2>
              <p>你的注册验证码是：</p>
              <p style="font-size:30px;font-weight:700;letter-spacing:8px;margin:18px 0">{code}</p>
              <p>验证码 {minutes} 分钟内有效。若非本人操作，请忽略此邮件。</p>
            </div>
            """.format(code=code, minutes=minutes),
            subtype="html",
        )

        context = ssl.create_default_context()
        try:
            if self.security == "ssl":
                with smtplib.SMTP_SSL(self.host, self.port, timeout=15, context=context) as smtp:
                    self._authenticate_and_send(smtp, message)
            else:
                with smtplib.SMTP(self.host, self.port, timeout=15) as smtp:
                    if self.security == "starttls":
                        smtp.starttls(context=context)
                    self._authenticate_and_send(smtp, message)
        except (OSError, smtplib.SMTPException) as exc:
            raise VerificationEmailError("验证码邮件发送失败，请稍后重试。") from exc

    def _authenticate_and_send(self, smtp: smtplib.SMTP, message: EmailMessage) -> None:
        if self.username:
            smtp.login(self.username, self.password)
        smtp.send_message(message)
