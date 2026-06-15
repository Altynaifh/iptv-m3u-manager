"""AI 排序同频道识别：台标与 tvg-name 覆盖（预览 / M3U 共用）。"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from models import Channel, OutputSource

_logo_reachability_cache: Dict[str, bool] = {}

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
    if n.endswith("新闻") and not n.endswith("新闻台"):
        n += "台"
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
    """按 tvg_id、tvg_name 与归一化名称将候选频道聚为同频道集群（可交叉合并）。"""
    by_tvg: Dict[str, List[int]] = {}
    by_tvg_name: Dict[str, List[int]] = {}
    by_name: Dict[str, List[int]] = {}
    cluster_lists: List[List[int]] = []
    for ch in channels:
        if ch.id is None:
            continue
        tid = (ch.tvg_id or "").strip().lower()
        if tid:
            by_tvg.setdefault(tid, []).append(ch.id)
        tvg_name_key = (ch.tvg_name or "").strip().lower()
        if tvg_name_key:
            by_tvg_name.setdefault(tvg_name_key, []).append(ch.id)
        name_key = normalize_channel_name_key(ch.name or "")
        if name_key:
            by_name.setdefault(name_key, []).append(ch.id)
    for ids in by_tvg.values():
        if len(ids) >= 2:
            cluster_lists.append(ids)
    for ids in by_tvg_name.values():
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


def is_logo_url_reachable(url: str, *, timeout: float = 1.8) -> bool:
    """检测台标 URL 是否可访问（结果按 URL 缓存，短超时避免阻塞预览）。"""
    raw = quote_logo_url((url or "").strip())
    if not raw:
        return False
    if raw in _logo_reachability_cache:
        return _logo_reachability_cache[raw]
    ok = False
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Range": "bytes=0-255",
    }
    try:
        req = Request(raw, method="GET", headers=headers)
        with urlopen(req, timeout=timeout) as resp:
            status_ok = getattr(resp, "status", 200) < 400
            ctype = (resp.headers.get("Content-Type") or "").lower()
            type_ok = (
                not ctype
                or "image" in ctype
                or "octet-stream" in ctype
                or "application/binary" in ctype
            )
            ok = status_ok and type_ok
    except Exception:
        ok = False
    _logo_reachability_cache[raw] = ok
    return ok


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


def _donor_rank(cid: int, layout_order: Optional[Dict[int, int]]) -> int:
    return layout_order.get(cid, cid) if layout_order else cid


def _score_cluster_donor(
    *,
    epg_ok: bool,
    logo_ok: bool,
    rank: int,
) -> Tuple[int, int]:
    """分数越高越优先；同分则布局更靠前。"""
    score = (2 if epg_ok else 0) + (2 if logo_ok else 0)
    return (-score, rank)


def _resolve_reachable_logo(logo: str, epg_logo: str = "") -> str:
    """优先原生台标，不可达时回退 EPG 台标。"""
    primary = quote_logo_url((logo or "").strip())
    if primary and is_logo_url_reachable(primary):
        return primary
    fallback = quote_logo_url((epg_logo or "").strip())
    if fallback and is_logo_url_reachable(fallback):
        return fallback
    return ""


def pick_validated_cluster_donor_channel(
    channels_by_id: Dict[int, Channel],
    cluster: List[int],
    *,
    layout_order: Optional[Dict[int, int]] = None,
    epg_url: Optional[str] = None,
) -> Optional[Channel]:
    """同频道集群：优先 EPG 已匹配且台标可达的频道作为供体。"""
    epg_donor, _logo_donor, _logo_url = pick_cluster_media_donors_channel(
        channels_by_id, cluster, layout_order=layout_order, epg_url=epg_url
    )
    return epg_donor


def _rank_cluster_members_dict(
    by_id: Dict[int, dict],
    cluster: List[int],
    *,
    layout_order: Optional[Dict[int, int]] = None,
) -> List[Tuple[Tuple[int, int], dict]]:
    ranked: List[Tuple[Tuple[int, int], dict]] = []
    for cid in cluster:
        ch = by_id.get(cid)
        if not ch:
            continue
        epg_ok = epg_program_matched(ch.get("epg_program"))
        logo_ok = bool(
            _resolve_reachable_logo(ch.get("logo") or "", ch.get("epg_logo") or "")
        )
        key = _score_cluster_donor(
            epg_ok=epg_ok,
            logo_ok=logo_ok,
            rank=_donor_rank(cid, layout_order),
        )
        ranked.append((key, ch))
    ranked.sort(key=lambda item: item[0])
    return ranked


def pick_cluster_media_donors_dict(
    by_id: Dict[int, dict],
    cluster: List[int],
    *,
    layout_order: Optional[Dict[int, int]] = None,
) -> Tuple[Optional[dict], Optional[dict], str]:
    """预览 dict：拆分 EPG 供体与台标供体（台标优先选集群内可达的任一成员）。"""
    ranked = _rank_cluster_members_dict(by_id, cluster, layout_order=layout_order)
    if not ranked:
        return None, None, ""
    epg_donor = ranked[0][1]
    logo_donor: Optional[dict] = None
    logo_url = ""
    for _key, ch in ranked:
        url = _donor_reachable_logo_dict(ch)
        if url:
            logo_donor = ch
            logo_url = url
            break
    return epg_donor, logo_donor, logo_url


def pick_validated_cluster_donor_dict(
    by_id: Dict[int, dict],
    cluster: List[int],
    *,
    layout_order: Optional[Dict[int, int]] = None,
) -> Optional[dict]:
    """预览 dict 版：用已算好的 epg_program / logo 选验证通过的供体。"""
    epg_donor, _logo_donor, _logo_url = pick_cluster_media_donors_dict(
        by_id, cluster, layout_order=layout_order
    )
    return epg_donor


def _rank_cluster_members_channel(
    channels_by_id: Dict[int, Channel],
    cluster: List[int],
    *,
    layout_order: Optional[Dict[int, int]] = None,
    epg_url: Optional[str] = None,
) -> List[Tuple[Tuple[int, int], Channel]]:
    from services.epg import EPGManager

    ranked: List[Tuple[Tuple[int, int], Channel]] = []
    for cid in cluster:
        ch = channels_by_id.get(cid)
        if not ch:
            continue
        epg_ok = False
        epg_logo = ""
        if epg_url:
            prog = EPGManager.lookup_program_sync(
                epg_url,
                "",
                effective_tvg_name_channel(ch),
                ch.logo,
            )
            epg_ok = epg_program_matched(prog.get("title"))
            epg_logo = prog.get("logo") or ""
        logo_ok = bool(_resolve_reachable_logo(ch.logo or "", epg_logo))
        key = _score_cluster_donor(
            epg_ok=epg_ok,
            logo_ok=logo_ok,
            rank=_donor_rank(cid, layout_order),
        )
        ranked.append((key, ch))
    ranked.sort(key=lambda item: item[0])
    return ranked


def pick_logo_donor_channel(
    channels_by_id: Dict[int, Channel],
    cluster: List[int],
    *,
    layout_order: Optional[Dict[int, int]] = None,
) -> Tuple[Optional[Channel], str]:
    """仅按台标可达性选供体（只检测 logo 字段，不额外查 EPG）。"""
    ranked: List[Tuple[Tuple[int, int], Channel, str]] = []
    for cid in cluster:
        ch = channels_by_id.get(cid)
        if not ch:
            continue
        logo_url = _resolve_reachable_logo(ch.logo or "", "")
        key = _score_cluster_donor(
            epg_ok=False,
            logo_ok=bool(logo_url),
            rank=_donor_rank(cid, layout_order),
        )
        ranked.append((key, ch, logo_url))
    if not ranked:
        return None, ""
    ranked.sort(key=lambda item: item[0])
    for _key, ch, logo_url in ranked:
        if logo_url:
            return ch, logo_url
    ch = ranked[0][1]
    return ch, quote_logo_url(ch.logo or "")


def pick_cluster_media_donors_channel(
    channels_by_id: Dict[int, Channel],
    cluster: List[int],
    *,
    layout_order: Optional[Dict[int, int]] = None,
    epg_url: Optional[str] = None,
) -> Tuple[Optional[Channel], Optional[Channel], str]:
    """Channel 版：拆分 EPG 供体与台标供体。"""
    ranked = _rank_cluster_members_channel(
        channels_by_id, cluster, layout_order=layout_order, epg_url=epg_url
    )
    if not ranked:
        return None, None, ""
    epg_donor = ranked[0][1]
    logo_donor, logo_url = pick_logo_donor_channel(
        channels_by_id, cluster, layout_order=layout_order
    )
    return epg_donor, logo_donor, logo_url


def pick_layout_logo_donor(
    channels_by_id: Dict[int, Channel],
    cluster: List[int],
    *,
    layout_order: Optional[Dict[int, int]] = None,
) -> Optional[Channel]:
    """仅按布局顺序选有 logo 字符串的供体（不做 HTTP 探测）。"""
    candidates: List[tuple] = []
    for cid in cluster:
        ch = channels_by_id.get(cid)
        if ch and (ch.logo or "").strip():
            candidates.append((_donor_rank(cid, layout_order), ch))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def pick_canonical_logo_donor(
    channels_by_id: Dict[int, Channel],
    cluster: List[int],
    *,
    layout_order: Optional[Dict[int, int]] = None,
    epg_url: Optional[str] = None,
    validate_logos: bool = True,
) -> Optional[Channel]:
    """在集群内选主台标源（可跳过 HTTP 探测以加速预览构建）。"""
    if validate_logos:
        donor = pick_validated_cluster_donor_channel(
            channels_by_id, cluster, layout_order=layout_order, epg_url=epg_url
        )
        if donor:
            return donor
    return pick_layout_logo_donor(channels_by_id, cluster, layout_order=layout_order)


def _donor_reachable_logo_channel(ch: Channel, epg_url: Optional[str] = None) -> str:
    from services.epg import EPGManager

    epg_logo = ""
    if epg_url:
        prog = EPGManager.lookup_program_sync(
            epg_url,
            "",
            effective_tvg_name_channel(ch),
            ch.logo,
        )
        epg_logo = prog.get("logo") or ""
    return _resolve_reachable_logo(ch.logo or "", epg_logo)


def _donor_reachable_logo_dict(ch: dict) -> str:
    return _resolve_reachable_logo(ch.get("logo") or "", ch.get("epg_logo") or "")


def compute_logo_overlays(
    channels: List[Channel],
    same_channel_clusters: List[List[int]],
    *,
    layout_order: Optional[Dict[int, int]] = None,
    epg_url: Optional[str] = None,
    validate_logos: bool = True,
) -> Dict[str, Dict[str, Any]]:
    """同频道集群：从供体覆盖缺失/不可达台标。"""
    by_id = {c.id: c for c in channels if c.id is not None}
    overlays: Dict[str, Dict[str, Any]] = {}
    for cluster in same_channel_clusters:
        if len(cluster) < 2:
            continue
        logo_donor: Optional[Channel] = None
        logo_url = ""
        if validate_logos:
            logo_donor, logo_url = pick_logo_donor_channel(
                by_id, cluster, layout_order=layout_order
            )
        else:
            logo_donor = pick_layout_logo_donor(
                by_id, cluster, layout_order=layout_order
            )
            if logo_donor:
                logo_url = quote_logo_url(logo_donor.logo or "")
        if not logo_donor or logo_donor.id is None or not logo_url:
            continue
        for cid in cluster:
            ch = by_id.get(cid)
            if not ch:
                continue
            native = quote_logo_url(ch.logo or "")
            if validate_logos:
                if native and is_logo_url_reachable(native):
                    continue
            elif native:
                continue
            overlays[str(cid)] = {
                "logo": logo_url,
                "source_id": logo_donor.id,
                "source_name": logo_donor.name or "",
            }
    return overlays


def augment_logo_overlays_from_tvg_name(
    logo_overlays: Dict[str, Dict[str, Any]],
    tvg_name_overlays: Dict[str, Dict[str, Any]],
    members_by_id: Dict[int, Channel],
    *,
    epg_url: Optional[str] = None,
    clusters: Optional[List[List[int]]] = None,
    layout_order: Optional[Dict[int, int]] = None,
    validate_logos: bool = True,
) -> Dict[str, Dict[str, Any]]:
    """tvg-name 已覆盖时，从集群内台标供体补台标（可与 EPG 供体不同）。"""
    out = dict(logo_overlays or {})
    cluster_by_cid: Dict[int, List[int]] = {}
    for cluster in clusters or []:
        for cid in cluster:
            cluster_by_cid[int(cid)] = cluster

    for cid_str, tvg_ov in (tvg_name_overlays or {}).items():
        if cid_str in out:
            continue
        try:
            cid = int(cid_str)
        except (TypeError, ValueError):
            continue
        cluster = cluster_by_cid.get(cid)
        logo_donor: Optional[Channel] = None
        logo_url = ""
        if cluster:
            if validate_logos:
                logo_donor, logo_url = pick_logo_donor_channel(
                    members_by_id,
                    cluster,
                    layout_order=layout_order,
                )
            else:
                logo_donor = pick_layout_logo_donor(
                    members_by_id, cluster, layout_order=layout_order
                )
                if logo_donor:
                    logo_url = quote_logo_url(logo_donor.logo or "")
            if logo_url and logo_donor and logo_donor.id is not None:
                out[cid_str] = {
                    "logo": logo_url,
                    "source_id": logo_donor.id,
                    "source_name": logo_donor.name or "",
                }
            continue
        donor_id = tvg_ov.get("source_id")
        if donor_id is None:
            continue
        donor = members_by_id.get(int(donor_id))
        if not donor:
            continue
        if validate_logos:
            logo_url = _donor_reachable_logo_channel(donor, epg_url)
        else:
            logo_url = quote_logo_url(donor.logo or "")
        if not logo_url:
            continue
        out[cid_str] = {
            "logo": logo_url,
            "source_id": donor_id,
            "source_name": tvg_ov.get("source_name") or donor.name or "",
        }
    return out


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


def pick_layout_tvg_name_donor(
    channels_by_id: Dict[int, Channel],
    cluster: List[int],
    *,
    layout_order: Optional[Dict[int, int]] = None,
) -> Optional[Channel]:
    """仅按布局顺序选有 effective tvg-name 的供体（不做 EPG/HTTP 探测）。"""
    candidates: List[tuple] = []
    for cid in cluster:
        ch = channels_by_id.get(cid)
        if ch and effective_tvg_name_channel(ch):
            candidates.append((_donor_rank(cid, layout_order), ch))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def pick_tvg_name_donor(
    channels_by_id: Dict[int, Channel],
    cluster: List[int],
    *,
    layout_order: Optional[Dict[int, int]] = None,
    epg_url: Optional[str] = None,
    validate_cluster_media: bool = True,
) -> Optional[Channel]:
    """在集群内选主 tvg-name 源（可跳过 EPG/台标验证以加速预览）。"""
    if validate_cluster_media:
        donor = pick_validated_cluster_donor_channel(
            channels_by_id, cluster, layout_order=layout_order, epg_url=epg_url
        )
        if donor and effective_tvg_name_channel(donor):
            return donor
    return pick_layout_tvg_name_donor(
        channels_by_id, cluster, layout_order=layout_order
    )


def pick_tvg_name_donor_dict(
    by_id: Dict[int, dict],
    cluster: List[int],
    *,
    layout_order: Optional[Dict[int, int]] = None,
    require_epg_match: bool = False,
) -> Optional[dict]:
    """预览 dict 版主 tvg-name 源（验证优先）。"""
    if not require_epg_match:
        donor = pick_validated_cluster_donor_dict(by_id, cluster, layout_order=layout_order)
        if donor and effective_tvg_name_dict(donor):
            return donor
    candidates: List[tuple] = []
    for cid in cluster:
        ch = by_id.get(cid)
        if not ch or not effective_tvg_name_dict(ch):
            continue
        if require_epg_match and not epg_program_matched(ch.get("epg_program")):
            continue
        candidates.append((_donor_rank(cid, layout_order), ch))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def compute_tvg_name_overlays(
    channels: List[Channel],
    same_channel_clusters: List[List[int]],
    *,
    layout_order: Optional[Dict[int, int]] = None,
    epg_url: Optional[str] = None,
    validate_cluster_media: bool = True,
) -> Dict[str, Dict[str, Any]]:
    """同频道集群：从供体覆盖 tvg-name（无显式 tvg_name 的成员）。"""
    by_id = {c.id: c for c in channels if c.id is not None}
    overlays: Dict[str, Dict[str, Any]] = {}
    for cluster in same_channel_clusters:
        if len(cluster) < 2:
            continue
        donor = pick_tvg_name_donor(
            by_id,
            cluster,
            layout_order=layout_order,
            epg_url=epg_url,
            validate_cluster_media=validate_cluster_media,
        )
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


def _channel_native_logo_dict(ch: dict) -> str:
    if ch.get("logo_native") is not None:
        return quote_logo_url((ch.get("logo_native") or "").strip())
    return quote_logo_url((ch.get("logo") or "").strip())


def apply_validated_cluster_overlays_to_preview(
    payload: dict,
    out: OutputSource,
    members: List[Channel],
    epg_url: Optional[str],
) -> None:
    """EPG 首轮后：每集群选验证供体，统一覆盖 tvg-name 与可达台标。"""
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
        epg_donor, logo_donor, donor_logo = pick_cluster_media_donors_dict(
            by_id, cluster, layout_order=order or None
        )
        if not epg_donor:
            continue
        epg_donor_id = epg_donor.get("id")
        donor_tvg_name = effective_tvg_name_dict(epg_donor)
        epg_donor_name = epg_donor.get("name") or ""
        logo_donor_id = logo_donor.get("id") if logo_donor else epg_donor_id
        logo_donor_name = (logo_donor or epg_donor).get("name") or ""
        if not donor_tvg_name and not donor_logo:
            continue

        for cid in cluster:
            ch = by_id.get(cid)
            if not ch:
                continue

            if donor_tvg_name and not epg_program_matched(ch.get("epg_program")):
                current = effective_tvg_name_dict(ch)
                if current != donor_tvg_name:
                    _apply_tvg_name_overlay_to_dict(
                        ch,
                        {
                            "tvg_name": donor_tvg_name,
                            "source_id": epg_donor_id,
                            "source_name": epg_donor_name,
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

            if donor_logo:
                current_logo = _channel_native_logo_dict(ch)
                display_logo = quote_logo_url((ch.get("logo") or "").strip())
                need_logo = (
                    not display_logo
                    or not is_logo_url_reachable(display_logo)
                    or not is_logo_url_reachable(current_logo)
                )
                if need_logo:
                    if "logo_native" not in ch:
                        ch["logo_native"] = (ch.get("logo") or "").strip()
                    ch["logo"] = donor_logo
                    ch["logo_overlay"] = {
                        "source_id": logo_donor_id,
                        "source_name": logo_donor_name,
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
    *,
    tvg_linked_ids: Optional[Set[int]] = None,
) -> List[Channel]:
    """M3U 导出前应用同频道台标覆盖（与预览一致：overlay 已判定需覆盖则写入）。"""
    _ = tvg_linked_ids  # 保留参数以兼容旧调用
    out: List[Channel] = []
    for ch in channels:
        ov = overlays.get(str(ch.id))
        if ov:
            out.append(Channel(**{**ch.model_dump(), "logo": quote_logo_url(ov["logo"])}))
        else:
            out.append(Channel(**ch.model_dump()))
    return out


def apply_validated_cluster_overlays_to_channels(
    channels: List[Channel],
    out: OutputSource,
    members: List[Channel],
    epg_url: Optional[str],
) -> List[Channel]:
    """M3U 导出：EPG 验证后统一覆盖同频道 tvg-name 与不可达台标（与预览口径一致）。"""
    from services.epg import EPGManager

    if not epg_url or not EPGManager.ensure_parsed_cache_sync(epg_url):
        return channels

    clusters = load_same_channel_clusters(
        out.layout_meta or "{}",
        members,
        layout_mode=(out.layout_mode or "rules"),
    )
    if not clusters:
        return channels

    order = layout_channel_order(out.channel_layout or "{}")
    members_by_id = {c.id: c for c in members if c.id is not None}
    export_ids = {c.id for c in channels if c.id is not None}
    updated: Dict[int, Channel] = {
        c.id: Channel(**c.model_dump()) for c in channels if c.id is not None
    }

    for cluster in clusters:
        if len(cluster) < 2:
            continue
        epg_donor, logo_donor, donor_logo = pick_cluster_media_donors_channel(
            members_by_id,
            cluster,
            layout_order=order or None,
            epg_url=epg_url,
        )
        if not epg_donor or epg_donor.id is None:
            continue
        donor_tvg_name = effective_tvg_name_channel(epg_donor)
        if not donor_tvg_name and not donor_logo:
            continue

        for cid in cluster:
            if cid not in export_ids or cid not in updated:
                continue
            ch = updated[cid]

            if donor_tvg_name:
                prog = EPGManager.lookup_program_sync(
                    epg_url,
                    ch.tvg_id or "",
                    effective_tvg_name_channel(ch),
                    ch.logo,
                )
                if not epg_program_matched(prog.get("title")):
                    current_tvg = effective_tvg_name_channel(ch)
                    if current_tvg != donor_tvg_name:
                        ch = Channel(**{**ch.model_dump(), "tvg_name": donor_tvg_name})
                        updated[cid] = ch

            if donor_logo:
                native = quote_logo_url((ch.logo or "").strip())
                need_logo = not native or not is_logo_url_reachable(native)
                if need_logo:
                    ch = Channel(**{**ch.model_dump(), "logo": quote_logo_url(donor_logo)})
                    updated[cid] = ch

    out_list: List[Channel] = []
    for ch in channels:
        if ch.id is not None and ch.id in updated:
            out_list.append(updated[ch.id])
        else:
            out_list.append(Channel(**ch.model_dump()))
    return out_list