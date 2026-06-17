"""聚合源更新后的自动后处理链路。"""

from typing import List, Optional, Tuple

from sqlmodel import Session, select

from models import Channel, OutputSource, Subscription
from services.output_resolver import aggregate_channels, filter_candidates
from services.playlist_organizer import PlaylistOrganizer
from services.stream_checker import StreamChecker
from services.visual_ai_checker import VisualAiChecker
from task_broker import push_console_log, update_task_status
from services.update_status_report import format_output_update_status, apply_output_update_status


def _screenshot_tail_candidates(channels: List[Channel]) -> List[Channel]:
    """扫尾补截图：从未检测且无截图的聚合成员。"""
    return [
        c for c in channels
        if c.check_date is None and not c.check_image
    ]


def _aggregate_screenshot_stats(session: Session, out: OutputSource) -> dict:
    members = aggregate_channels(session, out, None)
    enabled = sum(1 for c in members if c.is_enabled)
    return {"enabled": enabled, "disabled": len(members) - enabled}


async def _enforce_failed_check_disablement(
    session: Session,
    out: OutputSource,
) -> int:
    """截图已判定失败但仍启用的频道，强制禁用。"""
    from services.realtime_push import (
        broadcast_channel_patch,
        broadcast_preview_stats,
        channel_patch_fields,
    )

    members = aggregate_channels(session, out, None)
    changed: List[Channel] = []
    for ch in members:
        if ch.check_status is not False or not ch.is_enabled or ch.id is None:
            continue
        row = session.get(Channel, ch.id)
        if not row or not row.is_enabled:
            continue
        row.is_enabled = False
        session.add(row)
        changed.append(row)
    if not changed:
        return 0
    session.commit()
    for row in changed:
        session.refresh(row)
    if out.id is not None:
        patches = [channel_patch_fields(c) for c in changed]
        await broadcast_channel_patch(out.id, patches)
        members = aggregate_channels(session, out, None)
        enabled_n = sum(1 for c in members if c.is_enabled)
        await broadcast_preview_stats(out.id, len(members), enabled_n)
    return len(changed)


def _planned_steps(out: OutputSource) -> List[str]:
    steps: List[str] = []
    if out.auto_visual_check:
        steps.append("screenshot")
    if out.auto_ai_vision_check:
        steps.append("ai_vision")
    if out.auto_ai_organize:
        steps.append("ai_organize")
    return steps


async def _run_screenshot_step(
    session: Session,
    out: OutputSource,
    *,
    task_id: Optional[str],
    source: str,
    force_check: bool,
) -> tuple[bool, Optional[dict]]:
    channels = aggregate_channels(session, out, None)
    if not channels:
        if task_id:
            await update_task_status(task_id, progress=60, message="无匹配频道需截图检测")
        return True, {"enabled": 0, "disabled": 0}

    before_enabled = sum(1 for c in channels if c.is_enabled)
    auto_disable = getattr(out, "auto_disable_on_check", True)
    check_source = "manual" if force_check else source
    result = await StreamChecker.run_batch_check(
        session,
        channels,
        source=check_source,
        task_id=task_id,
        auto_disable=auto_disable,
        output_id=out.id,
    )
    if result is False:
        return False, None
    return True, _aggregate_screenshot_stats(session, out)


async def _run_screenshot_sweep_step(
    session: Session,
    out: OutputSource,
    *,
    task_id: Optional[str],
    source: str,
    force_check: bool,
) -> Tuple[bool, int]:
    """扫尾：对漏检且无图的聚合成员补跑截图。"""
    auto_disable = getattr(out, "auto_disable_on_check", True)
    pending = _screenshot_tail_candidates(aggregate_channels(session, out, None))
    if not pending:
        return True, 0

    if task_id:
        await update_task_status(
            task_id,
            progress=68,
            message=f"扫尾补截图（{len(pending)} 路漏检）...",
        )
    check_source = "manual" if force_check else source
    result = await StreamChecker.run_batch_check(
        session,
        pending,
        source=check_source,
        task_id=task_id,
        auto_disable=auto_disable,
        output_id=out.id,
    )
    if result is False:
        return False, 0
    return True, len(pending)


async def _run_ai_vision_step(
    session: Session,
    out: OutputSource,
    *,
    task_id: Optional[str],
) -> Optional[dict]:
    channels = [c for c in aggregate_channels(session, out, None) if c.is_enabled]
    if not channels:
        if task_id:
            await update_task_status(task_id, progress=85, message="无启用频道需 AI 视觉检测")
        return {"disabled": 0, "enabled": 0, "updated": 0}

    async def _progress(done, total, msg):
        if not task_id:
            return
        p = 70 + int((done / max(total, 1)) * 20)
        await update_task_status(task_id, progress=p, message=msg)

    stats = await VisualAiChecker.run_batch(
        session,
        channels,
        capture_missing=True,
        task_id=task_id,
        progress_cb=_progress,
        out=out,
    )
    summary = (
        f"[AI视觉] 聚合 id={out.id}：禁用 {stats.get('disabled', 0)}，"
        f"启用 {stats.get('enabled', 0)}，更新 {stats.get('updated', 0)}"
    )
    print(summary)
    await push_console_log(summary)
    return {
        "disabled": stats.get("disabled", 0),
        "enabled": stats.get("enabled", 0),
        "updated": stats.get("updated", 0),
    }


async def _run_ai_organize_step(
    session: Session,
    out: OutputSource,
    *,
    task_id: Optional[str],
) -> Optional[dict]:
    channels = aggregate_channels(session, out, None)
    if not channels:
        if task_id:
            await update_task_status(task_id, progress=90, message="无筛选频道需 AI 排序")
        return {"groups": 0}

    subs = session.exec(select(Subscription)).all()
    sub_map = {s.id: s.name or s.url for s in subs}
    layout = await PlaylistOrganizer.organize_output(session, out, None, sub_map)
    group_count = len(layout.get("groups", []))
    if task_id:
        await update_task_status(
            task_id,
            progress=95,
            message=f"AI 排序完成，共 {group_count} 个分组",
        )
    from services.realtime_push import broadcast_preview_layout

    await broadcast_preview_layout(out.id, out.layout_mode or "explicit", group_count)
    return {"groups": group_count}


async def run_output_postprocess_chain(
    session: Session,
    output_id: int,
    *,
    task_id: Optional[str] = None,
    source: str = "auto",
    force_check: bool = False,
    sync_ok: bool = True,
    rebuild_artifacts: bool = True,
) -> List[str]:
    """按固定顺序执行：截图 -> AI 视觉 -> AI 排序，并写入汇总 last_update_status。"""
    out = session.get(OutputSource, output_id)
    if not out:
        return []

    planned = _planned_steps(out)
    trigger = "manual" if source == "manual" else "auto"
    screenshot_stats = None
    vision_stats = None
    organize_stats = None

    if not planned:
        status = format_output_update_status(
            trigger,
            sync=sync_ok,
            screenshot_skipped=True,
            ai_vision_skipped=True,
            ai_organize_skipped=True,
        )
        apply_output_update_status(session, output_id, status)
        return []

    if task_id:
        await update_task_status(task_id, progress=55, message="开始自动后处理...")

    if "screenshot" in planned:
        if task_id:
            await update_task_status(task_id, progress=58, message="自动截图检测（全部筛选频道）...")
        ok, screenshot_stats = await _run_screenshot_step(
            session,
            out,
            task_id=task_id,
            source=source,
            force_check=force_check,
        )
        if not ok:
            apply_output_update_status(
                session,
                output_id,
                format_output_update_status(trigger, sync=sync_ok, screenshot={"enabled": 0, "disabled": 0}),
            )
            return planned

        sweep_ok, swept_n = await _run_screenshot_sweep_step(
            session,
            out,
            task_id=task_id,
            source=source,
            force_check=force_check,
        )
        if not sweep_ok:
            apply_output_update_status(
                session,
                output_id,
                format_output_update_status(trigger, sync=sync_ok, screenshot={"enabled": 0, "disabled": 0}),
            )
            return planned

        disabled_n = await _enforce_failed_check_disablement(session, out)
        screenshot_stats = _aggregate_screenshot_stats(session, out)
        if swept_n or disabled_n:
            msg = f"截图扫尾：补检 {swept_n} 路"
            if disabled_n:
                msg += f"，强制禁用 {disabled_n} 路"
            print(f"[Screenshot] 聚合 id={out.id} {msg}")
            await push_console_log(f"[截图] 聚合 id={out.id} {msg}")

    if "ai_vision" in planned:
        if task_id:
            await update_task_status(task_id, progress=72, message="自动 AI 视觉检测（仅启用频道）...")
        vision_stats = await _run_ai_vision_step(session, out, task_id=task_id)

    if "ai_organize" in planned:
        if task_id:
            await update_task_status(task_id, progress=88, message="自动 AI 排序（仅启用频道）...")
        organize_stats = await _run_ai_organize_step(session, out, task_id=task_id)

    status = format_output_update_status(
        trigger,
        sync=sync_ok,
        screenshot=screenshot_stats if "screenshot" in planned else None,
        ai_vision=vision_stats if "ai_vision" in planned else None,
        ai_organize=organize_stats if "ai_organize" in planned else None,
        screenshot_skipped="screenshot" not in planned,
        ai_vision_skipped="ai_vision" not in planned,
        ai_organize_skipped="ai_organize" not in planned,
    )
    apply_output_update_status(session, output_id, status)

    out = session.get(OutputSource, output_id)
    if out:
        from services.output_stats import invalidate_output_runtime_cache

        invalidate_output_runtime_cache(out, schedule_rebuild=rebuild_artifacts)

    if task_id:
        await update_task_status(
            task_id,
            status="success",
            progress=100,
            message=status,
        )
    return planned


async def enforce_screenshot_fail_disablement(session: Session, output_id: int) -> int:
    """对外：聚合范围内截图失败仍启用的频道强制禁用。"""
    out = session.get(OutputSource, output_id)
    if not out:
        return 0
    return await _enforce_failed_check_disablement(session, out)


from database import engine
from task_broker import broker


@broker.task
async def output_postprocess_task(
    task_id: str,
    output_id: int,
    source: str = "auto",
    force_check: bool = False,
):
    """Taskiq 入口：聚合源更新后串行执行自动后处理。"""
    await update_task_status(task_id, status="running", progress=5, message="自动后处理排队完成，开始执行...")
    with Session(engine) as session:
        await run_output_postprocess_chain(
            session,
            output_id,
            task_id=task_id,
            source=source,
            force_check=force_check,
        )
