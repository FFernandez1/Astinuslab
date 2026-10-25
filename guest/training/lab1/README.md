# Lab 1 prepared guest

This directory builds the private `training-dvwa-single` Ubuntu 24.04 guest
used by the single-Target Playground exercise. The image contains DVWA 2.1 at
commit `3663d72c4c4f51ac5bcd9f0c8a16b9d890383347`, Apache/PHP, MariaDB and the
QEMU guest agent. It is intentionally vulnerable and must only be attached to
the exercise's isolated network.

## Inputs and build

Download the immutable GitHub codeload archive for the pinned commit and verify
SHA-256 `80713919db387f36ecc96abf273dca6fc5631804d03df612e7816490b7b6cee6`.
Use the repository's Ubuntu 24.04 cloud image as the other input, then run:

```bash
guest/training/lab1/build.py \
  --source images/definitions/noble-server-cloudimg-amd64.img \
  --dvwa-archive /path/to/dvwa-3663d72.tar.gz \
  --output images/definitions/training-dvwa-single.qcow2
```

`virt-customize` needs a working libguestfs appliance with network access while
packages are installed. The builder refuses a different DVWA archive, an
existing image output, a non-qcow2 result, a disk smaller than 16 GiB, and
external backing or data files. It atomically refreshes the tracked catalog
sidecar only after customization succeeds.
The qcow2 is ignored by Git and remains a private build artifact.

The candidate built on 2026-09-24 measured:

- source image SHA-256: `931dba2bec09d574f032485bda386c664481ef01208371957af28fad40bc7f2d`
- output image SHA-256: `155ddec8ca6ef22aec690f358b6858cfbb1fa028b91805a0e47b6a6e884eb140`
- physical bytes: `2571632640`; virtual bytes: `17179869184`
- Apache `2.4.58-1ubuntu8.15`, MariaDB `1:10.11.14-0ubuntu0.24.04.1`
- PHP `2:8.3+93ubuntu2`, QEMU guest agent `1:8.2.2+ds-0ubuntu1.18`
- curl `8.5.0-2ubuntu10.13`, OpenSSH server `1:9.6p1-3ubuntu13.14`

DVWA includes GNU GPL version 3 terms in `COPYING.txt`. Ubuntu package licenses
remain with their packages. Do not embed or redistribute the qcow2 in a package
until the intended distribution and corresponding-source obligations have been
reviewed; the current draft package remains image-free and unlocked.

## Guest contract

The only exercise address is `10.77.10.10/24`. DHCP, link-local addressing,
IPv6 router advertisements, global IPv6 and the default route are disabled.
Cloud-init may add a session-specific operator SSH key, but its network renderer
cannot replace the prepared static configuration.

On first boot, `astinuslab-training-dvwa-setup.service` creates the DVWA database
through DVWA's own setup flow. `astinuslab-training-readiness.service` then
checks the local services, database row count, HTTP login page, QGA enablement,
addresses, routes, IPv6 state and absence of DHCP lease state before atomically
publishing:

```json
"v1:training-dvwa-single:ready"
```

The observation directory is root-owned and non-symlinked. The clean image has
no readiness value, exercise proof file, injected operator key or SSH host
private key. A separate boot unit clears observations before dependency startup,
and the readiness unit is bound to Apache and MariaDB so stopping either service
removes the success scalar. Apache's `PrivateTmp` isolation is disabled
specifically so the runbook's `/tmp/astinuslab-lab1-proof` reset assertion
measures guest-visible overlay state.

## Verification scope

Offline contracts are in `tests/python/tooling/test_training_lab1.py`. Run them
with the repository's locked Python environment, then run ShellCheck on the
three shell files.

The measured candidate was also booted with one Target NIC and a separate
entry-only client. DVWA low security executed a harmless command-injection
marker, a valid-IP request did not contain it, impossible security rejected the
same injection while HTTP stayed healthy, and a fresh overlay removed the proof
file while preserving exploitability. This direct boot evidence is not a
creator clean-run attestation or locked package export.
