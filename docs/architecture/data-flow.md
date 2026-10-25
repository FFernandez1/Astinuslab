# Data Flow

## Forensic Job Flow

```text
1. Operator -> FastAPI: POST /api/v1/jobs
2. FastAPI -> SQLite: persist queued Job
3. FastAPI -> Celery broker: enqueue task
4. Lifecycle runner -> SQLite: preparing_image
5. Lifecycle runner -> libvirt: overlay, seed, domain
6. Lifecycle runner -> guest: wait for IP/SSH; run scenario/workload
7. Lifecycle runner -> libvirt: capture memory and/or disk
8. Lifecycle runner -> storage: artifact + manifest + SHA-256
9. Lifecycle runner -> SQLite: completed / failed / cancelled
10. Operator -> FastAPI: download published evidence
```

All job status changes should pass through the repository transition logic. Terminal states are `completed`, `failed`, and `cancelled`.

## Manual Lab Flow

Manual lab jobs use `manual_trigger` scenarios:

1. The runner boots the target VM.
2. The selected workload is installed and health checked.
3. Optional attacker VM runtime metadata is recorded.
4. The job waits in `waiting_for_capture`.
5. The operator calls `/api/v1/jobs/{job_id}/capture` or uses the UI.
6. The runner captures memory, disk, or both.

## Playground Flow

Playground uses the same host runner but a different API surface and state model:

1. Create or reuse one active playground session.
2. Create libvirt-backed session networks.
3. Spawn VMs from catalog images.
4. Attach or detach NICs as needed.
5. Trigger VM-level memory or disk captures.
6. Destroy the session.

Playground VM overlays are ephemeral. Playground capture artifacts persist until explicitly deleted.

## Artifact Flow

Memory capture uses libvirt core dump functionality from the host. Disk capture converts VM disk state to a downloadable artifact. For completed artifacts, AstinusLab stores path, filename, size, and SHA-256 metadata and serves files through artifact endpoints with checksum headers when available.

## Volatility 3 Flow

For Linux memory analysis, AstinusLab identifies the guest kernel and tries host-side ISF providers in order:

```text
local_cache -> prebuilt_isf -> debuginfod -> host_fetch
```

If the host-side chain fails, AstinusLab tries the live-guest fallback, which may use an existing matching DWARF-bearing `vmlinux`, debuginfod, or explicitly permitted debug-package installation from the guest. BTF-only input is not supported by `dwarf2json` and is never used for this fallback. Reusable ISFs are stored in `vol3/symbols/linux/`. Per-job Volatility output is stored under `storage/dumps/<job_id>/vol3/`.
