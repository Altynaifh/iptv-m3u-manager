import re
from typing import Dict, List, Optional, Tuple

from models import Channel

class M3UGenerator:
    """M3U 生成器"""
    
    @staticmethod
    def _keyword_matches_channel(c: Channel, k_obj: dict) -> bool:
        """单条规则是否命中频道：按名称或来源分组（group-title）"""
        k_val = (k_obj.get("value") or "").lower()
        if not k_val:
            return False
        match_by = (k_obj.get("match_by") or "name").lower()
        if match_by == "source_group":
            haystack = (c.group or "").lower()
        else:
            haystack = (c.name or "").lower()
        return k_val in haystack

    @staticmethod
    def rule_display_key(k_obj: dict) -> str:
        """预览分组标题，与前端展示一致"""
        k_val = k_obj.get("value", "")
        k_group = (k_obj.get("group") or "").strip()
        match_by = (k_obj.get("match_by") or "name").lower()
        prefix = "[分组] " if match_by == "source_group" else ""
        base = f"{prefix}{k_val}"
        return f"{base} → {k_group}" if k_group else base

    @staticmethod
    def build_rule_preview_bucket(channels: List[Channel], k_obj: dict) -> List[Channel]:
        """单条规则预览桶：仅在本规则内按 URL 去重，与 /outputs/preview 一致。"""
        k_val = (k_obj.get("value") or "").strip()
        if not k_val:
            return []
        target_group = (k_obj.get("group") or "").strip()
        seen_urls = set()
        bucket: List[Channel] = []
        for c in channels:
            if c.url in seen_urls:
                continue
            if not M3UGenerator._keyword_matches_channel(c, k_obj):
                continue
            c_copy = c.model_copy()
            if target_group:
                c_copy.group = target_group
            bucket.append(c_copy)
            seen_urls.add(c.url)
        return bucket

    @staticmethod
    def build_rule_preview_buckets(
        channels: List[Channel], keywords: List[dict]
    ) -> List[Tuple[dict, List[Channel]]]:
        """按规则顺序生成预览桶列表。"""
        out: List[Tuple[dict, List[Channel]]] = []
        for k_obj in keywords or []:
            k_val = (k_obj.get("value") or "").strip()
            if not k_val:
                continue
            out.append((k_obj, M3UGenerator.build_rule_preview_bucket(channels, k_obj)))
        return out

    @staticmethod
    def merge_members_from_preview_buckets(
        rule_buckets: List[Tuple[dict, List[Channel]]],
        excluded_ids: Optional[List[int]] = None,
    ) -> List[Channel]:
        """
        从预览桶合并聚合成员：仅桶内 ID 可入选，排除列表与前端保存口径一致。
        跨规则再按 URL 去重（规则顺序优先）。
        """
        excluded_set = set(excluded_ids) if excluded_ids else set()
        seen_urls = set()
        merged: List[Channel] = []
        for _k_obj, bucket in rule_buckets:
            for c in bucket:
                if c.id in excluded_set:
                    continue
                if c.url in seen_urls:
                    continue
                merged.append(c)
                seen_urls.add(c.url)
        return merged

    @staticmethod
    def filter_channels(channels: List[Channel], regex_pattern: str, keywords: List[dict] = None, excluded_ids: List[int] = None) -> List[Channel]:
        """根据关键字和正则筛选频道，并排除指定 ID 的频道"""
        filtered: List[Channel] = []

        if keywords:
            rule_buckets = M3UGenerator.build_rule_preview_buckets(channels, keywords)
            filtered = M3UGenerator.merge_members_from_preview_buckets(rule_buckets, excluded_ids)
        else:
            # 没关键字就按 URL 去重
            excluded_set = set(excluded_ids) if excluded_ids else set()
            seen_urls = set()
            for c in channels:
                # 跳过聚合表级别排除的频道
                if c.id in excluded_set:
                    continue
                if c.url not in seen_urls:
                    filtered.append(c.model_copy())
                    seen_urls.add(c.url)
            
        # 正则筛选
        if regex_pattern and regex_pattern != ".*":
            try:
                pattern = re.compile(regex_pattern, re.IGNORECASE)
                filtered = [c for c in filtered if pattern.search(c.name)]
            except re.error:
                # 如果正则格式不正确，则跳过
                pass
                
        return filtered

    @staticmethod
    def propagate_logos(channels: List[Channel]) -> List[Channel]:
        """台标自动补全"""
        # 构建 ID/名称 -> 有效台标的映射表
        id_logo_map = {}
        
        # 收集有效台标
        for c in channels:
            if c.logo:
                key = (c.tvg_name or c.name or c.tvg_id or "").strip()
                if key and key not in id_logo_map:
                    id_logo_map[key] = c.logo
        
        # 2. 补全缺失台标
        for c in channels:
            if not c.logo:
                key = (c.tvg_name or c.name or c.tvg_id or "").strip()
                if key and key in id_logo_map:
                    try:
                        c.logo = id_logo_map[key]
                    except Exception:
                        pass
                    
        return channels

    @staticmethod
    def generate_m3u(channels: List[Channel], sub_map: Dict[int, str] = None, epg_url: str = None, include_suffix: bool = True) -> str:
        """生成 M3U 文本"""
        # 顺便补下台标
        channels = M3UGenerator.propagate_logos(channels)

        header = "#EXTM3U"
        if epg_url:
            from services.epg import primary_epg_url_for_export
            header += f' x-tvg-url="{primary_epg_url_for_export(epg_url)}"'
        lines = [header]
        
        for c in channels:
            # 开启后缀显示，就把来源贴在名后面
            source_tag = f" ({sub_map[c.subscription_id]})" if include_suffix and sub_map and c.subscription_id in sub_map else ""
            display_name = f"{c.name}{source_tag}"
            
            # 构建属性字符串：logo、tvg-id（原值）、tvg-name（EPG 匹配主键）
            logo_attr = f' tvg-logo="{c.logo or ""}"'
            tvg_id_attr = f' tvg-id="{c.tvg_id or ""}"'
            tvg_name_value = (c.tvg_name or c.name or "").strip()
            tvg_name_attr = f' tvg-name="{tvg_name_value}"'
            group_attr = f' group-title="{c.group or "Default"}"'
            
            inf = f'#EXTINF:-1{tvg_id_attr}{tvg_name_attr}{logo_attr}{group_attr},{display_name}'
            lines.append(inf)
            lines.append(c.url)
        return "\n".join(lines)
