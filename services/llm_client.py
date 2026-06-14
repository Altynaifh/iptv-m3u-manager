"""OpenAI 兼容 Chat Completions 客户端。"""
import json
import re
from typing import Any, Dict, List

import aiohttp


def _extract_json_object(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        raise ValueError("模型返回为空")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        return json.loads(m.group(1).strip())
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start : end + 1])
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
