"""聚合源卡片与预览行 WebSocket 实时推送。"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlmodel import Session

from database import engine
from models import Channel, OutputSource
from services.output_resolver import aggregate_channels
from services.output_stats import get_or_refresh_member_stats, load_enabled_subscription_channel_pool
from services.update_status_report import format_output_update_status
from task_broker import notifier

_SYNC_MARK_RE = re.compile(r"订阅同步([✓✗—])")
_AI_VISION_DISABLE = frozenset({"invalid", "promo_loop"})
_AI_VISION_ENABLE = frozenset({"ok", "frozen"})


def channel_patch_fields(ch: Channel) -> dict:
    """频道增量字段（不含 check_image base64）。"""
    return {
        "id": ch.id,
        "check_status": ch.check_status,
        "check_date": ch.check_date.isoformat() if ch.check_date else None,
        "check_error": ch.check_error,
        "check_source": ch.check_source,
        "ai_visual_status": ch.ai_visual_status,
        "ai_visual_detail": ch.ai_visual_detail,
        "ai_visual_date": ch.ai_visual_date.isoformat() if ch.ai_visual_date else None,
        "is_enabled": ch.is_enabled,
        "has_check_image": bool(ch.check_image),
    }


def _parse_sync_from_status(status_text: Optional[str]) -> Optional[bool]:
    if not status_text:
        return None
    match = _SYNC_MARK_RE.search(status_text)
    if not match:
        return None
    mark = match.group(1)
    if mark == "✓":
        return True
    if mark == "✗":
        return False
    return None


def _parse_trigger_from_status(status_text: Optional[str]) -> str:
    if status_text and "自动更新" in status_text:
        return "auto"
    return "manual"


def rebuild_manual_status_from_db(session: Session, output_id: int) -> str:
    """从 DB 重算 last_update_status，保留原订阅同步标记。"""
    out = session.get(OutputSource, output_id)
    if not out:
        return ""

    existing = out.last_update_status or ""
    sync = _parse_sync_from_status(existing)
    trigger = _parse_trigger_from_status(existing)
    channels = aggregate_channels(session, out, None)

    screenshot_stats = None
    screenshot_skipped = True
    if any(c.check_date is not None or c.check_status is not None for c in channels):
        screenshot_skipped = False
        enabled = sum(1 for c in channels if c.is_enabled)
        screenshot_stats = {"enabled": enabled, "disabled": len(channels) - enabled}

    ai_vision_stats = None
    ai_vision_skipped = True
    vision_channels = [c for c in channels if c.ai_visual_date is not None]
    if vision_channels:
        ai_vision_skipped = False
        ai_vision_stats = {
            "disabled": sum(
                1
                for c in vision_channels
                if (c.ai_visual_status or "").lower() in _AI_VISION_DISABLE
            ),
            "enabled": sum(
                1
                for c in vision_channels
                if (c.ai_visual_status or "").lower() in _AI_VISION_ENABLE
            ),
        }

    organize_stats = None
    ai_organize_skipped = True
    if (out.layout_mode or "rules") == "explicit":
        try:
            layout = json.loads(out.channel_layout or '{"groups":[]}')
            groups = layout.get("groups") or []
            if groups:
                ai_organize_skipped = False
                organize_stats = {"groups": len(groups)}
        except (TypeError, ValueError, json.JSONDecodeError):
            pass

    return format_output_update_status(
        trigger,
        sync=sync,
        screenshot=screenshot_stats,
        ai_vision=ai_vision_stats,
        ai_organize=organize_stats,
        screenshot_skipped=screenshot_skipped,
        ai_vision_skipped=ai_vision_skipped,
        ai_organize_skipped=ai_organize_skipped,
    )


def _serialize_output_payload(out: OutputSource, total: int, enabled: int, disabled: int) -> Dict[str, Any]:
    data = out.model_dump()
    data.update(
        {
            "total_count": total,
            "enabled_count": enabled,
            "disabled_count": disabled,
        }
    )
    for key in ("last_updated", "last_request_time", "preview_cache_at"):
        value = data.get(key)
        if value is not None and hasattr(value, "isoformat"):
            data[key] = value.isoformat()
    return data


async def broadcast_output_update(session: Session, output_id: int) -> None:
    """广播聚合源卡片字段（含成员统计）。"""
    out = session.get(OutputSource, output_id)
    if not out:
        return

    pool, enabled_sub_ids = load_enabled_subscription_channel_pool(session)
    total, enabled, disabled = get_or_refresh_member_stats(
        session,
        out,
        pool,
        enabled_sub_ids,
        force=True,
    )
    session.add(out)
    session.commit()
    session.refresh(out)

    if not notifier.active_connections:
        return

    await notifier.broadcast(
        {
            "type": "output_update",
            "output_id": output_id,
            "data": _serialize_output_payload(out, total, enabled, disabled),
        }
    )


async def broadcast_channel_patch(output_id: int, channels: List[dict]) -> None:
    if not channels or not notifier.active_connections:
        return
    await notifier.broadcast(
        {
            "type": "channel_patch",
            "output_id": output_id,
            "channels": channels,
        }
    )


async def broadcast_preview_stats(output_id: int, total: int, enabled: int) -> None:
    if not notifier.active_connections:
        return
    await notifier.broadcast(
        {
            "type": "preview_stats",
            "output_id": output_id,
            "total": total,
            "enabled": enabled,
        }
    )


async def broadcast_preview_layout(output_id: int, layout_mode: str, group_count: int) -> None:
    if not notifier.active_connections:
        return
    await notifier.broadcast(
        {
            "type": "preview_layout",
            "output_id": output_id,
            "layout_mode": layout_mode,
            "group_count": group_count,
        }
    )


async def refresh_output_and_broadcast(
    session: Session,
    output_id: int,
    *,
    status_text: Optional[str] = None,
) -> None:
    """刷新 last_updated / 成员统计并推送 output_update。"""
    out = session.get(OutputSource, output_id)
    if not out:
        return

    out.last_updated = datetime.utcnow()
    if status_text is not None:
        out.last_update_status = status_text

    pool, enabled_sub_ids = load_enabled_subscription_channel_pool(session)
    get_or_refresh_member_stats(session, out, pool, enabled_sub_ids, force=True)
    session.add(out)
    session.commit()
    await broadcast_output_update(session, output_id)


async def broadcast_output_update_by_id(output_id: int) -> None:
    with Session(engine) as session:
        await broadcast_output_update(session, output_id)


def schedule_output_broadcast(output_id: int) -> None:
    """同步上下文调度 output_update（如 apply_output_update_status）。"""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(broadcast_output_update_by_id(output_id))
    except Exception:
        pass