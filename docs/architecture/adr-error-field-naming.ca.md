# ADR: Error field naming convention

**Status:** Accepted
**Date:** 2026-05-27

## Context

The codebase exposes "error" information through three distinct channels and, over time, settled on three different field names for them. Reviewers occasionally flag this as drift, but each channel is internally consistent — the issue is that no doc records *which* name belongs in *which* channel, so future code can reintroduce real drift by picking the wrong one.

The three channels:

1. **Persisted records and their response schemas.** Job, Action, VM, and Network rows store the last terminal error on the row itself. Models: `src/astinuslab/db/models.py`. Schemas: `src/astinuslab/api/schemas.py`. Frontend consumers: `src/astinuslab/web/templates/partials/live_section.html`, `src/astinuslab/web/static/playground/render-details.js`.
2. **HTTP error response bodies.** Raised via `HTTPException(detail=...)`. The custom handler in `src/astinuslab/main.py` follows the same shape. The JS client parses `data.detail` in `src/astinuslab/web/static/playground/api.js`.
3. **In-process status dicts and snapshots.** Image inspection map (`src/astinuslab/image_inspection.py`), vol3 analysis result (`src/astinuslab/forensics/vol3_analysis.py`), ISF matrix runner result (`src/astinuslab/orchestration/runners/isf_matrix_runner.py`), system status probes (`src/astinuslab/core/system_status.py`), guest script exec result (`src/astinuslab/virtualization/guest_script_support.py`). Templates consume these as `dl.error`, `r.error`, `network.error`.

## Decision

| Context                                              | Field name      |
| ---------------------------------------------------- | --------------- |
| SQLAlchemy model columns + Pydantic response schemas | `error_summary` |
| HTTP error response bodies                           | `detail` plus optional structured `code`/`context` envelope |
| In-process status dicts / snapshots                  | `error`         |

Rationale: each name is already load-bearing in its channel, with frontend and DB consumers wired to it. Unifying on a single name would require either an Alembic migration that breaks the existing JS/templates, or a custom FastAPI exception handler that fights the framework. The cost outweighs the cosmetic benefit.

## Consequences

- New persisted error fields **must** use `error_summary`.
- New JSON error responses **must** use `detail`. Machine-branchable API errors should use `ApiError` with stable snake_case `code` values and `context` for structured fields; migrated `409 Conflict` responses use this envelope.
- New in-process status dicts **must** use `error` for the error string slot.
- `tools/quality/audit_error_fields.py` enforces the convention in CI.

## Non-goals

- Unrelated keys like settings-toast `message` or download `message` (status text, not error reporting) are out of scope.
- The `level_counts` histogram key `"error"` in `src/astinuslab/ui/logs_routes.py` is a log severity label, not an error field.


## Structured API error envelope

The machine-readable API error envelope is `{ "detail": string, "code": string, "context": object|null }`. `detail` remains the human-readable compatibility field. `code` values are stable snake_case identifiers and must not be reused for a different condition. `context` carries structured values that clients may branch on; use `null` when no extra values apply.

`409 Conflict` responses are migrated first. Existing job-transition conflicts keep top-level `from_status` and `to_status` for one release while also returning `context: {"from_status": ..., "to_status": ...}`.
