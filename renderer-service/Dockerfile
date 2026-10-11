FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg + fonts (DejaVu for Latin, Noto for Devanagari captions)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-dejavu-core \
        fonts-noto-core \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the whisper model so RENDERER_STT=whisper works offline at runtime
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('tiny', device='cpu', compute_type='int8')"

COPY app ./app

RUN useradd -m runner && mkdir -p /data/shared && chown -R runner:runner /srv /data/shared
USER runner

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
