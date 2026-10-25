# V1 Scope and Roadmap

AstinusLab V1 is an attack-and-forensics playground for local security research. It focuses on building vulnerable web-app labs, attaching attacker VMs, capturing evidence, and analyzing memory with Volatility 3.

## V1 scope

- Localhost/trusted-local operation by default, with optional single-admin bearer-token auth for API access when binding beyond loopback.
- Guided Playground UI as front-door lab workflow.
- JSON recipe import/export for repeatable Playground labs; broader YAML recipe authoring remains roadmap.
- Web-app vulnerable labs first: WordPress, DVWA, Juice Shop, GitLab-style services, and custom Workloads.
- Attacker VM support for pentest-style traffic generation.
- Capture memory and disk artifacts from host side.
- Evidence sidecars: manifests and SHA-256 checksums should be standard across Job and Playground artifacts.
- Volatility 3 is the core V1 analysis path for Linux memory, using Linux ISFs. The Windows PDB/catalog foundations are experimental and sit outside supported 1.0.0 workflows.
- Concurrency policy: one active Playground OR multiple queued Jobs. Default worker concurrency remains one active execution slot.
- Distribution from source checkout plus `ops/deploy/install.sh`.

## Security stance

V1 defaults to trusted local operator and localhost access. Network binds require single-admin API auth unless the operator explicitly sets an unsafe override. For LAN or remote use, still add TLS, firewall/VPN, and reverse-proxy controls around the API/UI.

Guest workloads are intentionally vulnerable. Hostile-guest hardening should be selectable during lab creation as controls mature; use isolated networks when testing exploit chains.

## Roadmap

- Automated Windows target provisioning parity.
- Continuous live telemetry beyond discrete captures.
- Richer generated reports beyond the Volatility summaries and plugin reports already published.
- Richer hostile-guest hardening presets.
- YAML recipe support and fuller recipe authoring UX beyond current JSON import/export.

## Out of scope for V1

- Portable lab packages: disabled, experimental and unaccepted until P1–P3 pass on one candidate. No end-to-end, two-host, offline or performance guarantee is claimed.
- Multi-user auth/RBAC inside AstinusLab beyond the single `admin` API role.
- Cloud-hosted multi-tenant deployment.
- SIEM replacement or always-on monitoring platform.
