#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <job_id> <vm_name>"
  exit 1
fi

JOB_ID="$1"
VM_NAME="$2"
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

echo "[1/3] Preparing host staging"
bash "$BASE_DIR/ops/host/prepare_host_dump_share.sh" "$JOB_ID"

echo "[2/3] Generando XML de virtiofs"
bash "$BASE_DIR/ops/host/render_virtiofs_xml.sh" "$JOB_ID"

echo "[3/3] Adjuntando recurso a la VM"
if ! bash "$BASE_DIR/ops/host/attach_virtiofs_to_vm.sh" "$JOB_ID" "$VM_NAME"; then
  echo
  echo "virtiofs no ha podido adjuntarse a la VM actual."
  echo "Puedes seguir con el fallback por scp mientras no redefinamos la VM con shared memory."
  exit 1
fi

echo
echo "Next step inside guest:"
echo "  sudo bash /tmp/mount_output_share.sh astinuslab_out /mnt/astinuslab-out"
