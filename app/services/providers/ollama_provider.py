"""Ollama LLM-провайдер — локальная модель через REST API."""

import json
import logging
from typing import AsyncGenerator

import httpx

from app.config import OLLAMA_URL, OLLAMA_MODEL
from app.services.providers.base import LLMProvider

logger = logging.getLogger(__name__)


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self):
        self.base_url = OLLAMA_URL
        self.model = OLLAMA_MODEL
        self._model_checked = False

    async def _ensure_model(self):
        """Проверяет что модель доступна, при необходимости fallback на другую qwen2.5."""
        if self._model_checked:
            return
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self.base_url}/api/tags")
                models = [m.get("name", "") for m in resp.json().get("models", [])]
                if not any(self.model in m for m in models):
                    available = [m for m in models if "qwen2.5" in m]
                    if available:
                        self.model = available[0]
                        logger.info(f"Модель {OLLAMA_MODEL} не найдена, fallback → {self.model}")
        except Exception:
            pass
        self._model_checked = True

    async def generate(self, messages: list[dict], system: str = "",
                       json_mode: bool = False, max_tokens: int = 4096) -> str:
        await self._ensure_model()
        url = f"{self.base_url}/api/chat"

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "num_predict": max_tokens,
                "num_ctx": 16384,
            },
        }

        if system:
            payload["messages"] = [{"role": "system", "content": system}] + payload["messages"]

        if json_mode:
            payload["format"] = "json"

        try:
            async with httpx.AsyncClient(timeout=300) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()

            data = response.json()
            content = data.get("message", {}).get("content", "")

            if not content:
                logger.warning("Ollama вернул пустой ответ")
                return ""

            return content

        except httpx.TimeoutException:
            logger.error(f"Ollama timeout ({self.model})")
            raise
        except httpx.HTTPStatusError as e:
            logger.error(f"Ollama HTTP ошибка: {e.response.status_code} — {e.response.text[:200]}")
            raise
        except Exception as e:
            logger.error(f"Ollama ошибка: {e}")
            raise

    async def generate_stream(self, messages: list[dict], system: str = "",
                              max_tokens: int = 4096) -> AsyncGenerator[str, None]:
        await self._ensure_model()
        url = f"{self.base_url}/api/chat"

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "options": {"num_predict": max_tokens, "num_ctx": 16384},
        }
        if system:
            payload["messages"] = [{"role": "system", "content": system}] + payload["messages"]

        try:
            async with httpx.AsyncClient(timeout=300) as client:
                async with client.stream("POST", url, json=payload) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            chunk = json.loads(line)
                            content = chunk.get("message", {}).get("content", "")
                            if content:
                                yield content
                        except json.JSONDecodeError:
                            continue
        except Exception as e:
            logger.error(f"Ollama stream ошибка: {e}")
            raise

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(f"{self.base_url}/api/tags")
                if response.status_code != 200:
                    return False

                data = response.json()
                models = [m.get("name", "") for m in data.get("models", [])]

                # Проверяем, есть ли нужная модель (с учётом :latest тега)
                model_base = self.model.split(":")[0]
                has_model = any(model_base in m for m in models)

                if not has_model:
                    # Fallback: если qwen2.5:7b не скачана, но есть qwen2.5:3b
                    fallback = any("qwen2.5" in m for m in models)
                    if fallback:
                        available = [m for m in models if "qwen2.5" in m]
                        self.model = available[0]
                        logger.info(f"Модель {OLLAMA_MODEL} не найдена, fallback → {self.model}")
                        return True
                    logger.warning(f"Ollama запущен, но модель {self.model} не найдена. Доступны: {models}")

                return has_model

        except Exception:
            return False
