# AstinusLab Volatility 3 Support

This directory contains the current Volatility 3 symbol-building support used by AstinusLab.

Volatility 3 Framework 2.28.0 and `dwarf2json` are installed and licensed by
the operator outside this directory. AstinusLab does not download, vendor, or
install either tool. Configure `volatility_bin`, `dwarf2json_path`, and the
exact `dwarf2json_sha256` pin before building or analyzing; see
[`docs/user/VOLATILITY3.md`](../docs/user/VOLATILITY3.md) for the operator
runbook.

The deprecated case-bundle workflow has been removed. Current operation is direct:

- fetch a distro-provided `vmlinux` on the host when package repositories can provide one
- build a Volatility Linux ISF from that `vmlinux` with `dwarf2json`
- or connect to a live guest and build the ISF from the running kernel
- store reusable symbols under `vol3/symbols/linux/`
- run configured Volatility plugins against a job's memory dump and write output under `storage/dumps/<job_id>/vol3/`

## Layout

```text
vol3/
  README.md
  volatility3-linux-symbol-table-generation.md
  phase1-evidence-log.md
  docs/
  scripts/
    fetch_vmlinux.sh
    build_symbol_from_live_guest.sh
  symbols/
    linux/
```

## Host Package Symbol Flow

Use `fetch_vmlinux.sh` when the guest distro and kernel release are known and debug packages are available from the distro.

```bash
bash vol3/scripts/fetch_vmlinux.sh \
  --work-dir /tmp/astinuslab-vol3-fetch \
  --symbols-dir "$PWD/vol3/symbols" \
  --distro ubuntu \
  --version 22.04 \
  --kernel-release 5.15.0-92-generic \
  --arch x86_64
```

The script prints:

```text
vmlinux_path=/absolute/path/to/vmlinux-<kernel>
system_map_path=/absolute/path/to/System.map-<kernel>
```

`system_map_path` is printed only when a System.map file is found.

Host package downloads are resumable and retried within a single run. ISF-only jobs persist host fetch diagnostics at `storage/dumps/<job_id>/vol3/symbol-build-host.log` so interrupted downloads, missing packages, and extraction failures can be reviewed after the job exits.

AstinusLab then runs the configured, verified operator executable:

```bash
<configured-dwarf2json-path> linux --elf "$vmlinux_path" > vol3/symbols/linux/<kernel>.json
```

## Live Guest Symbol Flow

Use `build_symbol_from_live_guest.sh` when the VM is still reachable and package fetch is not enough.

```bash
bash vol3/scripts/build_symbol_from_live_guest.sh \
  --guest-ip 192.168.122.50 \
  --guest-user ubuntu \
  --ssh-key "$HOME/.ssh/astinuslab_ed25519" \
  --work-dir /tmp/astinuslab-vol3-live \
  --symbols-dir "$PWD/vol3/symbols"
```

The backend passes the target catalog username with `--guest-user`; the script no longer relies on the default user being correct for every image.

## Backend Integration

`src/astinuslab/forensics/vol3_support.py` is the integration point:

- `run_vol3_isf_only(job_id)` reuses a valid cached ISF or fetches `vmlinux` and writes `vol3/symbols/linux/<kernel[_buildid]>.json`
- `run_vol3_symbol_build(job_id)` builds symbols from a live guest runtime
- existing symbols are reused only when the cache identity is acceptable for the detected kernel and known build ID
- explicit ISF-only jobs fail when no valid ISF is produced
- analysis stages the selected ISF into `storage/dumps/<job_id>/vol3/symbols/` and writes plugin output under `storage/dumps/<job_id>/vol3/`

The global ISF cache and job-local plugin output are intentionally separate. Delete job output from `storage/dumps/<job_id>/vol3/` when cleaning a job; delete `vol3/symbols/linux/<kernel>.json` only when you want to force ISF regeneration for that kernel.

## Strict ISF-Only Semantics

ISF-only work is validation work, not best-effort preparation. If AstinusLab is asked to build an ISF without running analysis, the job must produce a valid Linux ISF or fail. A cached ISF satisfies the request only when it is valid for the detected kernel.

The ISF-only host chain is `local_cache -> prebuilt_isf -> debuginfod -> host_fetch`. If it fails, the separate live-guest fallback may use an existing matching DWARF-bearing `vmlinux`, debuginfod, or explicitly permitted debug-package installation. BTF-only input is not supported by `dwarf2json` and is never used for this fallback. Host fetch diagnostics are written to `symbol-build-host.log`; live guest fallback diagnostics are written to `symbol-build-live.log`; provider-chain diagnostics are written to `symbol-diagnostics.json`.

Host-side Debian fetching no longer mutates the host apt configuration. Debian debug symbols are fetched through snapshot.debian.org/direct package URLs only. Live-guest package installation is invasive and disabled by default; set `[vol3].allow_invasive_guest_symbol_install = true` (or `ASTINUSLAB_VOL3_ALLOW_INVASIVE_GUEST_SYMBOL_INSTALL=1`) to pass `VOL3_ALLOW_GUEST_PACKAGE_INSTALL=1` into the guest build script.

## Troubleshooting

- Missing debug symbols: enable the distro debug symbol repository, confirm the exact kernel package exists, or use the live guest flow so packages can be installed in the VM.
- Invalid ISF: remove the generated file from `vol3/symbols/linux/` and rerun the build; check that `dwarf2json` supports the input `vmlinux`.
- Package repository failures: verify guest networking, DNS, repository mirrors, distro release support, and `[vol3].fetch_timeout_seconds` for large debug packages.
- Plugin failures: confirm the memory dump exists, the matching ISF is present, and inspect per-job output under `storage/dumps/<job_id>/vol3/`.
