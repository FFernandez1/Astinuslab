# Volatility 3 Rollout

AstinusLab now supports a direct Volatility 3 symbol workflow. The old case-bundle and config-orchestrated workflow is deprecated and is not part of current operation.

Before rollout, follow the [Volatility 3 operator runbook](../../docs/user/VOLATILITY3.md):
read the VSL and `THIRD_PARTY_NOTICES.md`, install Volatility 3 Framework 2.28.0
and `dwarf2json` outside the AstinusLab tree, record provenance and the exact
`dwarf2json` SHA-256, configure both paths, restart services, and verify
`/api/v1/volatility/readiness`. The installer never downloads these tools and
does not accept their licenses for the operator.

## Current Strategy

Keep acquisition and symbol generation separate:

- AstinusLab acquisition produces memory artifacts and records detected distro, version, architecture, and kernel release.
- Vol3 support builds reusable Linux ISF symbols under `vol3/symbols/linux/`.
- Volatility plugin execution is not coupled to job teardown or capture-critical runner stages.

## Supported Symbol Paths

### Host package fetch

Use `vol3/scripts/fetch_vmlinux.sh` when the target distro publishes debug kernel packages.

```bash
bash vol3/scripts/fetch_vmlinux.sh \
  --work-dir /tmp/astinuslab-vol3-fetch \
  --symbols-dir "$PWD/vol3/symbols" \
  --distro ubuntu \
  --version 22.04 \
  --kernel-release 5.15.0-92-generic \
  --arch x86_64
```

The script emits `vmlinux_path=...` and, when present, `system_map_path=...`. The backend then runs `dwarf2json` and writes `vol3/symbols/linux/<kernel>.json` or `vol3/symbols/linux/<kernel>_<buildid>.json` when a build ID is known.

### Live guest build

Use `vol3/scripts/build_symbol_from_live_guest.sh` while the target VM is reachable.

```bash
bash vol3/scripts/build_symbol_from_live_guest.sh \
  --guest-ip 192.168.122.50 \
  --guest-user ubuntu \
  --ssh-key "$HOME/.ssh/astinuslab_ed25519" \
  --work-dir /tmp/astinuslab-vol3-live \
  --symbols-dir "$PWD/vol3/symbols"
```

The backend resolves the target catalog's `default_username` and passes it as `--guest-user`.

## Backend Behavior

`src/astinuslab/forensics/vol3_support.py` owns the current integration:

- `run_vol3_isf_only(job_id)` fetches `vmlinux` from host-accessible package sources and writes an ISF.
- `run_vol3_symbol_build(job_id)` connects to a live target runtime and asks the guest to build an ISF.
- `_cached_symbol_path_for_job(kernel_release, build_id)` skips repeat work when the cache identity is acceptable.

Symbol failures are analysis failures, not acquisition failures. A completed acquisition remains valid even when symbols cannot be generated.

## Output Layout

```text
vol3/symbols/
  linux/
    <kernel-release>[_<buildid>].json
  vmlinux-<kernel-release>
  System.map-<kernel-release>
```

Temporary fetch/build work should live under `/tmp` or another caller-provided work directory.

## Compatibility Notes

- Ubuntu and Debian use debug package repositories when available.
- RPM-family distributions rely on debuginfo packages or debuginfod where supported.
- Unsupported distros should skip symbol generation without corrupting acquisition state.
- Linux ISF generation requires a debug-capable `vmlinux` matching the exact running kernel.

## Removed Workflow

The historical case directory workflow and its wrapper scripts are deprecated. Do not add new code or docs that depend on that workflow. If old artifacts still exist locally, treat them as historical analysis output, not current runtime state.
