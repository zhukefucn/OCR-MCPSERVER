# OCR MCP Server

Python 3.11 OCR service for Ubuntu Server. The repository contains the durable
SQLite task model, bounded orchestration and secondary-OCR workers, REST and MCP
transports, artifact handling, orientation recovery, health endpoints, safe
logging, and Prometheus metrics. Heavy OCR dependencies remain isolated in
separate pinned Paddle and MinerU image targets so CPU-only clients do not install
CUDA, vLLM, or PaddleOCR-VL.
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

## PP-StructureV3 CPU image

The `ppstructure-cpu` profile builds a Python 3.11 gateway with exactly
`paddlepaddle==3.3.0` and `paddleocr[doc-parser]==3.5.0`. It contains no GPU,
vLLM, or PaddleOCR-VL runtime. Place the verified PP-StructureV3 model bundle in
`models/pp-structure-v3/`, including `pp-structure-v3.yaml`; Compose mounts that
directory at `/models` read-only. Record and verify the bundle manifest and its
SHA-256 before startup. The container does not download models during startup.
The same bundle must expose the verified `PP-LCNet_x1_0_doc_ori` weights at
`doc-orientation/`. The gateway constructs PaddleOCR's dedicated document
orientation classifier from `/models/doc-orientation`; it never substitutes a
layout confidence score and never downloads this model at runtime.
`model-manifest.json` must map the PP-Structure
`DocOrientationClassify` node to exactly `doc-orientation`; the offline smoke
rejects any other mapping, verifies every file digest, and runs a dedicated
classifier prediction from that directory in addition to the PP-Structure
prediction.

The bundle must also contain `model-manifest.json`. Its `models` object maps the
eleven enabled PaddleX YAML node paths to relative model directories, while its
`files` array lists the pipeline YAML and every required model file as a relative
path plus lowercase SHA-256. The smoke check rejects missing, unlisted, modified,
absolute, or `/models`-escaping paths and injects the verified directories into
the exact `pp-structure-v3.yaml` used by production. Disabled chart, seal,
unwarping, text-line-orientation, and region models are not required.

The controller-owned Ubuntu delivery gate uses:

```bash
docker compose --profile ppstructure-cpu build ocr-gateway-ppstructure-cpu
docker compose --profile ppstructure-cpu up -d ocr-gateway-ppstructure-cpu
curl -fsS http://127.0.0.1:8000/health/live
docker compose --profile ppstructure-cpu exec ocr-gateway-ppstructure-cpu \
  python /app/scripts/smoke_pp_structure.py --device cpu \
  --fixture /app/scripts/fixtures/pp_structure_smoke.json
```

The smoke command checks exact package versions, confirms a CPU-only Paddle
build, validates the supplied model configuration, runs one real prediction,
and prints only version, device, and result-count fields. Remote build and
startup validation remain a separate release step after local review.
Run the image smoke once with outbound networking denied and an empty model
cache before release; any attempted model download must fail the candidate.

## PP-StructureV3 GPU image

The mutually scoped `ppstructure-gpu` profile uses the same verified read-only
model bundle and gateway runtime as the CPU profile, but installs exactly
`paddlepaddle-gpu==3.3.0` from PaddlePaddle's CUDA 12.9 index. It reserves one
NVIDIA GPU and configures PP-StructureV3 for `gpu:0`; do not activate the CPU
and GPU profiles together.

The controller-owned Ubuntu delivery gate uses:

```bash
docker compose --profile ppstructure-gpu build ocr-gateway-ppstructure-gpu
docker compose --profile ppstructure-gpu up -d ocr-gateway-ppstructure-gpu
curl -fsS http://127.0.0.1:8000/health/live
docker compose --profile ppstructure-gpu exec ocr-gateway-ppstructure-gpu \
  python /app/scripts/smoke_pp_structure.py --device gpu:0 \
  --fixture /app/scripts/fixtures/pp_structure_smoke.json
```

The GPU smoke requires a CUDA-enabled Paddle build, exactly one visible GPU,
`paddle.utils.run_check()`, and one real structured prediction. The GPU
constructor intentionally omits the CPU-only `enable_mkldnn` option. Perform
the real image build and smoke on the approved RTX 5090 Ubuntu host before
assigning an immutable tag.

## MinerU API and VLM images

The `mineru` profile builds two internal-only services. `mineru-api` pins
`mineru==3.2.0` and exposes a fixed-policy facade to the loopback official API.
Only `vlm-http-client` with `http://mineru-vlm:30000` is accepted; callers cannot
select a backend or VLM endpoint. `mineru-vlm` pins
`vllm/vllm-openai:v0.11.2` plus `mineru[core]==3.2.0`, reserves one NVIDIA GPU,
and starts with `--gpu-memory-utilization 0.45`.

Place the controller-verified MinerU model snapshot in
`models/mineru-vlm/`. Compose mounts it read-only at `/models/mineru-vlm`, and
the explicit `--model /models/mineru-vlm` command prevents startup-time model
selection or download. Record and verify the snapshot manifest and SHA-256
before startup. Neither MinerU service publishes a host port.

On the approved Ubuntu GPU host, the controller-owned validation sequence is:

```bash
docker compose --profile mineru build mineru-vlm mineru-api
docker compose --profile mineru up -d mineru-vlm mineru-api
docker compose --profile mineru ps
docker compose --profile mineru exec mineru-api \
  python /app/scripts/smoke_mineru.py --runtime-only
docker compose --profile mineru exec mineru-vlm \
  python /app/scripts/smoke_mineru.py --runtime-only
docker compose --profile mineru exec mineru-vlm \
  python /app/scripts/smoke_mineru.py
```

The runtime-only checks import OpenCV, resolve Noto CJK through `fc-match`, and
render one synthetic CJK glyph with Pillow entirely in memory. The full smoke
then verifies exact package versions, one CUDA 12.0-capable visible GPU, both
health surfaces, a loaded VLM model, and one synthetic PDF task whose ZIP
contains bounded non-empty Markdown. Both commands emit only finite
availability/version/capability/result-count fields. Run them immediately after
the remote build; assign immutable Git-SHA image tags only after they succeed.

## 远程 Ubuntu 容器验证

本机只运行测试和静态门禁，不执行 GPU Docker 镜像构建。Paddle 与
MinerU/vLLM 使用独立镜像目标；在远程 Ubuntu 完成构建、启动和 smoke
验证前，不得给候选镜像固定版本。
尚未在远程 Ubuntu 完成构建和启动验证。
默认 `ocr-gateway` 镜像仅包含 FastAPI 网关，不包含 MinerU 或 Paddle 推理依赖；
推理依赖只存在于对应 profile 的独立镜像中。

代码同步到 Ubuntu Server 后，在仓库根目录执行：

```bash
docker compose build ocr-gateway
docker compose up -d ocr-gateway
docker compose ps
curl -fsS http://127.0.0.1:8000/health/live
```

确认 `docker compose ps` 显示容器健康，且存活探针返回成功响应后，再记录远程实际构建得到的镜像 ID 和 digest。
