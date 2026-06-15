"""文本 LLM 生成 explicit channel_layout。"""
import json
import re
from datetime import datetime
from typing import Any, Dict, List, Set

from sqlmodel import Session

from models import Channel, OutputSource
from services.llm_client import LlmClient, _extract_json_object
from services.llm_settings import load_llm_blocks
from services.channel_logo_overlay import (
    heuristic_same_channel_clusters,
    merge_same_channel_clusters,
    parse_same_channels_from_parsed,
)
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

ORGANIZE_SYSTEM = """你是中国 IPTV 节目表编排助手。输入是候选频道 JSON（含 id、name、可选 tvg_name / tvg_id；source_group 仅作参考）。

【重要】完全忽略 source_group 与订阅自带分组。只根据频道名称、tvg_name、以及你对中文电视频道的常识，按中国大陆 IPTV 社区通用习惯分组并排序。

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

【同频道识别】不同 id 但实际为同一套节目的频道（名称写法、tvg_name、订阅源不同但内容相同），写入 same_channels：
- 每组 channel_ids 至少 2 个整数
- 同一 channel_id 最多出现在一个 same_channels 组
- 示例：「TVBS新闻台」与「TVBS新闻 [4gTV]」应归为同组

【输出】仅 JSON，无其它文字：
{"groups":[{"title":"分组名（必须与上述标准名一致）","channel_ids":[整数,...]}],"same_channels":[{"channel_ids":[整数,...]}]}

规则：每个 channel_id 在 groups 中必须且最多出现一次；输入列表中的频道须全部写入某一组；title 必须使用上述标准分组名之一；same_channels 可为空数组。"""

# 用户「按主题归组」意图（如：把所有体育相关的台归为一类）
_TOPIC_REGROUP_INTENT = re.compile(r"归|一类|一组|单独|拆|合并|集中|放到|放在|独立|成组")
# 主题词 -> 频道名/tvg_name 匹配模式（后处理兜底，弥补模型仍把主题频道留在默认组）
_TOPIC_CHANNEL_PATTERNS: Dict[str, re.Pattern[str]] = {
    "体育": re.compile(
        r"体育|足球|篮球|NBA|英超|西甲|意甲|德甲|法甲|欧冠|欧联|"
        r"赛事|Sport|ESPN|世界杯|五大联赛|F1|赛车|搏击|格斗|"
        r"台球|乒乓|羽毛|高尔夫|网球|马拉松|奥运|"
        r"CCTV-?5\+?|五星体育|纬来体育|爱尔达体育|澳门体育|体育广播|体育休闲",
        re.I,
    ),
}


def _extract_custom_topic_groups(custom_prompt: str) -> List[str]:
    """从用户提示词中识别需要单独成组的主题名。"""
    text = (custom_prompt or "").strip()
    if not text or not _TOPIC_REGROUP_INTENT.search(text):
        return []
    return [name for name in _TOPIC_CHANNEL_PATTERNS if name in text]


def _channel_topic_text(ch: Channel) -> str:
    return f"{ch.name or ''} {ch.tvg_name or ''} {ch.tvg_id or ''}"


def _apply_custom_topic_relocation(
    layout: Dict[str, Any],
    custom_prompt: str,
    channels_by_id: Dict[int, Channel],
) -> Dict[str, Any]:
    """将匹配主题词的频道从默认组搬入用户指定的主题组（后处理兜底）。"""
    topics = _extract_custom_topic_groups(custom_prompt)
    if not topics:
        return layout

    groups = [dict(g) for g in (layout.get("groups") or []) if isinstance(g, dict)]
    if not groups:
        return layout

    for topic in topics:
        pattern = _TOPIC_CHANNEL_PATTERNS[topic]
        target = next((g for g in groups if (g.get("title") or "").strip() == topic), None)
        if target is None:
            target = {"title": topic, "channel_ids": []}
            groups.append(target)
        target_ids: List[int] = list(target.get("channel_ids") or [])
        target_set = set(target_ids)

        for g in groups:
            if (g.get("title") or "").strip() == topic:
                continue
            kept: List[int] = []
            for cid in g.get("channel_ids") or []:
                try:
                    i = int(cid)
                except (TypeError, ValueError):
                    continue
                ch = channels_by_id.get(i)
                if ch and pattern.search(_channel_topic_text(ch)):
                    if i not in target_set:
                        target_ids.append(i)
                        target_set.add(i)
                else:
                    kept.append(i)
            g["channel_ids"] = kept

        target["channel_ids"] = target_ids

    groups = [g for g in groups if g.get("channel_ids")]
    return {"groups": groups}


def _build_organize_system(custom_prompt: str | None) -> str:
    prompt = (custom_prompt or "").strip()
    if not prompt:
        return ORGANIZE_SYSTEM
    topic_hint = ""
    topics = _extract_custom_topic_groups(prompt)
    if topics:
        joined = "、".join(f"「{t}」" for t in topics)
        topic_hint = (
            f"\n\n【主题归组·强制执行】\n"
            f"用户要求将 {joined} 相关频道单独成组时：\n"
            f"- 必须从央视、卫视、港澳台、地方台、数字频道等所有默认组中「搬移」名称或 tvg_name 匹配的频道，"
            f"不得因默认规则留在原组。\n"
            f"- 例如体育组须包含 CCTV-5/CCTV-5+、五星体育、纬来体育、爱尔达体育、澳门体育、各类体育广播与赛事专线。\n"
            f"- 仅与用户主题无关的频道，才继续按默认规则归类。\n"
        )
    return (
        ORGANIZE_SYSTEM
        + "\n\n【用户自定义要求】\n"
        + prompt
        + topic_hint
        + "\n\n【合并规则】\n"
        + "1. 系统默认规则仅适用于用户未要求的频道与分组。\n"
        + "2. 用户明确要求的分组名、组间顺序、拆组/并组，必须严格执行；与默认规则冲突时以用户要求为准。\n"
        + "3. 用户可新增非标准分组名（如「体育」「翡翠台」）；组间顺序按用户指定的先后输出，未指定则自定义组接在相关默认组之后。\n"
        + "4. 输出仍为合法 JSON；每个 channel_id 唯一且覆盖全部输入频道；title 可为标准名或用户新增名。\n"
    )


def _build_channel_payload(channels: List[Channel], sub_map: Dict[int, str]) -> List[dict]:
    out = []
    for c in channels:
        out.append(
            {
                "id": c.id,
                "name": c.name,
                "tvg_id": c.tvg_id or "",
                "tvg_name": c.tvg_name or "",
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


def validate_layout(parsed: Any, allowed_ids: Set[int], *, preserve_group_order: bool = False) -> Dict[str, Any]:
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
    if preserve_group_order:
        return layout
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

        custom_prompt = ""
        if draft and isinstance(draft, dict):
            custom_prompt = (draft.get("ai_organize_prompt") or "").strip()
        if not custom_prompt:
            custom_prompt = (getattr(out, "ai_organize_prompt", "") or "").strip()

        payload: Dict[str, Any] = {
            "channels": _build_channel_payload(channels, sub_map),
            "locale": "zh-CN",
            "ignore_fields": ["source_group"],
            "default_group_order": STANDARD_GROUP_ORDER,
        }
        if custom_prompt:
            payload["user_custom_instructions"] = custom_prompt
            payload["group_order_note"] = (
                "存在用户自定义要求时，组间顺序以用户要求为准，可插入非标准分组名；"
                "勿因 default_group_order 把主题频道强行留在央视/港澳台等默认组。"
            )
        else:
            payload["group_order"] = STANDARD_GROUP_ORDER
        user = json.dumps(payload, ensure_ascii=False)
        system_prompt = _build_organize_system(custom_prompt)
        raw = await client.chat_text(system_prompt, user)
        parsed = _extract_json_object(raw)
        layout = validate_layout(parsed, allowed, preserve_group_order=bool(custom_prompt))
        if custom_prompt:
            channels_by_id = {c.id: c for c in channels if c.id is not None}
            layout = _apply_custom_topic_relocation(layout, custom_prompt, channels_by_id)
            layout = validate_layout(layout, allowed, preserve_group_order=True)
        ai_same = parse_same_channels_from_parsed(parsed, allowed)
        heuristic_same = heuristic_same_channel_clusters(channels)
        same_channels = merge_same_channel_clusters(ai_same + heuristic_same)

        out.layout_mode = "explicit"
        out.channel_layout = json.dumps(layout, ensure_ascii=False)
        out.layout_meta = json.dumps(
            {
                "organized_at": datetime.utcnow().isoformat(),
                "model": text["model"],
                "channel_count": sum(len(g["channel_ids"]) for g in layout["groups"]),
                "same_channels": same_channels,
                "prompt_version": "standard-cn-groups-v2+custom+topic-relocate+same-channel" if custom_prompt else "standard-cn-groups-v1+same-channel",
                "custom_prompt": custom_prompt or None,
            },
            ensure_ascii=False,
        )
        session.add(out)
        session.commit()
        session.refresh(out)
        return layout
