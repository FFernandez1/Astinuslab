# Kali Attacker Base Image Manifest

## Target

- distro: kali
- version: 2025.4
- use: attacker VM for lab sessions with reliable SSH access

## Source

Locally derived image built from:

- `kali-linux-2025.4-qemu-amd64.qcow2`

## Recommended Name

- `kali-linux-2025.4-attacker-base.qcow2`

## Expected Contents

- `openssh-server` installed and enabled
- `cloud-init` present
- `qemu-guest-agent` present
- attacker user (`kali`) prepared for key-based access
- `cloud-init` state cleaned before use as a base image

## Recommended Local Paths

- attacker base image: `images/definitions/kali-linux-2025.4-attacker-base.qcow2`
- per-job overlay: `/var/lib/libvirt/images/astinuslab/<job_id>-attacker/<job_id>-attacker-job.qcow2`

## Metadata To Record

- original source image
- prebake date
- SHA-256 checksum of the derived image
- expected SSH user
- prebake script/tool used
