# Portable package evidence contract

P1–P3 use `astinuslab-lab-package-host-evidence/v1`, with
`contract_revision: 2`. Earlier receipts lack required observations and cannot
satisfy the strengthened gate. This track does not change the seven workflow
rows in the [release acceptance procedure](RELEASE_ACCEPTANCE.md).

The collector validates consistency of private observations. A green validator
does not authenticate an operator's statements: retain and inspect the actual
command logs, source/helper manifests, host identity records, network receipts
and inventories. Fixtures in `tests/python/tooling` are synthetic and never
native acceptance evidence.

## Native lifecycle producer

Use an approved Ubuntu 24.04 x86_64 installation of the frozen candidate. Keep
fake mode unset. Set the installed environment's actual data, database and storage
paths explicitly; pytest's default temporary paths are not connected to the
installed worker. Verify the imported `astinuslab` path and helper identities
before execution. Enable Packages only in the designated acceptance installation.

`ASTINUSLAB_REAL_HOST=1` and `ASTINUSLAB_REAL_PACKAGE_E2E=1` opt into the
destructive test. Additionally set `ASTINUSLAB_REAL_PACKAGE_FRESH_STORAGE=1`
**only after** verifying dedicated disposable storage, a fresh package identity
and resource ownership. This row deletes the imported package and runs zero-grace
GC. Never point it at the retained failed attempt or shared operator storage.

Supply the genuine locked export in `ASTINUSLAB_REAL_LAB_PACKAGE` and a new
private output path in `ASTINUSLAB_REAL_PACKAGE_LIFECYCLE_JSON`. The native test
checks live domain disks, NIC order/membership and network subnet/forwarding
against the recorded launch intent. It maps database rows by VM label and network
identity, never by row order. Each launch and reset phase contains:

| Field | Observation |
| --- | --- |
| `state` | Exactly `ready` |
| `boot_layers` | One `{vm_id, sha256, size_bytes}` for every declared VM; positive observed size and digest matching the archive |
| `topology.networks` | Logical `{id, subnet_cidr, isolated}` records |
| `topology.nics` | `{vm_id, network_id, ordinal}` using definition IDs, not database IDs |
| `readiness` | One `{vm_id, probes, passed: true}` per VM, using the real readiness checker; empty declared probes still require a running domain and guest IP |
| `checkpoints` | Every declared `{operation, passed: true}`, evaluated using bounded guest observations; an empty list is valid only when none are declared |

Reset also contains `previous_session_residue`. Teardown must be `destroyed`.
Both residual inventories require explicit empty `domains`, `networks`,
`overlays`, `reservations` and `temporary_paths` lists. The harness checks both
session snapshots, live libvirt lookups, session database rows, current session
directories and auxiliary disk/NVRAM paths; lookup permission/transport errors
are failures, not proof of absence. `resource_snapshots` preserves the actual
identities privately. Teardown records `retained_asset_digests` for all referenced
content before the separate deletion/GC assertions. Failure cleanup cannot publish
a successful lifecycle receipt. The test does not itself impose host networking
restrictions and therefore alone cannot prove P3.

## Installed identity and command provenance

After the non-skipped lifecycle command passes, prepare a private JSON provenance
document from actual receipts. Required fields are:

| Field | Requirement |
| --- | --- |
| `candidate_sha`, `archive_sha256` | Full lowercase candidate SHA and digest of the exact archive |
| `host_id_sha256` | Digest of stable physical/virtual host identity evidence, independently verified; changing labels is not a new host |
| `installation_id_sha256` | Digest of the independently verified installation identity |
| `identity_receipt_sha256` | Digest of the retained host/installation identity receipt |
| `installed_source` | `{candidate_sha, verified: true, manifest_sha256}` from installed-source verification |
| `installed_helpers` | `{verified: true, manifest_sha256}` from installed-helper verification |
| `command` | `{exit_code: 0, passed: positive_integer, failed: 0, skipped: 0, receipt_sha256, lifecycle_sha256}`; the last digest hashes the exact lifecycle JSON file bytes |

Keep raw machine IDs and installation receipts private. Do not invent IDs or
derive them solely from `host-a`/`host-b`. Collect on each corresponding host:

```bash
./tools/release/collect_lab_package_host_evidence.sh create \
  --archive "$PKG_REFERENCE_ARCHIVE" \
  --lifecycle "$PKG_ACCEPTANCE_DIR/$PKG_HOST_LABEL-lifecycle.json" \
  --provenance "$PKG_ACCEPTANCE_DIR/$PKG_HOST_LABEL-provenance.json" \
  --candidate-sha "$PKG_CANDIDATE_SHA" --host-label "$PKG_HOST_LABEL" \
  --output "$PKG_ACCEPTANCE_DIR/$PKG_HOST_LABEL-package-evidence.json"
```

The collector binds all declared observations to the archive, including the
runbook. `compare` revalidates both manifests and their derived comparison data;
both host and installation identities must differ, while candidate, archive and
all logical observations agree. Private physical identity never enters logical
reproducibility comparison. Preserve the raw files alongside the manifests.

## P3 receipt

Create a private JSON document with `schema_version` as above,
`contract_revision: 2`, `kind: "offline-performance"`, `candidate_sha`,
`archive_sha256`, `host_id_sha256` and `installation_id_sha256`. The receipt
also contains `profile`, `creator` and exactly three `runs` with `cache_mode`
equal to `cold`, `cached` and `unavailable`.

`profile` records `os_id: "ubuntu"`, `os_version: "24.04"`,
`architecture: "x86_64"`, `cpu_threads`, `memory_mb`, `free_disk_bytes`,
`guest_memory_mb`, `vm_count`, `compressed_content_bytes` and `receipt_sha256`.
The bounded lab has one or two VMs, at most 4096 MiB configured guest memory
and 10 GiB compressed package/cache content. Record actual host capacity/load;
minimum host capacity is four CPU threads, 8192 MiB RAM and 40 GiB free storage.
Larger hardware must remain visible in the profile and owner assessment.

Every timed workflow has `start_monotonic`, `end_monotonic`, ordered `phases`
with `{name, start_monotonic, end_monotonic, outcome}`, `included_work` equal to
`["hashing", "queue_waits", "readiness"]`, and `excluded_work`. Times use the
same host monotonic clock, are finite/non-negative, and form contiguous positive
intervals spanning the total. Queue waiting belongs inside its phase; do not
restart timing when a worker starts. Record raw commands and clock observations.

The `creator` phases are `build`, `verify`, `export`, all `passed`. Its only
exclusions are `["initial_asset_downloads", "custom_runbook_authoring"]`.
It includes `candidate_sha`, `archive_sha256`, `source_verified: true`,
`helpers_verified: true`, `installed_source_manifest_sha256`,
`installed_helper_manifest_sha256`, and `receipt_sha256` from real creator
verification. The complete duration must be **under 600 seconds**.

Each run excludes no work (`excluded_work: []`) and contains
`command_receipt_sha256`, `cache_receipt_sha256`, `cache_before` (observed digest
list), and `network`. Cold cache is empty and the package is fully embedded;
cached content includes every required layer. Each positive run has a `manifest`
path to its own independently produced P1 manifest on this host/installation;
its command digest must match that manifest's provenance. Reusing one lifecycle
or command for both runs is rejected. Phase names are `inspect`, `import`,
`prepare`, `preflight`, `launch`, `checkpoints`, `reset`, `teardown`, all `passed`.
Launch ends only after real readiness. Cached start-to-launch-end must be
**under 300 seconds**. Preserve the separate complete cold-cache measurement.

The `network` object contains:

- `applied_monotonic` and `restored_monotonic`, spanning the entire workflow.
- `applied_receipt_sha256`, `restored_receipt_sha256` and
  `continuous_receipt_sha256`, covering the applied/restored rules and evidence
  of continuous restriction (counters or capture). Restoration is mandatory.
- Four `probes`: DNS and TCP (`kind: "dns"` or `"tcp"`) at `stage: "before"`
  and `stage: "after"`. Each has `monotonic`, `blocked: true` and
  `receipt_sha256`. Before probes fall between restriction application and run
  start; after probes fall between run end and restoration. Raw receipts must
  identify actual external targets and failure results; demonstrate target
  reachability before restriction to distinguish a broken target from blocking.

Apply restrictions only under the reviewed disposable-host procedure with
out-of-band access. `network_opt_in: false`, a guest-only firewall, or a JSON
flag is not external-network evidence.

The `unavailable` control uses a separately genuine locked thin export with the
same VM/layer/topology/readiness/checkpoint contract, omitting one required layer
from both archive and cache. Record `control_archive` (private path),
`control_archive_sha256`, `reference_archive_sha256`,
`unavailable_asset_sha256`, `failure_code: "required_asset_unavailable"`,
`downloads_attempted: 0`, and an empty five-category `residue` inventory.
Its phases are `inspect`, `import`, `prepare`, `teardown`; only Prepare has
`outcome: "expected_failure"`. The wrapper command must successfully assert this
negative result and cleanup, not silently skip it.

The control's `provenance` uses the installed-identity structure above, bound
to the control archive and same candidate/host/installation. Its
`command.lifecycle_sha256` hashes the compact sorted JSON object containing
exactly `control_archive_sha256`, `reference_archive_sha256`,
`unavailable_asset_sha256`, `failure_code`, `downloads_attempted`, and `residue`
(UTF-8, `sort_keys=True`, `separators=(",", ":")`, no trailing newline).
Its command digest must match the run and be distinct from both positive runs.
Do not create the control by hand-editing a native attestation.

```bash
./tools/release/collect_lab_package_host_evidence.sh validate-p3 \
  --receipt "$PKG_ACCEPTANCE_DIR/p3.json" \
  --candidate-sha "$PKG_CANDIDATE_SHA"
```

Preserve failures and original configuration/timer state. Source changes require
a new frozen candidate and new acceptance receipts. Missing host authorization,
independent installations or genuine creator receipts leave P1–P3 open.
