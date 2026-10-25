# Forensic Integrity & Technical Specification

AstinusLab is designed with a "forensics-first" approach, prioritizing the integrity of captured artifacts and the auditability of the acquisition process.

## 1. Out-of-Band Memory Acquisition

The most critical feature of AstinusLab is its ability to acquire guest memory without running any code inside the guest.

*   **Mechanism**: AstinusLab uses libvirt `virDomainCoreDump` with `VIR_DUMP_MEMORY_ONLY`.
*   **Hypervisor Level**: The capture happens at the QEMU/KVM level. The guest CPU is paused, and the entire physical memory space is serialized to the host filesystem.
*   **Zero Guest Footprint**: Unlike traditional tools (e.g., LiME, AVML) that run as processes or kernel modules inside the guest, AstinusLab does not modify the guest's memory, process list, or kernel structures during acquisition.
*   **Format**: Memory is saved as an **ELF (Executable and Linkable Format)** memory-only dump with `PT_LOAD` segments mapping the guest's physical RAM. CPU register state is not captured; do not expect `NT_PRSTATUS` register notes.
*   **Structural validation**: At capture time the dump's ELF program headers are parsed and the artifact is rejected unless the file contains every `PT_LOAD` segment in full — a dump truncated mid-write never becomes a published artifact.

## 2. Disk Acquisition

Job disk acquisition has two paths:

1. **Powered-off target**: host `qemu-img convert` reads the retained qcow2 disk. The manifest records `capture_method: offline_disk`. Keep the domain and disk available until capture finishes: teardown that removes the disk also removes the capture source.
2. **Running target**: acquisition reads the mounted disk **inside the running guest** and copies it out. The manifest records `capture_method: remote_disk` and a `forensic_caveat`; the Dump Vault labels it "Live copy — non-forensic". Guest activity can change bytes during the read, and the guest mediates the result. It is not a frozen point-in-time forensic snapshot.

Both Job paths produce gzip-compressed raw images and record a raw (pre-compression) SHA-256 alongside the artifact checksum. An offline image avoids the live-copy caveat, but capture method alone does not certify evidence quality or application consistency.

Playground disk captures use a separate path: `pause_convert` suspends the domain for the duration of `qemu-img convert` and attempts to resume it afterwards, producing uncompressed QCOW2. The alternative `blockcopy` mode runs `virsh blockcopy --wait` followed by `virsh blockjob --pivot`; it is a live-copy workflow. Neither path uses a temporary libvirt snapshot. Preserve the capture method and acquisition window when interpreting the artifact.

## 3. Data Integrity & Chain of Custody

AstinusLab automates several steps to ensure the auditability of captured data:

### Checksumming
Immediately after an acquisition task completes, the host-side runner calculates a **SHA-256 checksum** of the resulting file. This checksum is persisted in the database and displayed in the UI. Checksums are also **re-verified at use time**: every Volatility 3 run recomputes the dump (and selected ISF) SHA-256 before execution and refuses to run on a mismatch; results record `input_verified` and the hash computed at use.

Job disk artifacts (`.img.gz`) are gzip-compressed; their manifest additionally records `integrity.raw_sha256`, the SHA-256 of the raw image reported by acquisition. Verify it with `gzip -dc <artifact>.img.gz | sha256sum`. The artifact checksum covers the compressed file, whose bytes can differ between compressors. Playground disk artifacts are uncompressed QCOW2; verify their artifact checksum directly rather than using gzip.

### Manifest sidecars
Job, Playground, and report artifacts use the same evidence pattern: artifact file, `.manifest.json` metadata file, and `.sha256` sidecar written beside the artifact. The manifest schema is versioned as `artifact-manifest/v1`; it records producer, kind, path, filename, SHA-256, size, job/session/acquisition/case links when present, and input artifacts for derived outputs such as reports. Capture manifests additionally record the real acquisition window: `captured_at_utc` is taken immediately before the memory dump or disk conversion starts and `capture_finished_at_utc` immediately after it completes, while `created_at_utc` is the manifest build time. Non-capture producers (reports, derived outputs) record `null` for the capture fields.

Acquisition filenames encode the guest OS so dumps are self-describing. Job captures name memory dumps `<distro>-<version>-kernel-<kernel>-memory.dump` (matching the sibling `<distro>-<version>-kernel-<kernel>-disk.img.gz`); Playground captures name them `<distro>-<version>-mem-<vm_id>-<timestamp>-<acq_id>.elf` (and `…-disk-…qcow2` for disks). When the OS cannot be resolved the prefix falls back to `unknown`.

Current sidecar naming:

- Artifact manifest: append `.manifest.json` after the artifact suffix, for example `ubuntu-22.04-kernel-5.15.0-91-generic-memory.dump.manifest.json` or `analysis-summary.json.manifest.json`.
- SHA-256 sidecar: append `.sha256` after the artifact suffix, for example `ubuntu-22.04-kernel-5.15.0-91-generic-memory.dump.sha256` or `analysis-summary.json.sha256`.
- SHA-256 sidecar content: `<sha256>  <artifact filename>`.

The durable data model separates investigation grouping from files:

- `Case`: optional container for an investigation or lab effort.
- `Acquisition`: capture lifecycle record that may belong to a case and produce artifacts.
- `Artifact`: concrete file plus provenance, checksum, manifest path, and SHA-256 sidecar path.
- `Report`: derived analysis output, such as a Volatility 3 summary, linked to input and output artifacts.

### Retention

Retention is evidence-aware: capture artifacts linked to an Acquisition or a non-closed Case, or whose job is not terminal, are never auto-deleted. Eligible memory and disk artifact deletions commit their custody records before removing the file, manifest, and SHA-256 sidecar, and are logged as `retention_deleted` job events. Disk artifacts are subject to the same retention window as memory artifacts. Orphan files without database records are managed from the Dump Vault, not by retention.

### Evidence path permissions

Captures are written by an unprivileged service identity, but qemu reads and writes them as
`libvirt-qemu`, so evidence directories carry narrow POSIX ACLs. Those ACLs are applied by a
root-executed helper that takes the target path only: the trust root comes from the root-owned
policy at `/etc/astinuslab/acl-policy.json`, so a compromised runtime cannot point the helper at an
arbitrary directory and have root grant itself access there. Registration is an operator action
(`ops/deploy/configure-privileged-roots.sh`); see
[Advanced Configuration](ADVANCED_CONFIG.md).

Two properties matter for evidence handling:

- **Grants are verified, not assumed.** The helper walks by directory descriptor, checks each
  component's owner and write bits through it, and reads the ACL back afterwards, failing if the
  mask clamped an entry or if anything inside the target changed inode under it.
  A grant that is listed but not effective is a failure, so a capture never proceeds on a path
  qemu cannot actually use.
- **Evidence directories are created with an explicit mode.** Capture directories are created at
  `0750` instead of inheriting the umask, and write bits are stripped from components that already
  exist. A group-writable ancestor — what a developer shell's `umask 0002` produces — is refused by
  the helper, so the mode is set rather than left to chance. The storage root itself is never
  modified: the installed layout sets it to `2770` on purpose, and the policy names that group in
  `storage_groups`.

The install tree itself is not writable by the runtime identity: source and `.venv` stay
installer-owned, and only `storage`, `config`, `images/definitions`, and `vol3/symbols` are
runtime-writable. Acquisition and analysis code therefore cannot be modified by the processes that
handle evidence, and `UMask=0027` on the service units keeps new evidence files off world-readable
modes.

### Evidence deletion

Every deletion path — retention, the Dump Vault, and any API caller — goes through
one custody-aware service. It refuses to remove an artifact whose job is not
terminal, whose Acquisition still owns it, or whose Case is open or sealed, and the Dump
Vault now reports that refusal to the operator instead of quietly reporting
success. Retention still logs its removals as `retention_deleted` job events;
other callers log `evidence_deleted`.

Before anything is unlinked, a **tombstone** row records the artifact id, path,
SHA-256, size, job/acquisition/case links, the exact file set, the actor, the
reason, and whether custody protection was overridden. The tombstone and the
removal of the Artifact row commit together, and only then are the bytes removed:
a crash in between leaves files an operator can find through the tombstone, where
the other ordering would leave a database row pointing at bytes that no longer
exist and reading as intact evidence.

Cleanup records start as `pending` and become `complete` or `failed` after a
separate cleanup transaction. Retention retries pending and failed records on
its next non-dry run, including those left by an interrupted API or UI deletion.
It refuses a path whose recorded file identity changed or which is referenced by
a live artifact; failure details remain in the tombstone notes for review.
Metadata-only deletion records `retained` and never schedules file removal.
Historical tombstones are marked `legacy` and never authorize new cleanup.

Job deletion returns 409 while artifacts, reports, or retained analysis context
still belong to the job, including metadata-only requests. Eligible evidence is
removed separately through Storage. Released capacity and confirmed runtime
cleanup are also required before removing job metadata. Surviving reports and
publication attempts have references to removed artifacts cleared; deleted
report outputs retain their history with status `deleted`. Migration `a040` repairs dangling
references from earlier deletions. Its downgrade refuses pending or failed
cleanup intents: resolve those records through cleanup and review before
downgrading, or use the documented database backup/restore recovery procedure.
Alembic commits each revision separately: a blocked multi-revision downgrade can
stop at `a040` after removing newer schema, including browser sessions. Inspect
the recorded revision before retrying or restoring the backup.

Path checks are not overridable. A symlink standing in for the artifact is
refused rather than followed, sidecars must sit in the artifact's own directory
inside a trusted evidence root, and `force` overrides custody policy only — it is
recorded in the tombstone along with the protection it bypassed.

### Event Logging (Audit Trail)
Every action taken by the orchestrator is logged in the `job_events` table with a high-precision timestamp. This includes:
*   VM Boot time.
*   Workload installation logs (including `shellcheck` output).
*   The exact moment acquisition started and finished.
*   The transition to terminal states.

### Metadata Collection
AstinusLab attempts to collect guest metadata (kernel version, distribution, CPU architecture) via the QEMU Guest Agent and `uname` calls *before* the capture. This metadata is stored alongside the artifact to assist analysts in selecting the correct Volatility profiles or symbol tables.

### Evidence export

Export an inventory of the selected evidence using `GET /api/v1/artifacts/{artifact_id}` and its `/download`, `/manifest`, and `/sha256` endpoints. Include source captures as well as derived analysis reports; verify the downloaded bytes against their SHA-256 sidecars. Metadata responses expose artifact identity/checksum, not host-side sidecar paths.

For a filesystem export, Jobs use `<storage_dir>/dumps/<job_id>/`; Playground uses the independently configured `<playground_capture_dir>/<session_id>/<vm_id>/` (default `storage/playground/captures`). Copy the relevant artifact files and sidecars from both roots as applicable, and reconcile them with your artifact/acquisition inventory. Copying only Jobs output omits Playground source memory. This is an evidence export, not a restorable application backup. Never copy all of `storage/`: `storage/ssh_keys/` contains guest SSH credentials.

## 4. Volatility 3 Compatibility

By providing memory dumps in ELF format along with captured register states and detected kernel versions, AstinusLab artifacts are natively compatible with [Volatility 3](https://github.com/volatilityfoundation/volatility3).

*   **Linux**: The ELF headers provide the physical layer mapping.
*   **Windows**: Experimental. Windows targets acquired via libvirt dump line up with the Volatility 3 `windows.memmap` physical layer and PDB symbol resolution, but that path is outside supported 1.0.0 scope and is not covered by release acceptance.

### External tool provenance

Volatility 3 Framework 2.28.0 and `dwarf2json` are operator-supplied external
tools. The operator installs them outside the AstinusLab tree, reviews the VSL
and upstream notices, and records the source, architecture, version, and exact
`dwarf2json` SHA-256. AstinusLab does not download the tools or accept upstream
licenses on the operator's behalf. A readiness check is not a substitute for
the immediate hash verification performed before execution or guest transfer.

### Analysis Provenance

Each Volatility 3 analysis run writes `analysis-summary.json` under `storage/dumps/<job_id>/vol3/`. The summary records the memory dump path and SHA-256, the selected ISF path and SHA-256, the job-local symbols directory passed to Volatility 3, and the Volatility 3 version string reported by the configured binary. Each plugin's published `.txt` contains Volatility 3 stdout only; progress output and framework warnings are segregated to a sibling `<plugin>.stderr.log`, recorded per plugin as `stderr_path` in the summary, so evidence files are deterministic and diffable across runs.

AstinusLab stages only the selected ISF into the job-local symbols directory before plugin execution, runs Volatility 3 with a job-local `--cache-path` (wiped before each run) so a stale banner cache cannot redirect symbol resolution, and saves each run's resolved configuration (`--save-config`) beside the outputs. After every Linux run the saved config's resolved `isf_url` entries are checked against the staged directory: a symbol table resolved from anywhere else fails the run, and a positive match is recorded as `isf_pinned` in the result, the analysis summary, and the report manifest's ISF input entry.

Analysis summaries and workbench run results also record `isf_source`, the provenance of the symbol table (`host_fetch`, `debuginfod`, `prebuilt_isf`, `live_guest`, and similar sources), resolved through cache hits to the original build source. Batch analysis and interactive workbench runs select the ISF through the same rule (build-ID match first, then the job's own valid ISF, then any valid ISF for the kernel release), so both surfaces always attest the same symbol table for a given dump. Results produced with a `live_guest` ISF are flagged low-trust in the workbench because the symbol table was produced by the analysis subject itself and could misdirect interpretation; prefer host-fetched or debuginfod symbols where available.

## 5. Network Isolation

To prevent "pollution" of the forensic environment and to ensure the security of the host, AstinusLab applies host-side `iptables` rules:
1.  **FORWARD chain**: All traffic from the guest to the LAN or Internet is dropped by default if `network_isolation` is enabled.
2.  **SSH exception**: A specific rule allows the host (and only the host) to communicate with the guest's SSH port for orchestration purposes.
3.  **Fail closed**: if a workload requests `network_isolation` and the iptables helper cannot apply it, the lab boot is aborted and the job fails with an `isolation_failed` event — the lab never comes up un-isolated while claiming containment.
