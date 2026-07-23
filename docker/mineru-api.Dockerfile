FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10

WORKDIR /app

RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --index-url https://pypi.org/simple mineru==3.2.0

COPY scripts/mineru_fixed_api.py ./scripts/mineru_fixed_api.py
COPY scripts/smoke_mineru.py ./scripts/smoke_mineru.py

RUN groupadd --gid 10001 mineru \
    && useradd --uid 10001 --gid 10001 --create-home \
        --home-dir /home/mineru --shell /usr/sbin/nologin mineru \
    && chown -R 10001:10001 /app

USER 10001:10001

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()"]

CMD ["python", "/app/scripts/mineru_fixed_api.py"]
