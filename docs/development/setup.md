# Development Setup

Use the repository checkout for development. Release-mode installation is documented in [Running Locally](../usage/running-locally.md).

## Prerequisites

Development is supported on Ubuntu 24.04 LTS x86_64 with KVM and Python 3.12 or 3.13.

Install package prerequisites:

```bash
sudo apt update
sudo apt install -y \
  git build-essential pkg-config acl curl wget ca-certificates \
  python3 python3-dev python3-venv python3-pip \
  libvirt-dev qemu-kvm qemu-utils libvirt-daemon-system libvirt-clients virtinst \
  cloud-image-utils cloud-init rsync openssh-client sudo \
  libguestfs-tools rpm2cpio cpio
```

Configure `libvirt` and `kvm` group membership and re-login:

```bash
sudo usermod -aG libvirt,kvm "$USER"
# Log out and log back in so both group memberships take effect
```

## Clone and Prepare Catalogs

```bash
git clone <repo-url> AstinusLab
cd AstinusLab
```

Run `./astinuslab.sh setup` next; it seeds `astinuslab.toml`,
`config/astinuslab.env`, and every live catalog from the corresponding example
when the live file is missing. Existing local configuration is never replaced.

## Python and Frontend Environment

The supported setup path is:

```bash
./astinuslab.sh setup
npm ci
npx playwright install chromium
```

The launcher first preflights the host (Python ≥ 3.12, venv support, libvirt
headers, C compiler), then creates `.venv`, installs locked Python development
dependencies (`requirements-dev.lock` then editable with `--no-deps`), and
offers to generate an ED25519 SSH key in `storage/ssh_keys/`. `npm ci` installs the
pinned frontend contributor tools (requires Node.js 24); the Playwright command
installs Chromium for browser tests. Setup prints these two commands when frontend tools
are missing. Non-interactive runs generate the SSH key automatically.

For low-level script work, `tools/dev/setup_backend_venv.sh` also creates `.venv`,
installing `requirements.lock` before editable `--no-deps`. Prefer
`./astinuslab.sh setup` for normal development.

## Start Services

```bash
./astinuslab.sh start
```

For docs:

```bash
./astinuslab.sh start --docs
```

Check status:

```bash
./astinuslab.sh status
```

## Source Layout

| Path | Purpose |
| --- | --- |
| `src/astinuslab/` | FastAPI app, service layer, runner logic, repositories, schemas, and support modules. |
| `tests/python/` | Backend test suite. |
| `src/astinuslab/web/templates/` | Jinja2 templates. |
| `src/astinuslab/web/static/` | CSS and JavaScript. |
| `ops/host/` | Host helper scripts used by the runner. |
| `guest/scripts/` | Scripts copied into guest VMs. |
| `docs/` | MkDocs documentation. |
| `config/catalog/` | Declarative catalogs. |

## Generated Files

Do not commit runtime output from `storage/`, MkDocs output from `site/`, cache directories, downloaded cloud images, or generated symbol files. See [Generated Runtime Files](GENERATED_RUNTIME_FILES.md).


## Environment Variables

For development mode, create `config/astinuslab.env` only when defaults need to change:

```bash
cp config/astinuslab.env.example config/astinuslab.env
```

Common values:

```bash
ASTINUSLAB_API_HOST=127.0.0.1
ASTINUSLAB_API_PORT=8000
ASTINUSLAB_API_LOG_LEVEL=info
ASTINUSLAB_LIBVIRT_URI=qemu:///system
ASTINUSLAB_LIBVIRT_NETWORK=default
```

`ASTINUSLAB_DOCS_PORT` and `ASTINUSLAB_SERVICE_USER` are also supported by `astinuslab.sh` even though they are not present in the example file.

Backend settings are loaded from `astinuslab.toml`, then overridden by exported `ASTINUSLAB_*` values. `config/astinuslab.env` is sourced and exported by `astinuslab.sh`, so values in that file affect the API server, worker, and DB CLI.

## Database Management

Source checkouts default to SQLite at `storage/astinuslab.db`. Split-identity
installations use explicit installed environment paths under `storage/databases/`;
see [installed database maintenance](../reference/ADVANCED_CONFIG.md#installed-split-database-paths).
Schema migrations are managed via Alembic through the `dbcli` wrapper. From the checkout root, load the same environment as the launcher. Stop services before manually migrating an existing database:

```bash
(
  set -a
  source config/astinuslab.env
  set +a
  .venv/bin/python -m astinuslab.dbcli init
  .venv/bin/python -m astinuslab.dbcli status    # exit 0 means schema is ready
  .venv/bin/python -m astinuslab.dbcli revision
)
```

`./astinuslab.sh start` automatically executes `dbcli init` before launching API and runner processes. `./astinuslab.sh smoke` checks database status with `dbcli status` without mutating the database.

The `a044_prepare_execution_token` migration requires a quiescent upgrade:
stop **all** API, worker, reconciliation, and maintenance processes that can
mutate package state before migrating, then restart them together on the new
revision. Apply the same rule before downgrading. Older processes do not use
the new Prepare package lock or execution token and must never overlap the new
ones. Existing Prepare IDs and states are preserved; their initially null
tokens are replaced when a worker acquires custody. Keep every process on the
same configured package storage, including its `.locks` directory; do not
remove lock files to recover a stalled operation.

Prepare cancellation is cooperative at asset boundaries. A live resolver keeps
package custody even after its leases expire; another request cannot take over
and Delete remains blocked. If cancellation cannot make progress, the operator
must arrange a controlled restart of the responsible worker and account for
its other work. Process exit releases custody; reconciliation can then resume
reserved/running intent immediately. Failed intent requires an explicit retry
with the original idempotency key. Native installed-service and split-identity
acceptance must be verified separately from simulated tests.

## Installation

Active catalogs are seeded automatically: `./astinuslab.sh setup` copies every
`config/catalog/*.example.yaml` to its live `*.yaml` counterpart when the live
file is missing, and does the same for `config/astinuslab.env` (see
[Configuration](../usage/configuration.md)).

Install Python dependencies and create the development virtualenv:

```bash
./astinuslab.sh setup
npm ci
npx playwright install chromium
```

Privileged helpers, the sudoers rule, and the optional AppArmor setup for the
development checkout are covered step by step in
[Local Development](#local-development).

### Cloud images

Download the cloud images for the targets you want to use and place them in `images/definitions/`:

```bash
mkdir -p images/definitions

# Ubuntu 22.04
wget -P images/definitions \
  https://cloud-images.ubuntu.com/releases/22.04/release/ubuntu-22.04-server-cloudimg-amd64-disk-kvm.img

# Ubuntu 24.04
wget -P images/definitions \
  https://cloud-images.ubuntu.com/releases/24.04/release/noble-server-cloudimg-amd64.img

# Debian 12
wget -P images/definitions \
  https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2

# Fedora 40
wget -P images/definitions \
  https://download.fedoraproject.org/pub/fedora/linux/releases/40/Cloud/x86_64/images/Fedora-Cloud-Base-Generic.x86_64-40-1.14.qcow2
```

Large VM images are local source assets, not normal source files. Download or
generate them from documented sources, and see
[Generated and Runtime Files](../development/GENERATED_RUNTIME_FILES.md) for
the workspace cleanup and image provenance policy.

## Run Modes

AstinusLab supports two run modes. Both share the same code and the same
privileged-helper contract under `/usr/local/libexec/astinuslab/`, but they
keep separate code trees, virtualenvs, storage directories, and process
managers.

| Aspect | Release (systemd) | Development (`astinuslab.sh`) |
|---|---|---|
| Code location | `/opt/astinuslab/current` -> `releases/<version>-<digest>/` | your repo checkout |
| Venv | `/opt/astinuslab/current/.venv` (one per release) | `<repo>/.venv` |
| Storage | `/opt/astinuslab/storage` | `<repo>/storage` |
| Process manager | `systemd` units `astinuslab-api@USER`, `astinuslab-runner@USER`, `astinuslab-analysis@USER` | `./astinuslab.sh start` (PID files in `<repo>/storage/`) |
| Auto-start at boot | yes | no |
| Reload on code edit | requires re-running `ops/deploy/install.sh` | edit and `./astinuslab.sh restart` |
| AppArmor / libvirt ACLs | configured by the installer for `/opt/astinuslab/storage` | optional via `sudo ./astinuslab.sh setup-host --with-apparmor` when running real VMs |
| Privileged helpers (`/usr/local/libexec/astinuslab/`) | installed by `ops/deploy/install.sh` | install/update with `sudo ./astinuslab.sh setup-host` |
| Sudoers rule (`/etc/sudoers.d/astinuslab-runner`) | installed by `ops/deploy/install.sh` | install/update with `sudo ./astinuslab.sh setup-host` |
| Default API port | `8000` | `8000` |

Use **release** mode when you want AstinusLab to behave as a long-running
local service that survives reboots. Use **development** mode when you are
editing code, running tests, or iterating on a feature.

> The two modes **must not run at the same time**. Stop/disable the systemd
> services before starting development mode. Both runners consume the same
> Celery queue and write into libvirt, so running them in parallel can make
> jobs execute on the wrong runner or collide on VM names, networks, and
> snapshots. `./astinuslab.sh status` reports the active mode and warns on a
> `mixed/conflict` state.

## Local Development

Development mode runs the API and host runner directly from your repo
checkout. Use it by itself: stop the systemd release services before
starting `./astinuslab.sh`. Development mode uses the installed privileged
helpers and sudoers rule from `/usr/local/libexec/astinuslab/` and
`/etc/sudoers.d/astinuslab-runner`; rerun the helper installer after any
change to scripts under `ops/host/`.

### 1. Clone

```bash
git clone <repo-url> AstinusLab
cd AstinusLab
```

### 2. Configuration

Prepare the active catalog files as shown in [Installation](#installation).

Edit `astinuslab.toml` for application configuration (storage paths,
catalog paths, Celery URLs, libvirt URI). `config/astinuslab.env` is also
sourced and exported by the launcher: any `ASTINUSLAB_*` name matching a
backend `Settings` field overrides the TOML value for the API, worker, and DB
CLI. `ASTINUSLAB_DOCS_PORT` and `ASTINUSLAB_SERVICE_USER` are launcher-only
settings.

### 3. Helpers + sudoers rule

The host runner runs `sudo -n /usr/local/libexec/astinuslab/libvirt_storage_admin.sh noop`
on startup as a preflight; if it fails the runner aborts with
`"The host runner requires passwordless sudo for the libvirt storage helper"`.
Choose one of the two paths to satisfy it:

- **You already ran `ops/deploy/install.sh` with `SERVICE_USER=$USER`** —
  helpers and sudoers are in place; skip to step 4.
- **You want development mode only** — install just the privileged
  helpers and the sudoers rule, without systemd services or `/opt`
  copy:

  ```bash
  sudo ./astinuslab.sh setup-host
  ```

  The installer copies `ops/host/libvirt_storage_admin.sh`,
  `ops/host/libvirt_storage_admin.py`,
  `ops/host/fix_libvirt_path_acl.sh`, and
  `ops/host/network_isolation_admin.sh` to
  `/usr/local/libexec/astinuslab/` as `root:root 0755`, then writes
  `/etc/sudoers.d/astinuslab-runner` for the installed `.sh` entry points
  (`libvirt_storage_admin.py` is a support file, never a sudo target).
  The installer also records the install root's device/inode into
  `libvirt_storage_admin.py`, so the helper refuses to run if that
  directory is later replaced by a different one at the same path.
  Re-run the installer after changing any of those helper scripts in the repo.

  Two consequences of that helper are operator-visible: it never overwrites an
  existing image (a re-bake onto an existing filename is refused), and it keeps
  a digest-verified copy of each base image, so replacing an image in
  `images/definitions/` under an unchanged filename is refused until the stale
  copy under `/var/lib/libvirt/images/astinuslab/base/` is removed. Both are
  documented under
  [Troubleshooting](../usage/troubleshooting.md#a-privileged-helper-refuses-to-overwrite-an-image).

The default `<checkout>/storage` directory is supported for an operator-owned
development checkout. A disjoint custom storage root has stricter requirements:
its parent chain must be root-owned and not writable by group or other. See the
[privileged-root policy](../usage/running-locally.md) before choosing custom storage.

Verify that the current shell can run the installed helpers without a password:

```bash
sudo -n /usr/local/libexec/astinuslab/libvirt_storage_admin.sh noop && echo OK
```

After starting development mode, verify the runner is not sandboxed by
`NoNewPrivileges`:

```bash
./astinuslab.sh status
pid=$(cat storage/runner.pid)
grep NoNewPrivs /proc/$pid/status   # expected: NoNewPrivs: 0
```

### 4. Group membership

Your interactive user must be in `libvirt`, `kvm`, and (if you also ran
the full installer) `astinuslab`:

```bash
sudo usermod -aG libvirt,kvm "$USER"
# Only if the full installer created the astinuslab group:
# sudo usermod -aG astinuslab "$USER"
# Log out and back in for all new memberships to apply.
```

### 5. AppArmor + ACLs for the dev `storage/` (required before launching real VMs)

The release installer sets up libvirt AppArmor and traversal ACLs for
`/opt/astinuslab/storage`. Development mode uses `<repo>/storage`,
which is not covered unless you opt in:

```bash
sudo ./astinuslab.sh setup-host --with-apparmor
```

`./astinuslab.sh start` already runs `setfacl u:libvirt-qemu:x` on
`./storage` and `./storage/logs` at startup; `setup-host --with-apparmor`
handles the traversal chain and AppArmor template rule.

For a custom checkout storage root, register it once with root-owned ACL policy:

```bash
sudo ops/deploy/configure-privileged-roots.sh --install-dir "$PWD" --storage-root "$PWD/storage"
```

### 6. Set up the venv, start, and smoke

```bash
sudo systemctl disable --now astinuslab-api@$USER astinuslab-runner@$USER astinuslab-analysis@$USER  # manual mode only
./astinuslab.sh setup                      # create .venv, install deps, generate SSH key
sudo ./astinuslab.sh setup-host --with-mac # helpers + sudoers + storage MAC/ACLs for this checkout
./astinuslab.sh start                      # sources env, applies dbcli init, starts API + runner + worker in background
./astinuslab.sh smoke --require-running    # safe checks against active services; no VM launch
```

`./astinuslab.sh start` automatically exports `config/astinuslab.env`, runs `.venv/bin/python -m astinuslab.dbcli init` to initialize or migrate the database schema, and starts background services. Running `./astinuslab.sh smoke --require-running` immediately after verifies that the services and database are live and healthy.

If you wish to run `smoke` before launching background services, initialize the database first:

```bash
(
  set -a
  source config/astinuslab.env
  set +a
  .venv/bin/python -m astinuslab.dbcli init
)
./astinuslab.sh smoke
./astinuslab.sh start
```

`setup` seeds `astinuslab.toml`, `config/astinuslab.env`, and
`config/catalog/*.yaml` from their example files only when missing. Edit the
seeded environment file when launcher defaults need to change.

It is the launcher's environment file: values there are exported before the API
and workers start, so any `ASTINUSLAB_*` name matching a settings field also
overrides configuration.

Web UI: `http://localhost:8000` (opens Playground; Jobs live at `/jobs`). Docs (with `--docs`):
`http://localhost:9000`.

### Control commands

```bash
./astinuslab.sh smoke      # safe install checks; no VM launch
./astinuslab.sh status     # show running state
./astinuslab.sh logs       # tail API, runner, and analysis worker logs
./astinuslab.sh stop       # stop all processes
./astinuslab.sh restart    # restart
./astinuslab.sh cleanup    # destroy VMs and clear runtime traces
./astinuslab.sh scan-images           # add new images/definitions/* to targets.yaml
./astinuslab.sh scan-images --dry-run # preview only
./astinuslab.sh precook <image>       # make any image compatible (SSH + guest agent), role auto-detected
```

For cleanup of generated files (`storage/`, `site/`, `.pytest_cache/`,
`.worktrees/`, Vol3 symbols), see
[Generated and Runtime Files](../development/GENERATED_RUNTIME_FILES.md).

## Tests

Run the backend suite:

```bash
ASTINUSLAB_FAKE_LIBVIRT=1 .venv/bin/python -m pytest tests/python -q
```

Run with the CI coverage gate:

```bash
ASTINUSLAB_FAKE_LIBVIRT=1 .venv/bin/python -m pytest tests/python --cov=src/astinuslab --cov-fail-under=86 -q
```

Run a targeted module:

```bash
.venv/bin/python -m pytest tests/python/api/test_api.py -q
```

## Linting and Formatting

Ruff is configured in `pyproject.toml` for syntax errors, Pyflakes, import sorting, and pyupgrade:

```bash
.venv/bin/python -m ruff check .
```

Mypy is configured for Python 3.12:

```bash
.venv/bin/python -m mypy src tests/python ops/deploy
```

Shell scripts are expected to pass ShellCheck when `shellcheck` is installed:

```bash
shellcheck ops/host/*.sh
```

Frontend linting and formatting (requires Node.js 24 and `npm ci`):

```bash
npm run lint:js         # ESLint static analysis
npm run format:check    # Prettier format check
npm run test:js:unit    # Unit test runner
```

## Documentation Build

Install docs dependencies if needed:

```bash
.venv/bin/python -m pip install -e ".[docs]"
```

Build the site:

```bash
.venv/bin/python -m mkdocs build -f docs/mkdocs.yml
```

Serve locally:

```bash
.venv/bin/python -m mkdocs serve -f docs/mkdocs.yml --dev-addr localhost:9000
```

## Switching Between Modes

Both modes contend for the same Celery queue, libvirt domain names, and
host runner state. Pick one at a time. Changing only the API port is not
enough because both runners can still consume jobs from the same queue.

They also share the global privileged helpers under
`/usr/local/libexec/astinuslab/`, which are rendered with the owning
tree's path baked in. Whenever you switch modes, re-install the helpers
for the tree you are activating (commands below) — otherwise helper path
checks fail and jobs error out.

Stop and disable release services before starting dev mode:

```bash
sudo systemctl disable --now astinuslab-api@$USER astinuslab-runner@$USER astinuslab-analysis@$USER
sudo ./astinuslab.sh setup-host   # re-point global helpers at this checkout
./astinuslab.sh start
./astinuslab.sh status   # expected: Active runner mode: manual
```

Return to release mode after a dev session:

```bash
./astinuslab.sh stop
./astinuslab.sh cleanup     # remove orphan VMs the dev runner left behind
sudo /opt/astinuslab/current/ops/host/install_sudoers_astinuslab.sh \
  --install-dir /opt/astinuslab --run-user $USER \
  --helper-source-dir /opt/astinuslab/current/ops/host   # re-point helpers at the release tree
sudo systemctl start astinuslab-api@$USER astinuslab-runner@$USER astinuslab-analysis@$USER
```

If you really need both installations available, run only one runner at a
time. You may give dev a different API/docs port for UI testing, but do
not leave both host runners active unless they use separate Celery broker
and result database URLs.

```bash
echo 'ASTINUSLAB_API_PORT=8001' >> config/astinuslab.env
echo 'ASTINUSLAB_DOCS_PORT=9001' >> config/astinuslab.env
./astinuslab.sh restart
```

You will still need to make sure the two installations target different
storage directories and different libvirt domain prefixes (the dev
checkout already does this — it stores under `<repo>/storage`, while
release writes under `/opt/astinuslab/storage`).
