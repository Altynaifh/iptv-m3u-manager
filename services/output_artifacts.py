"""聚合源静态产物：M3U 与预览 JSON 落盘，读时直出。"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import threading
from datetime import datetime
from typing import Any, Dict, Optional

from sqlmodel import Session, select

from database import engine
from models import OutputSource, Subscription
from services.generator import M3UGenerator
from services.output_resolver import export_m3u_channels, preview_export_groups
from services.preview_cache import compute_preview_cache_key


def artifacts_root() -> str:
    """产物根目录：Docker 用 /data/artifacts，本地用 ./data/artifacts。"""
    root = "/data/artifacts" if os.path.isdir("/data") else "./data/artifacts"
    for sub in ("exports", "previews"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    return root


def m3u_artifact_path(slug: str) -> str:
    return os.path.join(artifacts_root(), "exports", f"{slug}.m3u")


def preview_artifact_path(output_id: int) -> str:
    return os.path.join(artifacts_root(), "previews", f"{output_id}.json.gz")


def preview_meta_path(output_id: int) -> str:
    return os.path.join(artifacts_root(), "previews", f"{output_id}.meta.json")


def clear_output_artifacts(out: OutputSource) -> None:
    """删除该聚合源的全部磁盘产物。"""
    for path in (
        m3u_artifact_path(out.slug),
        preview_artifact_path(out.id),
        preview_meta_path(out.id),
    ):
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass


def _atomic_write_text(path: str, content: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    if os.path.isfile(path):
        os.remove(path)
    os.rename(tmp, path)


def _atomic_write_json_gz(path: str, payload: dict) -> None:
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, default=str)
    if os.path.isfile(path):
        os.remove(path)
    os.rename(tmp, path)


def _read_preview_file(output_id: int) -> Optional[dict]:
    path = preview_artifact_path(output_id)
    if not os.path.isfile(path):
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _read_artifact_meta(output_id: int) -> Optional[dict]:
    """读取预览产物元数据（含 cache_key）。"""
    path = preview_meta_path(output_id)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _artifact_bundle_complete(out: OutputSource) -> bool:
    """M3U、预览 gzip 与 meta 三者齐全才视为可直读。"""
    return (
        os.path.isfile(m3u_artifact_path(out.slug))
        and os.path.isfile(preview_artifact_path(out.id))
        and os.path.isfile(preview_meta_path(out.id))
    )


def is_artifact_cache_stale(session: Session, out: OutputSource) -> bool:
    """磁盘产物是否与当前聚合配置/成员状态不一致。"""
    if not _artifact_bundle_complete(out):
        return True
    meta = _read_artifact_meta(out.id)
    if not meta or not meta.get("cache_key"):
        return True
    current_key = compute_preview_cache_key(session, out, None)
    return meta.get("cache_key") != current_key


def _enrich_preview_epg(
    payload: dict,
    epg_url: Optional[str],
    *,
    out=None,
    members=None,
) -> dict:
    """生成预览节目表快照：先查 EPG，再按验证供体统一覆盖同频道 tvg-name/台标。"""
    from services.channel_logo_overlay import (
        apply_validated_cluster_overlays_to_preview,
        effective_tvg_name_dict,
    )
    from services.epg import EPGManager

    if epg_url and EPGManager.ensure_parsed_cache_sync(epg_url):
        for key in ("manual_groups", "ai_groups"):
            for sec in payload.get(key) or []:
                for ch in sec.get("channels") or []:
                    prog = EPGManager.lookup_program_sync(
                        epg_url,
                        "",
                        effective_tvg_name_dict(ch),
                        ch.get("logo"),
                    )
                    ch["epg_program"] = prog.get("title")
                    ch["epg_logo"] = prog.get("logo")
        payload["epg_snapshot_at"] = datetime.utcnow().isoformat()
        if out is not None and members:
            apply_validated_cluster_overlays_to_preview(payload, out, members, epg_url)
    return payload


def build_output_artifacts(session: Session, out: OutputSource) -> Dict[str, Any]:
    """同步生成 M3U 与预览 gzip 产物，并更新 DB 元数据（不再存大 JSON）。"""
    subs = session.exec(select(Subscription)).all()
    sub_map = {s.id: s.name or s.url for s in subs}

    filtered = export_m3u_channels(session, out)
    m3u_content = M3UGenerator.generate_m3u(
        filtered, sub_map, out.epg_url, out.include_source_suffix
    )
    _atomic_write_text(m3u_artifact_path(out.slug), m3u_content)

    from services.output_resolver import aggregate_channels

    payload = preview_export_groups(session, out, None)
    payload = _enrich_preview_epg(
        payload,
        out.epg_url,
        out=out,
        members=aggregate_channels(session, out, None),
    )
    _atomic_write_json_gz(preview_artifact_path(out.id), payload)

    cache_key = compute_preview_cache_key(session, out, None)
    meta = {
        "output_id": out.id,
        "slug": out.slug,
        "cache_key": cache_key,
        "built_at": datetime.utcnow().isoformat(),
        "epg_snapshot_at": payload.get("epg_snapshot_at"),
    }
    _atomic_write_text(preview_meta_path(out.id), json.dumps(meta, ensure_ascii=False))

    out.preview_cache_key = cache_key
    out.preview_cache_json = None
    out.preview_cache_at = datetime.utcnow()

    from services.output_stats import get_or_refresh_member_stats, load_enabled_subscription_channel_pool

    pool, enabled_sub_ids = load_enabled_subscription_channel_pool(session)
    get_or_refresh_member_stats(session, out, pool, enabled_sub_ids, force=True)
    session.add(out)
    session.commit()
    session.refresh(out)

    payload["cache"] = {
        "hit": False,
        "key": cache_key,
        "at": out.preview_cache_at.isoformat() if out.preview_cache_at else None,
        "source": "disk",
    }
    return payload


def get_or_build_m3u_file(session: Session, out: OutputSource, *, force: bool = False) -> str:
    """返回 M3U 磁盘路径；缺失或缓存陈旧时同步重建（与预览同一套筛选）。"""
    path = m3u_artifact_path(out.slug)
    if not force and os.path.isfile(path) and not is_artifact_cache_stale(session, out):
        return path
    if force:
        clear_output_artifacts(out)
    build_output_artifacts(session, out)
    return path


def get_or_build_preview_payload(
    session: Session,
    out: OutputSource,
    *,
    force: bool = False,
    epg_refresh: bool = False,
) -> Dict[str, Any]:
    """读取预览 gzip 产物；force/epg_refresh 时重建。"""
    if force or epg_refresh:
        clear_output_artifacts(out)

    if not force and not epg_refresh and not is_artifact_cache_stale(session, out):
        cached = _read_preview_file(out.id)
        if cached is not None:
            meta = _read_artifact_meta(out.id) or {}
            cached["cache"] = {
                "hit": True,
                "key": meta.get("cache_key"),
                "at": meta.get("built_at") or (out.preview_cache_at.isoformat() if out.preview_cache_at else None),
                "source": "disk",
            }
            return cached

    return build_output_artifacts(session, out)


def _build_artifacts_sync(output_id: int) -> Dict[str, Any]:
    """在线程池中执行：独立 Session，避免跨线程复用 ORM 会话。"""
    with Session(engine) as session:
        out = session.get(OutputSource, output_id)
        if not out:
            return {}
        return build_output_artifacts(session, out)


async def build_output_artifacts_async(
    output_id: int,
    *,
    epg_url: Optional[str] = None,
    epg_refresh: bool = False,
) -> Dict[str, Any]:
    """后台任务入口：可选先拉 EPG，再在线程池生成产物。"""
    if epg_refresh and epg_url:
        from services.epg import EPGManager, fetch_epg_cached

        await fetch_epg_cached(epg_url, refresh=True)
        EPGManager.ensure_parsed_cache_sync(epg_url, force_reload=True)

    return await asyncio.to_thread(_build_artifacts_sync, output_id)


_pending_rebuild_ids: set[int] = set()
_app_loop: Optional[asyncio.AbstractEventLoop] = None


def bind_artifacts_scheduler_loop(loop: asyncio.AbstractEventLoop) -> None:
    """绑定应用主事件循环，供同步上下文安全入队重建任务。"""
    global _app_loop
    _app_loop = loop


def _enqueue_rebuild_task(output_id: int, *, epg_refresh: bool) -> None:
    """将重建任务提交到 Taskiq（须在运行中的事件循环内 await）。"""
    async def _kick() -> None:
        try:
            await rebuild_output_artifacts_task.kiq(output_id=output_id, epg_refresh=epg_refresh)
        except Exception as e:
            print(f"[Artifacts] 入队重建失败 output={output_id}: {e}")
            _pending_rebuild_ids.discard(output_id)

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_kick())
        return
    except RuntimeError:
        pass

    loop = _app_loop
    if loop is not None and loop.is_running():
        asyncio.run_coroutine_threadsafe(_kick(), loop)
        return

    # 兜底：无可用事件循环时后台线程同步重建（避免 asyncio.run 取消已入队任务）
    def _fallback() -> None:
        try:
            if epg_refresh:
                with Session(engine) as session:
                    out = session.get(OutputSource, output_id)
                    if out and out.epg_url:
                        from services.epg import EPGManager

                        EPGManager.ensure_parsed_cache_sync(out.epg_url, force_reload=True)
            _build_artifacts_sync(output_id)
            print(f"[Artifacts] 已同步重建聚合 {output_id}")
        except Exception as e:
            print(f"[Artifacts] 同步重建失败 output={output_id}: {e}")
        finally:
            _pending_rebuild_ids.discard(output_id)

    threading.Thread(target=_fallback, name=f"artifacts-rebuild-{output_id}", daemon=True).start()


def schedule_rebuild_output_artifacts(output_id: int, *, epg_refresh: bool = False) -> None:
    """异步排队重建单个聚合产物（同一 ID 不重复入队）。"""
    if output_id in _pending_rebuild_ids:
        return
    _pending_rebuild_ids.add(output_id)
    _enqueue_rebuild_task(output_id, epg_refresh=epg_refresh)


def schedule_rebuild_all_output_artifacts(session: Session, *, epg_refresh: bool = False) -> None:
    for out in session.exec(select(OutputSource)).all():
        if out.id is not None:
            schedule_rebuild_output_artifacts(out.id, epg_refresh=epg_refresh)


from task_broker import broker


@broker.task
async def rebuild_output_artifacts_task(output_id: int, epg_refresh: bool = False):
    """Taskiq：后台重建聚合静态产物。"""
    slug = ""
    try:
        with Session(engine) as session:
            out = session.get(OutputSource, output_id)
            if not out:
                return
            slug = out.slug or ""
            epg_url = out.epg_url
        await build_output_artifacts_async(
            output_id,
            epg_url=epg_url,
            epg_refresh=epg_refresh,
        )
        print(f"[Artifacts] 已重建聚合 {output_id} ({slug})")
    except asyncio.CancelledError:
        print(f"[Artifacts] 重建被取消 output={output_id}")
        raise
    except Exception as e:
        print(f"[Artifacts] 重建异常 output={output_id}: {e}")
    finally:
        _pending_rebuild_ids.discard(output_id)