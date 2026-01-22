import asyncio
from typing import Optional, List
import base64
import os
import subprocess
import shutil
import uuid
import tempfile
import json
from datetime import datetime, timedelta
from sqlmodel import Session, select
from static_ffmpeg import run
from task_broker import broker, update_task_status, notifier
from models import TaskRecord

@broker.task
async def check_channels_task(task_id: str, channel_ids: List[int], source: str = 'manual'):
    try:
        await update_task_status(task_id, status="running", progress=0, message=f"准备检测 {len(channel_ids)} 个路径...")
        print(f"[Task] 收到深度检测请求: {len(channel_ids)} 个频道 (来源: {source})")
        
        from database import engine
        from models import Channel
        
        with Session(engine) as session:
            # 优化查询：一次性取出所有频道，减少数据库 IO
            statement = select(Channel).where(Channel.id.in_(channel_ids))
            channels = session.exec(statement).all()
            
            if not channels:
                print(f"[Task] 失败: 未找到有效频道")
                await update_task_status(task_id, status="success", progress=100, message="没有有效的频道需要检测")
                return
                
            if await StreamChecker.run_batch_check(session, channels, concurrency=5, source=source, task_id=task_id) is False:
                # 如果是因为中止而退出的，不发送最后的 success 广播
                return
            
        await update_task_status(task_id, status="success", progress=100, message="检测任务已完成")
    except Exception as e:
        print(f"[Task] 深度检测异常中断 (ID: {task_id}): {e}")
        import traceback
        traceback.print_exc()
        await update_task_status(task_id, status="failure", message=f"任务执行出错: {str(e)}")

class StreamChecker:
    _ffmpeg_path = None

    @classmethod
    def get_ffmpeg_path(cls):
        """获取并验证 FFmpeg 路径"""
        if cls._ffmpeg_path:
            return cls._ffmpeg_path

        # 1. 优先尝试系统路径中的 ffmpeg
        sys_ffmpeg = shutil.which("ffmpeg")
        if sys_ffmpeg:
            try:
                # 简单验证是否能跑
                subprocess.run([sys_ffmpeg, "-version"], capture_output=True, timeout=2)
                cls._ffmpeg_path = sys_ffmpeg
                print(f"DEBUG: 使用系统 FFmpeg: {sys_ffmpeg}")
                return cls._ffmpeg_path
            except Exception as e:
                print(f"DEBUG: 系统 FFmpeg ({sys_ffmpeg}) 运行失败: {e}")

        # 2. 尝试 static-ffmpeg 下载的二进制
        try:
            static_ffmpeg = run.get_or_fetch_platform_executables_else_raise()[0]
            try:
                subprocess.run([static_ffmpeg, "-version"], capture_output=True, timeout=2)
                cls._ffmpeg_path = static_ffmpeg
                print(f"DEBUG: 使用 static-ffmpeg 二进制: {static_ffmpeg}")
                return cls._ffmpeg_path
            except Exception as e:
                print(f"DEBUG: static-ffmpeg 二进制 ({static_ffmpeg}) 运行失败: {e}")
        except Exception as e:
            print(f"DEBUG: 获取 static-ffmpeg 二进制失败: {e}")

        # 最后兜底
        cls._ffmpeg_path = "ffmpeg"
        print(f"DEBUG: 未找到有效 FFmpeg，兜底使用命令: {cls._ffmpeg_path}")
        return cls._ffmpeg_path

    @classmethod
    async def check_stream_visual(cls, url: str) -> dict:
        ffmpeg_exe = cls.get_ffmpeg_path()
        temp_filename = os.path.join(tempfile.gettempdir(), f"capture_{uuid.uuid4()}.jpg")
        
        # 使用 -user_agent 参数代替 -headers，并在 -i 前增加 -t 限制探测时长
        cmd = [
            ffmpeg_exe,
            "-y",
            "-hide_banner",
            "-loglevel", "error",
            "-t", "5",          # 输入探测阶段限时 5 秒
            "-user_agent", "AptvPlayer/1.4.1",
            "-i", url,
            "-an", "-sn",       # 禁用音频和字幕
            "-frames:v", "1",
            "-vf", "scale=320:-1",
            "-f", "image2",
            "-c:v", "mjpeg",
            temp_filename 
        ]

        print(f"DEBUG: 执行截图命令: {' '.join(cmd)}")

        try:
            def run_ffmpeg():
                # env 使用 os.environ.copy() 确保在 LXC 环境下的变量继承
                return subprocess.run(
                    cmd, 
                    capture_output=True, 
                    timeout=15,
                    env=os.environ.copy()
                )

            result = await asyncio.to_thread(run_ffmpeg)
            
            if result.returncode == 0 and os.path.exists(temp_filename) and os.path.getsize(temp_filename) > 0:
                with open(temp_filename, "rb") as f:
                    img_data = f.read()
                
                b64 = base64.b64encode(img_data).decode('utf-8')
                return {"url": url, "status": True, "image": f"data:image/jpeg;base64,{b64}"}
            else:
                err_msg = result.stderr.decode('utf-8', errors='ignore') if result.stderr else "FFmpeg produced no image."
                
                if result.returncode == -11 or result.returncode == 139:
                    err_msg = f"FFmpeg 进程崩溃 (SIGSEGV, RC={result.returncode})。LXC 容器建议安装系统官方软件包。"
                
                print(f"DEBUG: [{url}] 检测失败 (RC={result.returncode}): {err_msg[:200]}")
                return {"url": url, "status": False, "error": err_msg[:100]}

        except subprocess.TimeoutExpired:
            print(f"DEBUG: [{url}] 检测超时")
            return {"url": url, "status": False, "error": "Detection Timeout"}
        except Exception as e:
            print(f"DEBUG: 运行异常: {e}")
            return {"url": url, "status": False, "error": str(e)}
        finally:
            if os.path.exists(temp_filename):
                try:
                    os.remove(temp_filename)
                except:
                    pass

    @classmethod
    async def run_batch_check(cls, session: Session, channels, concurrency: int = 5, source: str = 'manual', task_id: Optional[str] = None):
        """
        [重构版] 分批执行多个频道的深度检测
        """
        if not channels:
            return

        # 对频道按 URL 去重
        unique_channels = []
        seen_urls = set()
        for ch in channels:
            if ch.url not in seen_urls:
                unique_channels.append(ch)
                seen_urls.add(ch.url)
        
        total = len(unique_channels)
        sem = asyncio.Semaphore(concurrency)
        finished_count = 0
        last_reported_p = -1
        
        # 结果容器
        results = []

        local_aborted = False

        async def _worker(i, ch):
            nonlocal finished_count, last_reported_p, local_aborted
            
            # 1. 锁前检查：如果已熔断，直接秒速退出，不再排队
            if local_aborted:
                return {"status": "canceled", "ch_id": ch.id}

            try:
                async with sem:
                    # 2. 锁内检查：进入执行状态后的高频核实
                    # 如果已经有其他协程触发了局部熔断，直接退出
                    if local_aborted:
                        return {"status": "canceled", "ch_id": ch.id}

                    # 每 2 个任务同步一次数据库状态（作为局部熔断的来源）
                    if i % 2 == 0:
                        from database import engine
                        with Session(engine) as check_session:
                            task = check_session.get(TaskRecord, task_id)
                            if not task or task.status == "canceled":
                                print(f"[Check] 任务 {task_id} 已中止，触发全局熔断")
                                local_aborted = True # 标记局部熔断，让所有排队和运行中的协程看到
                                await update_task_status(task_id, status="canceled", message="检测作业已由用户中止")
                                return {"status": "canceled", "ch_id": ch.id}

                    print(f"[Check] 正在检测 ({i+1}/{total}): {ch.name[:20]}")
                    res = await cls.check_stream_visual(ch.url)
                    
                    # 完成一个，计数加一
                    finished_count += 1
                    # 重新计算进度：60% -> 98%
                    progress_val = 60 + int((finished_count / total) * 38)
                    
                    # 仅在进度增加且显著时上报
                    if not local_aborted and progress_val > last_reported_p and (progress_val - last_reported_p >= 2 or finished_count == total):
                        last_reported_p = progress_val
                        await update_task_status(task_id, progress=progress_val, message=f"正在检测 ({finished_count}/{total}): {ch.name}")
                    
                    if res['status']:
                        print(f"  └─ ✅ 成功")
                    else:
                        print(f"  └─ ❌ 失败: {res.get('error', 'Unknown')}")
                        
                    return {**res, "ch_id": ch.id}
            except Exception as e:
                print(f"[Check] 异常: {ch.name} -> {e}")
                return {"status": False, "error": str(e), "ch_id": ch.id}

        # 使用 asyncio.gather 但受控于信号量，并加入对取消信号的全局响应
        tasks = [_worker(i, ch) for i, ch in enumerate(unique_channels)]
        results = await asyncio.gather(*tasks)

        # 如果已经触发了局部熔断，直接返回 False 告知上层
        if local_aborted:
            return False

        # 批量写回数据库（过滤掉已取消的虚拟结果）
        from database import engine
        with Session(engine) as update_session:
            for res in results:
                if res and res.get('ch_id') and res.get('status') != "canceled":
                    ch = update_session.get(cls._get_channel_model(), res['ch_id'])
                    if ch:
                        ch.check_status = res['status']
                        ch.check_date = datetime.utcnow()
                        ch.check_image = res.get('image')
                        ch.check_error = res.get('error') if not res['status'] else None
                        ch.check_source = source
                        ch.is_enabled = res['status']
                        update_session.add(ch)
            update_session.commit()
        return True
            
        # 清理进度标记属性，防止内存泄漏或属性过多
        if hasattr(cls, f"_last_p_{task_id}"):
            delattr(cls, f"_last_p_{task_id}")

    @staticmethod
    def _get_channel_model():
        # 避免循环导入
        from models import Channel
        return Channel
