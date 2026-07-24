# Task 10 FastAPI REST and MCP Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:test-driven-development` and `superpowers:subagent-driven-development`. Every behavior starts with a failing test. Do not push or deploy from an implementation worker.

**Goal:** Expose the approved OCR workflow through a secure, weak-agent-friendly FastAPI REST API and exactly three FastMCP 2 tools without exposing MinerU, Paddle, model, device, concurrency, or orientation-strategy controls.

**Architecture:** Add a transport-neutral `DocumentGateway` application port and strict Pydantic request/response contracts. REST routes and MCP tools are thin adapters over the same port, so status and errors cannot diverge. A pure ASGI API-key middleware protects every `/v1` and MCP request while leaving liveness public. The concrete OCR composition remains injectable; Task 11 supplies the page-orientation recovery implementation and later composition tasks wire real storage/model services.

**FastMCP contract:** Pin `fastmcp>=2.13.1,<3`. Build the MCP ASGI app with `FastMCP.http_app()`, preserve its lifespan when mounting into FastAPI, and use `Context.report_progress(progress, total)` only through a content-free progress bridge. Official references: https://gofastmcp.com/v2/integrations/fastapi and https://gofastmcp.com/v2/servers/progress.

## Global constraints

- Exactly three public MCP tools: `parse_documents`, `get_task_status`, `reparse_with_page_orientation`.
- REST mirrors the same workflow and adds only the binary upload transport adapter needed to obtain a `file_id`.
- Request models use `extra="forbid"`; no engine, backend, model, device, angle, concurrency, server URL, local path, or retry-policy fields.
- A source is exactly one of an uploaded `file_id` or an allowlisted HTTPS URL. Arbitrary local paths are forbidden.
- `reparse_with_page_orientation` accepts only `recovery_token` and optional unique positive page numbers. It never accepts an angle or engine.
- All protected endpoints fail closed when no key is configured. Accept `Authorization: Bearer <key>` and `X-API-Key: <key>` with constant-time comparison; conflicting credentials fail.
- `/health/live` remains unauthenticated and contains no dependency or secret detail.
- Errors expose stable codes and safe generic messages only. Never serialize exception causes, OCR text, original filenames, URLs, keys, or file bytes.
- REST and MCP must use the same application port and response DTOs. MCP progress values are monotonic and content-free.
- Preserve one-process Paddle ownership. Do not add multiple Uvicorn workers or load Paddle/MinerU packages into the gateway image.

---

## Task 10A: Strict contracts and application port

**Files:**

- Add `src/ocr_mcp_server/api/contracts.py`
- Add `src/ocr_mcp_server/api/gateway.py`
- Add `tests/test_api_contracts.py`

- [ ] Define strict source, upload receipt, parse submission/result, batch/file status, artifact reference, safe error, and orientation-reparse DTOs.
- [ ] Define an async `DocumentGateway` protocol with `upload_document`, `parse_documents`, `get_task_status`, and `reparse_with_page_orientation` methods plus an optional content-free progress callback.
- [ ] Validate canonical UUID identifiers, bounded idempotency keys, HTTPS URLs, 1–20 sources, unique positive page numbers, and terminal result/artifact invariants.
- [ ] Prove forbidden internal controls and arbitrary path fields fail Pydantic validation.

## Task 10B: Fail-closed API-key authentication and REST routes

**Files:**

- Modify `src/ocr_mcp_server/settings.py`
- Modify `config/example.yaml`
- Add `src/ocr_mcp_server/api/auth.py`
- Add `src/ocr_mcp_server/api/rest.py`
- Modify `src/ocr_mcp_server/app.py`
- Add `tests/test_api_auth.py`
- Add `tests/test_rest_api.py`
- Modify `tests/test_settings.py`

- [ ] Add secret-safe authentication settings. Reject blank, duplicate, malformed, or boolean values and never include raw keys in representations or serialization used by errors.
- [ ] Implement pure ASGI authentication so streaming MCP responses are not buffered. Public liveness is the only bypass.
- [ ] Add REST endpoints: binary upload transport, parse submission, task status, and orientation reparse. Use stable `/v1` paths and explicit OpenAPI operation IDs.
- [ ] Bound upload metadata and stream handling before forwarding to the gateway. Do not read an entire document into memory in the route.
- [ ] Map validation, authentication, not-found, conflict, capacity, unavailable, and internal failures to stable HTTP statuses and content-free errors.
- [ ] Inject a fake gateway in tests; do not start MinerU or Paddle.

## Task 10C: Exactly three FastMCP tools

**Files:**

- Modify `pyproject.toml`
- Add `src/ocr_mcp_server/api/mcp.py`
- Modify `src/ocr_mcp_server/app.py`
- Add `tests/test_mcp_api.py`
- Modify Docker/README tests only where the dependency and mounted endpoint change the verified contract.

- [ ] Register exactly the three approved tool names with short, explicit descriptions suitable for weak models.
- [ ] Reuse the strict DTOs and same injected gateway as REST; do not generate tools automatically from all FastAPI routes.
- [ ] Bridge content-free progress to `Context.report_progress`; clients without a progress token must still succeed.
- [ ] Convert failures to safe MCP tool errors without causes or business content.
- [ ] Mount Streamable HTTP correctly with the FastMCP lifespan and verify initialization/list-tools/call-tools with an in-process FastMCP client.
- [ ] Verify API-key protection applies to MCP initialize, session, and tool calls.

## Task 10D: Integration gates and handoff

**Files:**

- Append `.superpowers/sdd/task-10-report.md`
- Update `.superpowers/sdd/progress.md` only after controller deployment succeeds.

- [ ] Run focused REST/auth/MCP/settings tests.
- [ ] Run the complete Windows suite, `pip check`, `compileall`, and `git diff --check`.
- [ ] Request independent controller review for security, exact tool count, lifespan, progress, and error redaction.
- [ ] After review READY, controller follows the mandated cadence: push GitHub, bundle/checksum sync to Ubuntu, Linux full tests, build, immediate startup, authenticated REST/MCP smoke, then immutable image pin and deployment record.

## Acceptance evidence

- OpenAPI exposes the documented REST operations and no engine-selection fields.
- Unauthenticated/invalid/conflicting credentials cannot reach gateway code.
- FastMCP lists exactly three tools and calls share REST DTO/error semantics.
- A fake parse workflow emits monotonic progress without filenames, URLs, OCR text, or keys.
- Reparse rejects angle/engine fields and accepts only recovery token plus optional page numbers.
- No API/MCP code imports PaddleOCR, PaddleX, MinerU, torch, OpenCV, Redis, Celery, or an external queue.
