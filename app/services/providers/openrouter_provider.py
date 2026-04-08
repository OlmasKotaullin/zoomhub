"""OpenRouter LLM-провайдер — агрегатор с бесплатными моделями (Llama 3.3 70B, Qwen3)."""

import json
import logging
from typing import AsyncGenerator

import httpx

from app.config import OPENROUTER_API_KEY
from app.services.providers.base import LLMProvider

logger = logging.getLogger(__name__)

API_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "meta-llama/llama-3.3-70b-instruct:free"  # 65K context, бесплатная


class OpenRouterProvider(LLMProvider):
    name = "openrouter"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or OPENROUTER_API_KEY

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://zoomhub.ru",
            "X-Title": "ZoomHub",
        }

    async def generate(self, messages: list[dict], system: str = "",
                       json_mode: bool = False, max_tokens: int = 4096) -> str:
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY не задан")

        all_messages = []
        if system:
            all_messages.append({"role": "system", "content": system})

        for msg in messages:
            if msg.get("role") != "system":
                all_messages.append({"role": msg["role"], "content": msg["content"]})

        body = {
            "model": MODEL,
            "messages": all_messages,
            "max_tokens": max_tokens,
            "temperature": 0.3,
        }

        if json_mode:
            body["response_format"] = {"type": "json_object"}

        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(API_URL, json=body, headers=self._headers())
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"]

        except httpx.HTTPStatusError as e:
            logger.error(f"OpenRouter API ошибка {e.response.status_code}: {e.response.text[:300]}")
            # Retry once
            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    resp = await client.post(API_URL, json=body, headers=self._headers())
                    resp.raise_for_status()
                    data = resp.json()
                    return data["choices"][0]["message"]["content"]
            except Exception:
                logger.error("OpenRouter retry не удался")
                raise

    async def generate_stream(self, messages: list[dict], system: str = "",
                              max_tokens: int = 4096) -> AsyncGenerator[str, None]:
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY не задан")

        all_messages = []
        if system:
            all_messages.append({"role": "system", "content": system})
        for msg in messages:
            if msg.get("role") != "system":
                all_messages.append({"role": msg["role"], "content": msg["content"]})

        body = {
            "model": MODEL,
            "messages": all_messages,
            "max_tokens": max_tokens,
            "temperature": 0.3,
            "stream": True,
        }

        try:
            async with httpx.AsyncClient(timeout=300) as client:
                async with client.stream("POST", API_URL, json=body, headers=self._headers()) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str.strip() == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                            content = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                            if content:
                                yield content
                        except json.JSONDecodeError:
                            continue
        except Exception as e:
            logger.error(f"OpenRouter stream ошибка: {e}")
            raise

    async def health_check(self) -> bool:
        if not self.api_key:
            return False
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://openrouter.ai/api/v1/models",
                    headers=self._headers(),
                )
                return resp.status_code == 200
        except Exception:
            return False
