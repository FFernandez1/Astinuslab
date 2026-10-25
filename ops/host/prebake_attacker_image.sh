#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <source_image> <output_image> [attacker_user] [ssh_public_key]" >&2
  echo "Example: $0 images/definitions/kali-linux-2025.4-qemu-amd64.qcow2 images/definitions/kali-linux-2025.4-attacker-base.qcow2 kali storage/ssh_keys/1.pub" >&2
  exit 1
fi

SOURCE_IMAGE="$1"
OUTPUT_IMAGE="$2"
ATTACKER_USER="${3:-kali}"
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SSH_PUBKEY="${4:-$BASE_DIR/storage/ssh_keys/1.pub}"

require_bin() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

require_bin qemu-img
require_bin virt-customize

if [[ ! -f "$SOURCE_IMAGE" ]]; then
  echo "Source image not found: $SOURCE_IMAGE" >&2
  exit 1
fi

if [[ ! -f "$SSH_PUBKEY" ]]; then
  echo "SSH public key not found: $SSH_PUBKEY" >&2
  exit 1
fi

mkdir -p "$(dirname "$OUTPUT_IMAGE")"
cp --reflink=auto "$SOURCE_IMAGE" "$OUTPUT_IMAGE"

virt-customize -a "$OUTPUT_IMAGE" \
  --install openssh-server,qemu-guest-agent,sudo,network-manager \
  --run-command "systemctl enable ssh || systemctl enable ssh.service || true" \
  --run-command "systemctl enable NetworkManager || systemctl enable NetworkManager.service || true" \
  --run-command "systemctl enable --now qemu-guest-agent || systemctl enable qemu-guest-agent || systemctl enable qemu-guest-agent.service || true" \
  --run-command "nmcli connection delete astinuslab-dhcp >/dev/null 2>&1 || true" \
  --run-command "nmcli connection add type ethernet con-name astinuslab-dhcp ipv4.method auto ipv6.method disabled connection.autoconnect yes connection.autoconnect-priority 100 || true" \
  --run-command "rm -f /etc/ssh/ssh_host_*" \
  --run-command "id -u ${ATTACKER_USER} >/dev/null 2>&1 || useradd -m -s /bin/bash -G sudo ${ATTACKER_USER}" \
  --run-command "passwd -l ${ATTACKER_USER} || true" \
  --run-command "install -d -m 0700 -o ${ATTACKER_USER} -g ${ATTACKER_USER} /home/${ATTACKER_USER}/.ssh" \
  --copy-in "$SSH_PUBKEY:/tmp" \
  --run-command "install -m 0600 -o ${ATTACKER_USER} -g ${ATTACKER_USER} /tmp/$(basename "$SSH_PUBKEY") /home/${ATTACKER_USER}/.ssh/authorized_keys" \
  --run-command "rm -f /tmp/$(basename "$SSH_PUBKEY")" \
  --run-command "sed -i 's/^#\\?PasswordAuthentication .*/PasswordAuthentication no/' /etc/ssh/sshd_config || true" \
  --run-command "sed -i 's/^#\\?PubkeyAuthentication .*/PubkeyAuthentication yes/' /etc/ssh/sshd_config || true" \
  --run-command "sed -i 's/^#\\?PermitRootLogin .*/PermitRootLogin no/' /etc/ssh/sshd_config || true"

echo "Prebaked attacker image created:"
echo "  source: $SOURCE_IMAGE"
echo "  output: $OUTPUT_IMAGE"
echo "  user:   $ATTACKER_USER"
echo "  key:    $SSH_PUBKEY"
echo
echo "Next steps:"
echo "  1. Update config/catalog/attackers.yaml to point to $(basename "$OUTPUT_IMAGE")"
echo "  2. Recreate the attacker VM/job so the new base image is used"
