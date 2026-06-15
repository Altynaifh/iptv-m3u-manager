"""AI 排序同频道识别与台标覆盖（预览 / M3U 共用）。"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Set
from urllib.parse import quote, urlsplit, urlunsplit

from models import Channel, OutputSource

_EPG_MISS_TITLES = frozenset({"无节目信息", "无 EPG 链接", ""})


_NAME_CHAR_MAP = str.maketrans(
    "臺亞際聞歡樂精採視廣電",
    "台亚洲闻欢乐精采视广电",
)


def normalize_channel_name_key(name: str) -> str:
    """仅按名称归一化，用于跨 tvg_id 写法聚类。"""
    n = name or ""
    n = re.sub(r"^\[[^\]]+\]", "", n)
    n = re.sub(r"\[.*?\]|【.*?】|（.*?）|\(.*?\)", "", n)
    n = n.translate(_NAME_CHAR_MAP)
    n = re.sub(r"\s+", "", n).lower()
    for token in ("4gtv", "hd", "fhd", "sd", "字幕", "多音轨", "音轨", "geo-blocked"):
        n = n.replace(token, "")
    return n


def normalize_channel_identity(name: str, tvg_id: str = "") -> str:
    """用于启发式聚类的频道身份键。"""
    tid = (tvg_id or "").strip().lower()
    if tid:
        return f"tvg:{tid}"
    n = normalize_channel_name_key(name)
    return f"name:{n}" if n else ""


def merge_same_channel_clusters(cluster_lists: List[List[int]]) -> List[List[int]]:
    parent: Dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        if parent[x] != x:
            parent[x] = find(parent[x])
        return parent[x]

    def unite(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for cluster in cluster_lists:
        ids = [int(x) for x in cluster]
        if len(ids) < 2:
            continue
        first = ids[0]
        for cid in ids[1:]:
            unite(first, cid)

    buckets: Dict[int, List[int]] = {}
    for cid in parent:
        root = find(cid)
        buckets.setdefault(root, []).append(cid)
    return [sorted(v) for v in buckets.values() if len(v) >= 2]


def heuristic_same_channel_clusters(channels: List[Channel]) -> List[List[int]]:
    """按 tvg_id 与归一化名称将候选频道聚为同频道集群（可交叉合并）。"""
    by_tvg: Dict[str, List[int]] = {}
    by_name: Dict[str, List[int]] = {}
    cluster_lists: List[List[int]] = []
    for ch in channels:
        if ch.id is None:
            continue
        tid = (ch.tvg_id or "").strip().lower()
        if tid:
            by_tvg.setdefault(tid, []).append(ch.id)
        name_key = normalize_channel_name_key(ch.name or "")
        if name_key:
            by_name.setdefault(name_key, []).append(ch.id)
    for ids in by_tvg.values():
        if len(ids) >= 2:
            cluster_lists.append(ids)
    for ids in by_name.values():
        if len(ids) >= 2:
            cluster_lists.append(ids)
    return merge_same_channel_clusters(cluster_lists)


def parse_same_channels_from_parsed(
    parsed: Any,
    allowed_ids: Set[int],
) -> List[List[int]]:
    """从 LLM 原始 JSON 解析 same_channels。"""
    raw = []
    if isinstance(parsed, dict):
        raw = parsed.get("same_channels") or []
    clusters: List[List[int]] = []
    for item in raw:
        ids: List[int] = []
        if isinstance(item, list):
            candidates = item
        elif isinstance(item, dict):
            candidates = item.get("channel_ids") or item.get("ids") or []
        else:
            continue
        for cid in candidates:
            try:
                i = int(cid)
            except (TypeError, ValueError):
                continue
            if i in allowed_ids and i not in ids:
                ids.append(i)
        if len(ids) >= 2:
            clusters.append(ids)
    return clusters


def load_same_channel_clusters(
    out_layout_meta: str,
    channels: List[Channel],
    *,
    layout_mode: str = "rules",
) -> List[List[int]]:
    """读取 layout_meta 中 AI 标注的集群，并与启发式结果合并。"""
    allowed = {c.id for c in channels if c.id is not None}
    ai_clusters: List[List[int]] = []
    try:
        meta = json.loads(out_layout_meta or "{}")
    except json.JSONDecodeError:
        meta = {}
    for item in meta.get("same_channels") or []:
        ids: List[int] = []
        if isinstance(item, list):
            candidates = item
        elif isinstance(item, dict):
            candidates = item.get("channel_ids") or item.get("ids") or []
        else:
            continue
        for cid in candidates:
            try:
                i = int(cid)
            except (TypeError, ValueError):
                continue
            if i in allowed and i not in ids:
                ids.append(i)
        if len(ids) >= 2:
            ai_clusters.append(ids)

    merged = merge_same_channel_clusters(ai_clusters + heuristic_same_channel_clusters(channels))
    if merged:
        return merged
    if (layout_mode or "rules") == "explicit":
        return heuristic_same_channel_clusters(channels)
    return []


def quote_logo_url(url: str) -> str:
    """台标 URL 路径含中文时做百分号编码，避免浏览器加载失败。"""
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        path = quote(parts.path, safe="/:@!$&'()*+,;=%")
        return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))
    except Exception:
        return raw


def layout_channel_order(channel_layout: str) -> Dict[int, int]:
    """读取 explicit 布局中的频道先后顺序（用于同集群台标主源）。"""
    try:
        layout = json.loads(channel_layout or "{}")
    except json.JSONDecodeError:
        return {}
    order: Dict[int, int] = {}
    idx = 0
    for g in layout.get("groups") or []:
        for cid in g.get("channel_ids") or []:
            try:
                i = int(cid)
            except (TypeError, ValueError):
                continue
            if i not in order:
                order[i] = idx
                idx += 1
    return order


def pick_canonical_logo_donor(
    channels_by_id: Dict[int, Channel],
    cluster: List[int],
    *,
    layout_order: Optional[Dict[int, int]] = None,
) -> Optional[Channel]:
    """在集群内选主台标源：优先 AI 布局中更靠前且有 logo 的频道。"""
    candidates: List[tuple] = []
    for cid in cluster:
        ch = channels_by_id.get(cid)
        if ch and (ch.logo or "").strip():
            rank = layout_order.get(cid, cid) if layout_order else cid
            candidates.append((rank, ch))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def compute_logo_overlays(
    channels: List[Channel],
    same_channel_clusters: List[List[int]],
    *,
    layout_order: Optional[Dict[int, int]] = None,
) -> Dict[str, Dict[str, Any]]:
    """同频道集群：仅对 logo 为空的成员，使用布局靠前且有台标频道的 URL。"""
    by_id = {c.id: c for c in channels if c.id is not None}
    overlays: Dict[str, Dict[str, Any]] = {}
    for cluster in same_channel_clusters:
        if len(cluster) < 2:
            continue
        donor = pick_canonical_logo_donor(by_id, cluster, layout_order=layout_order)
        if not donor or donor.id is None:
            continue
        logo_url = quote_logo_url(donor.logo or "")
        if not logo_url:
            continue
        for cid in cluster:
            if cid == donor.id:
                continue
            ch = by_id.get(cid)
            if not ch or (ch.logo or "").strip():
                continue
            overlays[str(cid)] = {
                "logo": logo_url,
                "source_id": donor.id,
                "source_name": donor.name or "",
            }
    return overlays


def epg_program_matched(title: Optional[str]) -> bool:
    """EPG 是否匹配到有效节目（非占位文案）。"""
    if title is None:
        return False
    return (title or "").strip() not in _EPG_MISS_TITLES


def pick_epg_donor_dict(
    by_id: Dict[int, dict],
    cluster: List[int],
    *,
    layout_order: Optional[Dict[int, int]] = None,
) -> Optional[dict]:
    """在集群内选主节目源：布局靠前且已匹配到节目的频道。"""
    candidates: List[tuple] = []
    for cid in cluster:
        ch = by_id.get(cid)
        if ch and epg_program_matched(ch.get("epg_program")):
            rank = layout_order.get(cid, cid) if layout_order else cid
            candidates.append((rank, ch))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def apply_epg_cluster_overlays(
    payload: dict,
    out: OutputSource,
    members: List[Channel],
) -> None:
    """预览节目表：同频道集群内，从未匹配成员覆盖有匹配成员的 EPG 快照。"""
    clusters = load_same_channel_clusters(
        out.layout_meta or "{}",
        members,
        layout_mode=(out.layout_mode or "rules"),
    )
    if not clusters:
        return
    order = layout_channel_order(out.channel_layout or "{}")
    by_id: Dict[int, dict] = {}
    for key in ("manual_groups", "ai_groups"):
        for sec in payload.get(key) or []:
            for ch in sec.get("channels") or []:
                cid = ch.get("id")
                if cid is not None:
                    by_id[int(cid)] = ch

    for cluster in clusters:
        if len(cluster) < 2:
            continue
        donor = pick_epg_donor_dict(by_id, cluster, layout_order=order or None)
        if not donor:
            continue
        donor_id = donor.get("id")
        for cid in cluster:
            if cid == donor_id:
                continue
            ch = by_id.get(cid)
            if not ch or epg_program_matched(ch.get("epg_program")):
                continue
            ch["epg_program_native"] = ch.get("epg_program")
            ch["epg_logo_native"] = ch.get("epg_logo")
            ch["epg_program"] = donor.get("epg_program")
            ch["epg_logo"] = donor.get("epg_logo")
            ch["epg_overlay"] = {
                "source_id": donor_id,
                "source_name": donor.get("name") or "",
            }


def apply_logo_overlays_to_dicts(
    channel_dicts: List[dict],
    overlays: Dict[str, Dict[str, Any]],
) -> None:
    """写入预览 JSON 字段：logo_native / logo / logo_overlay。"""
    for d in channel_dicts:
        if "logo_native" not in d:
            d["logo_native"] = (d.get("logo") or "").strip()
        ov = overlays.get(str(d.get("id")))
        if ov:
            d["logo"] = quote_logo_url(ov["logo"])
            d["logo_overlay"] = {
                "source_id": ov["source_id"],
                "source_name": ov["source_name"],
            }
        else:
            d["logo_overlay"] = None
        if d.get("logo"):
            d["logo"] = quote_logo_url(d["logo"])


def apply_logo_overlays_to_channels(
    channels: List[Channel],
    overlays: Dict[str, Dict[str, Any]],
) -> List[Channel]:
    """M3U 导出前应用同频道台标覆盖。"""
    out: List[Channel] = []
    for ch in channels:
        ov = overlays.get(str(ch.id))
        if ov and not (ch.logo or "").strip():
            out.append(Channel(**{**ch.model_dump(), "logo": quote_logo_url(ov["logo"])}))
        else:
            out.append(Channel(**ch.model_dump()))
    return out