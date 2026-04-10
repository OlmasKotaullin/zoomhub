# ZoomHub Whisper Worker (RunPod Serverless)

## Build & Push

```bash
# Build
docker build -t zoomhub-whisper .

# Tag for Docker Hub (or ghcr.io)
docker tag zoomhub-whisper YOUR_DOCKERHUB/zoomhub-whisper:latest

# Push
docker push YOUR_DOCKERHUB/zoomhub-whisper:latest
```

## Deploy on RunPod

1. Go to https://www.runpod.io/console/serverless
2. Create new endpoint:
   - Docker image: `YOUR_DOCKERHUB/zoomhub-whisper:latest`
   - GPU: RTX A4000 (16GB) or RTX 3090 (24GB)
   - Min workers: 0 (scale to zero)
   - Max workers: 5
   - Idle timeout: 5 seconds
3. Copy Endpoint ID and API Key

## Set env vars in Fly.io

```bash
fly secrets set RUNPOD_API_KEY=your_key RUNPOD_ENDPOINT_ID=your_endpoint_id TRANSCRIPTION_PROVIDER=runpod_whisper
```

## Test locally

```bash
python3 -c "
import runpod
runpod.api_key = 'YOUR_KEY'
result = runpod.Endpoint('YOUR_ENDPOINT_ID').run_sync({
    'input': {'audio_url': 'https://example.com/test.mp3', 'language': 'ru'}
})
print(result)
"
```
