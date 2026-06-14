from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlmodel import SQLModel, Session

from database import get_session
from services.access_auth import (
    clear_auth_cookie,
    is_protection_enabled,
    is_request_authenticated,
    public_access_settings,
    save_access_settings,
    set_auth_cookie,
    verify_login,
)

router = APIRouter(tags=["auth"])


class LoginIn(SQLModel):
    password: str


class AccessSettingsIn(SQLModel):
    enabled: bool = False
    password: str | None = None
    current_password: str | None = None


def _require_authenticated(request: Request, session: Session) -> None:
    if not is_protection_enabled(session):
        return
    if not is_request_authenticated(request):
        raise HTTPException(status_code=401, detail="未授权，请先登录")


@router.get("/api/auth/status")
def auth_status(request: Request, session: Session = Depends(get_session)):
    enabled = is_protection_enabled(session)
    authenticated = not enabled or is_request_authenticated(request)
    return {"protection_enabled": enabled, "authenticated": authenticated}


@router.post("/api/auth/login")
def auth_login(body: LoginIn, request: Request, response: Response, session: Session = Depends(get_session)):
    if not is_protection_enabled(session):
        set_auth_cookie(response)
        return {"status": "ok", "message": "未启用密码保护"}
    if not verify_login(session, body.password):
        raise HTTPException(status_code=401, detail="密码错误")
    set_auth_cookie(response)
    return {"status": "ok", "message": "登录成功"}


@router.post("/api/auth/logout")
def auth_logout(response: Response):
    clear_auth_cookie(response)
    return {"status": "ok", "message": "已退出登录"}


@router.get("/api/settings/access")
def get_access_settings(session: Session = Depends(get_session)):
    return public_access_settings(session)


@router.put("/api/settings/access")
def put_access_settings(
    body: AccessSettingsIn,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    if is_protection_enabled(session):
        _require_authenticated(request, session)
    result = save_access_settings(
        session,
        enabled=body.enabled,
        password=body.password,
        current_password=body.current_password,
    )
    if result.get("enabled") or not body.enabled:
        set_auth_cookie(response)
    return result