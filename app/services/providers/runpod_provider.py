"""RunPod Serverless транскрипция — faster-whisper large-v3 на GPU.

Отправляет сжатый аудиофайл через presigned URL (R2/S3) или base64.
Заменяет BukvitsaProvider — без мьютексов, протухающих сессий, лимитов.
"""

import asyncio
import base64
import logging
from pathlib import Path

import httpx

from app.services.providers.base import TranscriptionProvider

logger = logging.getLogger(__name__)

# RunPod Serverless API
RUNPOD_BASE = "https://api.runpod.ai/v2"
POLL_INTERVAL = 2  # seconds
MAX_WAIT = 600  # 10 minutes


class RunPodWhisperProvider(TranscriptionProvider):
    name = "runpod_whisper"

    def __init__(self):
        from app.config import RUNPOD_API_KEY, RUNPOD_ENDPOINT_ID
        self._api_key = RUNPOD_API_KEY
        self._endpoint_id = RUNPOD_ENDPOINT_ID

    @property
    def _headers(self):
        return {"Authorization": f"Bearer {self._api_key}"}

    @property
    def _run_url(self):
        return f"{RUNPOD_BASE}/{self._endpoint_id}/run"

    @property
    def _status_url(self):
        return f"{RUNPOD_BASE}/{self._endpoint_id}/status"

    async def transcribe(self, file_path: str, user_id: int | None = None) -> dict:
        """Transcribe audio via RunPod Serverless endpoint."""

        # Step 1: Compress audio
        send_path = await self._compress_audio(file_path)
        compressed = send_path != file_path

        try:
            file_size = Path(send_path).stat().st_size
            size_mb = file_size / (1024 * 1024)

            # Step 2: Always use URL — serve file via ZoomHub's temp endpoint
            # base64 fails for files >7MB due to RunPod 10MB payload limit
            audio_url = await self._get_serve_url(send_path)
            logger.info(f"RunPod transcription: {size_mb:.1f} MB, URL ready")
            payload = {
                "input": {
                    "audio_url": audio_url,
                    "language": "ru",
                    "beam_size": 5,
                }
            }

            # Step 3: Submit job
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(self._run_url, json=payload, headers=self._headers)
                resp.raise_for_status()
                job = resp.json()

            job_id = job.get("id")
            if not job_id:
                raise RuntimeError(f"RunPod не вернул job id: {job}")

            logger.info(f"RunPod job submitted: {job_id}")

            # Step 4: Poll for result
            result = await self._poll_result(job_id)

            if "error" in result:
                raise RuntimeError(f"RunPod error: {result['error']}")

            full_text = result.get("full_text", "")
            segments = result.get("segments", [])

            logger.info(f"RunPod done: {len(segments)} segments, {len(full_text)} chars")
            return {"full_text": full_text, "segments": segments}

        finally:
            if compressed:
                Path(send_path).unlink(missing_ok=True)

    async def _poll_result(self, job_id: str) -> dict:
        """Poll RunPod status endpoint until job completes."""
        url = f"{self._status_url}/{job_id}"

        async with httpx.AsyncClient(timeout=15) as client:
            for attempt in range(MAX_WAIT // POLL_INTERVAL):
                await asyncio.sleep(POLL_INTERVAL)

                resp = await client.get(url, headers=self._headers)
                resp.raise_for_status()
                data = resp.json()

                status = data.get("status")
                if status == "COMPLETED":
                    return data.get("output", {})
                elif status == "FAILED":
                    return {"error": data.get("error", "Unknown error")}
                elif status in ("IN_QUEUE", "IN_PROGRESS"):
                    if attempt % 15 == 0 and attempt > 0:
                        logger.info(f"RunPod job {job_id}: {status} ({attempt * POLL_INTERVAL}s)")
                else:
                    logger.warning(f"RunPod unexpected status: {status}")

        raise TimeoutError(f"RunPod job {job_id} timed out after {MAX_WAIT}s")

    async def _get_serve_url(self, file_path: str) -> str:
        """Generate a temporary public URL to serve file via ZoomHub.

        Creates a signed token so RunPod can download the file directly
        from our Fly.io server. No external storage needed.
        """
        import hashlib
        import time
        from app.config import SECRET_KEY, APP_URL

        # Create a temp token: sha256(secret + filepath + timestamp)
        # Valid for 1 hour
        ts = str(int(time.time()))
        token = hashlib.sha256(f"{SECRET_KEY}:{file_path}:{ts}".encode()).hexdigest()[:32]

        # Store token -> filepath mapping in a module-level dict (cleaned up after use)
        _temp_file_tokens[token] = {"path": file_path, "ts": int(ts)}

        url = f"{APP_URL}/api/temp-audio/{token}"
        logger.info(f"Serve URL created: {url}")
        return url

    async def _compress_audio(self, file_path: str) -> str:
        """Compress audio to opus mono 16kHz 24kbps for faster transfer."""
        import subprocess

        src = Path(file_path)
        compressed = src.parent / f"{src.stem}_compressed.opus"

        if compressed.exists():
            return str(compressed)

        src_size_mb = src.stat().st_size / 1024 / 1024
        if src_size_mb < 5:
            return file_path

        logger.info(f"Compressing {src.name} ({src_size_mb:.1f} MB) -> opus 24kbps...")

        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", file_path,
            "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "24k",
            "-y", str(compressed),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        await proc.wait()

        if proc.returncode != 0 or not compressed.exists():
            logger.warning("ffmpeg compression failed, sending original")
            return file_path

        new_size_mb = compressed.stat().st_size / 1024 / 1024
        logger.info(f"Compressed: {src_size_mb:.1f} MB -> {new_size_mb:.1f} MB")
        return str(compressed)

    async def health_check(self) -> bool:
        if not self._api_key or not self._endpoint_id:
            return False
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{RUNPOD_BASE}/{self._endpoint_id}/health",
                    headers=self._headers,
                )
                return resp.status_code == 200
        except Exception:
            return False


# Module-level dict for temp file tokens (used by serve_temp_audio endpoint)
_temp_file_tokens: dict[str, dict] = {}
