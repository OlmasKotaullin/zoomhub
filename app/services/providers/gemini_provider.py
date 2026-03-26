"""Google Gemini LLM-провайдер — бесплатный, 1M контекст."""

import json
import logging

import httpx

from app.config import GOOGLE_AI_API_KEY
from app.services.providers.base import LLMProvider

logger = logging.getLogger(__name__)

API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
MODEL = "gemini-2.0-flash"


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self):
        self.api_key = GOOGLE_AI_API_KEY

    async def generate(self, messages: list[dict], system: str = "",
                       json_mode: bool = False, max_tokens: int = 4096) -> str:
        if not self.api_key:
            raise RuntimeError("GOOGLE_AI_API_KEY не задан")

        # Map messages to Gemini format
        contents = []
        for msg in messages:
            role = msg.get("role", "user")
            if role == "system":
                continue
            gemini_role = "model" if role == "assistant" else "user"
            contents.append({
                "role": gemini_role,
                "parts": [{"text": msg["content"]}],
            })

        body = {
            "contents": contents,
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": 0.3,
            },
        }

        if system:
            body["systemInstruction"] = {
                "parts": [{"text": system}],
            }

        if json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"

        url = f"{API_BASE}/{MODEL}:generateContent?key={self.api_key}"

        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(url, json=body)
                resp.raise_for_status()
                data = resp.json()

                candidates = data.get("candidates", [])
                if not candidates:
                    raise RuntimeError(f"Gemini вернул пустой ответ: {data}")

                parts = candidates[0].get("content", {}).get("parts", [])
                return parts[0].get("text", "") if parts else ""

        except httpx.HTTPStatusError as e:
            logger.error(f"Gemini API ошибка {e.response.status_code}: {e.response.text[:300]}")
            # Retry
            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    resp = await client.post(url, json=body)
                    resp.raise_for_status()
                    data = resp.json()
                    parts = data["candidates"][0]["content"]["parts"]
                    return parts[0]["text"]
            except Exception:
                logger.error("Gemini retry не удался")
                raise

    async def health_check(self) -> bool:
        if not self.api_key:
            return False
        try:
            url = f"{API_BASE}?key={self.api_key}"
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
                return resp.status_code == 200
        except Exception:
            return False
