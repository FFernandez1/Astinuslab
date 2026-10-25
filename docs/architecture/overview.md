# Architecture Overview

AstinusLab is a local lab orchestration system. The API and UI accept operator requests, persist job state in SQLite, enqueue long-running work through Celery, and a host runner controls libvirt, SSH, disk capture, memory capture, and artifact publication.

```text
Browser / API clients
        |
        v
FastAPI -----> SQLite application state
        |
        v
Celery SQLite broker
        |                           |
        v                           v
Lifecycle runner + Beat       Analysis worker
(concurrency 1, threads)       (analysis queue, concurrency 1)
        |                           |
        +--> libvirt / QEMU / KVM    +--> Volatility plugins
        +--> guest setup via SSH
        +--> capture / ISF work
        +--> artifacts and logs <---+--> reports
```

## Boundaries

| Boundary | Responsibility |
| --- | --- |
| FastAPI app | REST API, HTML UI, request validation, job creation, artifact downloads. |
| SQLite database | Job records, transitions, events, host actions, runtimes, acquisitions, playground state. |
| Celery worker | Executes privileged and long-running orchestration actions outside request/response flow. |
| Host helper scripts | Perform restricted root operations through installed sudoers allow-list entries. |
| libvirt/QEMU | VM lifecycle, networks, disk overlays, memory dumps, and disk conversion. |
| Catalogs | Declarative target, scenario, workload, and attacker VM definitions. |
| Storage | Captured artifacts, logs, temporary files, Celery databases, SSH keys, workload cache. |

## Operating Modes

Development mode runs from the repository through `./astinuslab.sh`. Release mode copies the app to `/opt/astinuslab`, creates a virtualenv, installs helper scripts and systemd units, and runs services as `astinuslab-api@USER` and `astinuslab-runner@USER`.

The two modes must not run together because both runners can consume the same queue and control the same host libvirt resources.

## Security Model

AstinusLab has optional single-admin API authentication. It remains intended for trusted local labs by default. Development mode binds to loopback unless you override the bind host; non-loopback binds require API auth or an explicit unsafe override. For shared networks, still put it behind TLS, a reverse proxy, VPN, and firewall restrictions.
