#!/usr/bin/env bash
# Register root-administered storage policy and render matching systemd units.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The documented invocation is the copy inside the installed release, so the
# directory above this script is the release, not the install root. The install
# root is the data root: it owns storage, config and the `current` symlink, and
# it must not move when a release does.
_default_install_dir() {
  local candidate="$1" parent
  case "$candidate" in
    */current) printf '%s\n' "${candidate%/current}"; return ;;
  esac
  parent="$(dirname "$candidate")"
  if [[ "$(basename "$parent")" == releases ]]; then
    printf '%s\n' "$(dirname "$parent")"
    return
  fi
  printf '%s\n' "$candidate"
}
INSTALL_DIR="$(_default_install_dir "$(cd "$SCRIPT_DIR/../.." && pwd)")"
# Set from INSTALL_DIR once the arguments are parsed: the product source is
# whichever release `current` points at, or the checkout itself in development.
SOURCE_DIR="$INSTALL_DIR"
STORAGE_ROOT=""
STORAGE_ROOT_IDENTITY=""
INSTALL_SOURCE_AUTHORITY=""
POLICY_FILE="${ASTINUSLAB_POLICY_FILE:-/etc/astinuslab/acl-policy.json}"
SYSTEMD_DIR="${ASTINUSLAB_SYSTEMD_DIR:-/etc/systemd/system}"
CHECK_ONLY=false
RESTART=false
SERVICE_USER="${SUDO_USER:-${USER:-astinuslab}}"
RUNTIME_IDENTITIES_FILE="${ASTINUSLAB_RUNTIME_IDENTITIES_FILE:-/etc/astinuslab/runtime-identities}"
TRAVERSAL_ACL_MANIFEST="${ASTINUSLAB_TRAVERSAL_ACL_MANIFEST:-/etc/astinuslab/traversal-acl.manifest}"
MAC_POLICY_MANIFEST="${ASTINUSLAB_MAC_POLICY_MANIFEST:-/etc/astinuslab/mac-policy.manifest}"
SPLIT_USERS=false
API_USER="$SERVICE_USER"
RUNNER_USER="$SERVICE_USER"
ANALYSIS_USER="$SERVICE_USER"
UNITS=(astinuslab-api astinuslab-runner astinuslab-analysis)

# Cleanup state is script scope, not local to main: the EXIT trap runs after
# main has returned, so locals are already out of scope and `set -u` aborts the
# trap before it deletes anything - leaving temporary policy and unit backups
# behind in /etc and exiting non-zero from an otherwise successful run.
POLICY_TMP=""
POLICY_BACKUP=""
RENDERED=()
BACKUPS=()
BACKUP_EXISTED=()
ACL_MANIFEST_BEFORE=""
MAC_MANIFEST_BEFORE=""
HOST_ACCESS_TRANSACTION_LOG="${ASTINUSLAB_HOST_ACCESS_TRANSACTION_LOG:-}"
ACL_MANIFEST_EXISTED=false
MAC_MANIFEST_EXISTED=false
SERVICE_WAS_ACTIVE=()
POLICY_EXISTED=false

cleanup_artifacts() {
  rm -f -- "${POLICY_TMP:-}" "${POLICY_BACKUP:-}" "${ACL_MANIFEST_BEFORE:-}" \
    "${MAC_MANIFEST_BEFORE:-}" "${HOST_ACCESS_TRANSACTION_LOG:-}" \
    "${RENDERED[@]:-}" "${BACKUPS[@]:-}" 2>/dev/null || true
}

restore_previous_configuration() {
  local i=0 unit
  for unit in "${UNITS[@]}"; do
    if [[ "${BACKUP_EXISTED[$i]}" == true ]]; then
      restore_managed_unit "${BACKUPS[$i]}" "$SYSTEMD_DIR/$(unit_filename "$unit")" || return 1
    else
      rm -f -- "$SYSTEMD_DIR/$(unit_filename "$unit")" "${BACKUPS[$i]}" || return 1
    fi
    i=$((i + 1))
  done
  if $POLICY_EXISTED; then
    rm -f -- "$POLICY_FILE" || return 1
    mv -T -- "$POLICY_BACKUP" "$POLICY_FILE" || return 1
  else
    rm -f -- "$POLICY_FILE" || return 1
  fi
  systemctl daemon-reload || true
}

publish_rendered_units() {
  local i=0 unit
  for unit in "${UNITS[@]}"; do
    mv -f -- "${RENDERED[$i]}" "$SYSTEMD_DIR/$(unit_filename "$unit")" || return 1
    i=$((i + 1))
  done
  RENDERED=()
}

publish_rendered_configuration() {
  if ! publish_rendered_units; then
    restore_previous_configuration || return 2
    return 1
  fi
  if ! assert_storage_root_identity; then
    restore_previous_configuration || return 2
    return 1
  fi
}

usage() {
  cat <<'EOF'
Usage: configure-privileged-roots.sh [options]

Options:
  --install-dir PATH    AstinusLab install root (the data root holding `current`)
  --storage-root PATH   Existing non-symlink storage root to register
  --check               Report policy/unit mismatch without changing anything
  --restart             Restart AstinusLab services after successful update
  -h, --help            Show this help
EOF
}

die() { echo "configure-privileged-roots.sh: $*" >&2; exit 1; }

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --install-dir) [[ $# -ge 2 ]] || die "--install-dir requires PATH"; INSTALL_DIR="$2"; shift 2 ;;
      --storage-root) [[ $# -ge 2 ]] || die "--storage-root requires PATH"; STORAGE_ROOT="$2"; shift 2 ;;
      --check) CHECK_ONLY=true; shift ;;
      --restart) RESTART=true; shift ;;
      -h|--help) usage; exit 0 ;;
      *) die "unknown option: $1" ;;
    esac
  done
  [[ "$INSTALL_DIR" == /* ]] || die "--install-dir must be an absolute path"
  [[ -n "$STORAGE_ROOT" ]] || STORAGE_ROOT="$INSTALL_DIR/storage"
  [[ "$STORAGE_ROOT" == /* ]] || die "--storage-root must be an absolute path"
  [[ -d "$STORAGE_ROOT" && ! -L "$STORAGE_ROOT" ]] || die "--storage-root must be an existing non-symlink directory"
  # This is path selection only; main binds and validates the physical source
  # before any import, template read, or sourced shell execution.
  SOURCE_DIR="$INSTALL_DIR"
  [[ ! -d "$INSTALL_DIR/current/ops/systemd" ]] || SOURCE_DIR="$INSTALL_DIR/current"
  [[ -d "$SOURCE_DIR/ops/systemd" ]] || die "systemd templates not found under --install-dir"
}

inspect_install_source_authority() {
  python3 - "$INSTALL_DIR" <<'PY'
import os
import stat
import sys

install_path = os.path.normpath(sys.argv[1])
if not os.path.isabs(install_path):
    raise SystemExit("install root must be absolute")
if any(part in {".", ".."} for part in install_path.split("/") if part):
    raise SystemExit("install root must not contain dot path components")
directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


def require_trusted_directory(info: os.stat_result, label: str) -> None:
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != 0
        or info.st_gid != 0
        or info.st_mode & 0o022
    ):
        raise SystemExit(f"{label} must be root:root and non-writable by group/other")


def open_trusted_absolute(path: str) -> int:
    descriptor = os.open("/", directory_flags)
    require_trusted_directory(os.fstat(descriptor), "/")
    try:
        for component in [part for part in path.split("/") if part]:
            child = os.open(component, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            require_trusted_directory(os.fstat(descriptor), path)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


install_fd = open_trusted_absolute(install_path)
try:
    current_path = os.path.join(install_path, "current")
    if os.path.lexists(current_path):
        current_info = os.lstat(current_path)
        if not stat.S_ISLNK(current_info.st_mode) or current_info.st_uid != 0:
            raise SystemExit("install current entry must be a root-owned symlink")
        source_path = os.path.realpath(current_path)
        releases_path = os.path.realpath(os.path.join(install_path, "releases"))
        if os.path.commonpath((source_path, releases_path)) != releases_path or source_path == releases_path:
            raise SystemExit("install current symlink must resolve to a child of INSTALL_DIR/releases")
    else:
        source_path = install_path

    source_fd = open_trusted_absolute(source_path)
    try:
        required_files = (
            "ops/host/acl_policy.py",
            "ops/deploy/install.sh",
            "ops/systemd/astinuslab-api.service",
            "ops/systemd/astinuslab-runner.service",
            "ops/systemd/astinuslab-analysis.service",
        )
        for relative in required_files:
            parent_fd = os.dup(source_fd)
            try:
                components = relative.split("/")
                for component in components[:-1]:
                    child = os.open(component, directory_flags, dir_fd=parent_fd)
                    os.close(parent_fd)
                    parent_fd = child
                    require_trusted_directory(os.fstat(parent_fd), f"source path {relative}")
                leaf_fd = os.open(components[-1], file_flags, dir_fd=parent_fd)
                try:
                    info = os.fstat(leaf_fd)
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_uid != 0
                        or info.st_gid != 0
                        or info.st_nlink != 1
                        or info.st_mode & 0o022
                    ):
                        raise SystemExit(f"source file is not protected root-owned content: {relative}")
                finally:
                    os.close(leaf_fd)
            finally:
                os.close(parent_fd)
        install_info = os.fstat(install_fd)
        source_info = os.fstat(source_fd)
        print(f"{install_info.st_dev}:{install_info.st_ino}|{source_info.st_dev}:{source_info.st_ino}|{source_path}")
    finally:
        os.close(source_fd)
finally:
    os.close(install_fd)
PY
}

bind_install_source_authority() {
  INSTALL_SOURCE_AUTHORITY="$(inspect_install_source_authority)" || return 1
  SOURCE_DIR="${INSTALL_SOURCE_AUTHORITY#*|}"
  SOURCE_DIR="${SOURCE_DIR#*|}"
  [[ -n "$INSTALL_SOURCE_AUTHORITY" && -n "$SOURCE_DIR" ]]
}

assert_install_source_authority() {
  [[ -n "$INSTALL_SOURCE_AUTHORITY" ]] || return 1
  [[ "$(inspect_install_source_authority)" == "$INSTALL_SOURCE_AUTHORITY" ]]
}

detect_runtime_identities() {
  if [[ -e "$RUNTIME_IDENTITIES_FILE" || -L "$RUNTIME_IDENTITIES_FILE" ]]; then
    split_identity_marker_is_valid \
      || die "incomplete, malformed, or unprotected split-user runtime marker"
    SPLIT_USERS=true
    API_USER=astinuslab-api
    RUNNER_USER=astinuslab-runner
    ANALYSIS_USER=astinuslab-analysis
    SERVICE_USER="$RUNNER_USER"
  elif explicit_split_units_present; then
    die "explicit split units exist without a valid runtime marker"
  fi
}

split_identity_marker_is_valid() {
  python3 "$SCRIPT_DIR/privileged_state.py" split-identity-marker "$RUNTIME_IDENTITIES_FILE"
}

explicit_split_units_present() {
  local unit
  for unit in astinuslab-api.service astinuslab-runner.service astinuslab-analysis.service; do
    if [[ -e "$SYSTEMD_DIR/$unit" || -L "$SYSTEMD_DIR/$unit" ]] \
      || systemctl cat "$unit" >/dev/null 2>&1 \
      || systemctl is-active --quiet "$unit" 2>/dev/null \
      || systemctl is-enabled --quiet "$unit" 2>/dev/null; then
      return 0
    fi
  done
  return 1
}

unit_filename() {
  local unit="$1"
  if $SPLIT_USERS; then printf '%s.service\n' "$unit"; else printf '%s@.service\n' "$unit"; fi
}

service_unit_name() {
  local unit="$1"
  if $SPLIT_USERS; then printf '%s.service\n' "$unit"; else printf '%s@%s.service\n' "$unit" "$SERVICE_USER"; fi
}

validate_root_with_authority() {
  assert_install_source_authority || return 1
  PYTHONPATH="$SOURCE_DIR/ops/host${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -B - "$STORAGE_ROOT" "$INSTALL_DIR" <<'PY'
import sys
from acl_policy import validate_root, validate_storage_root_for_install
try:
    validate_root(sys.argv[1])
    validate_storage_root_for_install(sys.argv[1], sys.argv[2])
except ValueError as exc:
    raise SystemExit(str(exc))
PY
}

bind_storage_root_identity() {
  validate_root_with_authority || return 1
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
  [[ -n "$STORAGE_ROOT_IDENTITY" ]] || return 1
  validate_root_with_authority || return 1
  python3 - "$STORAGE_ROOT" "$STORAGE_ROOT_IDENTITY" <<'PY'
import os
import stat
import sys

info = os.lstat(sys.argv[1])
if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
    raise SystemExit("storage root changed type after validation")
if f"{info.st_dev}:{info.st_ino}" != sys.argv[2]:
    raise SystemExit("storage root inode changed after validation")
PY
}

snapshot_managed_unit() {
  local source="$1" destination="$2"
  if [[ -L "$source" ]]; then
    [[ "$(readlink -- "$source")" == /dev/null ]] || {
      echo "unsupported systemd unit symlink: $source" >&2
      return 1
    }
  elif [[ ! -f "$source" ]]; then
    echo "unsupported systemd unit state: $source" >&2
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

policy_payload() {
  assert_install_source_authority || return 1
  PYTHONPATH="$SOURCE_DIR/ops/host${PYTHONPATH:+:$PYTHONPATH}" python3 -B - "$STORAGE_ROOT" "$SERVICE_USER" "$SPLIT_USERS" <<'PY'
import json
import stat
import sys
from pathlib import Path
from acl_policy import normalize_roots

root = normalize_roots([sys.argv[1]])[0]
info = Path(root).stat()
group = info.st_gid
import grp
group_name = grp.getgrgid(group).gr_name if (info.st_mode & stat.S_IWGRP) else None
print(json.dumps({
    "schema_version": "astinuslab-acl-policy/v1",
    "allowed_callers": [sys.argv[2]],
    "allowed_roots": [root],
    "storage_groups": [group_name] if group_name else [],
    "evidence_read_groups": ["astinuslab"] if sys.argv[3] == "true" else [],
}, indent=2) + "\n")
PY
}

render_unit() {
  assert_install_source_authority || return 1
  local unit="$1" destination="$2" run_user="%i" run_group="%i"
  local api_unit="astinuslab-api@%i.service"
  if $SPLIT_USERS; then
    run_group=astinuslab
    api_unit=astinuslab-api.service
    case "$unit" in
      astinuslab-api) run_user="$API_USER" ;;
      astinuslab-runner) run_user="$RUNNER_USER" ;;
      astinuslab-analysis) run_user="$ANALYSIS_USER" ;;
    esac
  fi
  python3 - "$SOURCE_DIR/ops/systemd/${unit}.service" "$destination" "$INSTALL_DIR" "$STORAGE_ROOT" "$run_user" "$run_group" "$api_unit" "$unit" "$SPLIT_USERS" <<'PY'
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
    text = text.replace(" /var/lib/libvirt/images", "")
Path(dst).write_text(text, encoding="utf-8")
PY
}

json_matches() {
  [[ -f "$POLICY_FILE" ]] || return 1
  PYTHONPATH="$SOURCE_DIR/ops/host${PYTHONPATH:+:$PYTHONPATH}" python3 -B - "$POLICY_FILE" "$STORAGE_ROOT" "$SERVICE_USER" "$SPLIT_USERS" <<'PY'
import grp
import stat
import sys
from pathlib import Path
from acl_policy import load_policy, normalize_roots
try:
    policy = load_policy(Path(sys.argv[1]))
    root = normalize_roots([sys.argv[2]])[0]
except ValueError:
    raise SystemExit(1)
# policy_payload names the storage root's group only while the root is
# group-writable, and the ACL helper refuses a group-writable path component no
# policy names. A root whose mode or group changed after registration therefore
# fails every job while the rest of the policy still matches, so the field has
# to be compared here - exactly, in both directions, never widened.
info = Path(root).stat()
expected_storage_groups = []
if info.st_mode & stat.S_IWGRP:
    try:
        expected_storage_groups = [grp.getgrgid(info.st_gid).gr_name]
    except KeyError:
        # An unnamed owning group is not a group any policy can name.
        raise SystemExit(1)
raise SystemExit(not (
    policy["allowed_roots"] == [str(Path(sys.argv[2]))]
    and policy["allowed_callers"] == [sys.argv[3]]
    and policy["storage_groups"] == expected_storage_groups
    and policy["evidence_read_groups"] == (["astinuslab"] if sys.argv[4] == "true" else [])
))
PY
}

check() {
  local mismatch=false unit expected actual
  json_matches || mismatch=true
  for unit in "${UNITS[@]}"; do
    expected="$(mktemp)"
    render_unit "$unit" "$expected"
    actual="$SYSTEMD_DIR/$(unit_filename "$unit")"
    if [[ ! -f "$actual" ]] || ! cmp -s "$expected" "$actual"; then mismatch=true; fi
    rm -f -- "$expected"
  done
  if $mismatch; then
    echo "mismatch: policy or rendered systemd units differ; run configure-privileged-roots.sh with --storage-root $STORAGE_ROOT";
    return 1
  fi
  echo "policy and systemd units match $STORAGE_ROOT"
}

atomic_install() {
  local source="$1" destination="$2" mode="$3" temporary
  temporary="$(mktemp "${destination}.tmp.XXXXXX")"
  cp -- "$source" "$temporary"
  chmod "$mode" "$temporary"
  chown root:root "$temporary"
  python3 - "$temporary" <<'PY'
import os, sys
with open(sys.argv[1], "rb") as handle:
    os.fsync(handle.fileno())
PY
  mv -f -- "$temporary" "$destination"
}

configure_host_access() {
  assert_install_source_authority || return 1
  # Reuse the installer's descriptor-safe, manifest-backed host changes. Run it
  # in an isolated shell because install.sh and this helper intentionally share
  # several global names and shell helpers.
  INSTALL_DIR="$INSTALL_DIR" STORAGE_ROOT="$STORAGE_ROOT" \
    ASTINUSLAB_TRAVERSAL_ACL_MANIFEST="$TRAVERSAL_ACL_MANIFEST" \
    ASTINUSLAB_MAC_POLICY_MANIFEST="$MAC_POLICY_MANIFEST" \
    ASTINUSLAB_HOST_ACCESS_TRANSACTION_LOG="$HOST_ACCESS_TRANSACTION_LOG" \
    bash -euo pipefail -c 'source "$1"; configure_mac' \
      configure-privileged-roots "$SOURCE_DIR/ops/deploy/install.sh"
}

snapshot_host_access_manifests() {
  ACL_MANIFEST_BEFORE="$(mktemp)" || return 1
  MAC_MANIFEST_BEFORE="$(mktemp)" || return 1
  HOST_ACCESS_TRANSACTION_LOG="$(mktemp)" || return 1
  chmod 0600 "$HOST_ACCESS_TRANSACTION_LOG" || return 1
  ACL_MANIFEST_EXISTED=false
  MAC_MANIFEST_EXISTED=false
  local current snapshot existed_name
  for current in "$TRAVERSAL_ACL_MANIFEST" "$MAC_POLICY_MANIFEST"; do
    if [[ "$current" == "$TRAVERSAL_ACL_MANIFEST" ]]; then
      snapshot="$ACL_MANIFEST_BEFORE"
      existed_name=ACL_MANIFEST_EXISTED
    else
      snapshot="$MAC_MANIFEST_BEFORE"
      existed_name=MAC_MANIFEST_EXISTED
    fi
    if [[ -e "$current" || -L "$current" ]]; then
      python3 - "$current" "$snapshot" <<'PY' || return 1
import os
import stat
import sys

source, destination = sys.argv[1:]
fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
try:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise SystemExit(f"manifest is not a protected regular installer-owned file: {source}")
    with os.fdopen(os.dup(fd), "rb") as handle, open(destination, "wb") as output:
        output.write(handle.read())
        output.flush()
        os.fsync(output.fileno())
finally:
    os.close(fd)
PY
      printf -v "$existed_name" true
    else
      : > "$snapshot"
    fi
  done
}

rollback_host_access_delta() {
  [[ -n "$ACL_MANIFEST_BEFORE" && -n "$MAC_MANIFEST_BEFORE" ]] || return 1
  local status=0
  if ! python3 "$SCRIPT_DIR/host_access.py" rollback \
    "$TRAVERSAL_ACL_MANIFEST" "$ACL_MANIFEST_BEFORE" "$ACL_MANIFEST_EXISTED" \
    "$MAC_POLICY_MANIFEST" "$MAC_MANIFEST_BEFORE" "$MAC_MANIFEST_EXISTED" "$STORAGE_ROOT" \
    "$HOST_ACCESS_TRANSACTION_LOG"
  then
    status=1
  fi
  return "$status"
}

snapshot_service_runtime() {
  SERVICE_WAS_ACTIVE=()
  local unit
  for unit in "${UNITS[@]}"; do
    if systemctl is-active --quiet "$(service_unit_name "$unit")" 2>/dev/null; then
      SERVICE_WAS_ACTIVE+=(true)
    else
      SERVICE_WAS_ACTIVE+=(false)
    fi
  done
}

restore_previous_runtime() {
  local i=0 unit service failed=false
  for unit in "${UNITS[@]}"; do
    service="$(service_unit_name "$unit")"
    if [[ "${SERVICE_WAS_ACTIVE[$i]:-false}" == true ]]; then
      systemctl restart "$service" || failed=true
    elif systemctl is-active --quiet "$service" 2>/dev/null; then
      systemctl stop "$service" || failed=true
    fi
    i=$((i + 1))
  done
  $failed && return 1
  return 0
}

run_configured_deep_readiness() {
  assert_install_source_authority || return 1
  local readiness_user="$SERVICE_USER" env_file="$INSTALL_DIR/config/astinuslab.env"
  $SPLIT_USERS && readiness_user="$API_USER"
  runuser -u "$readiness_user" -- env -i \
    ASTINUSLAB_DATA_DIR="$INSTALL_DIR" PATH=/usr/bin:/bin \
    /usr/bin/python3 - "$env_file" "$SOURCE_DIR/.venv/bin/python" <<'PY'
import os
import re
import shlex
import sys
from pathlib import Path

env_path, release_python = sys.argv[1:]
environment = os.environ.copy()
path = Path(env_path)
if path.is_file():
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        if "=" not in stripped:
            raise SystemExit(f"invalid environment assignment at {path}:{number}")
        key, raw_value = stripped.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise SystemExit(f"invalid environment name at {path}:{number}")
        values = shlex.split(raw_value, comments=False, posix=True)
        environment[key] = " ".join(values) if values else ""
readiness_code = '''
import time
from astinuslab.readiness import collect_readiness, mandatory_runtime_ready
for attempt in range(4):
    report = collect_readiness()
    if mandatory_runtime_ready(report):
        raise SystemExit(0)
    if attempt != 3:
        time.sleep(2)
print(report.public_dict())
raise SystemExit(1)
'''
os.execve(
    "/usr/bin/timeout",
    ["/usr/bin/timeout", "60s", release_python, "-c", readiness_code],
    environment,
)
PY
}

verify_restarted_runtime() {
  local attempt unit
  for attempt in 1 2 3 4 5; do
    for unit in "${UNITS[@]}"; do
      systemctl is-active --quiet "$(service_unit_name "$unit")" || return 1
    done
    [[ "$attempt" == 5 ]] || sleep 1 || return 1
  done
  run_configured_deep_readiness
}

main() {
  parse_args "$@"
  bind_install_source_authority || die "unsafe install/source authority"
  detect_runtime_identities
  validate_root_with_authority || die "storage root rejected by acl_policy.py"
  if $CHECK_ONLY; then check; return $?; fi
  [[ $EUID -eq 0 ]] || die "run as root: sudo ops/deploy/configure-privileged-roots.sh ..."
  bind_storage_root_identity || die "could not bind the validated storage root"
  [[ "$POLICY_FILE" == /* && "$SYSTEMD_DIR" == /* ]] || die "policy and systemd paths must be absolute"
  install -d -o root -g root -m 0755 "$(dirname "$POLICY_FILE")" "$SYSTEMD_DIR"

  local unit tmp
  POLICY_EXISTED=false
  POLICY_TMP="$(mktemp "${POLICY_FILE}.tmp.XXXXXX")"
  policy_payload > "$POLICY_TMP"
  python3 - "$POLICY_TMP" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle: json.load(handle)
PY
  chmod 0644 "$POLICY_TMP"
  chown root:root "$POLICY_TMP"
  python3 - "$POLICY_TMP" <<'PY'
import os, sys
with open(sys.argv[1], "rb") as handle: os.fsync(handle.fileno())
PY

  POLICY_BACKUP="$(mktemp "${POLICY_FILE}.backup.XXXXXX")"
  if [[ -e "$POLICY_FILE" || -L "$POLICY_FILE" ]]; then
    [[ -f "$POLICY_FILE" && ! -L "$POLICY_FILE" ]] || die "unsupported prior ACL policy state"
    rm -f -- "$POLICY_BACKUP"
    cp -a --no-dereference -- "$POLICY_FILE" "$POLICY_BACKUP"
    POLICY_EXISTED=true
  else
    : > "$POLICY_BACKUP"
  fi

  RENDERED=()
  for unit in "${UNITS[@]}"; do
    tmp="$(mktemp "${SYSTEMD_DIR}/$(unit_filename "$unit").tmp.XXXXXX")"
    render_unit "$unit" "$tmp"
    chmod 0644 "$tmp"; chown root:root "$tmp"
    python3 - "$tmp" <<'PY'
import os, sys
with open(sys.argv[1], "rb") as handle: os.fsync(handle.fileno())
PY
    RENDERED+=("$tmp")
  done
  # Units are prepared before policy replacement. If policy replacement fails,
  # restore prior unit bytes so policy and sandbox cannot diverge.
  BACKUPS=()
  BACKUP_EXISTED=()
  trap cleanup_artifacts EXIT
  for unit in "${UNITS[@]}"; do
    tmp="$(mktemp "${SYSTEMD_DIR}/$(unit_filename "$unit").backup.XXXXXX")"
    if [[ -e "$SYSTEMD_DIR/$(unit_filename "$unit")" || -L "$SYSTEMD_DIR/$(unit_filename "$unit")" ]]; then
      snapshot_managed_unit "$SYSTEMD_DIR/$(unit_filename "$unit")" "$tmp" \
        || die "unsupported prior systemd unit state"
      BACKUP_EXISTED+=(true)
    else
      : > "$tmp"
      BACKUP_EXISTED+=(false)
    fi
    BACKUPS+=("$tmp")
  done
  if ! publish_rendered_configuration; then
    die "unit publication or storage-root identity check failed; prior policy and units restored"
  fi
  if ! mv -f -- "$POLICY_TMP" "$POLICY_FILE"; then
    restore_previous_configuration
    die "could not replace policy; prior policy and units restored"
  fi
  if ! systemctl daemon-reload; then
    restore_previous_configuration
    die "systemd reload failed; prior policy and units restored"
  fi
  if ! snapshot_host_access_manifests; then
    restore_previous_configuration
    die "could not snapshot privileged host access; prior policy and units restored"
  fi
  if ! assert_storage_root_identity; then
    restore_previous_configuration
    die "storage root changed before host access configuration; prior policy and units restored"
  fi
  if ! configure_host_access; then
    local access_rollback_ok=true
    rollback_host_access_delta || access_rollback_ok=false
    restore_previous_configuration
    $access_rollback_ok || die "host access configuration and privileged rollback failed; manifests retain unreverted records"
    die "host access configuration failed; prior policy and units restored"
  fi
  if $RESTART; then
    local service_units=()
    for unit in "${UNITS[@]}"; do service_units+=("$(service_unit_name "$unit")"); done
    snapshot_service_runtime
    if ! systemctl restart "${service_units[@]}" || ! verify_restarted_runtime; then
      local access_rollback_ok=true runtime_rollback_ok=true
      rollback_host_access_delta || access_rollback_ok=false
      restore_previous_configuration
      restore_previous_runtime || runtime_rollback_ok=false
      $access_rollback_ok || die "service restart/readiness and privileged rollback failed; manifests retain unreverted records"
      $runtime_rollback_ok || die "service restart/readiness failed and the previous runtime state could not be restored"
      die "service restart/readiness failed; prior policy and units restored"
    fi
  fi
  echo "configured privileged storage root: $STORAGE_ROOT"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
