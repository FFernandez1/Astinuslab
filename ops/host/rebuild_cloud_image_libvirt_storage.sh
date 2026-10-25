#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 5 ]]; then
  echo "Usage: $0 <job_id> <cloud_image_path> <ssh_public_key_file> <target_id> <os_variant> [disk_size_gb]"
  exit 1
fi

JOB_ID="$1"
IMAGE_PATH="$2"
SSH_PUBKEY_FILE="$3"
TARGET_ID="$4"
OS_VARIANT="$5"
DISK_SIZE="${6:-40}"
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LIBVIRT_URI="${LIBVIRT_URI:-qemu:///system}"
LIBVIRT_NETWORK="${LIBVIRT_NETWORK:-default}"

echo "[1/4] Cleaning up previous environment"
bash "$BASE_DIR/ops/host/cleanup_job_vm.sh" "$JOB_ID" || true

echo "[2/4] Preparing cloud image in libvirt native storage"
bash "$BASE_DIR/ops/host/bootstrap_cloud_image_libvirt_storage.sh" "$JOB_ID" "$IMAGE_PATH" "$SSH_PUBKEY_FILE" "$TARGET_ID" "$OS_VARIANT" "$DISK_SIZE"

echo "[3/4] Starting VM"
bash "$BASE_DIR/ops/host/start_cloud_image_libvirt_storage.sh" "$JOB_ID" "$TARGET_ID" "$OS_VARIANT"

echo "[4/4] Waiting for initial state"
bash "$BASE_DIR/ops/host/check_job_vm.sh" "$JOB_ID" || true
virsh -c "$LIBVIRT_URI" net-dhcp-leases "$LIBVIRT_NETWORK" || true
