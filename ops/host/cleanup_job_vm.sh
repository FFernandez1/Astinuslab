#!/usr/bin/env bash
# cleanup_job_vm.sh
# Safely removes a job's VM and storage artifacts from libvirt.
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <job_id>"
  exit 1
fi

JOB_ID="$1"
if [[ ! "$JOB_ID" =~ ^[a-zA-Z0-9-]+$ || "$JOB_ID" == "base" ]]; then
  echo "Invalid JOB_ID" >&2
  exit 1
fi
VM_NAME="astinuslab-${JOB_ID}"
LIBVIRT_URI="${LIBVIRT_URI:-qemu:///system}"
STORAGE_ROOT="/var/lib/libvirt/images/astinuslab"
JOB_DIR="${STORAGE_ROOT}/${JOB_ID}"
HELPER="/usr/local/libexec/astinuslab/libvirt_storage_admin.sh"

echo "Cleaning up VM: $VM_NAME"

# 1. Destroy and undefine VM if it exists
if virsh -c "$LIBVIRT_URI" dominfo "$VM_NAME" >/dev/null 2>&1; then
  echo "Destroying VM..."
  virsh -c "$LIBVIRT_URI" destroy "$VM_NAME" >/dev/null 2>&1 || true
  
  # Wait for VM to actually stop (up to 5s)
  for _ in {1..10}; do
    # Capture first: grep -q + pipefail reports a running VM as gone (SIGPIPE).
    running="$(virsh -c "$LIBVIRT_URI" list --name 2>/dev/null || true)"
    if ! grep -q "^${VM_NAME}$" <<<"$running"; then
      break
    fi
    echo "Waiting for VM to stop..."
    sleep 0.5
  done

  echo "Undefining VM..."
  virsh -c "$LIBVIRT_URI" undefine "$VM_NAME" --nvram >/dev/null 2>&1 || true
fi

# 2. Remove job directory via helper (requires sudo)
if [[ -d "$JOB_DIR" ]]; then
  echo "Removing job storage: $JOB_DIR"
  sudo -n "$HELPER" remove_job_dir "$JOB_DIR"
fi

echo "Cleanup complete for $JOB_ID"
