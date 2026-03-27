"""Groq LLM-провайдер — бесплатный, сверхбыстрый, Llama 3.3 70B."""

import json
import logging
from typing import AsyncGenerator

import httpx

from app.config import GROQ_API_KEY
from app.services.providers.base import LLMProvider

logger = logging.getLogger(__name__)

API_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL = "llama-3.3-70b-versatile"  # 128K context, free tier


class GroqProvider(LLMProvider):
    name = "groq"

    def __init__(self):
        self.api_key = GROQ_API_KEY

    async def generate(self, messages: list[dict], system: str = "",
                       json_mode: bool = False, max_tokens: int = 4096) -> str:
        if not self.api_key:
            raise RuntimeError("GROQ_API_KEY не задан")

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

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(API_URL, json=body, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"]

        except httpx.HTTPStatusError as e:
            logger.error(f"Groq API ошибка {e.response.status_code}: {e.response.text[:300]}")
            # Retry
            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    resp = await client.post(API_URL, json=body, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
                    return data["choices"][0]["message"]["content"]
            except Exception:
                logger.error("Groq retry не удался")
                raise

    async def generate_stream(self, messages: list[dict], system: str = "",
                              max_tokens: int = 4096) -> AsyncGenerator[str, None]:
        if not self.api_key:
            raise RuntimeError("GROQ_API_KEY не задан")

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
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=120) as client:
                async with client.stream("POST", API_URL, json=body, headers=headers) as resp:
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
            logger.error(f"Groq stream ошибка: {e}")
            raise

    async def health_check(self) -> bool:
        if not self.api_key:
            return False
        try:
            headers = {"Authorization": f"Bearer {self.api_key}"}
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get("https://api.groq.com/openai/v1/models", headers=headers)
                return resp.status_code == 200
        except Exception:
            return False
