FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/opt/openmontage

# ffmpeg + fonts (DejaVu Latin, Noto Devanagari captions) + git (OpenMontage
# clone) + shared libs required by Remotion's headless Chrome.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-dejavu-core \
        fonts-noto-core \
        ca-certificates \
        curl \
        git \
        gnupg \
        libnss3 \
        libnspr4 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libdrm2 \
        libxkbcommon0 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxrandr2 \
        libgbm1 \
        libasound2 \
        libpango-1.0-0 \
        libcairo2 \
        libx11-6 \
        libxcb1 \
        libxext6 \
        libxi6 \
        libxtst6 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Node.js 22 (Remotion runtime) — NodeSource handles both amd64 and arm64.
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# OpenMontage tool layer, pinned. n8n orchestrates (agent pipeline unused);
# we consume VideoCompose + remotion-composer directly.
ARG OM_SHA=9327439db69021ab4b0e2776729bf3b58fdb5a87
RUN git clone https://github.com/calesthio/OpenMontage.git /opt/openmontage \
    && cd /opt/openmontage \
    && git checkout -q "$OM_SHA" \
    && rm -rf /opt/openmontage/.git

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -r /opt/openmontage/requirements.txt

# Bake the whisper model so RENDERER_STT=whisper works offline at runtime
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('tiny', device='cpu', compute_type='int8')"

# Remotion composer: lockfile-pinned deps + bake headless Chrome into the
# image so the first render never needs network access.
RUN cd /opt/openmontage/remotion-composer \
    && npm ci \
    && npx remotion browser ensure

# Remotion defaults concurrency to cpus/2 (=1 on this 2-core VM); use both.
RUN printf 'import {Config} from "@remotion/cli/config";\nConfig.setConcurrency(2);\n' \
    > /opt/openmontage/remotion-composer/remotion.config.ts

# 30fps makes an 8s/240-frame reel blow the 760s budget on this 956MB VM
# (chrome <25% CPU each => swap-bound, not CPU-bound). Halve fps: line numbers
# are stable because OM_SHA is pinned; greps fail the build if they drift.
RUN sed -i \
        -e '128s/30 \* 60/15 * 60/' \
        -e '132s/\* 30)/\* 15)/' \
        -e '142s/fps={30}/fps={15}/' \
        /opt/openmontage/remotion-composer/src/Root.tsx \
    && sed -n '142p' /opt/openmontage/remotion-composer/src/Root.tsx | grep -q 'fps={15}' \
    && sed -n '132p' /opt/openmontage/remotion-composer/src/Root.tsx | grep -q '\* 15)'

COPY app ./app

# Runs as root: single-purpose container; Remotion's browser/npm caches live
# in $HOME and must be usable at runtime (baked during build as root).
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
