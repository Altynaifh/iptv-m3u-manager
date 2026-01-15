from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends
from sqlmodel import Session, select
from database import engine
from models import TaskRecord
from task_broker import notifier, broker
from typing import List

router = APIRouter(prefix="/api/tasks", tags=["Tasks"])

@router.get("/")
def list_tasks(limit: int = 50):
    """获取最近的任务列表"""
    with Session(engine) as session:
        statement = select(TaskRecord).order_by(TaskRecord.created_at.desc()).limit(limit)
        return session.exec(statement).all()

@router.post("/{task_id}/stop")
async def stop_task(task_id: str):
    """尝试中止进行中的任务"""
    print(f"[Action] 尝试中止任务: {task_id}")
    with Session(engine) as session:
        task = session.get(TaskRecord, task_id)
        if task and task.status in ["pending", "running"]:
            task.status = "canceled"
            task.message = "用户手动中止"
            session.add(task)
            session.commit()
            print(f"  └─ ✅ 已成功标记为中止状态")
            return {"status": "success", "message": "已发送中止信号"}
    print(f"  └─ ❌ 失败: 任务不存在或不可中止")
    return {"status": "error", "message": "任务不可取消或不存在"}


@router.delete("/cleanup")
def cleanup_tasks():
    """清理所有非活动任务 (除了正在运行和等待中的)"""
    with Session(engine) as session:
        # 定义需要清理的任务：非 running 且非 pending
        statement = select(TaskRecord).where(
            TaskRecord.status != "running",
            TaskRecord.status != "pending"
        )
        finished_tasks = session.exec(statement).all()
        count = len(finished_tasks)
        for task in finished_tasks:
            session.delete(task)
        session.commit()
        return {"status": "success", "count": count, "message": f"已清理 {count} 条历史记录"}

@router.websocket("/ws")
async def tasks_websocket(websocket: WebSocket):
    """全局任务进度推送通道"""
    await notifier.connect(websocket)
    print(f"[WS] 任务通道已建立新连接 (当前活跃: {len(notifier.active_connections)})")
    try:
        while True:
            # 保持连接，处理心跳或其他客户端消息
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        notifier.disconnect(websocket)
        print(f"[WS] 任务通道客户端已断开")
    except Exception as e:
        notifier.disconnect(websocket)
        print(f"[WS] 任务通道连接异常断开: {e}")
