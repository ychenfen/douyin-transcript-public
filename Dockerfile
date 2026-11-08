FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DOUYIN_SERVER_HOST=0.0.0.0 \
    DOUYIN_SERVER_PORT=8766 \
    DOUYIN_DATA_DIR=/data/items \
    DOUYIN_SHERPA_MODEL_DIR=/data/models/sensevoice \
    DOUYIN_CHROMIUM_EXECUTABLE=/usr/bin/chromium

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    chromium \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY douyin_workflow ./douyin_workflow

RUN pip install --upgrade pip && \
    pip install -e ".[lite,f2,browser]"

RUN mkdir -p /data/items /data/models/sensevoice /data/secrets

EXPOSE 8766

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8766/health || exit 1

CMD ["python", "-m", "douyin_workflow", "serve", "--host", "0.0.0.0", "--port", "8766"]
