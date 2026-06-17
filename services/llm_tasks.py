"""LLM 相关 Taskiq 任务。"""
import json
from typing import Any, Dict, List, Optional

from sqlmodel import Session, select

from database import engine
from models import Channel, OutputSource, Subscription, TaskRecord
from services.output_resolver import ai_vision_candidates, export_m3u_channels, filter_candidates
from services.playlist_organizer import PlaylistOrganizer
from services.realtime_push import (
    broadcast_preview_layout,
    rebuild_manual_status_from_db,
    refresh_output_and_broadcast,
)
from services.ai_vision_log import mask_secret, vision_log, vision_log_exc, vision_push
from services.llm_settings import load_llm_blocks
from services.visual_ai_checker import VisualAiChecker
from task_broker import TaskCanceledError, broker, guard_task_cancellation, update_task_status


@broker.task
async def llm_organize_task(
    task_id: str,
    output_id: int,
    draft_json: Optional[str] = None,
):
    draft = json.loads(draft_json) if draft_json else None
    try:
        await update_task_status(task_id, status="running", progress=5, message="准备频道列表...")
        with Session(engine) as session:
            out = session.get(OutputSource, output_id)
            if not out:
                await update_task_status(task_id, status="failure", message="聚合源不存在")
                return
            subs = session.exec(select(Subscription)).all()
            sub_map = {s.id: s.name or s.url for s in subs}
            await update_task_status(task_id, progress=30, message="调用文本 LLM 编排...")
            layout = await guard_task_cancellation(
                task_id,
                PlaylistOrganizer.organize_output(session, out, draft, sub_map),
            )
            group_count = len(layout.get("groups", []))
            await update_task_status(
                task_id,
                status="success",
                progress=100,
                message=f"已生成 explicit 布局，{group_count} 个分组",
                result=json.dumps(layout, ensure_ascii=False),
            )
            from services.output_artifacts import schedule_rebuild_output_artifacts
            from services.output_stats import invalidate_output_runtime_cache

            invalidate_output_runtime_cache(out, schedule_rebuild=False)
            schedule_rebuild_output_artifacts(output_id, epg_refresh=False)
            status = rebuild_manual_status_from_db(session, output_id)
            await refresh_output_and_broadcast(session, output_id, status_text=status)
            await broadcast_preview_layout(output_id, out.layout_mode or "explicit", group_count)
    except TaskCanceledError:
        await update_task_status(task_id, status="canceled", message="任务已中止")
    except Exception as e:
        await update_task_status(task_id, status="failure", message=str(e)[:500])


@broker.task
async def ai_visual_check_task(
    task_id: str,
    output_id: int,
    channel_ids: Optional[List[int]] = None,
    capture_missing: bool = True,
    draft_json: Optional[str] = None,
):
    draft = json.loads(draft_json) if draft_json else None
    try:
        vision_log(
            f"任务入队 task_id={task_id} output_id={output_id} "
            f"channel_ids={'指定' + str(len(channel_ids or [])) if channel_ids else '自动'} "
            f"capture_missing={capture_missing} draft_json={'有' if draft_json else '无'}"
        )
        await update_task_status(task_id, status="running", progress=5, message="加载频道...")
        with Session(engine) as session:
            out = session.get(OutputSource, output_id)
            if not out:
                vision_log(f"任务失败 task_id={task_id} 原因=聚合源不存在")
                await update_task_status(task_id, status="failure", message="聚合源不存在")
                return

            blocks = load_llm_blocks(session)
            vision_cfg = blocks.get("llm_vision") or {}
            vision_log(
                f"任务配置 task_id={task_id} output={out.name!r} "
                f"llm_vision base_url={vision_cfg.get('base_url', '')!r} "
                f"model={vision_cfg.get('model', '')!r} "
                f"api_key={mask_secret(vision_cfg.get('api_key', ''))}"
            )
            await vision_push(
                f"任务 {task_id[:8]}… 聚合「{out.name}」视觉 LLM={vision_cfg.get('model') or '未配置'}"
            )

            if channel_ids:
                channels = list(
                    session.exec(select(Channel).where(Channel.id.in_(channel_ids))).all()
                )
            else:
                channels = ai_vision_candidates(session, out, draft)

            vision_log(f"任务频道列表 task_id={task_id} count={len(channels)}")
            if not channels:
                await update_task_status(task_id, status="success", progress=100, message="无频道需要检测")
                status = rebuild_manual_status_from_db(session, output_id)
                await refresh_output_and_broadcast(session, output_id, status_text=status)
                return

            async def _progress(done, total, msg):
                p = 10 + int((done / max(total, 1)) * 85)
                await update_task_status(task_id, progress=p, message=msg)

            stats = await guard_task_cancellation(
                task_id,
                VisualAiChecker.run_batch(
                    session,
                    channels,
                    capture_missing=capture_missing,
                    task_id=task_id,
                    progress_cb=_progress,
                    out=out,
                    draft=draft,
                ),
            )
            vision_log(f"任务成功 task_id={task_id} stats={stats}")
            await update_task_status(
                task_id,
                status="success",
                progress=100,
                message=f"视觉 AI 完成：{stats}",
                result=json.dumps(stats, ensure_ascii=False),
            )
            status = rebuild_manual_status_from_db(session, output_id)
            await refresh_output_and_broadcast(session, output_id, status_text=status)
    except TaskCanceledError:
        vision_log(f"任务中止 task_id={task_id}")
        await update_task_status(task_id, status="canceled", message="任务已中止")
        with Session(engine) as session:
            status = rebuild_manual_status_from_db(session, output_id)
            await refresh_output_and_broadcast(session, output_id, status_text=status)
    except UnicodeEncodeError as e:
        vision_log_exc(f"任务 latin-1 编码失败 task_id={task_id}", e)
        snippet = (e.object or "")[e.start : e.end] if isinstance(e.object, str) else ""
        msg = (
            "AI 视觉请求编码失败：HTTP 头或 URL 不能含中文"
            + (f"（问题片段：{snippet}）" if snippet else "")
            + "；请检查视觉 LLM 的 API Key / Base URL 配置"
        )
        await vision_push(msg[:200])
        await update_task_status(task_id, status="failure", message=msg[:500])
        with Session(engine) as session:
            status = rebuild_manual_status_from_db(session, output_id)
            await refresh_output_and_broadcast(session, output_id, status_text=status)
    except Exception as e:
        vision_log_exc(f"任务未捕获异常 task_id={task_id}", e)
        await vision_push(f"任务失败: {str(e)[:120]}")
        await update_task_status(task_id, status="failure", message=str(e)[:500])
        with Session(engine) as session:
            status = rebuild_manual_status_from_db(session, output_id)
            await refresh_output_and_broadcast(session, output_id, status_text=status)
