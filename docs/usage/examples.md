# Examples

These examples assume the API is running at `http://localhost:8000`.

The jobs, job logs, cases, artifacts, reports, acquisitions, and host-actions collection endpoints return JSON arrays
and accept `limit` and `offset` query parameters. The default page size is 100 and the maximum is 500. Fetch the next
page by increasing `offset` by the requested `limit` until a page contains fewer rows than requested. For example:
`GET /api/v1/jobs?limit=500&offset=500`.

## Authentication and CSRF Setup

AstinusLab protects mutating API endpoints (POST, PUT, PATCH, DELETE) against CSRF. In release mode (or when API auth is enabled), requests must supply a valid Bearer token. In development mode without API auth, requests must seed an `astinuslab_csrf` cookie via `GET /playground` and echo it in the `X-CSRF-Token` header.

Define this reusable `api_curl` Bash helper before running the examples below:

```bash
BASE_URL="${BASE_URL:-http://localhost:8000}"
# Set ASTINUSLAB_TOKEN to the configured admin token for authenticated installs.
# Leave it unset only for local development with authentication disabled.
api_curl() {
  if [ -n "${ASTINUSLAB_TOKEN:-}" ]; then
    curl --fail-with-body -H "Authorization: Bearer $ASTINUSLAB_TOKEN" "$@"
    return $?
  fi
  local cookie_jar csrf_token request_status
  cookie_jar=$(mktemp "${TMPDIR:-/tmp}/astinuslab-cookies.XXXXXX") || return 1
  if ! curl --fail --silent --show-error -c "$cookie_jar" "$BASE_URL/playground" >/dev/null; then
    rm -f "$cookie_jar"
    return 1
  fi
  csrf_token=$(awk '$6 == "astinuslab_csrf" {value=$7} END {print value}' "$cookie_jar")
  if [ -z "$csrf_token" ]; then
    rm -f "$cookie_jar"
    echo "No CSRF cookie: check BASE_URL and authentication mode." >&2
    return 1
  fi
  if curl --fail-with-body -b "$cookie_jar" -H "X-CSRF-Token: $csrf_token" "$@"; then
    request_status=0
  else
    request_status=$?
  fi
  rm -f "$cookie_jar"
  return "$request_status"
}
```

See [Authentication](../api/authentication.md) for token configuration and security details.

## List Catalog Data

```bash
api_curl "$BASE_URL/api/v1/targets"
api_curl "$BASE_URL/api/v1/scenarios"
api_curl "$BASE_URL/api/v1/workloads"
api_curl "$BASE_URL/api/v1/cves"
```

## Create an Immediate Capture Job

```bash
api_curl -X POST "$BASE_URL/api/v1/jobs" \
  -H "Content-Type: application/json" \
  -d '{
    "target_id": "ubuntu-22-04-default",
    "scenario_id": "immediate-capture",
    "execution_mode": "immediate",
    "capture_type": "memory"
  }'
```

## Create a Manual Lab Job

```bash
api_curl -X POST "$BASE_URL/api/v1/jobs" \
  -H "Content-Type: application/json" \
  -d '{
    "target_id": "ubuntu-22-04-default",
    "scenario_id": "lab-session",
    "execution_mode": "manual_trigger",
    "capture_type": "memory",
    "workload_id": "dvwa"
  }'
```

## Trigger a Manual Capture

Use this only after a manual job reaches `waiting_for_capture`:

```bash
api_curl -X POST "$BASE_URL/api/v1/jobs/<job_id>/capture" \
  -H "Content-Type: application/json" \
  -d '{
    "capture_type": "both",
    "exploit_tags": ["manual-lab"],
    "exploit_notes": "Captured after operator activity"
  }'
```

## Download Artifacts

```bash
api_curl -OJ "$BASE_URL/api/v1/jobs/<job_id>/artifact"
api_curl -OJ "$BASE_URL/api/v1/jobs/<job_id>/disk-artifact"
```

The response includes `X-Astinuslab-SHA256` and `X-Astinuslab-Size` headers when checksum and size metadata are available.

## Create a Playground Session

```bash
api_curl -X POST "$BASE_URL/api/v1/playground/sessions"
```

## Create a Playground Network

The create-or-reuse endpoint creates a session if needed:

```bash
api_curl -X POST "$BASE_URL/api/v1/playground/networks" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "internal",
    "subnet_cidr": "10.42.10.0/24",
    "isolated": true
  }'
```

## Spawn a Playground VM

If `network_ids` is omitted, the wrapper creates or reuses the default playground network.

```bash
api_curl -X POST "$BASE_URL/api/v1/playground/vms" \
  -H "Content-Type: application/json" \
  -d '{
    "label": "target-1",
    "image_id": "ubuntu-22-04-default",
    "role": "target",
    "vcpus": 2,
    "memory_mb": 4096,
    "disk_gb": 40
  }'
```

## Capture a Playground VM

```bash
api_curl -X POST "$BASE_URL/api/v1/playground/vms/<vm_id>/captures" \
  -H "Content-Type: application/json" \
  -d '{"kind": "memory"}'
```

List captures for the VM:

```bash
api_curl "$BASE_URL/api/v1/playground/vms/<vm_id>/captures"
```

Stream capture events:

```bash
api_curl -N "$BASE_URL/api/v1/playground/captures/<acquisition_id>/events"
```

## Stop a Playground Session

```bash
api_curl -X DELETE "$BASE_URL/api/v1/playground/sessions/<session_id>"
```
