# Volatility 3

If an accepted analysis shows **Status unavailable**, its outcome is still unknown. Restore the connection or sign in again, then choose **Check status** to read that same run; this does not submit another analysis. Reloading the same tab restores observation of the accepted run and its exact input. Run stays disabled while that result is unresolved. Choosing a different input or plugin changes the context; YARA rule text is not stored for reload.

AstinusLab uses Volatility 3 for Linux memory analysis. That is the supported 1.0.0 path, and in V1 it is core to the Capture → Analyze → Evidence workflow. Windows PDB support is experimental groundwork: the code paths described below exist, but prepared Windows targets and end-to-end Windows analysis are outside supported 1.0.0 scope. The important boundary is:

- Linux ISFs are reusable global cache artifacts under `vol3/symbols/linux/`.
- Windows symbols live under `vol3/symbols/windows/` (see below).
- Plugin output is job-local under `storage/dumps/<job_id>/vol3/`.

## Analyze an exact Playground capture

1. Before ending the source VM, record its capture-time Linux kernel (`uname -r`), architecture (`uname -m`), and how you verified them, tied to the VM/session and acquisition. Preserve build identity and matching symbols when available. A later boot of the same catalog image is not proof of the captured kernel.
2. Complete a **memory** capture in Playground. In **Volatility**, select that exact Playground source; use its artifact/acquisition identity, not merely a similar filename. Disk artifacts cannot be analyzed as memory.
3. When context is required, enter **Captured Linux kernel**, `x86_64` architecture, and a **Verification source / note** explaining the capture-time evidence. Check **I verified this context against the selected capture**, then **Save verified context**. Never check this box on an inferred image version alone.
4. AstinusLab creates a Job with `execution_mode=artifact_analysis`, bound to the original artifact, checksum, session and acquisition. This records derived analysis; it does not boot or recapture a VM. Conflicting kernel context is rejected.
5. Attach/upload an ISF matching that capture. For Playground contexts the file’s Linux kernel and architecture metadata must agree with the verified context; relabeling a mismatched file is rejected. Prepare symbols before capture where possible; an ISF-only job on another boot is useful only if its exact kernel/build identity matches.
6. Select a Linux plugin such as `linux.pslist.PsList`, run it, and wait for its terminal result. Download the report and its manifest/checksum from the analysis Job or Artifact endpoints. Preserve the source memory artifact separately; see [evidence export](../reference/FORENSIC_INTEGRITY.md).

Automation uses `POST /api/v1/volatility/analysis-contexts` with `artifact_id`, `kernel_release`, `architecture: "x86_64"`, `verified: true`, and a nonempty `provenance` note. Use the authentication/CSRF setup in [API examples](../usage/examples.md). A disk capture, missing source bytes, inconsistent ownership, or unverified context is rejected.


## Volatility 101 for AstinusLab

If you are migrating from standard Volatility setups, you might find the requirement for "strict ISF-only jobs" just to get symbols a bit different. In AstinusLab, Linux memory analysis relies on building an Intermediate Symbol Format (ISF) file tailored to the exact kernel running in the VM, rather than guessing profiles.

A typical end-to-end workflow looks like this:

1. **Run ISF-only job to get symbols:** Execute a preliminary job to generate or cache the correct ISF for your VM's kernel.
2. **Run your exploit lab:** Execute your actual scenario, malware, or exploit inside the VM.
3. **Capture Memory:** Acquire the memory dump from the VM.
4. **Analyze:** Run Volatility 3 against the captured memory dump using the generated ISF.

## Windows Symbols (PDB)

Windows analysis does not build a per-kernel ISF from DWARF the way Linux does. Volatility 3 resolves the kernel's symbol table from PDB symbols. AstinusLab sources these in two ways:

- **Microsoft Symbol Server** (default): Volatility 3 downloads the matching PDB-derived symbols from `https://msdl.microsoft.com/download/symbols` on first analysis.
- **Offline PDB/ISF cache**: for air-gapped labs, populate `vol3/symbols/windows/` (or a configured offline cache directory) and AstinusLab prefers it over the network.

Because Windows uses downloaded PDB symbols rather than a guest-built ISF, the Linux ISF-only/live-guest build steps below do not apply to Windows targets. End-to-end Windows analysis needs a prepared Windows image and a captured Windows memory dump. Preparing Windows targets is not an automated 1.0.0 workflow, and this whole surface is experimental: it carries no release acceptance evidence and is not part of the supported 1.0.0 scope.

## ISF Generation

AstinusLab builds an ISF for the exact running kernel of the fresh VM. This is intentional architecture: the VM is the source of truth for kernel release, distro metadata, and package availability.

The production runner can build an ISF by:

- Reusing an existing valid ISF from the local cache when the kernel release and known build ID match.
- Fetching distro debug packages from host-side package repositories. For some distros, the host fetch script may use debuginfod internally when the guest build ID is known.
- Connecting to the live guest and using an existing matching DWARF-bearing `vmlinux`, debuginfod, or explicitly permitted debug-package installation when host fetch cannot produce a valid ISF. BTF-only input is not supported by `dwarf2json` and is never used for this fallback.

Each job records whether it produced a new ISF or reused a cached one.

## Strict ISF-Only Jobs

An explicit ISF-only job must produce or reuse a valid ISF. If no valid ISF is available, the job fails. This prevents later analysis from using missing, stale, or mismatched symbols.

ISF-only jobs use the production host-side provider chain: `local_cache -> prebuilt_isf -> debuginfod -> host_fetch`. If that chain fails, AstinusLab tries the live-guest fallback, which may use an existing matching DWARF-bearing `vmlinux`, debuginfod, or explicitly permitted debug-package installation from the guest. BTF-only input is not sent to `dwarf2json`. Host fetch logs are persisted at `storage/dumps/<job_id>/vol3/symbol-build-host.log`; live fallback logs are persisted at `storage/dumps/<job_id>/vol3/symbol-build-live.log`; provider attempts and skip/failure reasons are persisted at `storage/dumps/<job_id>/vol3/symbol-diagnostics.json`.

Cache identity is build-ID aware. A cached ISF with a matching build ID is reused; a cached ISF with a different build ID is treated as a miss and rebuilt. Legacy cached ISFs with no recorded build ID remain soft hits so older caches keep working, and AstinusLab records the job's known build ID on the new artifact row.

AstinusLab versions before Phase C wrote `vol3/symbols/linux/<kernel_release>.json`. Those files remain readable as legacy soft-hit cache entries when no conflicting build ID is recorded. New build-ID-qualified symbols use `vol3/symbols/linux/<kernel_release>_<buildid-prefix>.json`.

## Operator Tool Runbook

Volatility 3 and `dwarf2json` are external, operator-supplied tools. Read the
upstream VSL and the project inventory in `THIRD_PARTY_NOTICES.md` before
installing them. AstinusLab never accepts upstream licenses on the operator's
behalf, and never downloads, installs, or upgrades the tools. Neither tool is part of the AstinusLab
source tree or installer.

The installed launcher can render the current status plus all recommended,
copy-paste commands for the active single-user, split-user, or development
layout:

```bash
cd /opt/astinuslab/current
./astinuslab.sh guide-volatility
```

This command is read-only. It reports guidance and readiness; it does not
download, install, upgrade, configure, restart, or accept a license for either
external tool.

### Install and verify the tools

Keep both tools in operator-owned, versioned paths outside the AstinusLab tree.
The following is an operator-run example, not installer behavior:

```bash
sudo install -d -m 0755 /opt/volatility3-2.28.0
sudo chown "$(id -un):$(id -gn)" /opt/volatility3-2.28.0
python3.12 -m venv /opt/volatility3-2.28.0
/opt/volatility3-2.28.0/bin/python -m pip install 'volatility3==2.28.0'
/opt/volatility3-2.28.0/bin/vol -q frameworkinfo.FrameworkInfo
```

For this example the banner is `Volatility 3 Framework 2.28.0`. The minimum
accepted version and this release's tested version are both **2.28.0**. Newer
versions are accepted with `untested=true` (`READY (UNTESTED)` in the guide);
run your own regression checks before relying on them. Choose a trusted package
index or source and review its license and package hashes yourself.
Obtain or build `dwarf2json` from its upstream project in a separate path, then
record its upstream tag or commit, artifact origin, architecture, and local
hash. For the audited Linux amd64 release, the expected SHA-256 is
`e2e75ba5bc9c22bc38a48edffbb050456d089d54998d54f46cb580223d9fbc6b`.

- Audited artifact: `https://github.com/volatilityfoundation/dwarf2json/releases/download/v0.9.0/dwarf2json-linux-amd64`
- Pinned source commit: `9f14607e0d339d463ea725fbd5c08aa7b7d40f75`

```bash
sudo install -d -m 0755 /opt/dwarf2json/0.9.0
sudo install -m 0755 /path/to/downloaded/dwarf2json-linux-amd64 \
  /opt/dwarf2json/0.9.0/dwarf2json-linux-amd64
echo 'e2e75ba5bc9c22bc38a48edffbb050456d089d54998d54f46cb580223d9fbc6b  /opt/dwarf2json/0.9.0/dwarf2json-linux-amd64' \
  | sha256sum --check -
sha256sum /opt/dwarf2json/0.9.0/dwarf2json-linux-amd64
/opt/dwarf2json/0.9.0/dwarf2json-linux-amd64 --help
```

### Configure and check readiness

Set explicit paths and the exact `dwarf2json` pin in `astinuslab.toml` (or the
equivalent environment variables). Edit the existing `[vol3]` section, replacing
or uncommenting these keys and preserving other keys. Create the section only if
it is absent; never append a second `[vol3]` table:

```toml
[vol3]
enabled = true
volatility_bin = "/opt/volatility3-2.28.0/bin/vol"
dwarf2json_path = "/opt/dwarf2json/0.9.0/dwarf2json-linux-amd64"
dwarf2json_sha256 = "e2e75ba5bc9c22bc38a48edffbb050456d089d54998d54f46cb580223d9fbc6b"
```

Validate syntax before restarting, using your actual data-root path:

```bash
/opt/astinuslab/current/.venv/bin/python -c 'import pathlib,tomllib; tomllib.loads(pathlib.Path("/opt/astinuslab/astinuslab.toml").read_text()); print("TOML valid")'
```

This checks TOML syntax without changing configuration or running tools. The guide
still prints recovery instructions when configuration cannot be loaded. Correct
syntax and settings errors before restarting the AstinusLab API and workers.
Then check `/api/v1/volatility/readiness` or the Volatility page. Analysis
readiness and ISF-build readiness are independent: existing/manual ISFs can
still be used when `dwarf2json` is unavailable. Run a non-evidentiary smoke case
before relying on the tools, and preserve the configuration and provenance
record for release/incident evidence.

For a default single-identity release, copy the restart and authenticated
readiness check printed by the guide:

```bash
sudo systemctl restart astinuslab-api@"$USER" astinuslab-runner@"$USER" astinuslab-analysis@"$USER"
export ASTINUSLAB_ADMIN_TOKEN='<saved-install-token>'
curl -fsS -H "Authorization: Bearer $ASTINUSLAB_ADMIN_TOKEN" \
  http://127.0.0.1:8000/api/v1/volatility/readiness | python3 -m json.tool
```

`Analysis: READY` means the configured Volatility binary passed its banner
probe. `ISF build: READY` independently means the configured `dwarf2json`
binary passed its path, executable, and SHA-256 checks. One can be ready while
the other is unavailable.

### Failure recovery

Readiness exposes a stable capability code and a safe remediation detail:

- `volatility_not_configured` means set `volatility_bin`; `dwarf2json_not_configured`
  means set both its path and pin.
- `volatility_not_found` or `dwarf2json_not_found` means correct the operator
  path; `volatility_not_executable` or `dwarf2json_not_executable` means fix
  file permissions or select a regular executable file.
- `volatility_version_unsupported` means select a version at or above the
  minimum **2.28.0**. The tested choice is **2.28.0**; newer versions remain
  explicitly untested. `volatility_probe_failed` means inspect the
  protected service logs and rerun the probe manually.
- `dwarf2json_hash_not_configured` means add the exact 64-character SHA-256
  pin. `dwarf2json_hash_mismatch` means the file was replaced or upgraded;
  stop the build, verify provenance and hash, then update the path and pin as a
  matched pair. AstinusLab re-hashes immediately before local execution and
  before guest transfer, so a readiness result is not execution authorization.
- A guest transfer mismatch means discard the transferred file, check the host
  and guest copies against the configured hash, and retry only after both match.
- When a local build is unavailable, use the **Upload ISF** manual alternative
  with a validated ISF; this does not install or fetch `dwarf2json`.

### Upgrade and rollback

To upgrade, stop queueing new analysis, install the replacement in a new
versioned path, verify its license, provenance, architecture, and hash, and run
the supported banner probe plus the applicable regression tests. Update the
configuration atomically, restart services, and verify readiness. Keep the old
path and its provenance record until rollback evidence passes. Never overwrite
an existing tool in place.

To roll back, restore the previous configured Volatility path and
`dwarf2json_path`/`dwarf2json_sha256` pair, restart services, and verify
readiness before queueing work again. Retain both versioned paths until the
incident or release record is complete.

## Manual Override (Upload ISF / Dump)

Sometimes the build-ID-aware cache and live-guest build chain cannot produce symbols you already have — for example you built an ISF out of band, or you want to analyze a memory dump acquired by another tool. AstinusLab exposes two manual overrides:

- **Upload ISF** — lives in the **Volatility** workbench (`/volatility`) under **Symbol tables · ISF**, beside **Build ISF** for each Linux target. It accepts a `.json` Volatility 3 ISF. The file must be a valid ISF (a `metadata.linux` object and a non-empty `symbols` object) and expose a kernel release in `metadata.linux` (for example `metadata.linux.symbols[].name = vmlinux-<kernel-release>`). On success it is placed in `vol3/symbols/linux/`, recorded as an ISF artifact with `source=manual_upload`, and attached to a completed ISF-only Job that AstinusLab auto-creates or reuses for that target.
- **Upload memory dump** — lives on the job detail page (`/jobs/<job_id>`) in the **Manual override (advanced)** card when Volatility 3 is enabled and the job is no longer running. It accepts any file and stores it under `storage/dumps/<job_id>/` as a `kind=memory` artifact with `producer=manual_upload`. The ELF PT_LOAD completeness check applied to host captures is **skipped**; only a non-empty file is required. The newest memory artifact becomes the job's active dump.

Both overrides intentionally bypass the corresponding safeguard, so their provenance is recorded as **unverified**. Use them only when you know the symbols and dump correspond to the job's kernel; mismatched symbols typically fail Volatility's banner check rather than producing silently wrong results.

Endpoints: `POST /api/v1/volatility/targets/{target_id}/isf/upload` (multipart fields `file` and optional `scenario_id`), `POST /api/v1/jobs/{job_id}/isf/upload`, and `POST /api/v1/jobs/{job_id}/dump/upload` (multipart field `file`).

### Run ISF-only from the UI

1. Open **Volatility** in the sidebar (`/volatility`).
2. Select a Linux target.
3. Select an `immediate` build path.
4. Select **Build ISF** or **Upload ISF**.
5. Watch the linked job detail Volatility 3 ISF card or the Volatility build history table.

Jobs still execute the work internally; the Volatility page is the focused symbol-readiness workflow, while **Jobs** stays a run ledger.

### Run ISF-only from the API

Create a Linux-target job with `build_vol3_isf: true`. AstinusLab treats this as an ISF-only job for `immediate` modes. Windows targets reject this flag because Windows analysis uses PDB symbols after memory capture.

```json
{
  "target_id": "ubuntu-22-04-default",
  "scenario_id": "immediate-capture",
  "execution_mode": "immediate",
  "build_vol3_isf": true
}
```

## Analysis Output

When plugin analysis runs, results are written under `storage/dumps/<job_id>/vol3/`. These files belong to that job and can be removed with the job artifacts. They are not the global symbol cache.

`analysis-summary.json` records `dump_path`, `dump_sha256`, `isf_path`, `isf_sha256`, `symbols_path`, and `vol3_version`. During each run AstinusLab stages the selected ISF into a job-local symbols directory and points Volatility 3 at that directory so the recorded ISF is the only ISF available to plugin execution.

Successful analysis also publishes `analysis-summary.json` as a first-class report artifact:

- `analysis-summary.json.manifest.json` uses `artifact-manifest/v1` with `producer: vol3_analysis` and `kind: report`.
- `analysis-summary.json.sha256` contains the SHA-256 sidecar for the summary file.
- The Artifact row records the report file, manifest path, SHA-256 sidecar path, size, and checksum.
- The Report row links the input memory artifact to the output report artifact.

Common plugins include `pslist`, `pstree`, `lsmod`, `mountinfo`, `sockstat`, `ip.Addr`, and `ip.Link`.

### Workbench Plugin-Run Evidence

On-demand single-plugin runs from the **Volatility** workbench (`/volatility`) are also first-class evidence. Each completed run writes its result file at `storage/dumps/<job_id>/vol3/runs/<run_id>.json` and publishes it as a report Artifact with:

- `<run_id>.json.manifest.json` — `artifact-manifest/v1` with `producer: vol3_plugin_run` and `kind: report`.
- `<run_id>.json.sha256` — the SHA-256 sidecar for the result file.
- An Artifact row (report file, manifest path, SHA-256 sidecar path, size, checksum) and a `volatility3_plugin` Report row linking the input memory artifact to the output report.

Download any run's evidence from the results footer in the workbench, or directly via `GET /api/v1/artifacts/{artifact_id}/download` (with `/manifest` and `/sha256` for the sidecars). Evidence recording is best-effort: if indexing fails, the run result still renders and the failure is logged.

### Run Post-Capture Analysis

1. Complete a job with a memory dump.
2. Confirm the job has a valid ISF recorded in the Volatility 3 ISF card.
3. Select **Run Volatility 3** on the job detail page.
4. Review `analysis-summary.json` and per-plugin output under `storage/dumps/<job_id>/vol3/`.

Analysis also requires `settings.vol3_volatility_bin` and `settings.vol3_default_plugins` to be configured.

## Troubleshooting

### Missing debug symbols

Symptoms:

- ISF-only job fails.
- Logs mention missing `vmlinux`, `debuginfo`, `dbgsym`, or debug repository packages.

Fixes:

- Confirm the VM is running a supported x86_64 Linux distribution from `docs/reference/DISTRO_SUPPORT.md`. ARM64 (`aarch64`) architectures are explicitly not supported for ISF generation or memory analysis.
- Confirm the exact kernel release has matching debug packages.
- Enable the distro debug symbol repository.
- Inspect `storage/dumps/<job_id>/vol3/symbol-build-host.log` for the exact host fetch failure.
- Use the live guest fallback when host-side package fetch cannot resolve the package.

### Invalid ISF

Symptoms:

- The JSON file is written but Volatility rejects it.
- Plugin runs fail before producing useful output.

Fixes:

- Remove the bad file from `vol3/symbols/linux/` and rerun generation.
- Check that `dwarf2json` can parse the selected `vmlinux`.
- Confirm the ISF filename and job metadata match the detected kernel.

### Package repository failures

Symptoms:

- Package commands fail during host fetch or live guest build.
- Logs show DNS, mirror, GPG, subscription, or release metadata errors.

Fixes:

- Verify network and DNS from the host and guest.
- Check repository mirror availability.
- Allow enough time for large debug packages; Ubuntu/RHEL symbol packages can exceed 1 GB.
- Tune `[vol3].fetch_timeout_seconds` when mirrors are slow.
- For RHEL, confirm repository entitlement or an equivalent debuginfo source.
- Retry with a supported cloud image release if the distro release is end-of-life.

### Plugin failures

Symptoms:

- ISF generation succeeds but plugin output is missing or incomplete.
- Files under `storage/dumps/<job_id>/vol3/` contain errors.

Fixes:

- Confirm the memory dump exists for the job.
- Confirm the matching ISF exists under `vol3/symbols/linux/`.
- Inspect the job event log and per-job Volatility output.
- Rerun analysis after regenerating the ISF if symbols were stale, invalid, or built for a different kernel build (same `uname -r`, different build ID — e.g. after a vendor security rebuild).
