# Running Locally

The tested host is Ubuntu 24.04 LTS on x86_64 with KVM. Other host recipes are experimental. Download staging space is separate from operating capacity: strict smoke warns below 150 GB free disk or 16 GB RAM; each lab still needs its own resource budget.

This page is the operational runbook for installing and running AstinusLab from this repository as a system service. 

If you plan to modify code or run tests, please see [Development Setup](../development/setup.md) instead.

## Prerequisites

### System packages

The host machine must be running Linux with KVM hardware virtualisation support. Install the required packages based on your distribution:

**Debian / Ubuntu:**
```bash
sudo apt update
sudo apt install -y \
  git build-essential pkg-config acl curl wget ca-certificates \
  python3 python3-dev python3-venv python3-pip \
  qemu-kvm qemu-utils libvirt-daemon-system libvirt-clients libvirt-dev \
  virtinst cloud-image-utils cloud-init rsync openssh-client sudo

# Optional but recommended for image auto-detection:
sudo apt install -y libguestfs-tools
# Required for ISF symbol extraction from Fedora/RHEL targets:
sudo apt install -y rpm2cpio cpio
```

**Fedora / RHEL:**
```bash
sudo dnf install -y \
  qemu-kvm qemu-img libvirt libvirt-client libvirt-devel virt-install \
  cloud-utils pkgconf python3 python3-pip rsync openssh-clients sudo \
  policycoreutils-python-utils

sudo dnf install -y libguestfs-tools rpm-build cpio
```

**Arch Linux:**
```bash
sudo pacman -S --needed \
  qemu-desktop libvirt virt-install cloud-guest-utils pkgconf \
  python python-pip rsync openssh sudo cpio libguestfs
```

### Mandatory Access Control (AppArmor / SELinux)

Libvirt uses MAC systems to confine VMs. AstinusLab requires that libvirt VMs can read/write to the storage directory (`/opt/astinuslab/storage` by default).

- **AppArmor (Ubuntu/Debian):** The automated `ops/deploy/install.sh` script automatically patches the Libvirt AppArmor template (`/etc/apparmor.d/libvirt/TEMPLATE.qemu`).
- **SELinux (Fedora/RHEL):** `ops/deploy/install.sh` configures the context labels automatically when SELinux is Enforcing (it installs and uses `policycoreutils-python-utils`). Manual labelling is only needed for custom storage trees:
  ```bash
  sudo semanage fcontext -a -t svirt_image_t "/opt/astinuslab/storage(/.*)?"
  sudo restorecon -Rv /opt/astinuslab/storage
  ```

### Group membership

```bash
# Add your user to the libvirt and kvm groups
sudo usermod -aG libvirt,kvm "$USER"
# Log out and back in for group membership to take effect
```

Verify KVM is usable:

```bash
virsh -c qemu:///system list
```

### Python

Python 3.12 or 3.13 is required (`python3 --version`). AstinusLab 1.0.0 is tested
on those two runtimes only. The `requires-python = ">=3.12"` line in
`pyproject.toml` is the packaging minimum, not a statement that 3.14 or later is
supported.

### SSH key

AstinusLab uses an ED25519 key pair to connect to guest VMs. The install script can generate one automatically, or you can use an existing key. For dev mode, the key must be available before starting jobs (configurable in the Security section of the UI).

## Installation

AstinusLab is deployed from a source checkout as a systemd service. 

Run the installer as root from a clean checkout. By default, `SERVICE_USER` is
the local operator identity that runs all three services and receives the
`NOPASSWD` helper grant. Install and update preserve that single-identity shape
unless `--split-users` is explicitly supplied.

```bash
bash ops/deploy/install.sh --check \
  --install-dir /opt/astinuslab \
  --service-user "$USER"

sudo ops/deploy/install.sh \
  --install-dir /opt/astinuslab \
  --service-user "$USER"
```

`--check` is read-only and authoritative: its last line is exactly one of
`Preflight result: READY`, `READY WITH WARNINGS`, or `NOT READY`. Required
failures return nonzero. Fix a `NOT READY` result before running the installer.
The installer then shows the resolved plan and nine numbered phases.

For an existing installation, first install and start the candidate normally
without the split flag. Confirm the operator-owned services and a real VM still
work, record the current ownership and permissions of the install and storage
directories so a rollback can be checked, then run the installed candidate's identity transition:

```bash
sudo /opt/astinuslab/current/ops/deploy/install.sh \
  --install-dir /opt/astinuslab \
  --service-user "$USER" \
  --split-users
```

This creates `astinuslab-api`, `astinuslab-runner`, and
`astinuslab-analysis`. Only the runner joins `libvirt`/`kvm` and receives the
narrow helper sudoers grant. The installer first grants and verifies the new
runner's helper access while retaining the old operator grant, then stops the
old units, changes ownership, starts and verifies the explicit split units,
removes the old grant, and disables the old instances. Any failed step restores
the saved sudoers, helpers, ownership/ACLs, unit files, enablement, and active
state. Re-running without `--split-users` never performs this migration.
The transition is deliberately content-neutral: source, virtualenv, database
schema, and MAC policy must already be the verified candidate, so rollback only
has to restore identity-bound metadata and privileged policy. A fresh
`--split-users` install first bootstraps and starts that single-identity
candidate, then performs the same transition. `--split-users --no-start` is
rejected because the old privilege cannot be removed before the new services
have passed bounded liveness and deep-readiness checks.

After a split install, do not use the launcher's manual `start` or `restart`
commands: they refuse to collapse the three services back onto the operator
UID. The legacy launcher `cleanup` and `uninstall` workflows also refuse split
installs because they cannot delete state while explicit units are live; use
an explicit systemd/operator cleanup workflow, or `sudo ops/deploy/uninstall.sh`
for removal. Control the explicit units with systemd and pass the one-time
token from the install log when running operator diagnostics:

```bash
sudo systemctl restart astinuslab-api astinuslab-runner astinuslab-analysis
ASTINUSLAB_ADMIN_TOKEN='<token-from-install-log>' \
  /opt/astinuslab/current/astinuslab.sh doctor --require-running
```

For storage outside the default checkout, register root policy as root after
install. The path must be absolute, existing, non-symlink, and dedicated:

```bash
sudo /opt/astinuslab/current/ops/deploy/configure-privileged-roots.sh \
  --install-dir /opt/astinuslab \
  --storage-root /srv/astinuslab
```

Set `[storage] storage_dir` or `ASTINUSLAB_STORAGE_DIR` to the same path before
running the command. This selects the destination for future Job output; it does
not copy historical evidence or rewrite database references. Playground captures,
Playground disks and `db_path` have independent settings and retain their existing
roots. Keep old roots until a separately planned migration verifies the files,
sidecars, database references and service access. Registration alone does not
authorize retiring old data; see [Privileged storage policy](../reference/ADVANCED_CONFIG.md#privileged-storage-policy).

Run `--check` to inspect policy and systemd sandbox mismatch without mutation.
Units are re-rendered from this root; service restart happens only with
`--restart`. A root under `/home` gives every service unit `ProtectHome=tmpfs`
and a `BindPaths=` mount for that root, keeping other home directories hidden
while making the configured storage reachable and writable.

Useful installer flags:

```bash
# Preflight only; no writes, no root required for read-only checks
bash ops/deploy/install.sh --check --install-dir /opt/astinuslab --service-user "$USER"

# Update an existing release tree and restart units
sudo ops/deploy/install.sh --install-dir /opt/astinuslab --service-user "$USER" --restart

# Install/update without enabling or starting services
sudo ops/deploy/install.sh --install-dir /opt/astinuslab --service-user "$USER" --no-enable --no-start

# Noninteractive SSH-key behavior
sudo ops/deploy/install.sh --install-dir /opt/astinuslab --service-user "$USER" --yes
sudo ops/deploy/install.sh --install-dir /opt/astinuslab --service-user "$USER" --no-ssh-key
```

Re-running the installer preserves mutable runtime files under `/opt/astinuslab/astinuslab.toml`, `config/astinuslab.env`, and `config/catalog/*.yaml`; refreshed defaults remain available through `*.example.yaml` files.

A clean install enables admin authentication and shows the generated token when
created and once more in `CREDENTIALS — SAVE NOW`; save it immediately because
only its hash is stored. An update preserves the existing hash and
refuses to rotate it silently — pass `--allow-unauthenticated-local` only for an explicitly
development-only install. An update also keeps a storage root that was registered with
`configure-privileged-roots.sh`: the installer reads the root back from the policy instead of
reverting to `<install-dir>/storage`.

To direct future Job output to a different root, configure `storage_dir`, then run
`configure-privileged-roots.sh` against the new root. This does not migrate existing evidence.
Do not uninstall and reinstall for that:
`ops/deploy/uninstall.sh` removes the ACL policy,
the privileged helpers, and the sudoers rule, and it fails loudly if an unknown file is left in
`/usr/local/libexec/astinuslab`.

Deliberately returning to an older build requires a schema it supports, or a
verified pre-migration database backup restored with every writer stopped. The
installer's automatic recovery after a failed upgrade restores its own backup;
that does not make arbitrary later downgrades safe. Follow
[Schema migration and rollback](../reference/ADVANCED_CONFIG.md#schema-migration-and-rollback)
before switching code. Preserve evidence and reconcile any records or files
created after the backup; reinstalling code does not undo database migrations.

Installers keep application source and `.venv` installer-owned and read-only to services; runtime writes belong only in storage, mutable config, image definitions, and Volatility symbols.

After install or update, you can check the status:

```bash
cd /opt/astinuslab/current
./astinuslab.sh smoke --require-running
```

Only open `http://localhost:8000` after strict smoke passes (Playground opens;
Jobs live at `/jobs`). Volatility 3 is optional; print its read-only,
recommended-version setup and readiness commands with:

```bash
cd /opt/astinuslab/current
./astinuslab.sh guide-volatility
```

Useful commands:

```bash
# Logs
journalctl -u astinuslab-api@$USER -f
journalctl -u astinuslab-runner@$USER -f
journalctl -u astinuslab-analysis@$USER -f

# Lifecycle
sudo systemctl restart astinuslab-api@$USER astinuslab-runner@$USER astinuslab-analysis@$USER
sudo systemctl stop    astinuslab-api@$USER astinuslab-runner@$USER astinuslab-analysis@$USER
sudo systemctl disable astinuslab-api@$USER astinuslab-runner@$USER astinuslab-analysis@$USER  # do not start at boot

# Update after a git pull (re-runs rsync, venv, helpers, sudoers, units)
sudo INSTALL_DIR=/opt/astinuslab SERVICE_USER=$USER bash ops/deploy/install.sh

# Remove
sudo bash ops/deploy/uninstall.sh
```

`INSTALL_DIR` and every existing ancestor must be `root:root` and not writable
by group or other. Use a trusted system path such as `/opt/astinuslab`; the
installer rejects an ordinary operator-owned install path below `/home` so a
privileged update cannot be redirected by replacing a parent directory.

## Cloud Images

AstinusLab requires standard cloud images to provision VMs. The helper downloads basic Ubuntu,
Debian, and Fedora images into the stable data root: `/opt/astinuslab/images/definitions` in release
mode, or the checkout's `images/definitions/` in development. Run
`/opt/astinuslab/current/ops/images/download_images.sh` for release mode or
`./ops/images/download_images.sh` in a development checkout. An optional argument overrides the
destination directory.

The four catalog downloads are approximately 3 GB compressed. Keep at least
10 GB free for downloads, overlays, and working space.

```bash
# Release mode, single-identity install
cd /opt/astinuslab/current && sudo -u "$USER" ./ops/images/download_images.sh

# Release mode with --split-users
cd /opt/astinuslab/current && sudo -u astinuslab-runner ./ops/images/download_images.sh

# Development checkout
./ops/images/download_images.sh
```

Alternatively, you can manually download images and place them in `images/definitions/`. Large VM images are local source assets, not normal source files. Download or generate them from documented sources, and see [Generated and Runtime Files](../development/GENERATED_RUNTIME_FILES.md) for the workspace cleanup and image provenance policy.

## Docker or Compose

No Dockerfile or Compose file is present in this repository. The supported workflows are release mode through `ops/deploy/install.sh` plus systemd, and development mode through `./astinuslab.sh`.

## Common Errors

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `failed to connect to the hypervisor` | User lacks libvirt access | Add the user to `libvirt,kvm`, then log out and back in. |
| Runner exits with passwordless sudo error | Helper scripts or sudoers rule missing | Run `sudo ./astinuslab.sh setup-host`. |
| VM boots but SSH never connects | Wrong catalog username or cloud-init profile | Check `default_username`, `cloud_init_profile`, and `inject_network_config` in `targets.yaml`. |
| Image appears as unknown | `virt-inspector` missing or filename heuristic failed | Install `libguestfs-tools` or add explicit catalog metadata. |
| `mixed/conflict` in `./astinuslab.sh status` | Development and systemd modes both active | Stop one mode before launching jobs. |
| Docs dependency missing | `mkdocs` not installed in `.venv` | Run `.venv/bin/python -m pip install -e ".[docs]"`. |
| `smoke: config missing/unreadable: config/astinuslab.env` | The seeded launcher environment file is missing or unreadable | Re-run `./astinuslab.sh setup`; it creates missing config without overwriting existing files. |
