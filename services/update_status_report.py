"""聚合源 last_update_status 汇总文案。"""

from __future__ import annotations

from typing import Dict, List, Optional


def _mark(ok: bool, skip: bool = False) -> str:
    if skip:
        return "—"
    return "✓" if ok else "✗"


def format_output_update_status(
    trigger: str,
    *,
    sync: Optional[bool] = None,
    screenshot: Optional[Dict[str, int]] = None,
    ai_vision: Optional[Dict[str, int]] = None,
    ai_organize: Optional[Dict[str, int]] = None,
    screenshot_skipped: bool = False,
    ai_vision_skipped: bool = False,
    ai_organize_skipped: bool = False,
    error: Optional[str] = None,
) -> str:
    """生成首页「最后同步」括号内展示的全流程结果。"""
    trigger_label = "手动更新" if trigger == "manual" else "自动更新"
    if error:
        return f"{trigger_label} | 失败: {error[:120]}"

    parts: List[str] = [trigger_label]
    parts.append(f"订阅同步{_mark(sync is True, sync is None)}")

    if screenshot_skipped:
        parts.append("截图检查—")
    elif screenshot is not None:
        en = screenshot.get("enabled", 0)
        dis = screenshot.get("disabled", 0)
        parts.append(f"截图检查{_mark(True)}(启用{en}/禁用{dis})")
    else:
        parts.append(f"截图检查{_mark(False)}")

    if ai_vision_skipped:
        parts.append("AI视觉—")
    elif ai_vision is not None:
        parts.append(
            f"AI视觉{_mark(True)}(禁{ai_vision.get('disabled', 0)}/启{ai_vision.get('enabled', 0)})"
        )
    else:
        parts.append(f"AI视觉{_mark(False)}")

    if ai_organize_skipped:
        parts.append("AI排序—")
    elif ai_organize is not None:
        parts.append(f"AI排序{_mark(True)}({ai_organize.get('groups', 0)}组)")
    else:
        parts.append(f"AI排序{_mark(False)}")

    return " | ".join(parts)


def apply_output_update_status(session, output_id: int, status_text: str) -> None:
    from datetime import datetime

    from models import OutputSource
    from services.realtime_push import schedule_output_broadcast

    out = session.get(OutputSource, output_id)
    if not out:
        return
    out.last_update_status = status_text
    out.last_updated = datetime.utcnow()
    session.add(out)
    session.commit()
    schedule_output_broadcast(output_id)
