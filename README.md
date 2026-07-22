# OCR MCP Server

Python 3.11 OCR service for Ubuntu Server. The repository contains the durable
SQLite task model, bounded orchestration and secondary-OCR workers, REST and MCP
transports, artifact handling, orientation recovery, health endpoints, safe
logging, and Prometheus metrics. The container intentionally excludes the heavy
MinerU and Paddle runtime dependencies until their deployment task composes them.
REST 与 MCP 共享同一服务层（services），业务状态转换不绑定到传输协议。
项目已从最初的仓库骨架发展为经过测试的服务实现。

## Local setup and startup

```bash
uv venv --python 3.11
source .venv/bin/activate
uv pip install -e ".[dev]"
python -m ocr_mcp_server
```

Copy `config/example.yaml` for local configuration and select it with
`OCR_CONFIG_FILE`. Settings use the `OCR_` prefix and `__` for nested fields.
The secondary OCR engine is a deployment setting, never a request parameter.

## Health and traffic routing

`GET /health/live` is an unauthenticated process-liveness check. Docker keeps its
`HEALTHCHECK` on this endpoint using only Python's standard library. Liveness must
not be replaced by dependency readiness or require an API key.

`GET /health/ready` is strict traffic-routing readiness. It returns 503 unless
every required dependency is ready. It deliberately remains 503 until Task 13
composes live SQLite, MinerU, and Paddle probes; operators must not route customer
traffic based only on `/health/live`.

## Metrics and safe telemetry

`GET /metrics` is unauthenticated for scraper compatibility. Expose it only on a
trusted private network or behind an infrastructure access-control boundary.
Metric observations are best effort and never control request, queue, task, or
recovery behavior.

Exact application metric names are:

- `ocr_http_requests_total`
- `ocr_http_request_duration_seconds`
- `ocr_tasks_total`
- `ocr_task_duration_seconds`
- `ocr_pipeline_stage_duration_seconds`
- `ocr_orchestration_queue_depth`
- `ocr_secondary_ocr_queue_depth`
- `ocr_dependency_ready`
- `ocr_recovery_total`

Labels are finite and low-cardinality: allowlisted HTTP method/route/status class,
finite task/stage/recovery outcomes, finite processing stages, and the fixed
SQLite/MinerU/Paddle dependency set. Never add file or batch identifiers, URLs,
paths, filenames, OCR text, API keys, Authorization values, recovery tokens,
exception text, engine/model identifiers, page numbers, or angles to labels.

The same content is forbidden from logs. Log only validated event names, finite
codes, bounded counters, and explicitly safe scalar fields. Do not log raw request
bodies, headers, tokens, OCR content, filenames, URLs, filesystem paths, exception
objects, or exception messages.

## Deployment safety

The service account must exclusively own and write `data_root` (recommended
directory mode `0700`, file mode `0600`). Do not grant web users or unrelated
processes write access. Remote imports require HTTPS allowlisting plus egress
firewall or controlled-proxy enforcement to prevent access to private, loopback,
link-local, and reserved networks.

Build, deployment, and remote Ubuntu verification are controller-owned release
gates; feature tasks run local tests only and do not push, sync, build, or deploy.

## 远程 Ubuntu 容器验证

当前容器镜像仅包含 FastAPI 网关，不包含 MinerU 或 Paddle 推理依赖。本机只运行测试，不执行 Docker 镜像构建；尚未在远程 Ubuntu 完成构建和启动验证。

代码同步到 Ubuntu Server 后，在仓库根目录执行：

```bash
docker compose build ocr-gateway
docker compose up -d ocr-gateway
docker compose ps
curl -fsS http://127.0.0.1:8000/health/live
```

确认 `docker compose ps` 显示容器健康，且存活探针返回成功响应后，再记录远程实际构建得到的镜像 ID 和 digest。
