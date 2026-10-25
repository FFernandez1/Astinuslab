# Endpoints

This list is verified against `src/astinuslab/api/routes.py`, `src/astinuslab/playground/api.py`, `src/astinuslab/playground/routes/*.py`, and `src/astinuslab/main.py`.

## Health

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Dependency-free API process liveness check. |
| `GET` | `/ready` | Dependency readiness check; returns 503 only for database or writable-storage failure. |
| `GET` | `/api/v1/readiness` | Run authenticated deep dependency and permission readiness checks. |
| `GET` | `/api/v1/metrics` | Authenticated read-only activity metrics over a time window. |

### Operational probes

- `GET /health` is the process-liveness check. It is dependency-free and returns `200` while the API process can serve requests.
- `GET /ready` checks the database and writable storage as critical dependencies. Worker or libvirt failures return `200` with `status: degraded`; database or storage failures return `503` with `status: not_ready`.

Both endpoints are public and return an `X-Request-ID` header. Probe responses expose coarse component states only; use that request ID to find diagnostic details in backend logs.

### Activity metrics

`GET /api/v1/metrics` is the authenticated read-only summary of recent activity, aggregated from existing Job and HostAction rows. Where `/ready` answers whether the platform can work, this answers how well it has been working.

`window_hours` (default `24`, range 1–2160) selects the reporting window. The response contains `generated_at`, `window_hours`, an `actions` list (per action type: `completed`, `failed`, `p50_seconds`, `p95_seconds`, `max_seconds`), a `jobs` map of counts by status, and a `queue` block (`queued`, `running`, `oldest_queued_age_seconds`, `max_dispatch_attempts`). Duration percentiles are nearest-rank over completed actions only, so a capture that failed after two seconds does not flatter the p50. The queue block is not limited to the window: an action queued days ago is what it exists to surface. The same figures appear on the Settings page.

### Deep readiness

`GET /api/v1/readiness` is the authenticated operator check to run before starting Jobs. It returns `200` when every applicable check passed or is `not_applicable`, and `503` when at least one check failed. The full probe set has a 10-second response deadline; expiry returns the stable `deep_readiness_timeout` check instead of hanging the request.

The response contains `ready`, overall `status`, and an ordered `checks` list. Every check has a stable `name` and machine-readable `code`, a `status` of `passed`, `failed`, or `not_applicable`, an operator-safe `detail`, and `applies_to` service identities (`api`, `runner`, or `analysis`). Checks cover the database write transaction, Celery broker, separate runner and analysis heartbeats, libvirt connection, storage write and free-space guardrail, privileged-root policy, helper ownership/mode, application-source non-writability, Volatility analysis and ISF-build tooling, and fake-mode state.

| Check `name` | Stable non-ready `code` values | `applies_to` |
| --- | --- | --- |
| `deep_readiness_deadline` | `deep_readiness_timeout` | `api`, `runner`, `analysis` |
| `database_transaction` | `database_transaction_failed` | `api`, `runner`, `analysis` |
| `celery_broker` | `celery_broker_unavailable` | `api`, `runner`, `analysis` |
| `runner_heartbeat` | `runner_heartbeat_missing`, `remote_control_unsupported` | `runner` |
| `analysis_heartbeat` | `analysis_heartbeat_missing`, `remote_control_unsupported` | `analysis` |
| `privileged_helper_sudo_runner` | `privileged_helper_sudo_unavailable`, `runner_readiness_probe_in_progress`, `runner_readiness_report_invalid`, `runner_report_unavailable`, `remote_control_unsupported` | `runner` |
| `libvirt_connection` | `libvirt_connection_unavailable`, `fake_libvirt_active` | `runner` |
| `storage_write` | `storage_not_writable` | `api`, `runner`, `analysis` |
| `storage_free_space` | `storage_free_space_unavailable`, `storage_free_space_below_guardrail`, `storage_guardrail_unavailable` | `runner`, `analysis` |
| `root_policy` | `acl_policy_storage_root_mismatch`, `fake_libvirt_active` | `runner` |
| `privileged_helper_integrity` | `privileged_helper_missing`, `privileged_helper_invalid_type`, `privileged_helper_owner_mismatch`, `privileged_helper_mode_mismatch`, `fake_libvirt_active` | `runner` |
| `privileged_helper_sudo_api` | `not_applicable` | `api` |
| `source_non_writability` | `application_source_missing`, `application_source_writable`, `application_source_unreadable` | `api`, `runner`, `analysis` |
| `volatility_analysis`, `volatility_isf_build` | Existing Volatility readiness codes described below, or `feature_disabled` | `runner`, `analysis` / `runner` |
| `fake_mode` | `fake_mode_active` | `api`, `runner`, `analysis` |

Privileged-helper sudo is deliberately service-scoped. The API service runs with `NoNewPrivileges=true`, so its sudo check is always `not_applicable` and the API process never executes `sudo -n`. Runner sudo coverage is evaluated inside the runner and surfaced through its worker-owned readiness reply; if no runner reply arrives, the runner heartbeat fails while the sudo check remains `not_applicable`. Missing sudo coverage is also not a warning for the analysis worker.

Worker heartbeats are transport-scoped in the same way. Celery remote control is a fanout exchange, and the default SQLite broker transport does not implement fanout, so no worker reply can ever arrive. On such a broker `runner_heartbeat`, `analysis_heartbeat`, and `privileged_helper_sudo_runner` report `not_applicable` with `remote_control_unsupported` instead of a permanent false failure; worker liveness is proved by the service manager and by real queue round-trips.

## Web UI

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/volatility` | Open the Volatility 3 workbench for symbol readiness, ISF builds, diagnostics, and build history. |
| `POST` | `/volatility/build` | Create an ISF-only Linux symbol-build job from the Volatility workbench. Internally uses the same job queue and `build_vol3_isf` contract as the REST API. |

## Volatility JSON API

Canonical Volatility 3 workbench API, versioned under `/api/v1/volatility`. The older `/volatility/api/*` paths are deprecated aliases that emit `Deprecation: true`.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/volatility/readiness` | Report whether the operator-supplied Volatility 3 and `dwarf2json` tools are usable. |
| `GET` | `/api/v1/volatility/dumps` | List memory dumps available to the Volatility workbench. |
| `GET` | `/api/v1/volatility/catalog` | List supported Volatility plugins, optionally filtered by OS. |
| `GET` | `/api/v1/volatility/queue` | List recent workbench plugin runs across all dumps. |
| `DELETE` | `/api/v1/volatility/queue` | Clear the queue: delete every plugin run's result files (report and artifact rows are kept). |
| `POST` | `/api/v1/volatility/runs` | Queue an on-demand single-plugin run for a job dump. |
| `GET` | `/api/v1/volatility/runs/latest` | Return the newest completed run for a `job_id`+`plugin`, or `{"status": "none"}`. |
| `GET` | `/api/v1/volatility/runs/{job_id}/{run_id}` | Poll one workbench plugin run and retrieve persisted results. |
| `DELETE` | `/api/v1/volatility/runs/{job_id}/{run_id}` | Delete a single plugin run from the queue. |
| `POST` | `/api/v1/volatility/targets/{target_id}/isf/upload` | Store an operator-supplied ISF from the Volatility workbench target row (detailed under Manual ISF upload below). |

### Tool readiness

Volatility 3 and `dwarf2json` are operator-supplied host tools. `GET /api/v1/volatility/readiness` reports the two capabilities independently, so a client can offer memory analysis while Linux ISF builds stay unavailable, or the reverse:

```json
{
  "enabled": true,
  "analysis": {
    "ready": true,
    "code": "ready",
    "detail": "Volatility 3 is ready.",
    "version": "2.28.0",
    "untested": false
  },
  "isf_build": {
    "ready": false,
    "code": "dwarf2json_not_configured",
    "detail": "Configure [vol3].dwarf2json_path and dwarf2json_sha256 to build a Linux ISF from kernel debug symbols. Prebuilt symbol sources and manual ISF upload remain available.",
    "version": null,
    "untested": false
  }
}
```

- `enabled` is `false` when Volatility integration is turned off; both capabilities then report `feature_disabled` without probing the host.
- `code` is a stable machine-readable value. The full list is in [Errors](errors.md), which uses the same codes for the `503` / `dependency_unavailable` response.
- `version` carries the Volatility banner version whenever a probe produced one — that is, on `ready` and on `volatility_version_unsupported`. It is `null` otherwise and always `null` for `isf_build`, because the `dwarf2json` check verifies a SHA-256 pin rather than probing a version.
- `untested` is `true` only for an `analysis` tool that is `ready` at or above the minimum version but outside the versions this release was tested against. It is always `false` for `isf_build`.
- The response deliberately omits the configured executable paths, the expected and actual `dwarf2json` hashes, environment values, and raw probe output.

### Minimum version and untested versions

`2.28.0` is the documented **minimum**, not an exact match. Versions are ordered by [PEP 440](https://peps.python.org/pep-0440/), never as strings:

- Below the minimum — including a pre-release of it, such as `2.28.0rc1`, which sorts below `2.28.0` — the tool is refused: `ready` is `false` with code `volatility_version_unsupported`, and no analysis runs. A version string that cannot be parsed as a version is refused the same way, because it cannot be compared against the minimum.
- One of the tested versions: `ready` is `true`, `untested` is `false`, and nothing changes.
- At or above the minimum but not a tested version: analysis still runs, and the response flags it so a client can warn the operator that results are unvalidated:

```json
{
  "ready": true,
  "code": "ready",
  "detail": "Volatility 2.29.0 is newer than the versions tested with this release; analysis will run, but results have not been validated against this version.",
  "version": "2.29.0",
  "untested": true
}
```

The check is performed fresh on every call, and a readiness response is never treated as execution authorization: the `dwarf2json` hash is recomputed immediately before each local execution and each guest transfer.

Like the rest of the JSON API, this endpoint requires the admin bearer token or the browser session cookie when `auth.enabled = true`.

## Catalog

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/targets` | List target OS images. |
| `GET` | `/api/v1/scenarios` | List job scenarios. |
| `GET` | `/api/v1/workloads` | List workloads. Supports `cve`, `target_id`, and `compatible_only` query parameters. |
| `GET` | `/api/v1/cves` | List indexed CVEs and workload IDs. |

## Scenario Packages

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/scenario-packages/playground/current` | Export the active Playground as a shareable zip with `metadata.yaml` and `scenario.yaml`. |
| `POST` | `/api/v1/scenario-packages/import` | Import a scenario package zip. Optional multipart `resource_overrides` can resize VMs by label with `vcpus`, `memory_mb`, and `disk_gb`. |

## Jobs

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/v1/jobs` | Create a job. |
| `GET` | `/api/v1/jobs` | List jobs. |
| `GET` | `/api/v1/jobs/{job_id}` | Get job details. |
| `DELETE` | `/api/v1/jobs/{job_id}` | Delete a job record. Optional `delete_files=true` removes artifacts too. |
| `GET` | `/api/v1/jobs/{job_id}/logs` | List job events. |
| `GET` | `/api/v1/jobs/{job_id}/artifact` | Download memory artifact. |
| `GET` | `/api/v1/jobs/{job_id}/disk-artifact` | Download disk artifact. |
| `POST` | `/api/v1/jobs/{job_id}/cancel` | Request cancellation. Returns 409 (`conflict`) when the job is in a capture/publish phase — captures are not interruptible for forensic consistency. |
| `POST` | `/api/v1/jobs/{job_id}/capture` | Trigger manual memory, disk, or both capture. |
| `POST` | `/api/v1/jobs/{job_id}/acquire-memory-dump` | Alias for manual capture. |
| `POST` | `/api/v1/jobs/{job_id}/retry` | Create a new job from a failed job configuration. |
| `POST` | `/api/v1/jobs/{job_id}/force-fail` | Force a job into failed state and kill active subprocesses. |
| `GET` | `/api/v1/jobs/{job_id}/lab-info` | Get workload URLs, credentials, and lab metadata. |
| `POST` | `/api/v1/jobs/{job_id}/reset-lab` | Request restore from the clean lab snapshot. |
| `GET` | `/api/v1/jobs/{job_id}/runtimes` | List per-VM runtime records. |
| `POST` | `/api/v1/jobs/{job_id}/runtimes/{role}/{action}` | Request runtime action such as `reboot` or `shutdown`. |

## Acquisitions and Host Actions

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/acquisitions` | List acquisitions. |
| `POST` | `/api/v1/acquisitions` | Create an acquisition record. |
| `GET` | `/api/v1/acquisitions/{acquisition_id}` | Get acquisition details. |
| `PATCH` | `/api/v1/acquisitions/{acquisition_id}/status` | Update acquisition status to `live`, `paused`, or `sealed`. |
| `GET` | `/api/v1/cases` | List cases. |
| `POST` | `/api/v1/cases` | Create a case. |
| `GET` | `/api/v1/cases/{case_id}` | Get case details. |
| `PATCH` | `/api/v1/cases/{case_id}/status` | Update case status to `open`, `closed`, or `sealed`. |
| `GET` | `/api/v1/artifacts` | List artifacts. Supports `case_id`, `job_id`, and `acquisition_id` query parameters. |
| `GET` | `/api/v1/artifacts/{artifact_id}` | Get artifact identity, checksum and provenance metadata; use the download endpoints for sidecars. |
| `GET` | `/api/v1/artifacts/{artifact_id}/download` | Download artifact file by artifact ID. |
| `GET` | `/api/v1/artifacts/{artifact_id}/manifest` | Download artifact manifest sidecar. |
| `GET` | `/api/v1/artifacts/{artifact_id}/sha256` | Download artifact SHA-256 sidecar. |
| `GET` | `/api/v1/reports` | List reports. Supports `case_id`, `job_id`, `acquisition_id`, `input_artifact_id`, and `output_artifact_id` query parameters. |
| `GET` | `/api/v1/reports/{report_id}` | Get report metadata. |
| `GET` | `/api/v1/host-actions` | List host action records. |
| `POST` | `/api/v1/run-retention` | Trigger retention cleanup. |

## Playground Sessions and Networks

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/v1/playground/sessions` | Create a playground session. |
| `GET` | `/api/v1/playground/sessions/current` | Get the active playground session; returns 404 when none exists. |
| `GET` | `/api/v1/playground/sessions/current-or-null` | Get the active playground session or `null` without a 404. |
| `GET` | `/api/v1/playground/sessions/recent-failure` | Get the most recent failed Playground teardown, or `null` when none is recorded. |
| `DELETE` | `/api/v1/playground/sessions/{job_id}` | Destroy the active playground session. |
| `POST` | `/api/v1/playground/networks` | Create a network, creating a session if needed. |
| `GET` | `/api/v1/playground/networks/suggest-cidr` | Suggest the next available playground CIDR. Optional `job_id` and repeated `extra` query parameters reserve existing or proposed CIDRs. |
| `POST` | `/api/v1/playground/sessions/{job_id}/networks` | Create a network in an existing active session. |
| `DELETE` | `/api/v1/playground/networks/{network_id}` | Delete a network that has no attached VMs. |
| `GET` | `/api/v1/playground/sessions/current/topology-layout` | Get saved topology node positions. |
| `PATCH` | `/api/v1/playground/sessions/current/topology-layout/{node_id}` | Save one topology node position. |
| `GET` | `/api/v1/playground/sessions/current/recipe` | Export the active session as a JSON playground recipe. |
| `POST` | `/api/v1/playground/recipes/import` | Import a JSON playground recipe into the active session, creating one if needed. |

## Playground VMs, NICs, Captures, and Metrics

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/v1/playground/vms` | Spawn a VM, creating a session and default network if needed. |
| `POST` | `/api/v1/playground/sessions/{job_id}/vms` | Spawn a VM in an existing active session. |
| `DELETE` | `/api/v1/playground/vms/{vm_id}` | Destroy a VM. |
| `POST` | `/api/v1/playground/vms/{vm_id}/actions` | Request VM power action: `start`, `stop`, or `reboot`. |
| `POST` | `/api/v1/playground/sessions/current/vms/{vm_id}/nics` | Attach a NIC to a VM. |
| `DELETE` | `/api/v1/playground/sessions/current/vms/{vm_id}/nics/{nic_id}` | Detach a NIC from a VM. |
| `POST` | `/api/v1/playground/vms/{vm_id}/captures` | Trigger memory or disk capture for a running VM. |
| `GET` | `/api/v1/playground/vms/{vm_id}/captures` | List captures for a VM. |
| `GET` | `/api/v1/playground/captures/{acq_id}/events` | Stream capture events as Server-Sent Events. |
| `GET` | `/api/v1/playground/vms/{vm_id}/metrics` | Get live metrics for one VM. |
| `GET` | `/api/v1/playground/sessions/current/metrics` | Get live metrics for running VMs in the current session. |

## Playground Catalog

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/playground/catalog/images` | List playground-eligible images. Optional `role` query defaults to `target`. |
| `GET` | `/api/v1/playground/catalog/workloads` | List workloads compatible with `image_id`. |

## Advanced Overrides

Operator escape hatches for supplying artifacts manually (recorded with unverified provenance). These require the admin bearer token when auth is enabled.

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/v1/jobs/{job_id}/isf/upload` | Store an operator-supplied ISF for a job, bypassing GNU build-id enforcement. |
| `POST` | `/api/v1/jobs/{job_id}/dump/upload` | Store an operator-supplied memory dump for a job, bypassing ELF completeness validation. |
| `POST` | `/api/v1/volatility/targets/{target_id}/isf/upload` | Store an operator-supplied ISF from the Volatility workbench target row. |

## Request Examples

Create a job:

```json
{
  "target_id": "ubuntu-22-04-default",
  "scenario_id": "immediate-capture",
  "execution_mode": "immediate",
  "capture_type": "memory",
  "build_vol3_isf": false
}
```

Trigger a manual capture:

```json
{
  "capture_type": "both",
  "exploit_tags": ["manual-lab"],
  "exploit_notes": "Operator-triggered capture"
}
```

Create a playground network:

```json
{
  "name": "internal",
  "subnet_cidr": "10.42.10.0/24",
  "isolated": true
}
```

Spawn a playground VM:

```json
{
  "label": "target-1",
  "image_id": "ubuntu-22-04-default",
  "role": "target",
  "vcpus": 2,
  "memory_mb": 4096,
  "disk_gb": 40
}
```
