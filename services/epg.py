import os
import re
import time
import gzip
import io
import asyncio
import aiohttp
import xml.etree.ElementTree as ET
from hashlib import md5
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Iterable
from dateutil import parser as date_parser
import zhconv

# EPG 缓存目录
EPG_CACHE_DIR = "./epg_cache"
if not os.path.exists(EPG_CACHE_DIR):
    os.makedirs(EPG_CACHE_DIR, exist_ok=True)

# 并发控制与请求合并
_pending_futures: Dict[str, asyncio.Future] = {}  # 合并相同 EPG 配置组的解析任务
_url_download_futures: Dict[str, asyncio.Future] = {}  # 合并同源单链下载
_url_refresh_timestamps: Dict[str, float] = {}  # 配置组上次成功强制刷新时间
_url_disk_refresh_at: Dict[str, float] = {}  # 单链 EPG 上次成功写入磁盘时间
_cd_log_suppress_until: Dict[str, float] = {}  # CD 提示去重
_locks_lock = asyncio.Lock()
_download_guard = asyncio.Lock()
# 全流程刷新窗口内同源跳过重复下载（秒）
_SAME_SOURCE_REFRESH_COOLDOWN = 120


def split_epg_urls(epg_url: str) -> List[str]:
    """拆分聚合源配置的多条 EPG 链接（| 、逗号、换行）。"""
    if not epg_url:
        return []
    urls: List[str] = []
    for part in re.split(r"[|,\n]+", epg_url):
        part = (part or "").strip()
        if part and part not in urls:
            urls.append(part)
    return urls


def primary_epg_url_for_export(epg_url: str) -> str:
    """M3U x-tvg-url 只写主链，避免播放器不识别多链。"""
    urls = split_epg_urls(epg_url)
    return urls[0] if urls else (epg_url or "")


def epg_config_key(epg_url: str) -> str:
    """同一组 EPG 源共用内存缓存键。"""
    urls = split_epg_urls(epg_url)
    if not urls:
        return ""
    return md5("|".join(urls).encode()).hexdigest()


def merge_unique_epg_urls(*epg_url_parts: Optional[str]) -> List[str]:
    """合并多段 EPG 配置并去重，保持首次出现顺序。"""
    merged: List[str] = []
    for part in epg_url_parts:
        for url in split_epg_urls(part or ""):
            if url not in merged:
                merged.append(url)
    return merged


def collect_epg_sources_for_output(output_epg_url: Optional[str]) -> List[str]:
    """聚合源专属节目表来源（忽略订阅/频道自带链）。"""
    return split_epg_urls(output_epg_url or "")


async def refresh_epg_sources(urls: List[str], refresh: bool = False) -> List[str]:
    """按去重后的来源列表刷新磁盘缓存，同源只下载一次。"""
    unique_urls: List[str] = []
    for url in urls or []:
        if url and url not in unique_urls:
            unique_urls.append(url)
    refreshed: List[str] = []
    for url in unique_urls:
        path = await fetch_epg_cached(url, refresh=refresh)
        if path:
            refreshed.append(url)
    if refresh and refreshed:
        print(f"[EPG] 已刷新 {len(refreshed)} 个节目来源（去重后）")
    return refreshed


async def refresh_epg_group(epg_url: str, refresh: bool = False) -> None:
    """按配置刷新一组 EPG 源到本地磁盘缓存。"""
    await refresh_epg_sources(split_epg_urls(epg_url), refresh=refresh)


async def refresh_epg_for_output(
    output_epg_url: Optional[str],
    *,
    refresh: bool = False,
    reload_memory: bool = True,
) -> List[str]:
    """聚合全流程：仅拉取聚合表配置的节目来源，一次下载后可选重载内存索引。"""
    sources = collect_epg_sources_for_output(output_epg_url)
    if not sources:
        return []
    await refresh_epg_sources(sources, refresh=refresh)
    if reload_memory and output_epg_url:
        EPGManager.ensure_parsed_cache_sync(output_epg_url, force_reload=True)
    return sources


async def _download_epg_to_disk(url: str, refresh: bool) -> Optional[str]:
    """实际执行单链 EPG 下载（无并发合并）。"""
    if not url:
        return None

    url_hash = md5(url.encode()).hexdigest()
    cache_path = os.path.join(EPG_CACHE_DIR, f"{url_hash}.xml")
    tmp_path = cache_path + ".tmp"

    if not refresh and os.path.exists(cache_path):
        return cache_path

    if refresh:
        last = _url_disk_refresh_at.get(url_hash, 0)
        if time.time() - last < _SAME_SOURCE_REFRESH_COOLDOWN and os.path.exists(cache_path):
            return cache_path

    print(f"[EPG] 正在下载: {url}")
    try:
        headers = {
            "User-Agent": "APTVPlayer/1.3.9 (com.ios.aptv; build:1; iOS 15.1.0) Alamofire/5.2.2",
            "Accept": "*/*",
        }
        timeout = aiohttp.ClientTimeout(total=120, connect=20)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    print(f"[EPG] 下载响应异常 {url}: HTTP {response.status}")
                    return cache_path if os.path.exists(cache_path) else None
                content = await response.read()

                if url.endswith(".gz") or content[:2] == b"\x1f\x8b":
                    try:
                        with gzip.GzipFile(fileobj=io.BytesIO(content)) as gz:
                            xml_content = gz.read()
                    except Exception:
                        xml_content = content
                else:
                    xml_content = content

                with open(tmp_path, "wb") as f:
                    f.write(xml_content)
                if os.path.exists(cache_path):
                    os.remove(cache_path)
                os.rename(tmp_path, cache_path)
        _url_disk_refresh_at[url_hash] = time.time()
        return cache_path
    except Exception as e:
        print(f"[EPG] 下载失败 {url}: {e}")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return cache_path if os.path.exists(cache_path) else None


async def fetch_epg_cached(url: str, refresh: bool = False) -> str:
    """原子化下载并缓存 EPG；同源并发请求合并为一次下载。"""
    if not url:
        return None

    url_hash = md5(url.encode()).hexdigest()
    cache_path = os.path.join(EPG_CACHE_DIR, f"{url_hash}.xml")
    if not refresh and os.path.exists(cache_path):
        return cache_path

    async with _download_guard:
        fut = _url_download_futures.get(url_hash)
        if fut is None:
            fut = asyncio.get_event_loop().create_future()
            _url_download_futures[url_hash] = fut
            asyncio.create_task(_run_coalesced_epg_download(url, url_hash, refresh, fut))

    try:
        return await fut
    except Exception:
        return cache_path if os.path.exists(cache_path) else None


async def _run_coalesced_epg_download(
    url: str,
    url_hash: str,
    refresh: bool,
    fut: asyncio.Future,
) -> None:
    try:
        result = await _download_epg_to_disk(url, refresh)
        if not fut.done():
            fut.set_result(result)
    except Exception as exc:
        if not fut.done():
            fut.set_exception(exc)
    finally:
        async with _download_guard:
            if _url_download_futures.get(url_hash) is fut:
                _url_download_futures.pop(url_hash, None)


class EPGManager:
    """EPG 管理器"""

    _cache: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _merge_parsed_data(parts: List[Dict[str, Any]]) -> Dict[str, Any]:
        """合并多源解析结果，后写入的源覆盖同名键。"""
        merged = {
            "programs": {},
            "name_map": {},
            "logos": {},
            "reverse_logos": {},
        }
        for part in parts:
            if not part:
                continue
            merged["programs"].update(part.get("programs", {}))
            merged["name_map"].update(part.get("name_map", {}))
            merged["logos"].update(part.get("logos", {}))
            merged["reverse_logos"].update(part.get("reverse_logos", {}))
        return merged

    @classmethod
    def _load_parsed_from_disk(cls, epg_url: str) -> Dict[str, Any]:
        """从磁盘加载一组 EPG 源并合并。"""
        parts = []
        for url in split_epg_urls(epg_url):
            cache_path = os.path.join(EPG_CACHE_DIR, f"{md5(url.encode()).hexdigest()}.xml")
            if os.path.exists(cache_path):
                parts.append(cls._parse_epg_file(cache_path))
        return cls._merge_parsed_data(parts)

    @classmethod
    def ensure_parsed_cache_sync(cls, epg_url: str, *, force_reload: bool = False) -> bool:
        """从磁盘 EPG XML 同步加载到内存（不触发下载），供产物生成使用。"""
        if not epg_url:
            return False
        cache_key = epg_config_key(epg_url)
        now_ts = datetime.now(timezone.utc).timestamp()
        if not force_reload and cache_key in cls._cache:
            entry = cls._cache[cache_key]
            if now_ts - entry["timestamp"] < 3600:
                return bool(entry.get("programs") or entry.get("name_map"))
        parsed = cls._load_parsed_from_disk(epg_url)
        if not parsed["programs"] and not parsed["name_map"]:
            return False
        cls._cache[cache_key] = {
            "timestamp": now_ts,
            "programs": parsed["programs"],
            "name_map": parsed["name_map"],
            "logos": parsed["logos"],
            "reverse_logos": parsed.get("reverse_logos", {}),
            "source_count": len(split_epg_urls(epg_url)),
        }
        return True

    @staticmethod
    def _channel_is_enabled(channel) -> bool:
        if hasattr(channel, "is_enabled"):
            return bool(channel.is_enabled)
        if isinstance(channel, dict):
            return bool(channel.get("is_enabled", True))
        return True

    @staticmethod
    def _channel_lookup_fields(channel):
        from services.channel_logo_overlay import effective_tvg_name_channel, effective_tvg_name_dict

        if isinstance(channel, dict):
            ch_id = channel.get("id")
            tvg_id = (channel.get("tvg_id") or "").strip()
            tvg_name = effective_tvg_name_dict(channel)
            logo = channel.get("logo")
            return ch_id, tvg_id, tvg_name, logo
        ch_id = getattr(channel, "id", None)
        tvg_id = (getattr(channel, "tvg_id", None) or "").strip()
        tvg_name = effective_tvg_name_channel(channel)
        logo = getattr(channel, "logo", None)
        return ch_id, tvg_id, tvg_name, logo

    @classmethod
    async def batch_lookup_channels_async(
        cls,
        epg_url: str,
        channels: List[Any],
        *,
        enabled_only: bool = True,
    ) -> Dict[int, Dict[str, Any]]:
        """在线程池中批量匹配，避免阻塞事件循环。"""
        return await asyncio.to_thread(
            cls.batch_lookup_channels,
            epg_url,
            channels,
            enabled_only=enabled_only,
        )

    @classmethod
    def batch_lookup_channels(
        cls,
        epg_url: str,
        channels: List[Any],
        *,
        enabled_only: bool = True,
    ) -> Dict[int, Dict[str, Any]]:
        """对已加载的 EPG 内存缓存批量匹配频道节目。"""
        if not epg_url or not cls.ensure_parsed_cache_sync(epg_url):
            return {}
        cache_key = epg_config_key(epg_url)
        entry = cls._cache.get(cache_key)
        if not entry:
            return {}
        results: Dict[int, Dict[str, Any]] = {}
        for channel in channels or []:
            if enabled_only and not cls._channel_is_enabled(channel):
                continue
            ch_id, tvg_id, tvg_name, logo = cls._channel_lookup_fields(channel)
            if ch_id is None:
                continue
            prog = cls._lookup_in_memory(entry, tvg_id, tvg_name, logo)
            results[int(ch_id)] = {
                "program": prog.get("title", "无节目信息"),
                "logo": prog.get("logo"),
            }
        return results

    @classmethod
    async def refresh_and_load(cls, epg_url: str, refresh: bool = False) -> bool:
        """下载（可选强制）并解析多源 EPG 到内存，整组只执行一次。"""
        if not epg_url:
            return False

        cache_key = epg_config_key(epg_url)
        now_ts = datetime.now(timezone.utc).timestamp()

        if not refresh and cache_key in cls._cache:
            entry = cls._cache[cache_key]
            if now_ts - entry["timestamp"] < 3600:
                return bool(entry.get("programs") or entry.get("name_map"))

        actual_refresh = refresh
        if refresh:
            last_ref = _url_refresh_timestamps.get(cache_key, 0)
            if time.time() - last_ref < 300:
                if time.time() >= _cd_log_suppress_until.get(cache_key, 0):
                    print(f"[EPG] 刷新受限 (5分钟CD): {epg_url}")
                    _cd_log_suppress_until[cache_key] = time.time() + 300
                actual_refresh = False
            else:
                _url_refresh_timestamps[cache_key] = time.time()

        async with _locks_lock:
            if cache_key in _pending_futures:
                fut = _pending_futures[cache_key]
            else:
                fut = asyncio.get_event_loop().create_future()
                _pending_futures[cache_key] = fut
                asyncio.create_task(cls._bg_refresh_at_url(epg_url, cache_key, actual_refresh))

        try:
            await asyncio.wait_for(fut, timeout=120.0)
        except Exception as e:
            import traceback

            print(f"[EPG] 加载异常: {epg_url} -> {e}")
            traceback.print_exc()
            return False

        entry = cls._cache.get(cache_key)
        return bool(entry and (entry.get("programs") or entry.get("name_map")))

    @classmethod
    def lookup_program_sync(
        cls,
        epg_url: str,
        channel_id: str,
        channel_name: str,
        current_logo: str = None,
    ) -> dict:
        """同步查找当前节目（产物生成时批量调用）。"""
        if not epg_url:
            return {"title": "无 EPG 链接", "logo": None}
        if not cls.ensure_parsed_cache_sync(epg_url):
            return {"title": "无节目信息", "logo": None}
        cache_key = epg_config_key(epg_url)
        return cls._lookup_in_memory(cls._cache[cache_key], channel_id, channel_name, current_logo)

    @classmethod
    async def get_program(
        cls,
        epg_url: str,
        channel_id: str,
        channel_name: str,
        current_logo: str = None,
        refresh: bool = False,
    ) -> dict:
        """获取单频道节目（内部走 refresh_and_load）。"""
        if not epg_url:
            return {"title": "无 EPG 链接", "logo": None}
        if not await cls.refresh_and_load(epg_url, refresh=refresh):
            return {"title": "无节目信息", "logo": None}
        cache_key = epg_config_key(epg_url)
        entry = cls._cache.get(cache_key)
        if not entry:
            return {"title": "无节目信息", "logo": None}
        return cls._lookup_in_memory(entry, channel_id, channel_name, current_logo)

    @classmethod
    async def _bg_refresh_at_url(cls, epg_url: str, cache_key: str, refresh: bool):
        """后台执行真正的数据抓取与解析"""
        try:
            parts = []
            source_urls = split_epg_urls(epg_url)
            await refresh_epg_sources(source_urls, refresh=refresh)
            loop = asyncio.get_event_loop()
            for url in source_urls:
                xml_path = os.path.join(EPG_CACHE_DIR, f"{md5(url.encode()).hexdigest()}.xml")
                if os.path.exists(xml_path):
                    parts.append(await loop.run_in_executor(None, cls._parse_epg_file, xml_path))
            parsed_data = cls._merge_parsed_data(parts)
            if parsed_data["programs"] or parsed_data["name_map"]:
                cls._cache[cache_key] = {
                    "timestamp": datetime.now(timezone.utc).timestamp(),
                    "programs": parsed_data["programs"],
                    "name_map": parsed_data["name_map"],
                    "logos": parsed_data["logos"],
                    "reverse_logos": parsed_data.get("reverse_logos", {}),
                    "source_count": len(source_urls),
                }
                if refresh:
                    _url_refresh_timestamps[cache_key] = time.time()
                print(
                    f"[EPG] 解析完成: {len(source_urls)} 个源, "
                    f"{len(parsed_data['name_map'])} 个频道变体, "
                    f"{len(parsed_data['programs'])} 个节目源"
                )
        except Exception as e:
            print(f"[EPG] 后台解析崩溃: {e}")
            import traceback

            traceback.print_exc()
        finally:
            async with _locks_lock:
                fut = _pending_futures.get(cache_key)
                if fut and not fut.done():
                    fut.set_result(True)

            await asyncio.sleep(2)
            async with _locks_lock:
                if cache_key in _pending_futures:
                    _pending_futures.pop(cache_key, None)

    @staticmethod
    def _clean_name(name: str) -> str:
        """强化清洗名称用于模糊匹配 (自动支持简繁转换)"""
        if not name:
            return ""
        # 0. 去除名字中的所有空格 (应对 "翡翠 台" 这种变体)
        name = name.replace(" ", "")

        # 1. 移除干扰符号和其中间内容
        name = re.sub(r"[\(\[【「].*?[\)\]】」]", "", name)
        # 2. 移除干扰词
        noise = [
            "4K",
            "1080P",
            "HD",
            "高清",
            "超清",
            "频道",
            "TVB",
            "CCTV",
            "备用",
            "字幕",
            "匹配",
            "*sg",
            "geo-blocked",
            "fhd",
        ]
        for word in noise:
            escaped_word = re.escape(word)
            name = re.sub(rf"\b{escaped_word}\b", "", name, flags=re.IGNORECASE)
            name = name.replace(word, "").replace(word.lower(), "")

        # 3. 移除特殊符号（保留汉字、字母、数字）
        name = re.sub(r"[^\w\u4e00-\u9fa5]", "", name)
        name = name.strip().lower()

        return zhconv.convert(name, "zh-hans")

    @staticmethod
    def _expand_lookup_candidates(seed: str, name_map: dict) -> set:
        """从 tvg-name / tvg-id 种子词扩展 EPG 候选键。"""
        candidates = set()
        if not seed:
            return candidates
        candidates.add(seed)
        candidates.add(zhconv.convert(seed, "zh-hans"))
        candidates.add(zhconv.convert(seed, "zh-hant"))
        for c in list(candidates):
            if c in name_map:
                candidates.add(name_map[c])
        cleaned = EPGManager._clean_name(seed)
        if cleaned:
            candidates.add(cleaned)
            candidates.add(zhconv.convert(cleaned, "zh-hant"))
        for c in list(candidates):
            if c in name_map:
                candidates.add(name_map[c])
        return candidates

    @staticmethod
    def _lookup_candidates_in_programs(
        candidates: set,
        programs: dict,
        name_map: dict,
        logos: dict,
        now_dt: datetime,
    ) -> dict:
        """在给定候选集内查找当前节目与台标。"""
        found_title = "无节目信息"
        found_logo = None

        for cid in candidates:
            actual_cid = cid
            if cid not in programs and cid in name_map:
                actual_cid = name_map[cid]

            if actual_cid in programs and found_title == "无节目信息":
                for start_dt, stop_dt, title in programs[actual_cid]:
                    if start_dt <= now_dt <= stop_dt:
                        found_title = title
                        break

            if actual_cid in logos and not found_logo:
                found_logo = logos[actual_cid]

            if found_title != "无节目信息" and found_logo:
                break
        return {"title": found_title, "logo": found_logo}

    @staticmethod
    def _lookup_in_memory(cache_entry, channel_id, channel_name, current_logo=None):
        """优先 tvg-name，未命中再回退 tvg-id。"""
        programs = cache_entry["programs"]
        name_map = cache_entry["name_map"]
        logos = cache_entry.get("logos", {})
        now_dt = datetime.now(timezone.utc)

        if channel_name:
            name_candidates = EPGManager._expand_lookup_candidates(channel_name, name_map)
            result = EPGManager._lookup_candidates_in_programs(
                name_candidates,
                programs,
                name_map,
                logos,
                now_dt,
            )
            if result["title"] != "无节目信息" or result["logo"]:
                return result

        if channel_id:
            id_candidates = EPGManager._expand_lookup_candidates(channel_id, name_map)
            return EPGManager._lookup_candidates_in_programs(
                id_candidates,
                programs,
                name_map,
                logos,
                now_dt,
            )

        return {"title": "无节目信息", "logo": None}

    @staticmethod
    def _parse_epg_file(xml_path):
        """流式解析 XML，移除乱码字节并处理时区"""
        programs = {}
        name_map = {}
        logos = {}
        reverse_logos = {}

        try:
            with open(xml_path, "rb") as f:
                raw_data = f.read()

            cleaned_data = re.sub(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]", b"", raw_data)
            cleaned_data = cleaned_data.replace(b" & ", b" &amp; ")

            it = ET.iterparse(io.BytesIO(cleaned_data), events=("start", "end"))
            _, root = next(it)

            while True:
                try:
                    event, elem = next(it)
                    if event == "end":
                        if elem.tag == "channel":
                            cid = elem.get("id")
                            if cid:
                                for dn in elem.findall("display-name"):
                                    if dn.text:
                                        text = dn.text.strip()
                                        for t in [
                                            text,
                                            zhconv.convert(text, "zh-hans"),
                                            zhconv.convert(text, "zh-hant"),
                                        ]:
                                            name_map[t] = cid
                                        cleaned = EPGManager._clean_name(text)
                                        if cleaned:
                                            name_map[cleaned] = cid
                                icon = elem.find("icon")
                                if icon is not None:
                                    src = icon.get("src")
                                    if src:
                                        logos[cid] = src

                        elif elem.tag == "programme":
                            chan = elem.get("channel")
                            start_str = elem.get("start")
                            stop_str = elem.get("stop")
                            if chan and start_str and stop_str:
                                try:
                                    start_dt = date_parser.parse(start_str)
                                    stop_dt = date_parser.parse(stop_str)
                                    if start_dt.tzinfo is None:
                                        start_dt = start_dt.replace(tzinfo=timezone.utc)
                                    if stop_dt.tzinfo is None:
                                        stop_dt = stop_dt.replace(tzinfo=timezone.utc)
                                    title_elem = elem.find("title")
                                    title = title_elem.text if title_elem is not None else "未知节目"
                                    if chan not in programs:
                                        programs[chan] = []
                                    programs[chan].append((start_dt, stop_dt, title))
                                except Exception:
                                    pass
                        root.clear()
                except StopIteration:
                    break
                except Exception:
                    continue

        except Exception as e:
            print(f"[EPG] 解析遇到严重异常: {e}")

        return {
            "programs": programs,
            "name_map": name_map,
            "logos": logos,
            "reverse_logos": reverse_logos,
        }