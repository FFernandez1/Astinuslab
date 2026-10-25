#!/usr/bin/env bash
# Poll a local Windows image-build VM until DHCP and SSH are ready.
#
# This helper is portable source; per-run output still belongs under ignored
# runtime scratch such as build/windows/ or storage/tmp/windows-build/<id>/.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="${REPO_ROOT:-$(cd "$script_dir/../../.." && pwd)}"
vm_name="${WINBUILD_VM_NAME:-astinuslab-winbuild}"
mac_file="${WINBUILD_MAC_FILE:-/tmp/winbuild.mac}"
ssh_key="${WINBUILD_SSH_KEY:-$repo_root/storage/ssh_keys/key}"
libvirt_uri="${LIBVIRT_URI:-qemu:///system}"
network_name="${WINBUILD_NETWORK:-default}"
timeout_seconds="${WINBUILD_TIMEOUT:-3000}"
interval_seconds="${WINBUILD_INTERVAL:-60}"
screenshot_path="${WINBUILD_SCREENSHOT:-/tmp/astinuslab-winbuild.png}"
ssh_user="${WINBUILD_SSH_USER:-Administrator}"

usage() {
  cat <<'EOF'
Usage: poll_build.sh [options]

Poll a Windows build VM, capture screenshots, discover its DHCP lease, and test
SSH readiness with the configured Administrator key.

Options:
  --repo-root PATH       Repository root. Default: REPO_ROOT or auto-detected.
  --vm-name NAME         Libvirt domain name. Default: astinuslab-winbuild.
  --mac-file PATH        File containing VM MAC address. Default: /tmp/winbuild.mac.
  --ssh-key PATH         SSH private key. Default: <repo-root>/storage/ssh_keys/key.
  --uri URI              Libvirt URI. Default: qemu:///system.
  --network NAME         Libvirt network for DHCP lookup. Default: default.
  --timeout SECONDS      Maximum poll time. Default: 3000.
  --interval SECONDS     Delay between polls. Default: 60.
  --screenshot PATH      Screenshot output path. Default: /tmp/astinuslab-winbuild.png.
  --user USER            SSH user. Default: Administrator.
  -h, --help             Show this help.

Environment variables with matching names are also supported:
REPO_ROOT, WINBUILD_VM_NAME, WINBUILD_MAC_FILE, WINBUILD_SSH_KEY, LIBVIRT_URI,
WINBUILD_NETWORK, WINBUILD_TIMEOUT, WINBUILD_INTERVAL, WINBUILD_SCREENSHOT,
WINBUILD_SSH_USER.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-root)
      repo_root="$2"
      shift 2
      ;;
    --vm-name)
      vm_name="$2"
      shift 2
      ;;
    --mac-file)
      mac_file="$2"
      shift 2
      ;;
    --ssh-key)
      ssh_key="$2"
      shift 2
      ;;
    --uri)
      libvirt_uri="$2"
      shift 2
      ;;
    --network)
      network_name="$2"
      shift 2
      ;;
    --timeout)
      timeout_seconds="$2"
      shift 2
      ;;
    --interval)
      interval_seconds="$2"
      shift 2
      ;;
    --screenshot)
      screenshot_path="$2"
      shift 2
      ;;
    --user)
      ssh_user="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! -r "$mac_file" ]]; then
  echo "MAC file not readable: $mac_file" >&2
  exit 2
fi

mac_address="$(tr '[:upper:]' '[:lower:]' < "$mac_file" | tr -d '[:space:]')"
if [[ -z "$mac_address" ]]; then
  echo "MAC file is empty: $mac_file" >&2
  exit 2
fi

if [[ ! "$timeout_seconds" =~ ^[0-9]+$ ]] || [[ ! "$interval_seconds" =~ ^[0-9]+$ ]]; then
  echo "Timeout and interval must be non-negative integers" >&2
  exit 2
fi

ssh_options=(
  -i "$ssh_key"
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  -o BatchMode=yes
  -o ConnectTimeout=8
)

deadline=$(( $(date +%s) + timeout_seconds ))
iteration=0

while [[ "$(date +%s)" -lt "$deadline" ]]; do
  iteration=$((iteration + 1))

  virsh -c "$libvirt_uri" screenshot "$vm_name" "$screenshot_path" >/dev/null 2>&1 || true

  leases="$(virsh -c "$libvirt_uri" net-dhcp-leases "$network_name" 2>/dev/null || true)"
  ip_address="$(printf '%s\n' "$leases" | awk -v m="$mac_address" 'tolower($0) ~ m {split($5, a, "/"); print a[1]; exit}')"

  if [[ -n "$ip_address" ]]; then
    echo "[$iteration] lease: $ip_address"
    if ssh "${ssh_options[@]}" "$ssh_user@$ip_address" "exit 0" 2>/dev/null; then
      echo "SSH_OK $ip_address"
      exit 0
    fi
  else
    echo "[$iteration] no lease yet"
  fi

  sleep "$interval_seconds"
done

echo "TIMEOUT"
exit 1
