FROM python:3.11-slim-bookworm AS ppstructure-cpu

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OCR_SERVER__HOST=0.0.0.0 \
    OCR_SERVER__PORT=8000 \
    OCR_DATA_ROOT=/data \
    OCR_CONFIG_FILE=/app/config/example.yaml

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
COPY config/example.yaml ./config/example.yaml

RUN python -m pip install --no-cache-dir .

COPY docker/requirements/pp-structure-v3.txt ./docker/requirements/pp-structure-v3.txt

RUN python -m pip install --no-cache-dir \
        --index-url https://www.paddlepaddle.org.cn/packages/stable/cpu/ \
        paddlepaddle==3.3.0 \
    && python -m pip install --no-cache-dir \
        --index-url https://pypi.org/simple \
        -r docker/requirements/pp-structure-v3.txt

COPY scripts/smoke_pp_structure.py ./scripts/smoke_pp_structure.py
COPY scripts/fixtures/pp_structure_smoke.json ./scripts/fixtures/pp_structure_smoke.json

RUN groupadd --gid 10001 ocr \
    && useradd --uid 10001 --gid 10001 --create-home \
        --home-dir /home/ocr --shell /usr/sbin/nologin ocr \
    && mkdir -p /data /models \
    && chown -R 10001:10001 /data /app \
    && chmod 0555 /models

USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3).read()"]

CMD ["ocr-mcp-server"]

FROM python:3.11-slim-bookworm AS ppstructure-gpu

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OCR_SERVER__HOST=0.0.0.0 \
    OCR_SERVER__PORT=8000 \
    OCR_DATA_ROOT=/data \
    OCR_CONFIG_FILE=/app/config/example.yaml

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
COPY config/example.yaml ./config/example.yaml

RUN python -m pip install --no-cache-dir .

COPY docker/requirements/pp-structure-v3.txt ./docker/requirements/pp-structure-v3.txt

RUN python -m pip install --no-cache-dir \
        --index-url https://www.paddlepaddle.org.cn/packages/stable/cu129/ \
        paddlepaddle-gpu==3.3.0 \
    && python -m pip install --no-cache-dir \
        --index-url https://pypi.org/simple \
        -r docker/requirements/pp-structure-v3.txt

COPY scripts/smoke_pp_structure.py ./scripts/smoke_pp_structure.py
COPY scripts/fixtures/pp_structure_smoke.json ./scripts/fixtures/pp_structure_smoke.json

RUN groupadd --gid 10001 ocr \
    && useradd --uid 10001 --gid 10001 --create-home \
        --home-dir /home/ocr --shell /usr/sbin/nologin ocr \
    && mkdir -p /data /models \
    && chown -R 10001:10001 /data /app \
    && chmod 0555 /models

USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3).read()"]

CMD ["ocr-mcp-server"]
