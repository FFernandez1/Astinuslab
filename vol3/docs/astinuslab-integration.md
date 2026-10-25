# Vol3 Integration

AstinusLab's current Volatility 3 integration is symbol-focused and local-first. It does not restore the deprecated case-bundle workflow.

The Volatility 3 Framework 2.28.0 executable and `dwarf2json` are independent,
operator-installed dependencies. They must live outside the AstinusLab tree;
AstinusLab never downloads or accepts their upstream licenses. Set
`ASTINUSLAB_VOL3_VOLATILITY_BIN`, `ASTINUSLAB_VOL3_DWARF2JSON_PATH`, and
`ASTINUSLAB_VOL3_DWARF2JSON_SHA256`, restart services, and verify
`/api/v1/volatility/readiness` before using the workflows below. The operator
must retain upstream provenance and the local `dwarf2json` hash.

## Active Backend API

`src/astinuslab/forensics/vol3_support.py` provides:

| Symbol | Purpose |
|--------|---------|
| `is_vol3_enabled()` | Reads the Vol3 feature flag. |
| `run_vol3_isf_only(job_id)` | Builds an ISF from a host-fetched `vmlinux`. |
| `run_vol3_symbol_build(job_id)` | Builds an ISF from a reachable live guest. |

## Direct ISF Build

For completed jobs, `run_vol3_isf_only(job_id)` reads detected distro, version, and kernel release from the job record. It calls `vol3/scripts/fetch_vmlinux.sh`, then runs `dwarf2json` and writes the final symbol to `vol3/symbols/linux/<kernel>.json` or `vol3/symbols/linux/<kernel>_<buildid>.json` when a build ID is known.

The backend creates `vol3/symbols/linux/` before writing output so a fresh checkout does not fail on the first symbol build.

The backend resolves and re-checks the configured `dwarf2json` executable
immediately before local execution. The live-guest path transfers only that
operator-selected file and verifies the same configured hash on both sides.

## Live Guest ISF Build

For running jobs with a target runtime, `run_vol3_symbol_build(job_id)` reads the target IP and resolves the SSH username from the target catalog. It invokes:

```bash
bash vol3/scripts/build_symbol_from_live_guest.sh \
  --guest-ip <ip> \
  --ssh-key <private-key> \
  --work-dir <tmp-dir> \
  --symbols-dir <vol3-symbols-dir> \
  --guest-user <target-default-username>
```

This avoids assuming every guest user is `ubuntu`.

## Storage

Reusable Linux symbols live under:

```text
vol3/symbols/linux/<kernel-release>[_<buildid>].json
```

Fetched `vmlinux` and `System.map` files may also be stored under `vol3/symbols/` for reuse.

## Operational Rules

- Existing symbols are reused when both the kernel release and the GNU build ID match. A cached ISF with a mismatched build ID (same `uname -r`, different kernel build) is treated as a cache miss and rebuilt. When the cached ISF has no recorded build ID (pre-existing artifact) a soft-hit is applied.
- Unsupported distro fetches skip symbol generation rather than invalidating the acquisition.
- Only `x86_64` architectures are supported. ARM64 (`aarch64`) is explicitly not supported for Volatility 3 integration.
- Vol3 remains local-first and does not introduce authentication or remote service changes.
- Volatility plugin execution is supported through the job-local workbench API and writes run files under `storage/dumps/<job_id>/vol3/runs/`.

## Deprecated History

Older docs described a case-oriented workflow with per-case manifests and wrapper orchestration. That workflow has been removed from active operation. Keep any remaining historical artifacts separate from the current direct symbol path.
