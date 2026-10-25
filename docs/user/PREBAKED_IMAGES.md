# Pre-baked Images & Performance

Installing complex workloads (like a full LAMP stack) can take several minutes per job. To achieve near-instant lab launches, AstinusLab allows you to "bake" a workload directly into a new base image.

For Windows Server 2022, use the dedicated [Windows Images guide](WINDOWS_IMAGES.md).
The Linux preparation commands below do not prepare Windows.

## 0. Quick start: precook Linux images (recommended)

For a compatible Linux image, use one command to apply the preparation recipe.
`./astinuslab.sh precook <image>` auto-detects whether the image is a **target**
(a victim, provisioned by the per-VM cloud-init seed) or an **attacker** (an
offensive image such as Kali/Parrot with no cloud-init), resolves the active lab
SSH key for you, and applies the matching recipe from the sections below. Most
users never need to call the role-specific scripts directly.

```bash
# See the plan without changing anything (no root required):
./astinuslab.sh precook kali-linux-2025.4-qemu-amd64.qcow2 --resolve-only

# Make an offensive image compatible (auto-detected → bakes SSH key + guest agent + DHCP):
./astinuslab.sh precook kali-linux-2025.4-qemu-amd64.qcow2

# Make a stock cloud image compatible (auto-detected → cloud-init reset, network-free):
./astinuslab.sh precook debian-12-genericcloud-amd64.qcow2
```

`<image>` may be a filename under `images/definitions/`, a path to a `.qcow2`/`.img`,
or a catalog id. Force the role with `--role target|attacker` when auto-detection
is wrong, and pass `--with-packages` to also apt-install ssh + the guest agent on
a target. The SSH key comes from the active key on the **Security** page
(`storage/ssh_keys/.active`); override it with `--key`. Sections 2 and 3 document
the underlying recipes that `precook` invokes.

## 0c. Windows operator-prepared images

The Windows Server 2022 path is opt-in and consumes an operator-prepared
generalized qcow2, prebuilt public setup CD and adjacent schema-v3 provenance sidecar. It is not the
`precook` workflow and does not make Windows a release-supported target.

**The reviewed Server 2022 bundle passed two-clone native runtime acceptance**
on September 23, 2026, including unattended preparation, strict SSH, reboot
persistence and teardown isolation. Encryption remains unknown; this does not
certify Windows workloads, acquisition or release readiness. Manual OOBE, login
success or a passing metadata check is insufficient for another image. Import
requires the accepted bundle's final provenance and operator approval.

Follow [Windows Images](WINDOWS_IMAGES.md) for reference-VM preparation, required
guest components, disposable sealing and unattended validation, sidecar fields,
and the steps for using an independently accepted image. Preserve the reference
and recovery copies; run generalization only on a reviewed disposable candidate.

## 0b. Playground workloads on the isolated network (precook cache)

The default Playground network does not forward guest traffic to the internet. Uncached installers using package repositories or Git therefore need preparation before an isolated launch. The wizard displays this prerequisite without checking the cache or enabling egress.

1. End an ordinary Playground session and confirm cleanup before switching to Jobs. Activate the SSH key on **Security** and prepare the selected base image.
2. On a host with an explicitly available egress path for Jobs (normally libvirt’s NAT `default` network), run a Jobs `lab-session` with the desired image/workload. Wait for successful provisioning and `waiting_for_capture`; confirm a matching cache entry under `<storage_dir>/workload-cache/` before cancelling/ending the Job and waiting for cleanup.
3. Return to Playground and choose the same image/workload on the isolated network. The cache key binds target ID, workload ID, base-image identity and install-script fingerprint. A matching entry boots prepared content and skips installation. A cache miss still invokes the live installer; either prepare first or explicitly choose an egress-enabled network.

Use the tested `api_curl` setup in [API examples](../usage/examples.md) before submitting the preparation Job:

```bash
api_curl -sS -X POST "$BASE_URL/api/v1/jobs" \
  -H 'Content-Type: application/json' \
  -d '{"target_id":"ubuntu-24.04","workload_id":"dvwa",
       "scenario_id":"lab-session","execution_mode":"manual_trigger"}'
```


## Bake helper connection and failure handling

Run `ops/host/bake_workload.sh` on the same host and with the same configuration
as the running API. It discovers the installed data root beside `releases/`, or
uses `ASTINUSLAB_DATA_DIR` explicitly; it loads that root's `config/astinuslab.env`
and application settings, including `images_dir`, `libvirt_image_base_dir` and
`libvirt_uri`. `BASE_URL` can override the local HTTP endpoint (default
`http://localhost:<configured-api-port>`). `ASTINUSLAB_PYTHON` can select the
application interpreter when it is not at the code root's `.venv/bin/python`.

For authenticated installs, export `ASTINUSLAB_TOKEN` with the configured admin
bearer token. Without a token the helper obtains a CSRF cookie from `/playground`
and echoes it for mutations; this supports local development with auth disabled.
Never disable authentication to make the helper work. The helper requires curl,
jq, virsh and permission to use the installed storage helper. Its configured roots
must match the privileged root policy; choosing a data root does not register it.

HTTP and conversion failures return a nonzero status. Provisioning waits at most
30 minutes and graceful shutdown at most three minutes. A failed run prints its
Job ID and output path for inspection; it does not automatically destroy an
unfinished lab or delete an output. Successful conversion is reported separately
from cleanup, which must also succeed before “Baking complete” is printed. Do not
rerun blindly after a failure: inspect the Job and retain any completed image.

## 1. Baking a Workload

Use the `ops/host/bake_workload.sh` script to automate this process. It creates a temporary job, installs the workload, shuts down the VM, and flattens the result into a new standalone QCOW2 image.

### Usage
```bash
./ops/host/bake_workload.sh <target_id> <workload_id> [output_image_name]
```

**Example**:
```bash
./ops/host/bake_workload.sh ubuntu-22-04-default wordpress-6.4 ubuntu-22.04-wordpress.qcow2
```

### Re-baking over an existing image

The flattening step runs as root through the privileged storage helper, and that
helper **never overwrites an existing destination**: it creates the output file
with `O_EXCL`, so root can never be talked into clobbering a path the runner user
can write. Re-running a bake that would land on a file already in
`images/definitions/` therefore stops with:

```
DENY: cannot create image output without clobbering: [Errno 17] File exists
```

This matters because the third argument is optional — omitted, the output name
defaults to the stable `<target_id>-<workload_id>.qcow2`, so a second bake of the
same target + workload targets the same file.

Prefer a new versioned output name before re-baking:

```bash
# Keep the previous image and bake to a new name (recommended — nothing is lost):
./ops/host/bake_workload.sh ubuntu-22-04-default wordpress-6.4 ubuntu-22.04-wordpress-v2.qcow2

```

Retiring an old output requires the same
[backing-image dependency gate](#backing-image-dependency-gate) as installed
bases, plus removal of catalog references. A stopped VM can still depend on the
old file. Keep it until every dependency has been resolved and retained data has
been preserved.

A different denial, from the same privileged helper, fires when an image already
installed in the libvirt base pool no longer matches the copy in
`images/definitions/`. See
[Replacing a base image after an upstream refresh](#5-replacing-a-base-image-after-an-upstream-refresh).

### Next Steps
Once the script completes:
1.  The new image is saved under the configured data root's `images/definitions/`.
    The helper does not inspect it. Open **Images**, request inspection when
    `virt-inspector` is available, and verify distro, version and default username
    before using it. A successful flatten is not proof that the image boots or
    that its Workload is ready.
2.  (Optional) Add a curated target entry to `config/catalog/targets.yaml` if
    you want to override the auto-discovered defaults or pin scenario-specific
    settings:
    ```yaml
    - id: ubuntu-22.04-wordpress-prebaked
      enabled: true
      distro: { slug: ubuntu, version: "22.04", family: debian }
      image:
        source_type: qcow2
        image_ref: ubuntu-22.04-wordpress.qcow2
        default_username: ubuntu
    ```
3.  Refresh the catalog or restart AstinusLab and verify the target is available.
    Validate the new image in a disposable lab. The baked software is already in
    the image; selecting an additional Workload can still invoke its installer.

---

## 2. Pre-baking Attacker Images

Attacker images often require specific tools and SSH configurations. The shipped attacker catalog uses Kali; use `ops/host/prebake_attacker_image.sh` to prepare Kali or another custom attacker image.

### Usage
```bash
./ops/host/prebake_attacker_image.sh <source_image> <output_image> [attacker_user] [ssh_public_key]
```

**Example**:
```bash
./ops/host/prebake_attacker_image.sh \
  images/definitions/kali-linux-2024.qcow2 \
  images/definitions/kali-prebaked.qcow2 \
  kali \
  storage/ssh_keys/astinuslab-default.pub
```

### Why use this?
*   **Automatic SSH Setup**: Inject's the AstinusLab runner's public key so it can control the attacker VM.
*   **Guest Agent**: Installs `qemu-guest-agent` for graceful shutdowns.
*   **Speed**: No need for cloud-init to run heavy setup tasks on every boot.

---

## 3. Preparing Stock Target Images

Playground **target** VMs are provisioned by a per-VM cloud-init seed (the
orchestrator injects the active SSH key and `qemu-guest-agent` at boot). Some
stock cloud images (e.g. `ubuntu-20-04-legacy`, `debian-12`) ship with
cloud-init state that prevents the seed from being re-applied, so those targets
never become SSH-reachable — only already-prepared images (e.g. `ubuntu-24.04`)
work out of the box.

Unlike attacker prebaking, target preparation does **not** bake a user or key
into the image. The load-bearing step is `cloud-init clean`, which wipes the
image's stale cloud-init state so cloud-init re-runs on the next boot and
consumes the per-VM seed. This needs **no network**, so it works on an
air-gapped lab host. Host SSH keys are regenerated, and `ssh`/the guest agent
are enabled if already present (Ubuntu server cloud images ship
`openssh-server`).

### Usage
```bash
# dry run first (no root, just prints what would be prepared):
ops/host/prepare_target_image.sh ubuntu-20-04-legacy --resolve-only

# prepare it (in place; network-free):
ops/host/prepare_target_image.sh ubuntu-20-04-legacy

# online host or image missing ssh? also apt-install ssh + guest agent:
ops/host/prepare_target_image.sh debian-12 --with-packages
```

You can pass a catalog target id (resolved via `config/catalog/targets.yaml`)
or a raw image filename under `images/definitions/`. The image is rewritten
atomically (temp copy → `mv`), and its `.meta.json` is marked
`"prepared": true` on success.

> **If that image has already been used in a job**, preparing it in place changes
> its content under an unchanged filename. The privileged helper keeps a
> digest-verified copy in `/var/lib/libvirt/images/astinuslab/base/`, so the next
> job stops with `DENY: installed base image conflicts with the requested
> source` instead of booting the stale copy. Keep the installed base intact until
> the [backing-image dependency gate](#backing-image-dependency-gate) is satisfied.
> Preparing a versioned copy avoids changing the source used by existing labs.

### Requirements
`libguestfs-tools` (provides `virt-customize`). `--with-packages` additionally
needs apt/network inside the build appliance. If `virt-customize` fails with a
supermin error, the appliance usually cannot read the host kernel:

```bash
sudo apt install -y libguestfs-tools
sudo chmod 0644 /boot/vmlinuz-*
libguestfs-test-tool   # must exit 0
```

---

## 4. Performance Tips

*   **Reflink Copying**: Job VM disks use qcow2 backing-file overlays, which are already instant to create. The image-preparation scripts use `cp --reflink=auto` for working copies; on filesystems like **Btrfs** or **XFS**, that makes bake/prepare steps faster and can save disk space.
*   **SSD Storage**: Always place your `images/definitions` and `storage/` directories on an SSD for the best VM performance.

---

## 5. Replacing a base image after an upstream refresh

The first job that uses a target copies its image from `images/definitions/` into
the libvirt base pool (`/var/lib/libvirt/images/astinuslab/base/`) through the
privileged storage helper. If a file of that name is already installed, the
helper compares digests and **refuses to replace a base image whose content
differs**:

```
DENY: installed base image conflicts with the requested source
```

That is a safety property, not a bug: existing job overlays are backed by the
installed file, and silently swapping it would corrupt every VM built on it.

You will hit this when a cloud-image publisher reissues a build under a stable
filename — `ops/images/download_images.sh` pulls Ubuntu 24.04 from the rolling
`noble/current/` channel, which is the only channel still publishing the
`noble-server-cloudimg-amd64.img` name the catalog pins.

Confirm that is what happened by comparing the two files:

```bash
sha256sum images/definitions/noble-server-cloudimg-amd64.img
sudo sha256sum /var/lib/libvirt/images/astinuslab/base/noble-server-cloudimg-amd64.img
```

Different digests show that the source and installed base differ; they do not
prove why the bytes changed. Prefer keeping the installed base and giving a new
image revision its own filename and catalog entry. To use the old revision again,
preserve the new download separately before restoring a matching source copy:

```bash
# Keep the installed base image (recommended — no existing overlay is disturbed).
# Copy it back over the fresh download so source and base agree again, and give
# it back to your user: `sudo cp` writes root-owned, and a root-owned file in
# images/definitions/ makes the *next* download_images.sh run fail to update it.
sudo install -o "$USER" -g "$(id -gn)" -m 0644 \
  /var/lib/libvirt/images/astinuslab/base/noble-server-cloudimg-amd64.img \
  images/definitions/noble-server-cloudimg-amd64.img
ls -l images/definitions/noble-server-cloudimg-amd64.img   # confirm you own it

```

### Backing-image dependency gate

Before removing or replacing any source or installed base image:

1. Stop new admissions and identify every Job, ordinary Playground session and
   enabled Packages instance that references the image. Include stopped, failed
   and retained VMs; an inactive domain can still need its backing file.
2. Inspect the backing chains of their overlays with read-only
   `qemu-img info --backing-chain <overlay-path>` and the domain disk paths with
   `virsh -c qemu:///system domblklist <domain> --details`. Review configured
   Playground disk roots and retained/orphan overlays as well as known domains.
   Absence from a running-VM list is not proof of no dependency.
3. Preserve required evidence and recovery state. Either keep the old base at its
   exact path and prepare the new image under a new filename, or complete the
   owning lab's supported cleanup and confirm its domains and overlays are gone.
4. Retire only the exact old image after no backing chain or catalog entry still
   needs it. If dependency ownership is uncertain, keep the old file. Never use
   broad storage cleanup to resolve a base-image mismatch.

Removing a shared base or replacing it with different bytes can invalidate every
overlay that depends on it. Registration, a stopped service, or successful image
preparation does not establish that retirement is safe.
