<p align="center">
  <img src="docs/assets/logo.svg" alt="AstinusLab logo" width="96">
</p>

<h1 align="center">AstinusLab</h1>

<p align="center">
  An attack-and-forensics playground for Linux virtual machines on KVM/libvirt.
</p>

AstinusLab is a self-hosted security-research lab. It builds deliberately
vulnerable services, boots attacker machines next to them, and captures memory and
disk evidence from the hypervisor, so you can analyze it with Volatility 3. It is
for blue-team lab builders, incident responders, researchers and educators, with
pentest workflows available when you need an attacker VM on the wire.

- **No guest footprint.** Memory is acquired from the hypervisor with libvirt
  `virDomainCoreDump` as an ELF core dump. No kernel modules or agents are
  installed in the guest.
- **Evidence you can trust.** Every RAM dump and disk image is published with a
  canonical filename, a manifest and a SHA-256 checksum.
- **Analysis-ready.** AstinusLab identifies the exact running kernel and builds or
  reuses the matching Volatility 3 Linux symbol file (ISF).
- **Disposable labs.** VMs and networks are created for the experiment and
  destroyed afterwards. Evidence stays in storage.

> [!WARNING]
> AstinusLab runs intentionally vulnerable software and attacker tooling. Use it
> only on hosts you own or are explicitly authorized to test. Keep guests on
> isolated lab networks, and never expose them or AstinusLab itself to the
> internet. Captures can contain credentials and personal data: store, share and
> delete them as you would any other evidence.

## Contents

- [What you can do](#what-you-can-do)
- [Requirements](#requirements)
- [Install](#install)
- [Analyze with Volatility 3](#analyze-with-volatility-3)
- [What ships in the catalog](#what-ships-in-the-catalog)
- [Scope of 1.0.0](#scope-of-100)
- [Security model](#security-model)
- [Documentation](#documentation)
- [Contributing](#contributing)
- [License](#license)

## What you can do

### Playground: ad-hoc labs

The Playground is the front door and opens at `http://localhost:8000`. Build a
lab interactively, attack it, and capture evidence when you are ready.

1. Create isolated or routed networks with custom CIDRs.
2. Spawn target and attacker VMs from catalog images.
3. Run your experiment or attack.
4. Trigger memory and disk captures, and watch the topology and artifacts update.
5. End the session. VMs and networks are cleaned up in the background; captured
   evidence stays in storage.

One Playground session is active at a time. It is created when you add your
first network or VM. See [Playground Mode](docs/user/PLAYGROUND.md).

### Jobs: repeatable acquisitions

Jobs (at `/jobs`) run a scenario from start to finish without interaction:

1. Provision a clean VM with cloud-init.
2. Detect the distribution and running kernel, and read guest metadata through
   the QEMU guest agent.
3. Run the scenario.
4. Acquire the RAM dump and/or disk image from the host.
5. Publish artifacts with SHA-256 checksums.
6. Destroy the VM and release all resources.

Each Job uses one execution mode:

| Mode | What happens |
|------|--------------|
| `immediate` | Baseline capture: acquire memory right after the guest boots. |
| `post_script` | Run a workload script on the guest, then acquire. |
| `manual_trigger` | Prepare the guest, then wait for an operator to trigger capture from the UI or API. |

`post_script` and `manual_trigger` support `capture_type` `memory`, `disk` or
`both`.

### Interactive lab sessions

A `manual_trigger` Job with a workload gives you a guided lab. AstinusLab installs
a vulnerable service stack on the target, can boot a Kali or other attacker VM on
the same isolated network, snapshots the target so you can reset it, and shows
the service URLs and credentials. You work the lab and capture evidence when ready.

## Requirements

- Ubuntu 24.04 LTS on x86_64 with hardware virtualization (KVM)
- libvirt and QEMU
- Python 3.12 or 3.13
- At least 10 GB of free disk space for base images (about 3 GB download)

AstinusLab 1.0.0 is tested on that host with those two Python versions.

## Install

Install the host packages and join the `libvirt` and `kvm` groups first. These
steps apply to both run modes:

```bash
sudo apt update
sudo apt install -y \
  git build-essential pkg-config acl curl wget ca-certificates \
  python3 python3-dev python3-venv python3-pip \
  libvirt-dev qemu-kvm qemu-utils libvirt-daemon-system libvirt-clients virtinst \
  cloud-image-utils cloud-init rsync openssh-client sudo \
  libguestfs-tools rpm2cpio cpio
sudo usermod -aG libvirt,kvm "$USER"
```

Log out and back in so the group membership takes effect, then check KVM access
with `virsh -c qemu:///system list`.

Get the source:

```bash
git clone https://github.com/FFernandez1/Astinuslab.git AstinusLab
cd AstinusLab
```

Then choose **one** run mode. Release mode and development mode must not run at
the same time; see [Running Locally](docs/usage/running-locally.md).

### Release mode (recommended for operators)

Installs systemd services under `/opt/astinuslab` that survive reboots.

```bash
# 1. Read-only preflight: ends with READY, READY WITH WARNINGS or NOT READY
bash ops/deploy/install.sh --check --install-dir /opt/astinuslab --service-user "$USER"

# 2. Install
sudo ops/deploy/install.sh --install-dir /opt/astinuslab --service-user "$USER"

# 3. Download base images and verify the running services
cd /opt/astinuslab/current
sudo -u "$USER" ./ops/images/download_images.sh
./astinuslab.sh smoke --require-running
```

A release install enables admin authentication. The installer shows the
generated admin token at creation and again under `CREDENTIALS — SAVE NOW`.
**Save it then:** only its hash is stored.

Open `http://localhost:8000` and sign in with the token.

To update, re-run the installer from the new source tree; it keeps your
configuration, catalogs and token hash. To remove, run
`sudo bash ops/deploy/uninstall.sh`. [Running Locally](docs/usage/running-locally.md)
covers storage outside the install tree, the optional `--split-users` service
identities, downgrades and uninstall caveats.

### Development mode (run from the checkout)

```bash
./astinuslab.sh setup                       # venv, locked dependencies, example configs, guest SSH key
sudo ./astinuslab.sh setup-host --with-mac  # privileged helpers, sudoers, AppArmor/SELinux and storage ACLs
./ops/images/download_images.sh             # Ubuntu 22.04/24.04, Debian 12, Fedora 40 cloud images
./astinuslab.sh start                       # applies DB migrations, starts API and worker
./astinuslab.sh smoke --require-running     # checks services and host health
```

Open `http://localhost:8000`. A development checkout runs without authentication
on loopback. Add `--docs` to `start` to serve this documentation at
`http://localhost:9000`.

Configuration lives in `config/astinuslab.env` and `astinuslab.toml`; exported
`ASTINUSLAB_*` variables override the TOML file. See the
[configuration reference](docs/usage/configuration.md). For contributor tooling,
tests and database commands, see [Development setup](docs/development/setup.md).

## Analyze with Volatility 3

Volatility 3 Framework 2.28.0 and `dwarf2json` are optional, operator-supplied
tools. AstinusLab never downloads them or accepts their licenses for you. Print
the recommended version, copy-paste setup and readiness checks with:

```bash
./astinuslab.sh guide-volatility
```

Then confirm readiness at `/api/v1/volatility/readiness`.

- Linux ISFs are reusable and stored under `vol3/symbols/linux/`. Each Job records
  whether it built a new ISF or reused one.
- ISF-only Jobs boot a fresh VM just to identify the kernel and build its ISF.
  They fail if no valid ISF is produced.
- Plugin output is stored per Job under `storage/dumps/<job_id>/vol3/`.

See the [Volatility 3 guide](docs/user/VOLATILITY3.md) for installation,
verification, upgrade and rollback.

## What ships in the catalog

### Targets

| ID | Distribution | SSH user |
|----|--------------|----------|
| `ubuntu-22-04-default` | Ubuntu 22.04 | `ubuntu` |
| `ubuntu-24.04` | Ubuntu 24.04 | `ubuntu` |
| `debian-12` | Debian 12 | `debian` |
| `fedora-40` | Fedora 40 (end of life) | `fedora` |
| `ubuntu-20-04-legacy` | Ubuntu 20.04 (end of standard support, disabled by default) | `ubuntu` |

Fedora 40 and Ubuntu 20.04 no longer get regular security updates. They are kept
as deliberately dated lab targets; keep them on the isolated lab network. Add
images through auto-discovery or `config/catalog/targets.yaml`. See
[Distro Support](docs/reference/DISTRO_SUPPORT.md) for ISF coverage.

### Workloads

| ID | What it provides |
|----|------------------|
| `wordpress-6.4` | WordPress 6.4 with MySQL: SQLi, XSS and RCE exercises |
| `dvwa` | Damn Vulnerable Web App: SQLi, XSS, CSRF, file upload, command injection |
| `full-php-stack` | WordPress and DVWA on the same VM |
| `juice-shop` | OWASP Juice Shop |
| `vulnhub-basic-pentesting` | Apache/WordPress, SSH and Samba pentesting target |
| `gitlab-ce-11` | GitLab CE 11.4.7 SSRF/CSRF-to-RCE lab |

To write your own, see [Workloads and Scenarios](docs/development/WORKLOADS.md).

### Attackers

The shipped attacker catalog defines Kali and Parrot. Add more images in
`config/catalog/attackers.yaml`.

## Scope of 1.0.0

- **Supported:** localhost or trusted-local operation; vulnerable web-app labs,
  attacker VMs, memory and disk captures with manifest and SHA-256 sidecars, and
  Volatility 3 analysis of Linux guests.
- **Experimental:** Windows targets exist only at the catalog and analysis layers
  (including Volatility 3 PDB downloads). Automated Windows provisioning is on
  the roadmap.
- **Experimental and disabled by default:** portable lab packages. They are
  outside the supported 1.0.0 workflows; see the
  [experimental package guide](docs/user/PORTABLE_LABS.md).
- **Roadmap:** continuous live telemetry, richer reports, stronger hostile-guest
  hardening, and YAML recipes. Labs can be exported and imported as JSON recipes
  today.

## Security model

- **Loopback by default.** The server binds to `127.0.0.1:8000`. It refuses a
  non-loopback bind unless admin authentication is enabled or
  `allow_unsafe_network_bind_without_auth` is set explicitly.
- **Authentication.** Release installs enable single-admin authentication. API
  clients send `Authorization: Bearer <admin-token>`; browser users sign in at
  `/login`. See [API Authentication](docs/api/authentication.md).
- **Browser protection.** Every state-changing request from a browser must be
  same-origin and carry a CSRF token, with or without authentication.
- **Least privilege on the host.** Services cannot write to installed source.
  Privileged helpers are limited by root-owned policy. The optional
  `--split-users` install separates the API, runner and analysis identities; see
  [Running Locally](docs/usage/running-locally.md).

Authentication protects AstinusLab, not the vulnerable guests. Before using any
shared network, add TLS, a VPN or reverse proxy, and firewall rules, and keep
guest services isolated.

To report a vulnerability, follow [Security reporting and
support](docs/community/SECURITY.md). Do not post sensitive reports in public
issues. Maintenance is best effort, without a guaranteed response time.

## Documentation

| Topic | Where |
|-------|-------|
| First steps | [Getting Started](docs/usage/getting-started.md) |
| Install, update, remove | [Running Locally](docs/usage/running-locally.md) |
| Configuration | [Configuration](docs/usage/configuration.md) |
| Playground | [Playground Mode](docs/user/PLAYGROUND.md) |
| Volatility 3 | [Volatility 3 guide](docs/user/VOLATILITY3.md) |
| Evidence handling | [Forensic Integrity](docs/reference/FORENSIC_INTEGRITY.md) |
| HTTP API | [API Endpoints](docs/api/endpoints.md) |
| Architecture | [Architecture Overview](docs/architecture/overview.md) |
| Problems | [Troubleshooting](docs/usage/troubleshooting.md) |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to report problems and the
contribution policy, [Development setup](docs/development/setup.md) for the
contributor environment, and [Testing](docs/development/testing.md) for checks.
The Python test suite runs without real VMs:

```bash
ASTINUSLAB_FAKE_LIBVIRT=1 .venv/bin/python -m pytest tests/python -q
```

## License

AstinusLab is licensed under the [Volatility Software License 1.0](LICENSE)
(`Copyright 2026 Francisco Fernandez`), the license Volatility 3 uses. You may
use, share and build on AstinusLab for free, including commercially. If you make
changes, additions or software built with it available to others, the license
requires you to publish their source code; see [`LICENSE`](LICENSE) for the exact
terms. The VSL is not an OSI-approved open-source license, so GitHub lists it as
"Other". AstinusLab is not part of Volatility and is not made or endorsed by the
Volatility Foundation. The software comes as is, without warranty; see the No
Liability section of the license.

Third-party components keep their own licenses; see
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) and [`LICENSES/`](LICENSES/README.md).
Volatility 3 and `dwarf2json` are not included.
