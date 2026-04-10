FROM nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
RUN pip3 install --no-cache-dir \
    faster-whisper==1.1.0 \
    runpod==1.7.7 \
    httpx==0.28.1

# Pre-download the model at build time (baked into image = no cold start download)
RUN python3 -c "from faster_whisper import WhisperModel; WhisperModel('large-v3', device='cpu', compute_type='int8')" \
    && echo "Model downloaded"

COPY handler.py /app/handler.py

CMD ["python3", "-u", "/app/handler.py"]
