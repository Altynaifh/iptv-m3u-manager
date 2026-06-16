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
async def check_channels_task(
    task_id: str,
    channel_ids: List[int],
    source: str = 'manual',
    auto_disable: bool = True,
    output_id: Optional[int] = None,
):
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
                
            batch_stats = await StreamChecker.run_batch_check(
                session,
                channels,
                concurrency=5,
                source=source,
                task_id=task_id,
                auto_disable=auto_disable,
                output_id=output_id,
            )
            if batch_stats is False:
                return
            summary = batch_stats if isinstance(batch_stats, dict) else {}
            ok_n = int(summary.get("success", 0))
            total_n = int(summary.get("total", len(channels)))
            fail_n = int(summary.get("failed", max(0, total_n - ok_n)))
            msg = f"检测完成：成功截图 {ok_n}/{total_n}"
            if fail_n:
                msg += f"，失败 {fail_n}"
            await update_task_status(
                task_id,
                status="success",
                progress=100,
                message=msg,
                result=json.dumps(summary, ensure_ascii=False),
            )
            return

        await update_task_status(task_id, status="success", progress=100, message="检测任务已完成")
    except Exception as e:
        print(f"[Task] 深度检测异常中断 (ID: {task_id}): {e}")
        import traceback
        traceback.print_exc()
        await update_task_status(task_id, status="failure", message=f"任务执行出错: {str(e)}")

class StreamChecker:
    _ffmpeg_path = None
    _CAPTURE_VF_FALLBACKS = ("scale=480:-2", "scale=320:-2", None)

    @classmethod
    def _candidate_ffmpeg_paths(cls):
        """按优先级收集可用的 FFmpeg 可执行文件路径。"""
        candidates = []

        env_ffmpeg = os.environ.get("FFMPEG_PATH") or os.environ.get("FFMPEG_BINARY")
        if env_ffmpeg:
            candidates.append(env_ffmpeg)

        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        for rel_path in (
            os.path.join(project_root, "bin", "ffmpeg.exe"),
            os.path.join(project_root, "bin", "ffmpeg"),
        ):
            candidates.append(rel_path)

        sys_ffmpeg = shutil.which("ffmpeg")
        if sys_ffmpeg:
            candidates.append(sys_ffmpeg)

        try:
            static_ffmpeg = run.get_or_fetch_platform_executables_else_raise()[0]
            candidates.append(static_ffmpeg)
        except Exception as e:
            print(f"DEBUG: 获取 static-ffmpeg 二进制失败: {e}")

        candidates.append("ffmpeg")

        seen = set()
        ordered = []
        for candidate in candidates:
            normalized = os.path.abspath(candidate) if os.path.sep in candidate else candidate
            if normalized in seen:
                continue
            seen.add(normalized)
            ordered.append(candidate)
        return ordered

    @classmethod
    def get_ffmpeg_path(cls):
        """获取并验证 FFmpeg 路径。"""
        if cls._ffmpeg_path:
            return cls._ffmpeg_path

        for ffmpeg_path in cls._candidate_ffmpeg_paths():
            if os.path.sep in ffmpeg_path and not os.path.isfile(ffmpeg_path):
                continue
            try:
                subprocess.run([ffmpeg_path, "-version"], capture_output=True, timeout=2)
                cls._ffmpeg_path = ffmpeg_path
                print(f"DEBUG: 使用 FFmpeg: {ffmpeg_path}")
                return cls._ffmpeg_path
            except Exception as e:
                print(f"DEBUG: FFmpeg ({ffmpeg_path}) 运行失败: {e}")

        cls._ffmpeg_path = "ffmpeg"
        print(f"DEBUG: 未找到有效 FFmpeg，兜底使用命令: {cls._ffmpeg_path}")
        return cls._ffmpeg_path

    @classmethod
    def _build_capture_cmd(cls, ffmpeg_exe: str, url: str, output_path: str, vf_filter: Optional[str]) -> List[str]:
        """构造截图命令，优先在输入前限制探测时长。"""
        cmd = [
            ffmpeg_exe,
            "-y",
            "-hide_banner",
            "-loglevel", "error",
            "-timeout", "10000000",
            "-t", "8",
            "-user_agent", "AptvPlayer/1.4.1",
            "-i", url,
            "-an", "-sn",
            "-frames:v", "1",
        ]
        if vf_filter:
            cmd.extend(["-vf", vf_filter])
        cmd.extend(["-f", "image2", "-c:v", "mjpeg", output_path])
        return cmd

    @classmethod
    def _normalize_ffmpeg_error(cls, returncode: int, stderr: bytes) -> str:
        err_msg = stderr.decode("utf-8", errors="ignore") if stderr else "FFmpeg produced no image."
        if returncode == -11 or returncode == 139:
            return f"FFmpeg 进程崩溃 (SIGSEGV, RC={returncode})。LXC 容器建议安装系统官方软件包。"
        return err_msg

    @classmethod
    def _should_retry_with_fallback(cls, returncode: int, stderr: bytes) -> bool:
        if returncode in (-11, 139):
            return True
        err_msg = stderr.decode("utf-8", errors="ignore").lower() if stderr else ""
        retry_markers = (
            "scale",
            "vf",
            "width not divisible",
            "height not divisible",
            "invalid argument",
            "error reinitializing filters",
            "error while processing the decoded frame",
        )
        return any(marker in err_msg for marker in retry_markers)

    @classmethod
    async def check_stream_visual(cls, url: str) -> dict:
        ffmpeg_exe = cls.get_ffmpeg_path()
        temp_filename = os.path.join(tempfile.gettempdir(), f"capture_{uuid.uuid4()}.jpg")
        last_error = "FFmpeg produced no image."

        try:
            for vf_filter in cls._CAPTURE_VF_FALLBACKS:
                if os.path.exists(temp_filename):
                    try:
                        os.remove(temp_filename)
                    except OSError:
                        pass

                cmd = cls._build_capture_cmd(ffmpeg_exe, url, temp_filename, vf_filter)
                vf_label = vf_filter or "no-scale"
                print(f"DEBUG: 执行截图命令[{vf_label}]: {' '.join(cmd)}")

                try:
                    result = await asyncio.to_thread(
                        subprocess.run,
                        cmd,
                        capture_output=True,
                        timeout=20,
                        env=os.environ.copy(),
                    )
                except subprocess.TimeoutExpired:
                    print(f"DEBUG: [{url}] 截图超时 (vf={vf_label})")
                    last_error = "Detection Timeout"
                    continue

                if result.returncode == 0 and os.path.exists(temp_filename) and os.path.getsize(temp_filename) > 0:
                    with open(temp_filename, "rb") as f:
                        img_data = f.read()
                    b64 = base64.b64encode(img_data).decode("utf-8")
                    return {"url": url, "status": True, "image": f"data:image/jpeg;base64,{b64}"}

                last_error = cls._normalize_ffmpeg_error(result.returncode, result.stderr)
                print(f"DEBUG: [{url}] 截图失败 (vf={vf_label}, RC={result.returncode}): {last_error[:200]}")

                if not cls._should_retry_with_fallback(result.returncode, result.stderr):
                    break

            return {"url": url, "status": False, "error": last_error[:100]}
        except Exception as e:
            print(f"DEBUG: 运行异常: {e}")
            return {"url": url, "status": False, "error": str(e)}
        finally:
            if os.path.exists(temp_filename):
                try:
                    os.remove(temp_filename)
                except OSError:
                    pass

    @classmethod
    async def run_batch_check(
        cls,
        session: Session,
        channels,
        concurrency: int = 5,
        source: str = 'manual',
        task_id: Optional[str] = None,
        auto_disable: bool = True,
        output_id: Optional[int] = None,
    ):
        """
        [重构版] 分批执行多个频道的深度检测
        """
        if not channels:
            return {"total": 0, "success": 0, "failed": 0}

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

        async def _persist_channel_result(res: dict) -> None:
            if not res or not res.get("ch_id") or res.get("status") == "canceled":
                return
            from database import engine
            from models import OutputSource
            from services.output_resolver import aggregate_channels
            from services.realtime_push import (
                broadcast_channel_patch,
                broadcast_preview_stats,
                channel_patch_fields,
            )

            with Session(engine) as update_session:
                ch = update_session.get(cls._get_channel_model(), res["ch_id"])
                if not ch:
                    return
                ch.check_status = res["status"]
                ch.check_date = datetime.utcnow()
                ch.check_image = res.get("image")
                ch.check_error = res.get("error") if not res["status"] else None
                ch.check_source = source
                if auto_disable:
                    ch.is_enabled = res["status"]
                update_session.add(ch)
                update_session.commit()
                update_session.refresh(ch)
                if not output_id:
                    return
                await broadcast_channel_patch(output_id, [channel_patch_fields(ch)])
                out = update_session.get(OutputSource, output_id)
                if out:
                    members = aggregate_channels(update_session, out, None)
                    enabled_n = sum(1 for c in members if c.is_enabled)
                    await broadcast_preview_stats(output_id, len(members), enabled_n)

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

                    payload = {**res, "ch_id": ch.id}
                    await _persist_channel_result(payload)
                    return payload
            except Exception as e:
                print(f"[Check] 异常: {ch.name} -> {e}")
                payload = {"status": False, "error": str(e), "ch_id": ch.id}
                await _persist_channel_result(payload)
                return payload

        # 使用 asyncio.gather 但受控于信号量，并加入对取消信号的全局响应
        tasks = [_worker(i, ch) for i, ch in enumerate(unique_channels)]
        results = await asyncio.gather(*tasks)

        # 如果已经触发了局部熔断，直接返回 False 告知上层
        if local_aborted:
            return False

        valid_results = [
            res for res in results
            if res and res.get("ch_id") and res.get("status") != "canceled"
        ]
        success_count = sum(1 for res in valid_results if res.get("status") is True)
        failed_count = len(valid_results) - success_count

        if output_id:
            from database import engine
            from services.realtime_push import rebuild_manual_status_from_db

            with Session(engine) as refresh_session:
                status = rebuild_manual_status_from_db(refresh_session, output_id)
                await refresh_output_and_broadcast(refresh_session, output_id, status_text=status)

        return {"total": total, "success": success_count, "failed": failed_count}
            
        # 清理进度标记属性，防止内存泄漏或属性过多
        if hasattr(cls, f"_last_p_{task_id}"):
            delattr(cls, f"_last_p_{task_id}")

    @staticmethod
    def _get_channel_model():
        # 避免循环导入
        from models import Channel
        return Channel
