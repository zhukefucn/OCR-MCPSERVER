# Task 13 Production Images Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a production-composed OCR gateway, mutually exclusive PP-StructureV3 CPU/GPU images, and an isolated MinerU OpenAI-compatible VLM image that can be built and started on the Ubuntu RTX 5090 server.

**Architecture:** The detailed approved design is authoritative: PP-StructureV3 runs inside the selected `ocr-gateway` image and remains owned by the existing single-owner worker; it is not exposed as a network service. `mineru-api` is a lightweight fixed `vlm-http-client` service and `mineru-openai-server` is the only VLM/GPU inference service separated from the gateway. Production composition injects the real document gateway, SQLite/MinerU/Paddle readiness probes, and lifecycle-owned workers into FastAPI.

**Tech Stack:** Python 3.11, FastAPI, SQLite/SQLAlchemy, MinerU 3.2.0, vLLM 0.11.2, PaddlePaddle 3.3.0, PaddleOCR 3.5.0 with `doc-parser`, CUDA 12.9, Docker Compose, pytest.

## Global Constraints

- The normal MinerU backend is always `vlm-http-client`; callers cannot select or override the backend or VLM URL.
- The production secondary engine is always `pp_structure_v3`; PaddleOCR-VL remains outside Task 13 and outside default images.
- CPU images contain neither `paddlepaddle-gpu`, CUDA/vLLM, nor PaddleOCR-VL dependencies or weights.
- `mineru-openai-server` listens only on the internal Compose network; optional host diagnostics bind to `127.0.0.1` only.
- Production startup performs no uncontrolled model download. Models are supplied by a verified read-only volume or a prebuilt model layer whose manifest and SHA-256 are recorded.
- Logs, health responses, metrics, image labels, and build output must not contain document text, filenames, URLs with credentials, API keys, recovery tokens, or model-repository credentials.
- The gateway retains exactly three MCP tools and the approved FastAPI REST surface.
- No Redis, PostgreSQL, external queue, runtime engine switching, or Paddle network microservice is introduced.
- Every delivery batch follows: local tests, independent review, GitHub push, checksum-verified Ubuntu sync, remote build, immediate startup/health/smoke validation, then immutable image tag and deployment record.

---

### Task 13B: Pinned PP-StructureV3 CPU image

**Files:**

- Create: `docker/ocr-gateway-ppstructure.Dockerfile`
- Create: `docker/requirements/pp-structure-v3.txt`
- Create: `scripts/smoke_pp_structure.py`
- Modify: `compose.yaml`
- Modify: `.dockerignore`
- Modify: `tests/test_docker_assets.py`
- Create: `tests/test_container_smoke_scripts.py`
- Modify: `README.md`

**Interfaces:**

- Produces Docker target `ppstructure-cpu` with `paddlepaddle==3.3.0` and `paddleocr[doc-parser]==3.5.0`.
- Produces `python /app/scripts/smoke_pp_structure.py --device cpu --fixture PATH`, which exits non-zero unless Paddle versions, CPU-only device state, model availability, and one real PP-StructureV3 prediction are valid.
- Produces Compose profile `ppstructure-cpu`; it never requests a GPU.

- [ ] **Step 1: Write failing image-contract tests**

Assert exact version pins, the official HTTPS CPU wheel index, absence of GPU/VL/vLLM dependencies, non-root runtime, read-only model mount, bounded healthcheck, and no public Paddle port. Assert the gateway package is installed without its development extra.

- [ ] **Step 2: Confirm RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_docker_assets.py tests/test_container_smoke_scripts.py -q`

Expected: failure because the PP-StructureV3 Dockerfile, requirement file, smoke script, and Compose profile do not exist.

- [ ] **Step 3: Implement the minimal CPU target**

Use `python:3.11-slim-bookworm` as the CPU base. Install the gateway first from the local build context, install `paddlepaddle==3.3.0` only from `https://www.paddlepaddle.org.cn/packages/stable/cpu/`, then install the exact `paddleocr[doc-parser]==3.5.0` pin from the ordinary HTTPS Python index. Preserve UID/GID `10001`, `/data`, `/models:ro`, and the standard-library `/health/live` healthcheck.

- [ ] **Step 4: Implement a content-free real inference smoke**

The script accepts only `cpu` or `gpu:0`, validates exact package versions, validates the compiled device mode, constructs the existing fixed PP-StructureV3 feature set, runs one supplied repository-owned synthetic table/formula fixture, and prints only version/device/result-count fields.

- [ ] **Step 5: Verify locally and commit**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_docker_assets.py tests/test_container_smoke_scripts.py -q
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m pip check
git diff --check
```

Commit: `feat(docker): add pinned pp-structure cpu image`

- [ ] **Step 6: Deliver the CPU image batch**

After independent review, push the commit, sync a checksum-verified source archive to Ubuntu, run `docker compose --profile ppstructure-cpu build`, start immediately, check `/health/live`, execute the CPU smoke fixture, record the image ID, and pin `ocr-mcp-server:ppstructure-cpu-<git-sha>` only after all checks pass.

---

### Task 13C: Blackwell PP-StructureV3 GPU image

**Files:**

- Modify: `docker/ocr-gateway-ppstructure.Dockerfile`
- Modify: `scripts/smoke_pp_structure.py`
- Modify: `compose.yaml`
- Modify: `tests/test_docker_assets.py`
- Modify: `tests/test_container_smoke_scripts.py`
- Modify: `README.md`

**Interfaces:**

- Produces Docker target `ppstructure-gpu` with `paddlepaddle-gpu==3.3.0` from the official CUDA 12.9 index and `paddleocr[doc-parser]==3.5.0`.
- Produces Compose profile `ppstructure-gpu` with one NVIDIA GPU reservation and no privileged/host-network/Docker-socket access.
- Reuses `smoke_pp_structure.py --device gpu:0` and fails unless CUDA is compiled in, exactly one requested GPU is visible, `paddle.utils.run_check()` succeeds, and a real candidate prediction completes.

- [ ] **Step 1: Write failing Blackwell contract tests**

Assert the exact CUDA 12.9 Paddle index, exact pins, GPU reservation, `gpu:0` deployment setting, absence of PaddleOCR-VL, and the required real-inference smoke. Reject CUDA 11.x/12.6 bases and floating `latest` tags.

- [ ] **Step 2: Confirm RED, implement, and verify locally**

Run the focused tests before and after implementation. The local Windows gate validates assets and script behavior with injected modules; it does not install Paddle or build the production image.

- [ ] **Step 3: Commit and independently review**

Run the full local suite, `pip check`, `compileall`, and `git diff --check`.

Commit: `feat(docker): add blackwell pp-structure gpu image`

- [ ] **Step 4: Deliver the GPU image batch**

On Ubuntu record `nvidia-smi`, driver, compute capability, Docker runtime and Container Toolkit versions. Build the GPU target, start it immediately with the verified model volume, run the real GPU fixture, inspect bounded logs, then pin `ocr-mcp-server:ppstructure-gpu-<git-sha>` and record its image ID/RepoDigest.

---

### Task 13D: Pinned MinerU API and VLM images

**Files:**

- Create: `docker/mineru-api.Dockerfile`
- Create: `docker/mineru-vlm.Dockerfile`
- Create: `scripts/smoke_mineru.py`
- Modify: `compose.yaml`
- Modify: `.dockerignore`
- Modify: `tests/test_docker_assets.py`
- Modify: `tests/test_container_smoke_scripts.py`
- Modify: `README.md`

**Interfaces:**

- Produces lightweight `mineru-api` with `mineru==3.2.0`, fixed backend `vlm-http-client`, and fixed server URL `http://mineru-vlm:30000`.
- Produces `mineru-vlm` from `vllm/vllm-openai:v0.11.2`, installs `mineru[core]==3.2.0`, runs `mineru-openai-server --host 0.0.0.0 --port 30000 --gpu-memory-utilization 0.45`, and mounts verified models read-only.
- Produces a content-free smoke that checks `/health`, `/v1/models`, package versions, CUDA capability `(12, 0)`, then submits one synthetic PDF to `mineru-api` using only `vlm-http-client` and validates a non-empty ZIP/Markdown result without logging its content.

- [ ] **Step 1: Write failing MinerU contracts**

Assert exact MinerU/vLLM pins, fixed commands/URLs, no floating `>=` or `latest`, internal-only VLM networking, read-only models, one GPU reservation, `0.45` initial VLM memory utilization, and no host port except optional `127.0.0.1` diagnostics.

- [ ] **Step 2: Confirm RED and implement minimal images/Compose**

The API image must reject request-level backend/server overrides through its fixed deployment command/config. The VLM image must not download models during container startup.

- [ ] **Step 3: Verify locally and commit**

Run focused contract/smoke tests followed by the complete local suite, `pip check`, `compileall`, and `git diff --check`.

Commit: `feat(docker): add pinned mineru api and vlm images`

- [ ] **Step 4: Deliver the MinerU batch**

Build on Ubuntu, immediately start VLM and API on the internal network, wait for bounded health, run package/GPU checks and the synthetic end-to-end parse, capture image IDs and model-manifest SHA-256, then pin both images with `<git-sha>` tags.

---

### Task 13E: Production runtime composition

**Files:**

- Create: `src/ocr_mcp_server/bootstrap.py`
- Create: `src/ocr_mcp_server/api/production_gateway.py`
- Modify: `src/ocr_mcp_server/app.py`
- Modify: `src/ocr_mcp_server/__main__.py`
- Modify: `src/ocr_mcp_server/api/document_gateway.py`
- Modify: `src/ocr_mcp_server/infra/health_probes.py`
- Modify: `compose.yaml`
- Create: `tests/test_bootstrap.py`
- Create: `tests/test_production_gateway.py`
- Modify: `tests/test_app.py`
- Modify: `tests/test_rest_api.py`
- Modify: `tests/test_mcp_api.py`
- Modify: `tests/test_docker_assets.py`

**Interfaces:**

- Produces `build_runtime(settings, observability, event_logger) -> RuntimeResources` with explicit owned SQLite engine/session factory, repositories, MinerU adapter, PP-Structure worker, orchestration service, retention service, orientation recovery, document gateway, and strict readiness probes.
- `RuntimeResources.start()` creates/migrates SQLite, starts the single Paddle owner and orchestration workers, and performs no document/model download.
- `RuntimeResources.close()` closes services in reverse dependency order and is idempotent under partial startup failure or cancellation.
- Produces the concrete four-method `ProductionDocumentGateway`; REST and MCP share this exact instance.

- [ ] **Step 1: Write failing lifecycle/composition tests**

Use temporary SQLite/storage and injected fake MinerU/Paddle providers. Prove dependency construction, startup order, reverse close, partial-start rollback, idempotent close, cancellation propagation, one Paddle owner, no model load during import, and no background-task/thread leaks across 100 lifespans.

- [ ] **Step 2: Implement `RuntimeResources` and inject it through FastAPI lifespan**

Keep `create_app()` dependency-injectable for tests. The CLI path creates real runtime resources; test-created apps without a runtime retain deterministic unavailable behavior. Start resources inside lifespan and close them before MCP/observability teardown completes.

- [ ] **Step 3: Write failing concrete gateway tests**

Cover upload streaming/idempotency, approved local/HTTPS sources, 20-file/30MB/500-page/1GiB limits, durable batch creation, progress callbacks, safe exception mapping, status/artifact links, orientation decoration, and exact REST/MCP response equivalence. Assert no content-bearing values reach logs or errors.

- [ ] **Step 4: Implement the minimal gateway facade**

Compose existing file intake, remote fetch, task repository, orchestration notification, artifact/status projection, and orientation recovery services. Do not duplicate pipeline logic in the transport facade and do not expose engine/backend/server settings in request models.

- [ ] **Step 5: Inject real readiness**

SQLite runs passive `SELECT 1`; MinerU performs only its fixed health call; Paddle observes the already-started worker lifecycle. Readiness returns `200` only when all three are ready and remains content-free on every failure.

- [ ] **Step 6: Verify and commit**

Run focused bootstrap/gateway/app/REST/MCP tests, the full local suite, `pip check`, `compileall`, and `git diff --check`.

Commit: `feat: compose production ocr runtime`

---

### Task 13F: Integrated Ubuntu startup validation and immutable release

**Files:**

- Create: `scripts/verify_deployment.py`
- Create: `docs/deployment/task-13-runbook.md`
- Modify: `README.md`
- Modify: `tests/test_container_smoke_scripts.py`
- Modify: `.superpowers/sdd/progress.md`
- Modify: parent implementation checklist after all evidence is captured.

- [ ] **Step 1: Add a bounded content-free deployment verifier**

Validate Compose config, image IDs, dependency health, gateway live/ready, authenticated REST upload/submit/status, the exactly-three MCP tool list, one synthetic end-to-end result package, CPU/GPU package separation, model-manifest SHA-256, and bounded logs. Never print document content or credentials.

- [ ] **Step 2: Run the full local delivery gate and independent review**

No remote sync occurs until all local tests, static checks, plan gates, and review findings are clean.

- [ ] **Step 3: Sync and build on Ubuntu**

Push GitHub, create a source archive from the reviewed commit, verify SHA-256 on both machines, ensure the remote worktree is clean, then build the selected production profile on the RTX 5090 server.

- [ ] **Step 4: Start immediately and run integration smoke**

Start containers in dependency order, enforce bounded readiness waits, run the deployment verifier, inspect GPU memory/OOM state and sanitized logs, and stop/reject the candidate on any failure.

- [ ] **Step 5: Pin and record**

Only after success, tag all images with the exact Git SHA, record image IDs/RepoDigests, host driver/Container Toolkit versions, model manifest hash, Compose profile, test counts and verification timestamps in `deployments/ocr-mcp-server/<git-sha>.env`. Mark Task 13 complete in both progress and the parent plan.
