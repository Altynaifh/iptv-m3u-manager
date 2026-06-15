"""聚合源成员统计缓存：避免列表接口重复全量扫描频道。"""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from sqlmodel import Session, select

from models import Channel, OutputSource, Subscription
from services.output_resolver import aggregate_channels_from_pool
from services.preview_cache import clear_output_preview_cache


def load_enabled_subscription_channel_pool(session: Session) -> Tuple[List[Channel], Set[int]]:
    """一次加载所有启用订阅下的频道，供多聚合源复用。"""
    enabled_sub_ids = set(
        session.exec(select(Subscription.id).where(Subscription.is_enabled == True)).all()
    )
    if not enabled_sub_ids:
        return [], enabled_sub_ids
    channels = list(
        session.exec(
            select(Channel).where(Channel.subscription_id.in_(enabled_sub_ids))
        ).all()
    )
    return channels, enabled_sub_ids


def clear_output_member_stats(out: OutputSource) -> None:
    out.member_total = None
    out.member_enabled = None
    out.member_disabled = None


def invalidate_output_runtime_cache(out: OutputSource) -> None:
    clear_output_preview_cache(out)
    clear_output_member_stats(out)


def invalidate_all_output_runtime_caches(session: Session) -> None:
    outputs = session.exec(select(OutputSource)).all()
    for out in outputs:
        invalidate_output_runtime_cache(out)
        session.add(out)
    session.commit()


def compute_member_stats(
    out: OutputSource,
    pool: List[Channel],
    enabled_sub_ids: Set[int],
) -> Tuple[int, int, int]:
    members = aggregate_channels_from_pool(pool, out, enabled_sub_ids=enabled_sub_ids)
    total = len(members)
    enabled = sum(1 for c in members if c.is_enabled)
    return total, enabled, total - enabled


def get_or_refresh_member_stats(
    session: Session,
    out: OutputSource,
    pool: List[Channel],
    enabled_sub_ids: Set[int],
    *,
    force: bool = False,
) -> Tuple[int, int, int]:
    if (
        not force
        and out.member_total is not None
        and out.member_enabled is not None
        and out.member_disabled is not None
    ):
        return out.member_total, out.member_enabled, out.member_disabled

    total, enabled, disabled = compute_member_stats(out, pool, enabled_sub_ids)
    out.member_total = total
    out.member_enabled = enabled
    out.member_disabled = disabled
    session.add(out)
    return total, enabled, disabled