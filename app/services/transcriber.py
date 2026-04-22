"""Транскрибация аудио — smart routing: Groq (быстро, бесплатно) → RunPod (fallback).

Тонкая обёртка — вся логика в конкретных провайдерах:
  - app/services/providers/bukvitsa_provider.py
  - app/services/providers/whisper_provider.py
  - app/services/providers/runpod_provider.py
"""

import asyncio
import logging
import subprocess
from pathlib import Path

from app.services.providers import get_transcription_provider

# Re-exports для обратной совместимости (используются в routers и тестах)
from app.services.providers.bukvitsa_provider import (
    parse_response,
    _extract_transcript_section,
    _parse_time,
    _get_client,
)

logger = logging.getLogger(__name__)

GROQ_MAX_FILE_SIZE = 25 * 1024 * 1024  # 25 MB — Groq Whisper API limit
GROQ_MAX_DURATION_SEC = 7000           # Groq ASPH limit: 7200s/h, берём с запасом
CHUNK_DURATION_SEC = 5400              # 1.5 часа — длина одного чанка при нарезке


def _get_duration_sec(file_path: str) -> int:
    """Get audio duration via ffprobe. Returns 0 on failure."""
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", file_path],
            capture_output=True, text=True, timeout=15
        )
        if probe.returncode == 0 and probe.stdout.strip():
            return int(float(probe.stdout.strip()))
    except Exception as e:
        logger.warning(f"ffprobe failed for {file_path}: {e}")
    return 0


async def _split_audio_into_chunks(file_path: str, chunk_sec: int = CHUNK_DURATION_SEC) -> list[tuple[str, int]]:
    """Split audio into chunks of chunk_sec seconds each.

    Returns list of (chunk_path, offset_sec) tuples.
    Offset needed to shift timecodes when merging segments.
    """
    duration = _get_duration_sec(file_path)
    if duration <= chunk_sec:
        return [(file_path, 0)]

    src = Path(file_path)
    chunks: list[tuple[str, int]] = []
    offset = 0
    index = 0

    while offset < duration:
        chunk_path = src.parent / f"{src.stem}_chunk{index:02d}.opus"
        logger.info(f"Splitting chunk {index} at offset {offset}s (duration {chunk_sec}s)...")

        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-ss", str(offset), "-t", str(chunk_sec),
            "-i", file_path,
            "-ac", "1", "-ar", "16000",
            "-c:a", "libopus", "-b:a", "24k",
            "-y", str(chunk_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=600)
        except asyncio.TimeoutError:
            proc.kill()
            logger.error(f"ffmpeg chunk split timeout at offset {offset}s")
            break

        if proc.returncode != 0 or not chunk_path.exists():
            logger.warning(f"Chunk split failed at offset {offset}s (rc={proc.returncode})")
            break

        chunks.append((str(chunk_path), offset))
        offset += chunk_sec
        index += 1

    return chunks


def _cleanup_chunks(chunks: list[tuple[str, int]], original: str):
    """Delete temporary chunk files (keep original)."""
    for chunk_path, _ in chunks:
        if chunk_path != original:
            try:
                Path(chunk_path).unlink(missing_ok=True)
            except Exception as e:
                logger.warning(f"Could not delete chunk {chunk_path}: {e}")


def _merge_chunk_results(results: list[tuple[dict, int]]) -> dict:
    """Merge chunk transcription results into one, shifting timecodes by offset."""
    full_text_parts = []
    all_segments = []
    total_duration = 0

    for result, offset in results:
        if not result:
            continue
        text = result.get("full_text", "")
        if text:
            full_text_parts.append(text)
        for seg in result.get("segments", []):
            all_segments.append({
                "start": seg.get("start", 0) + offset,
                "end": seg.get("end", 0) + offset,
                "text": seg.get("text", ""),
            })
        total_duration = max(total_duration, offset + result.get("duration_seconds", 0))

    return {
        "full_text": " ".join(full_text_parts).strip(),
        "segments": all_segments,
        "duration_seconds": total_duration,
    }


async def _transcribe_via_groq(file_path: str) -> dict | None:
    """Try transcription via Groq Whisper API. Returns None on failure.

    For files longer than Groq's ASPH limit (~2h), splits into chunks automatically.
    """
    from app.config import GROQ_API_KEY
    if not GROQ_API_KEY:
        return None

    file_size = Path(file_path).stat().st_size
    if file_size > GROQ_MAX_FILE_SIZE:
        logger.info(f"File too large for Groq ({file_size / 1024 / 1024:.1f} MB > 25 MB)")
        return None

    duration = _get_duration_sec(file_path)
    if duration > GROQ_MAX_DURATION_SEC:
        logger.info(f"Audio too long for single Groq call ({duration}s > {GROQ_MAX_DURATION_SEC}s), splitting...")
        return await _transcribe_via_groq_chunked(file_path)

    return await _groq_call_single(file_path)


async def _groq_call_single(file_path: str) -> dict | None:
    """One Groq API call for a file that fits within limits."""
    from app.config import GROQ_API_KEY
    try:
        import httpx
        size_mb = Path(file_path).stat().st_size / 1024 / 1024
        logger.info(f"Транскрипция через Groq Whisper: {size_mb:.1f} MB ({Path(file_path).name})")

        async with httpx.AsyncClient(timeout=600) as client:
            with open(file_path, "rb") as f:
                resp = await client.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                    files={"file": (Path(file_path).name, f, "audio/ogg")},
                    data={
                        "model": "whisper-large-v3",
                        "language": "ru",
                        "response_format": "verbose_json",
                    },
                )

            if resp.status_code != 200:
                logger.warning(f"Groq Whisper error {resp.status_code}: {resp.text[:200]}")
                return None

            data = resp.json()
            full_text = data.get("text", "")
            segments = [{"start": s.get("start", 0), "end": s.get("end", 0), "text": s.get("text", "")}
                        for s in data.get("segments", [])]
            duration = data.get("duration", 0)
            logger.info(f"Groq Whisper done: {len(segments)} segments, {len(full_text)} chars, {duration:.0f}s")
            result = {"full_text": full_text, "segments": segments}
            if duration:
                result["duration_seconds"] = int(duration)
            return result

    except Exception as e:
        logger.warning(f"Groq Whisper failed: {e}")
        return None


async def _transcribe_via_groq_chunked(file_path: str) -> dict | None:
    """Split long audio into chunks and transcribe each via Groq sequentially."""
    chunks = await _split_audio_into_chunks(file_path)
    if not chunks:
        logger.warning("Failed to split audio into chunks")
        return None

    logger.info(f"Split into {len(chunks)} chunks, transcribing sequentially via Groq...")
    results: list[tuple[dict, int]] = []

    try:
        for chunk_path, offset in chunks:
            result = await _groq_call_single(chunk_path)
            if not result or not result.get("full_text"):
                logger.warning(f"Chunk at offset {offset}s failed — aborting chunked Groq path")
                return None
            results.append((result, offset))

        merged = _merge_chunk_results(results)
        logger.info(f"Chunked Groq done: {len(merged['segments'])} segments total, "
                    f"{len(merged['full_text'])} chars, {merged['duration_seconds']}s")
        return merged
    finally:
        _cleanup_chunks(chunks, file_path)


async def transcribe_file(file_path: str, user_id: int | None = None) -> dict:
    """Транскрибирует файл: Groq (быстрый, ≤25 MB, ≤2ч или с нарезкой) → RunPod (GPU, любой размер).

    Pipeline сжимает аудио до ~11 MB перед вызовом, поэтому Groq подходит
    для большинства файлов. Файлы длиннее 2 часов автоматически режутся на куски.
    RunPod — fallback для файлов >25 MB или при ошибках Groq.

    user_id: если передан, провайдер использует личную Telegram-сессию пользователя.

    Returns:
        {"full_text": str, "segments": list[dict]}
    """
    file_size = Path(file_path).stat().st_size

    # Primary: Groq Whisper — мгновенный, нет cold start (если файл ≤25 MB)
    if file_size <= GROQ_MAX_FILE_SIZE:
        result = await _transcribe_via_groq(file_path)
        if result and result.get("full_text"):
            return result
        logger.warning("Groq не справился, пробую RunPod...")

    # Fallback: RunPod — наш GPU сервер (любой размер, но cold start 1-5 мин)
    provider = get_transcription_provider()
    try:
        logger.info(f"Транскрипция через {provider.name}: {file_path}")
        result = await provider.transcribe(file_path, user_id=user_id)
        if result and result.get("full_text"):
            return result
        logger.warning(f"{provider.name} вернул пустой результат")
    except Exception as e:
        logger.warning(f"{provider.name} ошибка: {e}")

    # Последняя попытка: Groq (если ещё не пробовали — файл был >25 MB)
    if file_size > GROQ_MAX_FILE_SIZE:
        result = await _transcribe_via_groq(file_path)
        if result and result.get("full_text"):
            return result

    raise RuntimeError(f"Все провайдеры транскрипции не справились: {file_path}")
