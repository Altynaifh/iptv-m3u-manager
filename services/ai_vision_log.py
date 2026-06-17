"""AI 视觉检测追踪日志：终端 + 落盘，便于排查 latin-1 等编码问题。"""
from __future__ import annotations

import os
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_TRACE_LOG: Path | None = None


def vision_trace_log_path() -> Path:
    global _TRACE_LOG
    if _TRACE_LOG is not None:
        return _TRACE_LOG
    root = "/data/logs" if os.path.isdir("/data") else "./data/logs"
    _TRACE_LOG = Path(root) / "ai_vision_trace.log"
    return _TRACE_LOG


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mask_secret(value: str, *, keep: int = 4) -> str:
    """脱敏 API Key 等敏感字段。"""
    text = (value or "").strip()
    if not text:
        return "(空)"
    if len(text) <= keep + 2:
        return "***"
    return f"{text[:keep]}***{text[-2:]}"


def latin1_audit(label: str, value: str) -> dict[str, Any]:
    """检查字符串能否按 HTTP 头要求的 latin-1 编码。"""
    text = value or ""
    try:
        text.encode("latin-1")
        return {"label": label, "ok": True, "len": len(text)}
    except UnicodeEncodeError as e:
        snippet = text[e.start : e.end] if isinstance(e.object, str) else ""
        return {
            "label": label,
            "ok": False,
            "len": len(text),
            "encoding": e.encoding,
            "start": e.start,
            "end": e.end,
            "snippet": snippet,
            "preview": text[max(0, e.start - 8) : e.end + 8],
        }


def vision_log(message: str, *, also_print: bool = True) -> None:
    """写入 AI 视觉追踪日志；默认同步打印到终端（可被任务中心 WebSocket 捕获）。"""
    line = f"[AI视觉] {message}"
    if also_print:
        print(line)
    try:
        path = vision_trace_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(f"[{_ts()}] {message}\n")
    except OSError as e:
        print(f"[AI视觉] 无法写入追踪日志: {e}")


def vision_log_exc(context: str, exc: BaseException) -> None:
    """记录异常详情；对 UnicodeEncodeError 额外输出编码位置与片段。"""
    vision_log(f"{context} | 异常 {type(exc).__name__}: {exc}")
    if isinstance(exc, UnicodeEncodeError):
        obj = exc.object if isinstance(exc.object, str) else ""
        snippet = obj[exc.start : exc.end] if obj else ""
        vision_log(
            "UnicodeEncodeError 详情 "
            f"encoding={exc.encoding!r} pos={exc.start}-{exc.end} "
            f"snippet={snippet!r} preview={obj[max(0, exc.start - 12):exc.end + 12]!r}"
        )
    tb = traceback.format_exc()
    if tb and tb.strip() != "NoneType: None":
        try:
            path = vision_trace_log_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(f"[{_ts()}] traceback ({context}):\n{tb}\n")
        except OSError:
            pass


async def vision_push(line: str) -> None:
    """推送一行到任务中心控制台（有 WebSocket 连接时）。"""
    from task_broker import push_console_log

    await push_console_log(f"[AI视觉] {line}")