#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <job_id>"
  exit 1
fi

JOB_ID="$1"
VM_NAME="astinuslab-${JOB_ID}"
LIBVIRT_URI="${LIBVIRT_URI:-qemu:///system}"

echo "VM state:"
virsh -c "$LIBVIRT_URI" dominfo "$VM_NAME"
echo

echo "Interfaces detectadas:"
virsh -c "$LIBVIRT_URI" domifaddr "$VM_NAME" || true
echo

echo "Consola serie:"
virsh -c "$LIBVIRT_URI" ttyconsole "$VM_NAME" || true
echo

echo "Sugerencia SSH:"
echo "  ssh <usuario_del_guest>@<IP_DE_LA_VM>"
