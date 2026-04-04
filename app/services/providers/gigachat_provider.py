"""GigaChat (Сбер) LLM-провайдер — российский, бесплатный тариф 1000 запросов/мес."""

import json
import logging
import ssl
import uuid
from typing import AsyncGenerator

import httpx

from app.services.providers.base import LLMProvider

logger = logging.getLogger(__name__)

AUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
API_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
MODEL = "GigaChat-Pro"
SCOPE = "GIGACHAT_API_PERS"  # бесплатный для физлиц


class GigaChatProvider(LLMProvider):
    name = "gigachat"

    def __init__(self, auth_key: str | None = None):
        from app.config import GIGACHAT_AUTH_KEY
        self.auth_key = auth_key or GIGACHAT_AUTH_KEY
        self._token = None

    async def _get_token(self) -> str:
        """Получить OAuth-токен по auth key."""
        if not self.auth_key:
            raise RuntimeError("GIGACHAT_AUTH_KEY не задан")

        # GigaChat требует отключить проверку SSL (их сертификат самоподписанный)
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "RqUID": str(uuid.uuid4()),
            "Authorization": f"Basic {self.auth_key}",
        }

        async with httpx.AsyncClient(verify=ssl_ctx, timeout=30) as client:
            resp = await client.post(AUTH_URL, data=f"scope={SCOPE}", headers=headers)
            resp.raise_for_status()
            data = resp.json()
            self._token = data["access_token"]
            return self._token

    async def generate(self, messages: list[dict], system: str = "",
                       json_mode: bool = False, max_tokens: int = 4096) -> str:
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

        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(verify=ssl_ctx, timeout=120) as client:
                resp = await client.post(API_URL, json=body, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"]
        except Exception as e:
            logger.error(f"GigaChat ошибка: {e}")
            raise

    async def generate_stream(self, messages: list[dict], system: str = "",
                              max_tokens: int = 4096) -> AsyncGenerator[str, None]:
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

        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(verify=ssl_ctx, timeout=300) as client:
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
            logger.error(f"GigaChat stream ошибка: {e}")
            raise
