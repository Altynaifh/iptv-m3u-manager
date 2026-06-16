"""LLM 相关 Taskiq 任务。"""
import json
from typing import Any, Dict, List, Optional

from sqlmodel import Session, select

from database import engine
from models import Channel, OutputSource, Subscription, TaskRecord
from services.output_resolver import ai_vision_candidates, export_m3u_channels, filter_candidates
from services.playlist_organizer import PlaylistOrganizer
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
            await update_task_status(
                task_id,
                status="success",
                progress=100,
                message=f"已生成 explicit 布局，{len(layout.get('groups', []))} 个分组",
                result=json.dumps(layout, ensure_ascii=False),
            )
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
        await update_task_status(task_id, status="running", progress=5, message="加载频道...")
        with Session(engine) as session:
            out = session.get(OutputSource, output_id)
            if not out:
                await update_task_status(task_id, status="failure", message="聚合源不存在")
                return

            if channel_ids:
                channels = list(
                    session.exec(select(Channel).where(Channel.id.in_(channel_ids))).all()
                )
            else:
                channels = ai_vision_candidates(session, out, draft)

            if not channels:
                await update_task_status(task_id, status="success", progress=100, message="无频道需要检测")
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
            await update_task_status(
                task_id,
                status="success",
                progress=100,
                message=f"视觉 AI 完成：{stats}",
                result=json.dumps(stats, ensure_ascii=False),
            )
    except TaskCanceledError:
        await update_task_status(task_id, status="canceled", message="任务已中止")
    except Exception as e:
        await update_task_status(task_id, status="failure", message=str(e)[:500])
