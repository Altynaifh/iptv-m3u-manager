from fastapi import APIRouter, HTTPException, Depends
from sqlmodel import Session, select
from hashlib import md5
from typing import Any, Awaitable, Callable, Dict, List, Optional
from models import Subscription, Channel, TaskRecord
from database import get_session, engine
from services.fetcher import IPTVFetcher, fetch_subscription_task
from services.output_stats import invalidate_all_output_runtime_caches
from services.epg import fetch_epg_cached
from datetime import datetime
import uuid
from task_broker import update_task_status

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])

@router.post("/", response_model=dict)
async def create_subscription(sub: Subscription, session: Session = Depends(get_session)):
    """加个新订阅，顺便异步刷一遍"""
    print(f"[Action] 创建新订阅: {sub.name}")
    sub.url = (sub.url or "").strip()
    session.add(sub)
    session.commit()
    session.refresh(sub)
    
    # 创建异步任务记录
    task_id = str(uuid.uuid4())
    task_record = TaskRecord(
        id=task_id,
        name=f"首次同步订阅: {sub.name}",
        status="pending",
        progress=0,
        message="任务排队中..."
    )
    session.add(task_record)
    session.commit()

    # 派发异步任务
    await fetch_subscription_task.kiq(
        task_id=task_id,
        sub_id=sub.id,
        url_str=sub.url or "",
        ua=sub.user_agent or "AptvPlayer/1.4.1",
        headers_json=sub.headers or "{}"
    )
        
    return {"subscription": sub, "task_id": task_id}

@router.get("/", response_model=List[Subscription])
def list_subscriptions(session: Session = Depends(get_session)):
    """订阅源列表"""
    return session.exec(select(Subscription)).all()

@router.delete("/{sub_id}")
def delete_subscription(sub_id: int, session: Session = Depends(get_session)):
    """删除订阅源（连带频道一起删）"""
    sub = session.get(Subscription, sub_id)
    if not sub:
        raise HTTPException(status_code=404, detail="订阅不存在")
    
    print(f"[Action] 删除订阅: {sub.name} (ID: {sub_id})")
    # 频道得跟着一块走
    channels = session.exec(select(Channel).where(Channel.subscription_id == sub_id)).all()
    for c in channels:
        session.delete(c)
        
    session.delete(sub)
    session.commit()
    return {"message": "删除成功"}

@router.put("/{sub_id}", response_model=Subscription)
def update_subscription(sub_id: int, updated: Subscription, session: Session = Depends(get_session)):
    """修改订阅配置"""
    db_sub = session.get(Subscription, sub_id)
    if not db_sub:
        raise HTTPException(status_code=404, detail="订阅不存在")
    db_sub.name = updated.name
    db_sub.url = updated.url.strip()
    db_sub.user_agent = updated.user_agent
    db_sub.headers = updated.headers
    db_sub.auto_update_minutes = updated.auto_update_minutes
    db_sub.is_enabled = updated.is_enabled
    session.add(db_sub)
    session.commit()
    session.refresh(db_sub)
    return db_sub

@router.get("/{sub_id}/channels", response_model=List[Channel])
def get_subscription_channels(sub_id: int, session: Session = Depends(get_session)):
    """这个订阅下都有啥台？"""
    sub = session.get(Subscription, sub_id)
    if not sub:
        raise HTTPException(status_code=404, detail="订阅不存在")
    channels = session.exec(select(Channel).where(Channel.subscription_id == sub_id)).all()
    return channels

def subscription_fetch_key(sub: Subscription) -> str:
    """订阅抓取归一化键：同源（URL + UA + Headers）只请求一次。"""
    url = (sub.url or "").strip()
    ua = sub.user_agent or ""
    headers = (sub.headers or "{}").strip()
    return md5(f"{url}\0{ua}\0{headers}".encode()).hexdigest()


def _channel_states_for_subscription(session: Session, sub_id: int) -> Dict[str, dict]:
    """读取订阅下频道状态，刷新后按流 URL 恢复。"""
    old_channels = session.exec(select(Channel).where(Channel.subscription_id == sub_id)).all()
    return {
        c.url: {
            "is_enabled": c.is_enabled,
            "check_status": c.check_status,
            "check_date": c.check_date,
            "check_image": c.check_image,
        }
        for c in old_channels
    }


async def _apply_channels_to_subscription(
    session: Session,
    sub: Subscription,
    channels_data: list,
) -> int:
    """将已抓取的频道列表写入订阅（保留启用/检测状态）。"""
    channel_states = _channel_states_for_subscription(session, sub.id)
    old_channels = session.exec(select(Channel).where(Channel.subscription_id == sub.id)).all()
    for c in old_channels:
        session.delete(c)

    for item in channels_data:
        url = item.get("url")
        state = channel_states.get(url, {})
        channel = Channel(
            **item,
            subscription_id=sub.id,
            is_enabled=state.get("is_enabled", True),
            check_status=state.get("check_status"),
            check_date=state.get("check_date"),
            check_image=state.get("check_image"),
        )
        session.add(channel)

    sub.last_updated = datetime.utcnow()
    sub.last_update_status = "Success"
    session.add(sub)
    session.commit()
    return len(channels_data)


async def process_subscription_refresh(
    session: Session,
    sub: Subscription,
    *,
    invalidate_outputs: bool = True,
) -> int:
    """同步订阅（支持 M3U/TXT/Git 混合及多地址）。"""
    channels_data, _metadata = await IPTVFetcher.fetch_subscription(sub.url, sub.user_agent, sub.headers)
    count = await _apply_channels_to_subscription(session, sub, channels_data)
    if invalidate_outputs:
        invalidate_all_output_runtime_caches(session)
    return count


async def refresh_subscriptions_deduped(
    session: Session,
    subs: List[Subscription],
    *,
    invalidate_outputs: bool = True,
    on_group_done: Optional[
        Callable[[int, int, Subscription, List[Subscription]], Awaitable[None]]
    ] = None,
) -> Dict[str, Any]:
    """按抓取键去重刷新多个订阅；同源只下载一次，结果写入该组全部订阅。"""
    groups: Dict[str, List[Subscription]] = {}
    ordered_keys: List[str] = []
    for sub in subs:
        if not sub:
            continue
        key = subscription_fetch_key(sub)
        if key not in groups:
            groups[key] = []
            ordered_keys.append(key)
        groups[key].append(sub)

    failures = 0
    synced = 0
    for idx, key in enumerate(ordered_keys):
        group = groups[key]
        lead = group[0]
        try:
            channels_data, _metadata = await IPTVFetcher.fetch_subscription(
                lead.url, lead.user_agent, lead.headers
            )
            for sub in group:
                await _apply_channels_to_subscription(session, sub, channels_data)
                synced += 1
            if on_group_done:
                await on_group_done(idx + 1, len(ordered_keys), lead, group)
        except Exception as e:
            failures += len(group)
            print(f"[refresh_subscriptions_deduped] 抓取失败 {lead.name or lead.url}: {e}")

    if invalidate_outputs:
        invalidate_all_output_runtime_caches(session)
    return {"source_count": len(ordered_keys), "synced": synced, "failures": failures}

@router.post("/{sub_id}/refresh")
async def refresh_subscription(sub_id: int, session: Session = Depends(get_session)):
    """手动刷新订阅 (后台异步)"""
    sub = session.get(Subscription, sub_id)
    if not sub:
        raise HTTPException(status_code=404, detail="订阅不存在")
    
    # 创建异步任务记录
    task_id = str(uuid.uuid4())
    task_record = TaskRecord(
        id=task_id,
        name=f"同步订阅: {sub.name}",
        status="pending",
        progress=0,
        message="任务排队中..."
    )
    session.add(task_record)
    session.commit()

    # 派发异步任务
    print(f"[Action] 手动触发订阅刷新: {sub.name} (ID: {sub.id})")
    await fetch_subscription_task.kiq(
        task_id=task_id,
        sub_id=sub.id,
        url_str=sub.url or "",
        ua=sub.user_agent or "AptvPlayer/1.4.1",
        headers_json=sub.headers or "{}"
    )
    
    return {"status": "success", "task_id": task_id, "message": "已启动后台同步任务"}
