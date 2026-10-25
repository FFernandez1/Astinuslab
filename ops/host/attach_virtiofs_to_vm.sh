#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <job_id> <vm_name>"
  exit 1
fi

JOB_ID="$1"
VM_NAME="$2"
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
XML_PATH="$BASE_DIR/storage/tmp/$JOB_ID/virtiofs-${JOB_ID}.xml"
LIBVIRT_URI="${LIBVIRT_URI:-${ASTINUSLAB_LIBVIRT_URI:-qemu:///system}}"

if [[ ! -f "$XML_PATH" ]]; then
  echo "virtiofs XML does not exist. Run render_virtiofs_xml.sh first" >&2
  exit 1
fi

if ! virsh -c "$LIBVIRT_URI" attach-device "$VM_NAME" "$XML_PATH" --live --config; then
  echo "attach_failed=virtiofs_requires_shared_memory"
  echo "hint=usa_el_fallback_scp_o_redefine_la_vm_con_memoria_compartida"
  exit 1
fi
