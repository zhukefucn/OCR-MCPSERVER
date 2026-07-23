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
export OCR_VERIFY_API_KEY='set-without-shell-history'
python scripts/verify_deployment.py --phase static
python scripts/verify_deployment.py --phase runtime
python scripts/verify_deployment.py --phase e2e
```

Exit codes are `0` success, `1` configuration, `2` runtime/readiness, `3`
end-to-end, and `4` safety-boundary failure. Output is compact content-free
JSON. The verifier never prints HTTP bodies, document text, logs, filenames,
URLs, credentials, or OCR output and never tags an image.

On failure, retain only the finite failure code, stop the candidate, fix it
locally, and repeat the delivery sequence. Do not tag.

After success, independently record image IDs/RepoDigests, driver and Container
Toolkit versions, Compose profile, source/model-manifest SHA-256, test counts,
and UTC timestamps in `deployments/ocr-mcp-server/<git-sha>.env`. Only then
create Git-SHA image tags.
