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
SESSION_DAYS = 30
PASSWORD_ITERATIONS = 310_000
USERNAME_PATTERN = re.compile(r"^[\w.@+-]{3,64}$", re.UNICODE)


@dataclass(frozen=True)
class AuthUser:
    id: str
    username: str


class AuthError(ValueError):
    pass


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
    def validate_password(value: object) -> str:
        password = str(value or "")
        if len(password) < 8 or len(password) > 128:
            raise AuthError("密码长度须为 8-128 个字符。")
        return password

    @staticmethod
    def _password_hash(password: str, salt: bytes) -> bytes:
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def register(self, username_value: object, password_value: object) -> AuthUser:
        username, username_key = self.normalize_username(username_value)
        password = self.validate_password(password_value)
        salt = secrets.token_bytes(16)
        user = AuthUser(id=uuid4().hex, username=username)
        now = datetime.now(UTC).isoformat()
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO users(id, username, username_key, password_salt, password_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (user.id, user.username, username_key, salt, self._password_hash(password, salt), now),
                )
        except sqlite3.IntegrityError as exc:
            raise AuthError("该用户名已注册。") from exc
        return user

    def authenticate(self, username_value: object, password_value: object) -> AuthUser | None:
        try:
            _, username_key = self.normalize_username(username_value)
        except AuthError:
            return None
        password = str(password_value or "")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, username, password_salt, password_hash FROM users WHERE username_key = ?",
                (username_key,),
            ).fetchone()
        if row is None:
            return None
        candidate = self._password_hash(password, bytes(row["password_salt"]))
        if not hmac.compare_digest(candidate, bytes(row["password_hash"])):
            return None
        return AuthUser(id=str(row["id"]), username=str(row["username"]))

    def create_session(self, user: AuthUser) -> str:
        token = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        expires = now + timedelta(days=SESSION_DAYS)
        with self._connect() as connection:
            connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (now.isoformat(),))
            connection.execute(
                "INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
                (self._token_hash(token), user.id, expires.isoformat(), now.isoformat()),
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
                SELECT users.id, users.username
                FROM sessions JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ? AND sessions.expires_at > ?
                """,
                (self._token_hash(raw_token), now),
            ).fetchone()
        if row is None:
            return None
        return AuthUser(id=str(row["id"]), username=str(row["username"]))

    def delete_session(self, token: object) -> None:
        raw_token = str(token or "").strip()
        if not raw_token:
            return
        with self._connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (self._token_hash(raw_token),))
