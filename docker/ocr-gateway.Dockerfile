FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OCR_SERVER__HOST=0.0.0.0 \
    OCR_SERVER__PORT=8000 \
    OCR_DATA_ROOT=/data \
    OCR_CONFIG_FILE=/app/config/example.yaml

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY config/example.yaml ./config/example.yaml

RUN python -m pip install --no-cache-dir . \
    && groupadd --gid 10001 ocr \
    && useradd --uid 10001 --gid 10001 --create-home \
        --home-dir /home/ocr --shell /usr/sbin/nologin ocr \
    && mkdir -p /data \
    && chown -R 10001:10001 /data /app

USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3).read()"]

CMD ["ocr-mcp-server"]
