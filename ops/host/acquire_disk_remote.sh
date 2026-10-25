#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 6 ]]; then
  echo "Usage: $0 <job_id> <guest_ip> <ssh_private_key> <distro> <version> <kernel_release>" >&2
  exit 1
fi

JOB_ID="$1"
GUEST_IP="$2"
SSH_KEY="$3"
DISTRO="$4"
VERSION="$5"
KERNEL_RELEASE="$6"

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STORAGE_ROOT="${ASTINUSLAB_STORAGE_DIR:-$BASE_DIR/storage}"
REMOTE_SCRIPT="$BASE_DIR/guest/scripts/acquire_disk.sh"
REMOTE_STAGING_DIR="${GUEST_MOUNT_PATH:-/mnt/astinuslab-out}"
REMOTE_FALLBACK_DIR="/tmp/astinuslab-out"
REMOTE_FILENAME="disk.img.gz.partial"
REMOTE_DEST="$REMOTE_STAGING_DIR/$REMOTE_FILENAME"
LOCAL_FETCH="$STORAGE_ROOT/tmp/$JOB_ID/$REMOTE_FILENAME"
HOST_MOUNTED_STAGING="$STORAGE_ROOT/tmp/$JOB_ID/virtiofs-staging/$REMOTE_FILENAME"

if ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    "${GUEST_USER:-ubuntu}@$GUEST_IP" "mountpoint -q '$REMOTE_STAGING_DIR'" 2>/dev/null; then
  REMOTE_DEST="$REMOTE_STAGING_DIR/$REMOTE_FILENAME"
else
  REMOTE_DEST="$REMOTE_FALLBACK_DIR/$REMOTE_FILENAME"
  ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      "${GUEST_USER:-ubuntu}@$GUEST_IP" "mkdir -p '$REMOTE_FALLBACK_DIR'"
fi

ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    "${GUEST_USER:-ubuntu}@$GUEST_IP" "sudo bash -s -- '$REMOTE_DEST'" < "$REMOTE_SCRIPT"

mkdir -p "$(dirname "$LOCAL_FETCH")"

if [[ "$REMOTE_DEST" == "$REMOTE_STAGING_DIR/$REMOTE_FILENAME" ]]; then
  if [[ -f "$HOST_MOUNTED_STAGING" ]]; then
    cp "$HOST_MOUNTED_STAGING" "$LOCAL_FETCH"
  else
    scp -i "$SSH_KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        "${GUEST_USER:-ubuntu}@$GUEST_IP:$REMOTE_DEST" "$LOCAL_FETCH"
  fi
else
  scp -i "$SSH_KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      "${GUEST_USER:-ubuntu}@$GUEST_IP:$REMOTE_DEST" "$LOCAL_FETCH"
fi

bash "$BASE_DIR/ops/host/publish_disk_to_host.sh" \
    "$JOB_ID" "$LOCAL_FETCH" "$DISTRO" "$VERSION" "$KERNEL_RELEASE"
