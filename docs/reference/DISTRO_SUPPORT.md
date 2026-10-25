# Distro Support Matrix

AstinusLab supports Volatility 3 ISF generation for major x86_64 Linux distributions. Other CPU architectures are not supported.

## Identifying images

Distro, version, default user, and `os_variant` for each disk image in
`images_dir` are detected automatically by `virt-inspector` (from
`libguestfs-tools`) when the image is uploaded or downloaded. Results are
persisted as a `<image>.meta.json` sidecar and consumed by catalog
auto-discovery, so adding support for a new distro release no longer requires
editing source. If `virt-inspector` is unavailable, AstinusLab falls back to
filename heuristics (`noble`, `bookworm`, `ubuntu-24.04`, etc.); the image admin
page lets you edit detected fields or trigger re-inspection. See
[Advanced Configuration → `[image_inspection]`](ADVANCED_CONFIG.md) for tuning
the inspection timeout or disabling it.

The matrix below documents which distros currently have a working ISF / debug
symbol pipeline, independent of how they are identified.

| Distribution | Verified releases | Existing DWARF | debuginfod | debug pkg | Notes |
|--------------|--------------------|-----|------------|-----------|-------|
| Ubuntu | 20.04, 22.04, 24.04 | Guest fallback | Optional | Host/live apt | Requires matching `ddebs` or installable debug packages. |
| Ubuntu | 25.04, 25.10 | Guest fallback | Optional | Host/live apt | Requires matching `ddebs` or installable debug packages; unmapped future releases fall through to Launchpad codename discovery. |
| Debian | 10, 11, 12 | Guest fallback | Optional | Host/live apt | Requires matching debug symbol packages. |
| Debian | 13 | Guest fallback | Optional | Live apt | Host fetch codename support is not currently mapped; use live guest or configured repo support. |
| Fedora | 38-44 | Guest fallback | Optional | Host/live dnf | Requires matching `kernel-debuginfo` packages. |
| CentOS Stream | 8, 9 | Guest fallback | Optional | Host/live dnf | Requires matching debuginfo repositories. |
| Rocky Linux | 8, 9 | Guest fallback | Optional | Host/live dnf | Requires matching debuginfo repositories. |
| AlmaLinux | 8, 9 | Guest fallback | Optional | Host/live dnf | Requires matching debuginfo repositories. |
| RHEL | 8, 9 | Guest fallback | Optional | Live dnf | Requires Red Hat repositories or equivalent debuginfo packages. |
| Oracle Linux | 8, 9 | Guest fallback | Optional | Host/live dnf | Treated as a RHEL-compatible family when debuginfo packages are available. |
| openSUSE Leap | 15.x | Guest fallback | Optional | Host/live zypper | Requires matching debug packages from enabled repositories. |
| SUSE Linux Enterprise Server | 15.x | Guest fallback | Optional | Live zypper | Requires access to matching SLES debug packages. |
| Arch Linux | Rolling | Guest fallback | Optional | Live pacman | Kernel/debug package availability depends on the current package set and mirrors. |
| Alpine Linux | 3.x | Required | Optional | Limited apk | A matching DWARF-bearing debug kernel is required; BTF alone is unsupported. |
| Kali Linux | Rolling | Guest fallback | Optional | Debian-family apt | Treated as a Debian derivative when package metadata provides matching debug symbols. |
| Parrot OS | Rolling | Guest fallback | Optional | Debian-family apt | Treated as a Debian derivative when package metadata provides matching debug symbols. |

## Scope

- Supported architecture: `x86_64` only.
- Supported artifact: Linux Volatility 3 ISF JSON under `vol3/symbols/linux/`.
- Supported analysis output: job-local Volatility plugin output under `storage/dumps/<job_id>/vol3/`.
- Unsupported: ARM, AArch64, ppc64le, s390x, non-Linux guests, and kernels without accessible debug symbols.

## Practical Requirements

- The guest must boot far enough for AstinusLab to detect the running kernel.
- The exact kernel debug package must be available from distro repositories or installable inside the guest.
- Repository access must work from the host fetch flow or from the guest live flow.
- The generated ISF must be valid for the detected kernel before an ISF-only job can succeed.
