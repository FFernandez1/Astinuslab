#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <guest_ip> <ssh_private_key>"
  exit 1
fi

GUEST_IP="$1"
SSH_KEY="$2"
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REMOTE_SCRIPT="$BASE_DIR/guest/scripts/detect_environment.sh"

if [[ ! -f "$SSH_KEY" ]]; then
  echo "Private key does not exist: $SSH_KEY"
  exit 1
fi

ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "${GUEST_USER:-ubuntu}@$GUEST_IP" 'bash -s -- json' < "$REMOTE_SCRIPT"

