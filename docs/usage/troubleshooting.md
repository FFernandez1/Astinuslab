# Troubleshooting

## Permission and Libvirt Errors

Verify group membership:

```bash
id -nG | tr ' ' '\n' | grep -E 'libvirt|kvm|astinuslab'
```

If `libvirt` or `kvm` is missing:

```bash
sudo usermod -aG libvirt,kvm "$USER"
```

Log out and back in before retrying.

Verify libvirt:

```bash
virsh -c qemu:///system list
```

If the host runner cannot call `virsh` or `qemu-img`, make sure your user is in the `libvirt` and `kvm` groups (`groups $USER`) and that you have logged out and back in after adding them.

## Passwordless Sudo Preflight Fails

The host runner expects installed helper scripts under `/usr/local/libexec/astinuslab/`.

Install or refresh them:

```bash
sudo ./astinuslab.sh setup-host
```

Verify:

```bash
sudo -n /usr/local/libexec/astinuslab/libvirt_storage_admin.sh noop
```

Do not configure sudoers to run helper scripts directly from the source checkout.

The install script runs `visudo -c` before writing the rule. If validation fails,
verify that `/usr/local/libexec/astinuslab/libvirt_storage_admin.sh` and
`/usr/local/libexec/astinuslab/fix_libvirt_path_acl.sh` exist, are owned by
`root:root`, and have mode `0755`.

## A Privileged Helper Refuses to Overwrite an Image

The privileged storage helper never clobbers a file that the runner user can
write, so replacing an image is always an explicit two-step operation. Two
`DENY:` messages come from this rule.

### `installed base image conflicts with the requested source`

Before booting a VM, the helper copies the target's image from
`images/definitions/` into the shared base directory
`/var/lib/libvirt/images/astinuslab/base/`, keeping the same filename. On every
later job it re-checks that copy by SHA-256 instead of re-copying. If the
image in `images/definitions/` has changed **under the same filename** — you
re-downloaded a cloud image, or ran `ops/host/prepare_target_image.sh`,
which rewrites the image in place — the digests no longer agree and the job
fails rather than letting the old, stale base image silently stay in use.

Do not remove or replace the installed base while any Job or Playground overlay
depends on it, including stopped, failed or retained labs. Follow the
[backing-image dependency gate](../user/PREBAKED_IMAGES.md#backing-image-dependency-gate)
before retirement. Prefer a new versioned filename for the refreshed image, or
restore the matching source copy while preserving the installed base. Stopping a
VM alone does not remove its backing-file dependency.

### `cannot create image output without clobbering: [Errno 17] File exists`

A flatten step (`ops/host/bake_workload.sh`) tried to write an image that
already exists. Bake to a new filename, or delete the old image first — see
[Re-baking over an existing image](../user/PREBAKED_IMAGES.md#re-baking-over-an-existing-image).

## Development Mode Refuses to Start

`./astinuslab.sh start` refuses to start when systemd AstinusLab services are active.

```bash
./astinuslab.sh status
sudo systemctl disable --now astinuslab-api@$USER astinuslab-runner@$USER astinuslab-analysis@$USER
sudo ./astinuslab.sh setup-host
./astinuslab.sh start
```

This sequence is for a single-identity install. It rebinds the shared privileged
helpers to this development checkout. For split identities, use the explicit
`astinuslab-api`, `astinuslab-runner` and `astinuslab-analysis` units and preserve
their configured ownership; do not collapse them onto your user. Follow
[Switching Between Modes](../development/setup.md#switching-between-modes)
before returning to release mode so its helpers are rebound too.

## Legacy pre-Alembic database upgrades

AstinusLab still includes a frozen bootstrap for databases created before the Alembic baseline. That path creates any missing baseline tables from a snapshot, migrates legacy dump columns into artifacts, stamps baseline revision `ef55cafe2cea`, then runs normal Alembic upgrades. Legacy bootstrap EOL is release 0.3; pre-Alembic databases should upgrade through an intermediate release before that support is removed.

## Jobs Stay Queued During a Long Capture

AstinusLab V1 runs one Celery worker at `--concurrency=1`. Long captures intentionally occupy that thread, so reconciliation, expiry, retention, and later jobs can wait behind the active acquisition. Do not add runner instances or raise concurrency as a quick fix; see [Scaling](../architecture/components.md#scaling) for the capacity-vs-worker-concurrency distinction.

## A Capture Appears Stuck

Capture and publish phases do not expose a cancel button. The job state graph deliberately omits `cancel_requested` edges from `acquiring_memory`, `acquiring_disk`, and `publishing_artifact` so an operator cannot interrupt an acquisition in a way that leaves forensic state ambiguous.

For Playground disk captures, AstinusLab applies `playground_capture_timeout_seconds` (default `900`) to the host `qemu-img convert` call. When that timeout fires, the VM resume command is still attempted and the acquisition is marked failed. Job memory captures use libvirt core-dump APIs and are not timeout-able in V1.

Check the current state before intervening:

```bash
virsh domstate <domain>
./astinuslab.sh logs
ls storage/tmp/job-pids/<job_id>
```

If the capture is still progressing, wait for it or for the Playground disk timeout. If `qemu-img convert` is truly wedged, kill the recorded convert PID, resume the VM with `virsh resume <domain>`, then use retry or reset the lab before starting another capture.

## A Playground capture fails to create its output directory

When a Playground capture creates its output directory, the job and VM components below the capture root are created one at a time with `O_NOFOLLOW`, so a symbolic link planted at either of them is refused rather than followed. Without that refusal a capture would be written through the link, landing somewhere other than the path recorded for the evidence.

This covers a link that is already in place when the capture starts. It is not a general guarantee about every write beneath the capture root: later stages re-create the same directory with an ordinary `mkdir -p`, so a link swapped in *during* a capture is not detected. That residual is accepted under the V1 localhost/trusted-local model, where nothing untrusted can write into the capture tree.

Relocating `playground_capture_dir` onto a larger disk via symlink keeps working: the configured path is resolved before the directory walk, so the link is dereferenced once, up front.

If a capture fails with `NotADirectoryError` or `OSError` while creating its output directory, check for a link under the capture root:

```bash
find storage/playground/captures -maxdepth 2 -type l -ls
```

Use the configured path if `playground_capture_dir` (env `ASTINUSLAB_PLAYGROUND_CAPTURE_DIR`) has been changed from its `storage/playground/captures` default.

Replace any link found with a real directory, or point `playground_capture_dir` at the intended location instead of linking a job or VM directory into place.

## VM capacity reached (HTTP 409, code `vm_capacity`)

`orchestration.max_concurrent_vms` is set and that many VMs are already active. Destroy or stop a VM, or raise the limit and restart the services. If no VMs appear to be running, a `teardown_failed` or `destroying` row may still be counted as a fail-safe state: wait for the periodic libvirt reconciler (default every 60 seconds) to resolve it, or clean the zombie VM up manually and let reconciliation mark the row terminal.

## Jobs Stay in `waiting_for_guest_access`

Likely causes:

- `default_username` in `config/catalog/targets.yaml` does not match the cloud image.
- The guest did not process cloud-init data.
- Networking failed or DHCP did not assign an address.
- The SSH key is missing or not active in the UI.

Check:

```bash
./astinuslab.sh logs
```

For images with vendor-specific networking, try a target catalog override such as `inject_network_config: false`.

If the guest never becomes reachable because of stale cloud-init state, or because it is an offensive image such as Kali that ships no cloud-init at all, **precook** the image and relaunch:

```bash
./astinuslab.sh precook <image>   # auto-detects target vs attacker
```

See [Pre-baked Images](../user/PREBAKED_IMAGES.md).

## New Images Are Not Recognized

If you place a new image in `images/definitions/` and AstinusLab does not detect it correctly, the usual causes are:

- The filename does not match the auto-discovery rules
- There is no explicit entry in the catalog
- `default_username` is wrong
- `os_variant` is wrong
- The distro needs a cloud-init family or template that does not exist yet

Check the generated `<image>.meta.json` sidecar in `images/definitions/` to see what auto-discovery detected.

Steps to fix:

1. Install `libguestfs-tools` so `virt-inspector` is available.
2. Run `./astinuslab.sh scan-images --dry-run` to preview detection results.
3. Rename the image file so the filename clearly identifies the distro and version.
4. If that is not enough, add an explicit entry to `config/catalog/targets.yaml`.
5. If the distro or naming does not match the current detection rules, extend `src/astinuslab/catalog.py`.
6. If it is a new family, add its templates under `guest/cloud-init/`.

Fields to always check:

- `image.image_ref`
- `image.default_username`
- `distro.slug`
- `distro.version`
- `virtualization.os_variant`
- `virtualization.machine_type` (when applicable)

Typical symptoms:

- Image shown as `unknown` → `virt-inspector` could not identify it and filename fallback also failed
- VM boots but SSH fails → wrong `default_username`
- Erratic cloud-init or boot behaviour → wrong `os_variant`
- New distro with no SSH → missing cloud-init support

## AppArmor or Storage Access Errors

Release mode configures AppArmor and traversal ACLs for `/opt/astinuslab/storage`.

Development mode stores artifacts under `<repo>/storage`. If real VM jobs fail due to storage access, run:

```bash
sudo ./astinuslab.sh setup-host --with-apparmor
```

Then retry `./astinuslab.sh smoke` and the job.

## Volatility 3 ISF Generation Fails

First check `/api/v1/volatility/readiness` or the Volatility page. The most
common dependency codes are `dwarf2json_not_configured`,
`dwarf2json_not_found`, `dwarf2json_not_executable`,
`dwarf2json_hash_not_configured`, and `dwarf2json_hash_mismatch`. Correct the
operator-owned path or matching SHA-256 pair and restart services; do not copy a
tool into the AstinusLab tree. A `guest transfer mismatch` requires discarding
the guest copy and retrying only after host and guest verify the configured hash.
If a build cannot be performed, upload a validated ISF manually from the
Volatility page.

For analysis failures, check the corresponding `volatility_not_configured`,
`volatility_not_found`, `volatility_not_executable`,
`volatility_version_unsupported`, or `volatility_probe_failed` code. The
supported probe must report `Volatility 3 Framework 2.28.0`; inspect protected
service logs for probe detail rather than exposing subprocess stderr.

The production ISF-only host-side chain is:

```text
local_cache -> prebuilt_isf -> debuginfod -> host_fetch
```

If that chain fails, AstinusLab tries the live-guest fallback, which may use an existing matching DWARF-bearing `vmlinux`, debuginfod, or explicitly permitted debug-package installation from the guest. BTF-only input is not supported by `dwarf2json` and is never used for this fallback.

Common causes:

- Missing `rpm2cpio`, `cpio`, `libguestfs-tools`, `qemu-utils`, or `libvirt-clients`.
- No matching debug symbols for the exact guest kernel.
- Package repository metadata cannot be reached from the fresh VM.
- The generated JSON is not a valid Volatility 3 ISF.
- Prebuilt mirror URLs are missing or not `https://`.
- The target is not a supported x86_64 Linux distribution.

Check the job events and runner logs, then see [Volatility 3](../user/VOLATILITY3.md) and [Distro Support](../reference/DISTRO_SUPPORT.md).

### Plugin output is missing

ISFs and plugin output are stored in different places. Reusable ISFs live under `vol3/symbols/linux/`; per-job plugin output lives under `storage/dumps/<job_id>/vol3/`. If the ISF exists but plugin output is missing, check the job's Volatility analysis status and plugin errors in the job events.

## Disk Space Problems

Memory dumps can be as large as VM RAM. Disk artifacts can be several GB.

Check:

- `storage/dumps/`
- `storage/playground/captures/`
- `/var/lib/libvirt/images/astinuslab/`
- `vol3/symbols/`
- `site/`, `.pytest_cache/`, `.ruff_cache/`, `.mypy_cache/`

See [Generated Runtime Files](../development/GENERATED_RUNTIME_FILES.md) before deleting generated paths.

## Logs

Development mode writes logs under `storage/logs/`:

```bash
./astinuslab.sh logs
```

Release mode uses systemd journals:

```bash
journalctl -u astinuslab-api@$USER -f
journalctl -u astinuslab-runner@$USER -f
journalctl -u astinuslab-analysis@$USER -f
```

An installed deployment also keeps its own rotating copy under `storage/logs/`, which is what the **Logs** page reads. Set `[orchestration] log_to_file = false` to turn that copy off and use the journal alone.
