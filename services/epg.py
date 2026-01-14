import os
import time
import gzip
import io
import asyncio
import aiohttp
import xml.etree.ElementTree as ET
from hashlib import md5
from datetime import datetime, timezone
from typing import Dict, Any, List
from dateutil import parser as date_parser
import zhconv

# EPG 缓存目录
EPG_CACHE_DIR = "./epg_cache"
if not os.path.exists(EPG_CACHE_DIR):
    os.makedirs(EPG_CACHE_DIR, exist_ok=True)

# 并发控制与请求合并
_url_locks: Dict[str, asyncio.Lock] = {}
_pending_futures: Dict[str, asyncio.Future] = {} # 用于合并相同 URL 的解析任务
_url_refresh_timestamps: Dict[str, float] = {} # 记录上一次成功强制刷新的时间
_locks_lock = asyncio.Lock()

async def fetch_epg_cached(url: str, refresh: bool = False) -> str:
    """原子化下载并缓存 EPG"""
    if not url: return None
        
    url_hash = md5(url.encode()).hexdigest()
    cache_path = os.path.join(EPG_CACHE_DIR, f"{url_hash}.xml")
    tmp_path = cache_path + ".tmp"
    
    # 1. 强缓存模式：如果 refresh=False 且文件存在，直接视为有效
    if not refresh and os.path.exists(cache_path):
        return cache_path

    # 下载不需要全局锁主干，只需针对该文件的临时下载锁
    print(f"[EPG] 正在下载: {url}")
    try:
        # 使用 APTVPlayer 的 UA 绕过 429 封锁
        headers = {
            "User-Agent": "APTVPlayer/1.3.9 (com.ios.aptv; build:1; iOS 15.1.0) Alamofire/5.2.2",
            "Accept": "*/*"
        }
        timeout = aiohttp.ClientTimeout(total=120, connect=20)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url) as response:
                if response.status != 200: 
                    print(f"[EPG] 下载响应异常 {url}: HTTP {response.status}")
                    return cache_path if os.path.exists(cache_path) else None
                content = await response.read()
                
                if url.endswith(".gz") or content[:2] == b'\x1f\x8b':
                    try:
                        with gzip.GzipFile(fileobj=io.BytesIO(content)) as gz:
                            xml_content = gz.read()
                    except: xml_content = content
                else: xml_content = content
                
                # 先写临时文件，再瞬间移动（原子操作）
                with open(tmp_path, "wb") as f:
                    f.write(xml_content)
                if os.path.exists(cache_path): os.remove(cache_path)
                os.rename(tmp_path, cache_path)
        return cache_path
    except Exception as e:
        print(f"[EPG] 下载失败 {url}: {e}")
        if os.path.exists(tmp_path): os.remove(tmp_path)
        return cache_path if os.path.exists(cache_path) else None

class EPGManager:
    """EPG 管理器"""
    _cache: Dict[str, Dict[str, Any]] = {}
    
    @classmethod
    async def get_program(cls, epg_url: str, channel_id: str, channel_name: str, current_logo: str = None, refresh: bool = False) -> dict:
        """获取频道节目 (带请求合并与超时保护)"""
        if not epg_url: return {"title": "无 EPG 链接", "logo": None}
        
        url_hash = md5(epg_url.encode()).hexdigest()
        now_ts = datetime.now(timezone.utc).timestamp()
        
        # 1. 内存缓存极速命中
        if not refresh and url_hash in cls._cache:
            entry = cls._cache[url_hash]
            if now_ts - entry["timestamp"] < 3600:
                return cls._lookup_in_memory(entry, channel_id, channel_name, current_logo)
            
        # 2. 刷新频率控制 (Anti-Storm): 5 分钟内同一 URL 只允许一次真正的 refresh=True
        actual_refresh = refresh
        if refresh:
            last_ref = _url_refresh_timestamps.get(url_hash, 0)
            if time.time() - last_ref < 300: 
                print(f"[EPG] 刷新受限 (5分钟CD): {epg_url}")
                actual_refresh = False
            else:
                # 前置记录尝试时间，防止下载过程中由于并发穿透再次触发下载
                _url_refresh_timestamps[url_hash] = time.time()

        # 3. 请求合并逻辑 (Future Coalescing)
        async with _locks_lock:
            if url_hash in _pending_futures:
                fut = _pending_futures[url_hash]
            else:
                fut = asyncio.get_event_loop().create_future()
                _pending_futures[url_hash] = fut
                # 记录刷新任务属性
                asyncio.create_task(cls._bg_refresh_at_url(epg_url, url_hash, actual_refresh))
                
        try:
            # 增加 10 秒硬超时
            await asyncio.wait_for(fut, timeout=10.0)
            if url_hash in cls._cache:
                res = cls._lookup_in_memory(cls._cache[url_hash], channel_id, channel_name, current_logo)
                return res
        except Exception as e:
            # 记录详细错误堆栈防止静默退出
            import traceback
            print(f"[EPG] API 异常: {epg_url} -> {e}")
            traceback.print_exc()
            
        return {"title": "无节目信息", "logo": None}

    @classmethod
    async def _bg_refresh_at_url(cls, epg_url: str, url_hash: str, refresh: bool):
        """后台执行真正的数据抓取与解析"""
        try:
            xml_path = await fetch_epg_cached(epg_url, refresh=refresh)
            if xml_path and os.path.exists(xml_path):
                loop = asyncio.get_event_loop()
                parsed_data = await loop.run_in_executor(None, cls._parse_epg_file, xml_path)
                cls._cache[url_hash] = {
                    "timestamp": datetime.now(timezone.utc).timestamp(),
                    "programs": parsed_data["programs"],
                    "name_map": parsed_data["name_map"],
                    "logos": parsed_data["logos"],
                    "reverse_logos": parsed_data.get("reverse_logos", {})
                }
                # 如果是强制刷新成功，记录时间戳
                if refresh:
                    _url_refresh_timestamps[url_hash] = time.time()
                
                print(f"[EPG] 解析完成: 加载了 {len(parsed_data['name_map'])} 个频道变体, {len(parsed_data['programs'])} 个节目源")
        except Exception as e:
            print(f"[EPG] 后台解析崩溃: {e}")
            import traceback
            traceback.print_exc()
        finally:
            # 1. 显式通知所有等待该 URL 加载的请求
            async with _locks_lock:
                fut = _pending_futures.get(url_hash)
                if fut and not fut.done():
                    fut.set_result(True)
            
            # 2. 延迟 2 秒移除标记，防止前端瞬间重复触发下载
            await asyncio.sleep(2)
            async with _locks_lock:
                if url_hash in _pending_futures:
                    _pending_futures.pop(url_hash, None)

    @staticmethod
    def _clean_name(name: str) -> str:
        """强化清洗名称用于模糊匹配 (自动支持简繁转换)"""
        if not name: return ""
        import re
        # 0. 去除名字中的所有空格 (应对 "翡翠 台" 这种变体)
        name = name.replace(" ", "")
        
        # 1. 移除干扰符号和其中间内容
        name = re.sub(r'[\(\[【「].*?[\)\]】」]', '', name)
        # 2. 移除干扰词
        noise = [
            "4K", "1080P", "HD", "高清", "超清", "频道", 
            "TVB", "CCTV", "备用", "字幕", "匹配", 
            "*sg", "geo-blocked", "fhd"
        ]
        for word in noise:
            escaped_word = re.escape(word)
            name = re.sub(rf'\b{escaped_word}\b', '', name, flags=re.IGNORECASE)
            name = name.replace(word, "").replace(word.lower(), "")
        
        # 3. 移除特殊符号（保留汉字、字母、数字）
        name = re.sub(r'[^\w\u4e00-\u9fa5]', '', name)
        name = name.strip().lower()
        
        return zhconv.convert(name, 'zh-hans')

    @staticmethod
    def _lookup_in_memory(cache_entry, channel_id, channel_name, current_logo=None):
        """标准化多重查找策略"""
        programs = cache_entry["programs"]
        name_map = cache_entry["name_map"]
        logos = cache_entry.get("logos", {})
        reverse_logos = cache_entry.get("reverse_logos", {})
        
        candidates = set()
        
        # 收集所有可能的 ID 变体
        if channel_id: candidates.add(channel_id)
        if channel_name:
            candidates.add(channel_name)
            # 简繁体变体
            candidates.add(zhconv.convert(channel_name, 'zh-hans'))
            candidates.add(zhconv.convert(channel_name, 'zh-hant'))
            
            # 内存映射映射查找
            for c in list(candidates):
                if c in name_map: candidates.add(name_map[c])
            
            # 模糊匹配清洗后的名字 (清洗本身已转简体)
            c_name = EPGManager._clean_name(channel_name)
            if c_name:
                candidates.add(c_name)
                # 清洗后的繁体变体也能路过一下
                candidates.add(zhconv.convert(c_name, 'zh-hant'))
                
            # 再次深度尝试映射关系
            for c in list(candidates):
                if c in name_map: candidates.add(name_map[c])

        now_dt = datetime.now(timezone.utc)
        found_title = "无节目信息"
        found_logo = None
        
        # 深度调试与匹配追踪
        is_target = "翡翠" in channel_name or "Jade" in channel_name or "翡翠" in channel_id
        if is_target:
            print(f"[EPG] 匹配追踪 [{channel_name}]: 候选词={list(candidates)}")
            # 抽查内存核心库
            sample_keys = list(name_map.keys())[:5]
            print(f"[EPG] 匹配追踪 [{channel_name}]: 映射库样例键={sample_keys}")

        for cid in candidates:
            # 尝试通过名字映射到 ID
            actual_cid = cid 
            if cid not in programs and cid in name_map:
                actual_cid = name_map[cid]
                if is_target: print(f"[EPG] 匹配追踪 [{channel_name}]: 通过 '{cid}' 映射到 ID '{actual_cid}'")

            if actual_cid in programs and found_title == "无节目信息":
                if is_target: print(f"[EPG] 匹配追踪 [{channel_name}]: ID '{actual_cid}' 在节目库中命中！")
                for start_dt, stop_dt, title in programs[actual_cid]:
                    if start_dt <= now_dt <= stop_dt:
                        found_title = title
                        break
                if is_target and found_title == "无节目信息":
                    print(f"[EPG] 匹配追踪 [{channel_name}]: 命中频道但无当前时段节目 (当前时间: {now_dt})")
            
            if actual_cid in logos and not found_logo:
                found_logo = logos[actual_cid]
                
            if found_title != "无节目信息" and found_logo:
                break
                
        return {"title": found_title, "logo": found_logo}

    @staticmethod
    def _parse_epg_file(xml_path):
        """流式解析 XML，移除乱码字节并处理时区"""
        programs = {}
        name_map = {}
        logos = {}
        reverse_logos = {}
        import re
        
        try:
            # 方案：二进制读取 + 暴力纠正编码瑕疵
            with open(xml_path, 'rb') as f:
                raw_data = f.read()
            
            # 1. 过滤掉所有可能导致 parsing 报错的低位控制字符 (0x00-0x1F)
            # 除了 0x09 (TAB), 0x0A (LF), 0x0D (CR)
            cleaned_data = re.sub(rb'[\x00-\x08\x0b\x0c\x0e-\x1f]', b'', raw_data)
            
            # 2. 解决常见的 & 符号未转义问题 (XML 禁忌)
            # 很多 EPG 源直接写 "A & B" 而不是 "A &amp; B"
            cleaned_data = cleaned_data.replace(b' & ', b' &amp; ')
            
            # 使用 io.BytesIO 模拟流式解析，并采用更鲁棒的迭代模式
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
                                        for t in [text, zhconv.convert(text, 'zh-hans'), zhconv.convert(text, 'zh-hant')]:
                                            name_map[t] = cid
                                        cleaned = EPGManager._clean_name(text)
                                        if cleaned: name_map[cleaned] = cid
                                icon = elem.find("icon")
                                if icon is not None:
                                    src = icon.get("src")
                                    if src: logos[cid] = src
                                    
                        elif elem.tag == "programme":
                            chan = elem.get("channel")
                            start_str = elem.get("start")
                            stop_str = elem.get("stop")
                            if chan and start_str and stop_str:
                                try:
                                    start_dt = date_parser.parse(start_str)
                                    stop_dt = date_parser.parse(stop_str)
                                    if start_dt.tzinfo is None: start_dt = start_dt.replace(tzinfo=timezone.utc)
                                    if stop_dt.tzinfo is None: stop_dt = stop_dt.replace(tzinfo=timezone.utc)
                                    title_elem = elem.find("title")
                                    title = title_elem.text if title_elem is not None else "未知节目"
                                    if chan not in programs: programs[chan] = []
                                    programs[chan].append((start_dt, stop_dt, title))
                                except: pass
                        root.clear()
                except StopIteration:
                    break
                except Exception as ex:
                    # 遇到损坏的 XML 片段，跳过这一段继续寻找下一个标签
                    continue
                    
        except Exception as e:
            print(f"[EPG] 解析遇到严重异常: {e}")
            
        return {"programs": programs, "name_map": name_map, "logos": logos, "reverse_logos": reverse_logos}
