# Getting Started

AstinusLab provisions Linux VMs on KVM/libvirt, runs scenario or workload setup, captures memory and disk artifacts from the host, records checksums and metadata, then tears down lab resources.


The tested host is Ubuntu 24.04 LTS on x86_64 with KVM. Other host recipes are experimental. Download staging space is separate from operating capacity: strict smoke warns below 150 GB free disk or 16 GB RAM; each lab still needs its own resource budget.

## Pick a Run Mode

Use one mode at a time:

| Mode | Use when | Command entrypoint |
| --- | --- | --- |
| Development | Editing code, testing locally, or iterating on docs | `./astinuslab.sh` |
| Release | Running AstinusLab as a long-lived local service | `ops/deploy/install.sh` plus systemd units |

Do not run the development runner and the systemd runner together. They share Celery, libvirt, and VM naming resources.

Release installs enable admin authentication by default. A clean install shows
the generated raw token at creation and once more in the final credentials
block; save it immediately. Updates preserve the existing token hash. For an
explicitly local development-only no-auth install, pass
`--allow-unauthenticated-local` to `ops/deploy/install.sh`. See
[Authentication](../api/authentication.md) for hash and flag rules.

## First Release Install

Use the installer as a guided sequence: prerequisites → strict preflight →
install → save the token → download images → strict smoke → UI.

```bash
bash ops/deploy/install.sh --check --install-dir /opt/astinuslab --service-user "$USER"
sudo ops/deploy/install.sh --install-dir /opt/astinuslab --service-user "$USER"

cd /opt/astinuslab/current
sudo -u "$USER" ./ops/images/download_images.sh
./astinuslab.sh smoke --require-running
```

The preflight command is read-only, prints one final readiness verdict, and
returns nonzero when a required dependency or trust check fails. Save the token
from `CREDENTIALS — SAVE NOW`, then open `http://localhost:8000` after strict
smoke passes. The image set is approximately 3 GB compressed; keep at least
10 GB free for downloads and overlays.

Volatility 3 analysis is optional. After install, this read-only command prints
the recommended-version setup and application readiness checks without
downloading or installing either external tool:

```bash
cd /opt/astinuslab/current
./astinuslab.sh guide-volatility
```

## First Local Start

Install the system prerequisites first — packages, `libvirt`/`kvm` group
membership, and Python 3.12 or 3.13 — as listed in
[Running Locally § Prerequisites](running-locally.md#prerequisites).

```bash
./astinuslab.sh setup
sudo ./astinuslab.sh setup-host --with-mac
./ops/images/download_images.sh
./astinuslab.sh start
./astinuslab.sh smoke --require-running
```

`./astinuslab.sh setup` seeds every live catalog in `config/catalog/` from its
`*.example.yaml` counterpart when the live file is missing — no manual copying
is needed (see [Configuration](configuration.md)).

Open `http://localhost:8000`.

If you want the documentation server too:

```bash
./astinuslab.sh start --docs
```

The docs server uses `http://localhost:9000` by default.

## Add a Cloud Image

Place Linux cloud images in `images/definitions/`. Existing catalog entries reference filenames such as:

| Target ID | Expected image filename |
| --- | --- |
| `ubuntu-22-04-default` | `ubuntu-22.04-server-cloudimg-amd64-disk-kvm.img` |
| `ubuntu-24.04` | `noble-server-cloudimg-amd64.img` |
| `debian-12` | `debian-12-genericcloud-amd64.qcow2` |
| `fedora-40` | `Fedora-Cloud-Base-Generic.x86_64-40-1.14.qcow2` |

To scan newly added images:

```bash
./astinuslab.sh scan-images --dry-run
./astinuslab.sh scan-images
./astinuslab.sh restart
```

Stock cloud images usually boot as-is. If a target never becomes SSH-reachable,
or you are adding an offensive image such as Kali/Parrot, **precook** it first —
one command auto-detects whether the image is a target or an attacker and makes
it compatible (SSH key + `qemu-guest-agent`):

```bash
./astinuslab.sh precook <image> --resolve-only   # preview only, no changes
./astinuslab.sh precook <image>                  # apply
```

See [Pre-baked Images](../user/PREBAKED_IMAGES.md) for details.

## First capture in Playground

1. Open `http://localhost:8000` and log in if authentication is enabled. Home opens **Playground**. **Help** redirects to the separate local documentation server, normally `http://localhost:9000`, with the selected language. For development, start with `./astinuslab.sh start --docs`; to serve guides alongside an existing API, use `.venv/bin/python -m mkdocs serve -f docs/mkdocs.yml --dev-addr localhost:9000` from a docs-enabled checkout. If Help cannot connect, start that server or read these source guides. The link uses the browser's `localhost`; remote browsers need access to a documentation server on their own machine or an explicitly configured forwarding setup.
2. Choose **+ Target**, select an available image, and leave **No workload** selected for the first capture.
3. Review resources and the default isolated network, then choose **Create target**. The session is created automatically; there is no separate Start session control.
4. Wait until the VM is running, then select it and capture memory or disk from its details. Wait for capture completion before downloading the artifact, manifest, and checksum.
5. Find retained evidence in **Storage**, including after **End Session**. Ending a session starts asynchronous cleanup; wait for the terminal result and investigate any cleanup failure.

To add a vulnerable workload, first follow [Prepare workloads](../user/PREBAKED_IMAGES.md): online installers need a matching prepared cache/image on isolated networks, or an explicitly chosen egress-enabled network. The wizard reviews the form and displays this prerequisite; it does not check cache availability or host readiness.

Next, [analyze the exact capture](../user/VOLATILITY3.md) and [export its evidence](../reference/FORENSIC_INTEGRITY.md). Record kernel identity before ending the VM. [Portable lab packages](../user/PORTABLE_LABS.md) remain disabled, experimental and outside supported 1.0.0 workflows.

## Repeatable acquisition with Jobs

Open **Jobs → New Job** from the sidebar. Select a target, use `immediate-capture` for direct acquisition or `lab-session` for a manual lab, launch, and follow the job until its artifacts are ready. Jobs also provides the workload-cache preparation path. End an ordinary Playground session and confirm cleanup before switching to Jobs.

## Next Steps

- [Running Locally](running-locally.md) covers the full setup and runbook.
- [Configuration](configuration.md) explains `astinuslab.toml`, launcher env, and catalogs.
- [Examples](examples.md) gives common API calls.
- [Troubleshooting](troubleshooting.md) lists common errors and fixes.
