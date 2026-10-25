# Errors

AstinusLab uses standard FastAPI and application-specific HTTP errors.

## Common Status Codes

| Status | Meaning |
| --- | --- |
| `400 Bad Request` | Invalid request shape or invalid playground input such as unknown image/network references. |
| `401 Unauthorized` | Missing or invalid admin bearer token when API auth is enabled. Body `{"detail": "admin bearer token required"}` with a `WWW-Authenticate: Bearer` header. Not returned when auth is disabled (the default). |
| `404 Not Found` | Missing job, artifact, playground session, VM, network, acquisition, or lab metadata. |
| `409 Conflict` | Invalid state transition, duplicate active playground session, active job conflict, duplicate names, network in use, or capture requested while a VM is not running. |
| `422 Unprocessable Entity` | Pydantic validation failure, such as invalid enum values or malformed fields. |
| `500 Internal Server Error` | Unexpected server-side failure. |
| `503 Service Unavailable` | Playground work could not be queued, metrics collection is unavailable, or a request needs an operator-supplied Volatility tool that is not ready. The dependency case carries `code: "dependency_unavailable"` and `context.dependency_code`. |

## Error Bodies

Most errors return JSON with `detail`:

```json
{
  "detail": "no active playground session"
}
```

Invalid job state transitions include status metadata:

```json
{
  "detail": "invalid transition",
  "code": "invalid_job_transition",
  "context": { "from_status": "completed", "to_status": "queued" },
  "from_status": "completed",
  "to_status": "queued"
}
```

## Artifact Errors

Legacy job artifact endpoints return `404` when the requested memory or disk artifact is not available for that job.

Generic artifact file endpoints return `404` when the artifact file, manifest path, or SHA-256 sidecar path is missing or absent on disk. They return `403` when a recorded path resolves outside the allowed artifact storage boundary.

When available, successful artifact responses include:

| Header | Meaning |
| --- | --- |
| `X-Astinuslab-SHA256` | SHA-256 checksum recorded for the artifact. |
| `X-Astinuslab-Size` | Artifact size in bytes. |

## Playground Queue Errors

Playground endpoints that enqueue background work return `503` with:

```json
{
  "detail": "operation could not be queued"
}
```

The API attempts to mark the affected network, VM, or acquisition failed when enqueue compensation is available.

## Dependency Errors

Volatility 3 and `dwarf2json` are operator-supplied host tools that AstinusLab never downloads or installs. A request that needs one of them while it is missing, unverified, or an unsupported version fails with `503` and a stable machine-readable code:

```json
{
  "detail": "Configure [vol3].volatility_bin to enable Volatility analysis.",
  "code": "dependency_unavailable",
  "context": { "dependency_code": "volatility_not_configured" }
}
```

`context.dependency_code` is one of the readiness codes:

| Code | Meaning |
| --- | --- |
| `feature_disabled` | Volatility integration is turned off in configuration. |
| `volatility_not_configured` | No Volatility executable is configured. |
| `volatility_not_found` | The configured Volatility executable does not exist or is not on `PATH`. |
| `volatility_not_executable` | The configured Volatility path is not a regular executable file. |
| `volatility_probe_failed` | The version probe could not be run, failed, or returned an unrecognized banner. |
| `volatility_version_unsupported` | The probe reported a version below the minimum `2.28.0`, or a version string that could not be interpreted. |
| `dwarf2json_not_configured` | No `dwarf2json` path is configured. |
| `dwarf2json_hash_not_configured` | A `dwarf2json` path is configured without its SHA-256 pin. |
| `dwarf2json_not_found` | The configured `dwarf2json` file does not exist. |
| `dwarf2json_not_executable` | The configured `dwarf2json` path is not a regular executable file, or could not be read. |
| `dwarf2json_hash_mismatch` | The configured `dwarf2json` file does not match its configured SHA-256 pin. |

`volatility_version_unsupported` is a floor, not an exact match. `2.28.0` is the documented minimum: versions are ordered by [PEP 440](https://peps.python.org/pep-0440/), so a pre-release of the minimum such as `2.28.0rc1` sorts *below* it and is refused too, and a version string that cannot be parsed as a version is refused because it cannot be compared against the floor. A version at or above the minimum that is not among the versions this release was tested against is **not** refused: it reports `ready` with `untested: true`, and analysis runs. See [Endpoints](endpoints.md) for that field.

`detail` is a safe, operator-actionable message. It never carries the configured executable path, the expected or actual hash, environment values, or raw subprocess output; those stay in the protected service logs. Poll `GET /api/v1/volatility/readiness` to read the same codes before enqueueing work.

This replaced the earlier `409` / `conflict` response for unavailable tools. `409` / `conflict` still means a genuine state conflict, such as a run already queued.

Browser form posts in the web UI redirect back to the page with an error message instead of returning this body.
