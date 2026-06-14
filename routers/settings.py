from fastapi import APIRouter, Depends
from sqlmodel import SQLModel, Session

from database import get_session
from services.llm_settings import public_llm_settings, save_llm_settings, llm_settings_for_edit

router = APIRouter(tags=["settings"])


class LlmBlockIn(SQLModel):
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None


class LlmSettingsIn(SQLModel):
    llm_text: LlmBlockIn | None = None
    llm_vision: LlmBlockIn | None = None


@router.get("/api/settings/llm")
def get_llm_settings(reveal: bool = False, session: Session = Depends(get_session)):
    if reveal:
        return llm_settings_for_edit(session)
    return public_llm_settings(session)


@router.put("/api/settings/llm")
def put_llm_settings(body: LlmSettingsIn, session: Session = Depends(get_session)):
    payload = {}
    if body.llm_text is not None:
        payload["llm_text"] = body.llm_text.model_dump()
    if body.llm_vision is not None:
        payload["llm_vision"] = body.llm_vision.model_dump()
    return save_llm_settings(session, payload)
