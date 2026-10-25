#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <guest_ip> <ssh_private_key>"
  exit 1
fi

GUEST_IP="$1"
SSH_KEY="$2"

ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "${GUEST_USER:-ubuntu}@$GUEST_IP" \
  "mountpoint -q /mnt/astinuslab-out && echo mounted || (mount | grep virtiofs || true; exit 1)"

