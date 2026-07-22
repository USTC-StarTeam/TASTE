from __future__ import annotations

import json
import os
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from threading import Lock


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
    _connection: smtplib.SMTP | None = field(default=None, init=False, repr=False, compare=False)
    _connection_lock: Lock = field(default_factory=Lock, init=False, repr=False, compare=False)
    _ssl_context: ssl.SSLContext = field(default_factory=ssl.create_default_context, init=False, repr=False, compare=False)

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

    def send_email(
        self,
        recipients: list[str],
        subject: str,
        text_body: str,
        html_body: str | None = None,
    ) -> None:
        """Send all server mail through the configured, reusable SMTP session."""
        if not self.configured:
            raise VerificationEmailError("服务器尚未配置邮件服务，请联系管理员。")
        normalized_recipients = [recipient.strip() for recipient in recipients if recipient.strip()]
        if not normalized_recipients:
            raise VerificationEmailError("至少需要一个收件邮箱。")

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = formataddr((self.from_name, self.from_address))
        message["To"] = ", ".join(normalized_recipients)
        message.set_content(text_body)
        if html_body is not None:
            message.add_alternative(html_body, subtype="html")

        try:
            self._send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            raise VerificationEmailError("邮件发送失败，请稍后重试。") from exc

    def send_verification_code(
        self,
        recipient: str,
        code: str,
        expires_in: int,
        purpose: str = "register",
    ) -> None:
        if not self.configured:
            raise VerificationEmailError("服务器尚未配置邮件服务，请联系管理员。")

        minutes = max(1, expires_in // 60)
        resetting_password = purpose == "password_reset"
        action = "重置密码" if resetting_password else "注册"
        text_body = f"你的 TASTE {action}验证码是：{code}\n\n验证码 {minutes} 分钟内有效。若非本人操作，请忽略此邮件。"
        html_body = """
            <div style="font-family:system-ui,-apple-system,sans-serif;color:#172033;line-height:1.6">
              <h2 style="margin:0 0 16px">TASTE {action}验证码</h2>
              <p>你的{action}验证码是：</p>
              <p style="font-size:30px;font-weight:700;letter-spacing:8px;margin:18px 0">{code}</p>
              <p>验证码 {minutes} 分钟内有效。若非本人操作，请忽略此邮件。</p>
            </div>
            """.format(action=action, code=code, minutes=minutes)

        try:
            self.send_email([recipient], f"TASTE {action}验证码", text_body, html_body)
        except VerificationEmailError as exc:
            raise VerificationEmailError("验证码邮件发送失败，请稍后重试。") from exc

    def _open_connection(self) -> smtplib.SMTP:
        smtp: smtplib.SMTP | None = None
        try:
            if self.security == "ssl":
                smtp = smtplib.SMTP_SSL(
                    self.host,
                    self.port,
                    timeout=15,
                    context=self._ssl_context,
                )
            else:
                smtp = smtplib.SMTP(self.host, self.port, timeout=15)
                if self.security == "starttls":
                    smtp.starttls(context=self._ssl_context)
            if self.username:
                smtp.login(self.username, self.password)
            return smtp
        except (OSError, smtplib.SMTPException):
            if smtp is not None:
                smtp.close()
            raise

    def _discard_connection(self) -> None:
        connection = self._connection
        object.__setattr__(self, "_connection", None)
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass

    @staticmethod
    def _should_retry_connection(exc: BaseException) -> bool:
        return (
            isinstance(exc, (OSError, smtplib.SMTPServerDisconnected))
            or isinstance(exc, smtplib.SMTPResponseException) and exc.smtp_code == 421
        )

    def _send_message(self, message: EmailMessage) -> None:
        # SMTP connections are expensive because they include TCP, TLS and login
        # round trips. Serialize access and reuse a healthy connection; if the
        # provider expired it, reconnect once and send the same code immediately.
        with self._connection_lock:
            reused_connection = self._connection is not None
            if not reused_connection:
                object.__setattr__(self, "_connection", self._open_connection())
            try:
                assert self._connection is not None
                self._connection.send_message(message)
                return
            except (OSError, smtplib.SMTPException) as exc:
                self._discard_connection()
                if not reused_connection or not self._should_retry_connection(exc):
                    raise

            object.__setattr__(self, "_connection", self._open_connection())
            try:
                assert self._connection is not None
                self._connection.send_message(message)
            except (OSError, smtplib.SMTPException):
                self._discard_connection()
                raise
