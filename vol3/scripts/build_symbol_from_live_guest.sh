#!/usr/bin/env bash
# vol3/scripts/build_symbol_from_live_guest.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

analysis_log() {
  echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] $*" >&2
}
analysis_die() {
  analysis_log "ERROR: $*"
  exit 1
}

GUEST_IP=""
SSH_KEY=""
WORK_DIR="${BASE_DIR}/vol3/_work"
SYMBOLS_DIR="${BASE_DIR}/vol3/symbols"
GUEST_USER="${GUEST_USER:-ubuntu}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --guest-ip)     GUEST_IP="$2";     shift 2 ;;
        --ssh-key)      SSH_KEY="$2";      shift 2 ;;
        --work-dir)     WORK_DIR="$2";     shift 2 ;;
        --symbols-dir)  SYMBOLS_DIR="$2";  shift 2 ;;
        --guest-user)   GUEST_USER="$2";   shift 2 ;;
        *) analysis_die "Unknown argument: $1" ;;
    esac
done

[[ -n "$GUEST_IP" ]] || analysis_die "--guest-ip is required"
[[ -n "$SSH_KEY"  ]] || analysis_die "--ssh-key is required"
[[ -f "$SSH_KEY"  ]] || analysis_die "SSH key not found: $SSH_KEY"
: "${DWARF2JSON_LOCAL:?DWARF2JSON_LOCAL is required}"
: "${DWARF2JSON_SHA256:?DWARF2JSON_SHA256 is required}"

mkdir -p "$WORK_DIR"
ISF_OUTPUT_DIR="${WORK_DIR}/symbols/linux"
mkdir -p "$ISF_OUTPUT_DIR"

analysis_log "Starting build_symbol_from_live_guest"
analysis_log "Guest IP: ${GUEST_IP}"

REMOTE_SCRIPT="${BASE_DIR}/ops/host/build_vol3_symbol_remote.sh"
[[ -f "$REMOTE_SCRIPT" ]] || analysis_die "Remote script not found: $REMOTE_SCRIPT"

REMOTE_TMP_DIR="/tmp/astinuslab-vol3-$$"

# VMLINUX_OVERRIDE propagates to the remote script via process environment inheritance.
REMOTE_OUTPUT="$(
    GUEST_USER="$GUEST_USER" \
    VOL3_ALLOW_GUEST_PACKAGE_INSTALL="${VOL3_ALLOW_GUEST_PACKAGE_INSTALL:-0}" \
    DWARF2JSON_LOCAL="$DWARF2JSON_LOCAL" \
    DWARF2JSON_SHA256="$DWARF2JSON_SHA256" \
    "$REMOTE_SCRIPT" "$GUEST_IP" "$SSH_KEY" "$REMOTE_TMP_DIR" "$ISF_OUTPUT_DIR"
)"

ISF_LOCAL_PATH="$(printf '%s\n' "$REMOTE_OUTPUT" | grep '^vol3_symbol_path=' | tail -n 1 | cut -d= -f2- || true)"
KERNEL_RELEASE="$(printf '%s\n' "$REMOTE_OUTPUT"  | grep '^kernel_release='   | tail -n 1 | cut -d= -f2- || true)"
BUILD_ID="$(printf '%s\n' "$REMOTE_OUTPUT"  | grep '^build_id='        | tail -n 1 | cut -d= -f2- || true)"

[[ -n "$ISF_LOCAL_PATH" ]] || analysis_die "Script did not emit vol3_symbol_path"
[[ -n "$KERNEL_RELEASE" ]] || analysis_die "Script did not emit kernel_release"
[[ -f "$ISF_LOCAL_PATH" ]] || analysis_die "ISF not found at reported path: ${ISF_LOCAL_PATH}"

SYMBOLS_LINUX_DIR="${SYMBOLS_DIR}/linux"
mkdir -p "$SYMBOLS_LINUX_DIR"

ISF_FILENAME="$(basename "$ISF_LOCAL_PATH")"
SYMBOLS_DEST="${SYMBOLS_LINUX_DIR}/${ISF_FILENAME}"

cp "$ISF_LOCAL_PATH" "$SYMBOLS_DEST"
analysis_log "ISF copied to symbols dir: ${SYMBOLS_DEST}"

echo "isf_path=${SYMBOLS_DEST}"
echo "kernel_release=${KERNEL_RELEASE}"
if [[ -n "$BUILD_ID" ]]; then
    echo "build_id=${BUILD_ID}"
fi
analysis_log "build_symbol_from_live_guest complete."
