from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import stat
import tempfile
from threading import RLock
import time
from typing import Any

import yaml
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError


SESSION_COOKIE = "clusterx_admin_session"


def _validate_username(value: str) -> str:
    username = value.strip()
    if not 1 <= len(username) <= 64:
        raise ValueError("administrator username must contain 1 to 64 characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in username):
        raise ValueError("administrator username contains control characters")
    return username


def validate_password(value: str) -> None:
    if len(value) < 12:
        raise ValueError("administrator password must contain at least 12 characters")
    if len(value) > 1024:
        raise ValueError("administrator password is too long")


def _atomic_write(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError(f"refusing to replace symlink: {path}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def initialize_auth_config(
    path: str | Path, username: str, password: str, *,
    session_ttl_hours: int = 8, overwrite: bool = False,
) -> Path:
    destination = Path(path).expanduser().resolve()
    if destination.exists() and not overwrite:
        raise ValueError(f"administrator configuration already exists: {destination}")
    username = _validate_username(username)
    validate_password(password)
    if not 1 <= session_ttl_hours <= 24:
        raise ValueError("session TTL must be between 1 and 24 hours")
    password_hash = PasswordHasher().hash(password)
    payload = {
        "schema_version": 1,
        "username": username,
        "password_hash": password_hash,
        "session_ttl_hours": session_ttl_hours,
    }
    _atomic_write(
        destination,
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True).encode("utf-8"),
    )
    return destination


@dataclass(frozen=True)
class AdminSession:
    username: str
    csrf_token: str
    expires_at: datetime


class AdminAuth:
    """Single-administrator authentication with server-side, in-memory sessions."""

    def __init__(self, path: str | Path, *, allow_missing: bool = False) -> None:
        self.path = Path(path).expanduser().resolve()
        self._lock = RLock()
        self._sessions: dict[str, AdminSession] = {}
        self._attempts: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self.username = ""
        self.password_hash = ""
        self.session_ttl_hours = 8
        self._mtime_ns: int | None = None
        if self.path.exists() or not allow_missing:
            self.reload()

    @property
    def configured(self) -> bool:
        return self._mtime_ns is not None and bool(self.password_hash)

    def reload(self) -> None:
        try:
            mode = stat.S_IMODE(self.path.resolve(strict=True).stat().st_mode)
        except OSError as error:
            raise ValueError(f"administrator configuration is unavailable: {error}") from error
        if mode & 0o077:
            raise ValueError("administrator configuration permissions must be 600")
        payload = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("administrator configuration root must be a mapping")
        if set(payload) != {"schema_version", "username", "password_hash", "session_ttl_hours"}:
            raise ValueError("administrator configuration contains unsupported or missing fields")
        if payload.get("schema_version") != 1:
            raise ValueError("unsupported administrator configuration schema version")
        username = _validate_username(str(payload.get("username") or ""))
        password_hash = str(payload.get("password_hash") or "")
        if not password_hash.startswith("$argon2id$"):
            raise ValueError("administrator password must use an Argon2id hash")
        try:
            PasswordHasher().check_needs_rehash(password_hash)
        except InvalidHashError as error:
            raise ValueError("administrator password hash is invalid") from error
        ttl = int(payload.get("session_ttl_hours") or 0)
        if not 1 <= ttl <= 24:
            raise ValueError("administrator session TTL must be between 1 and 24 hours")
        with self._lock:
            changed = self.username and (
                not hmac.compare_digest(self.username, username)
                or not hmac.compare_digest(self.password_hash, password_hash)
            )
            self.username = username
            self.password_hash = password_hash
            self.session_ttl_hours = ttl
            self._mtime_ns = self.path.stat().st_mtime_ns
            if changed:
                self._sessions.clear()

    def reload_if_changed(self) -> None:
        try:
            if not self.path.is_file():
                raise ValueError("administrator authentication is not initialized")
            if self.path.stat().st_mtime_ns != self._mtime_ns:
                self.reload()
        except (OSError, ValueError) as error:
            raise RuntimeError("administrator authentication configuration is invalid") from error

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _rate_limited(self, client: str, username: str, now: float) -> bool:
        cutoff = now - 15 * 60
        limited = False
        for key in ((client, username.casefold()), (client, "*")):
            attempts = self._attempts[key]
            while attempts and attempts[0] < cutoff:
                attempts.popleft()
            limited = limited or len(attempts) >= 5
        return limited

    def login(self, username: str, password: str, client: str) -> tuple[str, AdminSession]:
        self.reload_if_changed()
        now = time.monotonic()
        normalized = str(username).strip()
        with self._lock:
            if self._rate_limited(client, normalized, now):
                raise PermissionError("too many login attempts; try again later")
            valid_username = hmac.compare_digest(normalized, self.username)
            try:
                valid_password = PasswordHasher().verify(self.password_hash, str(password))
            except (VerificationError, VerifyMismatchError, InvalidHashError):
                valid_password = False
            if not (valid_username and valid_password):
                self._attempts[(client, normalized.casefold())].append(now)
                self._attempts[(client, "*")].append(now)
                raise ValueError("invalid administrator credentials")
            self._attempts.pop((client, normalized.casefold()), None)
            self._attempts.pop((client, "*"), None)
            token = secrets.token_urlsafe(32)
            session = AdminSession(
                username=self.username,
                csrf_token=secrets.token_urlsafe(32),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=self.session_ttl_hours),
            )
            self._sessions[self._digest(token)] = session
            return token, session

    def session(self, token: str | None) -> AdminSession | None:
        self.reload_if_changed()
        if not token:
            return None
        key = self._digest(token)
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                return None
            if session.expires_at <= datetime.now(timezone.utc):
                self._sessions.pop(key, None)
                return None
            return session

    def logout(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._sessions.pop(self._digest(token), None)

    def public_session(self, session: AdminSession) -> dict[str, Any]:
        return {
            "authenticated": True,
            "username": session.username,
            "csrf_token": session.csrf_token,
            "expires_at": session.expires_at.isoformat(),
        }
