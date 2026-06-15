"""AI 排序同频道识别：台标与 tvg-name 覆盖（预览 / M3U 共用）。"""

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


def augment_logo_overlays_from_tvg_name(
    logo_overlays: Dict[str, Dict[str, Any]],
    tvg_name_overlays: Dict[str, Dict[str, Any]],
    members_by_id: Dict[int, Channel],
) -> Dict[str, Dict[str, Any]]:
    """tvg-name 已做同频道覆盖时，从同一供体补台标（含原生 URL 非空但可能失效）。"""
    out = dict(logo_overlays or {})
    for cid_str, tvg_ov in (tvg_name_overlays or {}).items():
        if cid_str in out:
            continue
        donor_id = tvg_ov.get("source_id")
        if donor_id is None:
            continue
        donor = members_by_id.get(int(donor_id))
        if not donor or not (donor.logo or "").strip():
            continue
        out[cid_str] = {
            "logo": quote_logo_url(donor.logo or ""),
            "source_id": donor_id,
            "source_name": tvg_ov.get("source_name") or donor.name or "",
        }
    return out


def sync_preview_logos_from_tvg_name_overlays(payload: dict) -> None:
    """预览 JSON：按 tvg_name_overlay 从同一供体同步台标。"""
    by_id = _payload_channel_maps(payload)
    for ch in by_id.values():
        if ch.get("logo_overlay"):
            continue
        tvg_ov = ch.get("tvg_name_overlay")
        if not tvg_ov:
            continue
        donor_id = tvg_ov.get("source_id")
        if donor_id is None:
            continue
        donor = by_id.get(int(donor_id))
        logo_url = quote_logo_url((donor.get("logo") or "").strip()) if donor else ""
        if not logo_url:
            continue
        if "logo_native" not in ch:
            ch["logo_native"] = (ch.get("logo") or "").strip()
        ch["logo"] = logo_url
        ch["logo_overlay"] = {
            "source_id": donor_id,
            "source_name": tvg_ov.get("source_name") or (donor.get("name") if donor else "") or "",
        }


def epg_program_matched(title: Optional[str]) -> bool:
    """EPG 是否匹配到有效节目（非占位文案）。"""
    if title is None:
        return False
    return (title or "").strip() not in _EPG_MISS_TITLES


def effective_tvg_name_channel(ch: Channel) -> str:
    """EPG 匹配用 tvg-name：显式 tvg_name 优先，否则回退频道名。"""
    return (ch.tvg_name or ch.name or "").strip()


def effective_tvg_name_dict(ch: dict) -> str:
    """预览 dict 版 effective tvg-name。"""
    return (ch.get("tvg_name") or ch.get("name") or "").strip()


def pick_tvg_name_donor(
    channels_by_id: Dict[int, Channel],
    cluster: List[int],
    *,
    layout_order: Optional[Dict[int, int]] = None,
) -> Optional[Channel]:
    """在集群内选主 tvg-name 源：布局靠前且有有效 tvg-name 的频道。"""
    candidates: List[tuple] = []
    for cid in cluster:
        ch = channels_by_id.get(cid)
        if ch and effective_tvg_name_channel(ch):
            rank = layout_order.get(cid, cid) if layout_order else cid
            candidates.append((rank, ch))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def pick_tvg_name_donor_dict(
    by_id: Dict[int, dict],
    cluster: List[int],
    *,
    layout_order: Optional[Dict[int, int]] = None,
    require_epg_match: bool = False,
) -> Optional[dict]:
    """预览 dict 版主 tvg-name 源；可选要求已匹配 EPG。"""
    candidates: List[tuple] = []
    for cid in cluster:
        ch = by_id.get(cid)
        if not ch or not effective_tvg_name_dict(ch):
            continue
        if require_epg_match and not epg_program_matched(ch.get("epg_program")):
            continue
        rank = layout_order.get(cid, cid) if layout_order else cid
        candidates.append((rank, ch))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def compute_tvg_name_overlays(
    channels: List[Channel],
    same_channel_clusters: List[List[int]],
    *,
    layout_order: Optional[Dict[int, int]] = None,
) -> Dict[str, Dict[str, Any]]:
    """同频道集群：仅对无显式 tvg_name 的成员，使用主源 tvg-name。"""
    by_id = {c.id: c for c in channels if c.id is not None}
    overlays: Dict[str, Dict[str, Any]] = {}
    for cluster in same_channel_clusters:
        if len(cluster) < 2:
            continue
        donor = pick_tvg_name_donor(by_id, cluster, layout_order=layout_order)
        if not donor or donor.id is None:
            continue
        tvg_name = effective_tvg_name_channel(donor)
        if not tvg_name:
            continue
        for cid in cluster:
            if cid == donor.id:
                continue
            ch = by_id.get(cid)
            if not ch or (ch.tvg_name or "").strip():
                continue
            overlays[str(cid)] = {
                "tvg_name": tvg_name,
                "source_id": donor.id,
                "source_name": donor.name or "",
            }
    return overlays


def _payload_channel_maps(payload: dict) -> Dict[int, dict]:
    by_id: Dict[int, dict] = {}
    for key in ("manual_groups", "ai_groups"):
        for sec in payload.get(key) or []:
            for ch in sec.get("channels") or []:
                cid = ch.get("id")
                if cid is not None:
                    by_id[int(cid)] = ch
    return by_id


def _apply_tvg_name_overlay_to_dict(ch: dict, ov: Dict[str, Any]) -> None:
    if "tvg_name_native" not in ch:
        ch["tvg_name_native"] = (ch.get("tvg_name") or "").strip()
    ch["tvg_name"] = ov["tvg_name"]
    ch["tvg_name_overlay"] = {
        "source_id": ov["source_id"],
        "source_name": ov["source_name"],
    }


def apply_tvg_name_overlays_to_dicts(
    channel_dicts: List[dict],
    overlays: Dict[str, Dict[str, Any]],
) -> None:
    for d in channel_dicts:
        ov = overlays.get(str(d.get("id")))
        if ov:
            _apply_tvg_name_overlay_to_dict(d, ov)
        else:
            if "tvg_name_native" not in d:
                d["tvg_name_native"] = (d.get("tvg_name") or "").strip()
            d["tvg_name_overlay"] = None


def apply_tvg_name_overlays_to_channels(
    channels: List[Channel],
    overlays: Dict[str, Dict[str, Any]],
) -> List[Channel]:
    """M3U 导出：仅对无显式 tvg_name 的成员写入主源 tvg-name。"""
    out: List[Channel] = []
    for ch in channels:
        ov = overlays.get(str(ch.id))
        if ov and not (ch.tvg_name or "").strip():
            out.append(Channel(**{**ch.model_dump(), "tvg_name": ov["tvg_name"]}))
        else:
            out.append(Channel(**ch.model_dump()))
    return out


def prepare_preview_tvg_names(
    payload: dict,
    out: OutputSource,
    members: List[Channel],
) -> None:
    """EPG 查询前：对无显式 tvg_name 成员做同频道覆盖。"""
    clusters = load_same_channel_clusters(
        out.layout_meta or "{}",
        members,
        layout_mode=(out.layout_mode or "rules"),
    )
    if not clusters:
        return
    order = layout_channel_order(out.channel_layout or "{}")
    overlays = compute_tvg_name_overlays(members, clusters, layout_order=order or None)
    by_id = _payload_channel_maps(payload)
    for cid_str, ov in overlays.items():
        ch = by_id.get(int(cid_str))
        if ch:
            _apply_tvg_name_overlay_to_dict(ch, ov)


def fix_epg_mismatch_via_tvg_name(
    payload: dict,
    out: OutputSource,
    members: List[Channel],
    epg_url: str,
) -> None:
    """EPG 首次查询后：未匹配成员改用同集群已匹配成员的 tvg-name 并重新查询。"""
    from services.epg import EPGManager

    if not epg_url or not EPGManager.ensure_parsed_cache_sync(epg_url):
        return
    clusters = load_same_channel_clusters(
        out.layout_meta or "{}",
        members,
        layout_mode=(out.layout_mode or "rules"),
    )
    if not clusters:
        return
    order = layout_channel_order(out.channel_layout or "{}")
    by_id = _payload_channel_maps(payload)

    for cluster in clusters:
        if len(cluster) < 2:
            continue
        donor = pick_tvg_name_donor_dict(
            by_id, cluster, layout_order=order or None, require_epg_match=True
        )
        if not donor:
            continue
        donor_tvg_name = effective_tvg_name_dict(donor)
        if not donor_tvg_name:
            continue
        donor_id = donor.get("id")
        for cid in cluster:
            if cid == donor_id:
                continue
            ch = by_id.get(cid)
            if not ch or epg_program_matched(ch.get("epg_program")):
                continue
            current = effective_tvg_name_dict(ch)
            if current == donor_tvg_name:
                continue
            _apply_tvg_name_overlay_to_dict(
                ch,
                {
                    "tvg_name": donor_tvg_name,
                    "source_id": donor_id,
                    "source_name": donor.get("name") or "",
                },
            )
            prog = EPGManager.lookup_program_sync(
                epg_url,
                "",
                effective_tvg_name_dict(ch),
                ch.get("logo"),
            )
            ch["epg_program"] = prog.get("title")
            ch["epg_logo"] = prog.get("logo")


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
    *,
    tvg_linked_ids: Optional[Set[int]] = None,
) -> List[Channel]:
    """M3U 导出前应用同频道台标覆盖。"""
    linked = tvg_linked_ids or set()
    out: List[Channel] = []
    for ch in channels:
        ov = overlays.get(str(ch.id))
        if ov and (not (ch.logo or "").strip() or ch.id in linked):
            out.append(Channel(**{**ch.model_dump(), "logo": quote_logo_url(ov["logo"])}))
        else:
            out.append(Channel(**ch.model_dump()))
    return out