#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 5 ]]; then
  echo "Usage: $0 <job_id> <cloud_image_path> <ssh_public_key_file> <target_id> <os_variant>"
  exit 1
fi

JOB_ID="$1"
IMAGE_PATH="$2"
SSH_PUBKEY_FILE="$3"
TARGET_ID="$4"
OS_VARIANT="$5"
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LIBVIRT_URI="${LIBVIRT_URI:-qemu:///system}"
LIBVIRT_NETWORK="${LIBVIRT_NETWORK:-default}"

echo "[1/5] Cleaning up previous environment"
bash "$BASE_DIR/ops/host/cleanup_job_vm.sh" "$JOB_ID" || true

echo "[2/5] Preparing cloud image in libvirt native storage"
bash "$BASE_DIR/ops/host/bootstrap_cloud_image_libvirt_storage.sh" "$JOB_ID" "$IMAGE_PATH" "$SSH_PUBKEY_FILE" "$TARGET_ID" "$OS_VARIANT"

echo "[3/5] Preparing host share for virtiofs"
SHARE_OUTPUT="$(bash "$BASE_DIR/ops/host/prepare_host_dump_share.sh" "$JOB_ID")"
echo "$SHARE_OUTPUT"
HOST_SHARE_DIR="$(printf '%s\n' "$SHARE_OUTPUT" | awk -F= '/^staging_root=/{print $2}' | tail -n 1)"

if [[ -z "$HOST_SHARE_DIR" ]]; then
  echo "Could not resolve HOST_SHARE_DIR" >&2
  exit 1
fi

echo "[4/5] Starting VM with native virtiofs"
bash "$BASE_DIR/ops/host/start_cloud_image_libvirt_storage_virtiofs.sh" "$JOB_ID" "$TARGET_ID" "$OS_VARIANT" "$HOST_SHARE_DIR"

echo "[5/5] Waiting for initial state"
bash "$BASE_DIR/ops/host/check_job_vm.sh" "$JOB_ID" || true
virsh -c "$LIBVIRT_URI" net-dhcp-leases "$LIBVIRT_NETWORK" || true

echo
echo "Next step:"
echo "  bash ops/host/mount_output_share_remote.sh <IP_GUEST> storage/ssh_keys/<nombre-clave>"
