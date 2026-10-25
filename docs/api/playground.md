# Playground API Reference

> **Internal but contract-stable. Frontend depends on this surface.**

The Playground exposes a REST API (v1) for interactive lab sessions: ad-hoc VMs, multi-segment networks, on-demand captures, and live metrics. All routes are mounted under the `playground` tag.

**Base URL**: `http://<host>:8000/api/v1/playground` for Playground-scoped routes. Scenario package routes are mounted at `http://<host>:8000/api/v1/scenario-packages`.

Request and response bodies reference Pydantic schemas defined in `src/astinuslab/api/schemas.py` (e.g. `PlaygroundSessionOut`, `PlaygroundVMOut`). Local response models declared in `src/astinuslab/playground/serializers.py` and `src/astinuslab/playground/routes/*.py` (`PlaygroundAcceptedOut`, `PlaygroundCaptureOut`, `PlaygroundAcquisitionOut`, `PlaygroundCreateIfNeededSpawnVMRequest`) are documented inline.

Routes that enqueue Celery work return HTTP `202 Accepted`. Single-session, naming, and VM-capacity conflicts return `409 Conflict`. Missing or non-active sessions return `404 Not Found`. Broker enqueue failures surface as `503 Service Unavailable` with detail `operation could not be queued`.


## Session identifier fields

Playground responses now expose `session_id` as the canonical playground session identifier. Legacy `job_id` remains present with the same value, is marked deprecated in OpenAPI, and will be removed in the next minor version. New frontend/API clients should read `session_id` and only fall back to `job_id` for older servers.

---

## 1. Sessions

A playground session is single-slot: only one session may be active at a time, and it cannot coexist with an active forensic job.

### Create Session
`POST /sessions` -> `202 Accepted`
Creates a new empty playground session.
**Response**: `PlaygroundSessionOut`.
**Errors**: `409` if a playground session is already active, or if a forensic job is currently active.

### Get Current Session
`GET /sessions/current` -> `200 OK`
Returns the currently active session, including all VMs and networks.
**Response**: `PlaygroundSessionOut`.
**Errors**: `404` if no active playground session.

### Get Current Session or Null
`GET /sessions/current-or-null` -> `200 OK`
Returns the currently active session, including all VMs and networks, or JSON `null` when no active session exists. The frontend uses this for normal polling without treating an empty Playground as an error.
**Response**: `PlaygroundSessionOut` or `null`.

### Get Most Recent Destroy Failure
`GET /sessions/recent-failure` -> `200 OK`
Returns the most recently completed Playground session whose teardown failed, or JSON `null` when no failed teardown has been recorded. The frontend uses this to surface a non-blocking cleanup failure after a session ends.
**Response**: `PlaygroundFailureOut` or `null`. A failure object contains `session_id`, `status` (`destroy_failed`), and optional `error_summary` and `finished_at` fields.

### Destroy Session
`DELETE /sessions/{job_id}` -> `202 Accepted`
Marks the session `destroy_requested` and enqueues teardown of all VMs and networks.
**Response**: `PlaygroundAcceptedOut`.
**Errors**: `404` if the session is not found or not active.

---

## 2. Networks

Networks are libvirt-backed segments scoped to a session. Each network has a name unique within its session, a CIDR, and an isolation flag.

### Suggest CIDR
`GET /networks/suggest-cidr?job_id=<session-id>&extra=10.42.2.0/24` -> `200 OK`
**Response**: `PlaygroundCidrSuggestionOut` (`subnet_cidr`).
Returns the next free `10.42.x.0/24` CIDR. `job_id` is optional and reserves existing networks for that session. `extra` is optional and repeatable; use it to reserve in-wizard draft CIDRs that are not yet persisted.
**Errors**: `409` if the available CIDR pool is exhausted.

### Create Network (seamless, create-or-reuse session)
`POST /networks` -> `202 Accepted`
**Payload**: `PlaygroundCreateNetworkRequest` (`name`, `subnet_cidr`, `isolated`).
**Response**: `PlaygroundSessionOut` — the full session state after the network has been registered as pending.

Reuses the active playground session if one exists, otherwise creates a new session first. The network is registered in the `pending` state before the Celery `create_network` task is queued, so the frontend can transition out of the "create first network" view in a single round trip and poll session state for activation. This differs from the session-scoped variant below, which requires the session to already exist.

**Errors**:
- `409` if a forensic job is active, if the network name already exists in the session, or if a CIDR conflict is detected.
- `503` if the broker enqueue fails after the network has been registered (the network is marked `failed`, and any session created in this call is torn down).

### Create Network (session-scoped)
`POST /sessions/{job_id}/networks` -> `202 Accepted`
**Payload**: `PlaygroundCreateNetworkRequest`.
**Response**: `PlaygroundNetworkOut`.
Requires the target session to be the active session. Registers a pending network and queues the create task.
**Errors**: `404` if the session is not active; `409` for name collisions, CIDR conflicts, or active forensic jobs; `503` on broker failure.

### Delete Network
`DELETE /networks/{network_id}` -> `202 Accepted`
Marks the network `destroying` and enqueues teardown.
**Response**: `PlaygroundAcceptedOut`.
**Errors**: `404` if the network is not found in the active session; `409` if any VM is still attached.

---

## 3. VMs

VMs are booted from catalog images and attached to one or more networks via NICs. Labels must be unique within a session.

### Spawn VM (create-if-needed wrapper)
`POST /vms` -> `202 Accepted`
**Payload**: `PlaygroundCreateIfNeededSpawnVMRequest`:

```json
{
  "label": "target-1",
  "image_id": "ubuntu-22-04-default",
  "vcpus": 2,
  "memory_mb": 4096,
  "disk_gb": 40,
  "role": "target",
  "workload_id": "wordpress-6.4",
  "network_ids": ["net-abc123"],
  "new_networks": [
    {"name": "dmz", "subnet_cidr": "10.42.2.0/24", "isolated": true}
  ]
}
```

`role` is `"target"`, `"attacker"`, or `"network"` (default `"target"`). `network_ids` is optional: when omitted with no `new_networks`, the wrapper ensures a default isolated network exists (`name="default"`, `subnet_cidr="10.42.1.0/24"`) and attaches the VM to it. `new_networks` is optional and contains inline `PlaygroundCreateNetworkRequest` objects; each inline network is created first, then attached to the VM with any existing `network_ids`. When `network_ids` is provided, every referenced network must already exist in the active session and be in state `active`.

The image id must resolve to a Playground-eligible catalog image with a present local image file. Unknown or unavailable images are rejected before creating a new session, default network, VM row, or spawn task whenever possible.

**Response**: `PlaygroundVMOut`.

Creates a playground session if none is active, ensures the requested networks (or a default network) are present, then delegates to the session-scoped spawn endpoint. Any session created by this call is torn down on failure.

**Errors**:
- `400` if a referenced network or image is unknown, or workload validation fails.
- `404` if a referenced network is missing.
- `409` if the label is already in use, the network is not usable, VM capacity is reached (`code: vm_capacity`), or session/network/VM uniqueness is violated.
- `500` on internal failure (any session created in this call is torn down).
- `503` if the broker enqueue fails.

### Spawn VM (session-scoped)
`POST /sessions/{job_id}/vms` -> `202 Accepted`
**Payload**: `PlaygroundSpawnVMRequest` (`label`, `image_id`, `nics`, optional `vcpus`/`memory_mb`/`disk_gb`, `role`, optional `workload_id`).
**Response**: `PlaygroundVMOut`.
Registers a pending VM with one NIC per entry in `nics` and queues the spawn task. The image id must resolve to a local file before the VM row is registered or the spawn task is queued.
**Errors**: `400` for unknown networks, unknown or unavailable image ids, or workload validation failures; `404` if the session is not active; `409` for label collisions, unusable network state, or VM capacity reached (`code: vm_capacity`); `503` if the broker enqueue fails (the VM is marked `failed`).

### Destroy VM
`DELETE /vms/{vm_id}` -> `202 Accepted`
Marks the VM `destroying` and enqueues teardown.
**Response**: `PlaygroundAcceptedOut`.
**Errors**: `404` if the VM is not found in the active session; `503` if the broker enqueue fails (the VM state is rolled back).

### VM Power Action
`POST /vms/{vm_id}/actions` -> `202 Accepted`
**Payload**: `PlaygroundVMActionRequest` (`action`: `"start"`, `"stop"`, or `"reboot"`).
**Response**: `PlaygroundAcceptedOut`.
Runs the corresponding libvirt action against the VM domain. `stop` maps to `virsh shutdown`; `start` and `reboot` use matching virsh actions. The VM state is updated optimistically after the command succeeds.
**Errors**: `404` if the VM is not found in the active session; `409` if the requested action is unavailable for the current VM/session state, or if `action=start` would exceed VM capacity (`code: vm_capacity`); `500` if the libvirt command fails.

---

## 4. NICs

NIC attach and detach operate on the active session.

### Attach NIC
`POST /sessions/current/vms/{vm_id}/nics` -> `202 Accepted`
**Payload**: `PlaygroundAttachNicRequest` (`network_id`).
**Response**: `PlaygroundAcceptedOut`.
**Errors**: `404` if no active session, VM, or network; `503` if the broker enqueue fails.

### Detach NIC
`DELETE /sessions/current/vms/{vm_id}/nics/{nic_id}?force=false` -> `202 Accepted`
**Response**: `PlaygroundAcceptedOut`.
**Errors**: `404` if no active session, VM, or NIC; `503` if the broker enqueue fails. If `force=true` and enqueue fails, the NIC row is removed locally and the endpoint returns `200 OK`.

---

## 5. Captures

Captures produce memory or disk acquisitions tied to a VM and the parent session.

### Trigger Capture
`POST /vms/{vm_id}/captures` -> `202 Accepted`
**Payload**: `PlaygroundCaptureRequest` (`kind`: `"memory"` or `"disk"`).
**Response** (local model `PlaygroundCaptureOut`):

```json
{
  "acquisition_id": "<hex>",
  "status": "queued",
  "task_id": "<celery-task-id-or-null>"
}
```

A new `Acquisition` row is created in status `queued` and a `queued` event is recorded before the capture task is enqueued.

**Errors**: `404` if the VM is not in the active session; `409` if the VM is not `running`; `503` if the broker enqueue fails (the acquisition is marked `failed` and a `failed` event is recorded).

### List Captures
`GET /vms/{vm_id}/captures` -> `200 OK`
**Response**: `list[PlaygroundAcquisitionOut]` (local model). Each entry:

```json
{
  "id": "<hex>",
  "name": "playground-<vm_id>-<kind>-<timestamp>",
  "status": "queued|running|completed|succeeded|failed|cancelled|error",
  "created_at": "<iso8601>",
  "path": "<path or null>",
  "sha256": "<hex or null>",
  "size_bytes": 0,
  "kind": "memory|disk",
  "session_id": "<job_id>",
  "vm_label": "<label>"
}
```

Ordered most-recent first.

### Stream Capture Events (SSE)
`GET /captures/{acq_id}/events` -> `200 OK` (`text/event-stream`)
Server-Sent Events stream of `AcquisitionEvent` rows for the given acquisition.

**Errors**: `404` if the acquisition is not found.

Each event is emitted in standard SSE format:

```
event: <status>
data: {"id":<int>,"status":"<status>","message":"<text>","created_at":"<iso8601>"}

```

**Event types**:

| Event | Meaning |
| --- | --- |
| `queued` | Capture has been recorded and the Celery task is enqueued. |
| `running` | Capture task has started executing. |
| `completed` | Capture finished successfully (terminal). |
| `failed` | Capture failed (terminal). |
| `stream_timeout` | The server-side stream deadline was reached. Emitted with `data: {"status":"stream_timeout","message":"server closed stream"}` before the connection closes. |

The stream closes when the acquisition reaches any terminal status (`succeeded`, `completed`, `failed`, `cancelled`, `error`), when the client disconnects, or when the server-side timeout (`playground_capture_stream_timeout_seconds`) elapses.

---

## 6. Catalog

Catalog endpoints surface the images and workloads that can be used in the playground.

### List Playground Images
`GET /catalog/images?role=target` -> `200 OK`
Returns playground-eligible base images for the requested `role` (default `"target"`). For target images, eligible means the catalog entry is enabled, supports cloud-image boot, and its local image file is present. Missing target image files are excluded from this endpoint, matching unavailable attacker and network images.
**Response**: `list[dict]` from `catalog.list_playground_images_for_role`.
**Errors**: `400` if the role is invalid.

### List Workloads for Image
`GET /catalog/workloads?image_id=<id>` -> `200 OK`
Returns the workloads compatible with the given image, annotated with compatibility status.
**Response**: `list[PlaygroundWorkloadCatalogOut]`.
**Errors**: `404` if the image is not found.

---

## 7. Metrics

Metrics are sampled per VM via `playground_metrics`. They are best-effort and return `503` if libvirt sampling is unavailable.

### Get VM Metrics
`GET /vms/{vm_id}/metrics` -> `200 OK`
**Response**: `PlaygroundVMMetricsOut` (`vm_id`, `state`, `cpu_pct`, `mem_pct`, `mem_used_mb`, `mem_total_mb`, `rx_bytes`, `tx_bytes`, `sampled_at`).
**Errors**: `404` if the VM is not in the active session; `503` if the sampler fails.

### Batch Session Metrics
`GET /sessions/current/metrics` -> `200 OK`
Samples every `running` VM in the active session.
**Response**: `dict[str, PlaygroundVMMetricsOut]` keyed by `vm_id`. VMs whose sample fails are omitted (the failure is logged but does not fail the request).
**Errors**: `404` if no active playground session.

---

## 8. Topology

Topology layout positions are persisted per node for the active session. The frontend debounces per-node saves on the client side; the server stores one row per node.

### Get Topology Layout
`GET /sessions/current/topology-layout` -> `200 OK`
**Response**: `PlaygroundTopologyLayout` (`nodes: list[PlaygroundTopologyNode]`).
**Errors**: `404` if no active playground session.

### Save Topology Node
`PATCH /sessions/current/topology-layout/{node_id}` -> `200 OK`
**Payload**: `PlaygroundTopologyNode` (`node_id`, `node_type` of `"vm"` or `"network"`, `x`, `y`). The body's `node_id` must match the path.
**Response**: `PlaygroundTopologyNode`.
**Errors**: `400` if the body `node_id` does not match the path; `404` if no active playground session.

---

## 9. Recipes

Recipes are JSON documents using `PlaygroundRecipe` (`schema_version: 1`). They capture networks, VMs, and topology positions for repeatable Playground labs. VM entries may include `vcpus`, `memory_mb`, and `disk_gb` resource hints; import passes them to the same spawn path used by direct VM creation.

### Export Current Recipe
`GET /sessions/current/recipe` -> `200 OK`
**Response**: `PlaygroundRecipe`.
Exports the active session. Network and VM IDs are translated to stable recipe names: network nodes use network names, VM nodes use VM labels.
**Errors**: `404` if no active playground session.

### Import Recipe
`POST /recipes/import` -> `202 Accepted`
**Payload**: `PlaygroundRecipe`.
**Response**: `PlaygroundSessionOut`.
Validates recipe shape, creates a session if needed, creates networks, spawns VMs, and stores topology positions. Recipe VM `networks` entries reference recipe network names, not database IDs.
**Errors**: `400` for invalid recipe references or schema validation errors; `409` for name collisions with existing networks or VM labels, or if the recipe would exceed VM capacity (`code: vm_capacity`) before network creation; endpoint-specific create/spawn errors may also surface from network or VM creation.

---

## 10. Scenario Packages

Scenario packages are zip archives for one-click sharing. They contain:

- `metadata.yaml` — package metadata including `schema_version: 1` and `package_type: astinuslab.playground.scenario`.
- `scenario.yaml` — Playground recipe data.
- Optional safe relative script entries for future catalog/workload package flows.

These routes are mounted under `/api/v1/scenario-packages`, not `/api/v1/playground`.

### Export Current Playground Package
`GET /api/v1/scenario-packages/playground/current` -> `200 OK` (`application/zip`)

Exports the active Playground session as a zip package. The response uses `Content-Disposition: attachment` with a `.zip` filename.

**Errors**: `404` if no active playground session.

### Import Playground Package
`POST /api/v1/scenario-packages/import` -> `202 Accepted`

**Payload**: `multipart/form-data` with:

- `file` — scenario package zip.
- `resource_overrides` — optional JSON object keyed by VM label. Allowed fields per VM are `vcpus`, `memory_mb`, and `disk_gb`.

Example `resource_overrides` value:

```json
{
  "target-1": {
    "vcpus": 4,
    "memory_mb": 8192,
    "disk_gb": 80
  }
}
```

**Response**: `PlaygroundSessionOut`.

The import validates the archive, rejects unsafe paths, parses `scenario.yaml`, applies resource overrides, validates the result as `PlaygroundRecipe`, then creates networks and VMs through the normal recipe import flow. Packaged scripts are validated/listed but are not installed into the local catalog by this endpoint.

**Errors**: `400` for malformed zips, unsafe paths, invalid YAML, invalid resource overrides, or invalid recipe schemas; `409` for name collisions with existing networks or VM labels.

---

## Local Response Models

These models are declared inside `src/astinuslab/playground/serializers.py` and `src/astinuslab/playground/routes/*.py` and used only by playground routes:

### `PlaygroundAcceptedOut`
```json
{
  "status": "accepted",
  "resource_id": "<id or null>",
  "job_id": "<job_id or null>",
  "task_id": "<celery-task-id or null>"
}
```
Returned by all `202 Accepted` endpoints that do not return a resource snapshot.

### `PlaygroundCreateIfNeededSpawnVMRequest`
The request schema for `POST /vms`. See **Spawn VM (create-if-needed wrapper)** above for the full field list.

### `PlaygroundCaptureOut`
Returned by `POST /vms/{vm_id}/captures`. See **Trigger Capture** above.

### `PlaygroundAcquisitionOut`
Returned by `GET /vms/{vm_id}/captures`. See **List Captures** above.
