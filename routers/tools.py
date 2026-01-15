from fastapi import APIRouter
import asyncio
import aiohttp
import os
import signal
from sqlmodel import SQLModel

from services.connectivity import check_url
from services.epg import EPGManager, fetch_epg_cached, md5

router = APIRouter(tags=["tools"])

from datetime import datetime
from sqlmodel import Session, select
from database import get_session
from fastapi import Depends
from models import Channel

class CheckRequest(SQLModel):
    """检测请求数据"""
    urls: list[str] = []
    items: list[dict] = [] # 包含 {id: int, url: str} 的列表
    auto_disable: bool = False

# 并发控制锁（防 FFmpeg 跑太猛爆 CPU）
visual_check_semaphore = asyncio.Semaphore(4)
from services.stream_checker import StreamChecker, check_channels_task
import uuid
from models import TaskRecord

@router.get("/api/epg/current")
async def get_epg_current(epg_url: str, tvg_id: str = None, tvg_name: str = None, current_logo: str = None, refresh: bool = False):
    """看现在播啥节目"""
    prog_data = await EPGManager.get_program(epg_url, tvg_id, tvg_name, current_logo, refresh=refresh)
    return {"program": prog_data.get("title", ""), "logo": prog_data.get("logo")}

@router.post("/check-connectivity")
async def check_connectivity(req: CheckRequest):
    """快速连通性检测（通不通）"""
    print(f"[Action] 触发快速连通性检测: {len(req.urls or req.items)} 个目标")
    async with aiohttp.ClientSession() as session:
        target_urls = req.urls if req.urls else [i['url'] for i in req.items]
        tasks = [check_url(u, session) for u in target_urls]
        results = await asyncio.gather(*tasks)
        return results

@router.post("/check-stream-visual")
async def check_stream_visual(req: CheckRequest, session: Session = Depends(get_session)):
    """深度检测 (异步后台任务)"""
    channel_ids = [item.get('id') for item in req.items if item.get('id')]
    if not channel_ids:
        return {"status": "error", "message": "没有选中的频道"}
    
    print(f"[Action] 触发深度检测任务: {len(channel_ids)} 个频道")
    # 创建异步任务记录
    task_id = str(uuid.uuid4())
    task_record = TaskRecord(
        id=task_id,
        name=f"批量深度检测: {len(channel_ids)} 个频道",
        status="pending",
        progress=0,
        message="任务排队中..."
    )
    session.add(task_record)
    session.commit()

    # 立即触发一次广播，让前端任务中心即时看到
    from task_broker import update_task_status
    asyncio.create_task(update_task_status(task_id, status="pending", progress=0, message="任务已接收"))

    # 派发异步任务
    await check_channels_task.kiq(
        task_id=task_id,
        channel_ids=channel_ids,
        source='manual'
    )
    
    return {"status": "success", "task_id": task_id, "message": "已在后台启动深度检测任务"}

@router.post("/api/system/restart")
async def restart_service():
    """重启服务接口（兼容 Windows，通过触碰文件触发 uvicorn 重载）"""
    print("[System] 收到重启服务请求，正在通过触碰 main.py 触发重载...")
    
    async def _do_restart():
        try:
            await asyncio.sleep(0.5)
            from pathlib import Path
            # 这里定位 main.py 路径，通常在当前工作目录
            main_file = Path("main.py")
            if main_file.exists():
                # 触碰文件，改变 mtime，uvicorn --reload 会监测到并重启
                main_file.touch()
                print("  └─ ✅ 已触碰 main.py，等待 uvicorn 重启...")
            else:
                # 如果找不到，尝试通过更激进的方式（如信号，作为兜底）
                print("  └─ ⚠️ 未找到 main.py，尝试使用信号兜底...")
                os.kill(os.getpid(), signal.SIGTERM)
        except Exception as e:
            print(f"  └─ ❌ 重启触发失败: {e}")

    asyncio.create_task(_do_restart())
    return {"status": "success", "message": "重启指令已发出，系统正在通过文件重载机制进行重启。请 5 秒后刷新页面。"}
