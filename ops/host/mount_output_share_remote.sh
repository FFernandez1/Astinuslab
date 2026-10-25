#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <guest_ip> <ssh_private_key>"
  exit 1
fi

GUEST_IP="$1"
SSH_KEY="$2"
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REMOTE_SCRIPT="$BASE_DIR/guest/scripts/mount_output_share.sh"

VIRTIOFS_MOUNT_TAG="${VIRTIOFS_MOUNT_TAG:-astinuslab_out}"
GUEST_MOUNT_PATH="${GUEST_MOUNT_PATH:-/mnt/astinuslab-out}"

ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "${GUEST_USER:-ubuntu}@$GUEST_IP" "sudo bash -s -- '$VIRTIOFS_MOUNT_TAG' '$GUEST_MOUNT_PATH'" < "$REMOTE_SCRIPT"
