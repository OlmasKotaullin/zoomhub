"""Буквица транскрипция-провайдер — Telegram-бот через Telethon userbot.

Логика перенесена из transcriber.py без изменений.
"""

import asyncio
import logging
import re
import tempfile
from pathlib import Path
from datetime import datetime, timezone

from telethon import TelegramClient

from app.config import TELEGRAM_API_ID, TELEGRAM_API_HASH, BUKVITSA_BOT_USERNAME, BASE_DIR, DATA_DIR
from app.services.providers.base import TranscriptionProvider

logger = logging.getLogger(__name__)

RESPONSE_TIMEOUT = 900  # 15 минут
POLL_INTERVAL = 5  # секунд между проверками
# Session in data dir (persistent volume on server)
_session_in_data = DATA_DIR / "zoomhub"
_session_in_base = BASE_DIR / "zoomhub"


def _get_session_path() -> str:
    """Динамически определяет путь к сессии (проверяет при каждом вызове)."""
    if _session_in_data.with_suffix(".session").exists():
        return str(_session_in_data)
    return str(_session_in_base)


SESSION_PATH = _get_session_path()

DONE_MARKERS = ["обработан", "расшифровка:", "создано в буквица"]
PROGRESS_MARKERS = [
    "проверяем", "обрабатыва", "⏳", "подождите", "загружа",
    "начали работу", "материал принят", "придет расшифровка",
    "в течение пары минут",
]

# Singleton Telethon-клиент (серверная сессия — только для fallback/admin)
_client: TelegramClient | None = None
_client_lock = asyncio.Lock()
_transcribe_lock = asyncio.Lock()

# Per-user клиенты: user_id → TelegramClient
_user_clients: dict[int, TelegramClient] = {}
_user_client_locks: dict[int, asyncio.Lock] = {}

# Ожидающие подтверждения кода: user_id → (client, phone)
_pending_auth: dict[int, tuple] = {}


async def _get_user_client(user_id: int, session_string: str, api_id: int, api_hash: str) -> TelegramClient:
    """Возвращает Telethon-клиент для конкретного пользователя (кэшируется)."""
    from telethon.sessions import StringSession

    if user_id not in _user_client_locks:
        _user_client_locks[user_id] = asyncio.Lock()

    async with _user_client_locks[user_id]:
        client = _user_clients.get(user_id)
        if client and client.is_connected():
            return client

        client = TelegramClient(StringSession(session_string), api_id, api_hash)
        await client.connect()

        if not await client.is_user_authorized():
            raise RuntimeError("Telegram-сессия пользователя не авторизована. Переподключите в Настройках.")

        _user_clients[user_id] = client
        logger.info(f"Telethon клиент подключён для user_id={user_id}")
        return client


async def _get_client() -> TelegramClient:
    global _client

    async with _client_lock:
        if _client and _client.is_connected():
            return _client

        if not TELEGRAM_API_ID or not TELEGRAM_API_HASH or not BUKVITSA_BOT_USERNAME:
            raise RuntimeError(
                "Telegram не настроен. Заполните TELEGRAM_API_ID, TELEGRAM_API_HASH, "
                "BUKVITSA_BOT_USERNAME в .env и запустите: python setup_telegram.py"
            )

        session_path = _get_session_path()
        session_file = Path(f"{session_path}.session")
        if not session_file.exists():
            raise RuntimeError("Telegram-сессия не найдена. Запустите: python setup_telegram.py")

        for attempt in range(3):
            try:
                client = TelegramClient(session_path, TELEGRAM_API_ID, TELEGRAM_API_HASH)
                await client.connect()
                break
            except Exception as e:
                if attempt < 2:
                    logger.warning(f"Telethon connect attempt {attempt + 1} failed: {e}, retry...")
                    await asyncio.sleep(3)
                else:
                    raise

        if not await client.is_user_authorized():
            raise RuntimeError("Telegram-сессия не авторизована. Запустите: python setup_telegram.py")

        _client = client
        logger.info("Telethon клиент подключён")
        return _client


class BukvitsaProvider(TranscriptionProvider):
    name = "bukvitsa"

    async def transcribe(self, file_path: str, user_id: int | None = None) -> dict:
        async with _transcribe_lock:
            return await self._transcribe_impl(file_path, user_id=user_id)

    async def _transcribe_impl(self, file_path: str, user_id: int | None = None) -> dict:
        # Пробуем per-user сессию, иначе серверная
        if user_id:
            from app.database import SessionLocal
            from app.models import User
            db = SessionLocal()
            try:
                u = db.query(User).filter(User.id == user_id).first()
                if u and u.tg_session and u.tg_api_id and u.tg_api_hash:
                    client = await _get_user_client(
                        user_id, u.tg_session, u.tg_api_id, u.tg_api_hash
                    )
                    bot_username = u.tg_bot_username or BUKVITSA_BOT_USERNAME
                    bot_entity = await client.get_entity(bot_username)
                    return await self._do_transcribe(client, bot_entity, file_path)
            finally:
                db.close()

        # Fallback: серверная сессия
        client = await _get_client()
        bot_entity = await client.get_entity(BUKVITSA_BOT_USERNAME)
        return await self._do_transcribe(client, bot_entity, file_path)

    async def _do_transcribe(self, client: "TelegramClient", bot_entity, file_path: str) -> dict:
        """Основная логика отправки файла в Буквицу и получения транскрипта."""
        send_path = await _compress_audio(file_path)
        compressed = send_path != file_path

        last_msg_before = await client.get_messages(bot_entity, limit=1)
        last_id_before = last_msg_before[0].id if last_msg_before else 0

        file_size_mb = Path(send_path).stat().st_size / 1024 / 1024
        logger.info(f"Отправляю файл {Path(send_path).name} ({file_size_mb:.1f} МБ)")

        upload_progress = {"last_pct": 0}

        def on_progress(sent, total):
            pct = int(sent * 100 / total)
            if pct - upload_progress["last_pct"] >= 20:
                upload_progress["last_pct"] = pct
                logger.info(f"Загрузка: {pct}% ({sent // 1024 // 1024}/{total // 1024 // 1024} МБ)")

        upload_timeout = max(300, int(file_size_mb * 30 + 180))
        try:
            await asyncio.wait_for(
                client.send_file(bot_entity, send_path, progress_callback=on_progress),
                timeout=upload_timeout,
            )
        except asyncio.TimeoutError:
            raise TimeoutError(f"Не удалось загрузить файл ({file_size_mb:.0f} МБ) за {upload_timeout}с")

        logger.info("Файл отправлен, жду ответ Буквицы (polling)...")

        doc_msg_id = None
        done_text = None

        for attempt in range(RESPONSE_TIMEOUT // POLL_INTERVAL):
            await asyncio.sleep(POLL_INTERVAL)

            messages = await client.get_messages(bot_entity, limit=10)
            new_msgs = [m for m in messages if m.id > last_id_before and not m.out]

            for msg in new_msgs:
                if msg.document and not doc_msg_id:
                    doc_msg_id = msg.id
                    logger.info(f"Polling: найден документ [{msg.id}]")
                text = msg.text or ""
                if text and any(marker in text.lower() for marker in DONE_MARKERS):
                    done_text = text
                    logger.info(f"Polling: найден ответ [{msg.id}]")

            if doc_msg_id and done_text:
                break
            if done_text and len(done_text) > 300 and "расшифровка:" in done_text.lower():
                break
            if done_text and not doc_msg_id:
                logger.info("Текст найден, жду документ ещё 20с...")
                for _ in range(4):
                    await asyncio.sleep(5)
                    msgs2 = await client.get_messages(bot_entity, limit=5)
                    for m2 in msgs2:
                        if m2.id > last_id_before and not m2.out and m2.document:
                            doc_msg_id = m2.id
                            break
                    if doc_msg_id:
                        break
                break

            if attempt % 12 == 0 and attempt > 0:
                logger.info(f"Ожидание... {attempt * POLL_INTERVAL}с")

        if doc_msg_id:
            try:
                doc_msgs = await client.get_messages(bot_entity, ids=[doc_msg_id])
                if doc_msgs and doc_msgs[0] and doc_msgs[0].document:
                    tmp_dir = Path(tempfile.mkdtemp())
                    downloaded = await client.download_media(doc_msgs[0], str(tmp_dir))
                    if downloaded and Path(downloaded).exists():
                        file_text = Path(downloaded).read_text(encoding='utf-8', errors='replace')
                        Path(downloaded).unlink(missing_ok=True)
                        if len(file_text) > 50:
                            self._cleanup_compressed(send_path, compressed)
                            return parse_response(file_text)
            except Exception as e:
                logger.error(f"Ошибка скачивания документа: {e}")

        if done_text:
            parsed = parse_response(done_text)
            self._cleanup_compressed(send_path, compressed)
            return parsed

        raise TimeoutError(f"Буквица не ответила за {RESPONSE_TIMEOUT} секунд")

    @staticmethod
    def _cleanup_compressed(send_path: str, was_compressed: bool):
        """Удаляет временный сжатый файл после успешной отправки."""
        if was_compressed:
            try:
                Path(send_path).unlink(missing_ok=True)
                logger.info(f"Удалён временный файл: {Path(send_path).name}")
            except Exception as e:
                logger.warning(f"Не удалось удалить {send_path}: {e}")

    async def health_check(self) -> bool:
        try:
            if not TELEGRAM_API_ID or not TELEGRAM_API_HASH or not BUKVITSA_BOT_USERNAME:
                return False
            session_file = Path(f"{SESSION_PATH}.session")
            return session_file.exists()
        except Exception:
            return False


async def _compress_audio(file_path: str) -> str:
    """Сжимает аудио в opus моно 16kHz 24kbps для быстрой загрузки."""
    import subprocess

    src = Path(file_path)
    compressed = src.parent / f"{src.stem}_compressed.opus"

    if compressed.exists():
        return str(compressed)

    src_size_mb = src.stat().st_size / 1024 / 1024
    if src_size_mb < 5:
        return file_path

    logger.info(f"Сжимаю {src.name} ({src_size_mb:.1f} МБ) → opus 24kbps mono...")

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-i", file_path,
        "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "24k",
        "-y", str(compressed),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    await proc.wait()

    if proc.returncode != 0 or not compressed.exists():
        logger.warning("ffmpeg сжатие не удалось, отправляю оригинал")
        return file_path

    new_size_mb = compressed.stat().st_size / 1024 / 1024
    logger.info(f"Сжато: {src_size_mb:.1f} МБ → {new_size_mb:.1f} МБ ({int(new_size_mb / src_size_mb * 100)}%)")
    return str(compressed)


# --- Парсинг ответов Буквицы (перенесено из transcriber.py) ---

def parse_response(text: str) -> dict:
    if not text or not text.strip():
        return {"full_text": "", "segments": []}

    gdoc_match = re.search(r'(https?://docs\.google\.com/document/d/[\w-]+[^\s]*)', text)
    if gdoc_match and len(text) < 500:
        return {
            "full_text": text.strip(),
            "segments": [],
            "google_doc_url": gdoc_match.group(1),
        }

    transcript_text = _extract_transcript_section(text)
    segments = _parse_segments(transcript_text)
    full_text = "\n".join(seg["text"] for seg in segments) if segments else transcript_text

    return {"full_text": full_text.strip(), "segments": segments}


def _extract_transcript_section(text: str) -> str:
    pattern = re.compile(r'расшифровка\s*:', re.IGNORECASE)
    match = pattern.search(text)

    if match:
        after_header = text[match.end():].strip()
        section_markers = [
            r'\n\s*анализ\s*:', r'\n\s*итоги\s*:', r'\n\s*задачи\s*:',
            r'\n\s*резюме\s*:', r'\n\s*ключевые\s+',
            r'\n\s*создано в буквица', r'\[создано в буквица',
        ]
        for marker in section_markers:
            section_match = re.search(marker, after_header, re.IGNORECASE)
            if section_match:
                after_header = after_header[:section_match.start()]
        return _strip_service_lines(after_header)

    return _strip_service_lines(text)


def _strip_service_lines(text: str) -> str:
    service_patterns = [
        "обработан", "✅", "👏", "⏳", "обрабатыва",
        "создано в буквица", "bukvitsaai_bot", "t.me/bukvitsa",
    ]
    lines = text.strip().split("\n")
    content_lines = []
    for line in lines:
        stripped = line.strip().strip('`').strip()
        if len(stripped) < 200 and any(m in stripped.lower() for m in service_patterns):
            continue
        if stripped:
            content_lines.append(stripped)
    return "\n".join(content_lines)


def _parse_segments(text: str) -> list[dict]:
    if not text.strip():
        return []

    lines = text.strip().split("\n")
    segments = []
    current_time = 0.0

    for line in lines:
        line = line.strip()
        if not line:
            continue

        timestamp = None
        speaker = ""
        content = line

        time_match = re.match(r'[\[\(](\d{1,2}:\d{2}(?::\d{2})?)\s*[\]\)]\s*(.*)', line)
        if time_match:
            timestamp = _parse_time(time_match.group(1))
            content = time_match.group(2).strip()

        speaker_match = re.match(r'^([А-Яа-яA-Za-z\s\d]+?):\s+(.*)', content)
        if speaker_match and len(speaker_match.group(1)) < 30:
            speaker = speaker_match.group(1).strip()
            content = speaker_match.group(2).strip()

        if timestamp is not None:
            current_time = timestamp

        if content:
            segments.append({
                "start": current_time,
                "end": current_time + 30.0,
                "speaker": speaker,
                "text": content,
            })

    return segments


def _parse_time(time_str: str) -> float:
    parts = time_str.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
    except (ValueError, IndexError):
        pass
    return 0.0
