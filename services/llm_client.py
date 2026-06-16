"""OpenAI 兼容 Chat Completions 客户端。"""
import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List

import aiohttp

_VISION_JSON_LOG: Path | None = None
# 连接阶段快速失败，避免断网时长时间挂起
_HTTP_TIMEOUT = aiohttp.ClientTimeout(connect=15, sock_connect=15, sock_read=120, total=180)


class LlmNetworkError(RuntimeError):
    """LLM 请求因网络不可达或超时失败。"""


class VisionJsonParseError(ValueError):
    """视觉 JSON 解析失败，附带最后一次模型原文便于排查。"""

    def __init__(self, message: str, raw: str = ""):
        super().__init__(message)
        self.raw = raw or ""


def _vision_json_log_path() -> Path:
    """视觉 JSON 解析失败日志路径。"""
    global _VISION_JSON_LOG
    if _VISION_JSON_LOG is not None:
        return _VISION_JSON_LOG
    root = "/data/logs" if os.path.isdir("/data") else "./data/logs"
    _VISION_JSON_LOG = Path(root) / "ai_vision_json_failures.log"
    return _VISION_JSON_LOG


def _repair_json_text(text: str) -> str:
    """修补模型常见 JSON 瑕疵（尾逗号、弯引号等）。"""
    t = (text or "").strip()
    t = t.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    t = re.sub(r",\s*}", "}", t)
    t = re.sub(r",\s*]", "]", t)
    return t


def _json_error_context(text: str, err: json.JSONDecodeError, *, radius: int = 50) -> str:
    """在解析失败位置附近截取片段，便于定位 column 类错误。"""
    pos = getattr(err, "pos", None)
    if pos is None:
        pos = max(0, int(getattr(err, "colno", 1)) - 1)
    start = max(0, pos - radius)
    end = min(len(text), pos + radius)
    snippet = text[start:end]
    caret = " " * (pos - start) + "^"
    return (
        f"{err.msg} (line {err.lineno} col {err.colno} pos {pos}); "
        f"snippet[{start}:{end}]: {snippet!r}; marker:\n{caret}"
    )


def _extract_detail_loose(chunk: str) -> str:
    """从可能含未转义双引号的片段中抽取 detail 字符串。"""
    m = re.search(r'"detail"\s*:\s*"', chunk)
    if not m:
        plain = re.search(r'"detail"\s*:\s*([^,}\]]+)', chunk)
        if plain:
            return plain.group(1).strip().strip('"').strip("'")
        return ""
    i = m.end()
    chars: List[str] = []
    n = len(chunk)
    while i < n:
        c = chunk[i]
        if c == "\\" and i + 1 < n:
            chars.append(chunk[i : i + 2])
            i += 2
            continue
        if c == '"':
            rest = chunk[i + 1 :].lstrip()
            if (
                not rest
                or rest[0] in ",}]"
                or rest.startswith(',"channel_id')
                or rest.startswith(', "channel_id')
                or rest.startswith(',"slot')
                or rest.startswith(', "slot')
                or rest.startswith(',"status')
            ):
                break
            chars.append('"')
            i += 1
            continue
        chars.append(c)
        i += 1
    return "".join(chars).replace('\\"', '"')


def _extract_vision_results_aggressive(text: str) -> List[dict]:
    """宽松抽取视觉结果：容忍 detail 内未转义双引号或截断 JSON。"""
    results: List[dict] = []
    if not text:
        return results

    chunks = re.split(r'(?=\{\s*"channel_id"\s*:)', text)
    for chunk in chunks:
        if '"channel_id"' not in chunk:
            continue
        cid_m = re.search(r'"channel_id"\s*:\s*(\d+)', chunk)
        if not cid_m:
            continue
        status_m = re.search(r'"status"\s*:\s*"([^"]+)"', chunk)
        detail = _extract_detail_loose(chunk)
        if not status_m and not detail:
            continue
        item: dict = {"channel_id": int(cid_m.group(1))}
        if status_m:
            item["status"] = status_m.group(1)
        if detail:
            item["detail"] = detail
        results.append(item)

    if results:
        return results

    for m in re.finditer(
        r'"slot"\s*:\s*(\d+)\s*,\s*"status"\s*:\s*"([^"]+)"',
        text,
    ):
        detail = _extract_detail_loose(text[m.start() : m.end() + 400])
        results.append({
            "slot": int(m.group(1)),
            "status": m.group(2),
            "detail": detail,
        })
    return results


def _extract_results_regex(text: str) -> List[dict]:
    """从标准 JSON 文本中抽取视觉检测结果（detail 须正确转义）。"""
    results: List[dict] = []
    pattern = re.compile(
        r'\{\s*"channel_id"\s*:\s*(\d+)\s*,\s*"status"\s*:\s*"([^"]+)"\s*,\s*"detail"\s*:\s*"((?:[^"\\]|\\.)*)"\s*\}',
        re.DOTALL,
    )
    for m in pattern.finditer(text):
        detail = m.group(3).replace('\\"', '"')
        results.append({
            "channel_id": int(m.group(1)),
            "status": m.group(2),
            "detail": detail,
        })
    if results:
        return results
    slot_pat = re.compile(
        r'"slot"\s*:\s*(\d+)\s*,\s*"status"\s*:\s*"([^"]+)"\s*,\s*"detail"\s*:\s*"((?:[^"\\]|\\.)*)"',
        re.DOTALL,
    )
    for m in slot_pat.finditer(text):
        results.append({
            "slot": int(m.group(1)),
            "status": m.group(2),
            "detail": m.group(3).replace('\\"', '"'),
        })
    return results


def _log_vision_json_failure(
    *,
    attempt: int,
    max_attempts: int,
    error: str,
    context: str,
    raw: str,
) -> None:
    """落盘记录视觉 JSON 解析失败，便于对照 column/char 位置排查。"""
    try:
        path = _vision_json_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        header = f"\n{'=' * 72}\n[{ts}] attempt {attempt}/{max_attempts}\nerror: {error}\n{context}\n"
        body = (raw or "")[:8000]
        with path.open("a", encoding="utf-8") as f:
            f.write(header)
            f.write("raw (trunc 8k):\n")
            f.write(body)
            f.write("\n")
    except OSError as e:
        print(f"[LLM] 无法写入视觉 JSON 失败日志: {e}")


def _parse_vision_json(text: str) -> Any:
    """解析视觉模型 JSON：标准解析 → 修补 → 正则 → 宽松抽取。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("模型返回为空")

    if text.startswith("["):
        text = '{"results":' + text + "}"

    candidates: List[str] = [text, _repair_json_text(text)]
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        block = m.group(1).strip()
        candidates.extend([block, _repair_json_text(block)])
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        slice_text = text[start : end + 1]
        candidates.extend([slice_text, _repair_json_text(slice_text)])

    last_err: Exception | None = None
    last_context = ""
    seen = set()
    for cand in candidates:
        if not cand or cand in seen:
            continue
        seen.add(cand)
        try:
            return json.loads(cand)
        except json.JSONDecodeError as e:
            last_err = e
            last_context = _json_error_context(cand, e)

    regex_fb = _extract_results_regex(text)
    aggressive_fb = _extract_vision_results_aggressive(text)
    if len(aggressive_fb) >= len(regex_fb):
        fallback = aggressive_fb
    else:
        fallback = regex_fb or aggressive_fb
    if fallback:
        print(
            f"[LLM] 视觉 JSON 宽松解析成功，抽取 {len(fallback)} 条"
            + (f"；原错误: {last_context}" if last_context else "")
        )
        return {"results": fallback}

    if last_err:
        raise ValueError(last_context or str(last_err))
    raise ValueError("无法解析 JSON")


def _find_balanced_json_object(text: str, start: int = 0) -> str | None:
    """从 start 起截取第一个括号配平的 {...} 子串（忽略字符串内的括号）。"""
    i = text.find("{", start)
    if i < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for j in range(i, len(text)):
        ch = text[j]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[i : j + 1]
    return None


def _loads_json_first_value(text: str) -> Any:
    """解析文本中第一个 JSON 值，忽略尾部多余内容。"""
    decoder = json.JSONDecoder()
    stripped = (text or "").lstrip()
    if not stripped:
        raise json.JSONDecodeError("Expecting value", text or "", 0)
    obj, _end = decoder.raw_decode(stripped)
    return obj


def _collect_json_object_candidates(text: str) -> List[str]:
    """收集 AI 文本回复里可能出现的 JSON 对象候选串。"""
    raw = (text or "").strip()
    if not raw:
        return []
    out: List[str] = []
    seen: set[str] = set()

    def add(cand: str) -> None:
        c = (cand or "").strip()
        if c and c not in seen:
            seen.add(c)
            out.append(c)

    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw, re.IGNORECASE)
    if m:
        add(m.group(1))

    pos = 0
    while pos < len(raw):
        obj = _find_balanced_json_object(raw, pos)
        if not obj:
            break
        add(obj)
        nxt = raw.find(obj, pos)
        if nxt < 0:
            break
        pos = nxt + len(obj)

    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        add(raw[start : end + 1])
    add(raw)
    return out


def _extract_organize_layout_fallback(text: str) -> dict | None:
    """AI 排序 JSON 宽松回退：分别抽取 groups / same_channels 数组。"""
    raw = text or ""
    groups: list = []
    same_channels: list = []

    for m in re.finditer(
        r'\{\s*"title"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,\s*"channel_ids"\s*:\s*\[([^\]]*)\]\s*\}',
        raw,
    ):
        title = m.group(1).replace('\\"', '"')
        ids = [int(x) for x in re.findall(r"\d+", m.group(2))]
        if ids:
            groups.append({"title": title, "channel_ids": ids})

    for m in re.finditer(r'\{\s*"channel_ids"\s*:\s*\[([^\]]*)\]\s*\}', raw):
        ids = [int(x) for x in re.findall(r"\d+", m.group(1))]
        if len(ids) >= 2:
            same_channels.append({"channel_ids": ids})

    if not groups:
        return None
    layout: dict = {"groups": groups}
    if same_channels:
        layout["same_channels"] = same_channels
    return layout


def _extract_json_object(text: str) -> Any:
    """解析通用 JSON 对象（AI 排序等）；优先配平截取，避免尾部 Extra data。"""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("模型返回为空")

    last_err: Exception | None = None
    last_context = ""
    for cand in _collect_json_object_candidates(raw):
        for variant in (cand, _repair_json_text(cand)):
            if not variant:
                continue
            for loader in (json.loads, _loads_json_first_value):
                try:
                    return loader(variant)
                except json.JSONDecodeError as e:
                    last_err = e
                    last_context = _json_error_context(variant, e)

    fallback = _extract_organize_layout_fallback(raw)
    if fallback:
        print(
            "[LLM] 排序 JSON 宽松解析成功，"
            f"groups={len(fallback.get('groups') or [])} "
            f"same_channels={len(fallback.get('same_channels') or [])}"
            + (f"；原错误: {last_context}" if last_context else "")
        )
        return fallback

    if last_err:
        raise ValueError(last_context or str(last_err))
    raise ValueError("无法解析 JSON")


class LlmClient:
    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""

    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    @staticmethod
    def chat_completions_url(base_url: str) -> str:
        """Base URL 已含 /v1 时不再重复拼接。"""
        base = (base_url or "").rstrip("/")
        if base.endswith("/v1"):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"

    async def chat_text(self, system: str, user: str, temperature: float = 0.2) -> str:
        data = await self._chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
        )
        return self._message_text(data)

    async def chat_vision_json(
        self,
        system: str,
        user_text: str,
        image_data_url: str,
        temperature: float = 0.1,
        *,
        max_attempts: int = 3,
    ) -> Any:
        """视觉模型返回 JSON；解析失败时重新请求，最多 max_attempts 次。"""
        content = [
            {"type": "text", "text": user_text},
            {"type": "image_url", "image_url": {"url": image_data_url}},
        ]
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ]
        last_err: Exception | None = None
        last_raw = ""
        attempts = max(1, int(max_attempts))
        use_json_format = True
        retry_hint = (
            "\n\n【格式要求】只输出合法 JSON，勿用 markdown 代码块。"
            "detail 字段勿含英文双引号 \"，改用中文引号或省略；字符串内特殊字符须转义。"
        )
        for attempt in range(1, attempts + 1):
            req_messages = messages
            if attempt > 1:
                req_messages = [
                    messages[0],
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": user_text + retry_hint
                                + f"\n（第 {attempt} 次请求：上次返回无法解析为 JSON，请修正。）",
                            },
                            {"type": "image_url", "image_url": {"url": image_data_url}},
                        ],
                    },
                ]
            try:
                raw = await self._chat_vision_raw(
                    req_messages,
                    temperature=temperature,
                    use_json_format=use_json_format,
                )
                last_raw = raw
                return _parse_vision_json(raw)
            except LlmNetworkError:
                raise
            except RuntimeError as e:
                if use_json_format:
                    use_json_format = False
                    print(f"[LLM] response_format 不可用，改用普通请求: {e}")
                    try:
                        raw = await self._chat_vision_raw(
                            req_messages,
                            temperature=temperature,
                            use_json_format=False,
                        )
                        last_raw = raw
                        return _parse_vision_json(raw)
                    except ValueError as ve:
                        last_err = ve
                        _log_vision_json_failure(
                            attempt=attempt,
                            max_attempts=attempts,
                            error=str(ve),
                            context=str(ve),
                            raw=last_raw,
                        )
                        if attempt < attempts:
                            print(f"[LLM] 视觉 JSON 不合规，重试 {attempt}/{attempts}: {ve}")
                            continue
                else:
                    raise
            except ValueError as e:
                last_err = e
                _log_vision_json_failure(
                    attempt=attempt,
                    max_attempts=attempts,
                    error=str(e),
                    context=str(e),
                    raw=last_raw,
                )
                if attempt < attempts:
                    print(f"[LLM] 视觉 JSON 不合规，重试 {attempt}/{attempts}: {e}")
                    continue
        raise VisionJsonParseError(
            str(last_err) if last_err else "无法解析 JSON",
            raw=last_raw,
        )

    async def _chat_vision_raw(
        self,
        messages: List[dict],
        *,
        temperature: float,
        use_json_format: bool,
    ) -> str:
        """请求视觉模型并返回原始文本；解析由调用方负责。"""
        kwargs: dict = {"temperature": temperature}
        if use_json_format:
            kwargs["response_format"] = {"type": "json_object"}
        data = await self._chat(messages, **kwargs)
        return self._message_text(data)

    async def _chat(
        self,
        messages: List[dict],
        temperature: float,
        *,
        response_format: dict | None = None,
    ) -> dict:
        if not self.configured():
            raise ValueError("LLM 未配置 base_url / api_key / model")
        url = self.chat_completions_url(self.base_url)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if response_format:
            payload["response_format"] = response_format
        try:
            async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    body = await resp.text()
                    if resp.status >= 400:
                        raise RuntimeError(f"LLM HTTP {resp.status}: {body[:500]}")
                    return json.loads(body)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise LlmNetworkError(f"LLM 网络请求失败: {type(e).__name__}: {e}") from e

    @staticmethod
    def _message_text(data: dict) -> str:
        choices = data.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        return msg.get("content") or ""