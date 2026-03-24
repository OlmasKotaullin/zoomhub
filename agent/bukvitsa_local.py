"""Локальная транскрипция через Буквицу — работает на компьютере пользователя.

Telegram-сессия хранится ЛОКАЛЬНО (~/.zoomhub/zoomhub.session), никогда не уходит на сервер.
"""

import asyncio
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

from telethon import TelegramClient

logger = logging.getLogger(__name__)

RESPONSE_TIMEOUT = 900  # 15 минут
POLL_INTERVAL = 5

DONE_MARKERS = ["обработан", "расшифровка:", "создано в буквица"]
PROGRESS_MARKERS = [
    "проверяем", "обрабатыва", "подождите", "загружа",
    "начали работу", "материал принят", "придет расшифровка",
]


def get_config_dir() -> Path:
    d = Path.home() / ".zoomhub"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_session_path() -> str:
    return str(get_config_dir() / "zoomhub")


async def get_client(api_id: int, api_hash: str) -> TelegramClient:
    session_path = get_session_path()
    session_file = Path(f"{session_path}.session")
    if not session_file.exists():
        raise RuntimeError(
            "Telegram-сессия не найдена. Запустите агент с --setup для настройки."
        )

    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()

    if not await client.is_user_authorized():
        raise RuntimeError("Telegram-сессия не авторизована. Запустите --setup.")

    return client


async def transcribe(file_path: str, api_id: int, api_hash: str,
                     bot_username: str) -> dict:
    """Отправляет аудио в Буквицу и возвращает транскрипт."""
    client = await get_client(api_id, api_hash)

    try:
        bot = await client.get_entity(bot_username)

        # Сжатие
        send_path = await compress_audio(file_path)
        compressed = send_path != file_path

        # Запоминаем последнее сообщение
        last_msgs = await client.get_messages(bot, limit=1)
        last_id = last_msgs[0].id if last_msgs else 0

        # Отправка
        file_size_mb = Path(send_path).stat().st_size / 1024 / 1024
        print(f"  📤 Отправляю в Буквицу ({file_size_mb:.1f} МБ)...", flush=True)

        upload_timeout = max(300, int(file_size_mb * 30 + 180))
        await asyncio.wait_for(
            client.send_file(bot, send_path),
            timeout=upload_timeout,
        )
        print("  ⏳ Жду расшифровку от Буквицы...", flush=True)

        # Polling ответа
        doc_msg_id = None
        done_text = None

        for attempt in range(RESPONSE_TIMEOUT // POLL_INTERVAL):
            await asyncio.sleep(POLL_INTERVAL)

            messages = await client.get_messages(bot, limit=10)
            new_msgs = [m for m in messages if m.id > last_id and not m.out]

            for msg in new_msgs:
                if msg.document and not doc_msg_id:
                    doc_msg_id = msg.id

                text = msg.text or ""
                if text and any(marker in text.lower() for marker in DONE_MARKERS):
                    done_text = text

            if doc_msg_id and done_text:
                break
            if done_text and len(done_text) > 300 and "расшифровка:" in done_text.lower():
                break
            if done_text and not doc_msg_id:
                # Ждём документ ещё 20с
                for _ in range(4):
                    await asyncio.sleep(5)
                    msgs2 = await client.get_messages(bot, limit=5)
                    for m2 in msgs2:
                        if m2.id > last_id and not m2.out and m2.document:
                            doc_msg_id = m2.id
                            break
                    if doc_msg_id:
                        break
                break

            if attempt % 12 == 0 and attempt > 0:
                elapsed = attempt * POLL_INTERVAL
                print(f"  ⏳ Ожидание... {elapsed}с", flush=True)

        # Скачать документ
        if doc_msg_id:
            try:
                doc_msgs = await client.get_messages(bot, ids=[doc_msg_id])
                if doc_msgs and doc_msgs[0] and doc_msgs[0].document:
                    tmp_dir = Path(tempfile.mkdtemp())
                    downloaded = await client.download_media(doc_msgs[0], str(tmp_dir))
                    if downloaded and Path(downloaded).exists():
                        file_text = Path(downloaded).read_text(encoding='utf-8', errors='replace')
                        Path(downloaded).unlink(missing_ok=True)
                        if len(file_text) > 50:
                            _cleanup(send_path, compressed)
                            return parse_response(file_text)
            except Exception as e:
                logger.warning(f"Ошибка скачивания документа: {e}")

        # Fallback — текст
        if done_text:
            _cleanup(send_path, compressed)
            return parse_response(done_text)

        raise TimeoutError(f"Буквица не ответила за {RESPONSE_TIMEOUT} секунд")

    finally:
        await client.disconnect()


def _cleanup(path: str, was_compressed: bool):
    if was_compressed:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass


async def compress_audio(file_path: str) -> str:
    src = Path(file_path)
    if src.stat().st_size / 1024 / 1024 < 5:
        return file_path

    compressed = src.parent / f"{src.stem}_compressed.opus"
    if compressed.exists():
        return str(compressed)

    print(f"  🗜 Сжимаю аудио...", flush=True)
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-i", file_path,
        "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "24k",
        "-y", str(compressed),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    await proc.wait()

    if proc.returncode != 0 or not compressed.exists():
        return file_path
    return str(compressed)


async def setup_session(api_id: int, api_hash: str, bot_username: str):
    """Интерактивная настройка Telegram-сессии."""
    session_path = get_session_path()
    client = TelegramClient(session_path, api_id, api_hash)
    await client.start()
    print("✅ Telegram-сессия создана!", flush=True)

    # Проверяем Буквицу
    try:
        bot = await client.get_entity(bot_username)
        print(f"✅ Бот @{bot_username} найден", flush=True)
    except Exception:
        print(f"⚠️  Бот @{bot_username} не найден. Убедитесь что вы подписаны на него.", flush=True)

    await client.disconnect()


# --- Парсинг ---

def parse_response(text: str) -> dict:
    if not text or not text.strip():
        return {"full_text": "", "segments": []}

    transcript_text = _extract_transcript_section(text)
    segments = _parse_segments(transcript_text)
    full_text = "\n".join(seg["text"] for seg in segments) if segments else transcript_text
    return {"full_text": full_text.strip(), "segments": segments}


def _extract_transcript_section(text: str) -> str:
    pattern = re.compile(r'расшифровка\s*:', re.IGNORECASE)
    match = pattern.search(text)
    if match:
        after = text[match.end():].strip()
        markers = [
            r'\n\s*анализ\s*:', r'\n\s*итоги\s*:', r'\n\s*задачи\s*:',
            r'\n\s*резюме\s*:', r'\n\s*ключевые\s+',
            r'\n\s*создано в буквица', r'\[создано в буквица',
        ]
        for m in markers:
            sm = re.search(m, after, re.IGNORECASE)
            if sm:
                after = after[:sm.start()]
        return _strip_service(after)
    return _strip_service(text)


def _strip_service(text: str) -> str:
    bad = ["обработан", "✅", "👏", "⏳", "обрабатыва", "создано в буквица", "bukvitsaai_bot", "t.me/bukvitsa"]
    lines = text.strip().split("\n")
    return "\n".join(
        l.strip().strip('`').strip()
        for l in lines
        if l.strip() and not (len(l.strip()) < 200 and any(m in l.strip().lower() for m in bad))
    )


def _parse_segments(text: str) -> list:
    if not text.strip():
        return []
    segments = []
    current_time = 0.0
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        timestamp, speaker, content = None, "", line
        tm = re.match(r'[\[\(](\d{1,2}:\d{2}(?::\d{2})?)\s*[\]\)]\s*(.*)', line)
        if tm:
            timestamp = _parse_time(tm.group(1))
            content = tm.group(2).strip()
        sm = re.match(r'^([А-Яа-яA-Za-z\s\d]+?):\s+(.*)', content)
        if sm and len(sm.group(1)) < 30:
            speaker = sm.group(1).strip()
            content = sm.group(2).strip()
        if timestamp is not None:
            current_time = timestamp
        if content:
            segments.append({"start": current_time, "end": current_time + 30.0, "speaker": speaker, "text": content})
    return segments


def _parse_time(ts: str) -> float:
    parts = ts.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
    except (ValueError, IndexError):
        pass
    return 0.0
