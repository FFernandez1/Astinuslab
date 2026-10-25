# API Overview

AstinusLab exposes the web UI and REST API from the FastAPI app in `src/astinuslab/main.py`.

| Surface | Base URL |
| --- | --- |
| Main API | `http://localhost:8000/api/v1` |
| Playground API | `http://localhost:8000/api/v1/playground` |
| Volatility JSON API | `http://localhost:8000/api/v1/volatility` |
| Liveness check | `http://localhost:8000/health` |
| Readiness check | `http://localhost:8000/ready` |
| OpenAPI JSON | `http://localhost:8000/openapi.json` |
| Swagger UI | `http://localhost:8000/docs` |
| Static assets | `http://localhost:8000/static/...` |
| Volatility workbench | `http://localhost:8000/volatility` |

Use `/health` to confirm that the API process is serving requests. Use `/ready` to check its database and writable storage; worker or libvirt failures report `degraded` without making the operator UI unavailable.

ISF-only symbol builds are still Jobs internally. UI users should start Linux symbol builds from `/volatility`; API callers should continue using `POST /api/v1/jobs` with `build_vol3_isf: true` for Linux `immediate` jobs. Windows targets reject `build_vol3_isf` because Windows analysis uses PDB symbols after memory capture.

Request and response models are defined in `src/astinuslab/api/schemas.py`, with a few local playground response models in `src/astinuslab/playground/api.py` and `src/astinuslab/playground/routes/*.py`.

## Content Type

Use JSON request bodies and first define `api_curl` from [API examples](../usage/examples.md). It handles a configured admin bearer token or the development CSRF cookie/header. Release installs enable authentication by default.

```bash
api_curl -X POST "$BASE_URL/api/v1/jobs" \
  -H "Content-Type: application/json" \
  -d '{"target_id":"ubuntu-22-04-default","scenario_id":"immediate-capture","execution_mode":"immediate"}'
```

Artifact endpoints return file responses.

## Authentication

AstinusLab supports configurable single-admin bearer-token authentication. Development checkouts normally keep it disabled on loopback; release installs enable it by default. When enabled, every route requires the bearer token except `/health`, `/ready`, `/login`, `/logout`, and `/static/*` assets — this covers the JSON API, Playground endpoints, artifact downloads, OpenAPI docs, and the web UI pages themselves. See [Authentication](authentication.md).

## Error Format

Most validation and application errors use FastAPI JSON error bodies with a `detail` field. Conflict (`409`) responses use the structured envelope `{ "detail": string, "code": string, "context": object|null }`. Invalid job state transitions also include legacy top-level `from_status` and `to_status` for one release.

See [Errors](errors.md).


## Breaking changes and deprecations

- `JobOut` no longer exposes host filesystem paths (`image_path`, `dump_path`, `disk_dump_path`) or `celery_task_id`. Use `has_memory_dump`, `has_disk_dump`, and existing dump metadata; fetch content through download endpoints.
- `ArtifactOut` no longer exposes server paths (`path`, `manifest_path`, `sha256_sidecar_path`). Fetch artifact content, manifests, and sha256 sidecars through the artifact download endpoints.
- `POST /api/v1/jobs/{job_id}/acquire-memory-dump` is deprecated. Use `POST /api/v1/jobs/{job_id}/capture`; alias remains for one minor release.
- Volatility JSON endpoints are versioned under `/api/v1/volatility/*`. Old `/volatility/api/*` paths are deprecated aliases and emit `Deprecation: true`; non-`/api/v1` routes are UI-internal and unstable.
