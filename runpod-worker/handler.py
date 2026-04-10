"""RunPod Serverless handler for faster-whisper transcription.

Accepts audio via URL (presigned R2/S3 link) or base64.
Returns full_text + timestamped segments.
"""

import base64
import os
import tempfile
import logging

import runpod
from faster_whisper import WhisperModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("whisper-worker")

# Load model once at cold start
MODEL_SIZE = os.environ.get("WHISPER_MODEL", "large-v3-turbo")
DEVICE = os.environ.get("WHISPER_DEVICE", "cuda")
COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE", "float16")

logger.info(f"Loading faster-whisper {MODEL_SIZE} on {DEVICE} ({COMPUTE_TYPE})...")
model = WhisperModel(MODEL_SIZE, device=DEVICE, compute_type=COMPUTE_TYPE)
logger.info("Model loaded.")


def _download_url(url: str, tmp_dir: str) -> str:
    """Download audio from URL to temp file."""
    import httpx

    resp = httpx.get(url, follow_redirects=True, timeout=300)
    resp.raise_for_status()

    # Determine extension from content-type or URL
    ext = ".opus"
    ct = resp.headers.get("content-type", "")
    if "mp4" in ct or "m4a" in ct:
        ext = ".m4a"
    elif "mp3" in ct or "mpeg" in ct:
        ext = ".mp3"
    elif "wav" in ct:
        ext = ".wav"
    elif "webm" in ct:
        ext = ".webm"

    path = os.path.join(tmp_dir, f"audio{ext}")
    with open(path, "wb") as f:
        f.write(resp.content)

    size_mb = os.path.getsize(path) / (1024 * 1024)
    logger.info(f"Downloaded {size_mb:.1f} MB -> {path}")
    return path


def handler(job):
    """RunPod handler: transcribe audio file."""
    inp = job["input"]

    language = inp.get("language", "ru")
    beam_size = inp.get("beam_size", 5)

    tmp_dir = tempfile.mkdtemp()

    try:
        # Get audio file
        if "audio_url" in inp:
            audio_path = _download_url(inp["audio_url"], tmp_dir)
        elif "audio_base64" in inp:
            audio_bytes = base64.b64decode(inp["audio_base64"])
            audio_path = os.path.join(tmp_dir, "audio.opus")
            with open(audio_path, "wb") as f:
                f.write(audio_bytes)
        else:
            return {"error": "Provide audio_url or audio_base64"}

        # Transcribe
        logger.info(f"Transcribing {audio_path} (lang={language}, beam={beam_size})...")

        segments_gen, info = model.transcribe(
            audio_path,
            language=language,
            beam_size=beam_size,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
        )

        segments = []
        full_parts = []

        for seg in segments_gen:
            segments.append({
                "start": round(seg.start, 2),
                "end": round(seg.end, 2),
                "speaker": "",
                "text": seg.text.strip(),
            })
            full_parts.append(seg.text.strip())

        full_text = " ".join(full_parts)

        logger.info(
            f"Done: {len(segments)} segments, {len(full_text)} chars, "
            f"duration={info.duration:.0f}s, lang={info.language}"
        )

        return {
            "full_text": full_text,
            "segments": segments,
            "duration_seconds": round(info.duration),
            "language": info.language,
        }

    except Exception as e:
        logger.error(f"Transcription error: {e}")
        return {"error": str(e)}

    finally:
        # Cleanup temp files
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


runpod.serverless.start({"handler": handler})
