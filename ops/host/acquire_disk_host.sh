#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 6 ]]; then
  echo "Usage: $0 <job_id> <target_id> <distro> <version> <kernel_release> <job_image_path>" >&2
  exit 1
fi

JOB_ID="$1"
TARGET_ID="$2"
DISTRO="$3"
VERSION="$4"
KERNEL_RELEASE="$5"
JOB_IMAGE_PATH="$6"

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STORAGE_ROOT="${ASTINUSLAB_STORAGE_DIR:-$BASE_DIR/storage}"
TMP_DIR="$STORAGE_ROOT/tmp/$JOB_ID"
LOG_DIR="$STORAGE_ROOT/logs"
QEMU_IMG_LOG="$LOG_DIR/${JOB_ID}-qemu-img.log"
SCRATCH_DIR=""

mkdir -p "$TMP_DIR" "$LOG_DIR"
echo "qemu_img_log=$QEMU_IMG_LOG"
cleanup() {
  local status=$?
  if [[ -n "$SCRATCH_DIR" ]]; then
    rm -rf -- "$SCRATCH_DIR" || {
      echo "Could not remove disk scratch: $SCRATCH_DIR" >&2
      status=1
    }
  fi
  if (( status != 0 )); then
    echo "Offline disk acquisition failed; diagnostics: $QEMU_IMG_LOG" >&2
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ ! -f "$JOB_IMAGE_PATH" ]]; then
  echo "Job disk image not found: $JOB_IMAGE_PATH" >&2
  exit 1
fi

echo "disk_acquire_mode=offline_host"
echo "disk_source_image=$JOB_IMAGE_PATH"
echo "disk_target_id=$TARGET_ID"

# QEMU raw output must be seekable. Budget the virtual size, not the qcow2's
# sparse allocated size: conversion and compression coexist, then publication
# may copy the compressed file. Include conservative gzip expansion and 64 MiB
# headroom. This is a preflight, not a reservation against concurrent writers.
VIRTUAL_SIZE="$(qemu-img info --output=json -f qcow2 "$JOB_IMAGE_PATH" 2>>"$QEMU_IMG_LOG" | python3 -c \
  'import json,sys; n=json.load(sys.stdin)["virtual-size"]; assert isinstance(n,int) and 0 < n < 2**60; print(n)')"
FREE_BYTES="$(df -PB1 "$TMP_DIR" | python3 -c 'import sys; print(int(sys.stdin.readlines()[-1].split()[3]))')"
REQUIRED_BYTES=$((2 * (VIRTUAL_SIZE + VIRTUAL_SIZE / 100 + 1048576) + 67108864))
echo "disk_virtual_bytes=$VIRTUAL_SIZE"
echo "disk_required_free_bytes=$REQUIRED_BYTES"
echo "disk_available_bytes=$FREE_BYTES"
if (( FREE_BYTES < REQUIRED_BYTES )); then
  echo "Insufficient storage for seekable disk conversion: need $REQUIRED_BYTES bytes, available $FREE_BYTES at $TMP_DIR" | tee -a "$QEMU_IMG_LOG" >&2
  exit 1
fi

SCRATCH_DIR="$(mktemp -d "$TMP_DIR/offline-disk.XXXXXX")"
RAW_PATH="$SCRATCH_DIR/disk.raw"
GZ_PATH="$SCRATCH_DIR/disk.img.gz.partial"
qemu-img convert -f qcow2 -O raw "$JOB_IMAGE_PATH" "$RAW_PATH" 2>>"$QEMU_IMG_LOG"
if [[ "$(stat -c %s "$RAW_PATH")" != "$VIRTUAL_SIZE" ]]; then
  echo "Converted raw disk size differs from virtual size $VIRTUAL_SIZE" | tee -a "$QEMU_IMG_LOG" >&2
  exit 1
fi
if command -v pigz >/dev/null 2>&1; then
  echo "compressor=pigz"
  pigz -1 -c "$RAW_PATH" > "$GZ_PATH" 2>>"$QEMU_IMG_LOG"
else
  echo "compressor=gzip"
  gzip -1 -c "$RAW_PATH" > "$GZ_PATH" 2>>"$QEMU_IMG_LOG"
fi
# Release the raw scratch before publication creates its compressed staging copy.
rm -- "$RAW_PATH"
if [[ ! -s "$GZ_PATH" ]]; then
  echo "Offline host disk dump is empty or missing: $GZ_PATH" >&2
  exit 1
fi
bash "$BASE_DIR/ops/host/publish_disk_to_host.sh" \
    "$JOB_ID" "$GZ_PATH" "$DISTRO" "$VERSION" "$KERNEL_RELEASE"
