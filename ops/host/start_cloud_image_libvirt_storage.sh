#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 3 ]]; then
  echo "Usage: $0 <job_id> <target_id> <os_variant>"
  exit 1
fi

JOB_ID="$1"
TARGET_ID="$2"
OS_VARIANT="$3"
VM_NAME="astinuslab-${JOB_ID}"
LIBVIRT_URI="${LIBVIRT_URI:-qemu:///system}"
LIBVIRT_NETWORK="${LIBVIRT_NETWORK:-default}"
JOB_DIR="/var/lib/libvirt/images/astinuslab/$JOB_ID"
JOB_IMAGE="$JOB_DIR/${TARGET_ID}-job.qcow2"
SEED_ISO="$JOB_DIR/seed.iso"
SERIAL_LOG="/tmp/astinuslab-${JOB_ID}-serial.log"
touch "$SERIAL_LOG" && chmod 0644 "$SERIAL_LOG"

if [[ ! -f "$JOB_IMAGE" || ! -f "$SEED_ISO" ]]; then
  echo "Missing job artifacts in $JOB_DIR"
  exit 1
fi

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
  "${VM_MACHINE_ARGS[@]}" \
  --import \
  --disk "path=$JOB_IMAGE,format=qcow2,bus=virtio" \
  --disk "path=$SEED_ISO,device=cdrom" \
  --network "network=${LIBVIRT_NETWORK},model=virtio" \
  --os-variant "$OS_VARIANT" \
  --graphics none \
  --video vga \
  --console file,path="${SERIAL_LOG}",target_type=serial \
  --noautoconsole

echo "VM created: $VM_NAME"
