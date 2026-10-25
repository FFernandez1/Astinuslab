#!/usr/bin/env bash
# AstinusLab dev control script.
# Usage:
#   ./astinuslab.sh start    — start API + host runner in background
#   ./astinuslab.sh stop     — stop all running processes
#   ./astinuslab.sh cleanup  — destroy all lab VMs/connections and wipe runtime traces
#   ./astinuslab.sh restart  — stop then start
#   ./astinuslab.sh status   — show running state
#   ./astinuslab.sh logs     — tail both log files
#   ./astinuslab.sh setup            — create venv and install dependencies (first run)
#   sudo ./astinuslab.sh setup-host  — install privileged host helpers/sudoers for this checkout
#   ./astinuslab.sh smoke            — verify install/dev checkout without launching VMs
#   ./astinuslab.sh doctor           — alias of smoke: host + checkout readiness checks
#   ./astinuslab.sh guide-volatility — print read-only Volatility 3 setup and readiness guidance
#   ./astinuslab.sh scan-images      — add any new images/definitions/*.qcow2|.img to targets.yaml
#   ./astinuslab.sh scan-images --dry-run  — preview what would be added without writing
#   ./astinuslab.sh precook <image>  — make any image compatible (SSH + guest agent), role auto-detected
#   ./astinuslab.sh start [--docs]   — start API + host runner (optionally with docs site)
set -euo pipefail

# BASE_DIR is the code root; DATA_DIR owns every mutable path. An installed
# release keeps its source under releases/<version>-<digest>/ and is reached
# through the `current` symlink, while storage, config, images and
# astinuslab.toml stay at the install root. `pwd -P` resolves `current` so the
# release directory is recognised. A development checkout has one root for both.
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
if [[ -n "${ASTINUSLAB_DATA_DIR:-}" ]]; then
  DATA_DIR="$ASTINUSLAB_DATA_DIR"
elif [[ "$(basename "$(dirname "$BASE_DIR")")" == "releases" ]]; then
  DATA_DIR="$(cd "$BASE_DIR/../.." && pwd -P)"
else
  DATA_DIR="$BASE_DIR"
fi
# Every child resolves its own paths from this, so a python started out of the
# release still finds the database and catalogs at the install root.
export ASTINUSLAB_DATA_DIR="$DATA_DIR"
VENV="$BASE_DIR/.venv"
LOG_DIR="$DATA_DIR/storage/logs"
API_LOG="$LOG_DIR/api.log"
RUNNER_LOG="$LOG_DIR/runner.log"
ANALYSIS_LOG="$LOG_DIR/analysis-worker.log"
DOCS_LOG="$LOG_DIR/docs.log"
PID_DIR="$DATA_DIR/storage"
API_PID="$PID_DIR/api.pid"
RUNNER_PID="$PID_DIR/runner.pid"
ANALYSIS_PID="$PID_DIR/analysis.pid"
DOCS_PID="$PID_DIR/docs.pid"
ENV_FILE="$DATA_DIR/config/astinuslab.env"
RUNTIME_IDENTITIES_FILE="${ASTINUSLAB_RUNTIME_IDENTITIES_FILE:-/etc/astinuslab/runtime-identities}"
SPLIT_USERS=false
split_identity_marker_is_valid() {
  [[ -f "$RUNTIME_IDENTITIES_FILE" && ! -L "$RUNTIME_IDENTITIES_FILE" ]] || return 1
  local expected actual
  expected=$'mode=split\napi=astinuslab-api\nrunner=astinuslab-runner\nanalysis=astinuslab-analysis\ngroup=astinuslab'
  actual="$(cat -- "$RUNTIME_IDENTITIES_FILE")" || return 1
  [[ "$actual" == "$expected" ]]
}
explicit_split_units_present() {
  local unit
  for unit in astinuslab-api.service astinuslab-runner.service astinuslab-analysis.service; do
    if systemctl cat "$unit" >/dev/null 2>&1 \
      || systemctl is-active --quiet "$unit" 2>/dev/null \
      || systemctl is-enabled --quiet "$unit" 2>/dev/null; then
      return 0
    fi
  done
  return 1
}
if [[ -e "$RUNTIME_IDENTITIES_FILE" || -L "$RUNTIME_IDENTITIES_FILE" ]]; then
  if split_identity_marker_is_valid; then
    SPLIT_USERS=true
  else
    echo "[astinuslab] incomplete or malformed split-user state; refusing operator lifecycle" >&2
    exit 1
  fi
elif explicit_split_units_present; then
  echo "[astinuslab] incomplete or malformed split-user state; explicit split units exist without a valid marker" >&2
  exit 1
fi

# astinuslab.env is sourced with `set -a`, so every ASTINUSLAB_* variable in it
# is exported and overrides astinuslab.toml for the API, worker, and db CLI.
# Precedence: environment variables > astinuslab.toml > code defaults.
if [[ -f "$ENV_FILE" && -r "$ENV_FILE" ]]; then
  set -a
  # Runtime data-root path, validated as readable above.
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

# Default to loopback only. Non-loopback binds require auth unless the unsafe
# override is explicitly enabled. To expose externally in dev mode, set
# ASTINUSLAB_API_HOST=0.0.0.0 in config/astinuslab.env. Exported variables also
# override astinuslab.toml inside the app (see above). These mirror the app
# defaults for URL display; the server itself binds from Settings (env > toml).
API_HOST="${ASTINUSLAB_API_HOST:-127.0.0.1}"
API_PORT="${ASTINUSLAB_API_PORT:-8000}"
DOCS_PORT="${ASTINUSLAB_DOCS_PORT:-9000}"
LIBVIRT_URI="${ASTINUSLAB_LIBVIRT_URI:-qemu:///system}"
LIBVIRT_NETWORK="${ASTINUSLAB_LIBVIRT_NETWORK:-default}"
SMOKE_KVM_DEVICE="${ASTINUSLAB_SMOKE_KVM_DEVICE:-/dev/kvm}"
SMOKE_CPUINFO_PATH="${ASTINUSLAB_SMOKE_CPUINFO_PATH:-/proc/cpuinfo}"
LOCAL_API_URL="http://localhost:${API_PORT}"
LOCAL_DOCS_URL="http://localhost:${DOCS_PORT}"
SERVICE_USER="${ASTINUSLAB_SERVICE_USER:-$(id -un)}"
if $SPLIT_USERS; then
  SYSTEMD_API_UNIT="astinuslab-api.service"
  SYSTEMD_RUNNER_UNIT="astinuslab-runner.service"
  SYSTEMD_ANALYSIS_UNIT="astinuslab-analysis.service"
else
  SYSTEMD_API_UNIT="astinuslab-api@${SERVICE_USER}.service"
  SYSTEMD_RUNNER_UNIT="astinuslab-runner@${SERVICE_USER}.service"
  SYSTEMD_ANALYSIS_UNIT="astinuslab-analysis@${SERVICE_USER}.service"
fi

if ! $SPLIT_USERS; then
  mkdir -p "$LOG_DIR"
  chmod 0700 "$DATA_DIR/storage" "$LOG_DIR" 2>/dev/null || true
  setfacl -m u:libvirt-qemu:x "$DATA_DIR/storage" 2>/dev/null || true
fi

# ── Colour helpers ─────────────────────────────────────────────────────────────
info()  { echo -e "\033[1;34m[astinuslab]\033[0m $*"; }
ok()    { echo -e "\033[1;32m[astinuslab]\033[0m $*"; }
warn()  { echo -e "\033[1;33m[astinuslab]\033[0m $*"; }
die()   { echo -e "\033[1;31m[astinuslab]\033[0m $*" >&2; exit 1; }

# ── Helpers ────────────────────────────────────────────────────────────────────
_venv_ok() { [[ -x "$VENV/bin/python" ]]; }

_is_running() {
  local pidfile="$1"
  [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null
}

_systemd_unit_active() {
  local unit="$1"
  command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$unit" 2>/dev/null
}

_seed_local_config() {
  info "Seeding local config from examples (only if missing)…"
  if [[ ! -f "$DATA_DIR/astinuslab.toml" ]]; then
    cp "$DATA_DIR/astinuslab.toml.example" "$DATA_DIR/astinuslab.toml"
  fi
  local ex live
  for ex in "$BASE_DIR"/config/catalog/*.example.yaml; do
    live="${ex%.example.yaml}.yaml"
    [[ -f "$live" ]] || cp "$ex" "$live"
  done
  if [[ ! -f "$DATA_DIR/config/astinuslab.env" ]]; then
    cp "$BASE_DIR/config/astinuslab.env.example" "$DATA_DIR/config/astinuslab.env"
  fi
}

_active_systemd_units() {
  local unit
  for unit in "$SYSTEMD_API_UNIT" "$SYSTEMD_RUNNER_UNIT" "$SYSTEMD_ANALYSIS_UNIT"; do
    if _systemd_unit_active "$unit"; then
      printf '%s\n' "$unit"
    fi
  done
}

_refuse_if_systemd_active() {
  local active_units=()
  mapfile -t active_units < <(_active_systemd_units)
  if [[ ${#active_units[@]} -eq 0 ]]; then
    return 0
  fi

  warn "Systemd AstinusLab services are already active:"
  local unit
  for unit in "${active_units[@]}"; do
    warn "  · $unit"
  done
  warn "Only one runner mode may be active at a time."
  warn "To use manual mode, stop and disable the systemd services:"
  warn "  sudo systemctl disable --now $SYSTEMD_API_UNIT $SYSTEMD_RUNNER_UNIT $SYSTEMD_ANALYSIS_UNIT"
  die "Manual start refused to avoid duplicate API/runner processes."
}

_stop_proc() {
  local name="$1" pidfile="$2"
  if _is_running "$pidfile"; then
    local pid
    pid="$(cat "$pidfile")"
    kill "$pid" 2>/dev/null && info "Stopped $name (PID $pid)"
    # Wait up to 5s for graceful shutdown
    local i=0
    while kill -0 "$pid" 2>/dev/null && (( i++ < 10 )); do sleep 0.5; done
    kill -9 "$pid" 2>/dev/null || true
    rm -f "$pidfile"
  else
    info "$name is not running"
  fi
}

_maybe_sudo() {
  if [[ $EUID -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

_cleanup_libvirt_vms() {
  local vm

  info "Destroying AstinusLab VMs..."
  while IFS= read -r vm; do
    [[ -z "$vm" ]] && continue
    info "  Destroying VM: $vm"
    virsh -c "$LIBVIRT_URI" destroy "$vm" >/dev/null 2>&1 || true
    virsh -c "$LIBVIRT_URI" undefine "$vm" --nvram >/dev/null 2>&1 || \
      virsh -c "$LIBVIRT_URI" undefine "$vm" >/dev/null 2>&1 || true
  done < <(virsh -c "$LIBVIRT_URI" list --all --name 2>/dev/null | grep "^astinuslab-" || true)
}

_cleanup_libvirt_storage() {
  local libvirt_dir="/var/lib/libvirt/images/astinuslab"
  if [[ -d "$libvirt_dir" ]]; then
    info "Removing $libvirt_dir..."
    _maybe_sudo rm -rf "$libvirt_dir"
  fi
}

_cleanup_libvirt_network() {
  info "Resetting libvirt ${LIBVIRT_NETWORK} network..."
  _maybe_sudo rm -f "/var/lib/libvirt/dnsmasq/${LIBVIRT_NETWORK}.leases" "/var/lib/libvirt/dnsmasq/${LIBVIRT_NETWORK}.status"
  virsh -c "$LIBVIRT_URI" net-destroy "$LIBVIRT_NETWORK" >/dev/null 2>&1 || true
  virsh -c "$LIBVIRT_URI" net-start "$LIBVIRT_NETWORK" >/dev/null 2>&1 || true
  virsh -c "$LIBVIRT_URI" net-autostart "$LIBVIRT_NETWORK" >/dev/null 2>&1 || true
}

_cleanup_runtime_traces() {
  # Removes transient runtime state (logs, tmp, active PIDs, dumps).
  # Does NOT remove workload-cache or ssh_keys — those are persistent state.
  # Use 'uninstall' or confirm the second prompt in 'cleanup' for a full wipe.
  info "Removing runtime traces under storage/..."
  rm -rf \
    "$DATA_DIR/storage/tmp" \
    "$DATA_DIR/storage/logs" \
    "$DATA_DIR/storage/dumps" \
    "$DATA_DIR/storage/celery_broker.db" \
    "$DATA_DIR/storage/celery_results.db" \
    "$DATA_DIR/storage/api.pid" \
    "$DATA_DIR/storage/runner.pid" \
    "$DATA_DIR/storage/analysis.pid"
  mkdir -p "$LOG_DIR"
}

_cleanup_storage_state() {
  info "Removing DB and SSH key state under storage/..."
  rm -rf \
    "$DATA_DIR/storage/astinuslab.db" \
    "$DATA_DIR/storage/ssh_keys"
}

_cleanup_jobs_db() {
  local db="$DATA_DIR/storage/astinuslab.db"
  [[ -f "$db" ]] || return 0
  info "Clearing job records from database..."
  # Goes through the app engine, not the sqlite3 CLI: sqlite3 is not a project
  # prerequisite, and where it was missing this step silently did nothing while
  # reporting success, leaving job_runtimes rows that read as an active
  # forensic job forever. dbcli also keeps ON DELETE CASCADE enforced.
  _venv_ok || { warn "Could not clear job records (venv missing; run: ./astinuslab.sh setup)."; return 0; }
  if "$VENV/bin/python" -m astinuslab.dbcli clear-jobs; then
    ok "Job records cleared."
  else
    warn "Could not clear job records."
  fi
}

# ── Commands ───────────────────────────────────────────────────────────────────
_setup_ssh_key() {
  local keys_dir="$DATA_DIR/storage/ssh_keys"
  local active_file="$keys_dir/.active"
  if [[ -f "$active_file" ]]; then
    ok "Active SSH key: $(cat "$active_file")"
    return
  fi
  echo
  local yn=""
  if [[ -t 0 ]]; then
    read -rp "$(echo -e "\033[1;34m[astinuslab]\033[0m Generate an ED25519 SSH key for AstinusLab? [Y/n] ")" yn
  else
    info "Non-interactive setup: generating default SSH key (answering Y)."
  fi
  if [[ "${yn,,}" == "n" ]]; then
    warn "No SSH key configured. Remember to set one up on the Security page in the UI before launching jobs."
    return
  fi
  local key_name="astinuslab-default"
  local key_path="$keys_dir/$key_name"
  mkdir -p "$keys_dir"
  chmod 0700 "$keys_dir"
  ssh-keygen -t ed25519 -f "$key_path" -N "" -C "astinuslab/$key_name" >/dev/null
  chmod 0600 "$key_path"
  echo "$key_name" > "$active_file"
  ok "Key generated and activated: $key_path"
}

cmd_setup() {
  info "Preflight: checking build prerequisites..."
  command -v python3 >/dev/null 2>&1 || die "python3 not found. Install Python 3.12+ (see docs/usage/running-locally.md#prerequisites)."
  python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' \
    || die "Python 3.12+ required, found: $(python3 -V 2>&1). Install a newer Python and re-run."
  python3 -c 'import ensurepip' 2>/dev/null \
    || die "python3 venv support missing. Debian/Ubuntu: sudo apt install python3-venv"
  if ! command -v pkg-config >/dev/null 2>&1 || ! pkg-config --exists libvirt 2>/dev/null; then
    die "libvirt development headers missing — the libvirt-python dependency cannot build.
  Debian/Ubuntu: sudo apt install pkg-config libvirt-dev
  Fedora/RHEL:   sudo dnf install pkgconf libvirt-devel
  Arch:          sudo pacman -S pkgconf libvirt
  openSUSE:      sudo zypper install pkgconf libvirt-devel"
  fi
  command -v cc >/dev/null 2>&1 || command -v gcc >/dev/null 2>&1 \
    || die "No C compiler found (needed to build libvirt-python). Debian/Ubuntu: sudo apt install build-essential"
  ok "Preflight passed"

  info "Setting up Python venv..."
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  info "Installing dependencies (this can take a few minutes; errors will still print)..."
  "$VENV/bin/python" -m pip install -r "$BASE_DIR/requirements-dev.lock"
  "$VENV/bin/python" -m pip install -e "$BASE_DIR" --no-deps
  _setup_ssh_key
  _seed_local_config
  ok "Setup complete."
  if [[ -f "$BASE_DIR/package.json" && ! -x "$BASE_DIR/node_modules/.bin/eslint" ]]; then
    info "Frontend contributor tools are not installed: run npm ci"
    info "For browser tests: npx playwright install chromium"
  fi
  info "Next (first time on this host): sudo ./astinuslab.sh setup-host --with-mac"
  info "Then: ./astinuslab.sh start && ./astinuslab.sh smoke --require-running"
}

cmd_setup_host() {
  local with_mac=false
  local run_user="${SUDO_USER:-}"
  $SPLIT_USERS && run_user="astinuslab-runner"

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --with-mac|--with-apparmor) with_mac=true; shift ;;
      --run-user) [[ $# -ge 2 ]] || die "--run-user requires USER"; run_user="$2"; shift 2 ;;
      --help|-h)
        cat <<'EOF'
Usage: ./astinuslab.sh setup-host [--with-mac] [--run-user USER]

Install privileged host helpers and sudoers for this checkout.
  --with-mac       Also apply dev storage MAC (AppArmor/SELinux) and traversal ACLs
  --run-user USER   User allowed to run helpers (required when root has no SUDO_USER)
EOF
        return 0
        ;;
      *) die "Unknown setup-host option: $1" ;;
    esac
  done

  if [[ $EUID -ne 0 ]]; then
    die "setup-host requires root. Run as: sudo ./astinuslab.sh setup-host"
  fi
  if [[ -z "$run_user" ]]; then
    die "--run-user must be specified when root has no SUDO_USER"
  fi
  if $SPLIT_USERS && [[ "$run_user" != astinuslab-runner ]]; then
    die "split-user installs grant privileged helpers only to astinuslab-runner"
  fi

  local sudoers_args=(
    --install-dir "$BASE_DIR"
    --run-user "$run_user"
    --helper-source-dir "$BASE_DIR/ops/host"
  )
  if $SPLIT_USERS; then
    sudoers_args+=(--evidence-read-group astinuslab)
  fi
  "$BASE_DIR/ops/host/install_sudoers_astinuslab.sh" "${sudoers_args[@]}"

  if $with_mac; then
    local storage_path apparmor_template curr
    storage_path="$(realpath "$DATA_DIR/storage")"
    apparmor_template="/etc/apparmor.d/libvirt/TEMPLATE.qemu"
    if [[ -f "$apparmor_template" ]] && ! grep -Fq "$storage_path" "$apparmor_template"; then
      python3 - "$apparmor_template" "$storage_path" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
storage = sys.argv[2]
text = path.read_text(encoding="utf-8")
line = f'  "{storage}/**" rwk,'
if line not in text:
    text = text.replace("\n}", f"\n{line}\n}}")
    path.write_text(text, encoding="utf-8")
PY
      systemctl restart apparmor 2>/dev/null || true
      systemctl restart libvirtd 2>/dev/null || true
    fi

    # SELinux (Fedora/RHEL)
    if command -v getenforce >/dev/null 2>&1 && [[ "$(getenforce)" == "Enforcing" ]]; then
      if command -v semanage >/dev/null 2>&1; then
        semanage fcontext -a -t svirt_image_t "${storage_path}(/.*)?" 2>/dev/null || true
        restorecon -Rv "$storage_path" >/dev/null 2>&1 || true
      else
        warn "semanage not found. Install policycoreutils-python-utils to configure SELinux."
      fi
    fi

    curr="$storage_path"
    local qemu_user="libvirt-qemu"
    if ! getent passwd libvirt-qemu >/dev/null 2>&1 && getent passwd qemu >/dev/null 2>&1; then
      qemu_user="qemu"
    fi
    while [[ "$curr" != "/" ]]; do
      if command -v setfacl >/dev/null 2>&1; then
        setfacl -m u:${qemu_user}:x "$curr" 2>/dev/null || true
      else
        chmod o+x "$curr" 2>/dev/null || true
      fi
      curr="$(dirname "$curr")"
    done
    ok "MAC/traversal ACL setup applied for $storage_path"
  else
    warn "Helper setup complete. Real VM launches may still need: sudo ./astinuslab.sh setup-host --with-mac"
  fi
}

_smoke_pass() { ok "smoke: $* OK"; }
_smoke_warn() { warn "smoke: $*"; }
_smoke_fail() { echo -e "\033[1;31m[astinuslab]\033[0m smoke: $*"; SMOKE_FAILURES=$((SMOKE_FAILURES + 1)); }

_probe_host_for_url() {
  local probe_host="$API_HOST"
  if [[ "$probe_host" == "0.0.0.0" ]]; then
    probe_host="127.0.0.1"
  fi
  if [[ "$probe_host" == *:* ]]; then
    echo "http://[${probe_host}]:${API_PORT}"
  else
    echo "http://${probe_host}:${API_PORT}"
  fi
}

# Liveness says the process answered; readiness says it can reach its database
# and writable storage. /ready is on the public allowlist, so no token here.
_smoke_readiness() {
  local url="$1"
  local response code body status unavailable
  if ! command -v curl >/dev/null 2>&1; then
    _smoke_warn "curl not found; skipping readiness check ($url/ready)"
    return
  fi
  # One request returns both: -w appends the status code on its own last line.
  if ! response="$(curl -sS -m 10 -w $'\n%{http_code}' "$url/ready" 2>/dev/null)"; then
    _smoke_fail "API readiness unreachable: $url/ready"
    return
  fi
  code="${response##*$'\n'}"
  body="${response%$'\n'*}"
  # `|| true` on both: under `set -o pipefail` a grep that matches nothing, or a
  # grep closed early by head, would otherwise abort the whole smoke run.
  status="$(printf '%s' "$body" | grep -o '"status":"[a-z_]*"' | head -n 1 | sed 's/.*:"//; s/"$//' || true)"
  # Name the components the operator has to look at, not just the verdict.
  unavailable="$(printf '%s' "$body" \
    | grep -o '"[a-z_]*":{"status":"unavailable"' \
    | sed 's/^"//; s/":{.*$//' \
    | tr '\n' ' ' || true)"
  unavailable="${unavailable% }"

  case "$code:$status" in
    200:ready)
      _smoke_pass "API readiness ($url/ready)"
      ;;
    200:degraded)
      _smoke_warn "API readiness degraded ($url/ready): ${unavailable:-unknown} unavailable"
      ;;
    *:not_ready | 503:*)
      _smoke_fail "API readiness not_ready ($url/ready): ${unavailable:-unknown} unavailable"
      ;;
    *)
      _smoke_fail "API readiness returned an unexpected response (HTTP ${code:-?}): $url/ready"
      ;;
  esac
}

cmd_smoke() {
  local require_running=false
  [[ "${1:-}" == "--require-running" ]] && require_running=true
  SMOKE_FAILURES=0

  if [[ ! -x "$VENV/bin/python" ]]; then
    _smoke_fail "venv missing or not executable: $VENV/bin/python"
    echo "Run: ./astinuslab.sh setup"
  else
    _smoke_pass "venv"
    if "$VENV/bin/python" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
PY
    then _smoke_pass "Python version"; else _smoke_fail "Python 3.12+ required"; fi
    if "$VENV/bin/python" - <<'PY' >/dev/null 2>&1
from astinuslab.main import app

raise SystemExit(0 if app is not None else 1)
PY
    then _smoke_pass "Python import"; else _smoke_fail "astinuslab.main:app import failed"; fi
    if $SPLIT_USERS; then
      _smoke_pass "service-owned config and database checks delegated to authenticated deep readiness"
    elif [[ -f "$DATA_DIR/storage/astinuslab.db" ]]; then
      if "$VENV/bin/python" -m astinuslab.dbcli status >/dev/null 2>&1; then
        _smoke_pass "database status"
      else
        _smoke_fail "database status failed"
      fi
    elif $require_running || [[ "$BASE_DIR" == /opt/astinuslab* || -f "/etc/systemd/system/astinuslab-api@.service" || -f "/etc/systemd/system/astinuslab-api.service" ]]; then
      _smoke_fail "database missing: $DATA_DIR/storage/astinuslab.db; run installer or start once as service user"
    else
      _smoke_warn "database missing; ./astinuslab.sh start initializes it"
    fi
  fi

  if ! $SPLIT_USERS; then
    local cfg missing_config=false
    if [[ ! -r "$DATA_DIR/astinuslab.toml" ]]; then
      _smoke_warn "config missing/unreadable: $DATA_DIR/astinuslab.toml; run ./astinuslab.sh setup to seed from examples"
    fi
    for cfg in \
      "$DATA_DIR/config/astinuslab.env" \
      "$DATA_DIR/config/catalog/targets.yaml" \
      "$DATA_DIR/config/catalog/workloads.yaml" \
      "$DATA_DIR/config/catalog/scenarios.yaml" \
      "$DATA_DIR/config/catalog/attackers.yaml" \
      "$DATA_DIR/config/catalog/profiles.yaml" \
      "$DATA_DIR/config/catalog/network_nodes.yaml"; do
      if [[ ! -r "$cfg" ]]; then
        _smoke_fail "config missing/unreadable: $cfg"
        missing_config=true
      fi
    done
    $missing_config || _smoke_pass "config"
  fi

  if $SPLIT_USERS; then
    _smoke_pass "runner-owned KVM access delegated to authenticated deep readiness"
  elif [[ ! -e "$SMOKE_KVM_DEVICE" ]]; then
    _smoke_fail "KVM not available: $SMOKE_KVM_DEVICE missing (enable VT-x/AMD-V in BIOS, install qemu-kvm)"
  elif [[ ! -r "$SMOKE_KVM_DEVICE" || ! -w "$SMOKE_KVM_DEVICE" ]]; then
    _smoke_warn "$SMOKE_KVM_DEVICE exists but is not accessible by $(id -un); add user to the kvm group"
  else
    _smoke_pass "KVM ($SMOKE_KVM_DEVICE)"
  fi

  if grep -Eq 'vmx|svm' "$SMOKE_CPUINFO_PATH"; then
    _smoke_pass "CPU virtualization flags"
  else
    _smoke_warn "no vmx/svm CPU flags detected; nested or disabled virtualization will be slow or fail"
  fi

  local have_virsh=true have_qemu_img=true
  if ! command -v virsh >/dev/null 2>&1; then
    _smoke_fail "virsh not found (install libvirt-clients)"
    have_virsh=false
  fi
  if ! command -v qemu-img >/dev/null 2>&1; then
    _smoke_fail "qemu-img not found (install qemu-utils)"
    have_qemu_img=false
  fi
  if $have_virsh && $have_qemu_img; then
    _smoke_pass "libvirt/qemu tools"
  fi

  local cpu_cores
  cpu_cores="$(nproc)"
  if (( cpu_cores < 4 )); then
    _smoke_warn "only ${cpu_cores} logical cores; official minimum is 4"
  else
    _smoke_pass "CPU cores (${cpu_cores})"
  fi

  local mem_kb
  mem_kb="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)"
  if (( mem_kb < 16777216 )); then
    _smoke_warn "RAM <16 GB detected; Playground + attacker VM needs 16 GB minimum"
  else
    _smoke_pass "RAM"
  fi

  local disk_free_kb
  disk_free_kb="$(df -Pk "$BASE_DIR" | awk 'NR == 2 {print $4}')"
  if (( disk_free_kb < 157286400 )); then
    _smoke_warn "free disk <150 GB at $BASE_DIR; images, overlays and RAM dumps need 150 GB minimum"
  else
    _smoke_pass "disk free"
  fi

  if $SPLIT_USERS; then
    _smoke_pass "runner-owned libvirt connection and network delegated to authenticated deep readiness"
  elif command -v virsh >/dev/null 2>&1; then
    if virsh -c "$LIBVIRT_URI" version >/dev/null 2>&1; then
      _smoke_pass "libvirt connection ($LIBVIRT_URI)"
      # Capture first: `grep -q` exits on the first match, the producer takes
      # SIGPIPE, and `set -o pipefail` turns that 141 into a failed pipeline -
      # reporting the network inactive while it is running.
      net_info="$(virsh -c "$LIBVIRT_URI" net-info "$LIBVIRT_NETWORK" 2>/dev/null || true)"
      if grep -q '^Active:.*yes' <<<"$net_info"; then
        _smoke_pass "libvirt network '$LIBVIRT_NETWORK' active"
      else
        _smoke_warn "libvirt network '$LIBVIRT_NETWORK' not active; VM networking will fail (try: virsh -c $LIBVIRT_URI net-start $LIBVIRT_NETWORK)"
      fi
    else
      _smoke_fail "cannot connect to libvirt at $LIBVIRT_URI (is libvirtd running? is $(id -un) in the libvirt group? re-login after usermod)"
    fi
  fi

  local image_count
  image_count="$(find "$DATA_DIR/images/definitions" -maxdepth 1 \( -name '*.qcow2' -o -name '*.img' \) 2>/dev/null | wc -l)"
  if (( image_count == 0 )); then
    _smoke_warn "no cloud images in images/definitions/ — run ./ops/images/download_images.sh before launching jobs"
  else
    _smoke_pass "cloud images present (${image_count})"
  fi

  if $SPLIT_USERS; then
    _smoke_warn "privileged helper is runner-owned in split mode; authenticated deep readiness must verify sudo coverage"
  elif sudo -n /usr/local/libexec/astinuslab/libvirt_storage_admin.sh noop >/dev/null 2>&1; then
    _smoke_pass "privileged helper"
  else
    _smoke_fail "privileged helper failed"
    echo "Run: sudo ./astinuslab.sh setup-host"
  fi

  if [[ -x "$VENV/bin/python" ]]; then
    if $SPLIT_USERS; then
      if "$VENV/bin/python" - <<'PY'
from astinuslab.virtualization.host_script_manifest import check_host_scripts

print("Host scripts:")
rows = check_host_scripts()
for row in rows:
    required = "required" if row["required"] else "optional"
    print(f"  {row['name']} [{required}]: {row['status']}")
print("Privileged helpers: runner-owned; inspect authenticated /api/v1/readiness")
raise SystemExit(1 if any(row["required"] and row["status"] != "ok" for row in rows) else 0)
PY
      then
        _smoke_pass "host-script preflight"
      else
        _smoke_fail "host-script preflight failed"
      fi
    elif "$VENV/bin/python" - <<'PY'
from astinuslab.virtualization.host_script_manifest import check_host_scripts, check_privileged_helpers, preflight_problems

print("Host scripts:")
for row in check_host_scripts():
    required = "required" if row["required"] else "optional"
    print(f"  {row['name']} [{required}]: {row['status']}")
print("Privileged helpers:")
for row in check_privileged_helpers():
    status = "ok" if row["installed"] and row["sudo_ok"] else (
        "not installed" if not row["installed"] else "no passwordless sudo"
    )
    print(f"  {row['name']}: {status}")
raise SystemExit(1 if preflight_problems() else 0)
PY
    then
      _smoke_pass "host-script preflight"
    else
      _smoke_fail "host-script preflight failed"
      echo "Run: sudo ./astinuslab.sh setup-host"
    fi
  fi

  local health_url
  health_url="$(_probe_host_for_url)"
  if _is_running "$API_PID" || _systemd_unit_active "$SYSTEMD_API_UNIT"; then
    if command -v curl >/dev/null 2>&1 && curl -fsS "$health_url/health" >/dev/null 2>&1; then
      _smoke_pass "API health ($health_url/health)"
    else
      _smoke_fail "API health failed: $health_url/health"
    fi
    _smoke_readiness "$health_url"
  elif $require_running; then
    _smoke_fail "API is stopped; --require-running was set"
  else
    _smoke_warn "API stopped; skipping health and readiness checks ($health_url)"
  fi

  if $SPLIT_USERS; then
    if ! _systemd_unit_active "$SYSTEMD_API_UNIT"; then
      _smoke_warn "authenticated deep readiness skipped while API is stopped"
    elif [[ -z "${ASTINUSLAB_ADMIN_TOKEN:-}" ]]; then
      _smoke_fail "authenticated deep readiness not checked; export ASTINUSLAB_ADMIN_TOKEN from the install log"
    elif command -v curl >/dev/null 2>&1 \
      && curl -fsS -H "Authorization: Bearer ${ASTINUSLAB_ADMIN_TOKEN}" \
        "$health_url/api/v1/readiness" >/dev/null 2>&1; then
      _smoke_pass "authenticated deep readiness"
    else
      _smoke_fail "authenticated deep readiness failed: $health_url/api/v1/readiness"
    fi
  fi

  if _is_running "$RUNNER_PID" || _systemd_unit_active "$SYSTEMD_RUNNER_UNIT"; then
    _smoke_pass "runner"
  elif $require_running; then
    _smoke_fail "runner is stopped; --require-running was set"
  else
    _smoke_warn "runner stopped; skipping runner liveness check"
  fi

  if _is_running "$ANALYSIS_PID" || _systemd_unit_active "$SYSTEMD_ANALYSIS_UNIT"; then
    _smoke_pass "analysis worker"
  elif $require_running; then
    _smoke_fail "analysis worker is stopped; --require-running was set"
  else
    _smoke_warn "analysis worker stopped; skipping analysis-worker liveness check"
  fi

  if [[ $SMOKE_FAILURES -eq 0 ]]; then
    ok "Smoke checks passed"
    return 0
  fi
  die "Smoke checks failed: $SMOKE_FAILURES"
}

cmd_start() {
  $SPLIT_USERS && die "split-user install must be controlled through its explicit systemd units"
  _venv_ok || die "Venv not found. Run: ./astinuslab.sh setup"
  local with_docs=false
  [[ "${1:-}" == "--docs" ]] && with_docs=true

  _refuse_if_systemd_active

  if $with_docs && ! "$VENV/bin/python" -c "import mkdocs" 2>/dev/null; then
    info "mkdocs not found — installing docs dependencies..."
    "$VENV/bin/pip" install --quiet -e "${BASE_DIR}[docs]" \
      || die "Failed to install docs dependencies. Run: pip install -e .[docs]"
  fi
  info "Applying database migrations..."
  "$VENV/bin/python" -m astinuslab.dbcli init \
    || die "database migration failed; refusing to start"

  if _is_running "$API_PID"; then
    warn "API already running (PID $(cat "$API_PID"))"
  else
    info "Starting API server..."
    # Dev convenience: ASTINUSLAB_DEV=1 enables auto-reload so source edits apply
    # without a manual restart. Scoped to src/astinuslab/ to avoid watching storage/, the
    # venv, etc. Off by default; never enable in production. Reloads the API only,
    # not the Celery worker.
    if [[ "${ASTINUSLAB_DEV:-0}" == "1" ]]; then
      info "Dev mode: API auto-reload enabled (watching src/astinuslab/)"
    fi
    nohup setsid "$VENV/bin/python" -m astinuslab.serve \
      </dev/null >> "$API_LOG" 2>&1 &
    echo $! > "$API_PID"
    ok "API started (PID $(cat "$API_PID")) → $LOCAL_API_URL"
    ok "Log: $API_LOG"
  fi

  if _is_running "$RUNNER_PID"; then
    warn "Celery worker already running (PID $(cat "$RUNNER_PID"))"
  else
    info "Starting Celery worker..."
    # This single runner also produces periodic tasks; analysis has no Beat.
    # PID_DIR is mutable runtime storage, independent of the release directory.
    nohup setsid "$VENV/bin/celery" -A astinuslab.jobs.celery_main worker \
      --loglevel=DEBUG --concurrency=1 --pool=threads \
      --beat --schedule="$PID_DIR/celerybeat-schedule" \
      </dev/null >> "$RUNNER_LOG" 2>&1 &
    echo $! > "$RUNNER_PID"
    ok "Celery worker started (PID $(cat "$RUNNER_PID"))"
    ok "Log: $RUNNER_LOG"
  fi

  if _is_running "$ANALYSIS_PID"; then
    warn "Analysis worker already running (PID $(cat "$ANALYSIS_PID"))"
  else
    info "Starting analysis worker (interactive Vol3 queue)..."
    # Same --concurrency=1 --pool=threads invariant as the runner: per-job
    # vol3 serialization is enforced by blocking_file_lock across processes,
    # but each worker still runs one vol3 at a time. Consumes ONLY -Q analysis.
    nohup setsid "$VENV/bin/celery" -A astinuslab.jobs.celery_main worker \
      -Q analysis -n analysis@%h \
      --loglevel=DEBUG --concurrency=1 --pool=threads \
      </dev/null >> "$ANALYSIS_LOG" 2>&1 &
    echo $! > "$ANALYSIS_PID"
    ok "Analysis worker started (PID $(cat "$ANALYSIS_PID"))"
    ok "Log: $ANALYSIS_LOG"
  fi

  if $with_docs; then
    if _is_running "$DOCS_PID"; then
      warn "Docs server already running (PID $(cat "$DOCS_PID"))"
    else
      info "Starting docs server..."
      nohup setsid "$VENV/bin/python" -m mkdocs serve -f docs/mkdocs.yml --dev-addr "localhost:${DOCS_PORT}" \
        </dev/null >> "$DOCS_LOG" 2>&1 &
      echo $! > "$DOCS_PID"
      ok "Docs server started (PID $(cat "$DOCS_PID")) → $LOCAL_DOCS_URL"
      ok "Log: $DOCS_LOG"
    fi
  fi
}

cmd_stop() {
  _stop_proc "API server"   "$API_PID"
  _stop_proc "Host runner"  "$RUNNER_PID"
  _stop_proc "Analysis worker" "$ANALYSIS_PID"
  _stop_proc "Docs server"  "$DOCS_PID"
  ok "Stopped."
}

cmd_cleanup() {
  $SPLIT_USERS && die "split-user cleanup must be performed through an explicit systemd/operator workflow"
  echo
  warn "This will: stop services, destroy astinuslab-* VMs, reset the ${LIBVIRT_NETWORK} network, clear runtime traces (logs, dumps, tmp), and wipe all job records from the UI."
  warn "VM base images in /var/lib/libvirt/images/astinuslab are preserved."
  echo
  read -rp "$(echo -e "\033[1;33m[astinuslab]\033[0m Continue? Type 'yes' to confirm: ")" confirm
  [[ "$confirm" == "yes" ]] || { info "Cancelled."; exit 0; }

  cmd_stop
  _cleanup_libvirt_vms
  _cleanup_libvirt_network
  _cleanup_runtime_traces
  _cleanup_jobs_db

  echo
  read -rp "$(echo -e "\033[1;34m[astinuslab]\033[0m Also delete the DB and SSH keys? This cannot be undone. [y/N] ")" do_state
  if [[ "${do_state,,}" == "y" ]]; then
    _cleanup_storage_state
  fi

  ok "Cleanup complete."
}

cmd_restart() {
  $SPLIT_USERS && die "split-user install must be controlled through its explicit systemd units"
  local restart_docs=false
  _is_running "$DOCS_PID" && restart_docs=true
  cmd_stop
  sleep 1
  if $restart_docs; then
    cmd_start --docs
  else
    cmd_start
  fi
}

cmd_status() {
  local manual_api=false
  local manual_runner=false
  local manual_analysis=false
  local systemd_api=false
  local systemd_runner=false
  local systemd_analysis=false
  local mode="stopped"

  _is_running "$API_PID" && manual_api=true
  _is_running "$RUNNER_PID" && manual_runner=true
  _is_running "$ANALYSIS_PID" && manual_analysis=true
  _systemd_unit_active "$SYSTEMD_API_UNIT" && systemd_api=true
  _systemd_unit_active "$SYSTEMD_RUNNER_UNIT" && systemd_runner=true
  _systemd_unit_active "$SYSTEMD_ANALYSIS_UNIT" && systemd_analysis=true

  if { $manual_api || $manual_runner || $manual_analysis; } && { $systemd_api || $systemd_runner || $systemd_analysis; }; then
    mode="mixed/conflict"
  elif $systemd_api || $systemd_runner || $systemd_analysis; then
    mode="systemd"
  elif $manual_api || $manual_runner || $manual_analysis; then
    mode="manual"
  fi

  echo
  info "Active runner mode: $mode"

  if _is_running "$API_PID"; then
    ok "Manual API server    running  (PID $(cat "$API_PID")) → $LOCAL_API_URL"
  else
    warn "Manual API server    stopped"
  fi

  if _is_running "$RUNNER_PID"; then
    ok "Manual host runner   running  (PID $(cat "$RUNNER_PID"))"
  else
    warn "Manual host runner   stopped"
  fi

  if _is_running "$ANALYSIS_PID"; then
    ok "Manual analysis wkr  running  (PID $(cat "$ANALYSIS_PID"))"
  else
    warn "Manual analysis wkr  stopped"
  fi

  if $systemd_api; then
    ok "Systemd API service   running  ($SYSTEMD_API_UNIT)"
  else
    info "Systemd API service   stopped  ($SYSTEMD_API_UNIT)"
  fi

  if $systemd_runner; then
    ok "Systemd runner        running  ($SYSTEMD_RUNNER_UNIT)"
  else
    info "Systemd runner        stopped  ($SYSTEMD_RUNNER_UNIT)"
  fi

  if $systemd_analysis; then
    ok "Systemd analysis wkr  running  ($SYSTEMD_ANALYSIS_UNIT)"
  else
    info "Systemd analysis wkr  stopped  ($SYSTEMD_ANALYSIS_UNIT)"
  fi

  if [[ "$mode" == "mixed/conflict" ]]; then
    warn "Mixed runner modes can race for queued jobs. Stop one mode before launching jobs."
    warn "For manual-only mode, run: sudo systemctl disable --now $SYSTEMD_API_UNIT $SYSTEMD_RUNNER_UNIT $SYSTEMD_ANALYSIS_UNIT"
  fi

  if _is_running "$DOCS_PID"; then
    ok "Docs server           running  (PID $(cat "$DOCS_PID")) → $LOCAL_DOCS_URL"
  else
    info "Docs server           stopped"
  fi
  echo
}

cmd_logs() {
  local resolved
  local -a paths=("$API_LOG" "$RUNNER_LOG" "$ANALYSIS_LOG")
  if $SPLIT_USERS; then
    _venv_ok || { warn "Cannot resolve installed logs without the release venv"; return 1; }
    resolved=$("$VENV/bin/python" - <<'PYEOF'
from astinuslab.core.config import settings
from astinuslab.core.log_layout import LOG_FILE_NAMES, log_path
for name in LOG_FILE_NAMES:
    print(log_path(settings.storage_dir, name))
PYEOF
    ) || { warn "Cannot safely resolve installed service logs"; return 1; }
    mapfile -t paths <<< "$resolved"
    [[ ${#paths[@]} -eq 3 ]] || { warn "Invalid installed service log paths"; return 1; }
  fi
  info "Tailing API, runner, and analysis worker logs (Ctrl-C to exit)..."
  tail -f "${paths[@]}" 2>/dev/null \
    || warn "No log files yet — start the services first"
}

cmd_scan_images() {
  _venv_ok || die "Venv not found. Run: ./astinuslab.sh setup"
  local dry_run=false
  [[ "${1:-}" == "--dry-run" ]] && dry_run=true

  info "Scanning images/definitions/ for unregistered images..."
  local result
  local dry_env=""
  $dry_run && dry_env="SCAN_DRY_RUN=1"
  result=$(env $dry_env "$VENV/bin/python" - << 'PYEOF'
import sys
from astinuslab.catalog import _infer_from_filename
from astinuslab.core.config import settings
import yaml, re

images_dir = settings.images_dir
targets_path = settings.targets_catalog_path

# Load existing catalog
existing = {}
if targets_path.exists():
    raw = yaml.safe_load(targets_path.read_text(encoding="utf-8")) or {}
    for t in raw.get("targets", []):
        ref = t.get("image", {}).get("image_ref")
        if ref:
            existing[ref] = t.get("id")

# Scan images
new_entries = []
skipped = []
for path in sorted(images_dir.iterdir()):
    if path.suffix not in {".qcow2", ".img"}:
        continue
    if path.name in existing:
        skipped.append((path.name, existing[path.name]))
        continue
    meta = _infer_from_filename(path.name)
    source_type = path.suffix.lstrip(".")
    target_id = re.sub(r"[^a-z0-9-]", "-", path.stem.lower()).strip("-")[:48]
    # Avoid id collisions
    existing_ids = set(existing.values())
    base_id = target_id
    suffix = 2
    while target_id in existing_ids:
        target_id = f"{base_id}-{suffix}"
        suffix += 1

    needs_pc = meta["machine_type"] == "pc"
    entry = {
        "id": target_id,
        "enabled": True,
        "distro": {"slug": meta["distro"], "version": meta["version"], "family": meta["family"]},
        "image": {
            "source_type": source_type,
            "image_ref": path.name,
            "cloud_init": True,
            "default_username": meta["default_username"],
        },
        "virtualization": {
            "hypervisor": "libvirt",
            "template_name": target_id,
            **( {"os_variant": meta["os_variant"]} if meta["os_variant"] != "generic" else {} ),
            **( {"machine_type": "pc"} if needs_pc else {} ),
            "vcpu": 2,
            "memory_mb": 4096,
            "disk_gb": 40,
            "network_profile": "default",
        },
        "guest_access": {"method": "ssh", "auth": "ssh_key", "serial_console": True, "guest_agent": "optional"},
        "kernel_policy": {"supported_kernels": "distribution_only", "detect_from_guest": True},
        "lifecycle": {"destroy_vm_after_job": True, "keep_failed_vm": False},
    }
    if meta.get("cloud_init_profile"):
        entry["image"]["cloud_init_profile"] = meta["cloud_init_profile"]
    if meta.get("inject_network_config") is not None:
        entry["image"]["inject_network_config"] = meta["inject_network_config"]
    new_entries.append(entry)

# Report skipped
for ref, tid in skipped:
    print(f"SKIP\t{ref}\t{tid}", flush=True)

if not new_entries:
    print("NONE", flush=True)
    sys.exit(0)

# Output new entries as YAML
for e in new_entries:
    print(f"NEW\t{e['image']['image_ref']}\t{e['id']}", flush=True)

# Write or append
if targets_path.exists():
    raw = yaml.safe_load(targets_path.read_text(encoding="utf-8")) or {}
else:
    raw = {"version": 1, "targets": []}

raw.setdefault("targets", [])
raw["targets"].extend(new_entries)

import os
if os.environ.get("SCAN_DRY_RUN") == "1":
    print("DRY_RUN_PREVIEW", flush=True)
    print(yaml.dump({"targets": new_entries}, default_flow_style=False, sort_keys=False, allow_unicode=True), flush=True)
    sys.exit(0)

targets_path.write_text(yaml.dump(raw, default_flow_style=False, sort_keys=False, allow_unicode=True), encoding="utf-8")
print("WROTE\t" + str(targets_path), flush=True)
PYEOF
)

  local exit_code=$?
  [[ $exit_code -ne 0 ]] && { warn "Scan failed."; echo "$result"; return 1; }

  local found_new=false
  while IFS=$'\t' read -r tag ref tid rest; do
    case "$tag" in
      SKIP) info "Already registered: $ref  (id: $tid)" ;;
      NONE) ok "All images already registered in targets.yaml" ;;
      NEW)  ok "New image: $ref  →  id: $tid"; found_new=true ;;
      WROTE) ok "targets.yaml updated: $tid" ;;
      DRY_RUN_PREVIEW)
        # remaining lines in $result after this tag are raw YAML
        echo
        info "--- Preview (dry-run, not written) ---"
        awk '/^DRY_RUN_PREVIEW/{found=1; next} found{print}' <<< "$result"
        found_new=false  # suppress restart hint for dry-run
        break
        ;;
    esac
  done <<< "$result"

  if $found_new && ! $dry_run; then
    info "Restart required to pick up new targets (catalog is cached per-process)."
    info "Run: ./astinuslab.sh restart"
  fi
}

cmd_precook() {
  local script="$BASE_DIR/ops/host/precook_image.sh"
  [[ -x "$script" ]] || die "Missing $script"
  if [[ $# -lt 1 ]]; then
    echo "Usage: $0 precook <image|catalog-id> [--role target|attacker] [--with-packages] [--resolve-only]" >&2
    echo "Makes any image compatible (SSH + qemu-guest-agent + DHCP); role is auto-detected." >&2
    return 1
  fi
  if "$script" "$@"; then
    if [[ "$*" != *--resolve-only* ]]; then
      info "Next: register it — targets via './astinuslab.sh scan-images', attackers by adding the"
      info "output image to config/catalog/attackers.yaml — then './astinuslab.sh restart'."
    fi
  else
    return 1
  fi
}

cmd_guide_volatility() {
  _venv_ok || die "Venv not found. Run: ./astinuslab.sh setup"
  local args=(
    --base-dir "$BASE_DIR"
    --data-dir "$DATA_DIR"
    --service-user "$SERVICE_USER"
    --api-url "$(_probe_host_for_url)"
  )
  $SPLIT_USERS && args+=(--split-users)
  "$VENV/bin/python" -m astinuslab.forensics.vol3_guide "${args[@]}"
}

usage() {
  echo "Usage: $0 {setup|setup-host [--with-mac] [--run-user USER]|smoke|doctor [--require-running]|start [--docs]|stop|cleanup|restart|status|logs|guide-volatility|scan-images [--dry-run]|precook <image> [--role target|attacker] [--with-packages] [--resolve-only]|uninstall}"
}

cmd_uninstall() {
  $SPLIT_USERS && die "split-user uninstall must use: sudo ops/deploy/uninstall.sh"
  echo
  warn "This will remove:"
  warn "  · Running processes (API + runner)"
  warn "  · Virtual environment (.venv/)"
  warn "  · Database, logs, SSH keys (storage/)"
  warn "  · Libvirt VMs created by AstinusLab"
  warn "  · Libvirt storage (/var/lib/libvirt/images/astinuslab/)"
  warn "  · Sudoers rule (/etc/sudoers.d/astinuslab-runner) if present"
  echo
  read -rp "$(echo -e "\033[1;33m[astinuslab]\033[0m Continue? Type 'yes' to confirm: ")" confirm
  [[ "$confirm" == "yes" ]] || { info "Cancelled."; exit 0; }

  # ── 1. Detener servicios ───────────────────────────────────────────────────
  info "Stopping services..."
  cmd_stop

  # ── 2. Limpiar VMs/red/artefactos de libvirt ──────────────────────────────
  _cleanup_libvirt_vms
  _cleanup_libvirt_storage
  _cleanup_libvirt_network

  # ── 3. Regla sudoers ──────────────────────────────────────────────────────
  if [[ -f "/etc/sudoers.d/astinuslab-runner" ]]; then
    info "Removing sudoers rule..."
    if _maybe_sudo rm -f /etc/sudoers.d/astinuslab-runner; then
      ok "  Removed"
    else
      warn "  Could not remove"
    fi
  fi

  # ── 4. Entorno virtual ────────────────────────────────────────────────────
  info "Removing .venv/..."
  rm -rf "$BASE_DIR/.venv"

  # ── 5. Storage (DB, logs, cache, keys, tmp) ───────────────────────────────
  info "Removing storage/..."
  rm -rf \
    "$DATA_DIR/storage/astinuslab.db" \
    "$DATA_DIR/storage/celery_broker.db" \
    "$DATA_DIR/storage/celery_results.db" \
    "$DATA_DIR/storage/logs" \
    "$DATA_DIR/storage/tmp" \
    "$DATA_DIR/storage/ssh_keys" \
    "$DATA_DIR/storage/dumps" \
    "$DATA_DIR/storage/workload-cache" \
    "$DATA_DIR/storage/api.pid" \
    "$DATA_DIR/storage/runner.pid"

  # ── 6. Disk images (optional) ─────────────────────────────────────────────
  IMAGES_DIR="$DATA_DIR/images/definitions"
  if [[ -n "$(ls "$IMAGES_DIR" 2>/dev/null)" ]]; then
    echo
    read -rp "$(echo -e "\033[1;33m[astinuslab]\033[0m Also delete disk images under images/definitions/? [y/N] ")" del_images
    if [[ "${del_images,,}" == "y" ]]; then
      rm -f "$IMAGES_DIR"/*.qcow2 "$IMAGES_DIR"/*.img "$IMAGES_DIR"/*.iso \
            "$IMAGES_DIR"/*.raw "$IMAGES_DIR"/*.vmdk 2>/dev/null || true
      ok "  Images deleted"
    else
      info "  Images kept in $IMAGES_DIR"
    fi
  fi

  echo
  ok "Uninstall complete. The project directory remains intact."
}

# ── Dispatch ───────────────────────────────────────────────────────────────────
case "${1:-}" in
  setup)        cmd_setup     ;;
  setup-host)   shift; cmd_setup_host "$@" ;;
  smoke|doctor) cmd_smoke "${2:-}" ;;
  start)        cmd_start "${2:-}"  ;;
  stop)         cmd_stop      ;;
  cleanup)      cmd_cleanup   ;;
  restart)      cmd_restart   ;;
  status)       cmd_status    ;;
  logs)         cmd_logs      ;;
  guide-volatility) cmd_guide_volatility ;;
  scan-images)  cmd_scan_images "${2:-}" ;;
  precook)      shift; cmd_precook "$@" ;;
  uninstall)    cmd_uninstall ;;
  help|-h|--help) usage ;;
  *)
    usage
    exit 1
    ;;
esac
