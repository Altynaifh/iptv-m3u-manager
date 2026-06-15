"""聚合源频道解析：规则筛选、explicit 布局、导出 gate。"""
import json
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlmodel import Session, select

from models import Channel, OutputSource, Subscription
from services.generator import M3UGenerator

AI_VISUAL_EXPORT_BLOCK = frozenset({"promo_loop", "invalid"})


def _parse_json_list(raw: str) -> list:
    try:
        return json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []


def _normalize_keyword_rules(raw_keywords: list) -> list:
    keywords = []
    for k in raw_keywords or []:
        if isinstance(k, str):
            keywords.append({"value": k, "group": "", "match_by": "name"})
        elif isinstance(k, dict):
            item = dict(k)
            if not item.get("match_by"):
                item["match_by"] = "name"
            keywords.append(item)
    return keywords


def _output_config(
    out: OutputSource,
    draft: Optional[Dict[str, Any]] = None,
) -> Tuple[List[int], str, List[dict], List[int], str, str]:
    if draft:
        sub_ids = draft.get("subscription_ids") or []
        regex = draft.get("filter_regex") or ".*"
        keywords = _normalize_keyword_rules(draft.get("keywords") or [])
        excluded = draft.get("excluded_channel_ids") or []
        layout_mode = draft.get("layout_mode") or out.layout_mode or "rules"
        channel_layout = draft.get("channel_layout")
        if channel_layout is None:
            channel_layout = out.channel_layout
    else:
        sub_ids = _parse_json_list(out.subscription_ids)
        regex = out.filter_regex or ".*"
        keywords = _normalize_keyword_rules(_parse_json_list(out.keywords))
        excluded = _parse_json_list(out.excluded_channel_ids or "[]")
        layout_mode = out.layout_mode or "rules"
        channel_layout = out.channel_layout

    try:
        excluded_ids = [int(x) for x in excluded]
    except (TypeError, ValueError):
        excluded_ids = []

    return sub_ids, regex, keywords, excluded_ids, layout_mode, channel_layout


def _load_channels_for_subs(session: Session, sub_ids: List[int], enabled_only: bool) -> List[Channel]:
    enabled_subs = session.exec(select(Subscription.id).where(Subscription.is_enabled == True)).all()
    active = [sid for sid in sub_ids if sid in enabled_subs] if sub_ids else list(enabled_subs)
    if not active:
        return []
    q = select(Channel).where(Channel.subscription_id.in_(active))
    if enabled_only:
        q = q.where(Channel.is_enabled == True)
    return list(session.exec(q).all())


def _apply_regex(channels: List[Channel], regex: str) -> List[Channel]:
    if not regex or regex == ".*":
        return channels
    try:
        pattern = re.compile(regex, re.IGNORECASE)
        return [c for c in channels if pattern.search(c.name or "")]
    except re.error:
        return channels


def passes_ai_visual_export_gate(channel: Channel) -> bool:
    if not channel.ai_visual_date:
        return True
    return (channel.ai_visual_status or "") not in AI_VISUAL_EXPORT_BLOCK


def _filter_channel_list(
    channels: List[Channel],
    regex: str,
    keywords: List[dict],
    excluded_ids: List[int],
    *,
    enabled_only: bool = False,
) -> List[Channel]:
    if enabled_only:
        channels = [c for c in channels if c.is_enabled]
    channels = _apply_regex(channels, regex)
    if keywords:
        return M3UGenerator.filter_channels(channels, None, keywords, excluded_ids)
    seen = set()
    out_list = []
    excluded_set = set(excluded_ids)
    for c in channels:
        if c.id in excluded_set:
            continue
        if c.url not in seen:
            out_list.append(c.model_copy())
            seen.add(c.url)
    return out_list


def filter_candidates(
    session: Session,
    out: OutputSource,
    draft: Optional[Dict[str, Any]] = None,
    *,
    enabled_only: bool = False,
) -> List[Channel]:
    sub_ids, regex, keywords, excluded_ids, _lm, _cl = _output_config(out, draft)
    channels = _load_channels_for_subs(session, sub_ids, enabled_only)
    return _filter_channel_list(
        channels, regex, keywords, excluded_ids, enabled_only=enabled_only
    )


def aggregate_channels_from_pool(
    pool: List[Channel],
    out: OutputSource,
    draft: Optional[Dict[str, Any]] = None,
    *,
    enabled_sub_ids: Optional[Set[int]] = None,
    enabled_only: bool = False,
) -> List[Channel]:
    """基于已加载频道池计算聚合成员，避免重复查库。"""
    sub_ids, regex, keywords, excluded_ids, _lm, _cl = _output_config(out, draft)
    active_subs = enabled_sub_ids or set()
    if sub_ids:
        target_subs = {sid for sid in sub_ids if sid in active_subs} if active_subs else set(sub_ids)
    else:
        target_subs = active_subs
    if not target_subs:
        return []
    channels = [c for c in pool if c.subscription_id in target_subs]
    return _filter_channel_list(
        channels, regex, keywords, excluded_ids, enabled_only=enabled_only
    )


def _expand_explicit_layout(
    session: Session,
    channel_layout_json: str,
    excluded_ids: List[int],
) -> List[Channel]:
    try:
        layout = json.loads(channel_layout_json or "{}")
    except json.JSONDecodeError:
        layout = {}
    groups = layout.get("groups") or []
    excluded_set = set(excluded_ids)
    ordered_ids: List[int] = []
    for g in groups:
        for cid in g.get("channel_ids") or []:
            try:
                i = int(cid)
            except (TypeError, ValueError):
                continue
            if i not in excluded_set and i not in ordered_ids:
                ordered_ids.append(i)
    if not ordered_ids:
        return []
    by_id = {
        c.id: c
        for c in session.exec(select(Channel).where(Channel.id.in_(ordered_ids))).all()
    }
    result = []
    for cid in ordered_ids:
        ch = by_id.get(cid)
        if not ch:
            continue
        copy = ch.model_copy()
        for g in groups:
            if cid in (g.get("channel_ids") or []):
                title = (g.get("title") or "").strip()
                if title:
                    copy.group = title
                break
        result.append(copy)
    return result




def aggregate_channels(
    session: Session,
    out: OutputSource,
    draft: Optional[Dict[str, Any]] = None,
) -> List[Channel]:
    """聚合表成员：始终按关键字/分组筛选结果统计，含已禁用频道。"""
    return filter_candidates(session, out, draft, enabled_only=False)


def _order_explicit_layout_members(
    members: List[Channel],
    channel_layout: str,
) -> List[Channel]:
    """按 explicit 布局排序成员，仅保留已在聚合成员集内的频道（与预览一致）。"""
    member_by_id = {c.id: c for c in members if c.id is not None}
    try:
        layout = json.loads(channel_layout or "{}")
    except json.JSONDecodeError:
        layout = {}
    groups_def = layout.get("groups") or []
    ordered: List[Channel] = []
    used: Set[int] = set()
    for g in groups_def:
        title = (g.get("title") or "").strip()
        for cid in g.get("channel_ids") or []:
            try:
                i = int(cid)
            except (TypeError, ValueError):
                continue
            ch = member_by_id.get(i)
            if not ch or i in used:
                continue
            copy = ch.model_copy()
            if title:
                copy.group = title
            ordered.append(copy)
            used.add(i)
    for ch in members:
        if ch.id is not None and ch.id not in used:
            ordered.append(ch.model_copy())
    return ordered


def export_m3u_channels(
    session: Session,
    out: OutputSource,
    draft: Optional[Dict[str, Any]] = None,
) -> List[Channel]:
    members = aggregate_channels(session, out, draft)
    _sub_ids, _regex, _keywords, _excluded_ids, layout_mode, channel_layout = _output_config(out, draft)

    if (layout_mode or "rules") == "explicit":
        ordered = _order_explicit_layout_members(members, channel_layout)
        return [c for c in ordered if c.is_enabled]

    return [c for c in members if c.is_enabled]


def organize_candidates(
    session: Session,
    out: OutputSource,
    draft: Optional[Dict[str, Any]] = None,
) -> List[Channel]:
    """AI 排序输入：关键字/分组筛选后的全部成员（含未启用）。"""
    return aggregate_channels(session, out, draft)


def ai_vision_candidates(
    session: Session,
    out: OutputSource,
    draft: Optional[Dict[str, Any]] = None,
) -> List[Channel]:
    """AI 视觉检测输入：筛选成员中已启用的频道。"""
    return [c for c in aggregate_channels(session, out, draft) if c.is_enabled]


def _channels_to_preview_dicts(channels: List[Channel], session: Session) -> List[dict]:
    subs = session.exec(select(Subscription)).all()
    sub_map = {s.id: s.name or s.url for s in subs}
    copies = [c.model_copy() for c in channels]
    copies = M3UGenerator.propagate_logos(copies)
    ids = [c.id for c in copies if c.id is not None]
    db_by_id = {}
    if ids:
        db_by_id = {
            ch.id: ch
            for ch in session.exec(select(Channel).where(Channel.id.in_(ids))).all()
        }
    out = []
    for c in copies:
        row = db_by_id.get(c.id)
        source_group = (row.group if row else c.group) or ""
        out.append(
            {
                **c.model_dump(),
                "source": sub_map.get(c.subscription_id, "Unknown"),
                "source_group": source_group,
            }
        )
    return out


def _bucket_by_group_title(channels: List[Channel]) -> List[Tuple[str, List[Channel]]]:
    order: List[str] = []
    buckets: Dict[str, List[Channel]] = {}
    for c in channels:
        title = (c.group or "").strip() or "默认"
        if title not in buckets:
            buckets[title] = []
            order.append(title)
        buckets[title].append(c)
    return [(t, buckets[t]) for t in order]


def _groups_to_payload(groups: List[Tuple[str, List[Channel]]], session: Session) -> List[dict]:
    return [
        {"title": title, "channels": _channels_to_preview_dicts(chs, session)}
        for title, chs in groups
    ]


def preview_export_groups(
    session: Session,
    out: OutputSource,
    draft: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """聚合预览：展示全部聚合成员（含禁用）；分组按手动规则或 explicit 布局。"""
    sub_ids, regex, keywords, excluded_ids, layout_mode, channel_layout = _output_config(out, draft)
    mode = (layout_mode or "rules").strip()
    manual_list = filter_candidates(session, out, draft, enabled_only=False)
    manual_groups = _groups_to_payload(_bucket_by_group_title(manual_list), session)
    if mode == "explicit":
        all_channels = aggregate_channels(session, out, draft)
        try:
            layout = json.loads(channel_layout or "{}")
        except json.JSONDecodeError:
            layout = {}
        groups_def = layout.get("groups") or []
        by_id = {c.id: c for c in all_channels}
        ai_groups: List[Tuple[str, List[Channel]]] = []
        used = set()
        for g in groups_def:
            title = (g.get("title") or "未命名").strip() or "未命名"
            bucket: List[Channel] = []
            for cid in g.get("channel_ids") or []:
                try:
                    i = int(cid)
                except (TypeError, ValueError):
                    continue
                ch = by_id.get(i)
                if ch and i not in used:
                    bucket.append(ch)
                    used.add(i)
            if bucket:
                ai_groups.append((title, bucket))
        for ch in all_channels:
            if ch.id not in used:
                ai_groups.append((ch.group or "未分组", [ch]))
        return {
            "layout_mode": mode,
            "manual_groups": manual_groups,
            "ai_groups": _groups_to_payload(ai_groups, session) if ai_groups else [],
            "ai_groups_stale": not bool(ai_groups),
        }
    return {
        "layout_mode": "rules",
        "manual_groups": _groups_to_payload(_bucket_by_group_title(aggregate_channels(session, out, draft)), session),
        "ai_groups": [],
    }
