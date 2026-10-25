# Ubuntu 22.04 Base Image Manifest

## Target

- distro: ubuntu
- version: 22.04
- architecture: amd64
- use: base target image for AstinusLab jobs

## Recommended Source

Download from Ubuntu cloud images:

- `https://cloud-images.ubuntu.com/releases/22.04/release/ubuntu-22.04-server-cloudimg-amd64-disk-kvm.img`

Recommended name:

- `ubuntu-22.04-server-cloudimg-amd64-disk-kvm.img`

## Expected Properties

- cloud-init enabled
- default user: `ubuntu`
- QEMU/KVM-compatible disk image
- suitable for catalog target `ubuntu-22-04-default`

## Recommended Local Paths

- base image: `images/definitions/ubuntu-22.04-server-cloudimg-amd64-disk-kvm.img`
- per-job overlay: `/var/lib/libvirt/images/astinuslab/<job_id>/<job_id>-job.qcow2`

## Metadata To Record

- download URL
- download date
- SHA-256 checksum of the downloaded image
- any local preparation steps applied before use
