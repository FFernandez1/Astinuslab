# Portable lab packages

**Experimental and unaccepted for 1.0.0.** Portable packages are disabled by
default and excluded from supported first-release workflows. End-to-end,
two-host, offline and performance acceptance (P1–P3) remains open. This page
documents retained experimental code for controlled testing, without those
guarantees. A **Locked** label does not certify release acceptance.

The **Verification attempt** selector reopens running, failed and recently cancelled attempts with a safe reason and copyable verification ID. **Completed verifications** shows the package title, version and actual UTC export deadline for each verified attempt. Expired or unavailable attestations stay readable with Export disabled; rebuild the authored lab and verify again after expiry. Export refusals appear beside the affected row, preserving your embedding and redistribution choices.

Portable lab packages are intended to let one local AstinusLab installation share a versioned
lab experience with another local installation. The archive contains the
logical topology, host-resource guidance, runbook, licensing metadata, content
digests, and (when permitted) immutable boot layers. It is a local exchange
format; it is not a cloud service.

For controlled experimental testing only, enable it in `astinuslab.toml`:

```toml
[packages]
enabled = true
```

Then open **Packages** in the sidebar. The UI exposes the experimental workflow; the
REST endpoints are listed at the end of this page for automation.

## Creator workflow

1. Enable packages and restart the API/application so the gated routes and sidebar entry are registered. Build and validate the lab in Playground; keep that authored session open.
2. Open **Packages → Create a package**. Enter the package ID, version, title, creator and license, then **Load from current Playground**. This generates canonical `LabDefinition` JSON and fills the matching authored-session ID without freezing or changing the lab. Resource estimates are derived from declared VM sizes; review and edit them. Changing the metadata form requires loading again; advanced edits go in the generated JSON.
3. Add the runbook, objectives, hints, checkpoints and supporting-file references. Objectives/hints are JSON string arrays; checkpoints are typed guest observations/assertions, never host shell hooks. Observation files are finite JSON under `/var/lib/astinuslab/observations` in the guest, read through QEMU Guest Agent with a 64 KiB limit. The image author must prevent symlinks from that directory to other guest paths: QGA enforces a lexical root.
4. Choose **Save draft and verify clean run** while the source session is still open. Verification automatically freezes it, runs asynchronously, and cleans up the source after completion or failure. Do not choose **End Session** before verification. Follow progress and any failure or cancellation result.
5. Successful attempts remain in **Completed verifications**, including after source cleanup or a browser reload. Export the chosen verification there; its ID binds the exact attested definition, runbook and boot layers. Embedding is off by default. If the attestation requires embedded layers, enable embedding and explicitly confirm redistribution rights; otherwise export is refused with `embedding_required` or `redistribution_unconfirmed`. This never silently changes the verified content.
6. For topology and instructions only, export an **Unlocked** package from a live Playground session. It is explicitly non-reproducible and does not prove identical starting bytes.

A non-blank runbook is required before verification. Export within 24 hours of successful verification: attestations then expire and export returns `attestation_expired`. Keep the downloaded archive. To obtain a new attestation after expiry, rebuild the authored lab and verify again; the cleaned source session cannot be recovered from the completed record.

A Playground recipe JSON describes a visual topology for import back into Playground. A canonical `LabDefinition` JSON is the creator input, with package identity, resources and experience. An `.astinuslab` archive is the exchange package consumed by **Import / inspect**. These formats are not interchangeable; loading from Playground performs the conversion.

Package inspection and rendering are passive: runbook Markdown and guest content are not executed.

## Recipient workflow

**Use a package** is the default view. **Open package** in the local library
reopens verified retained instructions without uploading or importing again.
Prepare, Preflight, Download and Delete belong to that selected package. Runtime
panels retain the name of their own package even when another package is open.

1. In **Import / inspect**, select an `.astinuslab` archive. Inspection validates
   paths, limits, canonical metadata, signatures, and SHA-256 inventory without
   creating a VM. Nothing is imported until **Import inspected package** is
   pressed.
2. Review the package card’s identity, signature/trust state, license,
   reproducibility label, and archive size.
3. Choose **Prepare**. Embedded bytes are preferred, then the local content
   cache, then explicitly pinned sources. Source-network access is opt-in and
   remains subject to local policy. Embedded or cached bytes are prerequisites
   for offline use; the complete offline workflow is unaccepted.
4. Run **Preflight** and read the retained-storage versus launch-storage
   estimate, host capabilities, topology, bridging/reachability, and warning
   codes. Every warning and guest-egress acknowledgement must be checked before
   Launch. The server recomputes preflight at launch.
5. Launch returns a durable operation handle. Follow its status until `ready`,
   then choose **Open lab in Playground**. The page checks that the instance's
   session is still current before navigating. An old instance cannot redirect
   you into another lab. For **Cleanup incomplete**, retain the instance ID and
   check **Logs**; resources may remain reserved. No repair action is implied.
   **Reset** recreates writable overlays from the verified immutable boot layer;
   **Teardown** releases the running instance while retaining content referenced
   by the package. Delete and garbage collection remove retained content only
   after the final reference and lease disappear.

## Experimental acceptance profile

Future native acceptance targets Ubuntu 24.04 LTS on x86_64 with KVM. No host is
currently accepted for the complete portable workflow; later Ubuntu versions
and other Linux distributions are also unverified. A small lab commonly suggests 2–4 CPU threads, 4–8 GB
RAM, and 40 GB free storage. Each package declares its own minimum and
recommended resources; preflight treats the minimum as a gate and the
recommendation as a warning.

## Trust, content, and limitations

Packages are untrusted by default. The inspection view distinguishes unsigned,
invalid, valid-but-untrusted, and locally trusted signatures. A valid Ed25519
signature proves package provenance and bytes, not that a vulnerable service is
safe or that its claims are correct. Review licensing before embedding or
redistributing third-party images and workload data.

The experimental implementation is local-only and does not provide cloud publishing or
multi-host coordination. Host KVM/libvirt policy, firmware, bridge support,
disk capacity, and the package’s declared topology still determine whether a
recipient can launch. P1–P3 must pass on one frozen candidate before this workflow
can enter supported scope; an unlocked export is only a topology/runbook exchange.

## API appendix

The same lifecycle is available under `/api/v1/lab-packages`:

```text
POST /inspect
POST /imports/{inspection_id}/commit
GET  /
GET  /{record_id}
GET  /{record_id}/experience
DELETE /{record_id}
POST /{record_id}/prepare
GET  /prepare/{attempt_id}
POST /{record_id}/preflight
POST /{record_id}/launch
GET  /operations/{instance_id}
POST /operations/{instance_id}/{reset|teardown}
POST /gc
POST /definitions/from-session
POST /drafts
POST /verify-clean-run
GET  /verifications/{attempt_id}
POST /verifications/{attempt_id}/cancel
POST /verifications/{attempt_id}/export
GET  /activity
```

Mutating requests require the normal AstinusLab CSRF and, when configured,
administrator authentication. API clients should preserve idempotency keys for
Prepare and Launch retries and should never treat an accepted operation as
ready until its status says so.
