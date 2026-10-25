#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 4 ]]; then
  echo "Usage: $0 <job_id> <target_id> <os_variant> <host_share_dir>"
  exit 1
fi

JOB_ID="$1"
TARGET_ID="$2"
OS_VARIANT="$3"
HOST_SHARE_DIR="$4"
VM_NAME="astinuslab-${JOB_ID}"
LIBVIRT_URI="${LIBVIRT_URI:-qemu:///system}"
LIBVIRT_NETWORK="${LIBVIRT_NETWORK:-default}"
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
JOB_DIR="/var/lib/libvirt/images/astinuslab/$JOB_ID"
JOB_IMAGE="$JOB_DIR/${TARGET_ID}-job.qcow2"
SEED_ISO="$JOB_DIR/seed.iso"
MOUNT_TAG="astinuslab_out"
SERIAL_LOG="/tmp/astinuslab-${JOB_ID}-serial.log"
# Pre-create with open permissions so QEMU (root) writes to it and the
# runner user can read it for diagnostics.
touch "$SERIAL_LOG" && chmod 0644 "$SERIAL_LOG"

if [[ ! -d "$HOST_SHARE_DIR" ]]; then
  echo "host_share_dir does not exist: $HOST_SHARE_DIR"
  exit 1
fi

if [[ ! -f "$JOB_IMAGE" || ! -f "$SEED_ISO" ]]; then
  echo "Missing job artifacts in $JOB_DIR"
  exit 1
fi

bash "$BASE_DIR/ops/host/check_virtiofs_host_support.sh"

if virsh -c "$LIBVIRT_URI" dominfo "$VM_NAME" >/dev/null 2>&1; then
  echo "VM $VM_NAME already exists"
  exit 1
fi

VM_MEMORY="${VM_MEMORY_MB:-4096}"
VM_CPUS="${VM_VCPUS:-2}"
VM_MACHINE_ARGS=()

if [[ -n "${VIRT_MACHINE_TYPE:-}" ]]; then
  VM_MACHINE_ARGS+=(--machine "$VIRT_MACHINE_TYPE")
fi

virt-install \
  --connect "$LIBVIRT_URI" \
  --name "$VM_NAME" \
  --memory "$VM_MEMORY" \
  --vcpus "$VM_CPUS" \
  --memorybacking source.type=memfd,access.mode=shared \
  "${VM_MACHINE_ARGS[@]}" \
  --import \
  --disk "path=$JOB_IMAGE,format=qcow2,bus=virtio" \
  --disk "path=$SEED_ISO,device=cdrom" \
  --network "network=${LIBVIRT_NETWORK},model=virtio" \
  --filesystem "source=$HOST_SHARE_DIR,target=$MOUNT_TAG,driver.type=virtiofs" \
  --os-variant "$OS_VARIANT" \
  --graphics none \
  --video vga \
  --console file,path="${SERIAL_LOG}",target_type=serial \
  --noautoconsole

echo "VM created with native virtiofs: $VM_NAME"
echo "mount_tag=$MOUNT_TAG"
