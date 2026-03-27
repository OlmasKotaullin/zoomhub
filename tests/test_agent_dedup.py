"""Тесты дедупликации Zoom audio/video файлов в агенте."""

import sys
import os

# Добавляем agent/ в путь для импорта
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'agent'))

from zoomhub_agent_v2 import _extract_zoom_id


class TestExtractZoomId:
    """Тесты извлечения Zoom meeting ID из имени файла."""

    def test_audio_file(self):
        assert _extract_zoom_id("audio1061998810.m4a") == "1061998810"

    def test_video_file(self):
        assert _extract_zoom_id("video1061998810.mp4") == "1061998810"

    def test_no_zoom_id(self):
        assert _extract_zoom_id("meeting_recording.mp3") is None

    def test_short_number(self):
        """Слишком короткий номер — не Zoom ID."""
        assert _extract_zoom_id("audio123.m4a") is None

    def test_long_number(self):
        assert _extract_zoom_id("audio12345678901234.m4a") == "12345678901234"

    def test_with_path(self):
        assert _extract_zoom_id("2026-03-27/audio1282806098.m4a") == "1282806098"

    def test_random_filename(self):
        assert _extract_zoom_id("синхронизация.mp3") is None
