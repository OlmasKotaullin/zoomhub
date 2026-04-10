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

# Model downloads at first run (~3GB) — RunPod caches between invocations
COPY handler.py /app/handler.py

CMD ["python3", "-u", "/app/handler.py"]
