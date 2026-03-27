"""Тесты парсера саммари и fallback-логики."""

import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from app.services.summarizer import _parse_summary, empty_summary, generate_summary


class TestParseSummary:
    """Тесты _parse_summary — парсинг JSON-ответа LLM."""

    def test_valid_json(self):
        raw = json.dumps({
            "tldr": "Обсудили план на квартал",
            "tasks": [{"task": "Сделать отчёт", "assignee": "Алмаз", "deadline": ""}],
            "topics": [{"topic": "Планирование", "details": "Обсудили Q2"}],
            "insights": [{"insight": "Нужно больше автоматизации"}],
        })
        result = _parse_summary(raw)
        assert result["tldr"] == "Обсудили план на квартал"
        assert len(result["tasks"]) == 1
        assert result["tasks"][0]["assignee"] == "Алмаз"
        assert len(result["topics"]) == 1
        assert len(result["insights"]) == 1
        assert result["raw_response"] == raw

    def test_json_with_markdown_fences(self):
        raw = '```json\n{"tldr": "Резюме встречи", "tasks": [], "topics": [], "insights": []}\n```'
        result = _parse_summary(raw)
        assert result["tldr"] == "Резюме встречи"
        assert result["tasks"] == []

    def test_truncated_json_extracts_tldr(self):
        """Обрезанный JSON — извлекаем tldr через regex."""
        raw = '{\n  "tldr": "Встреча посвящена передаче дел",\n  "tasks": ['
        result = _parse_summary(raw)
        assert result["tldr"] == "Встреча посвящена передаче дел"
        assert result["tasks"] == []
        assert result["topics"] == []

    def test_truncated_json_no_tldr(self):
        """Обрезанный JSON без tldr — пустой результат."""
        raw = '{\n  "tasks": ['
        result = _parse_summary(raw)
        assert result["tldr"] == ""

    def test_empty_response(self):
        result = _parse_summary("")
        assert result["tldr"] == ""

    def test_plain_text_response(self):
        """LLM вернул текст вместо JSON."""
        raw = "Вот резюме встречи: обсудили планы на квартал."
        result = _parse_summary(raw)
        assert result["tldr"] == ""

    def test_json_missing_fields(self):
        """JSON без некоторых полей — default пустые."""
        raw = json.dumps({"tldr": "Краткое резюме"})
        result = _parse_summary(raw)
        assert result["tldr"] == "Краткое резюме"
        assert result["tasks"] == []
        assert result["topics"] == []
        assert result["insights"] == []

    def test_tldr_with_escaped_quotes(self):
        """tldr содержит экранированные кавычки."""
        raw = '{\n  "tldr": "Обсудили \\"план\\" на квартал",\n  "tasks": ['
        result = _parse_summary(raw)
        assert 'план' in result["tldr"]


class TestEmptySummary:

    def test_returns_empty_fields(self):
        result = empty_summary()
        assert result["tldr"] == ""
        assert result["tasks"] == []
        assert result["topics"] == []
        assert result["insights"] == []
        assert result["raw_response"] == ""


class TestGenerateSummaryFallback:
    """Тесты fallback-логики generate_summary."""

    @pytest.mark.asyncio
    async def test_first_provider_succeeds(self):
        """Первый провайдер работает — fallback не нужен."""
        mock_provider = MagicMock()
        mock_provider.name = "groq"
        mock_provider.generate = AsyncMock(return_value=json.dumps({
            "tldr": "Резюме", "tasks": [], "topics": [], "insights": []
        }))

        with patch("app.services.summarizer.get_provider_for_text", return_value=mock_provider), \
             patch("app.services.summarizer.make_provider_by_name"), \
             patch("app.config.GOOGLE_AI_API_KEY", ""), \
             patch("app.config.ANTHROPIC_API_KEY", ""):
            result = await generate_summary("Текст транскрипта")

        assert result["tldr"] == "Резюме"
        mock_provider.generate.assert_called_once()

    @pytest.mark.asyncio
    async def test_fallback_on_first_provider_error(self):
        """Первый провайдер падает — переключаемся на Gemini."""
        mock_groq = MagicMock()
        mock_groq.name = "groq"
        mock_groq.generate = AsyncMock(side_effect=Exception("429 Too Many Requests"))

        mock_gemini = MagicMock()
        mock_gemini.name = "gemini"
        mock_gemini.generate = AsyncMock(return_value=json.dumps({
            "tldr": "Резюме от Gemini", "tasks": [], "topics": [], "insights": []
        }))

        with patch("app.services.summarizer.get_provider_for_text", return_value=mock_groq), \
             patch("app.services.summarizer.make_provider_by_name", return_value=mock_gemini), \
             patch("app.config.GOOGLE_AI_API_KEY", "test-key"), \
             patch("app.config.ANTHROPIC_API_KEY", ""):
            result = await generate_summary("Текст транскрипта")

        assert result["tldr"] == "Резюме от Gemini"

    @pytest.mark.asyncio
    async def test_all_providers_fail(self):
        """Все провайдеры падают — возвращаем empty_summary."""
        mock_provider = MagicMock()
        mock_provider.name = "groq"
        mock_provider.generate = AsyncMock(side_effect=Exception("Error"))

        mock_gemini = MagicMock()
        mock_gemini.name = "gemini"
        mock_gemini.generate = AsyncMock(side_effect=Exception("Error"))

        def make_by_name(name):
            if name == "gemini":
                return mock_gemini
            return mock_provider

        with patch("app.services.summarizer.get_provider_for_text", return_value=mock_provider), \
             patch("app.services.summarizer.make_provider_by_name", side_effect=make_by_name), \
             patch("app.config.GOOGLE_AI_API_KEY", "test-key"), \
             patch("app.config.ANTHROPIC_API_KEY", ""):
            result = await generate_summary("Текст транскрипта")

        assert result["tldr"] == ""
        assert result["tasks"] == []

    @pytest.mark.asyncio
    async def test_fallback_on_empty_tldr(self):
        """Первый провайдер вернул пустой tldr — пробуем следующий."""
        mock_groq = MagicMock()
        mock_groq.name = "groq"
        mock_groq.generate = AsyncMock(return_value=json.dumps({
            "tldr": "", "tasks": [], "topics": [], "insights": []
        }))

        mock_gemini = MagicMock()
        mock_gemini.name = "gemini"
        mock_gemini.generate = AsyncMock(return_value=json.dumps({
            "tldr": "Нормальное резюме", "tasks": [], "topics": [], "insights": []
        }))

        with patch("app.services.summarizer.get_provider_for_text", return_value=mock_groq), \
             patch("app.services.summarizer.make_provider_by_name", return_value=mock_gemini), \
             patch("app.config.GOOGLE_AI_API_KEY", "test-key"), \
             patch("app.config.ANTHROPIC_API_KEY", ""):
            result = await generate_summary("Текст транскрипта")

        assert result["tldr"] == "Нормальное резюме"

    @pytest.mark.asyncio
    async def test_explicit_provider_no_fallback(self):
        """Явно указанный провайдер — fallback не добавляется."""
        mock_provider = MagicMock()
        mock_provider.name = "claude"
        mock_provider.generate = AsyncMock(side_effect=Exception("529 Overloaded"))

        with patch("app.services.summarizer.make_provider_by_name", return_value=mock_provider), \
             patch("app.config.GOOGLE_AI_API_KEY", "test-key"), \
             patch("app.config.ANTHROPIC_API_KEY", "test-key"):
            result = await generate_summary("Текст", provider_name="claude")

        assert result["tldr"] == ""
        # Должен вызываться только один раз — без fallback
        mock_provider.generate.assert_called_once()
