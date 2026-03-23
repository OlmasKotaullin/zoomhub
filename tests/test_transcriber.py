"""Тесты парсера ответа Буквицы."""

from app.services.transcriber import parse_response, _extract_transcript_section, _parse_time


class TestParseResponse:
    """Тесты основной функции parse_response."""

    def test_real_bukvitsa_format(self):
        """Реальный формат ответа Буквицы."""
        text = (
            "Ваш материал обработан ✅👏\n"
            "\n"
            "Расшифровка:\n"
            "Я хотел бы знать, как мне автоматизировать мой процесс. "
            "Но не знаю дальше, как это всё реализовывать. "
            "Подскажи мне правильный ответ."
        )
        result = parse_response(text)

        assert "автоматизировать" in result["full_text"]
        assert "обработан" not in result["full_text"]
        assert "✅" not in result["full_text"]
        assert len(result["segments"]) >= 1

    def test_multiline_transcript(self):
        """Многострочный транскрипт."""
        text = (
            "Ваш материал обработан ✅👏\n"
            "\n"
            "Расшифровка:\n"
            "Первая строка транскрипта.\n"
            "Вторая строка транскрипта.\n"
            "Третья строка транскрипта."
        )
        result = parse_response(text)

        assert "Первая строка" in result["full_text"]
        assert "Третья строка" in result["full_text"]
        assert len(result["segments"]) == 3

    def test_with_timestamps(self):
        """Транскрипт с таймкодами."""
        text = (
            "Ваш материал обработан ✅\n"
            "\n"
            "Расшифровка:\n"
            "[00:00:05] Привет всем\n"
            "[00:01:30] Давайте начнём\n"
            "[00:05:00] Итого, план такой"
        )
        result = parse_response(text)

        assert len(result["segments"]) == 3
        assert result["segments"][0]["start"] == 5.0
        assert result["segments"][0]["text"] == "Привет всем"
        assert result["segments"][1]["start"] == 90.0
        assert result["segments"][2]["start"] == 300.0

    def test_with_speakers(self):
        """Транскрипт со спикерами."""
        text = (
            "Расшифровка:\n"
            "Алмаз: Давайте обсудим план\n"
            "Дмитрий: Согласен, начнём"
        )
        result = parse_response(text)

        assert result["segments"][0]["speaker"] == "Алмаз"
        assert result["segments"][0]["text"] == "Давайте обсудим план"
        assert result["segments"][1]["speaker"] == "Дмитрий"

    def test_with_analysis_section(self):
        """Транскрипт + секция анализа (которую надо отрезать)."""
        text = (
            "Ваш материал обработан ✅\n"
            "\n"
            "Расшифровка:\n"
            "Обсудили план на квартал.\n"
            "\n"
            "Анализ:\n"
            "Этот текст не должен попасть в транскрипт."
        )
        result = parse_response(text)

        assert "Обсудили план" in result["full_text"]
        assert "не должен попасть" not in result["full_text"]

    def test_empty_response(self):
        result = parse_response("")
        assert result["full_text"] == ""
        assert result["segments"] == []

    def test_no_header_fallback(self):
        """Если нет маркера 'Расшифровка:' — берём весь текст без служебных строк."""
        text = (
            "Ваш материал обработан ✅👏\n"
            "\n"
            "Просто текст без заголовка расшифровки."
        )
        result = parse_response(text)

        assert "Просто текст" in result["full_text"]
        assert "обработан" not in result["full_text"]


class TestExtractTranscriptSection:

    def test_extract_after_header(self):
        text = "Бла бла\n\nРасшифровка:\nТранскрипт текст"
        result = _extract_transcript_section(text)
        assert result == "Транскрипт текст"

    def test_case_insensitive(self):
        text = "расшифровка: Текст транскрипта"
        result = _extract_transcript_section(text)
        assert result == "Текст транскрипта"

    def test_cuts_analysis_section(self):
        text = "Расшифровка:\nТранскрипт\n\nАнализ:\nНе нужен"
        result = _extract_transcript_section(text)
        assert "Транскрипт" in result
        assert "Не нужен" not in result


class TestParseTime:

    def test_mmss(self):
        assert _parse_time("01:30") == 90.0

    def test_hhmmss(self):
        assert _parse_time("01:30:00") == 5400.0

    def test_zero(self):
        assert _parse_time("00:00") == 0.0

    def test_invalid(self):
        assert _parse_time("abc") == 0.0
