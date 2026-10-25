#!/usr/bin/env bash
# Save a workload snapshot cache from a running (or suspended) job VM.
#
# Usage:
#   ./save_workload_cache.sh <job_id> <target_id> <output_cache_path>
#
# Example:
#   ./save_workload_cache.sh job-abc123 ubuntu-22.04 \
#       storage/workload-cache/ubuntu-22.04-wordpress-6.4-manual.qcow2
#
# The VM is suspended while the image is flattened, then resumed. The
# intermediate write goes to <output>.partial and is renamed only on success
# so a failed run never leaves a corrupt cache file.
set -euo pipefail

if [[ $# -lt 3 ]]; then
  echo "Usage: $0 <job_id> <target_id> <output_cache_path>" >&2
  exit 1
fi

JOB_ID="$1"
TARGET_ID="$2"
OUTPUT="$3"
VM_NAME="astinuslab-${JOB_ID}"
JOB_DISK="/var/lib/libvirt/images/astinuslab/${JOB_ID}/${TARGET_ID}-job.qcow2"
PARTIAL="${OUTPUT}.partial"

if [[ ! -f "$JOB_DISK" ]]; then
  echo "Job disk not found: $JOB_DISK" >&2
  exit 1
fi

echo "[1/4] Suspending VM: $VM_NAME"
virsh suspend "$VM_NAME"

echo "[2/4] Flattening overlay to standalone qcow2 (this may take 30–120 s)…"
qemu-img convert -f qcow2 -O qcow2 -p "$JOB_DISK" "$PARTIAL"

echo "[3/4] Committing cache file"
mv "$PARTIAL" "$OUTPUT"

echo "[4/4] Resuming VM"
virsh resume "$VM_NAME"

SIZE="$(du -sh "$OUTPUT" | cut -f1)"
echo "Done. Cache saved: $OUTPUT ($SIZE)"
