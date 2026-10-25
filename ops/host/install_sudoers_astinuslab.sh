#!/usr/bin/env bash
# Install the NOPASSWD sudoers rule for AstinusLab privileged helpers.
# Usage: sudo bash ops/host/install_sudoers_astinuslab.sh [--install-dir PATH] [--run-user USER] [--helper-source-dir PATH]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${INSTALL_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
HELPER_SOURCE_DIR="$SCRIPT_DIR"
HELPER_INSTALL_DIR="${ASTINUSLAB_HELPER_INSTALL_DIR:-/usr/local/libexec/astinuslab}"
HELPER_STORAGE="$HELPER_INSTALL_DIR/libvirt_storage_admin.sh"
HELPER_ACL="$HELPER_INSTALL_DIR/fix_libvirt_path_acl.sh"
HELPER_NETISO="$HELPER_INSTALL_DIR/network_isolation_admin.sh"
SUDOERS_FILE="/etc/sudoers.d/astinuslab-runner"
POLICY_FILE="/etc/astinuslab/acl-policy.json"
RUN_USER="${SUDO_USER:-${USER:-}}"
ADDITIONAL_RUN_USERS=()
STORAGE_ROOT=""
STORAGE_GROUP=""
EVIDENCE_READ_GROUPS=()
HELPER_SET=(
  libvirt_storage_admin.sh
  libvirt_storage_admin.py
  fix_libvirt_path_acl.sh
  fix_libvirt_path_acl.py
  acl_policy.py
  network_isolation_admin.sh
)

usage() {
  cat <<'EOF'
Usage: install_sudoers_astinuslab.sh [options]

Options:
  --install-dir PATH         AstinusLab install/checkout path used in rendered helpers
  --run-user USER            User allowed to run privileged helpers
  --additional-run-user USER
                             Transitional caller retained during a verified
                             split-user migration (may be repeated)
  --helper-source-dir PATH   Directory containing helper scripts
  --storage-root PATH        Root-administered ACL policy root
  --storage-group GROUP      Group whose write bit is tolerated on that root
                             (default: the storage root's own group when it
                             is group-writable, otherwise none)
  --evidence-read-group GROUP
                             Group granted read access to evidence artifacts
                             (may be repeated)
  -h, --help                 Show this help
EOF
}

validate_storage_root_authority() {
  PYTHONPATH="$HELPER_SOURCE_DIR${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -B - "$STORAGE_ROOT" "$INSTALL_DIR" <<'PY'
import sys

from acl_policy import validate_storage_root_for_install

try:
    validate_storage_root_for_install(sys.argv[1], sys.argv[2])
except ValueError as exc:
    raise SystemExit(str(exc))
PY
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --install-dir) [[ $# -ge 2 ]] || { echo "--install-dir requires PATH" >&2; exit 1; }; INSTALL_DIR="$2"; shift 2 ;;
      --run-user) [[ $# -ge 2 ]] || { echo "--run-user requires USER" >&2; exit 1; }; RUN_USER="$2"; shift 2 ;;
      --additional-run-user) [[ $# -ge 2 ]] || { echo "--additional-run-user requires USER" >&2; exit 1; }; ADDITIONAL_RUN_USERS+=("$2"); shift 2 ;;
      --helper-source-dir) [[ $# -ge 2 ]] || { echo "--helper-source-dir requires PATH" >&2; exit 1; }; HELPER_SOURCE_DIR="$2"; shift 2 ;;
      --storage-root) [[ $# -ge 2 ]] || { echo "--storage-root requires PATH" >&2; exit 1; }; STORAGE_ROOT="$2"; shift 2 ;;
      --storage-group) [[ $# -ge 2 ]] || { echo "--storage-group requires GROUP" >&2; exit 1; }; STORAGE_GROUP="$2"; shift 2 ;;
      --evidence-read-group) [[ $# -ge 2 ]] || { echo "--evidence-read-group requires GROUP" >&2; exit 1; }; EVIDENCE_READ_GROUPS+=("$2"); shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
  done
  [[ "$INSTALL_DIR" == /* ]] || { echo "--install-dir must be absolute" >&2; exit 1; }
  [[ -n "$RUN_USER" ]] || { echo "--run-user cannot be empty" >&2; exit 1; }
  [[ "$RUN_USER" =~ ^[A-Za-z_][A-Za-z0-9_.-]{0,31}$ ]] || { echo "--run-user contains unsupported characters" >&2; exit 1; }
  local additional_user
  for additional_user in "${ADDITIONAL_RUN_USERS[@]}"; do
    [[ "$additional_user" =~ ^[A-Za-z_][A-Za-z0-9_.-]{0,31}$ ]] || {
      echo "--additional-run-user contains unsupported characters" >&2
      exit 1
    }
  done
  [[ -n "$STORAGE_ROOT" ]] || STORAGE_ROOT="$INSTALL_DIR/storage"
  [[ "$STORAGE_ROOT" == /* && "$STORAGE_ROOT" != "/" ]] || { echo "--storage-root must be an absolute non-root path" >&2; exit 1; }
  [[ -d "$STORAGE_ROOT" && ! -L "$STORAGE_ROOT" ]] || { echo "--storage-root must be an existing non-symlink directory" >&2; exit 1; }
  validate_storage_root_authority
  # Default the tolerated group to the storage root's own group, and only when
  # that root is actually group-writable: the supported installed layout is 2770,
  # while a checkout at 0710 needs no group-write tolerance at all.
  if [[ -z "$STORAGE_GROUP" ]] && (( 0$(stat -c '%a' "$STORAGE_ROOT") & 0020 )); then
    STORAGE_GROUP="$(stat -c '%G' "$STORAGE_ROOT")"
  fi
  if [[ -n "$STORAGE_GROUP" ]]; then
    getent group "$STORAGE_GROUP" >/dev/null || { echo "--storage-group must be an existing group" >&2; exit 1; }
  fi
  local evidence_read_group
  for evidence_read_group in "${EVIDENCE_READ_GROUPS[@]}"; do
    [[ "$evidence_read_group" =~ ^[A-Za-z_][A-Za-z0-9_.-]{0,31}$ ]] || {
      echo "--evidence-read-group contains unsupported characters" >&2
      exit 1
    }
    getent group "$evidence_read_group" >/dev/null || {
      echo "--evidence-read-group must be an existing group" >&2
      exit 1
    }
  done
}

require_root() {
  [[ $EUID -eq 0 ]] || { echo "Run as root: sudo bash $0" >&2; exit 1; }
}

render_helper_template() {
  local src="$1" dst="$2" install_dir="$3"
  local install_dev="${4:-0}" install_ino="${5:-0}"
  local libvirt_image_root="${6:-/var/lib/libvirt/images/astinuslab}"
  python3 - "$src" "$dst" "$install_dir" "$install_dev" "$install_ino" "$libvirt_image_root" <<'PY'
from pathlib import Path
import sys
src, dst, install_dir, install_dev, install_ino, libvirt_image_root = sys.argv[1:]
text = Path(src).read_text(encoding="utf-8")
text = text.replace("@@INSTALL_DIR@@", install_dir)
text = text.replace("@@INSTALL_DEV@@", install_dev)
text = text.replace("@@INSTALL_INO@@", install_ino)
text = text.replace("@@LIBVIRT_IMAGE_ROOT@@", libvirt_image_root)
text = text.replace("/opt/astinuslab", install_dir)
Path(dst).write_text(text, encoding="utf-8")
PY
}

prepare_helper_install_dir() {
  python3 - "$HELPER_INSTALL_DIR" <<'PY'
import os
import stat
import sys

path = os.path.normpath(sys.argv[1])
if not os.path.isabs(path) or path == "/":
    raise SystemExit("privileged helper root must be an absolute non-root path")
flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
descriptor = os.open("/", flags)
try:
    for component in path.split("/")[1:]:
        try:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
        except FileNotFoundError:
            os.mkdir(component, 0o755, dir_fd=descriptor)
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            # mkdir honours a caller's private umask. Runtime identities must
            # traverse new ancestors too; preserve every pre-existing parent.
            os.fchmod(next_descriptor, 0o755)
        info = os.fstat(next_descriptor)
        if not stat.S_ISDIR(info.st_mode):
            os.close(next_descriptor)
            raise SystemExit("privileged helper path contains a non-directory component")
        if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
            os.close(next_descriptor)
            raise SystemExit("privileged helper path has a writable non-sticky authority directory")
        os.close(descriptor)
        descriptor = next_descriptor
    leaf = os.fstat(descriptor)
    if leaf.st_uid != os.geteuid() or leaf.st_mode & 0o022:
        raise SystemExit("privileged helper root must be installer-owned and non-writable by group/other")
    os.fchmod(descriptor, 0o755)
finally:
    os.close(descriptor)
PY
}

install_helpers() {
  [[ -d "$INSTALL_DIR" && ! -L "$INSTALL_DIR" ]] || {
    echo "Install root must exist and must not be a symlink: $INSTALL_DIR" >&2
    return 1
  }
  INSTALL_DIR="$(realpath -e -- "$INSTALL_DIR")"
  prepare_helper_install_dir || return 1

  local helper_name source_helper target_helper
  local existing_name known
  for existing_name in "$HELPER_INSTALL_DIR"/*; do
    [[ -e "$existing_name" || -L "$existing_name" ]] || continue
    known=false
    for helper_name in "${HELPER_SET[@]}"; do
      [[ "$(basename "$existing_name")" == "$helper_name" ]] && known=true
    done
    # -r: an orphan can be a directory (a stray __pycache__ from an older
    # helper that imported a module). rm -f on a directory exits 1, which
    # under set -e aborts the installer and leaves the host un-upgradable.
    $known || rm -rf -- "$existing_name"
  done
  local install_dev install_ino
  read -r install_dev install_ino < <(stat -c '%d %i' -- "$INSTALL_DIR")
  for helper_name in "${HELPER_SET[@]}"; do
    source_helper="$HELPER_SOURCE_DIR/$helper_name"
    target_helper="$HELPER_INSTALL_DIR/$helper_name"
    if [[ ! -f "$source_helper" ]]; then
      echo "Helper not found: $source_helper" >&2
      return 1
    fi
    # A known target may itself be a stale symlink or directory. Remove that
    # directory entry under the already-bound protected helper root before the
    # renderer opens the destination by name.
    rm -rf -- "$target_helper" || return 1
    render_helper_template "$source_helper" "$target_helper" "$INSTALL_DIR" "$install_dev" "$install_ino" "/var/lib/libvirt/images/astinuslab"
    chown root:root "$target_helper"
    chmod 0755 "$target_helper"
  done
}

validate_helpers() {
  local helper owner_group mode helper_name
  for helper_name in "${HELPER_SET[@]}"; do
    helper="$HELPER_INSTALL_DIR/$helper_name"
    owner_group="$(stat -c '%U:%G' "$helper")"
    mode="$(stat -c '%a' "$helper")"
    if [[ "$owner_group" != "root:root" || "$mode" != "755" ]]; then
      echo "Refusing sudoers install: $helper is $owner_group mode $mode, expected root:root mode 755" >&2
      exit 1
    fi
  done
}

install_acl_policy() {
  validate_storage_root_authority
  install -d -o root -g root -m 0755 "$(dirname "$POLICY_FILE")"
  python3 - "$POLICY_FILE" "$STORAGE_ROOT" "$STORAGE_GROUP" "$RUN_USER" "${ADDITIONAL_RUN_USERS[@]}" -- "${EVIDENCE_READ_GROUPS[@]}" <<'PY'
import json
import os
import sys
from pathlib import Path

destination, root, group, *values = sys.argv[1:]
separator = values.index("--")
callers = values[:separator]
evidence_read_groups = values[separator + 1:]
if not os.path.isabs(root) or os.path.normpath(root) == "/":
    raise SystemExit("--storage-root must be an absolute non-root path")
payload = {
    "schema_version": "astinuslab-acl-policy/v1",
    "allowed_callers": callers,
    "allowed_roots": [os.path.normpath(root)],
    # The installed layout sets storage to 2770 so the service group can create
    # SQLite WAL/SHM siblings; naming the group here is what lets the ACL helper
    # accept that mode without tolerating group write from any other group.
    "storage_groups": [group] if group else [],
    "evidence_read_groups": evidence_read_groups,
}
Path(destination).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY
  chown root:root "$POLICY_FILE"
  chmod 0644 "$POLICY_FILE"
}

write_sudoers_rule() {
  cat > "$SUDOERS_FILE" <<EOF
# AstinusLab host runner - allow administrative helpers without password
EOF
  local caller
  for caller in "$RUN_USER" "${ADDITIONAL_RUN_USERS[@]}"; do
    printf '%s ALL=(root) NOPASSWD: %s, %s, %s\n' \
      "$caller" "$HELPER_STORAGE" "$HELPER_ACL" "$HELPER_NETISO" >> "$SUDOERS_FILE"
  done
  chmod 0440 "$SUDOERS_FILE"
  visudo -c -f "$SUDOERS_FILE" || { rm -f "$SUDOERS_FILE"; echo "sudoers syntax error - file removed" >&2; exit 1; }
}

main() {
  parse_args "$@"
  require_root
  install_helpers
  validate_helpers
  install_acl_policy
  write_sudoers_rule
  echo "Rule installed: $SUDOERS_FILE"
  cat "$SUDOERS_FILE"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
