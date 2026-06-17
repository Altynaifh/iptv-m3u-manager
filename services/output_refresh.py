"""聚合源手动刷新 Taskiq 任务。"""

import json
from datetime import datetime

from sqlmodel import Session

from database import engine
from models import OutputSource, Subscription, TaskRecord
from routers.subscriptions import process_subscription_refresh
from services.epg import refresh_epg_for_output
from services.output_postprocess import run_output_postprocess_chain
from services.update_status_report import format_output_update_status, apply_output_update_status
from task_broker import broker, update_task_status


@broker.task
async def refresh_output_task(task_id: str, output_id: int):
    """手动刷新：同步关联订阅 → EPG → 自动后处理链。"""
    await update_task_status(
        task_id,
        status="running",
        progress=10,
        message="开始刷新关联订阅...",
    )
    try:
        with Session(engine) as session:
            out = session.get(OutputSource, output_id)
            if not out:
                await update_task_status(task_id, status="failure", message="输出源不存在")
                return

            try:
                sub_ids = json.loads(out.subscription_ids)
            except Exception:
                sub_ids = []

            sub_failures = 0
            sync_ok = True

            for i, sub_id in enumerate(sub_ids):
                with Session(engine) as check_session:
                    task = check_session.get(TaskRecord, task_id)
                    if not task or task.status == "canceled":
                        await update_task_status(
                            task_id,
                            status="canceled",
                            message="刷新作业已由用户中止",
                        )
                        return

                try:
                    sub = session.get(Subscription, sub_id)
                    if sub:
                        await process_subscription_refresh(session, sub, invalidate_outputs=False)
                        p = 10 + int((i + 1) / len(sub_ids) * 40) if sub_ids else 50
                        await update_task_status(
                            task_id,
                            progress=p,
                            message=f"已同步订阅: {sub.name or sub_id}",
                        )
                except Exception as e:
                    sub_failures += 1
                    print(f"[refresh_output_task] Sub {sub_id} failed: {e}")

            if sub_failures:
                sync_ok = False

            related_subs = []
            for sub_id in sub_ids:
                sub = session.get(Subscription, sub_id)
                if sub:
                    related_subs.append(sub)

            epg_sources = []
            if out.epg_url or any(getattr(s, "epg_url", None) for s in related_subs):
                await update_task_status(task_id, progress=50, message="正在更新节目表...")
                try:
                    epg_sources = await refresh_epg_for_output(
                        out.epg_url,
                        related_subs,
                        refresh=True,
                        reload_memory=True,
                    )
                    if epg_sources:
                        await update_task_status(
                            task_id,
                            progress=55,
                            message=f"节目表已更新（{len(epg_sources)} 个来源）",
                        )
                except Exception as e:
                    print(f"[refresh_output_task] EPG failed: {e}")

            out = session.get(OutputSource, output_id)
            if out:
                out.last_updated = datetime.utcnow()
                session.add(out)
                session.commit()

            steps = await run_output_postprocess_chain(
                session,
                output_id,
                task_id=task_id,
                source="manual",
                force_check=True,
                sync_ok=sync_ok,
                rebuild_artifacts=False,
            )

            from services.output_artifacts import schedule_rebuild_output_artifacts
            from services.output_stats import invalidate_output_runtime_cache

            out = session.get(OutputSource, output_id)
            if out:
                invalidate_output_runtime_cache(out, schedule_rebuild=False)
            # 节目表已在上方拉取，产物重建只读本地缓存做匹配
            schedule_rebuild_output_artifacts(output_id, epg_refresh=False)

            if not steps:
                apply_output_update_status(
                    session,
                    output_id,
                    format_output_update_status(
                        "manual",
                        sync=sync_ok,
                        screenshot_skipped=True,
                        ai_vision_skipped=True,
                        ai_organize_skipped=True,
                    ),
                )
                with Session(engine) as check_session:
                    task = check_session.get(TaskRecord, task_id)
                    if task and task.status != "canceled":
                        out2 = check_session.get(OutputSource, output_id)
                        msg = out2.last_update_status if out2 else "刷新完成"
                        await update_task_status(
                            task_id,
                            status="success",
                            progress=100,
                            message=msg,
                        )
    except Exception as e:
        print(f"[refresh_output_task] 失败: {e}")
        with Session(engine) as fs:
            apply_output_update_status(fs, output_id, format_output_update_status("manual", sync=False, error=str(e)))
        await update_task_status(task_id, status="failure", message=str(e)[:500])
