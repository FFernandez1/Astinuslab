# AstinusLab

AstinusLab is an attack-and-forensics playground for local security research. Build vulnerable web-app labs, attach attacker VMs, run attacks, capture RAM/disk evidence, and analyze memory with Volatility 3.

Use Playground first for guided interactive labs. Use Jobs and current JSON Playground recipes for repeatable scenario-driven acquisitions with reproducible metadata, manifests, and SHA-256 checksums.

---

## Get started

*   [Playground](user/PLAYGROUND.md) - Build vulnerable labs, attach attacker VMs, and capture evidence on demand.
*   [Getting Started](usage/getting-started.md) - Install prerequisites, start services, and run your first acquisition.
*   [Running Locally](usage/running-locally.md) - Full development and release runbook.

## Guides

*   [Configuration](usage/configuration.md) - App settings, launcher environment, and catalogs.
*   [Examples](usage/examples.md) - Common UI and API workflows.
*   [Troubleshooting](usage/troubleshooting.md) - Resolve common setup, networking, and permission issues.
*   [Pre-baked Images](user/PREBAKED_IMAGES.md) - Optimize performance by baking workloads into base images.
*   [Windows Images](user/WINDOWS_IMAGES.md) - Prepare a Windows Server 2022 reference, understand acceptance gates, and use an independently accepted image (experimental).
*   [Volatility 3](user/VOLATILITY3.md) - Build reusable ISFs and run job-local memory analysis.

## Reference

*   [API Overview](api/overview.md) - Base URLs, schemas, and security model.
*   [API Endpoints](api/endpoints.md) - REST endpoints verified against the source.
*   [API Errors](api/errors.md) - Common HTTP statuses and response behavior.
*   [V1 Scope and Roadmap](development/ROADMAP.md) - Current product boundaries and deferred work.
*   [Advanced Configuration](reference/ADVANCED_CONFIG.md) - Detailed configuration reference.
*   [Distro Support](reference/DISTRO_SUPPORT.md) - Supported x86_64 Linux families for Volatility 3 symbols.
*   [Forensic Integrity](reference/FORENSIC_INTEGRITY.md) - Hashing, canonical filenames, and evidence-handling details.

## Architecture

*   [Architecture Overview](architecture/overview.md) - System responsibilities and boundaries.
*   [Components](architecture/components.md) - Backend, runner, catalog, storage, and UI modules.
*   [Data Flow](architecture/data-flow.md) - Job, playground, and artifact flows.
*   [Decisions](architecture/decisions.md) - Maintainer-facing design decisions.

## Development

*   [Development Setup](development/setup.md) - Local environment and repository workflow.
*   [Testing](development/testing.md) - Test, lint, type-check, and CI commands.
*   [Contributing](development/contributing.md) - Contribution workflow and standards.
*   [Workload Development](development/WORKLOADS.md) - Package repeatable lab scenarios and service stacks.

## Community

*   [Governance](community/GOVERNANCE.md) — How project decisions are made.
