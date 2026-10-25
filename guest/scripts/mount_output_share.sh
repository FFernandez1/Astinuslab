#!/usr/bin/env bash
set -euo pipefail

TAG="${1:-astinuslab_out}"
MOUNTPOINT="${2:-/mnt/astinuslab-out}"

if [[ "${ASTINUSLAB_SKIP_ROOT_CHECK:-0}" != "1" ]] && [[ "$(id -u)" -ne 0 ]]; then
  echo "Este script requiere privilegios de root" >&2
  exit 1
fi

mkdir -p "$MOUNTPOINT"

if mountpoint -q "$MOUNTPOINT"; then
  echo "already_mounted=true"
  echo "mountpoint=$MOUNTPOINT"
  exit 0
fi

VFS_ERR="/tmp/astinuslab-mount-vfs.err"
P9_ERR="/tmp/astinuslab-mount-9p.err"

# Try virtiofs first (best perf, requires guest kernel >= 5.4).
if mount -t virtiofs "$TAG" "$MOUNTPOINT" 2>"$VFS_ERR"; then
  echo "mounted=true"
  echo "mountpoint=$MOUNTPOINT"
  echo "mount_type=virtiofs"
  exit 0
fi

echo "virtiofs mount failed, falling back to 9p" >&2
if [[ -s "$VFS_ERR" ]]; then cat "$VFS_ERR" >&2 || true; fi

# Fall back to 9p, using the companion tag emitted by render_virtiofs_xml.sh.
if mount -t 9p -o trans=virtio,version=9p2000.L "${TAG}_9p" "$MOUNTPOINT" 2>"$P9_ERR"; then
  echo "mounted=true"
  echo "mountpoint=$MOUNTPOINT"
  echo "mount_type=9p"
  exit 0
fi

echo "9p mount also failed" >&2
if [[ -s "$P9_ERR" ]]; then cat "$P9_ERR" >&2 || true; fi
exit 1
