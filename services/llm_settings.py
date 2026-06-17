"""全站 LLM 配置读写。"""
import json
from typing import Any, Dict

from sqlmodel import Session

from models import AppSettings

MASK = "***"


def _reject_non_latin1_config(value: str, field: str) -> None:
    """API Key / 模型名须为 ASCII，避免 HTTP 头 latin-1 编码失败。"""
    text = (value or "").strip()
    if not text:
        return
    try:
        text.encode("latin-1")
    except UnicodeEncodeError as e:
        snippet = text[e.start : e.end]
        raise ValueError(
            f"{field} 含非 ASCII 字符（如「{snippet}」），请仅填写英文/数字密钥与模型名"
        ) from e


def _default_block() -> Dict[str, str]:
    return {"base_url": "", "api_key": "", "model": ""}


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return MASK
    return key[:4] + MASK + key[-2:]


def get_settings_row(session: Session) -> AppSettings:
    row = session.get(AppSettings, 1)
    if not row:
        row = AppSettings(id=1)
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


def load_llm_blocks(session: Session) -> Dict[str, Dict[str, str]]:
    row = get_settings_row(session)
    try:
        text = json.loads(row.llm_text_json or "{}")
    except json.JSONDecodeError:
        text = _default_block()
    try:
        vision = json.loads(row.llm_vision_json or "{}")
    except json.JSONDecodeError:
        vision = _default_block()
    for block in (text, vision):
        for k in ("base_url", "api_key", "model"):
            block.setdefault(k, "")
    return {"llm_text": text, "llm_vision": vision}


def public_llm_settings(session: Session) -> Dict[str, Any]:
    blocks = load_llm_blocks(session)
    out = {}
    for name, block in blocks.items():
        out[name] = {
            "base_url": block.get("base_url", ""),
            "model": block.get("model", ""),
            "api_key": _mask_key(block.get("api_key", "")),
            "configured": bool(block.get("base_url") and block.get("api_key") and block.get("model")),
        }
    return out




def llm_settings_for_edit(session: Session) -> Dict[str, Any]:
    """编辑表单回填：返回完整 api_key（仅管理端 UI 使用）。"""
    blocks = load_llm_blocks(session)
    out = {}
    for name, block in blocks.items():
        out[name] = {
            "base_url": block.get("base_url", ""),
            "model": block.get("model", ""),
            "api_key": block.get("api_key", ""),
            "configured": bool(block.get("base_url") and block.get("api_key") and block.get("model")),
        }
    return out

def save_llm_settings(session: Session, payload: Dict[str, Any]) -> Dict[str, Any]:
    row = get_settings_row(session)
    current = load_llm_blocks(session)
    for key in ("llm_text", "llm_vision"):
        if key not in payload:
            continue
        inc = payload[key] or {}
        merged = dict(current[key])
        if inc.get("base_url") is not None:
            merged["base_url"] = str(inc.get("base_url") or "").strip()
        if inc.get("model") is not None:
            merged["model"] = str(inc.get("model") or "").strip()
        api_key = inc.get("api_key")
        if api_key is not None and api_key != MASK and str(api_key).strip():
            key_text = str(api_key).strip()
            _reject_non_latin1_config(key_text, f"{key} api_key")
            merged["api_key"] = key_text
        if inc.get("model") is not None:
            _reject_non_latin1_config(merged.get("model", ""), f"{key} model")
        if key == "llm_text":
            row.llm_text_json = json.dumps(merged, ensure_ascii=False)
        else:
            row.llm_vision_json = json.dumps(merged, ensure_ascii=False)
    session.add(row)
    session.commit()
    return public_llm_settings(session)
