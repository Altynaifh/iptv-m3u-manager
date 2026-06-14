"""管理页访问密码（哈希存储 + 会话校验辅助）。"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Optional

from fastapi import HTTPException, Request, Response
from sqlmodel import Session

from models import AppSettings

_HASH_PREFIX = "pbkdf2_sha256"
_HASH_ITERATIONS = 260_000
AUTH_COOKIE = "iptv_auth"
AUTH_COOKIE_MAX_AGE = 60 * 60 * 24 * 7


def get_settings_row(session: Session) -> AppSettings:
    row = session.get(AppSettings, 1)
    if not row:
        row = AppSettings(id=1)
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


def is_protection_enabled(session: Session) -> bool:
    row = get_settings_row(session)
    return bool(row.access_password_enabled and row.access_password_hash)


def public_access_settings(session: Session) -> dict:
    row = get_settings_row(session)
    return {
        "enabled": bool(row.access_password_enabled and row.access_password_hash),
        "configured": bool(row.access_password_hash),
    }


def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        _HASH_ITERATIONS,
    ).hex()
    return f"{_HASH_PREFIX}${_HASH_ITERATIONS}${salt}${digest}"


def _verify_password(password: str, stored: str) -> bool:
    if not password or not stored:
        return False
    try:
        prefix, iterations, salt, digest = stored.split("$", 3)
        if prefix != _HASH_PREFIX:
            return False
        calc = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            int(iterations),
        ).hex()
        return hmac.compare_digest(calc, digest)
    except (ValueError, TypeError):
        return False


def verify_login(session: Session, password: str) -> bool:
    row = get_settings_row(session)
    if not is_protection_enabled(session):
        return True
    return _verify_password(password or "", row.access_password_hash or "")


def save_access_settings(
    session: Session,
    *,
    enabled: bool,
    password: Optional[str] = None,
    current_password: Optional[str] = None,
) -> dict:
    row = get_settings_row(session)
    currently_enabled = bool(row.access_password_enabled and row.access_password_hash)

    if currently_enabled:
        if not _verify_password(current_password or "", row.access_password_hash or ""):
            raise HTTPException(status_code=403, detail="当前密码不正确")

    if enabled:
        new_password = (password or "").strip()
        if new_password:
            row.access_password_hash = _hash_password(new_password)
        elif not row.access_password_hash:
            raise HTTPException(status_code=400, detail="启用密码保护前请先设置密码")
        row.access_password_enabled = True
    else:
        row.access_password_enabled = False

    session.add(row)
    session.commit()
    session.refresh(row)
    return public_access_settings(session)


def get_session_secret() -> str:
    return os.environ.get("IPTV_SESSION_SECRET") or "iptv-m3u-manager-session-secret"


def _sign_payload(data: str) -> str:
    return hmac.new(
        get_session_secret().encode("utf-8"),
        data.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def create_session_token() -> str:
    payload = {
        "authenticated": True,
        "exp": int(time.time()) + AUTH_COOKIE_MAX_AGE,
    }
    data = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("utf-8")
    return f"{data}.{_sign_payload(data)}"


def verify_session_token(token: Optional[str]) -> bool:
    if not token or "." not in token:
        return False
    data, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(_sign_payload(data), sig):
        return False
    try:
        payload = json.loads(base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8"))
    except (json.JSONDecodeError, ValueError):
        return False
    return bool(payload.get("authenticated")) and int(payload.get("exp") or 0) > int(time.time())


def is_request_authenticated(request: Request) -> bool:
    return verify_session_token(request.cookies.get(AUTH_COOKIE))


def set_auth_cookie(response: Response) -> None:
    response.set_cookie(
        AUTH_COOKIE,
        create_session_token(),
        httponly=True,
        samesite="lax",
        max_age=AUTH_COOKIE_MAX_AGE,
        path="/",
    )


def clear_auth_cookie(response: Response) -> None:
    response.delete_cookie(AUTH_COOKIE, path="/")