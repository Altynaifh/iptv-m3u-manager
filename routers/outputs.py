from fastapi import APIRouter, HTTPException, Depends, Response
from sqlmodel import Session, select
from typing import List, Dict, Any
import json
from datetime import datetime, timedelta
import re

from models import OutputSource, Subscription, Channel, TaskRecord
from database import get_session
from services.generator import M3UGenerator
from services.epg import fetch_epg_cached
from services.stream_checker import StreamChecker
from routers.subscriptions import process_subscription_refresh
from services.output_resolver import export_m3u_channels, filter_candidates, preview_export_groups, aggregate_channels
from services.preview_cache import clear_output_preview_cache, get_or_build_export_preview
from services.output_stats import (
    get_or_refresh_member_stats,
    invalidate_output_runtime_cache,
    load_enabled_subscription_channel_pool,
)
import uuid

router = APIRouter(tags=["outputs"])


def _normalize_keyword_rules(raw_keywords: list) -> list:
    """统一关键字规则结构，缺省按频道名匹配"""
    keywords = []
    for k in raw_keywords or []:
        if isinstance(k, str):
            keywords.append({"value": k, "group": "", "match_by": "name"})
        elif isinstance(k, dict):
            item = dict(k)
            if not item.get("match_by"):
                item["match_by"] = "name"
            keywords.append(item)
    return keywords


@router.get("/outputs/source-groups")
def list_source_groups(subscription_ids: str = "", session: Session = Depends(get_session)):
    """列出已选订阅源内的 group-title 及频道数量"""
    sub_ids = []
    if subscription_ids.strip():
        for part in subscription_ids.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                sub_ids.append(int(part))
            except ValueError:
                continue
    enabled_subs = session.exec(select(Subscription.id).where(Subscription.is_enabled == True)).all()
    active_sub_ids = [sid for sid in sub_ids if sid in enabled_subs] if sub_ids else list(enabled_subs)
    if not active_sub_ids:
        return {"groups": []}
    channels = session.exec(
        select(Channel).where(Channel.subscription_id.in_(active_sub_ids))
    ).all()
    counts: Dict[str, int] = {}
    for ch in channels:
        label = (ch.group or "").strip() or "Default"
        counts[label] = counts.get(label, 0) + 1
    groups = [{"name": name, "count": count} for name, count in sorted(counts.items(), key=lambda x: x[0])]
    return {"groups": groups}

@router.post("/outputs/", response_model=OutputSource)
async def create_output(out: OutputSource, session: Session = Depends(get_session)):
    """新建聚合源"""
    if out.epg_url:
        await fetch_epg_cached(out.epg_url, refresh=True)
        
    session.add(out)
    session.commit()
    session.refresh(out)
    return out

@router.get("/outputs/")
def list_outputs(refresh_stats: bool = False, session: Session = Depends(get_session)):
    """聚合源列表，包含详细统计（成员数使用 DB 缓存，避免重复全表扫描）。"""
    outputs = session.exec(select(OutputSource)).all()
    needs_compute = refresh_stats or any(out.member_total is None for out in outputs)
    pool: List[Channel] = []
    enabled_sub_ids: set = set()
    if needs_compute:
        pool, enabled_sub_ids = load_enabled_subscription_channel_pool(session)
    results = []
    stats_updated = False

    for out in outputs:
        if needs_compute and (refresh_stats or out.member_total is None):
            total, enabled, disabled = get_or_refresh_member_stats(
                session,
                out,
                pool,
                enabled_sub_ids,
                force=refresh_stats,
            )
            stats_updated = True
        else:
            total = out.member_total or 0
            enabled = out.member_enabled or 0
            disabled = out.member_disabled or 0

        out_dict = out.model_dump()
        out_dict.update({
            "total_count": total,
            "enabled_count": enabled,
            "disabled_count": disabled,
        })
        results.append(out_dict)

    if stats_updated:
        session.commit()

    return results

@router.get("/outputs/{output_id}")
def get_output(output_id: int, session: Session = Depends(get_session)):
    """获取单个聚合源完整配置（编辑回读）"""
    out = session.get(OutputSource, output_id)
    if not out:
        raise HTTPException(status_code=404, detail="输出源不存在")
    return out.model_dump()


@router.delete("/outputs/{output_id}")
def delete_output(output_id: int, session: Session = Depends(get_session)):
    """删除聚合源"""
    out = session.get(OutputSource, output_id)
    if not out:
        raise HTTPException(status_code=404, detail="输出源不存在")
    session.delete(out)
    session.commit()
    return {"message": "删除成功"}

@router.put("/outputs/{output_id}", response_model=OutputSource)
def update_output(output_id: int, output_data: OutputSource, session: Session = Depends(get_session)):
    """更新聚合配置"""
    output = session.get(OutputSource, output_id)
    if not output:
        raise HTTPException(status_code=404, detail="输出源不存在")
    
    # Slug 变了得检查重名
    if output_data.slug != output.slug:
        existing = session.exec(select(OutputSource).where(OutputSource.slug == output_data.slug)).first()
        if existing:
            raise HTTPException(status_code=400, detail="Slug 已被占用")

    output.name = output_data.name
    output.slug = output_data.slug
    output.filter_regex = output_data.filter_regex
    output.keywords = output_data.keywords
    output.subscription_ids = output_data.subscription_ids
    output.epg_url = output_data.epg_url
    output.include_source_suffix = output_data.include_source_suffix
    output.is_enabled = output_data.is_enabled
    output.auto_update_minutes = output_data.auto_update_minutes
    output.auto_visual_check = output_data.auto_visual_check
    output.auto_ai_vision_check = getattr(output_data, 'auto_ai_vision_check', False) or False
    output.auto_disable_on_check = getattr(output_data, 'auto_disable_on_check', True)
    output.auto_ai_organize = getattr(output_data, 'auto_ai_organize', False) or False
    output.enable_ai_vision = getattr(output_data, 'enable_ai_vision', False) or False
    output.enable_ai_organize = getattr(output_data, 'enable_ai_organize', False) or False
    output.ai_organize_prompt = getattr(output_data, 'ai_organize_prompt', '') or ''
    output.excluded_channel_ids = output_data.excluded_channel_ids
    output.layout_mode = output_data.layout_mode or 'rules'
    output.channel_layout = output_data.channel_layout or '{"groups":[]}'
    output.layout_meta = output_data.layout_meta or '{}'
    invalidate_output_runtime_cache(output)

    session.add(output)
    session.commit()
    session.refresh(output)
    return output



@router.get("/outputs/{output_id}/export-preview")
def export_preview_output(
    output_id: int,
    force: bool = False,
    session: Session = Depends(get_session),
):
    """聚合列表预览：按导出分组（手动 / AI explicit）返回频道；默认使用服务端缓存。"""
    out = session.get(OutputSource, output_id)
    if not out:
        raise HTTPException(status_code=404, detail="输出源不存在")
    return get_or_build_export_preview(session, out, None, force=force)


@router.post("/outputs/preview")
def preview_output(data: dict, session: Session = Depends(get_session)):
    """预览结果"""
    sub_ids = data.get("subscription_ids", [])
    raw_keywords = data.get("keywords", [])
    regex = data.get("filter_regex", ".*")
    excluded_ids = data.get("excluded_channel_ids", [])  # 聚合表级别排除
    # 确保 ID 都是整数，防止前端传字符串导致匹配失败
    try:
        excluded_set = {int(i) for i in excluded_ids} if excluded_ids else set()
    except:
        excluded_set = set()
    
    # 整理关键字列表
    keywords = _normalize_keyword_rules(raw_keywords)

    # 只要启用了的预览
    enabled_subs = session.exec(select(Subscription.id).where(Subscription.is_enabled == True)).all()
    active_sub_ids = [sid for sid in sub_ids if sid in enabled_subs] if sub_ids else enabled_subs

    if active_sub_ids:
        channels = session.exec(select(Channel).where(Channel.subscription_id.in_(active_sub_ids))).all()
    else:
        channels = []
    
    # 预览时不在此处过滤，由前端通过 excluded_channel_ids 显示恢复/排除按钮
    # if excluded_set:
    #     channels = [c for c in channels if c.id not in excluded_set]
        
    # 获取订阅名，方便看来源
    subs = session.exec(select(Subscription)).all()
    sub_map = {s.id: s.name or s.url for s in subs}

    # 应用正则过滤
    if regex and regex != ".*":
        try:
            pattern = re.compile(regex, re.IGNORECASE)
            channels = [c for c in channels if pattern.search(c.name)]
        except:
            pass

    results = {}
    if not keywords:
        # 没搜到关键字就全给它
        channels = M3UGenerator.propagate_logos(channels)
        results["All"] = [
            {**c.model_dump(), "source": sub_map.get(c.subscription_id, "Unknown")} 
            for c in channels 
        ]
    else:
        channels = M3UGenerator.propagate_logos(channels)
        for k_obj, matches in M3UGenerator.build_rule_preview_buckets(channels, keywords):
            display_key = M3UGenerator.rule_display_key(k_obj)
            results[display_key] = [
                {**c.model_dump(), "source": sub_map.get(c.subscription_id, "Unknown")}
                for c in matches
            ]
            
    return results


@router.post("/outputs/{output_id}/refresh")
async def refresh_output(output_id: int, session: Session = Depends(get_session)):
    """手动刷新：提交 Taskiq 任务后立即返回。"""
    out = session.get(OutputSource, output_id)
    if not out:
        raise HTTPException(status_code=404, detail="输出源不存在")

    from services.output_refresh import refresh_output_task

    task_id = str(uuid.uuid4())
    task_record = TaskRecord(
        id=task_id,
        name=f"刷新聚合: {out.name}",
        status="pending",
        progress=0,
        message="任务排队中...",
    )
    session.add(task_record)
    session.commit()

    await refresh_output_task.kiq(task_id=task_id, output_id=output_id)

    return {"message": "任务已提交", "task_id": task_id}



async def run_output_ai_visual_check(output_id: int, task_id: str):
    """聚合刷新后：AI 画面检测（四宫格）。"""
    from database import engine
    from sqlmodel import Session
    from task_broker import push_console_log, update_task_status
    from services.output_resolver import filter_candidates
    from services.visual_ai_checker import VisualAiChecker

    try:
        with Session(engine) as session:
            out = session.get(OutputSource, output_id)
            if not out:
                return
            from services.output_resolver import organize_candidates
            channels = organize_candidates(session, out, None)
            if not channels:
                await update_task_status(task_id, status="success", progress=100, message="无启用频道需 AI 画面检测")
                return

            async def _progress(done, total, msg):
                p = 55 + int((done / max(total, 1)) * 40)
                await update_task_status(task_id, progress=p, message=msg)

            stats = await VisualAiChecker.run_batch(session, channels, capture_missing=True, progress_cb=_progress)
            out = session.get(OutputSource, output_id)
            if out:
                out.last_update_status = "手动更新+AI画面检测完成"
                session.add(out)
                session.commit()
            summary = (
                f"[AI视觉] 自动链 聚合 id={output_id}：禁用 {stats.get('disabled', 0)}，"
                f"启用 {stats.get('enabled', 0)}，更新 {stats.get('updated', 0)}"
            )
            print(summary)
            await push_console_log(summary)
            await update_task_status(task_id, status="success", progress=100, message=f"AI 画面检测完成 {stats}")
    except Exception as e:
        err_line = f"[AI视觉] 自动链 聚合 id={output_id} 失败：{str(e)[:500]}"
        print(err_line)
        await push_console_log(err_line)
        await update_task_status(task_id, status="failure", message=str(e)[:500])

async def run_output_visual_check_v2(output_id: int, task_id: str, force_check: bool = False):
    """(优化版) 后台运行深度检测，接管已有 TaskID"""
    from database import engine
    from sqlmodel import Session
    from task_broker import update_task_status
    
    with Session(engine) as session:
        out = session.get(OutputSource, output_id)
        if not out: return
        
        try:
            sub_ids = json.loads(out.subscription_ids)
            raw_channels = []
            for sid in sub_ids:
                chs = session.exec(select(Channel).where(Channel.subscription_id == sid)).all()
                raw_channels.extend(chs)
            
            try: keywords = json.loads(out.keywords)
            except: keywords = []
            
            from services.generator import M3UGenerator
            matched_channels = M3UGenerator.filter_channels(raw_channels, out.filter_regex, keywords)
            
            if matched_channels:
                from services.stream_checker import StreamChecker
                check_source = 'manual' if force_check else 'auto'
                auto_disable = getattr(out, 'auto_disable_on_check', True)
                check_result = await StreamChecker.run_batch_check(session, matched_channels, source=check_source, task_id=task_id, auto_disable=auto_disable)
                
                # 如果检测因中止而提前退出，严禁发送成功广播
                if check_result is False:
                    print(f"[run_output_visual_check_v2] 任务 {task_id} 已由用户中止，跳过成功广播")
                    return
                
                # 再次同步聚合源状态
                out = session.get(OutputSource, output_id)
                if out:
                    out.last_update_status = "手动更新+深度检测完成"
                    session.add(out)
                    session.commit()
                
                # 最终出口防御：硬判状态再广播
                with Session(engine) as check_session:
                    task = check_session.get(TaskRecord, task_id)
                    if task and task.status == "canceled":
                        print(f"[run_output_visual_check_v2] 任务 {task_id} 已处于取消状态，跳过最终成功广播")
                        return
                
                await update_task_status(task_id, status="success", progress=100, message="更新与检测全部完成")
            else:
                await update_task_status(task_id, status="success", progress=100, message="刷新完成 (无匹配频道需检测)")
        except Exception as e:
            await update_task_status(task_id, status="failure", message=f"检测执行出错: {e}")

async def run_output_visual_check(output_id: int, force_check: bool = False):
    """后台运行深度检测"""
    from database import engine
    from sqlmodel import Session
    
    with Session(engine) as session:
        out = session.get(OutputSource, output_id)
        if not out: return
        
        # 创建一个异步任务记录，以便“刷新节目表”后触发的检测也能在任务中心看到
        import uuid
        from models import TaskRecord
        from task_broker import update_task_status
        task_id = str(uuid.uuid4())
        task_record = TaskRecord(
            id=task_id,
            name=f"刷新聚合检测: {out.name}",
            status="pending",
            progress=0,
            message="正在准备检测..."
        )
        session.add(task_record)
        session.commit()
        
        # 初始广播
        await update_task_status(task_id, status="pending", progress=0, message="任务排队中")

        try:
            sub_ids = json.loads(out.subscription_ids)
            raw_channels = []
            for sid in sub_ids:
                chs = session.exec(select(Channel).where(Channel.subscription_id == sid)).all()
                raw_channels.extend(chs)
            
            try:
                keywords = json.loads(out.keywords)
            except:
                keywords = []
            
            from services.generator import M3UGenerator
            matched_channels = M3UGenerator.filter_channels(raw_channels, out.filter_regex, keywords)
            
            # 彻底移除冷却限制：只要触发此任务，就对所有匹配频道进行探测
            pending_channels = matched_channels
            
            if pending_channels:
                print(f"[后台检测] 聚合源 {out.id} 触发同步深度检测，待测: {len(pending_channels)}")
                from services.stream_checker import StreamChecker
                check_source = 'manual' if force_check else 'auto'
                
                # 传入 task_id 以便更新进度
                auto_disable = getattr(out, 'auto_disable_on_check', True)
                await StreamChecker.run_batch_check(session, pending_channels, source=check_source, task_id=task_id, auto_disable=auto_disable)
                
                # 重新获取对象并更新状态
                out = session.get(OutputSource, output_id)
                if out:
                    out.last_update_status = "手动更新+深度检测完成"
                    session.add(out)
                    session.commit()
                
                await update_task_status(task_id, status="success", progress=100, message="检测完成")
                print(f"[后台检测] 聚合源 {out.id} 检测完成。")
            else:
                await update_task_status(task_id, status="success", progress=100, message="无匹配频道需要检测")
        except Exception as e:
            print(f"[后台检测] 聚合源 {out.id} 执行失败: {e}")

@router.get("/m3u/{slug}")
async def get_m3u_output(slug: str, session: Session = Depends(get_session)):
    """下载 M3U"""
    out = session.exec(select(OutputSource).where(OutputSource.slug == slug)).first()
    if not out:
        raise HTTPException(status_code=404, detail="输出源不存在")
    

    out.last_request_time = datetime.utcnow()
    session.add(out)
    session.commit()
    session.refresh(out) # 确保状态同步
    
    # 检查是否启用
    if not out.is_enabled:
        return Response(content="#EXTM3U\n# 频道已暂时下线，请在后台启用该聚合源后重试。", media_type="text/plain; charset=utf-8")

    try:
        sub_ids = json.loads(out.subscription_ids)
    except:
        sub_ids = []
    
    # 取出刷新的最新频道
    enabled_subs = session.exec(select(Subscription.id).where(Subscription.is_enabled == True)).all()
    active_sub_ids = [sid for sid in sub_ids if sid in enabled_subs] if sub_ids else enabled_subs

    if active_sub_ids:
        # 只要启用了的
        channels = session.exec(select(Channel).where(
            Channel.subscription_id.in_(active_sub_ids),
            Channel.is_enabled == True
        )).all()
    else:
        channels = []

    subs = session.exec(select(Subscription)).all()
    sub_map = {s.id: s.name or s.url for s in subs}

    try:
        raw_keywords = json.loads(out.keywords)
        keywords = _normalize_keyword_rules(raw_keywords)
    except:
        keywords = []
    
    # 解析排除列表
    try:
        excluded_ids = json.loads(out.excluded_channel_ids or "[]")
    except:
        excluded_ids = []
        
    filtered = export_m3u_channels(session, out)
    m3u_content = M3UGenerator.generate_m3u(filtered, sub_map, out.epg_url, out.include_source_suffix)
    return Response(content=m3u_content, media_type="application/x-mpegurl; charset=utf-8")

@router.post("/outputs/{output_id}/layout-mode")
def set_layout_mode(output_id: int, data: dict, session: Session = Depends(get_session)):
    out = session.get(OutputSource, output_id)
    if not out:
        raise HTTPException(status_code=404, detail="输出源不存在")
    mode = (data.get("layout_mode") or "rules").strip()
    if mode not in ("rules", "explicit"):
        raise HTTPException(status_code=400, detail="layout_mode 无效")
    out.layout_mode = mode
    invalidate_output_runtime_cache(out)
    session.add(out)
    session.commit()
    return {"layout_mode": out.layout_mode}


@router.post("/outputs/{output_id}/llm-organize")
async def llm_organize_output(output_id: int, data: dict, session: Session = Depends(get_session)):
    out = session.get(OutputSource, output_id)
    if not out:
        raise HTTPException(status_code=404, detail="输出源不存在")
    draft = None
    if data.get("use_draft"):
        draft = {
            "subscription_ids": data.get("subscription_ids") or [],
            "keywords": data.get("keywords") or [],
            "excluded_channel_ids": data.get("excluded_channel_ids") or [],
            "filter_regex": data.get("filter_regex") or out.filter_regex,
            "layout_mode": data.get("layout_mode") or out.layout_mode,
            "channel_layout": data.get("channel_layout") or out.channel_layout,
            "ai_organize_prompt": (data.get("ai_organize_prompt") or "").strip() if data.get("use_draft") else (out.ai_organize_prompt or ""),
        }
    task_id = str(uuid.uuid4())
    task_record = TaskRecord(
        id=task_id,
        name=f"AI 整理节目表: {out.name}",
        status="pending",
        message="排队中...",
    )
    session.add(task_record)
    session.commit()
    from services.llm_tasks import llm_organize_task
    import json as _json
    await llm_organize_task.kiq(
        task_id=task_id,
        output_id=output_id,
        draft_json=_json.dumps(draft, ensure_ascii=False) if draft else None,
    )
    return {"task_id": task_id, "message": "已提交 AI 整理任务"}


@router.post("/outputs/{output_id}/ai-visual-check")
async def ai_visual_check_output(output_id: int, data: dict, session: Session = Depends(get_session)):
    out = session.get(OutputSource, output_id)
    if not out:
        raise HTTPException(status_code=404, detail="输出源不存在")
    draft = None
    if data.get("use_draft"):
        draft = {
            "subscription_ids": data.get("subscription_ids") or [],
            "keywords": data.get("keywords") or [],
            "excluded_channel_ids": data.get("excluded_channel_ids") or [],
            "filter_regex": data.get("filter_regex") or out.filter_regex,
        }
    channel_ids = data.get("channel_ids")
    capture_missing = data.get("capture_missing", True)
    task_id = str(uuid.uuid4())
    task_record = TaskRecord(
        id=task_id,
        name=f"AI 画面检测: {out.name}",
        status="pending",
        message="排队中...",
    )
    session.add(task_record)
    session.commit()
    from services.llm_tasks import ai_visual_check_task
    import json as _json
    await ai_visual_check_task.kiq(
        task_id=task_id,
        output_id=output_id,
        channel_ids=channel_ids,
        capture_missing=capture_missing,
        draft_json=_json.dumps(draft, ensure_ascii=False) if draft else None,
    )
    return {"task_id": task_id, "message": "已提交 AI 画面检测任务"}
