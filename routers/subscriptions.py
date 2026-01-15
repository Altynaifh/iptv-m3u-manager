from fastapi import APIRouter, HTTPException, Depends
from sqlmodel import Session, select
from typing import List
from models import Subscription, Channel, TaskRecord
from database import get_session, engine
from services.fetcher import IPTVFetcher, fetch_subscription_task
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

async def process_subscription_refresh(session: Session, sub: Subscription) -> int:
    """同步订阅（支持 M3U/TXT/Git 混合及多地址）"""
    # 1. 记住当前已有的状态（禁用状态、检测结果），防止刷新后丢失
    old_channels = session.exec(select(Channel).where(Channel.subscription_id == sub.id)).all()
    
    # 建立以 URL 为 Key 的状态映射表
    channel_states = {}
    for c in old_channels:
        channel_states[c.url] = {
            "is_enabled": c.is_enabled,
            "check_status": c.check_status,
            "check_date": c.check_date,
            "check_image": c.check_image
        }
    
    # 2. 清掉旧台
    for c in old_channels:
        session.delete(c)
    
    # 3. 抓取并解析
    channels_data, metadata = await IPTVFetcher.fetch_subscription(sub.url, sub.user_agent, sub.headers)
    
    for item in channels_data:
        # 尝试从映射表中恢复状态
        url = item.get("url")
        state = channel_states.get(url, {})
        
        is_enabled = state.get("is_enabled", True)
        
        channel = Channel(
            **item, 
            subscription_id=sub.id, 
            is_enabled=is_enabled,
            check_status=state.get("check_status"),
            check_date=state.get("check_date"),
            check_image=state.get("check_image")
        )
        session.add(channel)
    
    sub.last_updated = datetime.utcnow()
    sub.last_update_status = "Success"
    session.add(sub)
    session.commit()
    return len(channels_data)

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
