FROM vllm/vllm-openai:v0.11.2

ENV PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10

RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --index-url https://pypi.org/simple "mineru[core]==3.2.0"

COPY scripts/smoke_mineru.py /app/scripts/smoke_mineru.py

ENTRYPOINT []

CMD ["mineru-openai-server", "--engine", "vllm", "--host", "0.0.0.0", "--port", "30000", "--gpu-memory-utilization", "0.45", "--model", "/models/mineru-vlm"]
