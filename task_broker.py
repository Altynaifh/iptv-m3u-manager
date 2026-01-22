import asyncio
import json
import sys
import io
from typing import Dict, List, Optional
from fastapi import WebSocket
import taskiq
from taskiq import InMemoryBroker, TaskiqEvents, TaskiqState
from sqlmodel import Session, select
from database import engine
from models import TaskRecord
from datetime import datetime, timezone, timedelta

# 定义中国标准时区 (UTC+8)
CST = timezone(timedelta(hours=8))

# 使用 InMemoryBroker 保持单进程轻量化
broker = InMemoryBroker()

class TaskNotifier:
    """WebSocket 任务进度推送中心"""
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        """向所有客户端广播任务更新"""
        dead_connections = []
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except:
                dead_connections.append(connection)
        
        for dead in dead_connections:
            self.disconnect(dead)

class ConsoleLogStream(io.TextIOBase):
    """自定义日志流，用于捕获 stdout 并通过 WebSocket 广播"""
    def __init__(self, original_stream, notifier_ref):
        self.original_stream = original_stream
        self.notifier = notifier_ref

    def write(self, s):
        # 首先写回原始流（终端可见）
        self.original_stream.write(s)
        self.original_stream.flush() # 强制刷新确保即时性
        
        if s and s.strip():
            # 仅向有连接的客户端异步发送日志
            if self.notifier.active_connections:
                # 注意：由于此方法常在同步上下文调用，使用 loop.create_task 推送
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        msg = {
                            "type": "console_log",
                            "line": s.strip(),
                            "timestamp": datetime.now(CST).strftime("%H:%M:%S")
                        }
                        loop.create_task(self.notifier.broadcast(msg))
                except:
                    pass
        return len(s)

    def flush(self):
        self.original_stream.flush()

notifier = TaskNotifier()

# 重定向 stdout 和 stderr
sys.stdout = ConsoleLogStream(sys.stdout, notifier)
sys.stderr = ConsoleLogStream(sys.stderr, notifier)

async def update_task_status(task_id: str, status: Optional[str] = None, progress: Optional[int] = None, message: Optional[str] = None, result: Optional[dict] = None):
    """更新数据库中的任务状态并向前端广播"""
    
    def _update_db():
        with Session(engine) as session:
            task = session.get(TaskRecord, task_id)
            if not task:
                # print(f"[DB] DEBUG: Task {task_id} not found in DB yet.") # 频繁打印会刷屏，仅在失败时打印
                return None
            
            # 终端状态保护：如果已经是中止或失败，严禁跳回运行或成功状态
            if task.status in ["canceled", "failure"]:
                if status not in ["canceled", "failure"] and status is not None:
                    # print(f"[DB] 拒绝状态回弹: {task_id} {task.status} -> {status}")
                    return None # 拦截该次更新，保持当前的终端状态
            
            if status: task.status = status
            if progress is not None: task.progress = progress
            if message: task.message = message
            if result: task.result = json.dumps(result)
            task.updated_at = datetime.utcnow()
            
            session.add(task)
            session.commit()
            session.refresh(task)
            return {
                "id": task.id,
                "name": task.name,
                "status": task.status,
                "progress": task.progress,
                "message": task.message,
                "updated_at": task.updated_at.isoformat()
            }

    async def _do_update():
        # 内部重试逻辑：由于异步任务可能在主线程 commit 之前就开始 update，这里需要多等等
        for i in range(5): 
            data = await asyncio.to_thread(_update_db)
            if data: return data
            await asyncio.sleep(0.5) 
        return None

    try:
        data = await _do_update()
        
        if not data:
            print(f"[WS] DEBUG: 无法更新任务进度 (ID: {task_id})")
            return

        # 广播给前端
        payload = {
            "type": "task_update",
            "data": data
        }
        await notifier.broadcast(payload)
    except Exception as e:
        print(f"[WS] DEBUG: update_task_status 广播出错: {e}")

@broker.on_event(TaskiqEvents.WORKER_STARTUP)
async def startup(state: TaskiqState):
    print("Taskiq Worker 已启动")

@broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def shutdown(state: TaskiqState):
    print("Taskiq Worker 已关闭")
