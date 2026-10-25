# Decisions

This page records current architectural decisions that affect setup, operations, and maintenance.

## Use Host-Side Acquisition

Memory and disk artifacts are captured from the host through libvirt and QEMU tooling. Guest-side agents are avoided for acquisition integrity and distro compatibility.

## Keep One Active Runner Mode

Development and release runners must not run at the same time. Both can consume Celery tasks and operate on libvirt resources, so mixed mode can send jobs to the wrong runner or collide on VM names, networks, and storage.

## Use SQLite for Local State

The project uses SQLite for the application database, Celery broker, and Celery result backend. The supported runner concurrency is `1`, matching the local, single-host design and avoiding shared-state races.

## Use Installed Privileged Helpers

The runner does not receive broad passwordless sudo. It can call specific installed helper scripts under `/usr/local/libexec/astinuslab/` through `/etc/sudoers.d/astinuslab-runner`.

## Treat Catalogs as User-Maintained Source Data

Targets, scenarios, workloads, and attackers are declared in YAML catalogs. Image auto-discovery can add targets, but maintainers should verify usernames, distro family, cloud-init profile, OS variant, and network behavior.

## Make Playground Ephemeral, Keep Captures Durable

Playground sessions, VMs, networks, and disk overlays are temporary. Captures, manifests, checksums, and acquisition rows persist after session destruction.

## VM-Based ISF Generation

Fresh VMs are the source of truth for Linux kernel identity and Volatility 3 ISF generation. See [VM-Based ISF Generation](adr-vm-based-isf-generation.md).

## VM capacity is admission control, not worker concurrency (2026-07)

The configurable limit on simultaneously running VMs (`orchestration.max_concurrent_vms`) is enforced at the product layer by counting `job_vms` rows in capacity-consuming states inside the admitting transaction (`BEGIN IMMEDIATE`). Celery workers stay at `--concurrency=1`: the SQLite broker, in-process capture locks, beat-task serialization, and single-host reconciliation all assume one serial runner, and that invariant is pinned by `tests/python/test_deploy_config.py`.

Default is no cap, preserving pre-existing behavior. Transitional VM states such as `destroying` and `teardown_failed` count toward the cap until reconciliation resolves them.

## Use package, execution-environment, and runtime boundaries for repository layout (2026-08)

The staged repository target uses `src/astinuslab` for the installable application and package-owned web/migration resources, a canonical `tests` hierarchy, `ops` for host/deployment code, and `tools` for contributor/release utilities. It retains `guest`, `config`, `images`, `vol3`, and `storage` as visible first-class boundaries because moving them under generic buckets would add path risk without clearer ownership.

Repository paths are migration interfaces: every move requires its producer/consumer matrix, verification, and rollback. Internal imports and source paths may break within an atomic stage, while HTTP routes, Celery task names, Alembic revision IDs, systemd unit names, configuration keys, artifact/catalog formats, and public static URLs remain stable contracts.

See [Repository placement policy](repository-placement-policy.md), [Repository target layout](repository-target-layout.md), and [Repository path migration matrix](path-migration-matrix.md).
