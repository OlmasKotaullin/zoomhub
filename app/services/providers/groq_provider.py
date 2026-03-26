"""Groq LLM-провайдер — бесплатный, сверхбыстрый, Llama 3.3 70B."""

import logging

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
