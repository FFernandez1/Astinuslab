#!/usr/bin/env bash
set -euo pipefail

SUBCOMMAND="${1:-}"
shift || true

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIBVIRT_URI="${LIBVIRT_URI:-${ASTINUSLAB_LIBVIRT_URI:-qemu:///system}}"
LIBVIRT_NETWORK="${LIBVIRT_NETWORK:-${ASTINUSLAB_LIBVIRT_NETWORK:-default}}"

case "$SUBCOMMAND" in
  noop)
    exit 0
    ;;
  ensure_dirs|install_base_image|finalize_job_artifacts|create_job_overlay|flatten_image|remove_job_dir|clear_storage_root)
    exec python3 -B "$SCRIPT_DIR/libvirt_storage_admin.py" "$SUBCOMMAND" "$@"
    ;;
  reset_default_network)
    if [[ ! "$LIBVIRT_NETWORK" =~ ^[a-zA-Z0-9._-]+$ ]]; then
      echo "DENY: invalid libvirt network name: $LIBVIRT_NETWORK" >&2
      exit 1
    fi
    rm -f "/var/lib/libvirt/dnsmasq/${LIBVIRT_NETWORK}.leases" "/var/lib/libvirt/dnsmasq/${LIBVIRT_NETWORK}.status"
    virsh -c "$LIBVIRT_URI" net-destroy "$LIBVIRT_NETWORK" >/dev/null 2>&1 || true
    virsh -c "$LIBVIRT_URI" net-start "$LIBVIRT_NETWORK" >/dev/null
    virsh -c "$LIBVIRT_URI" net-autostart "$LIBVIRT_NETWORK" >/dev/null 2>&1 || true
    ;;
  "")
    echo "Missing storage subcommand" >&2
    exit 1
    ;;
  *)
    echo "Unsupported subcommand: $SUBCOMMAND" >&2
    exit 1
    ;;
esac
