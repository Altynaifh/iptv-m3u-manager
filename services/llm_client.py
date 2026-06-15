"""OpenAI 兼容 Chat Completions 客户端。"""
import json
import re
from typing import Any, List

import aiohttp


def _repair_json_text(text: str) -> str:
    """修补模型常见 JSON 瑕疵（尾逗号、弯引号等）。"""
    t = (text or "").strip()
    t = t.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    t = re.sub(r",\s*}", "}", t)
    t = re.sub(r",\s*]", "]", t)
    return t


def _extract_results_regex(text: str) -> List[dict]:
    """从非标准 JSON 文本中抽取视觉检测结果。"""
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
    # 兼容仅含 status、用 slot 序号的情况
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


def _extract_json_object(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        raise ValueError("模型返回为空")

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
    seen = set()
    for cand in candidates:
        if not cand or cand in seen:
            continue
        seen.add(cand)
        try:
            return json.loads(cand)
        except json.JSONDecodeError as e:
            last_err = e

    fallback = _extract_results_regex(text)
    if fallback:
        return {"results": fallback}

    if last_err:
        raise ValueError(str(last_err))
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
    ) -> Any:
        content = [
            {"type": "text", "text": user_text},
            {"type": "image_url", "image_url": {"url": image_data_url}},
        ]
        data = await self._chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            temperature=temperature,
        )
        return _extract_json_object(self._message_text(data))

    async def _chat(self, messages: List[dict], temperature: float) -> dict:
        if not self.configured():
            raise ValueError("LLM 未配置 base_url / api_key / model")
        url = self.chat_completions_url(self.base_url)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=180)
            ) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    raise RuntimeError(f"LLM HTTP {resp.status}: {body[:500]}")
                return json.loads(body)

    @staticmethod
    def _message_text(data: dict) -> str:
        choices = data.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        return msg.get("content") or ""