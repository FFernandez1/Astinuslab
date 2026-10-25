#!/usr/bin/env bash
# AstinusLab installation script.
# Installs the API, host runner, sudoers rule, and systemd units.
# Must be run as root (or via sudo) from the repository root.
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/astinuslab}"
# Immutable, content-addressed release tree. INSTALL_DIR stays the stable
# root: it owns every mutable path and it is the identity the privileged
# helper bakes, so a release switch never changes what root policy trusts.
RELEASE_DIR=""
RELEASE_ID=""
# Set before a release switch so a failure after the schema moved can put
# both halves back: the database from its verified backup, and `current`
# from the release that was live before.
PREVIOUS_RELEASE_DIR=""
DB_BACKUP=""
DB_BACKUP_SHA256=""
DB_BACKUP_REVISION=""
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-astinuslab}}"
STORAGE_ROOT="${STORAGE_ROOT:-}"
STORAGE_ROOT_IDENTITY=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
# Resolved from the guarded repository path above.
# shellcheck disable=SC1091
source "$REPO_DIR/ops/images/image_download_metadata.sh"
ENABLE_SERVICES=true
START_SERVICES=true
RESTART_SERVICES=false
YES=false
GENERATE_SSH_KEY=false
NO_SSH_KEY=false
CHECK_ONLY=false
GENERATE_ADMIN_TOKEN=false
ADMIN_TOKEN_HASH=""
ALLOW_UNAUTHENTICATED_LOCAL=false
AUTH_ALLOW_UNAUTHENTICATED_LOCAL=false
GENERATED_ADMIN_TOKEN=""
SPLIT_USERS=false
API_USER="$SERVICE_USER"
RUNNER_USER="$SERVICE_USER"
ANALYSIS_USER="$SERVICE_USER"
SYSTEMD_UNIT_DIR="${ASTINUSLAB_SYSTEMD_UNIT_DIR:-/etc/systemd/system}"
MIGRATION_SNAPSHOT=""
MIGRATION_CREATED_DIRS=""
MIGRATION_SNAPSHOT_PARENT="${ASTINUSLAB_MIGRATION_SNAPSHOT_PARENT:-/var/tmp}"
SUDOERS_FILE="${ASTINUSLAB_SUDOERS_FILE:-/etc/sudoers.d/astinuslab-runner}"
HELPER_INSTALL_DIR="${ASTINUSLAB_HELPER_INSTALL_DIR:-/usr/local/libexec/astinuslab}"
POLICY_FILE="${ASTINUSLAB_POLICY_FILE:-/etc/astinuslab/acl-policy.json}"
RUNTIME_IDENTITIES_FILE="${ASTINUSLAB_RUNTIME_IDENTITIES_FILE:-/etc/astinuslab/runtime-identities}"
TRAVERSAL_ACL_MANIFEST="${ASTINUSLAB_TRAVERSAL_ACL_MANIFEST:-/etc/astinuslab/traversal-acl.manifest}"
MAC_POLICY_MANIFEST="${ASTINUSLAB_MAC_POLICY_MANIFEST:-/etc/astinuslab/mac-policy.manifest}"
HOST_ACCESS_TRANSACTION_LOG="${ASTINUSLAB_HOST_ACCESS_TRANSACTION_LOG:-}"
PREFLIGHT_KVM_DEVICE="${ASTINUSLAB_PREFLIGHT_KVM_DEVICE:-/dev/kvm}"
# libvirt starts virtiofsd itself for the job domain's managed virtiofs device, so
# the daemon binary has to exist on the host before any job can boot. Both the
# current and the legacy qemu path are accepted.
IFS=':' read -r -a VIRTIOFSD_CANDIDATES \
  <<< "${ASTINUSLAB_VIRTIOFSD_PATHS:-/usr/libexec/virtiofsd:/usr/lib/qemu/virtiofsd}"
PREFLIGHT_WARNINGS=0
PREFLIGHT_FAILURES=0
CURRENT_PHASE_NUMBER=0
CURRENT_PHASE_TITLE=""
declare -A REPORTED_INSTALL_PHASES=()

# ── Colour helpers ─────────────────────────────────────────────────────────────
info()  { echo -e "\033[1;34m[INFO]\033[0m  $*"; }
ok()    { echo -e "\033[1;32m[ OK ]\033[0m  $*"; }
warn()  { echo -e "\033[1;33m[WARN]\033[0m  $*"; }
die() {
  (( CURRENT_PHASE_NUMBER == 0 )) || fail_phase "$*"
  echo -e "\033[1;31m[FAIL]\033[0m  $*" >&2
  exit 1
}
find_virtiofsd_binary() {
  local candidate
  for candidate in "${VIRTIOFSD_CANDIDATES[@]}"; do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

virtiofsd_missing_message() {
  echo "virtiofsd not found at ${VIRTIOFSD_CANDIDATES[*]}. libvirt starts it for every job's shared output directory.
  Ubuntu/Debian: apt install virtiofsd
  Other distros: install your distribution's virtiofsd package"
}

virt_install_missing_message() {
  echo "virt-install not found. Attacker VM boot requires the command-line virt-install tool.
  Ubuntu/Debian: apt install virtinst
  Other distros: install your distribution's package providing virt-install"
}

preflight_pass() { ok "$*"; }
preflight_warn() { PREFLIGHT_WARNINGS=$((PREFLIGHT_WARNINGS + 1)); warn "$*"; }
preflight_fail() {
  PREFLIGHT_FAILURES=$((PREFLIGHT_FAILURES + 1))
  echo -e "\033[1;31m[FAIL]\033[0m  $*"
}

install_action() {
  if $SPLIT_USERS; then
    if [[ -e "$RUNTIME_IDENTITIES_FILE" || -L "$RUNTIME_IDENTITIES_FILE" ]]; then
      echo "split-user update"
    else
      echo "split-user migration"
    fi
  elif [[ -L "$INSTALL_DIR/current" ]]; then
    echo "update"
  else
    echo "clean install"
  fi
}

print_install_plan() {
  local runtime_users auth_mode services
  if $SPLIT_USERS; then
    runtime_users="$API_USER, $RUNNER_USER, $ANALYSIS_USER"
  else
    runtime_users="$SERVICE_USER"
  fi
  if $ALLOW_UNAUTHENTICATED_LOCAL; then
    auth_mode="disabled by explicit flag"
  elif [[ -f "$INSTALL_DIR/astinuslab.toml" ]] \
    && grep -Eq '^[[:space:]]*admin_api_token_hash[[:space:]]*=[[:space:]]*"sha256:[0-9a-fA-F]{64}"' \
      "$INSTALL_DIR/astinuslab.toml"; then
    auth_mode="preserve existing token"
  elif [[ -n "$ADMIN_TOKEN_HASH" ]]; then
    auth_mode="use supplied token hash"
  else
    auth_mode="generate admin token"
  fi
  if ! $START_SERVICES; then
    services="install only"
  elif $ENABLE_SERVICES; then
    services="enable and start"
  else
    services="start without enabling"
  fi
  cat <<EOF
AstinusLab installation plan
  Action:          $(install_action)
  Install root:    $INSTALL_DIR
  Storage root:    $STORAGE_ROOT
  Runtime users:   $runtime_users
  Authentication:  $auth_mode
  Services:        $services
  VM images:       downloaded after installation
  Volatility 3:    operator-supplied; guided separately
EOF
}

begin_phase() {
  local number="$1" title="$2"
  [[ -z "${REPORTED_INSTALL_PHASES[$number]:-}" ]] || return 0
  CURRENT_PHASE_NUMBER="$number"
  CURRENT_PHASE_TITLE="$title"
  REPORTED_INSTALL_PHASES[$number]=true
  info "[$number/9] $title"
}

complete_phase() {
  ok "Phase ${CURRENT_PHASE_NUMBER}/9 complete: ${CURRENT_PHASE_TITLE}"
}

skip_phase() {
  local number="$1" title="$2" reason="$3"
  [[ -z "${REPORTED_INSTALL_PHASES[$number]:-}" ]] || return 0
  begin_phase "$number" "$title"
  info "[SKIP] $reason"
}

fail_phase() {
  local operation="$1"
  warn "Failed during phase ${CURRENT_PHASE_NUMBER}/9: ${CURRENT_PHASE_TITLE}: $operation"
}

usage() {
  cat <<'EOF'
Usage: ops/deploy/install.sh [options]

Release/service installer options:
  --install-dir PATH     Install tree path (default: $INSTALL_DIR or /opt/astinuslab)
  --service-user USER    Service user (default: $SERVICE_USER, $SUDO_USER, or astinuslab)
  --split-users          Opt in to separate astinuslab-api, astinuslab-runner,
                         and astinuslab-analysis system identities
  --storage-root PATH    Storage root to trust (default: the installed ACL
                         policy's root when one exists, else INSTALL_DIR/storage)
  -y, --yes              Accept safe prompt defaults; generate missing SSH key
  --generate-ssh-key     Generate default SSH key if no active key exists
  --no-ssh-key           Skip SSH key generation if no active key exists
  --no-enable            Do not enable systemd services
  --no-start             Do not start/restart systemd services
  --restart              Restart services after install/update instead of start
  --check                Run read-only preflight checks and exit
  --generate-admin-token Generate a new admin token on clean install
  --admin-token-hash H   Use supplied sha256:<64-hex> admin token hash
  --allow-unauthenticated-local
                         Explicit development-only no-auth install mode
  Custom storage roots: sudo ops/deploy/configure-privileged-roots.sh --install-dir PATH --storage-root PATH
  Install-root policy: INSTALL_DIR and every existing ancestor must be root:root and not group/other-writable.
                       Ordinary operator-owned paths below /home are not supported; use /opt/astinuslab.
  -h, --help             Show this help
EOF
}

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

validate_existing_install_dir_state() {
  [[ -e "$INSTALL_DIR" || -L "$INSTALL_DIR" ]] || return 0
  if ! command -v python3 >/dev/null 2>&1; then
    # Pristine hosts reach this read-only gate before the package bootstrap.
    # Shell type tests reject the dangerous states now; the O_NOFOLLOW
    # descriptor check is repeated after prerequisites install Python.
    [[ -d "$INSTALL_DIR" && ! -L "$INSTALL_DIR" ]] || {
      warn "existing INSTALL_DIR must be a real non-symlink directory"
      return 1
    }
    return 0
  fi
  python3 - "$INSTALL_DIR" <<'PY'
import os
import stat
import sys

path = sys.argv[1]
flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
try:
    descriptor = os.open(path, flags)
except OSError as exc:
    raise SystemExit(f"existing INSTALL_DIR must be a real non-symlink directory: {exc}") from exc
try:
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        raise SystemExit("existing INSTALL_DIR is not a directory")
finally:
    os.close(descriptor)
PY
}

validate_existing_install_dir_authority() {
  if ! command -v python3 >/dev/null 2>&1; then
    # Clean hosts may need Python installed by check_prerequisites. This
    # read-only shell walk is sufficient before package-manager mutation; the
    # descriptor walk below is run again after Python is present and before any
    # INSTALL_DIR mutation.
    local current="$INSTALL_DIR" metadata owner group mode parent
    while [[ ! -e "$current" && ! -L "$current" ]]; do
      parent="$(dirname -- "$current")"
      [[ "$parent" != "$current" ]] || break
      current="$parent"
    done
    while :; do
      [[ -d "$current" && ! -L "$current" ]] \
        || { warn "INSTALL_DIR ancestor is not a real directory: $current"; return 1; }
      metadata="$(stat -c '%u:%g:%a' -- "$current")" || return 1
      IFS=: read -r owner group mode <<< "$metadata"
      [[ "$owner" == 0 && "$group" == 0 && $((8#$mode & 0022)) -eq 0 ]] \
        || { warn "INSTALL_DIR ancestor is not trusted: $current"; return 1; }
      [[ "$current" == / ]] && break
      current="$(dirname -- "$current")"
    done
    return 0
  fi
  python3 - "$INSTALL_DIR" <<'PY'
import os
import stat
import sys

path = sys.argv[1]
parts = [part for part in path.split("/") if part]
if any(part in {".", ".."} for part in parts):
    raise SystemExit("INSTALL_DIR must not contain dot path components")
flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
descriptor = os.open("/", flags)
try:
    for part in parts:
        try:
            child = os.open(part, flags, dir_fd=descriptor)
        except FileNotFoundError:
            # Root will create the missing suffix below the last trusted,
            # non-writable ancestor.
            break
        except OSError as exc:
            raise SystemExit(f"INSTALL_DIR ancestor must be a real non-symlink directory: {part}: {exc}") from exc
        os.close(descriptor)
        descriptor = child
        info = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != 0
            or info.st_gid != 0
            or info.st_mode & 0o022
        ):
            raise SystemExit(
                "INSTALL_DIR and every existing ancestor must be root:root "
                "and non-writable by group/other"
            )
finally:
    os.close(descriptor)
PY
}

validate_service_user() {
  [[ -n "$SERVICE_USER" ]] || die "SERVICE_USER cannot be empty"
  [[ "$SERVICE_USER" != *$'\n'* && "$SERVICE_USER" != *:* ]] || die "SERVICE_USER contains unsupported characters"
}

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

foreign_template_instances() {
  local loaded enabled
  loaded="$(systemctl list-units --all --no-legend --plain \
    "astinuslab-api@*.service" "astinuslab-runner@*.service" "astinuslab-analysis@*.service" \
    2>/dev/null)" || return 1
  # systemd 255 exits 1 from `list-unit-files` when the supplied patterns match
  # nothing — the state of every pristine host a clean install runs on — so the
  # patterned form made "no AstinusLab units exist" indistinguishable from a
  # broken query and aborted the install before prerequisites. The unpatterned
  # query exits 0 on that same host, so a non-zero status here is still a real
  # failure and still refuses the install. Selection moves entirely into the awk
  # filter below, which matches only managed template instances with a non-empty
  # instance name; the bare `name@.service` templates carry no instance and are
  # excluded by the same expression.
  enabled="$(systemctl list-unit-files --no-legend 2>/dev/null)" || return 1
  printf '%s\n%s\n' "$loaded" "$enabled" \
    | awk -v api="astinuslab-api@${SERVICE_USER}.service" \
      -v runner="astinuslab-runner@${SERVICE_USER}.service" \
      -v analysis="astinuslab-analysis@${SERVICE_USER}.service" '
      { unit = ($1 == "●" ? $2 : $1) }
      unit !~ /^astinuslab-(api|runner|analysis)@.+\.service$/ { next }
      unit != api && unit != runner && unit != analysis { print unit }
    ' | sort -u
}

refuse_foreign_template_instances() {
  local instances
  instances="$(foreign_template_instances)" || return 1
  [[ -z "$instances" ]] || {
    warn "Foreign AstinusLab template instances are loaded or enabled:"
    printf '%s\n' "$instances" >&2
    return 1
  }
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --install-dir) [[ $# -ge 2 ]] || die "--install-dir requires PATH"; INSTALL_DIR="$2"; shift 2 ;;
      --service-user) [[ $# -ge 2 ]] || die "--service-user requires USER"; SERVICE_USER="$2"; shift 2 ;;
      --split-users) SPLIT_USERS=true; shift ;;
      --storage-root) [[ $# -ge 2 ]] || die "--storage-root requires PATH"; STORAGE_ROOT="$2"; shift 2 ;;
      -y|--yes) YES=true; shift ;;
      --generate-ssh-key) GENERATE_SSH_KEY=true; shift ;;
      --no-ssh-key) NO_SSH_KEY=true; shift ;;
      --no-enable) ENABLE_SERVICES=false; shift ;;
      --no-start) START_SERVICES=false; shift ;;
      # Restarting is now the only supported behaviour; the flag is kept so
      # existing invocations and documentation do not break.
      --restart) RESTART_SERVICES=true; shift ;;
      --check) CHECK_ONLY=true; shift ;;
      --generate-admin-token) GENERATE_ADMIN_TOKEN=true; shift ;;
      --admin-token-hash) [[ $# -ge 2 ]] || die "--admin-token-hash requires HASH"; ADMIN_TOKEN_HASH="$2"; shift 2 ;;
      --allow-unauthenticated-local) ALLOW_UNAUTHENTICATED_LOCAL=true; AUTH_ALLOW_UNAUTHENTICATED_LOCAL=true; shift ;;
      -h|--help) usage; exit 0 ;;
      *) die "Unknown option: $1" ;;
    esac
  done
  validate_install_dir
  validate_service_user
  if $SPLIT_USERS; then
    API_USER="astinuslab-api"
    RUNNER_USER="astinuslab-runner"
    ANALYSIS_USER="astinuslab-analysis"
  else
    API_USER="$SERVICE_USER"
    RUNNER_USER="$SERVICE_USER"
    ANALYSIS_USER="$SERVICE_USER"
  fi
  if $RESTART_SERVICES && ! $START_SERVICES; then
    die "--restart conflicts with --no-start"
  fi
  if $SPLIT_USERS && ! $START_SERVICES; then
    die "--split-users requires starting and verifying the new services"
  fi
  if $GENERATE_SSH_KEY && $NO_SSH_KEY; then
    die "--generate-ssh-key conflicts with --no-ssh-key"
  fi
  if $GENERATE_ADMIN_TOKEN && [[ -n "$ADMIN_TOKEN_HASH" ]]; then
    die "--generate-admin-token conflicts with --admin-token-hash"
  fi
  if $GENERATE_ADMIN_TOKEN && $ALLOW_UNAUTHENTICATED_LOCAL; then
    die "--generate-admin-token conflicts with --allow-unauthenticated-local"
  fi
  if [[ -n "$ADMIN_TOKEN_HASH" ]] && $ALLOW_UNAUTHENTICATED_LOCAL; then
    die "--admin-token-hash conflicts with --allow-unauthenticated-local"
  fi
  if [[ -n "$ADMIN_TOKEN_HASH" ]] && ! [[ "$ADMIN_TOKEN_HASH" =~ ^sha256:[0-9a-fA-F]{64}$ ]]; then
    die "--admin-token-hash must be sha256:<64-hex>"
  fi
  if ! $SPLIT_USERS && ! $CHECK_ONLY; then
    if [[ -e "$RUNTIME_IDENTITIES_FILE" || -L "$RUNTIME_IDENTITIES_FILE" ]]; then
      split_identity_marker_is_valid \
        || die "incomplete or malformed split-user state; refuse a single-identity update"
      die "existing split-user install requires --split-users for an update"
    fi
    if explicit_split_units_present; then
      die "incomplete or malformed split-user state; explicit split units exist without a valid marker"
    fi
  fi
}

require_root() {
  [[ $EUID -eq 0 ]] || die "Run this script as root: sudo bash ops/deploy/install.sh"
}

detect_os_family() {
  if [[ -f /etc/os-release ]]; then
    # The guarded host file is the OS-family data source.
    # shellcheck disable=SC1091
    . /etc/os-release
    case "${ID_LIKE:-$ID}" in
      *debian*|*ubuntu*) echo "debian" ;;
      *fedora*|*rhel*|*centos*) echo "rhel" ;;
      *arch*) echo "arch" ;;
      *suse*) echo "suse" ;;
      *) echo "unknown" ;;
    esac
  else
    echo "unknown"
  fi
}

preflight_policy_matches_storage() {
  [[ -e "$POLICY_FILE" || -L "$POLICY_FILE" ]] || return 0
  if python3 - "$POLICY_FILE" "$STORAGE_ROOT" "$RUNNER_USER" <<'PY'
import json
import os
import stat
import sys

policy_path, storage_root, runner_user = sys.argv[1:]
flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
descriptor = os.open(policy_path, flags)
try:
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        raise SystemExit("privileged-root policy is not a regular file")
    with os.fdopen(os.dup(descriptor), encoding="utf-8") as handle:
        policy = json.load(handle)
finally:
    os.close(descriptor)

roots = {os.path.realpath(path) for path in policy.get("allowed_roots") or []}
callers = set(policy.get("allowed_callers") or [])
if os.path.realpath(storage_root) not in roots:
    raise SystemExit("privileged-root policy does not allow the resolved storage root")
if runner_user not in callers:
    raise SystemExit("privileged-root policy does not allow the runner identity")
PY
  then
    return 0
  fi
  return 1
}

preflight_units_match() {
  local comparison_dir status=0 unit src expected installed run_user api_unit target
  comparison_dir="$(mktemp -d)" || return 1
  installed=false
  printf -v api_unit 'astinuslab-api@%s.service' "$SERVICE_USER"
  $SPLIT_USERS && api_unit="astinuslab-api.service"

  for unit in astinuslab-api astinuslab-runner astinuslab-analysis; do
    src="$REPO_DIR/ops/systemd/${unit}.service"
    if $SPLIT_USERS; then
      target="$SYSTEMD_UNIT_DIR/${unit}.service"
      expected="$comparison_dir/${unit}.service"
      case "$unit" in
        astinuslab-api) run_user="$API_USER" ;;
        astinuslab-runner) run_user="$RUNNER_USER" ;;
        astinuslab-analysis) run_user="$ANALYSIS_USER" ;;
      esac
      render_template "$src" "$expected" "$INSTALL_DIR" "$STORAGE_ROOT" \
        "$run_user" astinuslab "$api_unit" "$unit" true || { status=1; break; }
      [[ ! -e "$SYSTEMD_UNIT_DIR/${unit}@.service" && ! -L "$SYSTEMD_UNIT_DIR/${unit}@.service" ]] \
        || status=1
    else
      target="$SYSTEMD_UNIT_DIR/${unit}@.service"
      expected="$comparison_dir/${unit}@.service"
      render_template "$src" "$expected" "$INSTALL_DIR" "$STORAGE_ROOT" || { status=1; break; }
      [[ ! -e "$SYSTEMD_UNIT_DIR/${unit}.service" && ! -L "$SYSTEMD_UNIT_DIR/${unit}.service" ]] \
        || status=1
    fi
    if [[ -e "$target" || -L "$target" ]]; then
      installed=true
      [[ -f "$target" && ! -L "$target" ]] && cmp -s -- "$expected" "$target" || status=1
    fi
  done

  rm -rf -- "$comparison_dir"
  $installed || return "$status"
  return "$status"
}

run_preflight() {
  PREFLIGHT_WARNINGS=0
  PREFLIGHT_FAILURES=0
  info "Preflight checks for AstinusLab release install"
  if (validate_install_dir); then
    preflight_pass "Install path is valid: $INSTALL_DIR"
  else
    preflight_fail "Install path is invalid: $INSTALL_DIR"
  fi
  if (validate_service_user); then
    preflight_pass "Service-user name is valid: $SERVICE_USER"
  else
    preflight_fail "Service-user name is invalid: $SERVICE_USER"
  fi

  local required_path
  for required_path in pyproject.toml src/astinuslab/main.py ops/host ops/systemd; do
    if [[ -e "$REPO_DIR/$required_path" ]]; then
      preflight_pass "Release input present: $required_path"
    else
      preflight_fail "Release input missing: $REPO_DIR/$required_path"
    fi
  done

  local cmd
  for cmd in python3 cc virsh virt-install qemu-img cloud-localds pkg-config rsync ssh-keygen visudo; do
    if command -v "$cmd" >/dev/null 2>&1; then
      preflight_pass "Found command: $cmd"
    else
      preflight_fail "Missing command: $cmd"
    fi
  done

  if command -v python3 >/dev/null 2>&1; then
    if python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)"; then
      preflight_pass "Python 3.12+"
    else
      preflight_fail "Python 3.12+ required"
    fi
    if python3 -c 'import ensurepip, venv' >/dev/null 2>&1; then
      preflight_pass "Python venv support"
    else
      preflight_fail "Python venv support is missing"
    fi
    if python3 -c 'import os, sysconfig; raise SystemExit(not os.path.isfile(os.path.join(sysconfig.get_path("include"), "Python.h")))' \
      >/dev/null 2>&1; then
      preflight_pass "Python development headers"
    else
      preflight_fail "Python development headers are missing"
    fi
  fi
  if command -v pkg-config >/dev/null 2>&1; then
    if pkg-config --exists libvirt; then
      preflight_pass "libvirt development headers"
    else
      preflight_fail "libvirt development headers are missing"
    fi
  fi

  if id "$SERVICE_USER" >/dev/null 2>&1; then
    preflight_pass "Service user exists: $SERVICE_USER"
  else
    preflight_warn "Service user will be created by full install: $SERVICE_USER"
  fi
  if [[ $EUID -ne 0 ]]; then
    info "Full install will require sudo for package/user/systemd/sudoers/MAC operations"
  fi
  if [[ -e "$PREFLIGHT_KVM_DEVICE" ]]; then
    preflight_pass "$PREFLIGHT_KVM_DEVICE present"
  else
    preflight_fail "$PREFLIGHT_KVM_DEVICE missing (enable VT-x/AMD-V; install qemu-kvm)"
  fi
  if command -v virsh >/dev/null 2>&1 && virsh -c qemu:///system version >/dev/null 2>&1; then
    preflight_pass "libvirtd reachable"
  else
    preflight_fail "libvirtd not reachable at qemu:///system"
  fi
  local virtiofsd_path
  if virtiofsd_path="$(find_virtiofsd_binary)"; then
    preflight_pass "virtiofsd present: $virtiofsd_path"
  else
    preflight_fail "$(virtiofsd_missing_message)"
  fi

  if validate_existing_install_dir_state; then
    preflight_pass "Existing install-root state is safe"
  else
    preflight_fail "Existing install-root state is unsafe"
  fi
  if validate_existing_install_dir_authority; then
    preflight_pass "install-root authority is trusted"
  else
    preflight_fail "install-root authority is not trusted"
  fi
  if resolve_storage_root; then
    if validate_install_storage_root; then
      preflight_pass "Storage root is safe: $STORAGE_ROOT"
    else
      preflight_fail "Storage root is unsafe: $STORAGE_ROOT"
    fi
    if (verify_storage_root_matches_config); then
      preflight_pass "Storage configuration agrees with privileged root"
    else
      preflight_fail "Storage configuration disagrees with privileged root"
    fi
    if command -v python3 >/dev/null 2>&1 && preflight_policy_matches_storage; then
      preflight_pass "Privileged-root policy matches storage and runner identity"
    elif [[ -e "$POLICY_FILE" || -L "$POLICY_FILE" ]]; then
      preflight_fail "Privileged-root policy drift detected"
    fi
    if command -v python3 >/dev/null 2>&1 && preflight_units_match; then
      preflight_pass "Installed systemd units match the requested install plan"
    elif [[ -d "$SYSTEMD_UNIT_DIR" ]]; then
      preflight_fail "Installed systemd unit drift detected"
    fi
  else
    preflight_fail "Could not resolve storage root"
  fi

  if (( PREFLIGHT_FAILURES > 0 )); then
    echo "Preflight result: NOT READY"
    return 1
  elif (( PREFLIGHT_WARNINGS > 0 )); then
    echo "Preflight result: READY WITH WARNINGS"
  else
    echo "Preflight result: READY"
  fi
}

check_prerequisites() {
  info "Checking prerequisites..."
  local os_family
  os_family="$(detect_os_family)"

  local missing_packages=()
  local missing_cmds=()
  local command_packages=()
  local cmd
  for cmd in python3 cc virsh qemu-img cloud-localds pkg-config rsync ssh-keygen visudo; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
      missing_cmds+=("$cmd")
    fi
  done
  if command -v python3 >/dev/null 2>&1 && ! python3 -c 'import ensurepip, venv' >/dev/null 2>&1; then
    missing_cmds+=("python-venv")
  fi
  if command -v python3 >/dev/null 2>&1 \
    && ! python3 -c 'import os, sysconfig; raise SystemExit(not os.path.isfile(os.path.join(sysconfig.get_path("include"), "Python.h")))' \
      >/dev/null 2>&1; then
    missing_cmds+=("python-dev")
  fi
  if ! command -v pkg-config >/dev/null 2>&1 || ! pkg-config --exists libvirt >/dev/null 2>&1; then
    missing_cmds+=("libvirt-dev")
  fi

  if (( ${#missing_cmds[@]} > 0 )); then
    info "Missing commands: ${missing_cmds[*]}"
    case "$os_family" in
      debian)
        declare -A DEB_PKG=( ["python3"]="python3 python3-venv python3-pip python3-dev" ["python-venv"]="python3-venv python3-pip" ["python-dev"]="python3-dev" ["cc"]="build-essential python3-dev" ["virsh"]="libvirt-clients" ["qemu-img"]="qemu-utils" ["cloud-localds"]="cloud-image-utils" ["pkg-config"]="pkg-config" ["libvirt-dev"]="libvirt-dev" ["rsync"]="rsync" ["ssh-keygen"]="openssh-client" ["visudo"]="sudo" )
        for cmd in "${missing_cmds[@]}"; do
          read -r -a command_packages <<< "${DEB_PKG[$cmd]}"
          missing_packages+=("${command_packages[@]}")
        done
        info "Installing missing system dependencies (apt)..."
        apt-get update -qq
        apt-get install -y -qq "${missing_packages[@]}" || die "Failed to install system dependencies"
        ;;
      rhel)
        declare -A RHEL_PKG=( ["python3"]="python3 python3-pip python3-devel" ["python-venv"]="python3-pip" ["python-dev"]="python3-devel" ["cc"]="gcc python3-devel" ["virsh"]="libvirt-client" ["qemu-img"]="qemu-img" ["cloud-localds"]="cloud-utils" ["pkg-config"]="pkgconf" ["libvirt-dev"]="libvirt-devel" ["rsync"]="rsync" ["ssh-keygen"]="openssh-clients" ["visudo"]="sudo" )
        for cmd in "${missing_cmds[@]}"; do
          read -r -a command_packages <<< "${RHEL_PKG[$cmd]}"
          missing_packages+=("${command_packages[@]}")
        done
        info "Installing missing system dependencies (dnf)..."
        dnf install -y -q "${missing_packages[@]}" policycoreutils-python-utils || die "Failed to install system dependencies"
        ;;
      arch)
        declare -A ARCH_PKG=( ["python3"]="python python-pip" ["python-venv"]="python-pip" ["python-dev"]="python" ["cc"]="base-devel" ["virsh"]="libvirt" ["qemu-img"]="qemu-desktop" ["cloud-localds"]="cloud-guest-utils" ["pkg-config"]="pkgconf" ["libvirt-dev"]="libvirt" ["rsync"]="rsync" ["ssh-keygen"]="openssh" ["visudo"]="sudo" )
        for cmd in "${missing_cmds[@]}"; do
          read -r -a command_packages <<< "${ARCH_PKG[$cmd]}"
          missing_packages+=("${command_packages[@]}")
        done
        info "Installing missing system dependencies (pacman)..."
        pacman -S --needed --noconfirm "${missing_packages[@]}" || die "Failed to install system dependencies"
        ;;
      suse)
        declare -A SUSE_PKG=( ["python3"]="python3 python3-pip python3-devel" ["python-venv"]="python3-pip" ["python-dev"]="python3-devel" ["cc"]="gcc python3-devel" ["virsh"]="libvirt-client" ["qemu-img"]="qemu-tools" ["cloud-localds"]="cloud-utils" ["pkg-config"]="pkgconf" ["libvirt-dev"]="libvirt-devel" ["rsync"]="rsync" ["ssh-keygen"]="openssh" ["visudo"]="sudo" )
        for cmd in "${missing_cmds[@]}"; do
          read -r -a command_packages <<< "${SUSE_PKG[$cmd]}"
          missing_packages+=("${command_packages[@]}")
        done
        info "Installing missing system dependencies (zypper)..."
        zypper install -y "${missing_packages[@]}" || die "Failed to install system dependencies"
        ;;
      *)
        die "Required commands not found (${missing_cmds[*]}) and OS not recognized for automatic installation."
        ;;
    esac
  fi

  python3 -c 'import ensurepip, venv' >/dev/null 2>&1 \
    || die "Python venv support with ensurepip is required (Ubuntu package: python3-venv)"
  python3 -c 'import os, sysconfig; raise SystemExit(not os.path.isfile(os.path.join(sysconfig.get_path("include"), "Python.h")))' \
    >/dev/null 2>&1 || die "Python development headers are required (Ubuntu package: python3-dev)"
  pkg-config --exists libvirt >/dev/null 2>&1 \
    || die "Libvirt development headers are required (Ubuntu package: libvirt-dev)"

  # The target XML boot path does not use virt-install, but paired attacker Jobs
  # do. Keep the CLI prerequisite explicit without pulling in a manager GUI.
  if ! command -v virt-install >/dev/null 2>&1; then
    if [[ "$os_family" == debian ]]; then
      info "Installing virt-install (apt package: virtinst)..."
      apt-get update -qq
      apt-get install -y -qq --no-install-recommends virtinst \
        || die "$(virt_install_missing_message)"
    fi
    command -v virt-install >/dev/null 2>&1 || die "$(virt_install_missing_message)"
  fi

  if ! command -v virt-inspector >/dev/null 2>&1; then
    case "$os_family" in
      debian) apt-get install -y -qq libguestfs-tools || warn "libguestfs-tools install failed" ;;
      rhel) dnf install -y -q libguestfs-tools || warn "libguestfs-tools install failed" ;;
      arch) pacman -S --needed --noconfirm libguestfs || warn "libguestfs install failed" ;;
      suse) zypper install -y guestfs-tools || warn "guestfs-tools install failed" ;;
      *) warn "virt-inspector not found; image inspection will fall back to filename heuristics" ;;
    esac
  fi
  python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)"     || die "Python 3.12+ required"
  # Hypervisor reality checks — client tools alone are not a working KVM host.
  if [[ ! -e /dev/kvm ]]; then
    die "/dev/kvm not found. Enable VT-x/AMD-V in firmware and install the hypervisor:
  Debian/Ubuntu: apt install qemu-kvm libvirt-daemon-system
  Fedora/RHEL:   dnf install qemu-kvm libvirt
  Arch:          pacman -S qemu-desktop libvirt
  openSUSE:      zypper install qemu-kvm libvirt
Then re-run this installer."
  fi
  if ! virsh -c qemu:///system version >/dev/null 2>&1; then
    die "libvirtd is not reachable at qemu:///system. Install and start it:
  Debian/Ubuntu: apt install libvirt-daemon-system && systemctl enable --now libvirtd
  Fedora/RHEL:   dnf install libvirt && systemctl enable --now libvirtd
Then re-run this installer."
  fi
  ok "Hypervisor: /dev/kvm present, libvirtd reachable"
  # libvirt launches virtiofsd itself for the job's managed virtiofs share. Only the
  # Debian/Ubuntu package name is backed by acceptance evidence, so any other family
  # gets an actionable failure rather than a guessed package name.
  local virtiofsd_path
  if ! virtiofsd_path="$(find_virtiofsd_binary)"; then
    if [[ "$os_family" == debian ]]; then
      info "Installing virtiofsd (apt)..."
      apt-get update -qq
      apt-get install -y -qq virtiofsd || warn "virtiofsd install failed"
    fi
    virtiofsd_path="$(find_virtiofsd_binary)" || die "$(virtiofsd_missing_message)
Then re-run this installer."
  fi
  ok "virtiofs: libvirt-managed daemon at $virtiofsd_path"
  ok "Prerequisites satisfied"
}

ensure_service_user() {
  if ! getent group astinuslab >/dev/null; then
    info "Creating group astinuslab..."
    groupadd -r astinuslab || return 1
  fi
  if ! id "$SERVICE_USER" &>/dev/null; then
    info "Creating user $SERVICE_USER..."
    useradd --system --no-create-home --shell /usr/sbin/nologin -g astinuslab "$SERVICE_USER"
  else
    usermod -aG astinuslab "$SERVICE_USER"
  fi
  local grp groups_added=false
  for grp in libvirt kvm; do
    if ! getent group "$grp" >/dev/null; then
      warn "Group '$grp' does not exist — hypervisor packages missing? VM launches will fail."
      continue
    fi
    member_of="$(id -nG "$SERVICE_USER" 2>/dev/null | tr ' ' '\n' || true)"
    if ! grep -qx "$grp" <<<"$member_of"; then
      usermod -aG "$grp" "$SERVICE_USER"
      groups_added=true
    fi
  done
  if $groups_added; then
    warn "Added $SERVICE_USER to libvirt/kvm. Interactive shells need a re-login to pick this up; the systemd services get it on next start."
  fi
  ok "Service user: $SERVICE_USER, Group: astinuslab"
}

ensure_split_users() {
  API_USER="astinuslab-api"
  RUNNER_USER="astinuslab-runner"
  ANALYSIS_USER="astinuslab-analysis"
  if ! getent group astinuslab >/dev/null; then
    info "Creating group astinuslab..."
    groupadd -r astinuslab || return 1
  fi

  local user runner_created=false
  for user in "$API_USER" "$RUNNER_USER" "$ANALYSIS_USER"; do
    if ! id "$user" &>/dev/null; then
      info "Creating user $user..."
      useradd --system --no-create-home --shell /usr/sbin/nologin -g astinuslab "$user" || return 1
      [[ "$user" != "$RUNNER_USER" ]] || runner_created=true
    else
      local primary groups group
      primary="$(id -gn "$user")" || return 1
      groups="$(id -nG "$user" | tr ' ' '\n')" || return 1
      if [[ "$primary" != astinuslab ]]; then
        echo "incompatible pre-existing identity $user: primary group must be astinuslab" >&2
        return 1
      fi
      while IFS= read -r group; do
        [[ -z "$group" || "$group" == astinuslab ]] && continue
        if [[ "$user" == "$RUNNER_USER" && ( "$group" == libvirt || "$group" == kvm ) ]]; then
          continue
        fi
        echo "incompatible pre-existing identity $user: unexpected group $group" >&2
        return 1
      done <<< "$groups"
    fi
  done

  # Only the runner crosses the virtualization boundary. Existing identities
  # are validated above but never mutated: if validation fails, rollback has no
  # account-group state to reconstruct.
  local hypervisor_groups=()
  local grp
  for grp in libvirt kvm; do
    if getent group "$grp" >/dev/null; then
      hypervisor_groups+=("$grp")
    else
      warn "Group '$grp' does not exist — VM launches will fail."
    fi
  done
  if (( ${#hypervisor_groups[@]} > 0 )); then
    local joined
    joined="$(IFS=,; echo "${hypervisor_groups[*]}")"
    if $runner_created; then
      usermod -aG "$joined" "$RUNNER_USER" || return 1
    else
      local runner_groups
      runner_groups="$(id -nG "$RUNNER_USER" | tr ' ' '\n')" || return 1
      for grp in "${hypervisor_groups[@]}"; do
        if ! grep -qx "$grp" <<< "$runner_groups"; then
          echo "incompatible pre-existing identity $RUNNER_USER: missing group $grp" >&2
          return 1
        fi
      done
    fi
  fi
  ok "Split identities: API=$API_USER runner=$RUNNER_USER analysis=$ANALYSIS_USER group=astinuslab"
}

sync_source_tree() {
  local destination="${1:-$INSTALL_DIR}"
  mkdir -p "$destination"
  # `.venv*`, not `.venv`: a suffixed sibling such as .venv313 defeated the
  # literal pattern and copied a whole interpreter tree into the install dir.
  # The build/cache/tooling entries below are dev residue on a working-tree
  # install. .github and deliberately tracked plans reach clean archive installs
  # too. .hermes and .superpowers are untracked working-tree residue; retaining
  # their exclusions is defense-in-depth. `site` is built docs, which would
  # otherwise sneak past the docs/ exclusion. Anchored where the name is
  # plausible elsewhere in the tree, unanchored where it never is.
  # NOTE: '/storage/*' is anchored on purpose. Unanchored, rsync applies the
  # pattern at every depth and empties the application's own
  # src/astinuslab/storage package, which astinuslab.main imports at start - the
  # installed tree then cannot boot at all.
  rsync -a --exclude='.venv*' --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='.pytest_cache' --exclude='.claude' --exclude='.git' \
    --exclude='.mypy_cache' --exclude='.ruff_cache' --exclude='node_modules' \
    --exclude='.github' --exclude='.hermes' --exclude='.superpowers' \
    --exclude='plans' --exclude='/tools' --exclude='/site' --exclude='/graphify-out' \
    --exclude='astinuslab.egg-info' \
    --include='/docs/' --include='/docs/user/***' --include='/docs/usage/***' \
    --include='/docs/reference/***' --include='/docs/api/***' --include='/docs/architecture/***' \
    --include='/docs/development/***' --include='/docs/community/***' \
    --include='/docs/index*.md' --include='/docs/LICENSE*.md' --include='/docs/THIRD_PARTY_NOTICES*.md' \
    --exclude='/docs/**' --exclude='docs' \
    --exclude='testdata' --exclude='tests' --exclude='to-do' --exclude='troubleshooting' \
    --exclude='images/manifests' --exclude='images/definitions/*' --exclude='/vol3/bin/' \
    --exclude='/storage/*' \
    --exclude='astinuslab.toml' --exclude='config/astinuslab.env' \
    --include='config/catalog/*.example.yaml' --exclude='config/catalog/*.yaml' \
    --include='config/catalog/*.example.yml' --exclude='config/catalog/*.yml' \
    "$REPO_DIR/" "$destination/"
}

# Releases before the operator-supplied tool boundary shipped a third-party
# executable, its checksum sidecar and a provenance README under vol3/bin/ (see
# the deletions in commit a7a7147). The rsync above excludes that path but has
# no --delete, so an in-place update would leave the old copies on disk forever
# — including the README that still advertises a vendored binary this project
# no longer redistributes. Remove exactly those three known-legacy paths —
# never a recursive wipe, because vol3/bin/ is now an operator-owned directory.
remove_legacy_vol3_bin_files() {
  local outcome line removed=false remaining=""
  outcome="$(python3 "$SCRIPT_DIR/privileged_state.py" clean-legacy-vol3-bin "$INSTALL_DIR")" || return 1
  while IFS= read -r line; do
    case "$line" in
      removed:*)
        info "Removing legacy third-party file left by an earlier install: $INSTALL_DIR/vol3/bin/${line#removed:}"
        removed=true
        ;;
      remaining:*) remaining="${line#remaining:}" ;;
      removed-dir) info "Removed now-empty directory: $INSTALL_DIR/vol3/bin" ;;
      skipped-cross-device)
        warn "Skipping legacy vol3/bin cleanup across a filesystem boundary"
        ;;
    esac
  done <<< "$outcome"
  $removed || return 0
  if [[ -n "$remaining" ]]; then
    warn "Removed the known legacy files, but $INSTALL_DIR/vol3/bin still contains: $remaining"
  else
    ok "Legacy third-party files removed from $INSTALL_DIR/vol3/bin"
  fi
}

install_config_if_missing() {
  local src="$1" dst="$2"
  local outcome
  outcome="$(python3 -B "$SCRIPT_DIR/managed_config.py" install-config "$src" "$dst")" || return 1
  case "$outcome" in
    preserved) info "Preserved existing config: $dst" ;;
    missing-source) warn "Default config source missing: $src" ;;
  esac
}

validate_managed_config_destinations() {
  python3 -B "$SCRIPT_DIR/managed_config.py" validate-destinations "$INSTALL_DIR" \
    astinuslab.toml \
    config/astinuslab.env \
    config/catalog/targets.yaml \
    config/catalog/workloads.yaml \
    config/catalog/scenarios.yaml \
    config/catalog/attackers.yaml \
    config/catalog/profiles.yaml \
    config/catalog/network_nodes.yaml
}

install_runtime_config_defaults() {
  local config_owner="$SERVICE_USER"
  $SPLIT_USERS && config_owner="$API_USER"
  validate_managed_config_destinations || return 1
  prepare_managed_directory config "$config_owner" astinuslab 0750 || return 1
  prepare_managed_directory config/catalog "$config_owner" astinuslab 0750 || return 1
  install_config_if_missing "$REPO_DIR/astinuslab.toml.example" "$INSTALL_DIR/astinuslab.toml" || return 1
  install_config_if_missing "$REPO_DIR/config/astinuslab.env.example" "$INSTALL_DIR/config/astinuslab.env" || return 1
  install_config_if_missing "$REPO_DIR/config/catalog/targets.example.yaml" "$INSTALL_DIR/config/catalog/targets.yaml" || return 1
  install_config_if_missing "$REPO_DIR/config/catalog/workloads.example.yaml" "$INSTALL_DIR/config/catalog/workloads.yaml" || return 1
  install_config_if_missing "$REPO_DIR/config/catalog/scenarios.example.yaml" "$INSTALL_DIR/config/catalog/scenarios.yaml" || return 1
  install_config_if_missing "$REPO_DIR/config/catalog/attackers.example.yaml" "$INSTALL_DIR/config/catalog/attackers.yaml" || return 1
  install_config_if_missing "$REPO_DIR/config/catalog/profiles.example.yaml" "$INSTALL_DIR/config/catalog/profiles.yaml" || return 1
  install_config_if_missing "$REPO_DIR/config/catalog/network_nodes.example.yaml" "$INSTALL_DIR/config/catalog/network_nodes.yaml" || return 1
  secure_managed_file astinuslab.toml "$config_owner" astinuslab 0640 || return 1
  secure_tree_permissions "$INSTALL_DIR/config" "$config_owner" astinuslab 0750 0640 || return 1
}

# Reconcile permissions on upgrades as well as clean installs. chown alone is
# insufficient: inherited group write, setgid, and named ACL entries survive.
reconcile_install_permissions() {
  local path immutable_only="${1:-false}"
  # The release directory itself comes first: `rsync -a` applies the source
  # directory's ownership to the destination, so a release staged from an
  # operator's checkout lands owned by that operator, and directory write is
  # what lets an identity replace the root-owned files inside it.
  local readonly_paths=(
    "$RELEASE_DIR"
    "$RELEASE_DIR/src" "$RELEASE_DIR/ops" "$RELEASE_DIR/tools"
    "$RELEASE_DIR/deploy" "$RELEASE_DIR/guest" "$RELEASE_DIR/.venv"
  )
  local readonly_files=("$RELEASE_DIR/pyproject.toml" "$RELEASE_DIR/requirements"*.lock)

  for path in "${readonly_paths[@]}" "${readonly_files[@]}"; do
    [[ -e "$path" ]] || continue
    setfacl -bR -- "$path"
    # Private source extractions still need service read/traverse access once
    # root owns the release; X preserves which regular files are executable.
    chmod -R a+rX,go-w -- "$path"
    chown -R root:root -- "$path"
    if [[ -d "$path" ]]; then
      find "$path" -type d -exec chmod g-s {} +
    fi
  done

  # An already-split install has distinct API/runner/analysis custody. Source
  # staging may normalize only immutable release content; the exact mutable
  # matrix is reconciled later by configure_split_permissions, after every
  # service has been quiesced.
  if $SPLIT_USERS || [[ "$immutable_only" == true ]]; then return 0; fi

  # Only these paths are runtime-owned. storage ACL traversal entries are
  # managed separately; never broaden this list to the install root.
  for path in "$INSTALL_DIR/storage" "$INSTALL_DIR/config" \
      "$INSTALL_DIR/images/definitions" "$INSTALL_DIR/vol3/symbols"; do
    [[ -d "$path" ]] || continue
    chown -R "$SERVICE_USER:astinuslab" -- "$path"
  done
}

# Own the credential file to the service identity, not to root. The atomic
# rewrite below replaces the inode, so it carries the temporary file's root
# ownership and discards the chown applied earlier in install_runtime_config_defaults.
# astinuslab.toml is read at import by every service (src/astinuslab/core/config.py:15),
# so a root-owned 0600 file means no service can start after a clean install.
secure_admin_config() {
  local path="$1"
  local config_owner="$SERVICE_USER" config_mode=0600
  if $SPLIT_USERS; then
    config_owner="$API_USER"
    config_mode=0640
  fi
  [[ "$path" == "$INSTALL_DIR/astinuslab.toml" ]] || return 1
  secure_managed_file astinuslab.toml "$config_owner" astinuslab "$config_mode"
}

# The systemd units source config/astinuslab.env, and environment overrides the
# TOML. Shipping ASTINUSLAB_AUTH_ENABLED in that file gave one setting two
# sources of truth, and the launcher env - seeded from the development example,
# where no-auth on loopback is the documented default - silently won. An
# installed release then served unauthenticated while astinuslab.toml said
# enabled = true and the installer had just printed a token for it.
#
# astinuslab.toml is the source of truth for auth. Comment out any auth keys in
# the launcher env so it cannot contradict it, leaving them visible with a
# pointer rather than deleting an operator's line.
neutralise_env_auth_overrides() {
  local env_file="$INSTALL_DIR/config/astinuslab.env"
  [[ -f "$env_file" ]] || return 0
  python3 -B "$SCRIPT_DIR/managed_config.py" neutralise-env-auth "$env_file"
}

configure_admin_auth() {
  local config_path="$INSTALL_DIR/astinuslab.toml"
  local token="" token_hash="$ADMIN_TOKEN_HASH"
  if [[ "${AUTH_ALLOW_UNAUTHENTICATED_LOCAL:-false}" == true ]]; then
    ALLOW_UNAUTHENTICATED_LOCAL=true
  fi
  mkdir -p "$INSTALL_DIR"

  if [[ -e "$config_path" || -L "$config_path" ]]; then
    secure_admin_config "$config_path" || return 1
  fi

  if [[ -f "$config_path" ]] && grep -Eq '^[[:space:]]*admin_api_token_hash[[:space:]]*=[[:space:]]*"sha256:[0-9a-fA-F]{64}"' "$config_path"; then
    if $GENERATE_ADMIN_TOKEN || $ALLOW_UNAUTHENTICATED_LOCAL || [[ -n "$ADMIN_TOKEN_HASH" ]]; then
      die "existing admin token hash conflicts with requested auth mode; refusing rotation"
    fi
    secure_admin_config "$config_path" || return 1
    neutralise_env_auth_overrides || return 1
    return 0
  fi

  if $ALLOW_UNAUTHENTICATED_LOCAL; then
    token_hash=""
  else
    if [[ -z "$token_hash" ]]; then
      token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
      token_hash="sha256:$(printf %s "$token" | sha256sum | cut -d' ' -f1)"
    fi
  fi

  AUTH_CONFIG_PATH="$config_path" AUTH_TOKEN_HASH="$token_hash" AUTH_ENABLED="$([[ $ALLOW_UNAUTHENTICATED_LOCAL == true ]] && echo false || echo true)" \
    python3 -B "$SCRIPT_DIR/managed_config.py" configure-admin-auth
  secure_admin_config "$config_path" || return 1
  neutralise_env_auth_overrides || return 1

  if [[ -n "$token" ]]; then
    GENERATED_ADMIN_TOKEN="$token"
    printf 'Admin token: %s\n' "$token"
    unset token
  fi
}

remove_legacy_flat_source() {
  # A pre-release-layout install kept source beside its mutable paths. Once the
  # versioned release is live those copies are dead code, and leaving a second
  # application tree on disk is exactly what a review flags. Only entries the
  # live release itself ships are removed, so mutable state and anything the
  # operator put at the root are left alone.
  [[ -n "$RELEASE_DIR" && -d "$RELEASE_DIR" ]] || return 0
  local name victim
  while IFS= read -r name; do
    case "$name" in
      storage|config|images|vol3|astinuslab.toml|releases|current|"") continue ;;
    esac
    victim="$INSTALL_DIR/$name"
    [[ -e "$victim" || -L "$victim" ]] || continue
    rm -rf -- "$victim" || return 1
  done < <(cd "$RELEASE_DIR" && ls -A)
  # Never shipped by rsync but always left by a flat install: the old
  # virtualenv and the editable-install metadata beside it.
  rm -rf -- "$INSTALL_DIR/.venv" "$INSTALL_DIR/astinuslab.egg-info" || return 1
}

cleanup_superseded_legacy_install() {
  # Deletion is intentionally post-commit. Flat installs have no previous
  # versioned release whose bytes could satisfy rollback, and the whole-tree
  # ACL snapshot requires every captured inode to remain present until that
  # rollback boundary has been discarded.
  remove_legacy_flat_source || return 1
  remove_legacy_vol3_bin_files || return 1
}

resolve_release_dir() {
  # The live release is whatever `current` points at. Used by flows such as the
  # split migration that operate on an already installed candidate.
  [[ -z "$RELEASE_DIR" ]] || return 0
  local link="$INSTALL_DIR/current"
  [[ -L "$link" ]] || return 0
  RELEASE_DIR="$(readlink -f -- "$link")" || return 1
  RELEASE_ID="$(basename -- "$RELEASE_DIR")"
}

release_id_for() {
  # Content addressing keeps a rerun of the same source idempotent: it resolves
  # to the release that is already installed instead of staging a second copy.
  local source="$1" version digest
  version="$(sed -n 's/^version = "\(.*\)"$/\1/p' "$source/pyproject.toml" | head -1)"
  [[ -n "$version" ]] || return 1
  digest="$(cd "$source" && find . -type f -print0 | LC_ALL=C sort -z \
    | xargs -0 sha256sum 2>/dev/null | sha256sum | cut -c1-12)"
  [[ -n "$digest" ]] || return 1
  printf '%s-%s\n' "$version" "$digest"
}

switch_current_release() {
  # Rename over the existing link so a running service never resolves through a
  # moment where `current` is absent.
  local target="$1" link="$INSTALL_DIR/current" staging="$INSTALL_DIR/current.tmp"
  [[ -d "$target" ]] || return 1
  rm -f -- "$staging" || return 1
  ln -s -- "$target" "$staging" || return 1
  mv -T -- "$staging" "$link" || { rm -f -- "$staging"; return 1; }
}

stage_release() {
  local staging
  install -d -m 0755 "$INSTALL_DIR/releases" || return 1
  # Stage first, then address what was staged. rsync filters build artefacts
  # such as __pycache__ out of the release, so hashing the checkout instead
  # would give two different ids to byte-identical releases.
  staging="$(mktemp -d "$INSTALL_DIR/releases/.staging.XXXXXXXX")" || return 1
  sync_source_tree "$staging" || { rm -rf -- "$staging"; return 1; }
  chmod 0755 "$staging" || { rm -rf -- "$staging"; return 1; }
  RELEASE_ID="$(release_id_for "$staging")" || { rm -rf -- "$staging"; return 1; }
  RELEASE_DIR="$INSTALL_DIR/releases/$RELEASE_ID"
  if [[ -d "$RELEASE_DIR" ]]; then
    rm -rf -- "$staging"
    info "Release already staged: $RELEASE_ID"
  else
    mv -T -- "$staging" "$RELEASE_DIR" || { rm -rf -- "$staging"; return 1; }
    ok "Release staged: $RELEASE_ID"
  fi
  PREVIOUS_RELEASE_DIR=""
  [[ ! -L "$INSTALL_DIR/current" ]] || PREVIOUS_RELEASE_DIR="$(readlink -f -- "$INSTALL_DIR/current")"
  if [[ ! -x "$RELEASE_DIR/.venv/bin/python" ]]; then
    if [[ -n "$PREVIOUS_RELEASE_DIR" && "$RELEASE_DIR" == "$PREVIOUS_RELEASE_DIR" ]]; then
      warn "The current release has no usable virtualenv; refusing to rebuild it while services may be running"
      return 1
    fi
    create_venv || return 1
  fi
  # Staging copies bytes; it does not redirect anything. `current` moves inside
  # switch_release_and_restart, while every service is stopped.
}

install_source() {
  local immutable_only="${1:-false}"
  info "Installing to $INSTALL_DIR..."
  stage_release || { warn "Could not stage the release"; return 1; }
  if [[ "$immutable_only" == true ]]; then
    reconcile_install_permissions true || return 1
    ok "Immutable release staged"
    return 0
  fi
  reconcile_install_permissions || return 1
  install_runtime_config_defaults || return 1
  configure_admin_auth || return 1
  # Source and virtualenv remain installer-owned.  Only explicitly mutable
  # paths receive runtime ownership in install_runtime_config_defaults and
  # prepare_storage below.
  local storage_owner="$SERVICE_USER"
  $SPLIT_USERS && storage_owner="$API_USER"
  prepare_managed_directory storage "$storage_owner" astinuslab 2770 || return 1
  secure_tree_permissions "$INSTALL_DIR/storage/ssh_keys" "$storage_owner" astinuslab 0700 0600 || return 1
  ok "Source installed"
}

prepare_single_upgrade_mutable_state() {
  # Existing writers are quiesced before this runs. Reconcile legacy layout,
  # defaults, credentials, and runtime custody without racing the prior release.
  reconcile_install_permissions || return 1
  install_runtime_config_defaults || return 1
  configure_admin_auth || return 1
}

install_split_upgrade_source() {
  info "Staging split-identity update in $INSTALL_DIR..."
  stage_release || { warn "Could not stage the release"; return 1; }
  # SPLIT_USERS makes this immutable-only. Do not touch config, credentials,
  # evidence, image definitions, symbols, or any storage tree while the old
  # split services are still running.
  reconcile_install_permissions || return 1
  ok "Split update source staged"
}

prepare_split_upgrade_mutable_state() {
  # This function is called only after quiesce_services. It may now create
  # missing defaults and normalize the exact per-service ownership/mode/ACL
  # matrix without racing live API, runner, or analysis writers.
  install_runtime_config_defaults || return 1
  configure_admin_auth || return 1
  configure_split_permissions || return 1
  remove_legacy_evidence_acls || return 1
  split_database_layout prepare || return 1
}

create_venv() {
  # One virtualenv per release, inside the release, so a switch never mutates
  # the environment a running service is executing from.
  local root="${RELEASE_DIR:-$INSTALL_DIR}"
  info "Creating Python venv..."
  python3 -m venv "$root/.venv" || return 1
  "$root/.venv/bin/pip" install --quiet --upgrade pip || return 1
  if [[ -f "$root/requirements.lock" ]]; then
    "$root/.venv/bin/pip" install --quiet -r "$root/requirements.lock" || return 1
    "$root/.venv/bin/pip" install --quiet -e "$root" --no-deps || return 1
  else
    "$root/.venv/bin/pip" install --quiet -e "$root" || return 1
  fi
  ok "Venv created"
}

prepare_storage() {
  info "Creating storage directories..."
  local dir storage_owner="$SERVICE_USER"
  $SPLIT_USERS && storage_owner="$API_USER"
  for dir in storage storage/dumps storage/logs storage/ssh_keys storage/tmp storage/workload-cache \
      images/definitions vol3/symbols; do
    prepare_managed_directory "$dir" "$storage_owner" astinuslab 2770 || return 1
  done
  if ! $SPLIT_USERS; then
    # Key selection is consumed by the service through pathnames. Do not leave
    # this tree writable by the shared group after the generic storage setup.
    secure_tree_permissions "$INSTALL_DIR/storage/ssh_keys" \
      "$SERVICE_USER" astinuslab 0700 0600 || return 1
  fi
  if ! $SPLIT_USERS; then
    API_USER="$SERVICE_USER"
  fi
  secure_libvirt_image_access || return 1
  prepare_shared_databases "$INSTALL_DIR/storage" || return 1
  ok "Storage directories ready"
}

prepare_managed_directory() {
  local relative="$1" owner="$2" group="$3" mode="$4" root="${5:-$INSTALL_DIR}"
  python3 - "$root" "$relative" "$owner" "$group" "$mode" "$MIGRATION_CREATED_DIRS" <<'PY'
import grp
import os
import pwd
import stat
import sys

root, relative, owner_name, group_name, mode_text, record = sys.argv[1:]
segments = relative.split("/")
created: list[str] = []
if any(segment in ("", ".", "..") for segment in segments):
    raise SystemExit("unsafe managed-directory path")
owner = pwd.getpwnam(owner_name).pw_uid
group = grp.getgrnam(group_name).gr_gid
mode = int(mode_text, 8)
flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
descriptor = os.open(root, flags)
try:
    device = os.fstat(descriptor).st_dev
    for index, segment in enumerate(segments):
        try:
            child = os.open(segment, flags, dir_fd=descriptor)
        except FileNotFoundError:
            os.mkdir(segment, 0o755, dir_fd=descriptor)
            created.append(os.path.join(root, *segments[: index + 1]))
            child = os.open(segment, flags, dir_fd=descriptor)
        os.close(descriptor)
        descriptor = child
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode) or info.st_dev != device:
            raise SystemExit("managed directory is unsafe or crosses a filesystem boundary")
        if index == len(segments) - 1:
            os.fchown(descriptor, owner, group)
            os.fchmod(descriptor, mode)
finally:
    os.close(descriptor)
# A failed split migration must leave no path behind that is owned by an
# identity the rollback then deletes, so every directory this run creates is
# recorded for migration_rollback to undo.
if record and created:
    with open(record, "a", encoding="utf-8") as handle:
        handle.write("".join(f"{path}\n" for path in created))
PY
}

secure_managed_file() {
  local relative="$1" owner="$2" group="$3" mode="$4"
  python3 "$SCRIPT_DIR/permissions.py" managed-file \
    "$INSTALL_DIR" "$relative" "$owner" "$group" "$mode"
}

prepare_shared_databases() {
  local storage="${1:-$INSTALL_DIR/storage}"
  python3 - "$storage" "$API_USER" astinuslab <<'PY'
import errno
import grp
import os
import pwd
import stat
import sys

storage, owner_name, group_name = sys.argv[1:]
owner = pwd.getpwnam(owner_name).pw_uid
group = grp.getgrnam(group_name).gr_gid
directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
file_flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
directory_fd = os.open(storage, directory_flags)
try:
    directory_info = os.fstat(directory_fd)
    if not stat.S_ISDIR(directory_info.st_mode):
        raise SystemExit("storage root is not a directory")
    os.fchown(directory_fd, owner, group)
    # Preserve package-root sticky custody across later database reconciliation.
    os.fchmod(directory_fd, 0o2770 | (directory_info.st_mode & stat.S_ISVTX))
    for database in ("astinuslab.db", "celery_broker.db", "celery_results.db"):
        for suffix in ("", "-wal", "-shm", "-journal"):
            name = database + suffix
            flags = file_flags
            if not suffix:
                flags |= os.O_CREAT
            try:
                fd = os.open(name, flags, 0o660, dir_fd=directory_fd)
            except FileNotFoundError:
                continue
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise SystemExit(f"refusing symlinked database path: {name}") from exc
                raise
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise SystemExit(f"refusing non-regular or multiply-linked database path: {name}")
                os.fchown(fd, owner, group)
                os.fchmod(fd, 0o660)
            finally:
                os.close(fd)
finally:
    os.close(directory_fd)
PY
}

quiesce_services() {
  # The schema is only ever migrated with no writer attached. Stopping is a
  # no-op on a clean install, where nothing is running yet.
  local unit state pid
  for unit in \
    "astinuslab-api@${SERVICE_USER}.service" \
    "astinuslab-runner@${SERVICE_USER}.service" \
    "astinuslab-analysis@${SERVICE_USER}.service" \
    astinuslab-api.service \
    astinuslab-runner.service \
    astinuslab-analysis.service; do
    state="$(systemctl show "$unit" -p ActiveState --value 2>/dev/null)" || return 1
    if [[ "$state" != inactive && "$state" != failed ]]; then
      systemctl stop "$unit" || return 1
    fi
    state="$(systemctl show "$unit" -p ActiveState --value 2>/dev/null)" || return 1
    pid="$(systemctl show "$unit" -p MainPID --value 2>/dev/null)" || return 1
    [[ "$state" == inactive || "$state" == failed ]] || return 1
    [[ "$pid" == 0 ]] || return 1
  done
}

# The same non-shell evaluator supplies every DB operation and readiness.
# Keep the evaluator in the trusted installer source so rollback to an older
# release does not require that release to contain this new helper. Identity
# probes receive it on stdin opened by the installer: split service users need
# not traverse the operator's private source checkout.
run_installed_database() {
  local db_user="$SERVICE_USER"
  $SPLIT_USERS && db_user="$API_USER"
  ( cd "$RELEASE_DIR" || exit 1
    env -i PATH=/usr/bin:/bin /usr/bin/python3 -I "$SCRIPT_DIR/runtime_environment.py" \
      --data-dir "$INSTALL_DIR" --settings-only --user "$db_user" -- \
      "$RELEASE_DIR/.venv/bin/python" -s -c \
      'import sys; from astinuslab.core.config import settings; print("effective_database=" + str(settings.db_path), file=sys.stderr); from astinuslab.db import main; raise SystemExit(main(sys.argv[1:]))' "$@"
  )
}

backup_database_before_migration() {
  # Beside the storage tree, never inside it: storage is what evidence bundles
  # are cut from and it holds the SSH key directory, and the backup of the
  # custody record belongs in neither.
  local directory="$INSTALL_DIR/backups" destination line
  install -d -o root -g root -m 0700 "$directory" || return 1
  destination="$directory/pre-migration-$(date -u +%Y%m%dT%H%M%SZ).db"
  # Import from the release and evaluate the installed environment before
  # settings load. Running from the checkout or inheriting shell overrides
  # could otherwise protect a different database from the service's database.
  line="$(run_installed_database backup "$destination" | grep '^backup: ')" || {
    warn "Could not back up the application database"
    return 1
  }
  DB_BACKUP="$destination"
  DB_BACKUP_SHA256="$(sed -n 's/.*sha256=\([^ ]*\).*/\1/p' <<<"$line")"
  DB_BACKUP_REVISION="$(sed -n 's/.*revision=\([^ ]*\).*/\1/p' <<<"$line")"
  [[ -n "$DB_BACKUP" && -n "$DB_BACKUP_SHA256" ]] || {
    warn "Database backup did not report a path and hash"
    return 1
  }
  ok "Database backup: $DB_BACKUP sha256=$DB_BACKUP_SHA256 mode=0600 revision=${DB_BACKUP_REVISION:-<unstamped>}"
}

restore_database_from_backup() {
  [[ -n "$DB_BACKUP" ]] || return 1
  run_installed_database restore "$DB_BACKUP"
}

switch_release_and_restart() {
  # One ordered sequence, taken by every install: nothing is running while
  # `current` moves, the unit definitions are reloaded before anything starts
  # again, and every mandatory unit is restarted rather than started.
  quiesce_services || return 1
  switch_current_release "$RELEASE_DIR" || return 1
  systemctl daemon-reload || return 1
  control_services || return 1
}

release_rollback() {
  # Stop every possible writer before touching the schema. A failed candidate
  # start/readiness check can leave one or more units running, and restoring the
  # SQLite backup underneath them would corrupt the rollback boundary.
  local reason="$1" restart_services="${2:-true}" failed=false database_restore_failed=false
  warn "$reason; restoring the previous database and release"
  quiesce_services || {
    warn "Could not stop every AstinusLab writer; refusing to restore the database"
    return 1
  }
  if [[ -n "$DB_BACKUP" ]]; then
    restore_database_from_backup || {
      warn "Could not restore the database from $DB_BACKUP; restore it by hand before starting the services"
      failed=true
      database_restore_failed=true
    }
  fi
  if [[ -n "$PREVIOUS_RELEASE_DIR" && -d "$PREVIOUS_RELEASE_DIR" ]]; then
    switch_current_release "$PREVIOUS_RELEASE_DIR" || failed=true
  fi
  # A previous release cannot safely run against a database that may still be
  # partially migrated. Leave every writer quiesced even when the release link
  # itself was restored successfully; the operator must restore the named
  # backup before starting services.
  $database_restore_failed && return 1
  if [[ "$restart_services" == true ]]; then
    RESTART_SERVICES=true
    control_services || failed=true
  fi
  # systemctl returns once it has started the units, which is before the
  # restored release is serving. Reporting a recovery there would hand back a
  # host that is still down - or never comes back - and call it restored.
  local restored_dir="$RELEASE_DIR"
  [[ -z "$PREVIOUS_RELEASE_DIR" || ! -d "$PREVIOUS_RELEASE_DIR" ]] || restored_dir="$PREVIOUS_RELEASE_DIR"
  if [[ "$restart_services" == true ]] && ! verify_release_readiness "$restored_dir"; then
    warn "The restored release at $restored_dir is not serving; check the service logs before using this host"
    failed=true
  fi
  $failed && return 1
  return 0
}

apply_db_migrations() {
  info "Applying database migrations..."
  local db_user="$SERVICE_USER"
  $SPLIT_USERS && db_user="$API_USER"
  ( cd "$RELEASE_DIR" || exit 1
    # The installer intentionally opens its private source before changing uid.
    # shellcheck disable=SC2024
    sudo -u "$db_user" env -i PATH=/usr/bin:/bin \
      /usr/bin/python3 -I - --data-dir "$INSTALL_DIR" -- \
      "$RELEASE_DIR/.venv/bin/python" -m astinuslab.dbcli init < "$SCRIPT_DIR/runtime_environment.py"
  ) || {
    warn "Database migration failed"
    return 1
  }
  ok "Database migrations applied"
}

inspect_or_generate_ssh_key() {
  local generate="$1" key_owner="$SERVICE_USER" key_directory_mode=0700
  $SPLIT_USERS && key_owner="$API_USER"
  $SPLIT_USERS && key_directory_mode=2700
  python3 "$SCRIPT_DIR/ssh_key.py" \
    "$INSTALL_DIR" "$key_owner" astinuslab "$generate" "$key_directory_mode"
}

handle_ssh_key() {
  info "SSH key setup..."
  local result status key_name
  result="$(inspect_or_generate_ssh_key false)" || return 1
  IFS='|' read -r status key_name <<< "$result"
  if [[ "$status" == existing ]]; then
    if $SPLIT_USERS; then
      secure_ssh_key_permissions "$INSTALL_DIR/storage/ssh_keys" || return 1
    else
      secure_tree_permissions "$INSTALL_DIR/storage/ssh_keys" \
        "$SERVICE_USER" astinuslab 0700 0600 || return 1
    fi
    ok "Active SSH key already configured: $key_name"
    return 0
  fi
  [[ "$status" == missing ]] || return 1
  if $NO_SSH_KEY; then
    warn "No SSH key configured. Set one up on the Security page in the UI before launching jobs."
    return 0
  fi
  local should_generate=false
  if $YES || $GENERATE_SSH_KEY; then
    should_generate=true
  else
    local yn
    read -rp "[astinuslab] Generate an ED25519 SSH key for AstinusLab? [Y/n] " yn
    [[ "${yn,,}" != "n" ]] && should_generate=true
  fi
  if $should_generate; then
    result="$(inspect_or_generate_ssh_key true)" || return 1
    IFS='|' read -r status key_name <<< "$result"
    [[ "$status" == generated || "$status" == existing ]] || return 1
    if $SPLIT_USERS; then
      secure_ssh_key_permissions "$INSTALL_DIR/storage/ssh_keys" || return 1
    else
      secure_tree_permissions "$INSTALL_DIR/storage/ssh_keys" \
        "$SERVICE_USER" astinuslab 0700 0600 || return 1
    fi
    ok "Key generated and activated: $INSTALL_DIR/storage/ssh_keys/$key_name"
  else
    warn "No SSH key configured. Set one up on the Security page in the UI before launching jobs."
  fi
}

render_template() {
  local src="$1" dst="$2" install_dir="$3"
  local storage_root="${4:-$install_dir/storage}"
  local service_user="${5:-%i}" service_group="${6:-%i}"
  local api_unit="${7:-astinuslab-api@%i.service}"
  local service_name="${8:-}" split_mode="${9:-false}"
  python3 - "$src" "$dst" "$install_dir" "$storage_root" "$service_user" "$service_group" "$api_unit" "$service_name" "$split_mode" <<'PY'
from pathlib import Path
import sys
src, dst, install_dir, storage_root, service_user, service_group, api_unit, service_name, split_mode = sys.argv[1:]
text = Path(src).read_text(encoding="utf-8")
text = text.replace("/opt/astinuslab", install_dir).replace("@@INSTALL_DIR@@", install_dir)
text = text.replace("@@CURRENT_DIR@@", f"{install_dir}/current")
text = text.replace("@@STORAGE_ROOT@@", storage_root)
default_storage = f"{install_dir}/storage"
if storage_root != default_storage:
    text = text.replace(
        f"ReadWritePaths={storage_root} ",
        f"ReadWritePaths={storage_root} {default_storage} ",
    )
if install_dir.startswith("/home/"):
    # With the install root itself under /home, tmpfs would hide the source, the
    # venv, config, images and the symbol cache at once, and systemd fails the
    # namespace on the first of them. read-only leaves the tree reachable and the
    # writable exceptions stay the ReadWritePaths the unit already declares.
    protect_home = "ProtectHome=read-only\n"
elif storage_root.startswith("/home/"):
    # ProtectHome=tmpfs covers /home with an empty filesystem, and systemd cannot
    # bind a ReadWritePaths= entry that the tmpfs hides: the unit fails outright
    # with 226/NAMESPACE. BindPaths= mounts the real directory over the tmpfs and
    # stays writable under ProtectSystem=strict, so every other home stays hidden
    # and the storage root is still reachable.
    protect_home = (
        "ProtectHome=tmpfs\n"
        "# ProtectHome=tmpfs permits a custom storage root under /home via BindPaths.\n"
        f"BindPaths={storage_root}\n"
    )
    text = text.replace(f"ReadWritePaths={storage_root} ", "ReadWritePaths=")
else:
    protect_home = "ProtectHome=read-only\n"
text = text.replace("@@PROTECT_HOME@@", protect_home)
text = text.replace("User=%i", f"User={service_user}").replace("Group=%i", f"Group={service_group}")
text = text.replace("astinuslab-api@%i.service", api_unit)
if split_mode == "true" and service_name == "astinuslab-analysis":
    text = text.replace("After=network.target libvirtd.service " + api_unit, "After=network.target " + api_unit)
    text = text.replace("Wants=libvirtd.service\n", "")
    text = text.replace(
        "# Privilege hardening — mirrors astinuslab-runner.service.\n"
        "# NoNewPrivileges is intentionally NOT set: workers invoke privileged helpers\n"
        "# via `sudo -n` (/etc/sudoers.d/astinuslab-runner); NoNewPrivileges blocks sudo.\n",
        "# The split analysis identity has no sudo, libvirt, or KVM access.\n"
        "NoNewPrivileges=true\nPrivateDevices=true\n",
    )
Path(dst).write_text(text, encoding="utf-8")
PY
}

# An operator who registered a custom storage root through
# ops/deploy/configure-privileged-roots.sh must not have it silently reverted by
# the next upgrade: re-rendering policy and units from $INSTALL_DIR/storage
# would leave every capture under the real root failing an ACL policy
# mismatch. Prefer an explicit --storage-root, then whatever the installed
# policy already trusts, and only then the default.
resolve_storage_root() {
  [[ -z "$STORAGE_ROOT" ]] || return 0
  if [[ -r "$POLICY_FILE" ]]; then
    command -v python3 >/dev/null 2>&1 || {
      warn "python3 is required to read the existing privileged storage policy"
      return 1
    }
    STORAGE_ROOT="$(python3 - "$POLICY_FILE" <<'PY'
import json
import sys
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        roots = json.load(handle).get("allowed_roots") or []
    print(roots[0] if roots else "")
except Exception:
    print("")
PY
)" || true
  fi
  [[ -n "$STORAGE_ROOT" && -d "$STORAGE_ROOT" ]] || STORAGE_ROOT="$INSTALL_DIR/storage"
  info "Storage root: $STORAGE_ROOT"
}

configured_storage_root() {
  # Where the application will actually write: the units read
  # config/astinuslab.env through EnvironmentFile and environment overrides the
  # TOML, so an uncommented ASTINUSLAB_STORAGE_DIR there wins over [storage].
  local config="$INSTALL_DIR/astinuslab.toml" env_file="$INSTALL_DIR/config/astinuslab.env"
  python3 - "$config" "$env_file" "$INSTALL_DIR" <<'PY'
import os
import re
import stat
import sys
import tomllib
from pathlib import Path

config, env_file, install_dir = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])


def read_optional_regular(path: Path) -> str | None:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SystemExit(f"cannot safely open {path}: {exc}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise SystemExit(f"configuration path is not a regular file: {path}")
        with os.fdopen(os.dup(descriptor), encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise SystemExit(f"cannot read {path}: {exc}") from exc
    finally:
        os.close(descriptor)


value = ""
config_text = read_optional_regular(config)
env_text = read_optional_regular(env_file)
if config_text is not None:
    try:
        data = tomllib.loads(config_text)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"cannot read or parse {config}: {exc}") from exc
    section = data.get("storage") or {}
    if not isinstance(section, dict):
        raise SystemExit(f"invalid [storage] table in {config}")
    configured = section.get("storage_dir") or ""
    if not isinstance(configured, str):
        raise SystemExit(f"storage.storage_dir must be a string in {config}")
    value = configured
if env_text is not None:
    for line in env_text.splitlines():
        match = re.match(r"\s*ASTINUSLAB_STORAGE_DIR\s*=\s*(.+?)\s*$", line)
        if match:
            value = match.group(1).strip("\"'")
if not value:
    raise SystemExit(0)
path = Path(value.replace("{base_dir}", str(install_dir)))
print(path if path.is_absolute() else install_dir / path)
PY
}

verify_storage_root_matches_config() {
  # Deep readiness compares the application's configured storage against the
  # installed ACL policy and fails the switch when the policy does not cover it.
  # By then the services are stopped and the release is half-moved, so a root
  # that was registered but never configured took the whole host down. Refuse
  # here instead, before anything moves, and name both values.
  local config="$INSTALL_DIR/astinuslab.toml" env_file="$INSTALL_DIR/config/astinuslab.env"
  if ! command -v python3 >/dev/null 2>&1; then
    if [[ -e "$config" || -L "$config" || -e "$env_file" || -L "$env_file" ]]; then
      warn "python3 is required to validate existing storage configuration before dependency installation"
      return 1
    fi
    return 0
  fi
  local configured
  configured="$(configured_storage_root)" || {
    warn "could not determine configured storage root; refusing to change the host"
    return 1
  }
  [[ -n "$configured" ]] || return 0
  local resolved
  resolved="$(realpath -m -- "$STORAGE_ROOT")"
  [[ "$(realpath -m -- "$configured")" != "$resolved" ]] || return 0
  die "storage root disagreement: the privileged root is $resolved but the application is configured for $configured.
       Evidence would be written under one root while the ACL policy, systemd units and sudoers rule name the other,
       and deep readiness refuses that. Point them at one root: set [storage] storage_dir (or ASTINUSLAB_STORAGE_DIR in
       config/astinuslab.env) to $resolved, or register the configured root with
       ops/deploy/configure-privileged-roots.sh --install-dir $INSTALL_DIR --storage-root $configured."
}

validate_install_storage_root() {
  if ! command -v python3 >/dev/null 2>&1; then
    local normalized_root normalized_default
    normalized_root="$(realpath -m -- "$STORAGE_ROOT")" || return 1
    normalized_default="$(realpath -m -- "$INSTALL_DIR/storage")" || return 1
    [[ "$normalized_root" == "$normalized_default" ]] || {
      warn "python3 is required to validate a custom storage root before dependency installation"
      return 1
    }
    return 0
  fi
  PYTHONPATH="$REPO_DIR/ops/host${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -B - "$STORAGE_ROOT" "$INSTALL_DIR" <<'PY'
import sys

from acl_policy import validate_storage_root_for_install

try:
    validate_storage_root_for_install(sys.argv[1], sys.argv[2])
except ValueError as exc:
    raise SystemExit(str(exc))
PY
}

bind_storage_root_identity() {
  validate_install_storage_root || return 1
  STORAGE_ROOT_IDENTITY="$(python3 - "$STORAGE_ROOT" <<'PY'
import os
import stat
import sys

info = os.lstat(sys.argv[1])
if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
    raise SystemExit("storage root must remain a non-symlink directory")
print(f"{info.st_dev}:{info.st_ino}")
PY
)" || return 1
  [[ -n "$STORAGE_ROOT_IDENTITY" ]]
}

assert_storage_root_identity() {
  [[ -n "$STORAGE_ROOT_IDENTITY" ]] || {
    warn "Storage-root identity was not bound"
    return 1
  }
  validate_install_storage_root || return 1
  python3 - "$STORAGE_ROOT" "$STORAGE_ROOT_IDENTITY" <<'PY'
import os
import stat
import sys

path, expected = sys.argv[1:]
try:
    info = os.lstat(path)
except OSError as exc:
    raise SystemExit(f"storage root disappeared after validation: {exc}") from exc
if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
    raise SystemExit("storage root changed type after validation")
actual = f"{info.st_dev}:{info.st_ino}"
if actual != expected:
    raise SystemExit("storage root inode changed after validation")
PY
}

record_host_access_transaction() {
  [[ -n "$HOST_ACCESS_TRANSACTION_LOG" ]] || return 0
  local kind="$1" target="$2" value="$3" extra_name="${4:-}" extra_value="${5:-}"
  python3 "$SCRIPT_DIR/host_access.py" record \
    "$HOST_ACCESS_TRANSACTION_LOG" "$kind" "$target" "$value" "$extra_name" "$extra_value"
}

install_sudoers_rule() {
  info "Installing sudoers rule..."
  "$RELEASE_DIR/ops/host/install_sudoers_astinuslab.sh" \
    --install-dir "$INSTALL_DIR" \
    --run-user "$SERVICE_USER" \
    --storage-root "$STORAGE_ROOT" \
    --helper-source-dir "$RELEASE_DIR/ops/host" || return "$?"
  ok "Sudoers rule installed: /etc/sudoers.d/astinuslab-runner"
}

grant_qemu_traversal() {
  # The walk to / crosses directories this product does not own, an operator's
  # home among them. Every entry it actually adds is recorded so uninstall can
  # take back exactly that and nothing else; a component that already carried
  # the entry was never ours to grant, and is left out of the manifest.
  local storage_path="$1" stop_at="${2:-/}" qemu_user="libvirt-qemu"
  if ! getent passwd libvirt-qemu >/dev/null 2>&1 && getent passwd qemu >/dev/null 2>&1; then
    qemu_user="qemu"
  fi
  getent passwd "$qemu_user" >/dev/null 2>&1 || {
    warn "No libvirt qemu identity exists; refusing to create an unverifiable traversal grant"
    return 1
  }
  if ! command -v setfacl >/dev/null 2>&1 || ! command -v getfacl >/dev/null 2>&1; then
    warn "POSIX ACL tools are required for reversible qemu traversal grants"
    return 1
  fi
  if [[ "$stop_at" != "/" && $EUID -eq 0 ]]; then
    warn "A traversal stop may only be supplied by an unprivileged unit-test harness"
    return 1
  fi
  info "Ensuring $qemu_user can traverse to $storage_path..."
  local manifest_parent
  manifest_parent="$(dirname "$TRAVERSAL_ACL_MANIFEST")"
  if [[ $EUID -eq 0 ]]; then
    install -d -o root -g root -m 0755 "$manifest_parent" 2>/dev/null
  else
    install -d -m 0755 "$manifest_parent" 2>/dev/null
  fi || {
    warn "Could not create the traversal manifest directory"
    return 1
  }
  python3 "$SCRIPT_DIR/host_access.py" grant "$storage_path" "$stop_at" "$qemu_user" \
    "$TRAVERSAL_ACL_MANIFEST" "$HOST_ACCESS_TRANSACTION_LOG"
}

record_mac_change() {
  local kind="$1" target="$2" value="$3" selinux_type="${4:-}" manifest_parent
  manifest_parent="$(dirname "$MAC_POLICY_MANIFEST")"
  if [[ $EUID -eq 0 ]]; then
    install -d -o root -g root -m 0755 "$manifest_parent" || return 1
  else
    install -d -m 0755 "$manifest_parent" || return 1
  fi
  python3 - "$MAC_POLICY_MANIFEST" "$kind" "$target" "$value" "$selinux_type" <<'PY'
import json
import os
import stat
import sys
from pathlib import Path

manifest, kind, target, value, selinux_type = Path(sys.argv[1]), *sys.argv[2:]
record = {"version": 2, "kind": kind, "target": target, "value": value}
if kind == "selinux":
    if not target or not selinux_type:
        raise SystemExit("SELinux ownership records require a root and expected type")
    record["selinux_type"] = selinux_type
flags = os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
created = False
try:
    fd = os.open(manifest, flags)
except FileNotFoundError:
    fd = os.open(manifest, flags | os.O_CREAT | os.O_EXCL, 0o600)
    created = True
try:
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or (not created and info.st_mode & 0o022)
    ):
        raise SystemExit("MAC manifest must be a regular file owned by the installer identity")
    os.fchmod(fd, 0o600)
    os.lseek(fd, 0, os.SEEK_SET)
    with os.fdopen(os.dup(fd), encoding="utf-8") as handle:
        records = [json.loads(line) for line in handle if line.strip()]
    if record not in records:
        payload = (json.dumps(record, sort_keys=True) + "\n").encode()
        record_offset = os.lseek(fd, 0, os.SEEK_END)
        try:
            if os.write(fd, payload) != len(payload):
                raise OSError("short write while recording MAC change")
            os.fsync(fd)
        except BaseException:
            os.ftruncate(fd, record_offset)
            os.fsync(fd)
            raise
finally:
    os.close(fd)
PY
}

validate_apparmor_template_custody() {
  local target="$1"
  python3 - "$target" <<'PY'
import os
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
close_on_exec = getattr(os, "O_CLOEXEC", 0)
parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | close_on_exec)
try:
    parent_info = os.fstat(parent_fd)
    if not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != 0 or parent_info.st_mode & 0o022:
        raise SystemExit("AppArmor template parent is not a protected root-owned directory")
    template_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | close_on_exec, dir_fd=parent_fd)
    try:
        template_info = os.fstat(template_fd)
        if not stat.S_ISREG(template_info.st_mode) or template_info.st_uid != 0 or template_info.st_mode & 0o022:
            raise SystemExit("AppArmor template is not a protected root-owned file")
    finally:
        os.close(template_fd)
finally:
    os.close(parent_fd)
PY
}

undo_apparmor_rule() {
  local target="$1" rule="$2" expected_identity="$3"
  python3 - "$target" "$rule" "$expected_identity" <<'PY'
import os
import secrets
import stat
import sys
from pathlib import Path

path, rule, expected_identity = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
close_on_exec = getattr(os, "O_CLOEXEC", 0)
flags = os.O_RDONLY | os.O_NOFOLLOW | close_on_exec
parent_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | close_on_exec)
try:
    parent_info = os.fstat(parent_descriptor)
    if not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != 0 or parent_info.st_mode & 0o022:
        raise SystemExit("AppArmor template parent lost protected root custody before immediate rollback")
    descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or info.st_mode & 0o022
            or f"{info.st_dev}:{info.st_ino}" != expected_identity
        ):
            raise SystemExit("AppArmor template changed before immediate rollback")
        with os.fdopen(os.dup(descriptor), encoding="utf-8") as handle:
            text = handle.read()
    finally:
        os.close(descriptor)
    lines = text.splitlines()
    if rule not in lines:
        raise SystemExit("AppArmor rule disappeared before immediate rollback")
    lines.remove(rule)
    replacement = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    temporary_name = f".{path.name}.rollback.{secrets.token_hex(8)}"
    temporary_fd = os.open(
        temporary_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | close_on_exec,
        0o600,
        dir_fd=parent_descriptor,
    )
    try:
        os.fchmod(temporary_fd, stat.S_IMODE(info.st_mode))
        os.fchown(temporary_fd, info.st_uid, info.st_gid)
        with os.fdopen(temporary_fd, "w", encoding="utf-8") as handle:
            handle.write(replacement)
            handle.flush()
            os.fsync(handle.fileno())
        current_parent = path.parent.lstat()
        current = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISDIR(current_parent.st_mode)
            or current_parent.st_uid != 0
            or current_parent.st_mode & 0o022
            or (current_parent.st_dev, current_parent.st_ino) != (parent_info.st_dev, parent_info.st_ino)
        ):
            raise SystemExit("AppArmor template parent changed during immediate rollback")
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_uid != 0
            or current.st_mode & 0o022
            or f"{current.st_dev}:{current.st_ino}" != expected_identity
        ):
            raise SystemExit("AppArmor template changed during immediate rollback")
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        os.fsync(parent_descriptor)
    except BaseException:
        try:
            os.unlink(temporary_name, dir_fd=parent_descriptor)
        except OSError:
            pass
        raise
finally:
    os.close(parent_descriptor)
PY
}

configure_mac() {
  info "Configuring Mandatory Access Control (AppArmor / SELinux) for Libvirt..."
  # The configured root, not the default tree: a registered custom root is only
  # usable if qemu can traverse to *it* and AppArmor permits *it*.
  local storage_path
  storage_path="$(realpath -m -- "${STORAGE_ROOT:-$INSTALL_DIR/storage}")"

  # AppArmor (Ubuntu/Debian)
  local apparmor_template="${ASTINUSLAB_APPARMOR_TEMPLATE:-/etc/apparmor.d/libvirt/TEMPLATE.qemu}"
  local apparmor_rule="  \"${storage_path}/**\" rwk,"
  if [[ -e "$apparmor_template" || -L "$apparmor_template" ]]; then
    local apparmor_inspection apparmor_state apparmor_identity \
      apparmor_identity_device apparmor_identity_inode apparmor_parent_identity \
      apparmor_parent_device apparmor_parent_inode
    if ! apparmor_inspection="$(python3 - "$apparmor_template" "$apparmor_rule" <<'PY'
import os
import stat
import sys
from pathlib import Path

path, rule = Path(sys.argv[1]), sys.argv[2]
close_on_exec = getattr(os, "O_CLOEXEC", 0)
parent_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | close_on_exec)
try:
    parent_info = os.fstat(parent_descriptor)
    if not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != 0 or parent_info.st_mode & 0o022:
        raise SystemExit("AppArmor template parent is not a protected root-owned directory")
    descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | close_on_exec, dir_fd=parent_descriptor)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise SystemExit("AppArmor template is not a regular non-symlink file")
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise SystemExit("AppArmor template is not a protected root-owned file")
        with os.fdopen(os.dup(descriptor), encoding="utf-8") as handle:
            present = rule in handle.read().splitlines()
    finally:
        os.close(descriptor)
finally:
    os.close(parent_descriptor)
print(
    f"{'present' if present else 'absent'}:{info.st_dev}:{info.st_ino}:"
    f"{parent_info.st_dev}:{parent_info.st_ino}"
)
PY
)"; then
      warn "AppArmor template could not be inspected safely"
      return 1
    fi
    IFS=: read -r apparmor_state apparmor_identity_device apparmor_identity_inode \
      apparmor_parent_device apparmor_parent_inode <<<"$apparmor_inspection"
    apparmor_identity="${apparmor_identity_device}:${apparmor_identity_inode}"
    apparmor_parent_identity="${apparmor_parent_device}:${apparmor_parent_inode}"
    if [[ "$apparmor_state" == absent ]]; then
      info "Adding $storage_path to AppArmor template..."
      local apparmor_updated_identity
      apparmor_updated_identity="$(python3 - "$apparmor_template" "$storage_path" \
        "$apparmor_identity" "$apparmor_parent_identity" <<'PY'
import os
import secrets
import stat
import sys
from pathlib import Path

path, storage, expected_identity, expected_parent_identity = Path(sys.argv[1]), *sys.argv[2:]
close_on_exec = getattr(os, "O_CLOEXEC", 0)
flags = os.O_RDONLY | os.O_NOFOLLOW | close_on_exec
parent_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | close_on_exec
parent_descriptor = os.open(path.parent, parent_flags)
try:
    parent_info = os.fstat(parent_descriptor)
    if (
        not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != 0
        or parent_info.st_mode & 0o022
        or f"{parent_info.st_dev}:{parent_info.st_ino}" != expected_parent_identity
    ):
        raise SystemExit("AppArmor template parent changed or lost protected root custody")
    descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
    try:
        path_info = os.fstat(descriptor)
        if not stat.S_ISREG(path_info.st_mode):
            raise SystemExit("AppArmor template changed type before update")
        if path_info.st_uid != 0 or path_info.st_mode & 0o022:
            raise SystemExit("AppArmor template lost protected root custody before update")
        if f"{path_info.st_dev}:{path_info.st_ino}" != expected_identity:
            raise SystemExit("AppArmor template changed before update")
        with os.fdopen(os.dup(descriptor), encoding="utf-8") as handle:
            text = handle.read()
    finally:
        os.close(descriptor)
    line = f'  "{storage}/**" rwk,'
    if line not in text:
        replacement = text.replace("\n}", f"\n{line}\n}}")
        if line not in replacement.splitlines():
            raise SystemExit("AppArmor template has no supported closing brace")
        temp_name = f".{path.name}.tmp.{secrets.token_hex(8)}"
        temp_fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | close_on_exec,
            0o600,
            dir_fd=parent_descriptor,
        )
        try:
            os.fchmod(temp_fd, stat.S_IMODE(path_info.st_mode))
            os.fchown(temp_fd, path_info.st_uid, path_info.st_gid)
            with os.fdopen(temp_fd, "w", encoding="utf-8") as handle:
                handle.write(replacement)
                handle.flush()
                os.fsync(handle.fileno())
            current_parent = path.parent.lstat()
            current = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
            if (
                not stat.S_ISDIR(current_parent.st_mode)
                or current_parent.st_uid != 0
                or current_parent.st_mode & 0o022
                or (current_parent.st_dev, current_parent.st_ino) != (parent_info.st_dev, parent_info.st_ino)
            ):
                raise SystemExit("AppArmor template parent changed during update")
            if (
                not stat.S_ISREG(current.st_mode)
                or current.st_uid != 0
                or current.st_mode & 0o022
                or (current.st_dev, current.st_ino) != (path_info.st_dev, path_info.st_ino)
            ):
                raise SystemExit("AppArmor template changed during update")
            os.replace(temp_name, path.name, src_dir_fd=parent_descriptor, dst_dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
        except BaseException:
            try:
                os.unlink(temp_name, dir_fd=parent_descriptor)
            except OSError:
                pass
            raise
    verification_fd = os.open(path.name, flags, dir_fd=parent_descriptor)
    try:
        verification_info = os.fstat(verification_fd)
        if verification_info.st_uid != 0 or verification_info.st_mode & 0o022:
            raise SystemExit("AppArmor template lost protected root custody after update")
        with os.fdopen(os.dup(verification_fd), encoding="utf-8") as handle:
            if line not in handle.read().splitlines():
                raise SystemExit("AppArmor rule was not inserted")
    finally:
        os.close(verification_fd)
finally:
    os.close(parent_descriptor)
print(f"{verification_info.st_dev}:{verification_info.st_ino}")
PY
      )" || return 1
      if ! record_mac_change apparmor "$apparmor_template" "$apparmor_rule"; then
        undo_apparmor_rule "$apparmor_template" "$apparmor_rule" "$apparmor_updated_identity" \
          || warn "Could not immediately undo an unrecorded AppArmor rule"
        return 1
      fi
      record_host_access_transaction apparmor "$apparmor_template" "$apparmor_rule" || return 1
      validate_apparmor_template_custody "$apparmor_template" || return 1
      systemctl restart apparmor 2>/dev/null || {
        warn "AppArmor policy reload failed"
        return 1
      }
      systemctl restart libvirtd 2>/dev/null || {
        warn "Libvirt restart failed after AppArmor policy update"
        return 1
      }
    fi
  fi

  # SELinux (Fedora/RHEL)
  if command -v getenforce >/dev/null 2>&1 && [[ "$(getenforce)" == "Enforcing" ]]; then
    info "SELinux is Enforcing. Configuring fcontext for $storage_path..."
    if command -v semanage >/dev/null 2>&1; then
      local selinux_expression fcontext_list selinux_rule_state
      selinux_expression="$(python3 - "$storage_path" <<'PY'
import re
import sys

print(re.escape(sys.argv[1]) + r"(/.*)?")
PY
)" || return 1
      fcontext_list="$(semanage fcontext -l 2>/dev/null || true)"
      selinux_rule_state="$(python3 -c '
import sys
expression = sys.argv[1]
matches = [line.split()[-1] for line in sys.stdin
           if line.startswith(expression) and line[len(expression):len(expression) + 1].isspace()]
print("absent" if not matches else
      "correct" if all(":svirt_image_t:" in context for context in matches) else "wrong")
' "$selinux_expression" <<<"$fcontext_list")" || return 1
      if [[ "$selinux_rule_state" == wrong ]]; then
        warn "Existing SELinux fcontext for $storage_path does not use svirt_image_t"
        return 1
      fi
      if [[ "$selinux_rule_state" == absent ]]; then
        semanage fcontext -a -t svirt_image_t "$selinux_expression" 2>/dev/null || {
          warn "Could not add SELinux fcontext for $storage_path"
          return 1
        }
        fcontext_list="$(semanage fcontext -l 2>/dev/null || true)"
        selinux_rule_state="$(python3 -c '
import sys
expression = sys.argv[1]
matches = [line.split()[-1] for line in sys.stdin
           if line.startswith(expression) and line[len(expression):len(expression) + 1].isspace()]
print("absent" if not matches else
      "correct" if all(":svirt_image_t:" in context for context in matches) else "wrong")
' "$selinux_expression" <<<"$fcontext_list")" || return 1
        if [[ "$selinux_rule_state" != correct ]]; then
          semanage fcontext -d "$selinux_expression" >/dev/null 2>&1 || true
          warn "SELinux fcontext could not be verified after creation"
          return 1
        fi
        if ! record_mac_change selinux "$storage_path" "$selinux_expression" svirt_image_t; then
          semanage fcontext -d "$selinux_expression" >/dev/null 2>&1 \
            || warn "Could not immediately undo an unrecorded SELinux fcontext"
          restorecon -Rv "$storage_path" >/dev/null 2>&1 || true
          return 1
        fi
        record_host_access_transaction \
          selinux "$storage_path" "$selinux_expression" selinux_type svirt_image_t || return 1
      fi
      restorecon -Rv "$storage_path" >/dev/null 2>&1 || {
        warn "Could not apply the SELinux context to $storage_path"
        return 1
      }
    else
      warn "semanage not found. Install policycoreutils-python-utils to configure SELinux."
      return 1
    fi
  fi

  grant_qemu_traversal "$storage_path" || return 1
  ok "MAC permissions configured"
}

install_systemd_units() {
  info "Installing systemd units..."
  local unit src dst run_user
  for unit in astinuslab-api astinuslab-runner astinuslab-analysis; do
    src="$RELEASE_DIR/ops/systemd/${unit}.service"
    if $SPLIT_USERS; then
      dst="$SYSTEMD_UNIT_DIR/${unit}.service"
      case "$unit" in
        astinuslab-api) run_user="$API_USER" ;;
        astinuslab-runner) run_user="$RUNNER_USER" ;;
        astinuslab-analysis) run_user="$ANALYSIS_USER" ;;
      esac
      render_template "$src" "$dst" "$INSTALL_DIR" "$STORAGE_ROOT" \
        "$run_user" astinuslab astinuslab-api.service "$unit" true || return 1
    else
      dst="$SYSTEMD_UNIT_DIR/${unit}@.service"
      render_template "$src" "$dst" "$INSTALL_DIR" "$STORAGE_ROOT" || return 1
    fi
  done
  systemctl daemon-reload || return 1
  ok "Systemd units installed"
}

service_units() {
  if $SPLIT_USERS; then
    printf '%s\n' astinuslab-api.service astinuslab-runner.service astinuslab-analysis.service
  else
    printf '%s\n' \
      "astinuslab-api@${SERVICE_USER}.service" \
      "astinuslab-runner@${SERVICE_USER}.service" \
      "astinuslab-analysis@${SERVICE_USER}.service"
  fi
}

control_services() {
  local unit
  local units=()
  mapfile -t units < <(service_units)
  if $ENABLE_SERVICES; then
    info "Enabling AstinusLab services..."
    for unit in "${units[@]}"; do
      systemctl enable "$unit" || return 1
    done
  fi
  if ! $START_SERVICES; then
    warn "Service start skipped by --no-start"
    return 0
  fi
  # Always restart, never start. `systemctl start` is a no-op on an active
  # unit, so an upgrade that used it left the old processes attached to a
  # release the installer had already superseded. `restart` starts an inactive
  # unit too, so one verb covers the clean install and the upgrade and there is
  # no second path to get wrong.
  info "Restarting AstinusLab services..."
  for unit in "${units[@]}"; do
    systemctl restart "$unit" || return 1
  done
  ok "Services restarted"
}

_migration_fail() {
  local message="$1"
  fail_phase "$message"
  warn "$message; restoring pre-migration identities and permissions"
  migration_rollback || warn "Split-user rollback encountered an error; Section 3.1 Gate A snapshot remains authoritative"
  return 1
}

snapshot_privileged_manifest() {
  local source="$1" destination="$2"
  python3 - "$source" "$destination" <<'PY'
import os
import stat
import sys

source, destination = sys.argv[1:]
read_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
source_fd = os.open(source, read_flags)
try:
    info = os.fstat(source_fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise SystemExit(f"manifest is not a protected regular installer-owned file: {source}")
    destination_fd = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        while payload := os.read(source_fd, 1024 * 1024):
            view = memoryview(payload)
            while view:
                written = os.write(destination_fd, view)
                view = view[written:]
        os.fsync(destination_fd)
    finally:
        os.close(destination_fd)
finally:
    os.close(source_fd)
PY
}

snapshot_protected_regular() {
  local source="$1" destination="$2"
  python3 - "$source" "$destination" <<'PY'
import os
import stat
import sys

source, destination = sys.argv[1:]
descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
try:
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise SystemExit(f"privileged state is not a protected regular installer-owned file: {source}")
finally:
    os.close(descriptor)
if os.path.lexists(destination):
    raise SystemExit(f"privileged snapshot destination already exists: {destination}")
PY
  cp -a --no-dereference -- "$source" "$destination"
}

restore_protected_regular() {
  local snapshot="$1" destination="$2"
  [[ -f "$snapshot" && ! -L "$snapshot" ]] || return 1
  install -d -o root -g root -m 0755 "$(dirname "$destination")" || return 1
  rm -f -- "$destination" || return 1
  cp -a --no-dereference -- "$snapshot" "$destination"
}

snapshot_managed_unit() {
  local source="$1" destination="$2"
  if [[ -L "$source" ]]; then
    [[ "$(readlink -- "$source")" == /dev/null ]] || {
      warn "Unsupported systemd unit symlink: $source"
      return 1
    }
  elif [[ ! -f "$source" ]]; then
    warn "Unsupported systemd unit state: $source"
    return 1
  fi
  rm -f -- "$destination" || return 1
  cp -a --no-dereference -- "$source" "$destination"
}

restore_managed_unit() {
  local backup="$1" destination="$2"
  if [[ -L "$backup" ]]; then
    [[ "$(readlink -- "$backup")" == /dev/null ]] || return 1
  elif [[ ! -f "$backup" ]]; then
    return 1
  fi
  rm -f -- "$destination" || return 1
  mv -T -- "$backup" "$destination"
}

snapshot_managed_config_state() {
  local destination="$MIGRATION_SNAPSHOT/managed-config.json"
  python3 -B "$SCRIPT_DIR/managed_config.py" capture "$INSTALL_DIR" "$destination"
}

restore_managed_config_state() {
  local snapshot="$MIGRATION_SNAPSHOT/managed-config.json"
  [[ -f "$snapshot" && ! -L "$snapshot" ]] || return 1
  python3 -B "$SCRIPT_DIR/managed_config.py" restore "$INSTALL_DIR" "$snapshot"
}

snapshot_acl_state() {
  local root="$1" destination="$2"
  python3 "$SCRIPT_DIR/acl_snapshot.py" capture "$root" "$destination"
}

migration_snapshot_mutable_state() {
  [[ -n "$MIGRATION_SNAPSHOT" && -d "$MIGRATION_SNAPSHOT" ]] || return 1
  [[ ! -e "$MIGRATION_SNAPSHOT/mutable-state.complete" ]] || return 0
  if [[ -e "$INSTALL_DIR" ]]; then
    snapshot_acl_state "$INSTALL_DIR" "$MIGRATION_SNAPSHOT/install.acl" || return 1
    snapshot_managed_config_state || return 1
  else
    touch "$MIGRATION_SNAPSHOT/install.missing" || return 1
  fi
  if [[ -n "$STORAGE_ROOT" && "$STORAGE_ROOT" != "$INSTALL_DIR"/* ]]; then
    snapshot_acl_state "$STORAGE_ROOT" "$MIGRATION_SNAPSHOT/storage-root.acl" || return 1
  fi
  touch "$MIGRATION_SNAPSHOT/mutable-state.complete"
}

migration_snapshot() {
  local include_mutable="${1:-true}"
  [[ -z "$MIGRATION_SNAPSHOT" ]] || return 0
  MIGRATION_SNAPSHOT="$(mktemp -d "$MIGRATION_SNAPSHOT_PARENT/astinuslab-split.XXXXXXXX")" || return 1
  mkdir -p "$MIGRATION_SNAPSHOT/units" || return 1
  MIGRATION_CREATED_DIRS="$MIGRATION_SNAPSHOT/created-dirs"
  HOST_ACCESS_TRANSACTION_LOG="$MIGRATION_SNAPSHOT/host-access.transaction"
  : > "$HOST_ACCESS_TRANSACTION_LOG" || return 1
  chmod 0600 "$HOST_ACCESS_TRANSACTION_LOG" || return 1

  if [[ "$include_mutable" == true ]]; then migration_snapshot_mutable_state || return 1; fi
  getent group astinuslab >/dev/null || touch "$MIGRATION_SNAPSHOT/group.missing" || return 1
  if [[ -e "$SUDOERS_FILE" || -L "$SUDOERS_FILE" ]]; then
    snapshot_protected_regular "$SUDOERS_FILE" "$MIGRATION_SNAPSHOT/sudoers" || return 1
  else
    touch "$MIGRATION_SNAPSHOT/sudoers.missing" || return 1
  fi
  if [[ -d "$HELPER_INSTALL_DIR" ]]; then
    cp -a "$HELPER_INSTALL_DIR" "$MIGRATION_SNAPSHOT/libexec" || return 1
  else
    touch "$MIGRATION_SNAPSHOT/libexec.missing" || return 1
  fi
  if [[ -e "$POLICY_FILE" || -L "$POLICY_FILE" ]]; then
    snapshot_protected_regular "$POLICY_FILE" "$MIGRATION_SNAPSHOT/acl-policy.json" || return 1
  else
    touch "$MIGRATION_SNAPSHOT/policy.missing" || return 1
  fi
  if [[ -e "$RUNTIME_IDENTITIES_FILE" || -L "$RUNTIME_IDENTITIES_FILE" ]]; then
    snapshot_protected_regular "$RUNTIME_IDENTITIES_FILE" "$MIGRATION_SNAPSHOT/runtime-identities" || return 1
  else
    touch "$MIGRATION_SNAPSHOT/runtime-identities.missing" || return 1
  fi
  local manifest label
  for manifest in "$TRAVERSAL_ACL_MANIFEST" "$MAC_POLICY_MANIFEST"; do
    if [[ "$manifest" == "$TRAVERSAL_ACL_MANIFEST" ]]; then
      label=traversal-acl
    else
      label=mac-policy
    fi
    if [[ -e "$manifest" || -L "$manifest" ]]; then
      [[ -f "$manifest" && ! -L "$manifest" ]] || return 1
      snapshot_privileged_manifest "$manifest" "$MIGRATION_SNAPSHOT/$label.before" || return 1
      touch "$MIGRATION_SNAPSHOT/$label.existed" || return 1
    else
      : > "$MIGRATION_SNAPSHOT/$label.before"
    fi
  done

  local unit unit_file path
  for unit_file in \
    astinuslab-api@.service astinuslab-runner@.service astinuslab-analysis@.service \
    astinuslab-api.service astinuslab-runner.service astinuslab-analysis.service; do
    path="$SYSTEMD_UNIT_DIR/$unit_file"
    if [[ -e "$path" || -L "$path" ]]; then
      snapshot_managed_unit "$path" "$MIGRATION_SNAPSHOT/units/$unit_file" || return 1
    fi
  done
  while IFS= read -r unit; do
    if systemctl is-enabled --quiet "$unit" 2>/dev/null; then
      echo "$unit" >> "$MIGRATION_SNAPSHOT/enabled" || return 1
    fi
    if systemctl is-active --quiet "$unit" 2>/dev/null; then
      echo "$unit" >> "$MIGRATION_SNAPSHOT/active" || return 1
    fi
  done < <(
    printf '%s\n' \
      "astinuslab-api@${SERVICE_USER}.service" \
      "astinuslab-runner@${SERVICE_USER}.service" \
      "astinuslab-analysis@${SERVICE_USER}.service" \
      astinuslab-api.service astinuslab-runner.service astinuslab-analysis.service
  )

  local user
  for user in "$API_USER" "$RUNNER_USER" "$ANALYSIS_USER"; do
    id "$user" &>/dev/null || echo "$user" >> "$MIGRATION_SNAPSHOT/users.missing" || return 1
  done
  ok "Privileged rollback snapshot: $MIGRATION_SNAPSHOT"
}

rollback_snapshot_host_access() {
  [[ -n "$MIGRATION_SNAPSHOT" && -d "$MIGRATION_SNAPSHOT" ]] || return 1
  local acl_existed=false mac_existed=false helper
  [[ ! -f "$MIGRATION_SNAPSHOT/traversal-acl.existed" ]] || acl_existed=true
  [[ ! -f "$MIGRATION_SNAPSHOT/mac-policy.existed" ]] || mac_existed=true
  helper="$REPO_DIR/ops/deploy/configure-privileged-roots.sh"
  [[ -f "$helper" ]] || return 1
  ASTINUSLAB_TRAVERSAL_ACL_MANIFEST="$TRAVERSAL_ACL_MANIFEST" \
    ASTINUSLAB_MAC_POLICY_MANIFEST="$MAC_POLICY_MANIFEST" \
    ASTINUSLAB_HOST_ACCESS_TRANSACTION_LOG="$HOST_ACCESS_TRANSACTION_LOG" \
    bash -euo pipefail -c '
      source "$1"
      ACL_MANIFEST_BEFORE="$2"
      ACL_MANIFEST_EXISTED="$3"
      MAC_MANIFEST_BEFORE="$4"
      MAC_MANIFEST_EXISTED="$5"
      STORAGE_ROOT="$6"
      rollback_host_access_delta
    ' astinuslab-upgrade-rollback \
      "$helper" \
      "$MIGRATION_SNAPSHOT/traversal-acl.before" "$acl_existed" \
      "$MIGRATION_SNAPSHOT/mac-policy.before" "$mac_existed" \
      "$STORAGE_ROOT"
}

restore_acl_snapshot() {
  local snapshot="$1"
  shift
  python3 "$SCRIPT_DIR/acl_snapshot.py" restore "$snapshot" "$@"
}

undo_migration_created_dirs() {
  local record="$1"
  [[ -f "$record" ]] || return 0
  python3 - "$record" "$SERVICE_USER" astinuslab <<'PY'
import grp
import os
import pwd
import stat
import sys

record, owner_name, group_name = sys.argv[1:]
owner = pwd.getpwnam(owner_name).pw_uid
group = grp.getgrnam(group_name).gr_gid
directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
with open(record, encoding="utf-8") as handle:
    paths = [line.rstrip("\n") for line in handle if line.strip()]
# Deepest first, so a parent this migration created becomes removable once its
# created children are gone.
for path in sorted(set(paths), key=lambda value: (value.count("/"), value), reverse=True):
    parent, name = os.path.split(path)
    if not name or not os.path.isabs(path):
        raise SystemExit(f"refusing to undo an unsafe recorded path: {path}")
    try:
        parent_fd = os.open(parent, directory_flags)
    except FileNotFoundError:
        continue
    try:
        try:
            child_fd = os.open(name, directory_flags, dir_fd=parent_fd)
        except FileNotFoundError:
            continue
        try:
            info = os.fstat(child_fd)
            if not stat.S_ISDIR(info.st_mode):
                raise SystemExit(f"recorded path is no longer a directory: {path}")
            with os.scandir(child_fd) as entries:
                empty = next(entries, None) is None
            if not empty:
                # Never destroy bytes written under a directory this migration
                # created; returning custody is enough to keep the invariant.
                os.fchown(child_fd, owner, group)
                continue
        finally:
            os.close(child_fd)
        os.rmdir(name, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)
PY
}

rollback_runtime_control_locks() {
  [[ -f "$MIGRATION_SNAPSHOT/forensic-locks.configured" ]] || return 0
  python3 -I "$REPO_DIR/ops/deploy/runtime_control_locks.py" --rollback \
    "$STORAGE_ROOT" "$API_USER" "$RUNNER_USER" "$SERVICE_USER" \
    "$MIGRATION_SNAPSHOT/install.acl" "$MIGRATION_SNAPSHOT/storage-root.acl"
}

migration_rollback() {
  [[ -n "$MIGRATION_SNAPSHOT" && -d "$MIGRATION_SNAPSHOT" ]] || return 1
  local restart_services="${1:-true}" unit user failed=false
  if ! quiesce_services; then
    warn "Could not stop every AstinusLab service; refusing to restore privileged or identity state"
    return 1
  fi
  for unit in \
    "astinuslab-api@${SERVICE_USER}.service" \
    "astinuslab-runner@${SERVICE_USER}.service" \
    "astinuslab-analysis@${SERVICE_USER}.service" \
    astinuslab-api.service astinuslab-runner.service astinuslab-analysis.service; do
    if systemctl is-enabled --quiet "$unit" 2>/dev/null; then
      if ! systemctl disable "$unit"; then
        warn "Could not disable $unit; refusing to restore privileged or identity state"
        return 1
      fi
    fi
  done

  # Services are stopped. Reject unexpected lock data before restoring/deleting
  # identities; new empty coordination files are safe to recreate after rollback.
  # Package data must be reconciled before restoring/deleting writer identities.
  rollback_package_custody || return 1
  rollback_runtime_control_locks || return 1
  split_database_layout rollback || return 1
  rollback_service_logs || return 1
  if ! rollback_snapshot_host_access; then failed=true; fi

  if [[ -f "$MIGRATION_SNAPSHOT/sudoers" ]]; then
    if ! restore_protected_regular "$MIGRATION_SNAPSHOT/sudoers" "$SUDOERS_FILE"; then failed=true; fi
  else
    if ! rm -f "$SUDOERS_FILE"; then failed=true; fi
  fi
  if ! rm -rf -- "$HELPER_INSTALL_DIR"; then failed=true; fi
  if [[ -d "$MIGRATION_SNAPSHOT/libexec" ]]; then
    if ! cp -a "$MIGRATION_SNAPSHOT/libexec" "$HELPER_INSTALL_DIR"; then failed=true; fi
  fi
  if [[ -f "$MIGRATION_SNAPSHOT/acl-policy.json" ]]; then
    if ! restore_protected_regular "$MIGRATION_SNAPSHOT/acl-policy.json" "$POLICY_FILE"; then failed=true; fi
  else
    if ! rm -f "$POLICY_FILE"; then failed=true; fi
  fi
  if [[ -f "$MIGRATION_SNAPSHOT/runtime-identities" ]]; then
    if ! restore_protected_regular "$MIGRATION_SNAPSHOT/runtime-identities" "$RUNTIME_IDENTITIES_FILE"; then failed=true; fi
  else
    if ! rm -f "$RUNTIME_IDENTITIES_FILE"; then failed=true; fi
  fi

  if ! rm -f -- \
      "$SYSTEMD_UNIT_DIR/astinuslab-api@.service" \
      "$SYSTEMD_UNIT_DIR/astinuslab-runner@.service" \
      "$SYSTEMD_UNIT_DIR/astinuslab-analysis@.service" \
      "$SYSTEMD_UNIT_DIR/astinuslab-api.service" \
      "$SYSTEMD_UNIT_DIR/astinuslab-runner.service" \
      "$SYSTEMD_UNIT_DIR/astinuslab-analysis.service"; then
    failed=true
  fi
  if [[ -d "$MIGRATION_SNAPSHOT/units" ]]; then
    if ! cp -a "$MIGRATION_SNAPSHOT/units/." "$SYSTEMD_UNIT_DIR/"; then failed=true; fi
  fi
  if ! systemctl daemon-reload; then failed=true; fi

  if [[ -f "$MIGRATION_SNAPSHOT/managed-config.json" ]]; then
    if ! restore_managed_config_state; then failed=true; fi
  fi
  if [[ -f "$MIGRATION_SNAPSHOT/install.acl" ]]; then
    if ! restore_acl_snapshot "$MIGRATION_SNAPSHOT/install.acl" "$INSTALL_DIR/storage"; then failed=true; fi
  elif [[ -f "$MIGRATION_SNAPSHOT/install.missing" && -d "$INSTALL_DIR" ]]; then
    if ! rm -rf -- "$INSTALL_DIR"; then failed=true; fi
  fi
  if [[ -f "$MIGRATION_SNAPSHOT/storage-root.acl" ]]; then
    if ! restore_acl_snapshot "$MIGRATION_SNAPSHOT/storage-root.acl" "$STORAGE_ROOT"; then failed=true; fi
  fi
  if ! undo_migration_created_dirs "$MIGRATION_SNAPSHOT/created-dirs"; then failed=true; fi

  if ! visudo -c; then failed=true; fi
  # A failed ACL/config restore can still leave package bytes owned by the new
  # service users. Preserve those identities and the snapshot for recovery.
  $failed && return 1

  if [[ -f "$MIGRATION_SNAPSHOT/users.missing" ]]; then
    while IFS= read -r user; do
      if id "$user" &>/dev/null; then
        if ! userdel "$user" 2>/dev/null; then failed=true; fi
      fi
    done < "$MIGRATION_SNAPSHOT/users.missing"
  fi
  if [[ -f "$MIGRATION_SNAPSHOT/group.missing" ]] && getent group astinuslab >/dev/null; then
    if ! groupdel astinuslab; then failed=true; fi
  fi
  # Never start writers after a partial privileged, identity, ACL, or unit
  # restore. Enabled state is restored only after every static boundary is
  # known good, and active state is optional so an outer database rollback can
  # explicitly keep the host quiesced.
  $failed && return 1
  if [[ "$restart_services" == true && -f "$MIGRATION_SNAPSHOT/enabled" ]]; then
    while IFS= read -r unit; do
      if ! systemctl enable "$unit"; then failed=true; fi
    done < "$MIGRATION_SNAPSHOT/enabled"
  fi
  $failed && return 1
  if [[ "$restart_services" == true && -f "$MIGRATION_SNAPSHOT/active" ]]; then
    while IFS= read -r unit; do
      if ! systemctl start "$unit"; then failed=true; fi
    done < "$MIGRATION_SNAPSHOT/active"
  fi
  $failed && return 1
  return 0
}

migration_commit() {
  split_database_layout retire || return 1
  publish_runtime_identities || return 1
  if ! split_database_layout commit; then
    # A killed helper can report failure after durable receipt publication.
    # Never undo that committed layout because its cleanup was interrupted.
    split_database_layout committed || return 1
  fi
  migration_discard_snapshot || warn "Split migration committed; private snapshot cleanup remains"
  return 0
}

split_database_layout() {
  local action="$1"
  if [[ "$action" == pending ]]; then
    [[ -d "$INSTALL_DIR" ]] || return 0
    python3 -I -B "$SCRIPT_DIR/shared_databases.py" pending --install "$INSTALL_DIR"
    return $?
  fi
  if [[ "$action" != prepare && "$action" != preflight ]]; then
    if [[ -z "$MIGRATION_SNAPSHOT" || ! -f "$MIGRATION_SNAPSHOT/sqlite-layout.json" ]]; then
      [[ "$action" == retain || "$action" == rollback ]] && return 0
      return 1
    fi
  fi
  python3 -I -B "$SCRIPT_DIR/shared_databases.py" "$action" \
    --install "$INSTALL_DIR" --storage "$STORAGE_ROOT" --release "$RELEASE_DIR" \
    --snapshot "$MIGRATION_SNAPSHOT" --api "$API_USER" --runner "$RUNNER_USER" \
    --analysis "$ANALYSIS_USER" --legacy "$SERVICE_USER"
}

provision_service_logs() {
  local root="$1" label=install
  [[ -n "$MIGRATION_SNAPSHOT" && -d "$MIGRATION_SNAPSHOT" ]] || return 1
  [[ "$root" == "$INSTALL_DIR/storage" ]] || label="storage-root"
  python3 -I "$SCRIPT_DIR/service_logs.py" provision "$root" \
    "$API_USER" "$RUNNER_USER" "$ANALYSIS_USER" "$SERVICE_USER" \
    "$MIGRATION_SNAPSHOT/service-logs-$label.json"
}

rollback_service_logs() {
  local root label record
  for label in install storage-root; do
    record="$MIGRATION_SNAPSHOT/service-logs-$label.json"
    [[ -f "$record" ]] || continue
    root="$INSTALL_DIR/storage"
    [[ "$label" == install ]] || root="$STORAGE_ROOT"
    python3 -I -B "$SCRIPT_DIR/shared_databases.py" backups --install "$INSTALL_DIR" || return 1
    python3 -I "$SCRIPT_DIR/service_logs.py" rollback "$root" \
      "$API_USER" "$RUNNER_USER" "$ANALYSIS_USER" "$SERVICE_USER" "$record" \
      "$INSTALL_DIR/backups/service-logs-${MIGRATION_SNAPSHOT##*/}-$label" || return 1
  done
}

publish_runtime_identities() {
  local marker_dir marker_tmp
  marker_dir="$(dirname "$RUNTIME_IDENTITIES_FILE")"
  install -d -o root -g root -m 0755 "$marker_dir" || return 1
  marker_tmp="$(mktemp "$marker_dir/.runtime-identities.XXXXXXXX")" || return 1
  if ! cat > "$marker_tmp" <<EOF
mode=split
api=$API_USER
runner=$RUNNER_USER
analysis=$ANALYSIS_USER
group=astinuslab
EOF
  then
    rm -f -- "$marker_tmp"
    return 1
  fi
  if ! chown root:root "$marker_tmp" || ! chmod 0644 "$marker_tmp"; then
    rm -f -- "$marker_tmp"
    return 1
  fi
  if ! python3 - "$marker_tmp" "$RUNTIME_IDENTITIES_FILE" <<'PY'
import os
import stat
import sys

temporary, destination = sys.argv[1:]
directory = os.path.dirname(destination)
if os.path.dirname(temporary) != directory:
    raise SystemExit("runtime identity marker temporary file crossed directories")
directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
directory_fd = os.open(directory, directory_flags)
try:
    temporary_name = os.path.basename(temporary)
    destination_name = os.path.basename(destination)
    descriptor = os.open(temporary_name, file_flags, dir_fd=directory_fd)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SystemExit("runtime identity marker temporary file is unsafe")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(
        temporary_name,
        destination_name,
        src_dir_fd=directory_fd,
        dst_dir_fd=directory_fd,
    )
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
  then
    rm -f -- "$marker_tmp"
    return 1
  fi
}

migration_discard_snapshot() {
  if [[ "$MIGRATION_SNAPSHOT" == "$MIGRATION_SNAPSHOT_PARENT"/astinuslab-split.* && -d "$MIGRATION_SNAPSHOT" ]]; then
    rm -rf -- "$MIGRATION_SNAPSHOT" || return 1
  fi
  MIGRATION_SNAPSHOT=""
  MIGRATION_CREATED_DIRS=""
  HOST_ACCESS_TRANSACTION_LOG=""
}

ensure_split_storage_root() {
  prepare_managed_directory storage "$API_USER" astinuslab 2770 || return 1
  resolve_storage_root || return 1
  # Root-owned authority must exist before split services open package stores.
  # The already captured snapshot restores it if migration fails.
  publish_runtime_identities || return 1
}

install_transitional_sudoers() {
  local command=(
    "$REPO_DIR/ops/host/install_sudoers_astinuslab.sh"
    --install-dir "$INSTALL_DIR"
    --run-user "$RUNNER_USER"
    --storage-root "$STORAGE_ROOT"
    --helper-source-dir "$REPO_DIR/ops/host"
    --evidence-read-group astinuslab
  )
  if [[ "$SERVICE_USER" != "$RUNNER_USER" ]]; then
    command+=(--additional-run-user "$SERVICE_USER")
  fi
  "${command[@]}"
}

finalize_split_sudoers() {
  "$RELEASE_DIR/ops/host/install_sudoers_astinuslab.sh" \
    --install-dir "$INSTALL_DIR" \
    --run-user "$RUNNER_USER" \
    --storage-root "$STORAGE_ROOT" \
    --evidence-read-group astinuslab \
    --helper-source-dir "$RELEASE_DIR/ops/host"
}

verify_final_runner_helpers() {
  verify_runner_helpers
}

verify_runner_helpers() {
  runuser -u "$RUNNER_USER" -- sudo -n \
    /usr/local/libexec/astinuslab/libvirt_storage_admin.sh noop >/dev/null || return 1
  runuser -u "$RUNNER_USER" -- sudo -n \
    /usr/local/libexec/astinuslab/fix_libvirt_path_acl.sh --probe >/dev/null || return 1
  runuser -u "$RUNNER_USER" -- sudo -n \
    /usr/local/libexec/astinuslab/network_isolation_admin.sh noop >/dev/null || return 1
}

stop_old_services() {
  quiesce_services
}

disable_old_services() {
  local unit
  for unit in \
    "astinuslab-api@${SERVICE_USER}.service" \
    "astinuslab-runner@${SERVICE_USER}.service" \
    "astinuslab-analysis@${SERVICE_USER}.service"; do
    if systemctl is-enabled --quiet "$unit" 2>/dev/null; then
      systemctl disable "$unit" || return 1
    fi
  done
}

secure_tree_permissions() {
  local root="$1" owner="$2" group="$3" directory_mode="$4" file_mode="$5" extra_owner="${6:-}"
  [[ -d "$root" ]] || return 0
  python3 "$SCRIPT_DIR/permissions.py" tree \
    "$root" "$owner" "$group" "$directory_mode" "$file_mode" \
    "$SERVICE_USER" "$API_USER" "$RUNNER_USER" "$ANALYSIS_USER" "$extra_owner"
}

secure_ssh_key_permissions() {
  local root="${1:-$INSTALL_DIR/storage/ssh_keys}"
  [[ -e "$root" || -L "$root" ]] || return 0
  python3 "$SCRIPT_DIR/permissions.py" ssh-keys \
    "$root" "$API_USER" "$RUNNER_USER" astinuslab
}

secure_libvirt_image_access() {
  local root="$INSTALL_DIR/images/definitions"
  [[ -d "$root" ]] || return 0
  python3 "$SCRIPT_DIR/permissions.py" libvirt-images \
    "$root" libvirt-qemu \
    "$SERVICE_USER" "$API_USER" "$RUNNER_USER" "$ANALYSIS_USER"
}

secure_managed_directory_root() {
  local root="$1" owner="$2" group="$3" mode="$4"
  python3 "$SCRIPT_DIR/permissions.py" directory-root \
    "$root" "$owner" "$group" "$mode" \
    "$SERVICE_USER" "$API_USER" "$RUNNER_USER" "$ANALYSIS_USER"
}

secure_runtime_control_lock_permissions() {
  local root="$1"
  prepare_managed_directory tmp/forensic-capture-locks "$RUNNER_USER" astinuslab 0750 "$root" || return 1
  # Record before applying ACLs: a partial failure still needs rollback cleanup.
  if [[ -n "$MIGRATION_SNAPSHOT" ]]; then
    touch "$MIGRATION_SNAPSHOT/forensic-locks.configured" || return 1
  fi
  python3 -I "$REPO_DIR/ops/deploy/runtime_control_locks.py" \
    "$root" "$API_USER" "$RUNNER_USER" "$SERVICE_USER"
}

secure_package_storage_permissions() {
  local root="$1" name
  if [[ -n "$MIGRATION_SNAPSHOT" ]]; then
    touch "$MIGRATION_SNAPSHOT/package-custody.configured" || return 1
  fi
  for name in lab-packages lab-content; do
    if [[ ! -e "$root/$name" && ! -L "$root/$name" ]]; then
      prepare_managed_directory "$name" "$API_USER" astinuslab 0700 "$root" || return 1
    fi
  done
  python3 -I "$REPO_DIR/src/astinuslab/lab_packages/storage_custody.py" \
    "$root" "$SERVICE_USER" "$API_USER" "$RUNNER_USER"
}

rollback_package_custody() {
  [[ -f "$MIGRATION_SNAPSHOT/package-custody.configured" ]] || return 0
  local root
  for root in "$INSTALL_DIR/storage" "$STORAGE_ROOT"; do
    [[ -d "$root" ]] || continue
    python3 -I "$REPO_DIR/src/astinuslab/lab_packages/storage_custody.py" --rollback \
      "$root" "$SERVICE_USER" "$API_USER" "$RUNNER_USER" "$MIGRATION_SNAPSHOT/created-dirs" \
      "$MIGRATION_SNAPSHOT/install.acl" "$MIGRATION_SNAPSHOT/storage-root.acl" || return 1
    [[ "$INSTALL_DIR/storage" != "$STORAGE_ROOT" ]] || break
  done
}

configure_split_storage_permissions() {
  local root="$1" path owner mode
  secure_managed_directory_root "$root" "$API_USER" astinuslab 2770 || return 1
  while read -r path owner mode; do
    prepare_managed_directory "$path" "$owner" astinuslab "$mode" "$root" || return 1
  done <<EOF
dumps $RUNNER_USER 2750
ssh_keys $API_USER 2700
tmp $RUNNER_USER 0750
workload-cache $RUNNER_USER 0750
playground $RUNNER_USER 0700
playground/disks $RUNNER_USER 0700
playground/captures $RUNNER_USER 0750
EOF
  provision_service_logs "$root" || return 1
  secure_tree_permissions "$root/tmp" "$RUNNER_USER" astinuslab 0750 0640 || return 1
  # Reapply after generic tmp normalization on fresh, migrated and split upgrades.
  # Existing ACL snapshots and created-directory tracking cover rollback.
  secure_runtime_control_lock_permissions "$root" || return 1
  secure_tree_permissions "$root/workload-cache" "$RUNNER_USER" astinuslab 0750 0640 || return 1
  secure_managed_directory_root "$root/playground" "$RUNNER_USER" astinuslab 0700 || return 1
  # qemu owns the seed ISO it wrote for a running guest, so the disk tree it
  # may write accepts that transitional custody exactly like the image tree.
  secure_tree_permissions "$root/playground/disks" "$RUNNER_USER" astinuslab 0700 0600 libvirt-qemu || return 1
  prepare_managed_directory playground/disks "$RUNNER_USER" astinuslab 0700 "$root" || return 1
  prepare_managed_directory playground/captures "$RUNNER_USER" astinuslab 0750 "$root" || return 1
  secure_ssh_key_permissions "$root/ssh_keys" || return 1
  secure_package_storage_permissions "$root" || return 1
}

configure_split_permissions() {
  # Normalize every immutable shipped path, including top-level launchers,
  # documentation, lockfiles, and the venv. Exact mutable roots are pruned.
  find "$INSTALL_DIR" -xdev \
    \( -path "$INSTALL_DIR/astinuslab.toml" \
       -o -path "$INSTALL_DIR/config" \
       -o -path "$INSTALL_DIR/storage" \
       -o -path "$INSTALL_DIR/images/definitions" \
       -o -path "$INSTALL_DIR/vol3/symbols" \) -prune \
    -o -exec chown -h root:root {} + || return 1
  find "$INSTALL_DIR" -xdev \
    \( -path "$INSTALL_DIR/astinuslab.toml" \
       -o -path "$INSTALL_DIR/config" \
       -o -path "$INSTALL_DIR/storage" \
       -o -path "$INSTALL_DIR/images/definitions" \
       -o -path "$INSTALL_DIR/vol3/symbols" \) -prune \
    -o \( -type d -o -type f \) -exec chmod go-w {} + || return 1
  chmod 0755 "$INSTALL_DIR" || return 1

  secure_managed_file astinuslab.toml "$API_USER" astinuslab 0640 || return 1
  secure_tree_permissions "$INSTALL_DIR/config" "$API_USER" astinuslab 2750 0640 || return 1
  secure_tree_permissions \
    "$INSTALL_DIR/images/definitions" "$RUNNER_USER" astinuslab 0750 0640 libvirt-qemu || return 1
  secure_libvirt_image_access || return 1
  secure_tree_permissions "$INSTALL_DIR/vol3/symbols" "$ANALYSIS_USER" astinuslab 2770 0660 || return 1
  configure_split_storage_permissions "$INSTALL_DIR/storage" || return 1
  if [[ -n "$STORAGE_ROOT" && "$STORAGE_ROOT" != "$INSTALL_DIR/storage" ]]; then
    configure_split_storage_permissions "$STORAGE_ROOT" || return 1
  fi
}

remove_legacy_evidence_acls() {
  # Reconcile shared reads even when the prior identity is already the runner.
  python3 "$SCRIPT_DIR/permissions.py" drop-legacy-evidence-acl \
    "$STORAGE_ROOT" "$SERVICE_USER"
}

run_deep_readiness_as() {
  local readiness_user="$1" release_dir="$2" readiness_code="$3"
  runuser -u "$readiness_user" -- env -i PATH=/usr/bin:/bin \
    /usr/bin/python3 -I - --data-dir "$INSTALL_DIR" -- \
    /usr/bin/timeout 60s "$release_dir/.venv/bin/python" -c "$readiness_code" < "$SCRIPT_DIR/runtime_environment.py"
}

verify_release_readiness() {
  # The same deep readiness the split migration runs, applied to every install
  # so a schema or release change is never left live unverified. The release to
  # check is an argument because a rollback verifies the one it restored, not
  # the one that just failed.
  local release_dir="${1:-$RELEASE_DIR}"
  $START_SERVICES || {
    warn "[SKIP] Deep readiness skipped by --no-start; this release switch is unverified"
    return 0
  }
  local readiness_code readiness_user="$SERVICE_USER"
  $SPLIT_USERS && readiness_user="$API_USER"
  readiness_code=$'import time\nfrom astinuslab.readiness import collect_readiness, mandatory_runtime_ready\nfor attempt in range(4):\n    report = collect_readiness()\n    if mandatory_runtime_ready(report):\n        raise SystemExit(0)\n    if attempt != 3:\n        time.sleep(2)\nprint(report.public_dict())\nraise SystemExit(1)'
  (cd "$release_dir" && run_deep_readiness_as "$readiness_user" "$release_dir" "$readiness_code")
}

verify_split_rollback_units() {
  local snapshot="$1" unit field state pid enabled
  local -a units=(
    "astinuslab-api@${SERVICE_USER}.service"
    "astinuslab-runner@${SERVICE_USER}.service"
    "astinuslab-analysis@${SERVICE_USER}.service"
    astinuslab-api.service astinuslab-runner.service astinuslab-analysis.service
  )
  local -A known=() recorded=()
  [[ -d "$snapshot" && ! -L "$snapshot" ]] || return 1
  for unit in "${units[@]}"; do known["$unit"]=true; done
  for field in active enabled; do
    [[ ! -L "$snapshot/$field" ]] || return 1
    if [[ ! -e "$snapshot/$field" ]]; then continue; fi
    [[ -f "$snapshot/$field" ]] || return 1
    while IFS= read -r unit || [[ -n "$unit" ]]; do
      [[ -n "$unit" && "${known[$unit]:-false}" == true ]] || return 1
      [[ "${recorded[$field:$unit]:-false}" == false ]] || return 1
      recorded["$field:$unit"]=true
    done < "$snapshot/$field"
  done
  for unit in "${units[@]}"; do
    enabled=false
    if systemctl is-enabled --quiet "$unit" 2>/dev/null; then enabled=true; fi
    [[ "$enabled" == "${recorded[enabled:$unit]:-false}" ]] || return 1
    state="$(systemctl show "$unit" -p ActiveState --value 2>/dev/null)" || return 1
    if [[ "${recorded[active:$unit]:-false}" == true ]]; then
      [[ "$state" == active ]] || return 1
    else
      pid="$(systemctl show "$unit" -p MainPID --value 2>/dev/null)" || return 1
      [[ "$state" == inactive || "$state" == failed ]] || return 1
      [[ "$pid" == 0 ]] || return 1
    fi
  done
}

verify_split_services() {
  local release_dir="${1:-$RELEASE_DIR}"
  local rollback_snapshot="${2:-}" full_readiness=true
  local unit helper=/usr/local/libexec/astinuslab/libvirt_storage_admin.sh
  if [[ -n "$rollback_snapshot" ]]; then
    verify_split_rollback_units "$rollback_snapshot" || return 1
    for unit in astinuslab-api.service astinuslab-runner.service astinuslab-analysis.service; do
      if ! grep -Fxq -- "$unit" "$rollback_snapshot/active" 2>/dev/null; then full_readiness=false; fi
    done
  fi
  if $START_SERVICES && $full_readiness; then
    local attempt
    for attempt in 1 2 3 4 5; do
      for unit in astinuslab-api.service astinuslab-runner.service astinuslab-analysis.service; do
        systemctl is-active --quiet "$unit" || return 1
      done
      [[ "$attempt" == 5 ]] || sleep 1 || return 1
    done
  fi
  verify_runner_helpers || return 1
  if runuser -u "$API_USER" -- sudo -n -l "$helper" >/dev/null 2>&1; then
    warn "API identity unexpectedly has privileged-helper sudo access"
    return 1
  fi
  if runuser -u "$ANALYSIS_USER" -- sudo -n -l "$helper" >/dev/null 2>&1; then
    warn "Analysis identity unexpectedly has privileged-helper sudo access"
    return 1
  fi
  id "$API_USER" >/dev/null 2>&1 || return 1
  id "$ANALYSIS_USER" >/dev/null 2>&1 || return 1
  if id -nG "$API_USER" | tr ' ' '\n' | grep -Eq '^(libvirt|kvm)$'; then return 1; fi
  if id -nG "$ANALYSIS_USER" | tr ' ' '\n' | grep -Eq '^(libvirt|kvm)$'; then return 1; fi
  if ! $full_readiness; then
    info "Saved service state restored; full runtime readiness not asserted for originally inactive units"
    return 0
  fi
  local readiness_code
  readiness_code=$'import time\nfrom astinuslab.readiness import collect_readiness, mandatory_runtime_ready\nfor attempt in range(4):\n    report = collect_readiness()\n    if mandatory_runtime_ready(report):\n        raise SystemExit(0)\n    if attempt != 3:\n        time.sleep(2)\nprint(report.public_dict())\nraise SystemExit(1)'
  (cd "$release_dir" && run_deep_readiness_as "$API_USER" "$release_dir" "$readiness_code") || return 1
  return 0
}

migrate_to_split_users() {
  skip_phase 4 "Stage release and Python environment" "Using the already verified release candidate"
  begin_phase 5 "Prepare configuration, storage, and databases"
  resolve_storage_root || { _migration_fail "Could not resolve split storage root"; return 1; }
  bind_storage_root_identity || { _migration_fail "Could not bind the split storage root"; return 1; }
  migration_snapshot false || return 1
  complete_phase
  skip_phase 6 "Configure authentication and guest SSH" "Preserving candidate authentication and SSH configuration"
  begin_phase 7 "Install privileged host integration"
  assert_storage_root_identity || { _migration_fail "Storage root changed before transitional policy"; return 1; }
  install_transitional_sudoers || { _migration_fail "Could not install transitional helper policy"; return 1; }
  verify_runner_helpers || { _migration_fail "New runner helper verification failed"; return 1; }
  stop_old_services || { _migration_fail "Could not stop old services"; return 1; }
  migration_snapshot_mutable_state || { _migration_fail "Could not snapshot quiesced mutable state"; return 1; }
  ensure_split_storage_root || { _migration_fail "Could not prepare split storage root"; return 1; }
  configure_split_permissions || { _migration_fail "Could not apply split ownership"; return 1; }
  split_database_layout prepare || { _migration_fail "Could not prepare configured split databases"; return 1; }
  remove_legacy_evidence_acls || { _migration_fail "Could not remove legacy evidence ACL principals"; return 1; }
  complete_phase
  begin_phase 8 "Publish services and verify readiness"
  assert_storage_root_identity || { _migration_fail "Storage root changed before unit publication"; return 1; }
  install_systemd_units || { _migration_fail "Could not install split units"; return 1; }
  control_services || { _migration_fail "Could not start split units"; return 1; }
  verify_split_services || { _migration_fail "Split service verification failed"; return 1; }
  assert_storage_root_identity || { _migration_fail "Storage root changed before final policy"; return 1; }
  finalize_split_sudoers || { _migration_fail "Could not finalize runner-only sudoers"; return 1; }
  verify_final_runner_helpers || { _migration_fail "Final runner-only helper verification failed"; return 1; }
  disable_old_services || { _migration_fail "Could not disable old unit instances"; return 1; }
  migration_commit || { _migration_fail "Could not persist split identity state"; return 1; }
  cleanup_superseded_legacy_install || {
    warn "Split migration committed, but superseded legacy files could not be fully removed"
    return 1
  }
  complete_phase
}

rollback_existing_split_upgrade() {
  local reason="$1" failed=false restart_services=true
  fail_phase "$reason"
  quiesce_services || return 1
  split_database_layout retain || return 1
  # Restore the database/current link without starting through candidate unit
  # files. The privileged snapshot then restores those files and the exact
  # pre-upgrade active/enabled state before readiness is evaluated.
  release_rollback "$reason" false || { failed=true; restart_services=false; }
  migration_rollback "$restart_services" || failed=true
  if ! $failed && ! verify_split_services "$PREVIOUS_RELEASE_DIR" "$MIGRATION_SNAPSHOT"; then failed=true; fi
  $failed && warn "Split-upgrade rollback did not completely restore the prior runtime"
  $failed && return 1
  return 0
}

rollback_existing_single_upgrade() {
  local reason="$1" failed=false restart_services=true
  fail_phase "$reason"
  release_rollback "$reason" false || { failed=true; restart_services=false; }
  migration_rollback "$restart_services" || failed=true
  if ! $failed && [[ -n "$PREVIOUS_RELEASE_DIR" && -d "$PREVIOUS_RELEASE_DIR" ]] && $START_SERVICES; then
    verify_release_readiness "$PREVIOUS_RELEASE_DIR" || failed=true
  fi
  $failed && warn "Single-identity rollback did not completely restore the prior runtime"
  $failed && return 1
  return 0
}

upgrade_single_identity_install() {
  begin_phase 4 "Stage release and Python environment"
  migration_snapshot false || return 1
  install_source true || {
    rollback_existing_single_upgrade "Release staging failed" || true
    return 1
  }
  complete_phase
  begin_phase 5 "Prepare configuration, storage, and databases"
  quiesce_services || {
    rollback_existing_single_upgrade "Service quiesce failed" || true
    return 1
  }
  migration_snapshot_mutable_state || {
    rollback_existing_single_upgrade "Quiesced mutable-state snapshot failed" || true
    return 1
  }
  prepare_single_upgrade_mutable_state || {
    rollback_existing_single_upgrade "Mutable install-state preparation failed" || true
    return 1
  }
  prepare_storage || {
    rollback_existing_single_upgrade "Storage preparation failed" || true
    return 1
  }
  backup_database_before_migration || {
    rollback_existing_single_upgrade "Database backup failed" || true
    return 1
  }
  if ! apply_db_migrations; then
    rollback_existing_single_upgrade "Database migration failed" || true
    return 1
  fi
  complete_phase
  begin_phase 6 "Configure authentication and guest SSH"
  handle_ssh_key || {
    rollback_existing_single_upgrade "SSH-key preparation failed" || true
    return 1
  }
  complete_phase
  begin_phase 7 "Install privileged host integration"
  resolve_storage_root || {
    rollback_existing_single_upgrade "Storage-root resolution failed" || true
    return 1
  }
  bind_storage_root_identity || {
    rollback_existing_single_upgrade "Storage-root identity validation failed" || true
    return 1
  }
  assert_storage_root_identity || {
    rollback_existing_single_upgrade "Storage root changed before sudoers publication" || true
    return 1
  }
  install_sudoers_rule || {
    rollback_existing_single_upgrade "Sudoers installation failed" || true
    return 1
  }
  assert_storage_root_identity || {
    rollback_existing_single_upgrade "Storage root changed before MAC configuration" || true
    return 1
  }
  configure_mac || {
    rollback_existing_single_upgrade "MAC configuration failed" || true
    return 1
  }
  complete_phase
  begin_phase 8 "Publish services and verify readiness"
  assert_storage_root_identity || {
    rollback_existing_single_upgrade "Storage root changed before unit publication" || true
    return 1
  }
  install_systemd_units || {
    rollback_existing_single_upgrade "Unit installation failed" || true
    return 1
  }
  if ! switch_release_and_restart; then
    rollback_existing_single_upgrade "Could not switch to the new release" || true
    return 1
  fi
  if ! verify_release_readiness; then
    rollback_existing_single_upgrade "Deep readiness failed after the release switch" || true
    return 1
  fi
  migration_discard_snapshot || {
    rollback_existing_single_upgrade "Could not discard the privileged rollback snapshot" || true
    return 1
  }
  complete_phase
  cleanup_superseded_legacy_install || {
    warn "Upgrade committed, but superseded legacy files could not be fully removed"
    return 1
  }
}

upgrade_existing_split_install() {
  # A valid split marker means the identity transaction already committed. An
  # update changes only source/schema/service state and must use the same atomic
  # release path as a single-identity update rather than repeating migration.
  API_USER=astinuslab-api
  RUNNER_USER=astinuslab-runner
  ANALYSIS_USER=astinuslab-analysis
  info "Updating an existing split-identity installation..."
  begin_phase 4 "Stage release and Python environment"
  migration_snapshot false || return 1
  install_split_upgrade_source || {
    rollback_existing_split_upgrade "Release staging failed during split upgrade" || true
    return 1
  }
  complete_phase
  begin_phase 5 "Prepare configuration, storage, and databases"
  quiesce_services || {
    rollback_existing_split_upgrade "Service quiesce failed during split upgrade" || true
    return 1
  }
  migration_snapshot_mutable_state || {
    rollback_existing_split_upgrade "Quiesced mutable-state snapshot failed during split upgrade" || true
    return 1
  }
  backup_database_before_migration || {
    rollback_existing_split_upgrade "Database backup failed during split upgrade" || true
    return 1
  }
  resolve_storage_root || {
    rollback_existing_split_upgrade "Storage-root resolution failed during split upgrade" || true
    return 1
  }
  bind_storage_root_identity || {
    rollback_existing_split_upgrade "Storage-root identity validation failed during split upgrade" || true
    return 1
  }
  prepare_split_upgrade_mutable_state || {
    rollback_existing_split_upgrade "Split permission reconciliation failed during split upgrade" || true
    return 1
  }
  if ! apply_db_migrations; then
    rollback_existing_split_upgrade "Database migration failed during split upgrade" || true
    return 1
  fi
  complete_phase
  begin_phase 6 "Configure authentication and guest SSH"
  handle_ssh_key || {
    rollback_existing_split_upgrade "SSH-key preparation failed during split upgrade" || true
    return 1
  }
  complete_phase
  begin_phase 7 "Install privileged host integration"
  assert_storage_root_identity || {
    rollback_existing_split_upgrade "Storage root changed before MAC configuration" || true
    return 1
  }
  configure_mac || {
    rollback_existing_split_upgrade "MAC configuration failed during split upgrade" || true
    return 1
  }
  complete_phase
  begin_phase 8 "Publish services and verify readiness"
  assert_storage_root_identity || {
    rollback_existing_split_upgrade "Storage root changed before unit publication" || true
    return 1
  }
  install_systemd_units || {
    rollback_existing_split_upgrade "Unit installation failed during split upgrade" || true
    return 1
  }
  assert_storage_root_identity || {
    rollback_existing_split_upgrade "Storage root changed before runner policy publication" || true
    return 1
  }
  finalize_split_sudoers || {
    rollback_existing_split_upgrade "Runner sudoers installation failed during split upgrade" || true
    return 1
  }
  verify_final_runner_helpers || {
    rollback_existing_split_upgrade "Runner helper verification failed during split upgrade" || true
    return 1
  }
  if ! switch_release_and_restart; then
    rollback_existing_split_upgrade "Could not switch the split installation to the new release" || true
    return 1
  fi
  if ! verify_split_services; then
    rollback_existing_split_upgrade "Split services failed readiness after the release switch" || true
    return 1
  fi
  migration_commit || {
    rollback_existing_split_upgrade "Could not commit the split upgrade" || true
    return 1
  }
  complete_phase
  cleanup_superseded_legacy_install || {
    warn "Split upgrade committed, but superseded legacy files could not be fully removed"
    return 1
  }
}

bootstrap_split_candidate_if_missing() {
  resolve_release_dir
  if [[ -n "$RELEASE_DIR" && -f "$RELEASE_DIR/src/astinuslab/main.py" && -x "$RELEASE_DIR/.venv/bin/python" ]]; then
    return 0
  fi
  info "Bootstrapping a verified single-identity candidate before the split transition..."
  SPLIT_USERS=false
  API_USER="$SERVICE_USER"
  RUNNER_USER="$SERVICE_USER"
  ANALYSIS_USER="$SERVICE_USER"
  ensure_service_user
  begin_phase 4 "Stage release and Python environment"
  install_source
  complete_phase
  begin_phase 5 "Prepare configuration, storage, and databases"
  prepare_storage
  quiesce_services || die "Could not quiesce services before migrating"
  backup_database_before_migration || die "Refusing to migrate without a verified database backup"
  if ! apply_db_migrations; then
    release_rollback "Database migration failed during split bootstrap" || die "Database migration failed and its rollback did not complete"
    die "Database migration failed; the previous database and release were restored"
  fi
  complete_phase
  begin_phase 6 "Configure authentication and guest SSH"
  handle_ssh_key
  complete_phase
  begin_phase 7 "Install privileged host integration"
  resolve_storage_root
  bind_storage_root_identity || die "Could not bind the storage root"
  assert_storage_root_identity || die "Storage root changed before sudoers publication"
  install_sudoers_rule
  assert_storage_root_identity || die "Storage root changed before MAC configuration"
  configure_mac
  complete_phase
  begin_phase 8 "Publish services and verify readiness"
  assert_storage_root_identity || die "Storage root changed before unit publication"
  install_systemd_units
  switch_release_and_restart || die "Could not switch to the bootstrapped release"
  complete_phase
  SPLIT_USERS=true
  API_USER=astinuslab-api
  RUNNER_USER=astinuslab-runner
  ANALYSIS_USER=astinuslab-analysis
}

validate_split_candidate() {
  [[ -f "$RELEASE_DIR/src/astinuslab/main.py" && -x "$RELEASE_DIR/.venv/bin/python" ]] \
    || die "split migration requires an installed candidate"
  resolve_storage_root || return 1
  split_database_layout preflight
}

print_success() {
  echo
  echo "────────────────────────────────────────────────────────────"
  echo " AstinusLab installed successfully"
  echo ""
  local api_port
  api_port="$(awk -F= '$1=="ASTINUSLAB_API_PORT"{print $2}' "$INSTALL_DIR/config/astinuslab.env" 2>/dev/null || true)"
  api_port="${api_port:-8000}"
  if [[ -n "$GENERATED_ADMIN_TOKEN" ]]; then
    echo " CREDENTIALS — SAVE NOW"
    echo "   This token is shown for the final time and is not stored by AstinusLab."
    printf '   Admin token: %s\n' "$GENERATED_ADMIN_TOKEN"
    unset GENERATED_ADMIN_TOKEN
    echo ""
  fi
  echo " REQUIRED BEFORE THE FIRST VM"
  echo "   1. Download approximately $ASTINUSLAB_IMAGE_DOWNLOAD_APPROX_GB GB of compressed images; keep at least $ASTINUSLAB_IMAGE_DOWNLOAD_MIN_FREE_GB GB free:"
  echo "        cd $INSTALL_DIR/current && sudo -u $RUNNER_USER ./ops/images/download_images.sh"
  echo "   2. Verify the running application and host:"
  echo "        cd $INSTALL_DIR/current && ./astinuslab.sh smoke --require-running"
  echo "   3. Open http://localhost:${api_port} and start a Playground session."
  echo ""
  echo " OPTIONAL VOLATILITY 3 ANALYSIS"
  echo "   Volatility 3 and dwarf2json are operator-supplied tools."
  echo "   AstinusLab did not download or install them; review the upstream licenses first. These optional"
  echo "   capabilities remain unavailable until configured. Print the audited guide:"
  echo "        cd $INSTALL_DIR/current && ./astinuslab.sh guide-volatility"
  echo ""
  echo " API:    http://localhost:${api_port}"
  if $SPLIT_USERS; then
    echo " Logs:   journalctl -u astinuslab-api -f"
    echo "         journalctl -u astinuslab-runner -f"
    echo "         journalctl -u astinuslab-analysis -f"
  else
    echo " Logs:   journalctl -u astinuslab-api@${SERVICE_USER} -f"
    echo "         journalctl -u astinuslab-runner@${SERVICE_USER} -f"
    echo "         journalctl -u astinuslab-analysis@${SERVICE_USER} -f"
  fi
  echo ""
  echo " App config:        $INSTALL_DIR/astinuslab.toml"
  echo " Launcher settings: $INSTALL_DIR/config/astinuslab.env"
  echo " Config catalog: $INSTALL_DIR/config/catalog/targets.yaml"
  echo " Storage:        $INSTALL_DIR/storage/"
  echo "────────────────────────────────────────────────────────────"
}

main() {
  CURRENT_PHASE_NUMBER=0
  CURRENT_PHASE_TITLE=""
  REPORTED_INSTALL_PHASES=()
  GENERATED_ADMIN_TOKEN=""
  parse_args "$@"
  if $CHECK_ONLY; then
    run_preflight
    return
  fi
  begin_phase 1 "Validate host and source"
  validate_existing_install_dir_state || die "Unsafe existing install root"
  complete_phase
  begin_phase 2 "Resolve mode, identities, and trusted roots"
  resolve_storage_root || die "Could not resolve storage root"
  validate_install_storage_root || die "Unsafe storage root"
  validate_existing_install_dir_authority || die "Unsafe install-root parent authority"
  verify_storage_root_matches_config
  print_install_plan
  complete_phase
  require_root
  refuse_foreign_template_instances || die "Foreign AstinusLab template instances must be stopped and disabled"
  begin_phase 3 "Install host prerequisites"
  check_prerequisites
  # Repeat the stdlib-only gates after bootstrapping a missing Python. The exact
  # O_NOFOLLOW descriptor walk remains the last authority decision before any
  # install-root mutation.
  validate_existing_install_dir_state || die "Unsafe existing install root"
  validate_install_storage_root || die "Unsafe storage root"
  validate_existing_install_dir_authority || die "Unsafe install-root parent authority"
  verify_storage_root_matches_config
  complete_phase
  if $SPLIT_USERS; then
    API_USER=astinuslab-api
    RUNNER_USER=astinuslab-runner
    ANALYSIS_USER=astinuslab-analysis
    split_database_layout pending || die "Pending database layout requires quiesced recovery"
    if [[ -e "$RUNTIME_IDENTITIES_FILE" || -L "$RUNTIME_IDENTITIES_FILE" ]]; then
      split_identity_marker_is_valid \
        || die "incomplete or malformed split-user state; refusing an update"
      resolve_release_dir
      validate_split_candidate || die "Unsafe configured split database layout"
      upgrade_existing_split_install || die "Split-user upgrade failed and rollback was attempted"
    else
      explicit_split_units_present \
        && die "incomplete split-user state; explicit units exist without a valid marker"
      bootstrap_split_candidate_if_missing
      validate_split_candidate || die "Unsafe configured split database layout"
      migration_snapshot false
      ensure_split_users || { migration_rollback || true; die "Could not create split service identities"; }
      if ! migrate_to_split_users; then
        die "Split-user migration failed and was rolled back"
      fi
    fi
  else
    ensure_service_user
    upgrade_single_identity_install \
      || die "Single-identity install/update failed and rollback was attempted"
  fi
  begin_phase 9 "Present credentials and next actions"
  print_success
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
