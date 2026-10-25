# ADR: VM-Based ISF Generation

## Status

Accepted.

## Context

Volatility 3 Linux analysis requires an ISF that matches the exact kernel that produced the memory dump. Cloud image labels and catalog metadata are useful, but they are not enough: package updates, image rebuilds, and custom kernels can change the running kernel.

AstinusLab already provisions fresh VMs for acquisition. That lifecycle gives the runner a live system from which it can detect the real kernel, install debug packages when needed, and build a matching ISF before the VM is destroyed.

## Decision

Fresh VMs remain the core architecture for Linux ISF generation.

- AstinusLab detects the running kernel from the guest.
- AstinusLab may fetch distro debug packages on the host when repositories provide them.
- AstinusLab may install debug packages inside the guest when the live VM path is needed.
- Generated ISFs are global reusable cache artifacts under `vol3/symbols/linux/`.
- Cache reuse is gated on both **kernel release** and **build ID**. A cached ISF for the same `uname -r` but a different GNU build ID (vendor backport, security rebuild) is treated as a cache miss and rebuilt. When the build ID of the cached ISF is unknown (pre-existing artifact) a soft-hit is applied: the cached ISF is accepted and the build ID is recorded for future comparisons.
- Each job records which ISF it produced or reused, including the build ID.
- Volatility plugin output remains job-local under `storage/dumps/<job_id>/vol3/`.
- Explicit ISF-only jobs fail when no valid ISF is produced.

## Consequences

The cache avoids rebuilding symbols for repeated jobs with the same kernel, while job-local analysis output preserves per-run evidence. The system supports major x86_64 Linux distributions only; non-x86_64 targets are out of scope.
