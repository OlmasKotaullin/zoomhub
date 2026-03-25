"""GigaChat (Сбер) LLM-провайдер — русскоязычный, 1М токенов/мес бесплатно."""

import json
import logging
import uuid
from typing import AsyncGenerator

import httpx

from app.config import GIGACHAT_AUTH_KEY
from app.services.providers.base import LLMProvider

logger = logging.getLogger(__name__)

AUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
API_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
MODEL = "GigaChat"

# GigaChat uses OAuth2: Authorization key → short-lived access token (30 min)
_access_token: str | None = None
_token_expires: float = 0


class GigaChatProvider(LLMProvider):
    name = "gigachat"

    def __init__(self, api_key: str | None = None):
        self.auth_key = api_key or GIGACHAT_AUTH_KEY

    async def _get_token(self) -> str:
        """Get or refresh OAuth2 access token."""
        import time
        global _access_token, _token_expires

        if _access_token and time.time() < _token_expires - 60:
            return _access_token

        if not self.auth_key:
            raise RuntimeError("GIGACHAT_AUTH_KEY не задан")

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "RqUID": str(uuid.uuid4()),
            "Authorization": f"Basic {self.auth_key}",
        }

        async with httpx.AsyncClient(verify=False, timeout=15) as client:
            resp = await client.post(
                AUTH_URL,
                data={"scope": "GIGACHAT_API_PERS"},
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            _access_token = data["access_token"]
            _token_expires = data["expires_at"] / 1000  # ms → sec
            logger.info("GigaChat token refreshed")
            return _access_token

    async def generate(self, messages: list[dict], system: str = "",
                       json_mode: bool = False, max_tokens: int = 4096) -> str:
        if not self.auth_key:
            raise RuntimeError("GIGACHAT_AUTH_KEY не задан")

        token = await self._get_token()

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

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(verify=False, timeout=120) as client:
                resp = await client.post(API_URL, json=body, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"]

        except httpx.HTTPStatusError as e:
            logger.error(f"GigaChat API ошибка {e.response.status_code}: {e.response.text[:300]}")
            # Retry with fresh token
            try:
                global _access_token
                _access_token = None
                token = await self._get_token()
                headers["Authorization"] = f"Bearer {token}"
                async with httpx.AsyncClient(verify=False, timeout=120) as client:
                    resp = await client.post(API_URL, json=body, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
                    return data["choices"][0]["message"]["content"]
            except Exception:
                logger.error("GigaChat retry не удался")
                raise

    async def generate_stream(self, messages: list[dict], system: str = "",
                              max_tokens: int = 4096) -> AsyncGenerator[str, None]:
        if not self.auth_key:
            raise RuntimeError("GIGACHAT_AUTH_KEY не задан")

        token = await self._get_token()

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
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(verify=False, timeout=120) as client:
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
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                yield content
                        except json.JSONDecodeError:
                            continue
        except Exception as e:
            logger.error(f"GigaChat stream ошибка: {e}")
            yield f"\n\n[Ошибка: {e}]"

    async def health_check(self) -> bool:
        if not self.auth_key:
            return False
        try:
            await self._get_token()
            return True
        except Exception:
            return False
