from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4


SESSION_COOKIE = "taste_session"
SESSION_DAYS = 7
SESSION_LIMIT_PER_USER = 2
PASSWORD_ITERATIONS = 310_000
VERIFICATION_CODE_ITERATIONS = 120_000
VERIFICATION_CODE_TTL = timedelta(minutes=10)
VERIFICATION_CODE_COOLDOWN = timedelta(seconds=60)
VERIFICATION_CODE_WINDOW = timedelta(hours=1)
VERIFICATION_CODE_EMAIL_LIMIT = 5
VERIFICATION_CODE_REQUESTER_LIMIT = 20
VERIFICATION_CODE_ATTEMPT_LIMIT = 5
USERNAME_PATTERN = re.compile(r"^[\w.@+-]{3,64}$", re.UNICODE)
EMAIL_LOCAL_PATTERN = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}$")


@dataclass(frozen=True)
class AuthUser:
    id: str
    username: str
    email: str = ""


@dataclass(frozen=True)
class EmailVerification:
    email: str
    code: str
    code_hash: bytes
    expires_in: int
    retry_after: int


class AuthError(ValueError):
    pass


class AuthRateLimitError(AuthError):
    def __init__(self, message: str, retry_after: int):
        super().__init__(message)
        self.retry_after = max(1, int(retry_after))


class AuthStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    username_key TEXT NOT NULL UNIQUE,
                    password_salt BLOB NOT NULL,
                    password_hash BLOB NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS sessions_user_id ON sessions(user_id);
                """
            )
            columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(users)")}
            if "email" not in columns:
                connection.execute("ALTER TABLE users ADD COLUMN email TEXT")
            if "email_key" not in columns:
                connection.execute("ALTER TABLE users ADD COLUMN email_key TEXT")
            if "email_verified_at" not in columns:
                connection.execute("ALTER TABLE users ADD COLUMN email_verified_at TEXT")
            connection.executescript(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS users_email_key_unique
                    ON users(email_key) WHERE email_key IS NOT NULL;
                CREATE TABLE IF NOT EXISTS email_verifications (
                    email_key TEXT PRIMARY KEY,
                    email TEXT NOT NULL,
                    code_salt BLOB NOT NULL,
                    code_hash BLOB NOT NULL,
                    expires_at TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS email_verification_limits (
                    scope_key TEXT PRIMARY KEY,
                    window_started_at TEXT NOT NULL,
                    request_count INTEGER NOT NULL
                );
                """
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def normalize_username(value: object) -> tuple[str, str]:
        username = unicodedata.normalize("NFKC", str(value or "")).strip()
        if not USERNAME_PATTERN.fullmatch(username):
            raise AuthError("用户名须为 3-64 个字母、数字、下划线或 . @ + -。")
        return username, username.casefold()

    @staticmethod
    def normalize_email(value: object) -> tuple[str, str]:
        raw = unicodedata.normalize("NFKC", str(value or "")).strip()
        if len(raw) > 254 or raw.count("@") != 1:
            raise AuthError("请输入有效的邮箱地址。")
        local, domain = raw.rsplit("@", 1)
        if not EMAIL_LOCAL_PATTERN.fullmatch(local) or local.startswith(".") or local.endswith(".") or ".." in local:
            raise AuthError("请输入有效的邮箱地址。")
        try:
            ascii_domain = domain.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise AuthError("请输入有效的邮箱地址。") from exc
        labels = ascii_domain.split(".")
        if (
            len(labels) < 2
            or any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in labels)
            or any(not re.fullmatch(r"[a-z0-9-]+", label) for label in labels)
        ):
            raise AuthError("请输入有效的邮箱地址。")
        email = f"{local}@{ascii_domain}"
        return email, email.casefold()

    @staticmethod
    def validate_password(value: object) -> str:
        password = str(value or "")
        if len(password) < 8 or len(password) > 128:
            raise AuthError("密码长度须为 8-128 个字符。")
        return password

    @staticmethod
    def _password_hash(password: str, salt: bytes) -> bytes:
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)

    @staticmethod
    def _code_hash(code: str, salt: bytes) -> bytes:
        return hashlib.pbkdf2_hmac("sha256", code.encode("ascii"), salt, VERIFICATION_CODE_ITERATIONS)

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _scope_key(scope: str, value: str) -> str:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        return f"{scope}:{digest}"

    @staticmethod
    def _seconds_until(target: datetime, now: datetime) -> int:
        return max(1, int((target - now).total_seconds() + 0.999))

    def _consume_limit(
        self,
        connection: sqlite3.Connection,
        scope_key: str,
        limit: int,
        now: datetime,
    ) -> None:
        row = connection.execute(
            "SELECT window_started_at, request_count FROM email_verification_limits WHERE scope_key = ?",
            (scope_key,),
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO email_verification_limits(scope_key, window_started_at, request_count) VALUES (?, ?, 1)",
                (scope_key, now.isoformat()),
            )
            return
        window_started = datetime.fromisoformat(str(row["window_started_at"]))
        window_ends = window_started + VERIFICATION_CODE_WINDOW
        if now >= window_ends:
            connection.execute(
                "UPDATE email_verification_limits SET window_started_at = ?, request_count = 1 WHERE scope_key = ?",
                (now.isoformat(), scope_key),
            )
            return
        if int(row["request_count"]) >= limit:
            raise AuthRateLimitError("验证码发送过于频繁，请稍后再试。", self._seconds_until(window_ends, now))
        connection.execute(
            "UPDATE email_verification_limits SET request_count = request_count + 1 WHERE scope_key = ?",
            (scope_key,),
        )

    def begin_email_verification(self, email_value: object, requester: object = "") -> EmailVerification:
        email, email_key = self.normalize_email(email_value)
        requester_key = str(requester or "unknown").strip() or "unknown"
        now = datetime.now(UTC)
        code = f"{secrets.randbelow(1_000_000):06d}"
        salt = secrets.token_bytes(16)
        code_hash = self._code_hash(code, salt)
        expires_at = now + VERIFICATION_CODE_TTL
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM users WHERE email_key = ? OR username_key = ?",
                (email_key, email_key),
            ).fetchone():
                raise AuthError("该邮箱已注册或已作为用户名使用。")
            previous = connection.execute(
                "SELECT sent_at FROM email_verifications WHERE email_key = ?",
                (email_key,),
            ).fetchone()
            if previous is not None:
                retry_at = datetime.fromisoformat(str(previous["sent_at"])) + VERIFICATION_CODE_COOLDOWN
                if now < retry_at:
                    raise AuthRateLimitError("验证码已发送，请稍后再试。", self._seconds_until(retry_at, now))
            self._consume_limit(
                connection,
                self._scope_key("email", email_key),
                VERIFICATION_CODE_EMAIL_LIMIT,
                now,
            )
            self._consume_limit(
                connection,
                self._scope_key("requester", requester_key),
                VERIFICATION_CODE_REQUESTER_LIMIT,
                now,
            )
            connection.execute(
                """
                INSERT INTO email_verifications(email_key, email, code_salt, code_hash, expires_at, sent_at, attempts)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(email_key) DO UPDATE SET
                    email = excluded.email,
                    code_salt = excluded.code_salt,
                    code_hash = excluded.code_hash,
                    expires_at = excluded.expires_at,
                    sent_at = excluded.sent_at,
                    attempts = 0
                """,
                (email_key, email, salt, code_hash, expires_at.isoformat(), now.isoformat()),
            )
            connection.execute(
                "DELETE FROM email_verification_limits WHERE window_started_at < ?",
                ((now - VERIFICATION_CODE_WINDOW * 2).isoformat(),),
            )
        return EmailVerification(
            email=email,
            code=code,
            code_hash=code_hash,
            expires_in=int(VERIFICATION_CODE_TTL.total_seconds()),
            retry_after=int(VERIFICATION_CODE_COOLDOWN.total_seconds()),
        )

    def cancel_email_verification(self, verification: EmailVerification) -> None:
        _, email_key = self.normalize_email(verification.email)
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM email_verifications WHERE email_key = ? AND code_hash = ?",
                (email_key, verification.code_hash),
            )

    def register(
        self,
        username_value: object,
        email_value: object,
        password_value: object,
        verification_code_value: object,
    ) -> AuthUser:
        username, username_key = self.normalize_username(username_value)
        email, email_key = self.normalize_email(email_value)
        password = self.validate_password(password_value)
        verification_code = str(verification_code_value or "").strip()
        if not re.fullmatch(r"\d{6}", verification_code):
            raise AuthError("请输入 6 位邮箱验证码。")
        salt = secrets.token_bytes(16)
        user = AuthUser(id=uuid4().hex, username=username, email=email)
        now = datetime.now(UTC)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            verification = connection.execute(
                "SELECT code_salt, code_hash, expires_at, attempts FROM email_verifications WHERE email_key = ?",
                (email_key,),
            ).fetchone()
            if verification is None:
                raise AuthError("请先获取邮箱验证码。")
            if now >= datetime.fromisoformat(str(verification["expires_at"])):
                connection.execute("DELETE FROM email_verifications WHERE email_key = ?", (email_key,))
                connection.commit()
                raise AuthError("邮箱验证码已过期，请重新获取。")
            attempts = int(verification["attempts"])
            candidate = self._code_hash(verification_code, bytes(verification["code_salt"]))
            if attempts >= VERIFICATION_CODE_ATTEMPT_LIMIT or not hmac.compare_digest(
                candidate, bytes(verification["code_hash"])
            ):
                attempts += 1
                if attempts >= VERIFICATION_CODE_ATTEMPT_LIMIT:
                    connection.execute("DELETE FROM email_verifications WHERE email_key = ?", (email_key,))
                    message = "验证码错误次数过多，请重新获取。"
                else:
                    connection.execute(
                        "UPDATE email_verifications SET attempts = ? WHERE email_key = ?",
                        (attempts, email_key),
                    )
                    message = "邮箱验证码错误。"
                connection.commit()
                raise AuthError(message)
            conflict = connection.execute(
                """
                SELECT username_key, email_key FROM users
                WHERE username_key IN (?, ?) OR email_key IN (?, ?)
                """,
                (username_key, email_key, username_key, email_key),
            ).fetchone()
            if conflict is not None:
                if str(conflict["username_key"]) == username_key:
                    raise AuthError("该用户名已注册。")
                raise AuthError("该邮箱已注册或与现有登录名冲突。")
            connection.execute(
                """
                INSERT INTO users(
                    id, username, username_key, email, email_key, email_verified_at,
                    password_salt, password_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user.id,
                    user.username,
                    username_key,
                    user.email,
                    email_key,
                    now.isoformat(),
                    salt,
                    self._password_hash(password, salt),
                    now.isoformat(),
                ),
            )
            connection.execute("DELETE FROM email_verifications WHERE email_key = ?", (email_key,))
        return user

    def authenticate(self, identifier_value: object, password_value: object) -> AuthUser | None:
        identifier = unicodedata.normalize("NFKC", str(identifier_value or "")).strip()
        username_key: str | None = None
        email_key: str | None = None
        try:
            _, username_key = self.normalize_username(identifier)
        except AuthError:
            pass
        try:
            _, email_key = self.normalize_email(identifier)
        except AuthError:
            pass
        if username_key is None and email_key is None:
            return None
        password = str(password_value or "")
        with self._connect() as connection:
            row = None
            if username_key is not None:
                row = connection.execute(
                    """
                    SELECT id, username, email, password_salt, password_hash
                    FROM users WHERE username_key = ?
                    """,
                    (username_key,),
                ).fetchone()
            if row is None and email_key is not None:
                row = connection.execute(
                    """
                    SELECT id, username, email, password_salt, password_hash
                    FROM users WHERE email_key = ?
                    """,
                    (email_key,),
                ).fetchone()
        if row is None:
            return None
        candidate = self._password_hash(password, bytes(row["password_salt"]))
        if not hmac.compare_digest(candidate, bytes(row["password_hash"])):
            return None
        return AuthUser(id=str(row["id"]), username=str(row["username"]), email=str(row["email"] or ""))

    def create_session(self, user: AuthUser) -> str:
        token = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        expires = now + timedelta(days=SESSION_DAYS)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (now.isoformat(),))
            connection.execute(
                "INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
                (self._token_hash(token), user.id, expires.isoformat(), now.isoformat()),
            )
            connection.execute(
                """
                DELETE FROM sessions
                WHERE user_id = ? AND token_hash NOT IN (
                    SELECT token_hash FROM sessions
                    WHERE user_id = ?
                    ORDER BY created_at DESC, rowid DESC
                    LIMIT ?
                )
                """,
                (user.id, user.id, SESSION_LIMIT_PER_USER),
            )
        return token

    def user_for_session(self, token: object) -> AuthUser | None:
        raw_token = str(token or "").strip()
        if not raw_token:
            return None
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT users.id, users.username, users.email
                FROM sessions JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ? AND sessions.expires_at > ?
                """,
                (self._token_hash(raw_token), now),
            ).fetchone()
        if row is None:
            return None
        return AuthUser(id=str(row["id"]), username=str(row["username"]), email=str(row["email"] or ""))

    def delete_session(self, token: object) -> None:
        raw_token = str(token or "").strip()
        if not raw_token:
            return
        with self._connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (self._token_hash(raw_token),))
