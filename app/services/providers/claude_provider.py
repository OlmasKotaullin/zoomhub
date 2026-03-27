"""Claude LLM-провайдер — Anthropic API."""

import logging
from typing import AsyncGenerator

import anthropic

from app.config import ANTHROPIC_API_KEY
from app.services.providers.base import LLMProvider

logger = logging.getLogger(__name__)


class ClaudeProvider(LLMProvider):
    name = "claude"

    def __init__(self):
        self.api_key = ANTHROPIC_API_KEY
        self.model = "claude-sonnet-4-20250514"

    async def generate(self, messages: list[dict], system: str = "",
                       json_mode: bool = False, max_tokens: int = 4096) -> str:
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY не задан")

        client = anthropic.AsyncAnthropic(api_key=self.api_key)

        # Claude не принимает system в messages — отдельный параметр
        # Фильтруем system-сообщения если попали в messages
        filtered_messages = [m for m in messages if m.get("role") != "system"]

        try:
            response = await client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=filtered_messages,
            )
            return response.content[0].text

        except anthropic.APIError as e:
            logger.error(f"Claude API ошибка: {e}")
            # Retry один раз
            try:
                response = await client.messages.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=filtered_messages,
                )
                return response.content[0].text
            except Exception:
                logger.error("Claude retry не удался")
                raise

    async def generate_stream(self, messages: list[dict], system: str = "",
                              max_tokens: int = 4096) -> AsyncGenerator[str, None]:
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY не задан")

        client = anthropic.AsyncAnthropic(api_key=self.api_key)
        filtered_messages = [m for m in messages if m.get("role") != "system"]

        try:
            async with client.messages.stream(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=filtered_messages,
            ) as stream:
                async for text in stream.text_stream:
                    yield text
        except Exception as e:
            logger.error(f"Claude stream ошибка: {e}")
            raise

    async def health_check(self) -> bool:
        return bool(self.api_key)
