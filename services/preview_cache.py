"""聚合预览结果缓存：草稿走实时计算，正式预览读磁盘产物。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Dict, Optional

from sqlmodel import Session, select

from models import Channel, OutputSource, Subscription
from services.output_resolver import filter_candidates, preview_export_groups


def _channel_preview_signature(channel: Channel) -> dict:
    """频道预览相关字段摘要，用于缓存失效判断。"""
    img = channel.check_image or ""
    return {
        "id": channel.id,
        "is_enabled": channel.is_enabled,
        "group": channel.group,
        "name": channel.name,
        "url": channel.url,
        "logo": channel.logo,
        "tvg_id": channel.tvg_id,
        "subscription_id": channel.subscription_id,
        "check_status": channel.check_status,
        "check_date": channel.check_date.isoformat() if channel.check_date else None,
        "check_error": channel.check_error,
        "check_source": channel.check_source,
        "check_image_len": len(img),
        "ai_visual_status": channel.ai_visual_status,
        "ai_visual_detail": channel.ai_visual_detail,
    }


def compute_preview_cache_key(
    session: Session,
    out: OutputSource,
    draft: Optional[Dict[str, Any]] = None,
) -> str:
    """根据聚合配置与成员频道状态计算缓存指纹。"""
    if draft is not None:
        cfg = {
            "subscription_ids": draft.get("subscription_ids") or [],
            "keywords": draft.get("keywords") or [],
            "filter_regex": draft.get("filter_regex") or out.filter_regex,
            "excluded_channel_ids": draft.get("excluded_channel_ids") or [],
            "layout_mode": draft.get("layout_mode") or out.layout_mode,
            "channel_layout": draft.get("channel_layout") if draft.get("channel_layout") is not None else out.channel_layout,
        }
    else:
        cfg = {
            "subscription_ids": out.subscription_ids,
            "keywords": out.keywords,
            "filter_regex": out.filter_regex,
            "excluded_channel_ids": out.excluded_channel_ids,
            "layout_mode": out.layout_mode,
            "channel_layout": out.channel_layout,
        }

    subs = session.exec(select(Subscription)).all()
    sub_names = {
        s.id: (s.name or s.url or "")
        for s in subs
        if s.is_enabled
    }

    channels = filter_candidates(session, out, draft, enabled_only=False)
    channel_sigs = [
        _channel_preview_signature(c)
        for c in sorted(channels, key=lambda x: x.id or 0)
    ]

    payload = {
        "cfg": cfg,
        "subs": sub_names,
        "channels": channel_sigs,
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def clear_output_preview_cache(out: OutputSource) -> None:
    """清空聚合源上已存的预览缓存元数据。"""
    out.preview_cache_key = None
    out.preview_cache_json = None
    out.preview_cache_at = None


def get_or_build_export_preview(
    session: Session,
    out: OutputSource,
    draft: Optional[Dict[str, Any]] = None,
    *,
    force: bool = False,
    epg_refresh: bool = False,
) -> Dict[str, Any]:
    """读取或生成聚合预览 JSON。"""
    if draft is not None:
        payload = preview_export_groups(session, out, draft)
        payload["cache"] = {"hit": False, "reason": "draft"}
        return payload

    from services.output_artifacts import get_or_build_preview_payload

    return get_or_build_preview_payload(
        session,
        out,
        force=force,
        epg_refresh=epg_refresh,
    )