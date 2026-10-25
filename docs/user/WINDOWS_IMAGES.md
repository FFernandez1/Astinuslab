# Windows Server 2022 images

AstinusLab has an experimental path for **operator-prepared Windows Server 2022
images**. The operator installs and prepares Windows; AstinusLab's runtime is
designed to create disposable instances and configure access through the QEMU
Guest Agent (QGA).

!!! warning "Current status — experimental runtime and memory acceptance verified"

    The reviewed Server 2022 Standard Evaluation bundle, build **20348.5622**,
    passed native runtime acceptance on two independent clones on September 23,
    2026: unattended preparation, strict SSH, identity/key/ACL persistence through
    reboot and teardown isolation. A separate disposable clone passed real memory
    capture/publication, same-boot resume and Windows Volatility Info, PsList and
    PsTree analysis. Encryption remains **unknown**. Windows disk acquisition,
    workloads and release certification remain unverified. No release-supported
    Windows image or one-command Windows image builder is provided.

    This guide describes development work. An installed release must contain the
    experimental Windows runtime before it can use this path; adding the
    documentation alone does not enable Windows targets.

    Use this guide to understand the preparation requirements and review gates.
    A successful installation, manual login, metadata check or unit-test run does
    not make an image ready for automated runs. Registration requires the exact
    accepted bundle, its final provenance and operator approval; the acceptance
    run did not change the live catalog.

## Choose your starting point

| What you have | Your next step |
| --- | --- |
| A Windows Server 2022 installation ISO | [Prepare a reference VM](#prepare-a-reference-vm). An ISO is installation media, not a runtime base image. |
| A working Windows VM or qcow2 | Preserve it as a reference. Check its hardware, disk layout and guest services before creating a disposable sealing candidate. |
| A generalized image without acceptance evidence | Obtain a reviewed disposable validation procedure. Do not infer readiness from the filename or sidecar. |
| An image with independent real-host acceptance evidence | Follow [Use an accepted image](#use-an-accepted-image), including the provenance and compatibility checks. |

The Linux `precook`, `prepare_target_image.sh` and workload-baking recipes do not
prepare Windows. Windows does not consume the Linux cloud-init seed. Renaming a
disk or changing its distro field cannot convert it into a reusable Windows image.

## Before you begin

Complete the host setup in [Getting Started](../usage/getting-started.md) and
[Running Locally](../usage/running-locally.md). The host needs working KVM/libvirt,
OVMF firmware, image storage accessible through the existing privileged helpers,
and capacity for the preserved reference, recovery copy and disposable clones.
If the host runs inside another VM, hardware virtualization must also be available
to that guest.

Obtain Windows Server 2022 media through your Microsoft licensing or evaluation
channel. You are responsible for choosing the edition, reviewing the terms and
ensuring your use is permitted. AstinusLab does not supply installation media,
product keys, activation or license automation.

Obtain the Windows VirtIO drivers and QGA installer from the
[virtio-win project's distribution links](https://github.com/virtio-win/virtio-win-pkg-scripts/blob/master/README.md).
Record the exact media version used. Select drivers suitable for your Windows
version and devices; do not bypass driver-signature enforcement to install them.

### Current experimental hardware profile

| Setting | Required profile |
| --- | --- |
| Guest OS | Windows Server 2022, x86_64; libvirt OS variant `win2k22` |
| Disk | qcow2 with **exactly 40 GiB virtual capacity**: `42949672960` bytes |
| Partitioning | Healthy GPT layout whose partitions and backup GPT fit the actual disk |
| Machine and firmware | Q35 with OVMF UEFI; an independent NVRAM file for each instance |
| Secure Boot / TPM | The current experimental profile declares both absent/disabled; images requiring them are outside this profile |
| CPU and memory | Runtime defaults are 4 vCPUs and 8192 MiB RAM; leave additional resources for the host |
| Guest access | QGA plus Windows OpenSSH Server; runtime account `Administrator` |
| Network and console | A reviewed private lab network; VNC/noVNC accessible only through loopback/private access |

Choose this profile for a **new isolated lab reference**. Do not disable security
features on an existing production VM or host to make it fit. If your requirements
need Secure Boot or TPM, stop and use a separately reviewed profile rather than
changing the metadata to pass preflight.

Virtual capacity is different from the qcow2 file's current size on the host. Do
not simply shrink an 80 GiB qcow2 to 40 GiB: its partitions and backup GPT may still
refer to the old disk end. Create a correctly sized disk initially, or have a
separate disposable migration reviewed and validated.

## Prepare a reference VM

These steps prepare a development reference. They do not seal or register it.

1. **Create a new VM in your libvirt manager.** Give it a distinct reference name,
   select the hardware profile above, allocate a new 40 GiB qcow2 and separate
   NVRAM, and leave autostart off. Add the Windows ISO and VirtIO driver media.
   Configure the QGA virtio-serial channel named `org.qemu.guest_agent.0`.
2. **Install Windows through the private console.** Select your Server 2022 edition.
   If Setup cannot see the virtual disk, use **Load driver** and select the matching
   storage driver from the VirtIO media. Finish the initial reference installation.
   Enter any initial Administrator credential privately; do not put it in a shared
   script, answer file, sidecar, terminal transcript or support message.
3. **Install the required guest components.** Install the matching VirtIO storage,
   network and serial drivers, then QGA. Check that the QGA service starts
   automatically and the host can reach it through the agent channel. QGA access
   does not require opening a guest TCP port. The
   [QGA package description](https://packages.fedoraproject.org/pkgs/qemu/qemu-guest-agent/)
   documents the channel used by the agent.
4. **Install OpenSSH Server.** Use Microsoft's
   [Windows OpenSSH installation instructions](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_install_firstuse).
   Install the Server feature, not only the client. Its installer creates an SSH
   firewall rule; review access against your private lab network policy. Keep
   Defender, authentication and host isolation enabled. Do not bake a user's
   authorized key or a private key into the reusable artifact. The sealing review
   must specify how SSH stays stopped until instance access is configured.
5. **Record a bounded baseline.** Record edition/build/revision, disk capacity and
   GPT health, QGA/OpenSSH installation and startup configuration, installed roles,
   pending restart state and your Windows Update choice. Updates are optional;
   observe the pending update count rather than guessing zero. Keep an unknown
   encryption state marked unknown. Do not retrieve recovery protectors or
   credentials to fill in the record.
6. **Shut down and preserve the reference.** Wait until libvirt reports it off and
   confirm no relevant QEMU process remains. Make recovery copies only while cold.
   Preserve the original disk and NVRAM. Use a separately named descendant for
   later sealing or repair; never generalize the preserved reference.

In an already authorized reference session, this PowerShell query displays only
fixed disk fields:

```powershell
Get-Disk | Select-Object Number, Size, PartitionStyle, IsBoot, IsSystem, OperationalStatus, HealthStatus
```

Expect the OS disk's `Size` to be `42949672960` and `PartitionStyle` to be `GPT`.
This live observation complements an offline partition-layout check; it is not
proof that an earlier disk-resize operation repaired every GPT structure.

**Checkpoint:** you have a preserved, bootable reference with the required guest
components. It is still a development/recovery asset, even if you can log in.

## Review a disposable sealing candidate

Before any cleanup or generalization, prepare an operator-reviewed plan bound to
one exact source and one new disposable candidate. The plan must identify:

- Source disk identity, observed shutdown state and the artifacts to preserve.
- New candidate name, UUID, MAC, disk location and independent NVRAM.
- Exact cleanup targets, including inherited SSH host keys, authorized keys and
  cached answer files; how their ancestry and absence will be checked safely.
- The unattended configuration for the selected Windows edition/build and setup
  passes, including locale and the operator's license-term decision.
- How guest access will be initialized without embedding reusable credentials.
- The exact Sysprep operation, its supervision, clean-shutdown deadline and
  post-seal validation. Generalization must run only on the disposable candidate.
- Stop conditions and retained evidence if any phase fails.

Microsoft describes generalization as removing installation-specific information
for image deployment, and warns that some configured Windows Server roles may not
survive it. Review those limitations for your reference before sealing.
[Sysprep overview](https://learn.microsoft.com/en-us/windows-hardware/manufacture/desktop/sysprep--system-preparation--overview)

!!! warning "Sealing is a review gate"

    There is no validated, general-purpose AstinusLab sealing command to copy
    from this page. The reviewed binary ACL setter has passed isolated NTFS and
    candidate placement checks. Do not relax file permissions
    or reuse an old experimental helper to get past that gate. A failed one-use
    procedure is preserved and reviewed; its attempt marker is never reset.

Keep the resulting sealed source off. First boot belongs to a disposable copy,
because Windows Setup changes the disk during specialization.

## Prove unattended first boot

Windows Setup uses answer files to apply configuration during installation and
later setup passes. Their discovery and caching depend on the setup path; attaching
an ISO containing `Autounattend.xml` is not evidence that Windows applied it.
[Windows Setup automation](https://learn.microsoft.com/en-us/windows-hardware/manufacture/desktop/windows-setup-automation-overview)

The repository's unattended configuration builder is preparation code, not a
validated disk writer or a complete first-boot deployment mechanism. Have the
candidate-specific procedure validate placement, permissions and execution before
relying on its generated files.

On a **new disposable copy**, the reviewed acceptance procedure must establish:

1. Setup reaches completion without console clicks, license-page interaction or
   manually entered credentials. **Hi there / Next** is an incomplete OOBE screen.
2. QGA is available, and bounded setup observations show `IMAGE_STATE_COMPLETE`,
   `SystemSetupInProgress = 0` and `OOBEInProgress = 0`. The current runtime waits
   for 30 seconds of continuous agent readiness before querying unfinished Setup,
   restarting that interval after an agent outage or an incomplete observation.
   It then requires two complete observations on the same boot before configuring access.
3. Any initialization receipt is bound to this instance and agrees with independent
   observations. A receipt alone is not acceptance.
4. Access is configured using the selected public SSH key; the computer name and
   SSH host keys belong to this instance. Any required identity reboot completes.
5. Strict SSH host verification succeeds against the fingerprint obtained through
   QGA, and the instance shuts down within the reviewed deadline.

If QGA is unavailable or Setup remains incomplete, stop the acceptance attempt.
Do not bypass the setup gate or manually finish OOBE and report an unattended pass.
If the VM remains running, obtain a state-appropriate shutdown procedure before
copying, hashing or registering its disk.

Repeat acceptance with **two independent runtime clones** from the unchanged sealed
source. Verify different names, MACs, NVRAM and SSH host keys, correct public-key
access, identity/key persistence through reboot, and that tearing down one clone
does not affect the other or the source. Record actual evidence before declaring
the base reusable.

## Use an accepted image

This section applies **only after** independent real-host acceptance and operator
approval. It does not authorize importing the current development candidate.

1. **Obtain the complete handoff.** You need the finalized cold qcow2, its observed
   SHA-256, verified public setup CD, acceptance report and matching schema-v3 `.meta.json` sidecar. An ISO,
   a reference backup or a JSON file asserting success is insufficient.
2. **Place the bundle in the configured images directory.** With all relevant VMs
   off, use the installation's reviewed image import procedure and a new filename.
   The default source location is `images/definitions/`; see
   [storage configuration](../usage/configuration.md). For example, a disk named
   `windows-server-2022-accepted.qcow2` needs an adjacent
   `windows-server-2022-accepted.qcow2.meta.json`. Never replace a base used by
   existing overlays. Uploading a disk alone does not supply Windows provenance.
   The CD filename in `first_boot.media` names a sibling file; paths and symlinks
   are refused. Source files/directories must be owned by root or the runtime
   operator and must not be writable by group or others.
3. **Check discovery and metadata.** Open **Images**, then check the target catalog.
   Windows images with missing or invalid explicit provenance require operator
   action; automatic inspection cannot create the missing acceptance evidence.
   The generic metadata editor and **Re-inspect** are not Windows sealing tools.
4. **Select the active key in Security.** Keep private key material on the host.
   The runtime injects the corresponding public key through QGA after Setup is
   complete; there is no Administrator-password bootstrap field to fill in.
5. **Use the target ID actually returned by the catalog.** Choose a scenario whose
   guest operations were validated for Windows. Linux shell workloads, package
   installers and Linux ISF generation are not interchangeable with Windows tasks.
   Do not override compatibility checks to make a combination selectable.
6. **Start a disposable run only under the accepted procedure.** Confirm Setup,
   identity and strict SSH checks complete, then perform the validated acquisition
   and teardown. Preserve failure evidence and the source if a check fails.

You can inspect catalog availability without starting a job. Define `api_curl` as
shown in [API examples](../usage/examples.md), then:

```bash
api_curl "$BASE_URL/api/v1/targets"
api_curl "$BASE_URL/api/v1/scenarios"
api_curl "$BASE_URL/api/v1/workloads"
```

Discovery can enable an image that passes the metadata gate. The `reusable` flag
is a **metadata classification**, not an independent disk-content or real-host
acceptance test. Keep the acceptance report with the image.

### What the runtime handles

The implemented Windows path validates provenance and backing-disk bytes, uses a
per-job disk overlay, NVRAM and verified read-only setup CD copy, assigns a unique
MAC and computer name, and waits for QGA and Setup. It validates the protected
initialization receipt and account continuity, then restricts/verifies the cached
answer file with the exact reviewed native ACL before installing a public key or
starting SSH. It verifies the new
identity after a required reboot and pins the QGA-reported host fingerprint before
strict SSH access. It does not use Linux cloud-init to configure Windows access.
Only harmless readiness pings are retried on the typed agent-unresponsive error;
an uncertain guest-process launch/status stops the operation.

Windows memory analysis uses Windows Volatility plugins and matching Windows
symbols. The Linux `isf-only` workflow is not a Windows symbol-generation step;
see [Volatility 3](VOLATILITY3.md). A successful boot alone does not establish that
a workload or forensic workflow works on Windows.

### Acquisition limits

Windows Job disk acquisition requires a shut-down VM. The live disk helper uses
Linux guest commands, so Windows live-disk requests are rejected. Combined
memory/disk requests are also rejected before capturing memory: capture memory
while the VM is running, then shut it down cleanly before a separate disk capture.
Memory capture uses the host's libvirt dumper. The reviewed bundle passed native
memory capture, integrity/publication checks, same-boot resume and strict SSH,
followed by Windows Info, PsList and PsTree analysis of the retained dump. Disk
acquisition still requires separate native acceptance.

The offline disk helper checks capacity for raw conversion and compressed
publication before writing a capture. A 40 GiB disk requires approximately
**80.9 GiB free** at that point; budget separately for retained memory dumps and
other evidence. Do not remove preservation artifacts or bypass this check to
start an acceptance run.

## What belongs in the sidecar

Create provenance from observed preparation and acceptance evidence, not by
copying a passing test fixture. The current contract includes:

| Fields | Expected evidence or value |
| --- | --- |
| `schema_version`, `platform`, `source` | `3`, `windows`, `operator_prepared` |
| `distro`, `version`, `family`, `os_variant` | `windows-server`, `2022`, `windows`, `win2k22` |
| `default_username`, architecture | `Administrator`; x86_64, with consistent architecture fields |
| `edition`, `build`, `revision`, `detected_at` | Actual observed version and observation time; revision is a non-negative integer |
| `generalization`, `shutdown_state` | `generalized`, `shut_off`, backed by preparation and clean-shutdown evidence |
| `virtual_size_bytes` | Exactly `42949672960` |
| `firmware` | `machine: q35`, `firmware: ovmf`, `secure_boot: false`, `tpm: false`, `nvram: per_instance_copy` |
| `guest_access` | `method: openssh`, `bootstrap: qga`, `key_in_image: false`, `host_keys: regenerate_per_instance`; no password bootstrap |
| `windows_update` | `policy: optional` and an observed non-negative integer `pending_count` |
| `sha256` | Digest of the finalized cold artifact: 64 lowercase hexadecimal characters |
| `first_boot` | Exact `profile: server2022-unattend-v1`, public 32-hex `nonce`, and `media` object |
| `first_boot.media` | Sibling `.iso` `filename`, measured `size_bytes` and `sha256`, `label`, and `initializer_sha256` matching the reviewed initializer for that nonce |
| Linux-only fields | No Linux `kernel_release` or `cloud_init_profile`; `inject_network_config: false` |

If an observation is unavailable, leave the artifact unaccepted and resolve it.
Do not substitute `0`, `generalized` or an invented digest to satisfy validation.
An unknown encryption state remains unknown; these fields do not prove otherwise.
Schema-v2 Windows declarations remain readable but require operator action;
they cannot bypass the first-boot gate. Do not upgrade them by changing only the
version number. The current profile contains the reviewed Server 2022 parent
ACLs in application code; arbitrary ACLs, scripts and credential fields are not
accepted in `first_boot`. The public nonce is a receipt binding, not a password
or cryptographic attestation. Linux schema-v1 behavior is unchanged.

## Troubleshooting and stop conditions

| Symptom | What to do |
| --- | --- |
| Image is listed but cannot be selected | Check explicit Windows provenance and preflight reasons. A bootable extension is not proof of reusability. |
| VM shows **Hi there / Next**, license terms or an account page | Setup is waiting for input. Treat unattended acceptance as failed; record only non-sensitive screen labels. |
| QGA is unavailable | Review the agent service, VirtIO serial driver and channel on the authorized diagnostic clone. Do not open new guest ports to replace the channel. |
| Setup observation is invalid or times out | Preserve the result and request a bounded diagnostic. Do not collect raw guest logs or skip the gate. |
| SSH host fingerprint differs | Stop and preserve evidence. Never disable strict checking or blindly accept a replacement key. |
| Disk is 40 GiB but GPT is suspect | Review offline geometry on a disposable descendant; do not repair or shrink the preserved source in place. |
| Offline ACL/file-placement check fails | Stop before writing setup data. Do not loosen permissions, retry a consumed helper or claim that the file is safely staged. |
| Shutdown deadline expires | Do not copy or hash the disk. Confirm state and obtain a reviewed recovery procedure; do not assume force-off is a clean shutdown. |

For a preparation review, provide only the bounded result, exact failed phase,
observed VM state and the candidate/source identifiers. Keep credentials, private
keys, raw guest logs and credential hives out of documentation and support reports.
