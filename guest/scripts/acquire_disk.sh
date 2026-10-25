#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <output_path>" >&2
  exit 1
fi

OUTPUT_PATH="$1"

if [[ $EUID -ne 0 ]]; then
  echo "This script requires root privileges" >&2
  exit 1
fi

# Auto-detect the root block device by stripping partition number
ROOT_SOURCE="$(findmnt -n -o SOURCE / 2>/dev/null || echo '')"
DISK=""
if [[ -n "$ROOT_SOURCE" ]]; then
  # Strip the trailing partition-number run only.  `${var%%[0-9]*}` is wrong
  # here: on /dev/nvme0n1 it would match from the "0" and yield /dev/nvme.
  # extglob's +([0-9]) anchors the match to the trailing digits.
  shopt -s extglob
  DISK="${ROOT_SOURCE%%+([0-9])}"
  shopt -u extglob
fi

# Fallback detection
if [[ -z "$DISK" ]] || [[ ! -b "$DISK" ]]; then
  if [[ -b /dev/vda ]]; then
    DISK=/dev/vda
  elif [[ -b /dev/sda ]]; then
    DISK=/dev/sda
  else
    echo "ERROR: cannot determine root disk device" >&2
    exit 1
  fi
fi

echo "disk_device=$DISK"

OUTPUT_DIR="$(dirname "$OUTPUT_PATH")"
mkdir -p "$OUTPUT_DIR"

if mountpoint -q "$OUTPUT_DIR" 2>/dev/null; then
  sync
fi

# Dump with dd and compress. Use pigz (parallel gzip) when available for
# significantly faster compression on multi-core guests; fall back to gzip.
if command -v pigz >/dev/null 2>&1; then
  echo "compressor=pigz"
  dd if="$DISK" bs=4M status=none | pigz -1 > "$OUTPUT_PATH"
else
  echo "compressor=gzip"
  dd if="$DISK" bs=4M status=none | gzip -1 > "$OUTPUT_PATH"
fi
sync

if [[ ! -f "$OUTPUT_PATH" ]] || [[ ! -s "$OUTPUT_PATH" ]]; then
  echo "ERROR: disk dump output is empty or missing: $OUTPUT_PATH" >&2
  exit 1
fi

echo "dump_path=$OUTPUT_PATH"
