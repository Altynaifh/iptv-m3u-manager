from fastapi import APIRouter, HTTPException, Depends
from sqlmodel import Session, select
from models import Channel
from database import get_session
from services.output_stats import invalidate_outputs_for_channel

router = APIRouter(prefix="/channels", tags=["channels"])

@router.post("/{channel_id}/toggle", response_model=Channel)
def toggle_channel(channel_id: int, session: Session = Depends(get_session)):
    """开关频道"""
    channel = session.get(Channel, channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="频道不存在")
    
    # 状态取反
    channel.is_enabled = not channel.is_enabled
    session.add(channel)
    session.commit()
    session.refresh(channel)
    # 仅失效相关聚合源缓存并排队重建，避免全量失效引发预览读到过期 gzip
    invalidate_outputs_for_channel(session, channel, schedule_rebuild=True)
    return channel
