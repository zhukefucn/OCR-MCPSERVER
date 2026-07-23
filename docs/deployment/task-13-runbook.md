# Task 13 Ubuntu deployment gate

Run this controller-owned gate only after the reviewed commit is on Ubuntu and
before creating immutable image tags.

## Preconditions

- The clean worktree commit and source-archive SHA-256 match the controller.
- `OCR_AUTH__API_KEYS` and `OCR_VERIFY_API_KEY` contain the deployment key.
- `OCR_PUBLIC_BASE_URL` is the externally correct HTTPS origin.
- Offline model manifests and every listed file are verified.
- Production, CPU PP-Structure, MinerU API and MinerU VLM images are built.

## Start and verify

```bash
docker compose --profile production up -d mineru-vlm mineru-api ocr-production
if [ -z "${OCR_VERIFY_API_KEY:-}" ]; then
  read -rsp 'OCR verification API key: ' OCR_VERIFY_API_KEY
  export OCR_VERIFY_API_KEY
  printf '\n'
fi
GIT_SHA="$(git rev-parse HEAD)"
RUN_ID="$(
  python scripts/verify_deployment.py --derive-run-id --git-sha "$GIT_SHA"
)"
python scripts/verify_deployment.py --phase static --run-id "$RUN_ID"
python scripts/verify_deployment.py --phase runtime --run-id "$RUN_ID"
python scripts/verify_deployment.py --phase e2e --run-id "$RUN_ID"
```

Exit codes are `0` success, `1` configuration, `2` runtime/readiness, `3`
end-to-end, and `4` safety-boundary failure. Output is compact content-free
JSON. The verifier never prints HTTP bodies, document text, logs, filenames,
URLs, credentials, or OCR output and never tags an image.

`RUN_ID` binds the exact Git commit and the canonical, sorted IDs of all three
production Compose images (`ocr-production`, `mineru-api`, and `mineru-vlm`).
Reuse it when retrying the same candidate so REST idempotency is stable.
Recompute it after the commit or any image changes. The verifier rejects a run
ID that does not match the currently selected three-image candidate.

On failure, retain only the finite failure code, stop the candidate, fix it
locally, and repeat the delivery sequence. Do not tag.

After success, independently record image IDs/RepoDigests, driver and Container
Toolkit versions, Compose profile, source/model-manifest SHA-256, test counts,
and UTC timestamps in `deployments/ocr-mcp-server/<git-sha>.env`. Only then
create Git-SHA image tags.
