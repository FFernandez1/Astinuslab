#!/usr/bin/env bash
# AstinusLab uninstall script.
# Reverses the actions of ops/deploy/install.sh.
# Must be run as root (or via sudo) from any directory.
set -euo pipefail

info()  { echo -e "\033[1;34m[INFO]\033[0m  $*"; }
ok()    { echo -e "\033[1;32m[ OK ]\033[0m  $*"; }
warn()  { echo -e "\033[1;33m[WARN]\033[0m  $*"; }
die()   { echo -e "\033[1;31m[FAIL]\033[0m  $*" >&2; exit 1; }

INSTALL_DIR="${INSTALL_DIR:-/opt/astinuslab}"
SYSTEMD_UNIT_DIR="${ASTINUSLAB_SYSTEMD_DIR:-/etc/systemd/system}"

# Byte-for-byte duplicate of ops/deploy/install.sh:validate_install_dir(). This
# script runs as root and `rm -rf`s $INSTALL_DIR, so it must never accept a
# directory the installer would have refused to create. The logic is copied
# rather than sourced from a shared snippet because both scripts are run
# directly by operators and must stay standalone; the two copies are held in
# sync by tests/python/tooling/test_install_script.py, which compares the two function
# bodies and fails the build the moment they differ.
validate_install_dir() {
  [[ "$INSTALL_DIR" == /* ]] || die "INSTALL_DIR must be an absolute path"
  [[ "$INSTALL_DIR" =~ ^/[A-Za-z0-9._/@+/-]+$ ]] \
    || die "INSTALL_DIR contains unsupported characters for systemd/AppArmor policy"
  [[ "$INSTALL_DIR" != "/" ]] || die "INSTALL_DIR cannot be the root directory (/)"
  case "$INSTALL_DIR" in
    /etc|/usr|/var|/home|/bin|/sbin|/lib|/sys|/dev|/proc|/root|/tmp|/opt)
      die "INSTALL_DIR cannot be a top-level system directory ($INSTALL_DIR)"
      ;;
  esac
}
validate_install_dir

# This script is run from its own directory in the release tree
# (`sudo bash ops/deploy/uninstall.sh`), the same way ops/deploy/install.sh is,
# and reads the same two support modules the installer writes its manifests
# with. Prove both are present before anything is stopped or removed: a missing
# module found halfway through would leave the installation half reversed.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
require_support_modules() {
  local module
  for module in host_access.py privileged_state.py; do
    [[ -f "$SCRIPT_DIR/$module" ]] \
      || die "Missing $SCRIPT_DIR/$module. Run this script from a complete release tree."
  done
}
require_support_modules

[[ $EUID -eq 0 ]] || die "Run this script as root: sudo bash ops/deploy/uninstall.sh"

validate_install_dir_authority() {
  python3 "$SCRIPT_DIR/privileged_state.py" install-dir-authority "$INSTALL_DIR"
}
validate_install_dir_authority || die "Unsafe install-root parent authority"

# Determine the service user from the running install (default same as install.sh)
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-astinuslab}}"
SPLIT_USERS=false
RUNTIME_IDENTITIES_FILE="/etc/astinuslab/runtime-identities"
split_identity_marker_is_valid() {
  python3 "$SCRIPT_DIR/privileged_state.py" split-identity-marker "$RUNTIME_IDENTITIES_FILE"
}
explicit_split_units_present() {
  local unit
  for unit in astinuslab-api.service astinuslab-runner.service astinuslab-analysis.service; do
    if [[ -e "$SYSTEMD_UNIT_DIR/$unit" || -L "$SYSTEMD_UNIT_DIR/$unit" ]] \
      || systemctl cat "$unit" >/dev/null 2>&1 \
      || systemctl is-active --quiet "$unit" 2>/dev/null \
      || systemctl is-enabled --quiet "$unit" 2>/dev/null; then
      return 0
    fi
  done
  return 1
}
if [[ -e "$RUNTIME_IDENTITIES_FILE" || -L "$RUNTIME_IDENTITIES_FILE" ]]; then
  split_identity_marker_is_valid \
    || die "incomplete or malformed split-user state; refusing uninstall"
  SPLIT_USERS=true
elif explicit_split_units_present; then
  die "incomplete or malformed split-user state; explicit split units exist without a valid marker"
fi
SERVICE_UNITS=(
  astinuslab-api.service
  astinuslab-runner.service
  astinuslab-analysis.service
  "astinuslab-api@${SERVICE_USER}.service"
  "astinuslab-runner@${SERVICE_USER}.service"
  "astinuslab-analysis@${SERVICE_USER}.service"
)

foreign_template_instances() {
  local loaded unit_files
  loaded="$(systemctl list-units --all --no-legend --plain \
    "astinuslab-api@*.service" "astinuslab-runner@*.service" "astinuslab-analysis@*.service" \
    2>/dev/null)" || return 1
  # Like install.sh, query all unit files: systemd 255 returns 1 when patterns
  # match nothing, including a split-only install with no template units.
  # Filter locally so real enumeration failures still refuse removal.
  unit_files="$(systemctl list-unit-files --no-legend 2>/dev/null)" || return 1
  printf '%s\n%s\n' "$loaded" "$unit_files" | awk -v api="astinuslab-api@${SERVICE_USER}.service" \
    -v runner="astinuslab-runner@${SERVICE_USER}.service" \
    -v analysis="astinuslab-analysis@${SERVICE_USER}.service" \
    '{ unit = ($1 == "●" ? $2 : $1) }
     unit !~ /^astinuslab-(api|runner|analysis)@.+\.service$/ {next}
     unit != api && unit != runner && unit != analysis {print unit}' | sort -u
}

# A template may have been instantiated or merely enabled for some other
# identity. That unit is not owned by this invocation, so deleting its shared
# template/source would strand it. Refuse until the operator names the correct
# SERVICE_USER or disables the foreign instance explicitly.
other_instances="$(foreign_template_instances)" \
  || die "Could not enumerate AstinusLab template instances"
if [[ -n "$other_instances" ]]; then
  warn "Other AstinusLab template instances are active:"
  echo "$other_instances"
  die "Refusing to remove shared units or source while an unowned instance is active"
fi

# Warn if no active/enabled services found for this user
managed_service_found=false
for unit in "${SERVICE_UNITS[@]}"; do
  if systemctl is-active --quiet "$unit" 2>/dev/null \
    || systemctl is-enabled --quiet "$unit" 2>/dev/null; then
    managed_service_found=true
    break
  fi
done
if ! $managed_service_found; then
  warn "No active/enabled services found for user '$SERVICE_USER'."
  warn "If installed under a different user, re-run with: SERVICE_USER=<name> sudo bash ops/deploy/uninstall.sh"
fi

# ── Reverse AppArmor/SELinux rules added by install.sh ────────────────────────
MAC_POLICY_MANIFEST="${ASTINUSLAB_MAC_POLICY_MANIFEST:-/etc/astinuslab/mac-policy.manifest}"

revoke_recorded_mac_changes() {
  [[ -e "$MAC_POLICY_MANIFEST" || -L "$MAC_POLICY_MANIFEST" ]] || return 0
  # Empty storage root: there is no current install whose root a legacy
  # version-1 SELinux record without its own target could refer to, so such a
  # record is refused and left pending rather than guessed at.
  if ! python3 "$SCRIPT_DIR/host_access.py" revoke-all mac "$MAC_POLICY_MANIFEST" "" >/dev/null; then
    return 1
  fi
  ok "Removed recorded MAC changes"
}

# ── Stop and disable services ──────────────────────────────────────────────────
stop_and_disable_managed_services() {
  local unit state pid
  for unit in "${SERVICE_UNITS[@]}"; do
    state="$(systemctl show "$unit" -p ActiveState --value 2>/dev/null)" || return 1
    if [[ "$state" != inactive && "$state" != failed ]]; then
      systemctl stop "$unit" || return 1
      ok "Stopped $unit"
    fi
    state="$(systemctl show "$unit" -p ActiveState --value 2>/dev/null)" || return 1
    pid="$(systemctl show "$unit" -p MainPID --value 2>/dev/null)" || return 1
    [[ "$state" == inactive || "$state" == failed ]] || return 1
    [[ "$pid" == 0 ]] || return 1
    if systemctl is-enabled --quiet "$unit" 2>/dev/null; then
      systemctl disable "$unit" || return 1
      systemctl is-enabled --quiet "$unit" 2>/dev/null && return 1
      ok "Disabled $unit"
    fi
  done
}
info "Stopping and disabling AstinusLab services..."
stop_and_disable_managed_services \
  || die "Could not prove every managed single/split service is stopped and disabled"

# Host access policy is changed only after every managed writer is proven
# stopped and disabled. A failed quiesce therefore leaves AppArmor/SELinux/ACL
# state byte-for-byte untouched for the still-live installation.
mac_manifest_present=false
if [[ -e "$MAC_POLICY_MANIFEST" || -L "$MAC_POLICY_MANIFEST" ]]; then
  mac_manifest_present=true
  revoke_recorded_mac_changes || die "Could not revoke every recorded MAC change; retry uninstall after correcting the reported paths"
fi
if ! $mac_manifest_present; then
  warn "No MAC ownership manifest found. Preserving unrecorded AppArmor/SELinux policy."
fi

# ── Remove systemd unit files ──────────────────────────────────────────────────
info "Removing systemd unit files..."

remove_managed_systemd_units() {
  local unit_file
  for unit_file in \
    "$SYSTEMD_UNIT_DIR/astinuslab-api@.service" \
    "$SYSTEMD_UNIT_DIR/astinuslab-runner@.service" \
    "$SYSTEMD_UNIT_DIR/astinuslab-analysis@.service" \
    "$SYSTEMD_UNIT_DIR/astinuslab-api.service" \
    "$SYSTEMD_UNIT_DIR/astinuslab-runner.service" \
    "$SYSTEMD_UNIT_DIR/astinuslab-analysis.service"; do
    if [[ -e "$unit_file" || -L "$unit_file" ]]; then
      rm -f -- "$unit_file"
      ok "Removed $unit_file"
    fi
  done
}
remove_managed_systemd_units
systemctl daemon-reload
ok "systemd reloaded"

# ── Remove sudoers rule ────────────────────────────────────────────────────────
# Custom storage policy is removed with this install. To move a live install,
# use ops/deploy/configure-privileged-roots.sh before uninstalling instead.
SUDOERS_FILE="/etc/sudoers.d/astinuslab-runner"
if [[ -f "$SUDOERS_FILE" ]]; then
  info "Removing sudoers rule $SUDOERS_FILE..."
  rm -f "$SUDOERS_FILE"
  ok "Sudoers rule removed"
fi

TRAVERSAL_ACL_MANIFEST="${ASTINUSLAB_TRAVERSAL_ACL_MANIFEST:-/etc/astinuslab/traversal-acl.manifest}"

revoke_recorded_traversal_grants() {
  # Only the entries the installer recorded adding. Components it found already
  # carrying the entry were never recorded, so an operator's own grant — and
  # anything on a shared parent such as /home — survives untouched.
  [[ -e "$TRAVERSAL_ACL_MANIFEST" || -L "$TRAVERSAL_ACL_MANIFEST" ]] || return 0
  local removed
  if ! removed="$(python3 "$SCRIPT_DIR/host_access.py" revoke-all acl "$TRAVERSAL_ACL_MANIFEST" "")"; then
    return 1
  fi
  [[ "$removed" -eq 0 ]] || ok "Removed $removed recorded traversal grant(s)"
}

HELPER_INSTALL_DIR="${ASTINUSLAB_HELPER_INSTALL_DIR:-/usr/local/libexec/astinuslab}"
HELPER_SET=(
  libvirt_storage_admin.sh
  libvirt_storage_admin.py
  fix_libvirt_path_acl.sh
  fix_libvirt_path_acl.py
  acl_policy.py
  network_isolation_admin.sh
)
revoke_recorded_traversal_grants

remove_installed_privileged_helpers() {
  [[ -e "$HELPER_INSTALL_DIR" || -L "$HELPER_INSTALL_DIR" ]] || return 0
  python3 "$SCRIPT_DIR/privileged_state.py" remove-privileged-helpers \
    "$HELPER_INSTALL_DIR" "${HELPER_SET[@]}"
}

if [[ -e "$HELPER_INSTALL_DIR" || -L "$HELPER_INSTALL_DIR" ]]; then
  info "Removing installed privileged helpers from $HELPER_INSTALL_DIR..."
  remove_installed_privileged_helpers \
    || die "Could not safely remove the privileged helper directory: $HELPER_INSTALL_DIR"
  ok "Privileged helpers removed"
fi

POLICY_FILE="/etc/astinuslab/acl-policy.json"
if [[ -f "$POLICY_FILE" ]]; then
  rm -f -- "$POLICY_FILE"
fi
rm -f -- "$RUNTIME_IDENTITIES_FILE"
rmdir /etc/astinuslab 2>/dev/null || true

# ── Remove legacy third-party files shipped by older releases ────────────────
# Releases before the operator-supplied tool boundary installed a third-party
# executable, its checksum sidecar and a provenance README under
# $INSTALL_DIR/vol3/bin/ (see the deletions in commit a7a7147). Remove exactly
# those three known-legacy paths up front so they are gone even when the
# operator declines the full install-directory removal below. vol3/bin/ is now
# an operator-owned directory: never wipe it recursively, and drop it only if
# removing the legacy files leaves it empty.

LEGACY_VOL3_BIN_DIR="$INSTALL_DIR/vol3/bin"
legacy_vol3_bin_removed=false
legacy_vol3_bin_remaining=""
legacy_vol3_outcome="$(python3 "$SCRIPT_DIR/privileged_state.py" clean-legacy-vol3-bin "$INSTALL_DIR")" || die "Could not safely clean legacy vol3/bin entries"
while IFS= read -r legacy_line; do
  case "$legacy_line" in
    removed:*)
      info "Removing legacy third-party file left by an earlier install: $LEGACY_VOL3_BIN_DIR/${legacy_line#removed:}"
      legacy_vol3_bin_removed=true
      ;;
    remaining:*) legacy_vol3_bin_remaining="${legacy_line#remaining:}" ;;
    removed-dir) info "Removed now-empty directory: $LEGACY_VOL3_BIN_DIR" ;;
    skipped-cross-device) warn "Skipping legacy vol3/bin cleanup across a filesystem boundary" ;;
  esac
done <<< "$legacy_vol3_outcome"
if $legacy_vol3_bin_removed; then
  if [[ -n "$legacy_vol3_bin_remaining" ]]; then
    warn "Removed the known legacy files, but $LEGACY_VOL3_BIN_DIR still contains: $legacy_vol3_bin_remaining"
  else
    ok "Legacy third-party files removed from $LEGACY_VOL3_BIN_DIR"
  fi
fi

# ── Optionally remove install directory ───────────────────────────────────────
if [[ -d "$INSTALL_DIR" ]]; then
  echo
  warn "Install directory $INSTALL_DIR contains your artifacts and database."
  read -rp "[astinuslab] Delete $INSTALL_DIR and all its contents? [y/N] " yn
  if [[ "${yn,,}" == "y" ]]; then
    rm -rf "$INSTALL_DIR"
    ok "Removed $INSTALL_DIR"
  else
    warn "Skipped — $INSTALL_DIR left in place"
    if [[ -L "$INSTALL_DIR/current" || -d "$INSTALL_DIR/releases" ]]; then
      warn "Application source remains under $INSTALL_DIR/releases/ with $INSTALL_DIR/current pointing at it; no units run it now"
    fi
  fi
fi

# ── Optionally remove service user ────────────────────────────────────────────
if $SPLIT_USERS; then
  for split_user in astinuslab-api astinuslab-runner astinuslab-analysis; do
    if id "$split_user" &>/dev/null; then
      echo
      read -rp "[astinuslab] Delete split service user '$split_user'? [y/N] " yn
      if [[ "${yn,,}" == "y" ]]; then
        if userdel "$split_user"; then
          ok "User $split_user deleted"
        else
          warn "Could not delete split service user $split_user"
        fi
      else
        warn "Skipped — user $split_user left in place"
      fi
    fi
  done
elif id "$SERVICE_USER" &>/dev/null && [[ "$SERVICE_USER" != "root" ]]; then
  service_uid="$(id -u "$SERVICE_USER")"
  if (( service_uid >= 1000 )); then
    info "Keeping user '$SERVICE_USER' (uid $service_uid) — regular login account, not a system service user."
  else
    echo
    read -rp "[astinuslab] Delete service user '$SERVICE_USER'? [y/N] " yn
    if [[ "${yn,,}" == "y" ]]; then
      if userdel "$SERVICE_USER"; then
        ok "User $SERVICE_USER deleted"
      else
        warn "Could not delete user $SERVICE_USER — ensure no processes are running as that user and retry manually: userdel $SERVICE_USER"
      fi
    else
      warn "Skipped — user $SERVICE_USER left in place"
    fi
  fi
fi

echo
echo "────────────────────────────────────────────────────────────"
echo " AstinusLab uninstalled"
echo "────────────────────────────────────────────────────────────"
