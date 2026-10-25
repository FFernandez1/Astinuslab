# Backend Architecture & Development

AstinusLab's backend is a high-performance, asynchronous system designed to manage the full lifecycle of forensic lab VMs.

## Tech Stack

*   **Language**: Python 3.12 / 3.13 (tested matrix)
*   **Web Framework**: FastAPI (Asynchronous REST API + Jinja2 UI templates)
*   **Task Queue**: Celery (Asynchronous background job orchestration)
*   **Database**: SQLite (SQLAlchemy ORM)
*   **Virtualization**: libvirt (QEMU/KVM)
*   **Communication**: SSH (for guest interaction), virtiofs (for host-to-guest file sharing)
*   **Image inspection**: `virt-inspector` from `libguestfs-tools` when available, with filename heuristics as fallback

---

## Host Prerequisites for ISF Extraction

ISF symbol extraction (Volatility 3) requires the following packages on the
host. The orchestrator runs a preflight check at job start and aborts with an
actionable message if any are missing.

```bash
sudo apt install \
  rpm2cpio cpio \
  libguestfs-tools \
  qemu-utils \
  libvirt-clients
```

`rpm2cpio` is required for extracting `vmlinux` from Fedora/RHEL kernel-debuginfo
RPMs. `libguestfs-tools` provides `virt-customize` and `virt-inspector`.
`qemu-utils` provides `qemu-img`. `libvirt-clients` provides `virsh`.

---

## Architecture Overview

```text
+-----------------------+
|  Frontend UI / HTMX   |
+-----------------------+
            |
            v
+-----------------------+
|      FastAPI API      |
+-----------------------+
     |             |
     v             v
+----------+  +-------------------------------+
|  SQLite  |  |  Celery Broker (SQLite queue) |
+----------+  +-------------------------------+
                               |
                               v
              +-------------------------------+
              |  Celery Worker / Orchestrator  |
              +-------------------------------+
                 |             |             |
                 v             v             v
          +------------+  +----------+  +----------+
          |  libvirt / |  | Guest VM |  |  SQLite  |
          |    QEMU    |  | via SSH  |  |          |
          +------------+  +----------+  +----------+
```

Application settings are loaded from `astinuslab.toml` in the source checkout /
install directory. `src/astinuslab/core/config.py` also applies
`ASTINUSLAB_<SETTINGS_FIELD>` environment overrides for fields on the flattened
`Settings` model.

### Core Components

| Component | Responsibility |
| :--- | :--- |
| **API (`api/routes.py`)** | Handles REST requests, input validation, and job submission. |
| **Domain services (`jobs/job_services.py`, `forensics/forensic_services.py`, `forensics/case_services.py`)** | Orchestrate business logic across repositories and tasks. |
| **Repository (`src/astinuslab/db/repositories.py`)** | Manages DB persistence and enforces the **Job State Machine**. |
| **Job-run flow (`orchestration/runners/boot_capture_runner.py`, `orchestration/deps.py`)** | The engine for VM lifecycle (Boot, Wait, Capture, Teardown). |
| **VM Lifecycle (`virtualization/vm_lifecycle.py`)** | Low-level libvirt domain XML generation and disk overlay management. |
| **Workload Support (`workloads/workload_support.py`)** | Handles script delivery, execution, and health monitoring. |
| **Playground package (`playground/`)** | Owns Playground API assembly, routes, orchestration, persistence helpers, workloads, reconciliation, and JSON recipes. Root-level `playground_*.py` facades have been removed. |

---

## 1. The Job State Machine

The state machine is the source of truth for all job activities. Transitions are strictly enforced in `JobRepository.transition_job_status()`.

**Key States:**
*   `queued`: Initial state.
*   `preparing_image`: Creating QCOW2 overlays and cloud-init seeds.
*   `waiting_for_guest_access`: VM started, waiting for IP resolution and SSH.
*   `mounting_output_share`: Preparing the virtiofs or SSH mount for artifact delivery.
*   `generating_isf`: Building or reusing a Volatility 3 ISF without capturing memory/disk.
*   `running_scenario_script`: Executing custom post-boot or scenario scripts.
*   `provisioning_workload`: Executing install scripts and verifying health.
*   `waiting_for_capture`: VM ready for the operator.
*   `capture_requested`: Acquisition task triggered via UI or API.
*   `acquiring_memory`: Running `virDomainCoreDump` (RAM capture).
*   `acquiring_disk`: Running `qemu-img convert` (Disk capture).
*   `publishing_artifact`: Finalizing and moving captured artifacts to host storage.
*   `cancel_requested`: Pending cancellation before full teardown. Note that `cancel` and `force-fail` actions now immediately and forcefully terminate any underlying active OS subprocesses running in the worker for that job.
*   `completed` / `failed` / `cancelled`: Terminal states.

---

## 2. Background Task Execution

Long-running operations (like VM booting or memory dumping) are dispatched as Celery tasks.

*   **Broker**: SQLite-based (via `kombu.transport.sqlite`).
*   **Task Definition**: Located in `src/astinuslab/jobs/celery_app.py`; `src/astinuslab/jobs/celery_worker.py` is the backward-compatible import shim.
*   **Mapping**: `TASK_MAP` maps action types to Python functions.

---

## 2a. Logging

`core/logging_config.py` owns the configuration. Both the API and the Celery
workers call `configure_logging()`, so one policy covers every process.

**Format.** `[orchestration] log_format` selects `text` (default, the human
format the Logs page renders) or `json` (one object per record, fields named).
An unrecognised value degrades to `text` rather than failing startup. Records
also reach a rotating file under `storage/logs/` when `log_to_file` is on;
installed systemd units enable it because their stdout goes to the journal.

**Correlation.** `RequestContextFilter` puts `request_id`, `job_id` and
`action_id` on every record from context variables. HTTP middleware binds the
request id; `task_prerun` binds it in the worker from the published task header,
along with the Job the task is working on. Do not write identifiers into message
prose — set the context and let the filter place them, so they stay filterable.

**Message shape.** Prefer `event_name key=value` (`http_request method=GET
path=/jobs status=200`) over a sentence. The same shape reads correctly in both
formats and survives aggregation.

**Levels.** Choose by what the operator has to do, not by how unusual the event
feels:

| Level | Means | Example |
| :--- | :--- | :--- |
| `DEBUG` | Detail for reproducing a specific failure; off in deployments. | The argv and timeout of a host command. |
| `INFO` | Expected lifecycle. Nothing is wrong. | A capture started; retention deleted an eligible artifact. |
| `WARNING` | Degraded, but the system recovers on its own. | Orphaned jobs found and being recovered; a reconciliation pass skipped. |
| `ERROR` | An operator has to act; something did not happen. | A task failed; package reconciliation failure blocked Playground cleanup. |

The distinction that matters is WARNING against ERROR. A WARNING an operator
cannot act on trains them to ignore the level that is supposed to summon them.
When the same condition is reported twice at different layers, the layer that
already handled it logs INFO and only the unhandled one logs higher.

**Redaction.** Anything that renders a command line for a human — a log record,
an exception message, `HostAction.error_summary` — goes through
`process_support.redacted_command()`. Keep the deny-list narrow: an
over-redacted line no longer explains the failure it was written to explain.

---

## 3. Host-Guest Communication

AstinusLab avoids "heavy" agents inside the guest. Instead, it uses:
*   **SSH**: Primary transport for command execution.
*   **virtiofs**: High-performance host directory sharing (mounted at `/mnt/astinuslab-out` in the guest).
*   **QEMU Guest Agent (Optional)**: Used for graceful shutdowns and advanced metadata collection if present.
*   **Power-state polling**: `virsh domstate` reads are used to detect VM power state changes.

---

## 4. Development Workflow

### Adding a New API Endpoint
1.  Define the Pydantic schema in `src/astinuslab/api/schemas.py`.
2.  For Playground endpoints, add route handlers under `src/astinuslab/playground/routes/` and update `src/astinuslab/playground/api.py` only for router assembly or shared route helpers.
3.  For non-Playground endpoints, keep the route in `src/astinuslab/api/routes.py` or the existing domain router.
4.  Implement non-Playground logic in `jobs/job_services.py`, `forensics/forensic_services.py`, `forensics/case_services.py`, or a narrow support module. Playground behavior belongs under `src/astinuslab/playground/`.
5.  Add focused API tests in `tests/python/api/test_api.py` or the relevant Playground test file.

### Adding a New Background Action
1.  Implement the logic in `orchestration/runners/boot_capture_runner.py` or a support module.
2.  Add an action type string to `TASK_MAP` in `src/astinuslab/jobs/celery_app.py`.
3.  Ensure the job state machine in `src/astinuslab/db/repositories.py` supports the required transitions.

---

## 5. Security Mandates

*   **Artifact Sanitization**: Always use `src/astinuslab/jobs/job_services.py:safe_artifact_path` to prevent path traversal when downloading dumps.
*   **Credential Handling**: Never store plaintext passwords in the DB; use the `Keystore` for SSH keys.
*   **Network Isolation**: Use the `iptables` hooks in `workloads/workload_support.py` to ensure vulnerable VMs cannot attack the host or LAN.

---

## 6. Testing

Run the full test suite before committing:
```bash
.venv/bin/python -m pytest tests/python/
```

Individual module tests:
```bash
.venv/bin/python -m pytest tests/python/jobs/test_repositories.py
.venv/bin/python -m pytest tests/python/orchestration/test_lab_orchestrator.py
```
