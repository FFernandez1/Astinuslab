#!/usr/bin/env bash
# bootstrap_cloud_image_libvirt_storage.sh
# Prepares a job-specific overlay image and cloud-init seed ISO in libvirt storage.
set -euo pipefail

if [[ $# -lt 5 ]]; then
  echo "Usage: $0 <job_id> <cloud_image_path> <ssh_public_key_file> <target_id> <os_variant> [disk_size_gb]"
  exit 1
fi

JOB_ID="$1"
CLOUD_IMAGE_PATH="$2"
SSH_PUBKEY_FILE="$3"
TARGET_ID="$4"
OS_VARIANT="$5"
DISK_SIZE="${6:-40}G"

if [[ -z "$OS_VARIANT" ]]; then
  echo "os_variant must not be empty" >&2
  exit 1
fi

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STORAGE_ROOT="/var/lib/libvirt/images/astinuslab"
BASE_IMAGE_DIR="${STORAGE_ROOT}/base"
JOB_DIR="${STORAGE_ROOT}/${JOB_ID}"
HELPER="/usr/local/libexec/astinuslab/libvirt_storage_admin.sh"

# 1. Ensure directories exist
sudo -n "$HELPER" ensure_dirs "$BASE_IMAGE_DIR" "$JOB_DIR"

# 2. Install base image if missing
BASE_IMAGE_NAME="$(basename "$CLOUD_IMAGE_PATH")"
BACKING_IMAGE="$BASE_IMAGE_DIR/$BASE_IMAGE_NAME"
IMAGES_DIR_DEFAULT="$BASE_DIR/images/definitions"
sudo -n "$HELPER" install_base_image "${ASTINUSLAB_IMAGES_DIR:-$IMAGES_DIR_DEFAULT}" "$CLOUD_IMAGE_PATH" "$BACKING_IMAGE"

# 3. Create job overlay with specified size
JOB_IMAGE="$JOB_DIR/${TARGET_ID}-job.qcow2"
sudo -n "$HELPER" create_job_overlay "$BACKING_IMAGE" "$JOB_IMAGE" "${DISK_SIZE%G}"

# 4. Generate cloud-init seed ISO
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

# Honor the caller's catalog user; infer one only for legacy direct callers.
if [[ -z "${GUEST_USER:-}" ]]; then
  GUEST_USER="ubuntu"
  [[ "$TARGET_ID" == *"kali"* ]] && GUEST_USER="kali"
  [[ "$TARGET_ID" == *"attacker"* ]] && GUEST_USER="kali"
fi

cat > "$TMP_DIR/user-data" <<EOF
#cloud-config
users:
  - default
  - name: $GUEST_USER
    sudo: ALL=(ALL) NOPASSWD:ALL
    groups: users, adm, sudo
    shell: /bin/bash
    lock_passwd: true
    ssh_authorized_keys:
      - $(cat "$SSH_PUBKEY_FILE")

ssh_pwauth: false
disable_root: true

growpart:
  mode: auto
  devices: ["/"]
  ignore_growroot_disabled: false

resize_rootfs: true

package_update: true
packages:
  - qemu-guest-agent

runcmd:
  - systemctl start qemu-guest-agent || /usr/sbin/qemu-ga -d
EOF

cat > "$TMP_DIR/meta-data" <<EOF
instance-id: $JOB_ID
local-hostname: astinuslab-$JOB_ID
EOF

SEED_ISO="$JOB_DIR/seed.iso"
cloud-localds "$SEED_ISO" "$TMP_DIR/user-data" "$TMP_DIR/meta-data"

# 5. Finalize permissions
sudo -n "$HELPER" finalize_job_artifacts "$JOB_IMAGE" "$SEED_ISO"

echo "Bootstrap complete: $JOB_DIR"
