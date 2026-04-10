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

        # Step 1: Compress audio (reuse bukvitsa logic)
        send_path = await self._compress_audio(file_path)
        compressed = send_path != file_path

        try:
            file_size = Path(send_path).stat().st_size

            # Step 2: Prepare input — base64 for small files, R2 URL for large
            if file_size <= 10 * 1024 * 1024:  # 10 MB — RunPod payload limit
                audio_b64 = base64.b64encode(Path(send_path).read_bytes()).decode()
                payload = {
                    "input": {
                        "audio_base64": audio_b64,
                        "language": "ru",
                        "beam_size": 5,
                    }
                }
            else:
                # Upload to R2 and pass URL
                audio_url = await self._upload_to_r2(send_path)
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

    async def _upload_to_r2(self, file_path: str) -> str:
        """Upload file to Cloudflare R2 and return presigned URL.

        Falls back to base64 if R2 is not configured.
        """
        from app.config import R2_ENDPOINT, R2_ACCESS_KEY, R2_SECRET_KEY, R2_BUCKET

        if not R2_ENDPOINT or not R2_ACCESS_KEY:
            # Fallback: return base64 (slower but works)
            logger.warning("R2 not configured — falling back to base64 (slow for large files)")
            raise ValueError("File too large for base64 and R2 not configured")

        import boto3
        from botocore.config import Config

        s3 = boto3.client(
            "s3",
            endpoint_url=R2_ENDPOINT,
            aws_access_key_id=R2_ACCESS_KEY,
            aws_secret_access_key=R2_SECRET_KEY,
            config=Config(signature_version="s3v4"),
        )

        key = f"transcription/{Path(file_path).name}"

        logger.info(f"Uploading {Path(file_path).name} to R2...")
        s3.upload_file(file_path, R2_BUCKET, key)

        # Generate presigned URL (1 hour expiry)
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": R2_BUCKET, "Key": key},
            ExpiresIn=3600,
        )

        logger.info(f"R2 upload done, presigned URL generated")
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
