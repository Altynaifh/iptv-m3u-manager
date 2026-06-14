"""文本 LLM 生成 explicit channel_layout。"""
import json
from datetime import datetime
from typing import Any, Dict, List, Set

from sqlmodel import Session

from models import Channel, OutputSource
from services.llm_client import LlmClient, _extract_json_object
from services.llm_settings import load_llm_blocks
from services.output_resolver import aggregate_channels, organize_candidates

# 标准组名顺序（与社区 M3U / EPG 习惯一致）
STANDARD_GROUP_ORDER = [
    "央视",
    "卫视",
    "港澳台",
    "地方台",
    "数字频道",
    "其他",
]

ORGANIZE_SYSTEM = """你是中国 IPTV 节目表编排助手。输入是候选频道 JSON（含 id、name、可选 tvg_id；source_group 仅作参考）。

【重要】完全忽略 source_group 与订阅自带分组。只根据频道名称、tvg_id、以及你对中文电视频道的常识，按中国大陆 IPTV 社区通用习惯分组并排序。

【组间顺序】必须严格按以下 title 先后输出 groups（没有的组可省略，但顺序不变）：
1. 央视 — 中央电视台及相关（CCTV-x、CGTN 等）
2. 卫视 — 省级上星卫视（如湖南卫视、浙江卫视、东方卫视等）
3. 港澳台 — 香港、澳门、台湾频道（含所有翡翠台/TVB/明珠/凤凰/台视/中视/民视等，名称不标准也要归入本组）
4. 地方台 — 省市地面频道、新闻综合、都市频道等
5. 数字频道 — 付费/数字电影/纪实/少儿/购物等专业频道
6. 其他 — 无法归类的海外或杂项

【组内排序】
- 央视：CCTV-1 综合优先，其次 CCTV-2、3…13，再 CCTV-4 及其他央视套。
- 卫视：一线卫视（北京、湖南、东方、江苏、浙江、广东等）靠前，其余按频道名或常见认知排序。
- 港澳台：同系列合并（如多种「翡翠台」写法视为同一节目族，排在港澳台组内相邻位置）。
- 地方台、数字频道：按名称或频道重要性合理排序。

【输出】仅 JSON，无其它文字：
{"groups":[{"title":"分组名（必须与上述标准名一致）","channel_ids":[整数,...]}]}

规则：每个 channel_id 必须且最多出现一次；输入列表中的频道须全部写入某一组；title 必须使用上述标准分组名之一。"""


def _build_channel_payload(channels: List[Channel], sub_map: Dict[int, str]) -> List[dict]:
    out = []
    for c in channels:
        out.append(
            {
                "id": c.id,
                "name": c.name,
                "tvg_id": c.tvg_id or "",
                "source_group": c.group or "",
                "source": sub_map.get(c.subscription_id, ""),
            }
        )
    return out


def _sort_groups_by_standard_order(layout: Dict[str, Any]) -> Dict[str, Any]:
    """按 STANDARD_GROUP_ORDER 重排 groups。"""
    groups = layout.get("groups") or []
    by_title = {g.get("title"): g for g in groups if isinstance(g, dict)}
    ordered = []
    for title in STANDARD_GROUP_ORDER:
        if title in by_title:
            ordered.append(by_title[title])
    for g in groups:
        t = (g.get("title") or "").strip()
        if t and t not in STANDARD_GROUP_ORDER:
            ordered.append(g)
    return {"groups": ordered}


def validate_layout(parsed: Any, allowed_ids: Set[int]) -> Dict[str, Any]:
    if not isinstance(parsed, dict):
        raise ValueError("布局根节点必须是对象")
    groups = parsed.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ValueError("groups 不能为空")
    seen = set()
    clean_groups = []
    for g in groups:
        if not isinstance(g, dict):
            continue
        title = (g.get("title") or "未分组").strip() or "未分组"
        ids = []
        for cid in g.get("channel_ids") or []:
            try:
                i = int(cid)
            except (TypeError, ValueError):
                continue
            if i not in allowed_ids or i in seen:
                continue
            seen.add(i)
            ids.append(i)
        if ids:
            clean_groups.append({"title": title, "channel_ids": ids})
    missing = [i for i in allowed_ids if i not in seen]
    if missing:
        other = next((g for g in clean_groups if g.get("title") == "其他"), None)
        if other:
            other["channel_ids"].extend(missing)
        else:
            clean_groups.append({"title": "其他", "channel_ids": missing})
    if not clean_groups:
        raise ValueError("校验后无有效分组")
    layout = {"groups": clean_groups}
    return _sort_groups_by_standard_order(layout)


class PlaylistOrganizer:
    @classmethod
    async def organize_output(
        cls,
        session: Session,
        out: OutputSource,
        draft: Dict[str, Any] | None,
        sub_map: Dict[int, str],
    ) -> Dict[str, Any]:
        blocks = load_llm_blocks(session)
        text = blocks["llm_text"]
        client = LlmClient(text["base_url"], text["api_key"], text["model"])
        if not client.configured():
            raise ValueError("文本 LLM 未配置")

        channels = aggregate_channels(session, out, draft)
        allowed = {c.id for c in channels if c.id is not None}
        if not allowed:
            raise ValueError("没有可整理的频道（请先配置关键字/分组筛选）")

        payload = {
            "channels": _build_channel_payload(channels, sub_map),
            "locale": "zh-CN",
            "ignore_fields": ["source_group"],
            "group_order": STANDARD_GROUP_ORDER,
        }
        user = json.dumps(payload, ensure_ascii=False)
        raw = await client.chat_text(ORGANIZE_SYSTEM, user)
        parsed = _extract_json_object(raw)
        layout = validate_layout(parsed, allowed)

        out.layout_mode = "explicit"
        out.channel_layout = json.dumps(layout, ensure_ascii=False)
        out.layout_meta = json.dumps(
            {
                "organized_at": datetime.utcnow().isoformat(),
                "model": text["model"],
                "channel_count": sum(len(g["channel_ids"]) for g in layout["groups"]),
                "prompt_version": "standard-cn-groups-v1",
            },
            ensure_ascii=False,
        )
        session.add(out)
        session.commit()
        session.refresh(out)
        return layout
