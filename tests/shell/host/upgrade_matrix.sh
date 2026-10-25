#!/usr/bin/env bash
# Install, upgrade and rollback acceptance matrix.
#
# Runs the installer and uninstaller for real against a live systemd, so it
# belongs on a disposable host and nowhere else: it installs, upgrades, forces
# a failed migration, a real failed service start, a distinct failed deep
# readiness probe, an existing-split rollback, and uninstalls twice.
# Each row records ownership, modes, the release symlink, the identity actually
# serving, the database revision, helper policy, health and cleanup, and every
# row runs even after an earlier one fails so a single run reports the whole
# picture rather than the first stop.
#
#   sudo -v && tests/shell/host/upgrade_matrix.sh
#
# RELEASE_MODE=first explicitly records row 0 as not applicable for a first
# public release. The default upgrade mode requires genuine published history.
# Rows 1-13 always run; first mode never supplies historical upgrade evidence.
# Environment: SRC and PRIOR_SRC (trees extracted from the two git archives),
# CANDIDATE_ARCHIVE, PRIOR_ARCHIVE, CANDIDATE_SHA, PRIOR_SHA, INSTALL_DIR,
# SERVICE_USER, CUSTOM_ROOT.
set -uo pipefail

SRC="${SRC:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)}"
RELEASE_MODE="${RELEASE_MODE-upgrade}"
case "$RELEASE_MODE" in
  first)
    if [[ -n "${PRIOR_SRC:-}${PRIOR_ARCHIVE:-}${PRIOR_SHA:-}" ]]; then
      echo "first-release mode refuses prior-release inputs" >&2
      exit 2
    fi
    ;;
  upgrade)
    PRIOR_SRC="${PRIOR_SRC:?Set PRIOR_SRC to an extracted, genuine prior-version source archive}"
    PRIOR_ARCHIVE="${PRIOR_ARCHIVE:?Set PRIOR_ARCHIVE to the published git archive used for PRIOR_SRC}"
    PRIOR_SHA="${PRIOR_SHA:?Set PRIOR_SHA to the exact published prior-version commit}"
    ;;
  *) echo "RELEASE_MODE must be first or upgrade" >&2; exit 2 ;;
esac
CANDIDATE_ARCHIVE="${CANDIDATE_ARCHIVE:?Set CANDIDATE_ARCHIVE to the candidate git archive used for SRC}"
CANDIDATE_SHA="${CANDIDATE_SHA:?Set CANDIDATE_SHA to the exact candidate commit under test}"
INSTALL_DIR="${INSTALL_DIR:-/opt/astinuslab}"
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-$USER}}"
CUSTOM_ROOT="${CUSTOM_ROOT:-/srv/astinuslab-acceptance/storage}"
PASS=0
FAIL=0
CHANGED_SRC=""
PROVENANCE_TMP=""

source_tree_digest() {
  python3 - "$1" <<'PYEOF'
import hashlib
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
digest = hashlib.sha256()
for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
    relative = path.relative_to(root).as_posix()
    info = path.lstat()
    digest.update(relative.encode() + b"\0")
    digest.update(f"{stat.S_IMODE(info.st_mode):04o}\0".encode())
    if stat.S_ISLNK(info.st_mode):
        digest.update(b"link\0" + os.readlink(path).encode() + b"\0")
    elif stat.S_ISDIR(info.st_mode):
        digest.update(b"directory\0")
    elif stat.S_ISREG(info.st_mode):
        digest.update(b"file\0" + path.read_bytes() + b"\0")
    else:
        raise SystemExit(f"unsupported source-tree entry: {relative}")
print(digest.hexdigest())
PYEOF
}

archive_commit() {
  local commit
  case "$1" in
    *.tar.gz|*.tgz)
      # Git reads only the leading header. Drain the stream so gzip can verify
      # its trailer; pipefail rejects producer errors before publishing the ID.
      commit="$(
        gzip -cd -- "$1" | {
          local git_status=0
          git get-tar-commit-id || git_status=$?
          cat >/dev/null || exit 1
          exit "$git_status"
        }
      )" || return 1
      printf '%s\n' "$commit"
      ;;
    *.tar) git get-tar-commit-id < "$1" ;;
    *) echo "archive must be a git-produced .tar or .tar.gz: $1" >&2; return 1 ;;
  esac
}

archive_source_root() {
  local archive="$1" destination="$2"
  local -a roots=()
  mkdir -p -- "$destination" || return 1
  tar -xf "$archive" -C "$destination" || return 1
  if [[ -x "$destination/ops/deploy/install.sh" || -x "$destination/deploy/install.sh" ]]; then
    printf '%s\n' "$destination"
    return 0
  fi
  mapfile -d '' roots < <(find "$destination" -mindepth 1 -maxdepth 1 -type d -print0)
  [[ "${#roots[@]}" -eq 1 ]] || return 1
  [[ -x "${roots[0]}/ops/deploy/install.sh" || -x "${roots[0]}/deploy/install.sh" ]] || return 1
  printf '%s\n' "${roots[0]}"
}

installer_path() {
  local root="$1"
  if [[ -x "$root/ops/deploy/install.sh" ]]; then
    printf '%s\n' "$root/ops/deploy/install.sh"
  elif [[ -x "$root/deploy/install.sh" ]]; then
    printf '%s\n' "$root/deploy/install.sh"
  else
    return 1
  fi
}

uninstaller_path() {
  local root="$1"
  if [[ -x "$root/ops/deploy/uninstall.sh" ]]; then
    printf '%s\n' "$root/ops/deploy/uninstall.sh"
  elif [[ -x "$root/deploy/uninstall.sh" ]]; then
    printf '%s\n' "$root/deploy/uninstall.sh"
  else
    return 1
  fi
}

privileged_roots_path() {
  local root="$1"
  if [[ -x "$root/ops/deploy/configure-privileged-roots.sh" ]]; then
    printf '%s\n' "$root/ops/deploy/configure-privileged-roots.sh"
  elif [[ -x "$root/deploy/configure-privileged-roots.sh" ]]; then
    printf '%s\n' "$root/deploy/configure-privileged-roots.sh"
  else
    return 1
  fi
}

cleanup_matrix_tree() {
  local path="$1"
  [[ -n "$path" ]] || return 0
  case "$path" in
    /tmp/astinuslab-matrix-source.*|/tmp/astinuslab-matrix-provenance.*) ;;
    *) echo "retaining unexpected matrix source fixture: $path" >&2; return 1 ;;
  esac
  [[ -d "$path" && ! -L "$path" ]] || return 0
  find "$path" -xdev -depth -delete
}

cleanup_matrix_sources() {
  cleanup_matrix_tree "$CHANGED_SRC"
  cleanup_matrix_tree "$PROVENANCE_TMP"
}
trap cleanup_matrix_sources EXIT

[[ "$CANDIDATE_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "CANDIDATE_SHA must be a full commit SHA" >&2; exit 2; }
[[ -f "$CANDIDATE_ARCHIVE" && ! -L "$CANDIDATE_ARCHIVE" ]] \
  || { echo "CANDIDATE_ARCHIVE must be a regular non-symlink file" >&2; exit 2; }
SRC_INSTALLER="$(installer_path "$SRC")" || {
  echo "SRC must contain executable installer sources" >&2
  exit 2
}
SRC_UNINSTALLER="$(uninstaller_path "$SRC")" || exit 2
CANDIDATE_ARCHIVE_COMMIT="$(archive_commit "$CANDIDATE_ARCHIVE")" || exit 2
[[ "$CANDIDATE_ARCHIVE_COMMIT" == "$CANDIDATE_SHA" ]] \
  || { echo "candidate archive commit does not match CANDIDATE_SHA" >&2; exit 2; }
PROVENANCE_TMP="$(mktemp -d /tmp/astinuslab-matrix-provenance.XXXXXXXX)" || exit 2
CANDIDATE_ARCHIVE_ROOT="$(archive_source_root "$CANDIDATE_ARCHIVE" "$PROVENANCE_TMP/candidate")" || exit 2
CANDIDATE_SOURCE_DIGEST="$(source_tree_digest "$SRC")" || exit 2
[[ "$CANDIDATE_SOURCE_DIGEST" == "$(source_tree_digest "$CANDIDATE_ARCHIVE_ROOT")" ]] \
  || { echo "SRC does not match the candidate archive" >&2; exit 2; }
printf 'release_mode=%s\ncandidate_sha=%s\n' "$RELEASE_MODE" "$CANDIDATE_SHA"
printf 'candidate_archive_sha256=%s\ncandidate_source_sha256=%s\n' \
  "$(sha256sum "$CANDIDATE_ARCHIVE" | awk '{print $1}')" "$CANDIDATE_SOURCE_DIGEST"
if [[ "$RELEASE_MODE" == upgrade ]]; then
  [[ "$PRIOR_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "PRIOR_SHA must be a full commit SHA" >&2; exit 2; }
  [[ "$CANDIDATE_SHA" != "$PRIOR_SHA" ]] || { echo "candidate and prior commits must differ" >&2; exit 2; }
  [[ -f "$PRIOR_ARCHIVE" && ! -L "$PRIOR_ARCHIVE" ]] \
    || { echo "PRIOR_ARCHIVE must be a regular non-symlink file" >&2; exit 2; }
  PRIOR_INSTALLER="$(installer_path "$PRIOR_SRC")" || {
    echo "PRIOR_SRC must contain executable installer sources" >&2
    exit 2
  }
  PRIOR_ARCHIVE_COMMIT="$(archive_commit "$PRIOR_ARCHIVE")" || exit 2
  [[ "$PRIOR_ARCHIVE_COMMIT" == "$PRIOR_SHA" ]] \
    || { echo "prior archive commit does not match PRIOR_SHA" >&2; exit 2; }
  PRIOR_ARCHIVE_ROOT="$(archive_source_root "$PRIOR_ARCHIVE" "$PROVENANCE_TMP/prior")" || exit 2
  PRIOR_SOURCE_DIGEST="$(source_tree_digest "$PRIOR_SRC")" || exit 2
  [[ "$PRIOR_SOURCE_DIGEST" == "$(source_tree_digest "$PRIOR_ARCHIVE_ROOT")" ]] \
    || { echo "PRIOR_SRC does not match the prior archive" >&2; exit 2; }
  printf 'prior_sha=%s\nprior_archive_sha256=%s\nprior_source_sha256=%s\n' \
    "$PRIOR_SHA" "$(sha256sum "$PRIOR_ARCHIVE" | awk '{print $1}')" "$PRIOR_SOURCE_DIGEST"
fi

SOURCE_DIGEST_BEFORE="$(source_tree_digest "$SRC")"
CHANGED_SRC="$(mktemp -d /tmp/astinuslab-matrix-source.XXXXXXXX)"
cp -a -- "$SRC/." "$CHANGED_SRC/"
CHANGED_INSTALLER="$(installer_path "$CHANGED_SRC")" || exit 2

row() { printf '\n════ ROW %s ════\n' "$*"; }
check() {
  local label="$1" expected="$2" actual="$3"
  if [[ "$actual" == "$expected" ]]; then
    printf '  PASS  %-46s %s\n' "$label" "$actual"; PASS=$((PASS + 1))
  else
    printf '  FAIL  %-46s got=%s want=%s\n' "$label" "$actual" "$expected"; FAIL=$((FAIL + 1))
  fi
}
units_active() {
  local n=0 u unit
  for u in astinuslab-api astinuslab-runner astinuslab-analysis; do
    unit="$u@$SERVICE_USER.service"
    if sudo grep -qx 'mode=split' /etc/astinuslab/runtime-identities 2>/dev/null; then
      unit="$u.service"
    fi
    [[ "$(systemctl is-active "$unit")" == active ]] && n=$((n + 1))
  done
  echo "$n"
}
health() { curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/health; }
wait_health() {
  local code=000
  for _ in {1..10}; do
    code="$(health)"
    if [[ "$code" == 200 ]]; then echo "$code"; return 0; fi
    sleep 1
  done
  echo "$code"
}
runtime_storage_root() {
  local unit="astinuslab-api@$SERVICE_USER.service" pid
  if sudo grep -qx 'mode=split' /etc/astinuslab/runtime-identities 2>/dev/null; then
    unit=astinuslab-api.service
  fi
  pid="$(systemctl show "$unit" -p ExecMainPID --value)"
  sudo python3 - "$pid" <<'PYEOF'
import sys
for item in open(f"/proc/{sys.argv[1]}/environ", "rb").read().split(b"\0"):
    if item.startswith(b"ASTINUSLAB_STORAGE_DIR="):
        print(item.split(b"=", 1)[1].decode())
        break
PYEOF
}
runtime_identity_marker_valid() {
  printf '%s\n' \
    mode=split \
    api=astinuslab-api \
    runner=astinuslab-runner \
    analysis=astinuslab-analysis \
    group=astinuslab \
    | sudo cmp -s - /etc/astinuslab/runtime-identities
}
process_owner() {
  local pid
  pid="$(systemctl show "$1" -p ExecMainPID --value)"
  sudo stat -c '%U' "/proc/$pid"
}
process_uid() {
  local pid
  pid="$(systemctl show "$1" -p ExecMainPID --value)"
  sudo stat -c '%u' "/proc/$pid"
}
named_acl_permissions() {
  local path="$1" principal="$2"
  sudo getfacl -cp -- "$path" 2>/dev/null \
    | awk -F: -v principal="$principal" '$1 == "user" && $2 == principal { print $3; exit }'
}
qemu_principal() {
  if getent passwd libvirt-qemu >/dev/null 2>&1; then
    echo libvirt-qemu
  else
    echo qemu
  fi
}
db_identity() {
  if sudo grep -qx 'mode=split' /etc/astinuslab/runtime-identities 2>/dev/null; then
    echo astinuslab-api
  else
    echo "$SERVICE_USER"
  fi
}
installed_database_python() {
  # Read the candidate's loader before changing identity: the extracted source
  # can be private, and a genuine prior release need not ship this helper yet.
  # shellcheck disable=SC2024
  sudo -u "$(db_identity)" env -i PATH=/usr/bin:/bin \
    /usr/bin/python3 -I - --data-dir "$INSTALL_DIR" -- \
    "$INSTALL_DIR/current/.venv/bin/python" "$@" < "$SRC/ops/deploy/runtime_environment.py"
}
schema() {
  installed_database_python -m astinuslab.db revision
}
head_revision() {
  # The head the installed candidate declares, not a value pinned in this file:
  # a new migration must not need an edit here to stay covered.
  sudo -u "$(db_identity)" env ASTINUSLAB_DATA_DIR="$INSTALL_DIR" \
    "$INSTALL_DIR/current/.venv/bin/python" -c '
from importlib.resources import files
from alembic.config import Config
from alembic.script import ScriptDirectory
config = Config(str(files("astinuslab.migrations").joinpath("alembic.ini")))
head = ScriptDirectory.from_config(config).get_current_head()
if not head:
    raise RuntimeError("Candidate migration head is empty")
print(head)'
}
release() { readlink "$INSTALL_DIR/current"; }
db_path() {
  installed_database_python -c 'from astinuslab.core.config import settings; print(settings.db_path)'
}
database_content() {
  local database
  database="$(db_path)" || return 1
  sudo -u "$(db_identity)" env ASTINUSLAB_DATA_DIR="$INSTALL_DIR" \
    "$INSTALL_DIR/current/.venv/bin/python" - "$database" <<'PYEOF'
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
exists = connection.execute(
    "select count(*) from sqlite_master where type='table' and name='acceptance_marker'"
).fetchone()[0]
if not exists:
    print("missing")
else:
    print("|".join(row[0] for row in connection.execute("select v from acceptance_marker order by rowid")))
PYEOF
}
process_cwd() {
  local pid
  pid="$(systemctl show "$1" -p ExecMainPID --value)"
  sudo readlink -f "/proc/$pid/cwd"
}
service_state_digest() {
  local unit
  for unit in \
    "astinuslab-api@$SERVICE_USER.service" \
    "astinuslab-runner@$SERVICE_USER.service" \
    "astinuslab-analysis@$SERVICE_USER.service" \
    astinuslab-api.service astinuslab-runner.service astinuslab-analysis.service; do
    printf '%s active=%s enabled=%s\n' "$unit" \
      "$(systemctl is-active "$unit" 2>/dev/null || true)" \
      "$(systemctl is-enabled "$unit" 2>/dev/null || true)"
  done | sha256sum | awk '{print $1}'
}
managed_units_with_live_pid() {
  local unit active pid count=0
  for unit in \
    "astinuslab-api@$SERVICE_USER.service" \
    "astinuslab-runner@$SERVICE_USER.service" \
    "astinuslab-analysis@$SERVICE_USER.service" \
    astinuslab-api.service astinuslab-runner.service astinuslab-analysis.service; do
    active="$(systemctl is-active "$unit" 2>/dev/null || true)"
    pid="$(systemctl show "$unit" -p ExecMainPID --value 2>/dev/null || true)"
    [[ -n "$pid" ]] || pid=0
    if [[ "$active" == active || "$pid" != 0 ]]; then
      count=$((count + 1))
    fi
  done
  echo "$count"
}
split_mutable_custody_digest() {
  local databases receipt="${1:-}"
  databases="$(installed_database_python -c '
import json
from sqlalchemy.engine import make_url
from astinuslab.core.config import settings
paths = [str(settings.db_path)]
for value, prefix in ((settings.celery_broker_url, "sqla+"), (settings.celery_result_backend, "db+")):
    if value.startswith(prefix + "sqlite:///"):
        paths.append(make_url(value.removeprefix(prefix)).database)
print(json.dumps(paths))')" || return 1
  sudo python3 - \
    "$INSTALL_DIR/astinuslab.toml" \
    "$INSTALL_DIR/config" \
    "$INSTALL_DIR/images/definitions" \
    "$INSTALL_DIR/vol3/symbols" \
    "$INSTALL_DIR/storage" \
    "$CUSTOM_ROOT" "$databases" "$receipt" <<'PYEOF'
import hashlib
import json
import re
import os
import stat
import subprocess
import sys
from pathlib import Path

top_level = [Path(value) for value in sys.argv[1:-2]]
sidecars = {Path(path + suffix) for path in json.loads(sys.argv[-2]) for suffix in ("-wal", "-shm", "-journal")}
logs = {"api": "api.log", "runner": "runner.log", "analysis": "analysis-worker.log"}
log_roots = {top_level[-2] / "logs", top_level[-1] / "logs"}
roots = list(dict.fromkeys(top_level))
paths = []
seen = set()
for root in roots:
    candidates = [root]
    if root.is_dir():
        candidates.extend(sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()))
    for candidate in candidates:
        key = os.fspath(candidate)
        if key not in seen:
            seen.add(key)
            paths.append(candidate)

digest = hashlib.sha256()
records = []
for path in paths:
    # Live sidecars and role-log rotations legitimately change writer UID/ACL
    # spelling across restart. Dedicated DB/log checks validate their policy.
    role = path.parent.name
    role_log = (path.parent.parent in log_roots and role in logs and
                re.fullmatch(re.escape(logs[role]) + r"(?:\.[1-9][0-9]{0,17})?", path.name))
    try:
        info = path.lstat()
    except FileNotFoundError:
        if path in sidecars or role_log:
            continue
        raise
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise SystemExit(f"mutable root is not regular/directory: {path}")
    if stat.S_ISREG(info.st_mode) and (path in sidecars or role_log):
        continue
    digest.update(f"{path}\0{info.st_uid}:{info.st_gid}:{stat.S_IMODE(info.st_mode):04o}\0".encode())
    acl = subprocess.run(
        ["getfacl", "-cp", "--", os.fspath(path)],
        check=True,
        capture_output=True,
    )
    digest.update(acl.stdout + b"\0")
    records.append({"path": str(path), "uid": info.st_uid, "gid": info.st_gid,
                    "mode": stat.S_IMODE(info.st_mode), "acl": acl.stdout.decode()})
if sys.argv[-1]:
    descriptor = os.open(sys.argv[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump({"sha256": digest.hexdigest(), "records": records}, output, indent=2)
        output.flush()
        os.fsync(output.fileno())
print(digest.hexdigest())
PYEOF
}
check_single_ssh_custody() {
  local phase="$1" active_key
  active_key="$(sudo cat "$INSTALL_DIR/storage/ssh_keys/.active" | tr -d '\n')"
  check "$phase SSH directory custody" "$SERVICE_USER:astinuslab:700" \
    "$(sudo stat -c '%U:%G:%a' "$INSTALL_DIR/storage/ssh_keys")"
  check "$phase SSH marker custody" "$SERVICE_USER:astinuslab:600" \
    "$(sudo stat -c '%U:%G:%a' "$INSTALL_DIR/storage/ssh_keys/.active")"
  check "$phase SSH private-key custody" "$SERVICE_USER:astinuslab:600" \
    "$(sudo stat -c '%U:%G:%a' "$INSTALL_DIR/storage/ssh_keys/$active_key")"
  check "$phase SSH public-key custody" "$SERVICE_USER:astinuslab:600" \
    "$(sudo stat -c '%U:%G:%a' "$INSTALL_DIR/storage/ssh_keys/$active_key.pub")"
}
check_split_custody() {
  local phase="$1" path expected active_key
  while IFS='|' read -r path expected; do
    check "$phase ${path#"$INSTALL_DIR"/} custody" "$expected" \
      "$(sudo stat -c '%U:%G:%a' "$path")"
  done <<EOF
$INSTALL_DIR/astinuslab.toml|astinuslab-api:astinuslab:640
$INSTALL_DIR/config|astinuslab-api:astinuslab:2750
$INSTALL_DIR/images/definitions|astinuslab-runner:astinuslab:750
$INSTALL_DIR/vol3/symbols|astinuslab-analysis:astinuslab:2770
$INSTALL_DIR/storage|astinuslab-api:astinuslab:3770
$INSTALL_DIR/storage/dumps|astinuslab-runner:astinuslab:2750
$INSTALL_DIR/storage/logs|root:root:1770
$INSTALL_DIR/storage/ssh_keys|astinuslab-api:astinuslab:2750
$INSTALL_DIR/storage/tmp|astinuslab-runner:astinuslab:750
$INSTALL_DIR/storage/workload-cache|astinuslab-runner:astinuslab:750
$INSTALL_DIR/storage/playground|astinuslab-runner:astinuslab:700
$INSTALL_DIR/storage/playground/disks|astinuslab-runner:astinuslab:700
$INSTALL_DIR/storage/playground/captures|astinuslab-runner:astinuslab:750
$CUSTOM_ROOT|astinuslab-api:astinuslab:3770
$CUSTOM_ROOT/dumps|astinuslab-runner:astinuslab:2750
$CUSTOM_ROOT/logs|root:root:1770
$CUSTOM_ROOT/ssh_keys|astinuslab-api:astinuslab:2750
$CUSTOM_ROOT/tmp|astinuslab-runner:astinuslab:750
$CUSTOM_ROOT/workload-cache|astinuslab-runner:astinuslab:750
$CUSTOM_ROOT/playground|astinuslab-runner:astinuslab:710
$CUSTOM_ROOT/playground/disks|astinuslab-runner:astinuslab:700
$CUSTOM_ROOT/playground/captures|astinuslab-runner:astinuslab:750
EOF
  check "$phase custom Playground evidence traversal ACL" \
    $'user::rwx\ngroup::---\ngroup:astinuslab:--x\nmask::--x\nother::---' \
    "$(sudo getfacl -cpE "$CUSTOM_ROOT/playground")"
  active_key="$(sudo cat "$INSTALL_DIR/storage/ssh_keys/.active" | tr -d '\n')"
  check "$phase catalog child custody" astinuslab-api:astinuslab:640 \
    "$(sudo stat -c '%U:%G:%a' "$INSTALL_DIR/config/catalog/targets.yaml")"
  check "$phase active marker custody" astinuslab-api:astinuslab:640 \
    "$(sudo stat -c '%U:%G:%a' "$INSTALL_DIR/storage/ssh_keys/.active")"
  check "$phase private key custody" astinuslab-api:astinuslab:640 \
    "$(sudo stat -c '%U:%G:%a' "$INSTALL_DIR/storage/ssh_keys/$active_key")"
  check "$phase public key custody" astinuslab-api:astinuslab:640 \
    "$(sudo stat -c '%U:%G:%a' "$INSTALL_DIR/storage/ssh_keys/$active_key.pub")"
  check "$phase database child custody" astinuslab-api:astinuslab:660 \
    "$(sudo stat -c '%U:%G:%a' "$(db_path)")"
  check "$phase image child custody" astinuslab-runner:astinuslab:640 \
    "$(sudo stat -c '%U:%G:%a' "$INSTALL_DIR/images/definitions/.matrix-custody.probe")"
  check "$phase symbol child custody" astinuslab-analysis:astinuslab:660 \
    "$(sudo stat -c '%U:%G:%a' "$INSTALL_DIR/vol3/symbols/.matrix-custody.json")"
  check "$phase custom evidence child custody" "$SERVICE_USER:$SERVICE_USER:440" \
    "$(sudo stat -c '%U:%G:%a' "$CUSTOM_ROOT/dumps/.matrix-custody.bin")"
  check "$phase image qemu ACL" r-x \
    "$(named_acl_permissions "$INSTALL_DIR/images/definitions" libvirt-qemu)"
  check "$phase SSH runner ACL" r-x \
    "$(named_acl_permissions "$INSTALL_DIR/storage/ssh_keys" astinuslab-runner)"
  check "$phase custom SSH runner ACL" r-x \
    "$(named_acl_permissions "$CUSTOM_ROOT/ssh_keys" astinuslab-runner)"
}
verify_split_role_logs() {
  # No log writes or restarts. One total bound includes all polling/ACL/read probes.
  sudo -n timeout --kill-after=1s 28s /usr/bin/python3 -I -B \
    "$SRC/tests/shell/host/verify_split_logs.py" \
    --install "$INSTALL_DIR" --storage "$CUSTOM_ROOT"
}
privileged_state_digest() {
  sudo python3 - "$INSTALL_DIR" "$CUSTOM_ROOT" <<'PYEOF'
import hashlib
import os
import stat
import subprocess
import sys
from pathlib import Path

install_dir, custom_root = map(Path, sys.argv[1:])
roots = [
    Path("/etc/systemd/system/astinuslab-api@.service"),
    Path("/etc/systemd/system/astinuslab-runner@.service"),
    Path("/etc/systemd/system/astinuslab-analysis@.service"),
    Path("/etc/systemd/system/astinuslab-api.service"),
    Path("/etc/systemd/system/astinuslab-runner.service"),
    Path("/etc/systemd/system/astinuslab-analysis.service"),
    Path("/etc/sudoers.d/astinuslab-runner"),
    Path("/etc/astinuslab/acl-policy.json"),
    Path("/etc/astinuslab/runtime-identities"),
    Path("/etc/astinuslab/traversal-acl.manifest"),
    Path("/etc/astinuslab/mac-policy.manifest"),
    Path("/usr/local/libexec/astinuslab"),
    Path("/etc/apparmor.d/libvirt/TEMPLATE.qemu"),
]
digest = hashlib.sha256()


def add(path: Path) -> None:
    digest.update((str(path) + "\0").encode())
    try:
        info = path.lstat()
    except FileNotFoundError:
        digest.update(b"missing\0")
        return
    digest.update(f"{info.st_mode:o}:{info.st_uid}:{info.st_gid}\0".encode())
    if stat.S_ISLNK(info.st_mode):
        digest.update(os.readlink(path).encode() + b"\0")
    elif stat.S_ISREG(info.st_mode):
        digest.update(path.read_bytes())
    elif stat.S_ISDIR(info.st_mode):
        with os.scandir(path) as entries:
            children = sorted((Path(entry.path) for entry in entries), key=lambda child: child.name)
        for child in children:
            add(child)


for root in roots:
    add(root)

# File bytes are not the whole privileged boundary. Hash the effective ACL for
# every component that either manifest names or either supported storage root
# traverses, so a rollback cannot pass with a candidate-only named grant left
# on the host after old manifest bytes were restored.
acl_paths: set[Path] = set()
for leaf in (install_dir / "storage", custom_root):
    current = Path(os.path.abspath(leaf))
    while current != Path("/"):
        acl_paths.add(current)
        current = current.parent
manifest = Path("/etc/astinuslab/traversal-acl.manifest")
if manifest.is_file():
    for line in manifest.read_text(encoding="utf-8").splitlines():
        fields = line.split("\t")
        if len(fields) == 5 and fields[0] == "v2":
            acl_paths.add(Path(fields[4]))
for path in sorted(acl_paths, key=str):
    digest.update(("acl:" + str(path) + "\0").encode())
    result = subprocess.run(["getfacl", "-cp", "--", str(path)], capture_output=True, check=False)
    digest.update(f"return={result.returncode}\0".encode())
    digest.update(result.stdout)

# AppArmor bytes are already included above. On SELinux hosts also bind the
# registered fcontext rules and the effective labels on both storage roots.
if subprocess.run(["sh", "-c", "command -v semanage >/dev/null 2>&1"], check=False).returncode == 0:
    result = subprocess.run(["semanage", "fcontext", "-l"], capture_output=True, check=False)
    matching = sorted(
        line for line in result.stdout.splitlines()
        if any(str(path).encode() in line for path in (install_dir / "storage", custom_root))
    )
    for line in matching:
        digest.update(b"selinux-fcontext:")
        digest.update(line + b"\0")
for path in (install_dir / "storage", custom_root):
    digest.update(("selinux-label:" + str(path) + "\0").encode())
    try:
        digest.update(os.getxattr(path, "security.selinux") + b"\0")
    except OSError as exc:
        digest.update(f"missing:{exc.errno}\0".encode())
print(digest.hexdigest())
PYEOF
}
custom_root_mac_recorded() {
  sudo python3 - /etc/astinuslab/mac-policy.manifest "$CUSTOM_ROOT" <<'PYEOF'
import json
import sys
from pathlib import Path

manifest, root = Path(sys.argv[1]), sys.argv[2]
if not manifest.is_file():
    raise SystemExit(1)
for line in manifest.read_text(encoding="utf-8").splitlines():
    record = json.loads(line)
    if record.get("target") == root or root in str(record.get("value", "")):
        raise SystemExit(0)
raise SystemExit(1)
PYEOF
}
selinux_expression() {
  sudo python3 - /etc/astinuslab/mac-policy.manifest "$CUSTOM_ROOT" <<'PYEOF'
import json
import sys
from pathlib import Path

manifest, root = Path(sys.argv[1]), sys.argv[2]
for line in manifest.read_text(encoding="utf-8").splitlines():
    record = json.loads(line)
    if record.get("kind") == "selinux" and record.get("target") == root:
        print(record["value"])
        raise SystemExit(0)
raise SystemExit(1)
PYEOF
}
custom_root_mac_effective() {
  local template=/etc/apparmor.d/libvirt/TEMPLATE.qemu
  if sudo test -f "$template"; then
    sudo grep -Fxq -- "  \"${CUSTOM_ROOT}/**\" rwk," "$template" \
      && sudo aa-status --enabled
    return
  fi
  if command -v getenforce >/dev/null 2>&1 && [[ "$(getenforce)" == Enforcing ]]; then
    local expression fcontexts matching context
    expression="$(selinux_expression)" || return 1
    fcontexts="$(sudo semanage fcontext -l 2>/dev/null)" || return 1
    matching="$(awk -v expression="$expression" \
      'index($0, expression) == 1 && substr($0, length(expression) + 1, 1) ~ /[[:space:]]/' \
      <<<"$fcontexts")" || return 1
    [[ -n "$matching" ]] || return 1
    grep -q ':svirt_image_t:' <<<"$matching" || return 1
    context="$(sudo stat -c '%C' "$CUSTOM_ROOT")" || return 1
    grep -q ':svirt_image_t:' <<<"$context"
    return
  fi
  return 1
}
custom_root_libvirt_disk_open() {
  local name="astinuslab-matrix-storage-$$" probe_dir probe_identity disk xml result=0
  local domain_cleanup_ok=true domain_state
  probe_dir="$(sudo -u "$SERVICE_USER" mktemp -d "$CUSTOM_ROOT/.astinuslab-libvirt-open.XXXXXXXX")" \
    || return 1
  probe_identity="$(sudo stat -c '%d:%i' -- "$probe_dir")" || return 1
  disk="$probe_dir/probe.qcow2"
  xml="/tmp/${name}.xml"
  sudo -u "$SERVICE_USER" qemu-img create -q -f qcow2 "$disk" 64M || result=$?
  if [[ "$result" -eq 0 ]]; then
    # The operator shell intentionally owns the disposable probe log.
    # shellcheck disable=SC2024
    sudo -u "$SERVICE_USER" sudo -n /usr/local/libexec/astinuslab/fix_libvirt_path_acl.sh "$probe_dir" \
      >/tmp/astinuslab-matrix-libvirt-acl.log 2>&1 || result=$?
  fi
  if [[ "$result" -eq 0 ]]; then
    cat > "$xml" <<EOF
<domain type='kvm'>
  <name>$name</name>
  <memory unit='MiB'>64</memory>
  <vcpu>1</vcpu>
  <os><type arch='x86_64'>hvm</type><boot dev='hd'/></os>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='$disk'/>
      <target dev='vda' bus='virtio'/>
      <readonly/>
    </disk>
    <console type='pty'/>
  </devices>
</domain>
EOF
    # The operator shell intentionally owns the disposable probe log.
    # shellcheck disable=SC2024
    sudo virsh --connect qemu:///system define "$xml" >/tmp/astinuslab-matrix-libvirt-define.log 2>&1 \
      || result=$?
  fi
  if [[ "$result" -eq 0 ]]; then
    # The operator shell intentionally owns the disposable probe log.
    # shellcheck disable=SC2024
    sudo virsh --connect qemu:///system start "$name" >/tmp/astinuslab-matrix-libvirt-start.log 2>&1 \
      || result=$?
  fi
  if sudo virsh --connect qemu:///system dominfo "$name" >/dev/null 2>&1; then
    domain_state="$(sudo virsh --connect qemu:///system domstate "$name" 2>/dev/null || true)"
    if [[ "$domain_state" != "shut off" ]]; then
      sudo virsh --connect qemu:///system destroy "$name" >/dev/null 2>&1 \
        || domain_cleanup_ok=false
      domain_state="$(sudo virsh --connect qemu:///system domstate "$name" 2>/dev/null || true)"
      [[ "$domain_state" == "shut off" ]] || domain_cleanup_ok=false
    fi
    if $domain_cleanup_ok; then
      sudo virsh --connect qemu:///system undefine "$name" --nvram >/dev/null 2>&1 \
        || sudo virsh --connect qemu:///system undefine "$name" >/dev/null 2>&1 \
        || domain_cleanup_ok=false
    fi
    if sudo virsh --connect qemu:///system dominfo "$name" >/dev/null 2>&1; then
      domain_cleanup_ok=false
    fi
  fi
  rm -f -- "$xml"
  if $domain_cleanup_ok && sudo test -d "$probe_dir" && ! sudo test -L "$probe_dir" \
    && [[ "$(sudo stat -c '%d:%i' -- "$probe_dir")" == "$probe_identity" ]]; then
    sudo rm -f -- "$disk" || result=1
    sudo rmdir -- "$probe_dir" || result=1
  else
    echo "retaining libvirt probe custody after teardown/identity failure: $probe_dir" >&2
    result=1
  fi
  return "$result"
}
recorded_acl_grants_remaining() {
  local manifest="$1" version qemu_user _device _inode path remaining=0
  [[ -f "$manifest" ]] || { echo 0; return; }
  while IFS=$'\t' read -r version qemu_user _device _inode path; do
    [[ "$version" == v2 && -n "$qemu_user" && -n "$path" ]] || continue
    if sudo test -d "$path" \
      && sudo getfacl -cp -- "$path" 2>/dev/null | grep -Eq "^user:${qemu_user}:[rwx-]{3}"; then
      remaining=$((remaining + 1))
    fi
  done < <(sudo cat "$manifest")
  echo "$remaining"
}
recorded_mac_changes_remaining() {
  local manifest="$1" kind target value remaining=0
  [[ -f "$manifest" ]] || { echo 0; return; }
  while IFS=$'\t' read -r kind target value; do
    case "$kind" in
      apparmor)
        if [[ -f "$target" ]] && sudo grep -Fxq -- "$value" "$target"; then
          remaining=$((remaining + 1))
        fi
        ;;
      selinux)
        if command -v semanage >/dev/null 2>&1 \
          && sudo semanage fcontext -l 2>/dev/null | grep -Fq -- "$value"; then
          remaining=$((remaining + 1))
        fi
        ;;
    esac
  done < <(sudo python3 - "$manifest" <<'PYEOF'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    for line in handle:
        if not line.strip():
            continue
        record = json.loads(line)
        print(record["kind"], record["target"], record["value"], sep="\t")
PYEOF
)
  echo "$remaining"
}

# ── ROW 0: upgrade a genuine published prior version ─────────────────────────
# This isolated fixture starts from PRIOR_SRC, whose installer predates the
# ownership manifests. The candidate must not pretend it owns that legacy ACL
# or MAC state, and its uninstall must preserve both. Harness cleanup happens
# only after those assertions and only for state proven absent before this row.
row "0 genuine prior-version upgrade"
if [[ "$RELEASE_MODE" == first ]]; then
  printf 'row0=not_applicable:first_release\n'
else
LEGACY_INSTALL_DIR="/opt/astinuslab-legacy-acceptance"
LEGACY_QEMU_USER="$(qemu_principal)"
LEGACY_STORAGE="$LEGACY_INSTALL_DIR/storage"
LEGACY_APPARMOR_TEMPLATE=/etc/apparmor.d/libvirt/TEMPLATE.qemu
LEGACY_APPARMOR_LINE="  \"${LEGACY_STORAGE}/**\" rwk,"
if sudo test -e "$LEGACY_INSTALL_DIR" || sudo test -L "$LEGACY_INSTALL_DIR"; then
  echo "refusing pre-existing legacy matrix install root: $LEGACY_INSTALL_DIR" >&2
  exit 2
fi
LEGACY_OPT_ACL_BEFORE="$(named_acl_permissions /opt "$LEGACY_QEMU_USER")"
LEGACY_MAC_BEFORE=0
if sudo test -f "$LEGACY_APPARMOR_TEMPLATE" \
  && sudo grep -Fxq -- "$LEGACY_APPARMOR_LINE" "$LEGACY_APPARMOR_TEMPLATE"; then
  LEGACY_MAC_BEFORE=1
fi
check "legacy fixture MAC rule starts absent" 0 "$LEGACY_MAC_BEFORE"
check "legacy install root starts absent" 0 \
  "$(sudo test -e "$LEGACY_INSTALL_DIR" && echo 1 || echo 0)"
(cd "$PRIOR_SRC" && sudo "$PRIOR_INSTALLER" --install-dir "$LEGACY_INSTALL_DIR" \
  --service-user "$SERVICE_USER" -y --generate-admin-token) >/tmp/r0-prior.log 2>&1
check "published prior install exit" 0 "$?"
check "published prior health" 200 "$(wait_health)"
check "prior has no traversal ownership manifest" 0 \
  "$(sudo test -e /etc/astinuslab/traversal-acl.manifest && echo 1 || echo 0)"
check "prior has no MAC ownership manifest" 0 \
  "$(sudo test -e /etc/astinuslab/mac-policy.manifest && echo 1 || echo 0)"
check "prior leaves trusted /opt ACL unchanged" "$LEGACY_OPT_ACL_BEFORE" \
  "$(named_acl_permissions /opt "$LEGACY_QEMU_USER")"
LEGACY_PRIOR_RELEASE="$(readlink "$LEGACY_INSTALL_DIR/current")"
(cd "$SRC" && sudo "$SRC_INSTALLER" --install-dir "$LEGACY_INSTALL_DIR" \
  --service-user "$SERVICE_USER" -y) >/tmp/r0-upgrade.log 2>&1
check "genuine prior-version upgrade exit" 0 "$?"
check "candidate replaced the published prior release" changed \
  "$([[ "$(readlink "$LEGACY_INSTALL_DIR/current")" != "$LEGACY_PRIOR_RELEASE" ]] && echo changed || echo same)"
check "candidate health after genuine upgrade" 200 "$(wait_health)"
check "candidate did not claim the pre-existing /opt traversal state" 0 \
  "$(sudo awk -F'\t' -v p=/opt '$5 == p' \
    /etc/astinuslab/traversal-acl.manifest 2>/dev/null | wc -l)"
check "trusted /opt ACL survives candidate upgrade" "$LEGACY_OPT_ACL_BEFORE" \
  "$(named_acl_permissions /opt "$LEGACY_QEMU_USER")"
# The operator shell intentionally owns the /tmp acceptance log.
# shellcheck disable=SC2024
printf 'y\ny\ny\ny\n' | sudo INSTALL_DIR="$LEGACY_INSTALL_DIR" SERVICE_USER="$SERVICE_USER" \
  "$SRC_UNINSTALLER" >/tmp/r0-uninstall.log 2>&1
check "candidate uninstall after genuine upgrade" 0 "$?"
check "trusted /opt ACL survives candidate uninstall" "$LEGACY_OPT_ACL_BEFORE" \
  "$(named_acl_permissions /opt "$LEGACY_QEMU_USER")"
if sudo test -f "$LEGACY_APPARMOR_TEMPLATE"; then
  check "unrecorded legacy MAC rule survives uninstall" 1 \
    "$(sudo grep -Fxq -- "$LEGACY_APPARMOR_LINE" "$LEGACY_APPARMOR_TEMPLATE" && echo 1 || echo 0)"
fi
check "legacy install root removed" 0 \
  "$(sudo test -e "$LEGACY_INSTALL_DIR" && echo 1 || echo 0)"

# Disposable-fixture cleanup, intentionally outside product uninstall. Never
# remove access that existed before this row.
if [[ "$LEGACY_MAC_BEFORE" -eq 0 ]] && sudo test -f "$LEGACY_APPARMOR_TEMPLATE" \
  && sudo grep -Fxq -- "$LEGACY_APPARMOR_LINE" "$LEGACY_APPARMOR_TEMPLATE"; then
  sudo python3 - "$LEGACY_APPARMOR_TEMPLATE" "$LEGACY_APPARMOR_LINE" <<'PYEOF'
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
line = sys.argv[2]
lines = path.read_text(encoding="utf-8").splitlines()
path.write_text("\n".join(item for item in lines if item != line) + "\n", encoding="utf-8")
os.chmod(path, 0o644)
PYEOF
  sudo systemctl restart apparmor
fi
check "legacy fixture /opt ACL unchanged" "$LEGACY_OPT_ACL_BEFORE" \
  "$(named_acl_permissions /opt "$LEGACY_QEMU_USER")"
check "legacy fixture MAC rule cleaned" "$LEGACY_MAC_BEFORE" \
  "$(sudo test -f "$LEGACY_APPARMOR_TEMPLATE" \
    && sudo grep -Fxq -- "$LEGACY_APPARMOR_LINE" "$LEGACY_APPARMOR_TEMPLATE" && echo 1 || echo 0)"
fi

# ── ROW 1: clean install ──────────────────────────────────────────────────────
row "1 clean install"
if sudo test -e "$INSTALL_DIR" || sudo test -L "$INSTALL_DIR"; then
  echo "refusing pre-existing matrix install root: $INSTALL_DIR" >&2
  exit 2
fi
(cd "$SRC" && sudo "$SRC_INSTALLER" --install-dir "$INSTALL_DIR" --service-user "$SERVICE_USER" -y --generate-admin-token) > /tmp/r1.log 2>&1
check "install exit" 0 "$?"
check "units active" 3 "$(units_active)"
check "health" 200 "$(health)"
check "schema at the candidate's head" "$(head_revision)" "$(schema)"
check "install root owner" "root:root" "$(sudo stat -c '%U:%G' "$INSTALL_DIR")"
check "release root-owned" "root:root" "$(sudo stat -c '%U:%G' "$(readlink -f "$INSTALL_DIR/current")")"
check "storage setgid 2770" "2770" "$(sudo stat -c '%a' "$INSTALL_DIR/storage")"
check "helper policy present" "1" "$(sudo test -f /etc/astinuslab/acl-policy.json && echo 1 || echo 0)"
check "sudoers present" "1" "$(sudo test -f /etc/sudoers.d/astinuslab-runner && echo 1 || echo 0)"
check "traversal manifest recorded" "1" "$(sudo test -s /etc/astinuslab/traversal-acl.manifest && echo 1 || echo 0)"
SERVICE_UID="$(id -u "$SERVICE_USER")"
check "api process exact uid" "$SERVICE_UID" "$(process_uid "astinuslab-api@$SERVICE_USER.service")"
check "runner process exact uid" "$SERVICE_UID" "$(process_uid "astinuslab-runner@$SERVICE_USER.service")"
check "analysis process exact uid" "$SERVICE_UID" "$(process_uid "astinuslab-analysis@$SERVICE_USER.service")"
check_single_ssh_custody "clean single install"
R1=$(release)

# ── ROW 2: rerun is idempotent ────────────────────────────────────────────────
row "2 rerun"
(cd "$SRC" && sudo "$SRC_INSTALLER" --install-dir "$INSTALL_DIR" --service-user "$SERVICE_USER" -y) > /tmp/r2.log 2>&1
check "rerun exit" 0 "$?"
check "reused the staged release" "1" "$(grep -qi 'already staged' /tmp/r2.log && echo 1 || echo 0)"
check "release unchanged" "$R1" "$(release)"
check "units active" 3 "$(units_active)"
check_single_ssh_custody "single rerun"
check "manifest not duplicated" \
  "$(sudo sort -u /etc/astinuslab/traversal-acl.manifest | wc -l)" \
  "$(sudo cat /etc/astinuslab/traversal-acl.manifest | wc -l)"

# ── ROW 3: update from a changed candidate ────────────────────────────────────
row "3 update from changed candidate"
R3_UNITS=(
  "astinuslab-api@$SERVICE_USER.service"
  "astinuslab-runner@$SERVICE_USER.service"
  "astinuslab-analysis@$SERVICE_USER.service"
)
declare -A R3_PID_BEFORE R3_CWD_BEFORE
for unit in "${R3_UNITS[@]}"; do
  R3_PID_BEFORE["$unit"]="$(systemctl show "$unit" -p ExecMainPID --value)"
  R3_CWD_BEFORE["$unit"]="$(process_cwd "$unit")"
done
printf '# upgrade\n' >> "$CHANGED_SRC/README.md"
(cd "$CHANGED_SRC" && sudo "$CHANGED_INSTALLER" --install-dir "$INSTALL_DIR" --service-user "$SERVICE_USER" -y) > /tmp/r3.log 2>&1
check "upgrade exit" 0 "$?"
check "release changed" "changed" "$([[ "$(release)" != "$R1" ]] && echo changed || echo same)"
for unit in "${R3_UNITS[@]}"; do
  check "$unit process replaced" replaced \
    "$([[ "$(systemctl show "$unit" -p ExecMainPID --value)" != "${R3_PID_BEFORE[$unit]}" ]] \
      && echo replaced || echo stale)"
  check "$unit left its prior cwd" changed \
    "$([[ "$(process_cwd "$unit")" != "${R3_CWD_BEFORE[$unit]}" ]] && echo changed || echo stale)"
  check "$unit runs the new release" "$(readlink -f "$INSTALL_DIR/current")" \
    "$(process_cwd "$unit")"
done
check "units active" 3 "$(units_active)"
check "health" 200 "$(health)"
# Return to the exact immutable candidate before rollback rows. The changed
# source above is a matrix-owned fixture used only to force a distinct release.
(cd "$SRC" && sudo "$SRC_INSTALLER" --install-dir "$INSTALL_DIR" --service-user "$SERVICE_USER" -y) \
  >/tmp/r3-exact-restore.log 2>&1
check "exact candidate restore exit" 0 "$?"
check "exact candidate release restored" "$R1" "$(release)"
check "exact candidate health restored" 200 "$(wait_health)"
check_single_ssh_custody "single changed-candidate upgrade"
R3=$(release)

# ── ROW 4: a failed migration restores both halves ────────────────────────────
row "4 failed migration"
sudo -u "$SERVICE_USER" env ASTINUSLAB_DATA_DIR="$INSTALL_DIR" \
  "$INSTALL_DIR/current/.venv/bin/python" - "$(db_path)" >/dev/null <<'PYEOF'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute("create table if not exists acceptance_marker (v text)")
c.execute("insert into acceptance_marker values ('row4')")
c.commit()
PYEOF
cat > /tmp/drop.py <<PYEOF
import sqlite3
c = sqlite3.connect("$(db_path)")
c.execute("drop table acceptance_marker")
c.commit()
PYEOF
printf '# failed migration\n' >> "$CHANGED_SRC/README.md"
# The operator shell intentionally owns the /tmp acceptance log.
# shellcheck disable=SC2024
sudo bash -c "
  source \"$CHANGED_INSTALLER\"
  apply_db_migrations() {
    ( cd \"\$RELEASE_DIR\" && ASTINUSLAB_DATA_DIR=$INSTALL_DIR \"\$RELEASE_DIR/.venv/bin/python\" /tmp/drop.py )
    return 1
  }
  cd $CHANGED_SRC && main --install-dir $INSTALL_DIR --service-user $SERVICE_USER -y
" > /tmp/r4.log 2>&1
check "installer reported failure" "1" "$?"
check "release rolled back" "$R3" "$(release)"
check "dropped table restored" "True" \
  "$(sudo -u "$SERVICE_USER" env ASTINUSLAB_DATA_DIR="$INSTALL_DIR" "$INSTALL_DIR/current/.venv/bin/python" - "$(db_path)" <<'PYEOF'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
print(bool(c.execute("select count(*) from sqlite_master where type='table' and name='acceptance_marker'").fetchone()[0]))
PYEOF
)"
check "db owner preserved" "$SERVICE_USER:astinuslab" "$(sudo stat -c '%U:%G' "$(db_path)")"
check "units active" 3 "$(units_active)"
check "restored release is serving" 200 "$(health)"

# ── ROW 5: a real failed service start rolls back ─────────────────────────────
row "5 failed service start"
printf '# failed service\n' >> "$CHANGED_SRC/README.md"
R5_BEFORE=$(release)
R5_PRIVILEGED_BEFORE=$(privileged_state_digest)
R5_UNIT_STATE_BEFORE=$(service_state_digest)
R5_CWD_BEFORE=$(process_cwd "astinuslab-api@$SERVICE_USER.service")
# The operator shell intentionally owns the /tmp acceptance log.
# shellcheck disable=SC2024
sudo env MATRIX_SRC="$CHANGED_SRC" MATRIX_INSTALL_DIR="$INSTALL_DIR" MATRIX_SERVICE_USER="$SERVICE_USER" bash <<'BASH' \
  > /tmp/r5.log 2>&1
source "$MATRIX_SRC/ops/deploy/install.sh"
eval "$(declare -f control_services | sed '1s/^control_services /real_control_services /')"
forced_failure=false
control_services() {
  if ! $forced_failure; then
    forced_failure=true
    local unit="astinuslab-api@${SERVICE_USER}.service"
    local dropin_dir="/run/systemd/system/${unit}.d"
    install -d -o root -g root -m 0755 "$dropin_dir"
    printf '[Service]\nExecStart=\nExecStart=/bin/false\n' > "$dropin_dir/acceptance-failure.conf"
    systemctl daemon-reload
    local result
    if real_control_services; then result=0; else result=$?; fi
    rm -f -- "$dropin_dir/acceptance-failure.conf"
    rmdir -- "$dropin_dir"
    systemctl daemon-reload
    [[ "$result" -ne 0 ]] || return 1
    return "$result"
  fi
  real_control_services
}
cd "$MATRIX_SRC"
main --install-dir "$MATRIX_INSTALL_DIR" --service-user "$MATRIX_SERVICE_USER" -y
BASH
check "installer reported failure" "1" "$?"
check "release rolled back" "$R5_BEFORE" "$(release)"
check "access manifests and helpers restored" "$R5_PRIVILEGED_BEFORE" "$(privileged_state_digest)"
check "unit enablement restored" "$R5_UNIT_STATE_BEFORE" "$(service_state_digest)"
check "process cwd restored" "$R5_CWD_BEFORE" "$(process_cwd "astinuslab-api@$SERVICE_USER.service")"
check "units active" 3 "$(units_active)"
check "health" 200 "$(health)"

# ── ROW 6: deep readiness fails after real services start ─────────────────────
row "6 failed readiness"
printf '# failed readiness\n' >> "$CHANGED_SRC/README.md"
R6_BEFORE=$(release)
R6_PRIVILEGED_BEFORE=$(privileged_state_digest)
# Fail the candidate's first deep probe only. Rollback calls the original probe
# against the restored release, so this row proves both failure detection and a
# serving recovery without conflating readiness with a systemd start failure.
# The operator shell intentionally owns the /tmp acceptance log.
# shellcheck disable=SC2024
sudo env MATRIX_SRC="$CHANGED_SRC" MATRIX_INSTALL_DIR="$INSTALL_DIR" MATRIX_SERVICE_USER="$SERVICE_USER" bash <<'BASH' \
  > /tmp/r6.log 2>&1
source "$MATRIX_SRC/ops/deploy/install.sh"
eval "$(declare -f verify_release_readiness | sed '1s/^verify_release_readiness /real_verify_release_readiness /')"
forced_readiness_failure=false
verify_release_readiness() {
  if ! $forced_readiness_failure; then
    forced_readiness_failure=true
    return 1
  fi
  real_verify_release_readiness "$@"
}
cd "$MATRIX_SRC"
main --install-dir "$MATRIX_INSTALL_DIR" --service-user "$MATRIX_SERVICE_USER" -y
BASH
check "installer reported failure" "1" "$?"
check "release rolled back" "$R6_BEFORE" "$(release)"
check "readiness rollback restored privileged state" "$R6_PRIVILEGED_BEFORE" "$(privileged_state_digest)"
check "units active" 3 "$(units_active)"
check "restored release is serving" 200 "$(health)"

# ── ROW 7: a custom storage root ──────────────────────────────────────────────
row "7 custom storage root"
if [[ "$(dirname "$CUSTOM_ROOT")" == /srv/astinuslab-acceptance ]] \
  && sudo test -e /srv/astinuslab-acceptance; then
  echo "refusing pre-existing matrix cleanup root: /srv/astinuslab-acceptance" >&2
  exit 2
fi
# The stable astinuslab group keeps the root traversable/writable across the
# later split-identity transition without making the parent world-traversable.
# qemu is not in this group, so configure-privileged-roots must still apply and
# record its own named traversal ACL.
sudo install -d -o root -g astinuslab -m 0750 "$(dirname "$CUSTOM_ROOT")"
sudo install -d -o "$SERVICE_USER" -g astinuslab -m 2770 "$CUSTOM_ROOT"
if [[ "$(dirname "$CUSTOM_ROOT")" == /srv/astinuslab-acceptance ]]; then
  sudo install -o root -g root -m 0600 /dev/null \
    /srv/astinuslab-acceptance/.upgrade-matrix-owned
fi
printf '\nASTINUSLAB_STORAGE_DIR=%s\n' "$CUSTOM_ROOT" \
  | sudo tee -a "$INSTALL_DIR/config/astinuslab.env" >/dev/null
R7_UNITS=(
  "astinuslab-api@$SERVICE_USER.service"
  "astinuslab-runner@$SERVICE_USER.service"
  "astinuslab-analysis@$SERVICE_USER.service"
)
declare -A R7_PID_BEFORE
for unit in "${R7_UNITS[@]}"; do
  R7_PID_BEFORE["$unit"]="$(systemctl show "$unit" -p ExecMainPID --value)"
done
# The operator shell intentionally owns the /tmp acceptance log.
# shellcheck disable=SC2024
sudo "$(privileged_roots_path "$INSTALL_DIR/current")" \
  --install-dir "$INSTALL_DIR" --storage-root "$CUSTOM_ROOT" --restart > /tmp/r6.log 2>&1
check "installed helper accepted a disjoint root" 0 "$?"
check "policy names the custom root" "1" \
  "$(sudo grep -qF "$CUSTOM_ROOT" /etc/astinuslab/acl-policy.json && echo 1 || echo 0)"
check "units render the custom root" "1" \
  "$(grep -qF "$CUSTOM_ROOT" "/etc/systemd/system/astinuslab-api@.service" && echo 1 || echo 0)"
check "units active after custom-root restart" 3 "$(units_active)"
check "health after custom-root restart" 200 "$(wait_health)"
check "runtime uses the custom root" "$CUSTOM_ROOT" "$(runtime_storage_root)"
for unit in "${R7_UNITS[@]}"; do
  check "$unit restarted for custom root" replaced \
    "$([[ "$(systemctl show "$unit" -p ExecMainPID --value)" != "${R7_PID_BEFORE[$unit]}" ]] \
      && echo replaced || echo stale)"
  check "$unit custom-root cwd" "$(readlink -f "$INSTALL_DIR/current")" "$(process_cwd "$unit")"
  check "$unit custom-root uid" "$(id -u "$SERVICE_USER")" "$(process_uid "$unit")"
done
check "custom-root MAC ownership recorded" 1 "$(custom_root_mac_recorded && echo 1 || echo 0)"
check "custom-root MAC policy effective" 1 "$(custom_root_mac_effective && echo 1 || echo 0)"
CUSTOM_RUNTIME_PROBE="$CUSTOM_ROOT/.runtime-write-probe"
sudo -u "$SERVICE_USER" python3 - "$CUSTOM_RUNTIME_PROBE" <<'PYEOF'
import os
import sys

descriptor = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
try:
    os.write(descriptor, b"astinuslab custom-root acceptance\n")
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PYEOF
check "custom-root runtime write works" 0 "$?"
sudo rm -f -- "$CUSTOM_RUNTIME_PROBE"
CUSTOM_PROBE="$CUSTOM_ROOT/acceptance-probe"
sudo install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$CUSTOM_PROBE"
# The operator shell intentionally owns the /tmp acceptance log.
# shellcheck disable=SC2024
sudo -u "$SERVICE_USER" sudo -n /usr/local/libexec/astinuslab/fix_libvirt_path_acl.sh "$CUSTOM_PROBE" \
  > /tmp/r6-helper.log 2>&1
check "custom-root ACL helper works" 0 "$?"
check "custom-root qemu ACL effective" 1 \
  "$(sudo getfacl -cpe "$CUSTOM_PROBE" 2>/dev/null | grep -Eq '^user:libvirt-qemu:.*x' && echo 1 || echo 0)"
check "libvirt opens a disk from the custom root" 0 "$(custom_root_libvirt_disk_open; echo $?)"

# ── ROW 8: migrate a live single-identity install to split identities ─────────
row "8 split identity migration"
# Seed custody under the old identity so migration applies the real ACL policy.
# A non-image suffix prevents catalog discovery from generating a new sidecar.
sudo test -d "$CUSTOM_ROOT/dumps" || \
  sudo install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 2750 "$CUSTOM_ROOT/dumps" || exit 1
sudo install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0640 /dev/null \
  "$INSTALL_DIR/images/definitions/.matrix-custody.probe" || exit 1
sudo install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0660 /dev/null \
  "$INSTALL_DIR/vol3/symbols/.matrix-custody.json" || exit 1
sudo install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0440 /dev/null \
  "$CUSTOM_ROOT/dumps/.matrix-custody.bin" || exit 1
(cd "$SRC" && sudo "$SRC_INSTALLER" --install-dir "$INSTALL_DIR" \
  --service-user "$SERVICE_USER" --storage-root "$CUSTOM_ROOT" --split-users -y) > /tmp/r7.log 2>&1
check "split migration exit" 0 "$?"
check "runtime identity marker valid" 0 "$(runtime_identity_marker_valid; echo $?)"
check "split units active" 3 "$(units_active)"
check "health under split identities" 200 "$(health)"
check "api process identity" astinuslab-api "$(process_owner astinuslab-api.service)"
check "runner process identity" astinuslab-runner "$(process_owner astinuslab-runner.service)"
check "analysis process identity" astinuslab-analysis "$(process_owner astinuslab-analysis.service)"
check "api process exact uid" "$(id -u astinuslab-api)" "$(process_uid astinuslab-api.service)"
check "runner process exact uid" "$(id -u astinuslab-runner)" "$(process_uid astinuslab-runner.service)"
check "analysis process exact uid" "$(id -u astinuslab-analysis)" "$(process_uid astinuslab-analysis.service)"
check "split role logs written and API-readable" 0 "$(verify_split_role_logs; echo $?)"
# The operator shell intentionally owns the /tmp acceptance log.
# shellcheck disable=SC2024
sudo -u astinuslab-runner sudo -n /usr/local/libexec/astinuslab/libvirt_storage_admin.sh noop \
  > /tmp/r7-runner-helper.log 2>&1
check "runner helper access" 0 "$?"
check "api helper access denied" 0 \
  "$(sudo -u astinuslab-api sudo -n /usr/local/libexec/astinuslab/libvirt_storage_admin.sh noop \
    >/dev/null 2>&1 && echo 1 || echo 0)"
R8=$(release)
check_split_custody "pre-upgrade"
R9_CUSTODY_RECEIPTS="$(sudo mktemp -d /tmp/astinuslab-matrix-custody.XXXXXXXX)" || exit 1
printf 'split_custody_receipts=%s\n' "$R9_CUSTODY_RECEIPTS"
R9_MUTABLE_CUSTODY_BEFORE="$(split_mutable_custody_digest "$R9_CUSTODY_RECEIPTS/before.json")" || exit 1

# ── ROW 9: update an already-split installation without remigrating identities ─
row "9 existing split upgrade"
R9_UNITS=(astinuslab-api.service astinuslab-runner.service astinuslab-analysis.service)
declare -A R9_PID_BEFORE R9_CWD_BEFORE
for unit in "${R9_UNITS[@]}"; do
  R9_PID_BEFORE["$unit"]="$(systemctl show "$unit" -p ExecMainPID --value)"
  R9_CWD_BEFORE["$unit"]="$(process_cwd "$unit")"
done
printf '# existing split upgrade\n' >> "$CHANGED_SRC/README.md"
(cd "$CHANGED_SRC" && sudo "$CHANGED_INSTALLER" --install-dir "$INSTALL_DIR" \
  --service-user "$SERVICE_USER" --storage-root "$CUSTOM_ROOT" --split-users -y) > /tmp/r8.log 2>&1
check "existing split upgrade exit" 0 "$?"
check "existing split path selected" 1 \
  "$(grep -q 'Updating an existing split-identity installation' /tmp/r8.log && echo 1 || echo 0)"
check "split release changed" changed "$([[ "$(release)" != "$R8" ]] && echo changed || echo same)"
for unit in "${R9_UNITS[@]}"; do
  check "$unit split process replaced" replaced \
    "$([[ "$(systemctl show "$unit" -p ExecMainPID --value)" != "${R9_PID_BEFORE[$unit]}" ]] \
      && echo replaced || echo stale)"
  check "$unit left prior split cwd" changed \
    "$([[ "$(process_cwd "$unit")" != "${R9_CWD_BEFORE[$unit]}" ]] && echo changed || echo stale)"
  check "$unit split process new cwd" "$(readlink -f "$INSTALL_DIR/current")" "$(process_cwd "$unit")"
done
check "existing split api process exact uid" "$(id -u astinuslab-api)" \
  "$(process_uid astinuslab-api.service)"
check "existing split runner process exact uid" "$(id -u astinuslab-runner)" \
  "$(process_uid astinuslab-runner.service)"
check "existing split analysis process exact uid" "$(id -u astinuslab-analysis)" \
  "$(process_uid astinuslab-analysis.service)"
check "runtime identity marker remains valid" 0 "$(runtime_identity_marker_valid; echo $?)"
check "split units remain active" 3 "$(units_active)"
check "split health" 200 "$(health)"
check "upgraded split role logs written and API-readable" 0 "$(verify_split_role_logs; echo $?)"
check "split runtime keeps custom root" "$CUSTOM_ROOT" "$(runtime_storage_root)"
check_split_custody "post-upgrade"
check "split mutable custody unchanged" "$R9_MUTABLE_CUSTODY_BEFORE" \
  "$(split_mutable_custody_digest "$R9_CUSTODY_RECEIPTS/after.json")"
check "split process runs the new release" "$(readlink -f "$INSTALL_DIR/current")" \
  "$(process_cwd astinuslab-api.service)"
check "split schema at candidate head" "$(head_revision)" "$(schema)"
# Restore the exact immutable candidate before the rollback proof.
(cd "$SRC" && sudo "$SRC_INSTALLER" --install-dir "$INSTALL_DIR" \
  --service-user "$SERVICE_USER" --storage-root "$CUSTOM_ROOT" --split-users -y) \
  >/tmp/r8-exact-restore.log 2>&1
check "exact split candidate restore exit" 0 "$?"
check "exact split candidate release restored" "$R8" "$(release)"
check "exact split candidate health restored" 200 "$(wait_health)"

# ── ROW 10: a failed existing-split readiness restores privileged state ───────
row "10 existing split rollback"
R10_BEFORE=$(release)
PRIVILEGED_SHA_BEFORE="$(privileged_state_digest)"
UNIT_STATE_BEFORE="$(service_state_digest)"
declare -A R10_PID_BEFORE R10_CWD_BEFORE
for unit in "${R9_UNITS[@]}"; do
  R10_PID_BEFORE["$unit"]="$(systemctl show "$unit" -p ExecMainPID --value)"
  R10_CWD_BEFORE["$unit"]="$(process_cwd "$unit")"
done
DATABASE_REVISION_BEFORE="$(schema)"
DATABASE_CONTENT_BEFORE="$(database_content)"
printf '# existing split rollback candidate\n' >> "$CHANGED_SRC/README.md"
printf '# acceptance candidate must be rolled back\n' >> "$CHANGED_SRC/ops/systemd/astinuslab-api.service"
# The operator shell intentionally owns the /tmp acceptance log.
# shellcheck disable=SC2024
sudo env MATRIX_SRC="$CHANGED_SRC" MATRIX_INSTALL_DIR="$INSTALL_DIR" \
  MATRIX_SERVICE_USER="$SERVICE_USER" MATRIX_CUSTOM_ROOT="$CUSTOM_ROOT" bash <<'BASH' \
  > /tmp/r10-rollback.log 2>&1
source "$MATRIX_SRC/ops/deploy/install.sh"
eval "$(declare -f verify_split_services | sed '1s/^verify_split_services /real_verify_split_services /')"
forced_split_readiness_failure=false
verify_split_services() {
  if ! $forced_split_readiness_failure; then
    forced_split_readiness_failure=true
    return 1
  fi
  real_verify_split_services "$@"
}
cd "$MATRIX_SRC"
main --install-dir "$MATRIX_INSTALL_DIR" --service-user "$MATRIX_SERVICE_USER" \
  --storage-root "$MATRIX_CUSTOM_ROOT" --split-users -y
BASH
check "installer reported failure" "1" "$?"
check "split release rolled back" "$R10_BEFORE" "$(release)"
check "privileged files restored" "$PRIVILEGED_SHA_BEFORE" \
  "$(privileged_state_digest)"
check "unit enablement restored" "$UNIT_STATE_BEFORE" "$(service_state_digest)"
for unit in "${R9_UNITS[@]}"; do
  check "$unit rollback process replaced" replaced \
    "$([[ "$(systemctl show "$unit" -p ExecMainPID --value)" != "${R10_PID_BEFORE[$unit]}" ]] \
      && echo replaced || echo stale)"
  check "$unit rollback cwd restored" "${R10_CWD_BEFORE[$unit]}" "$(process_cwd "$unit")"
done
check "database revision restored" "$DATABASE_REVISION_BEFORE" "$(schema)"
check "database content restored" "$DATABASE_CONTENT_BEFORE" "$(database_content)"
check "runtime identity marker remains valid" 0 "$(runtime_identity_marker_valid; echo $?)"
check "split units active after rollback" 3 "$(units_active)"
check "split health after rollback" 200 "$(health)"
check "rolled-back split role logs written and API-readable" 0 "$(verify_split_role_logs; echo $?)"
check "api identity after rollback" astinuslab-api "$(process_owner astinuslab-api.service)"
check "runner identity after rollback" astinuslab-runner "$(process_owner astinuslab-runner.service)"
check "analysis identity after rollback" astinuslab-analysis "$(process_owner astinuslab-analysis.service)"
check "api exact uid after rollback" "$(id -u astinuslab-api)" "$(process_uid astinuslab-api.service)"
check "runner exact uid after rollback" "$(id -u astinuslab-runner)" "$(process_uid astinuslab-runner.service)"
check "analysis exact uid after rollback" "$(id -u astinuslab-analysis)" "$(process_uid astinuslab-analysis.service)"

# ── ROW 11: uninstall preserving the install root ─────────────────────────────
row "11 uninstall, preserve"
HOME_ENTRIES_BEFORE=$(getfacl -cp "/home/$SERVICE_USER" 2>/dev/null | grep -c '^user:libvirt-qemu:')
ACL_MANIFEST_SNAPSHOT="/tmp/astinuslab-matrix-acl.$$.manifest"
MAC_MANIFEST_SNAPSHOT="/tmp/astinuslab-matrix-mac.$$.manifest"
sudo cp -- /etc/astinuslab/traversal-acl.manifest "$ACL_MANIFEST_SNAPSHOT"
if sudo test -f /etc/astinuslab/mac-policy.manifest; then
  sudo cp -- /etc/astinuslab/mac-policy.manifest "$MAC_MANIFEST_SNAPSHOT"
else
  sudo install -o root -g root -m 0600 /dev/null "$MAC_MANIFEST_SNAPSHOT"
fi
check "MAC ownership record captured" "1" "$(sudo test -s "$MAC_MANIFEST_SNAPSHOT" && echo 1 || echo 0)"
# The operator shell intentionally owns the /tmp acceptance log.
# shellcheck disable=SC2024
printf 'n\nn\nn\nn\n' | sudo INSTALL_DIR="$INSTALL_DIR" SERVICE_USER="$SERVICE_USER" \
  "$SRC_UNINSTALLER" > /tmp/r9.log 2>&1
check "uninstall exit" 0 "$?"
check "all six units inactive with no PID" 0 "$(managed_units_with_live_pid)"
check "install root preserved" "1" "$(sudo test -d "$INSTALL_DIR" && echo 1 || echo 0)"
check "release tree named in output" "1" "$(grep -qi 'releases/' /tmp/r9.log && echo 1 || echo 0)"
check "units removed" "0" \
  "$(find /etc/systemd/system -maxdepth 1 \( -type f -o -type l \) -name 'astinuslab-*.service' | wc -l)"
check "sudoers removed" "0" "$(sudo test -f /etc/sudoers.d/astinuslab-runner && echo 1 || echo 0)"
check "libexec fully removed" "0" "$(sudo test -d /usr/local/libexec/astinuslab && echo 1 || echo 0)"
check "traversal manifest consumed" "0" "$(sudo test -f /etc/astinuslab/traversal-acl.manifest && echo 1 || echo 0)"
check "all recorded traversal grants revoked" "0" \
  "$(recorded_acl_grants_remaining "$ACL_MANIFEST_SNAPSHOT")"
check "all recorded MAC changes revoked" "0" \
  "$(recorded_mac_changes_remaining "$MAC_MANIFEST_SNAPSHOT")"
check "no standing grant on /opt" "0" "$(getfacl -cp /opt 2>/dev/null | grep -c '^user:libvirt-qemu:')"
check "operator home grants untouched" "$HOME_ENTRIES_BEFORE" \
  "$(getfacl -cp "/home/$SERVICE_USER" 2>/dev/null | grep -c '^user:libvirt-qemu:')"

# ── ROW 12: uninstall deleting the install root ───────────────────────────────
row "12 uninstall, delete"
(cd "$SRC" && sudo "$SRC_INSTALLER" --install-dir "$INSTALL_DIR" \
  --service-user "$SERVICE_USER" --storage-root "$CUSTOM_ROOT" --split-users -y) >/tmp/r10-install.log 2>&1
check "split reinstall exit" 0 "$?"
# The operator shell intentionally owns the /tmp acceptance log.
# shellcheck disable=SC2024
printf 'y\ny\ny\ny\n' | sudo INSTALL_DIR="$INSTALL_DIR" SERVICE_USER="$SERVICE_USER" \
  "$SRC_UNINSTALLER" > /tmp/r10.log 2>&1
check "uninstall exit" 0 "$?"
check "all six units inactive with no PID after delete" 0 "$(managed_units_with_live_pid)"
check "install root removed" "0" "$(sudo test -d "$INSTALL_DIR" && echo 1 || echo 0)"
check "no standing grant on /opt" "0" "$(getfacl -cp /opt 2>/dev/null | grep -c '^user:libvirt-qemu:')"
check "libexec fully removed" "0" "$(sudo test -d /usr/local/libexec/astinuslab && echo 1 || echo 0)"
sudo rm -f -- "$ACL_MANIFEST_SNAPSHOT" "$MAC_MANIFEST_SNAPSHOT"

# External evidence storage is intentionally preserved by uninstall. The
# default acceptance root is matrix-owned, so remove that exact tree only after
# the product assertions finish; a caller-supplied root is never deleted here.
CUSTOM_PARENT="$(dirname "$CUSTOM_ROOT")"
if [[ "$CUSTOM_PARENT" == /srv/astinuslab-acceptance ]] \
  && sudo test -f "$CUSTOM_PARENT/.upgrade-matrix-owned"; then
  sudo find "$CUSTOM_PARENT" -xdev -depth -delete
fi
if [[ "$CUSTOM_PARENT" == /srv/astinuslab-acceptance ]]; then
  check "test-owned custom root removed" "0" \
    "$(sudo test -e "$CUSTOM_PARENT" && echo 1 || echo 0)"
else
  check "caller-supplied custom root preserved" "1" \
    "$(sudo test -d "$CUSTOM_ROOT" && echo 1 || echo 0)"
fi

# ── ROW 13: reject an install below an operator-writable parent ───────────────
# Root performs path-based release mutations. The only enforceable stable-path
# boundary is a root-owned, non-writable existing ancestor chain, so an ordinary
# operator home must be rejected before any install-root or ACL mutation.
row "13 unsafe install parent rejection"
HOME_ROOT="/home/$SERVICE_USER/astinuslab-acceptance"
HOME_GRANTS_BEFORE=$(getfacl -cp "/home/$SERVICE_USER" 2>/dev/null | grep -c '^user:libvirt-qemu:')
HOME_MODE_BEFORE="$(stat -c '%u:%g:%a' "/home/$SERVICE_USER")"
(cd "$SRC" && sudo "$SRC_INSTALLER" --install-dir "$HOME_ROOT" --service-user "$SERVICE_USER" -y) \
  > /tmp/r9.log 2>&1
HOME_INSTALL_STATUS=$?
check "unsafe home-parent install rejected" "nonzero" \
  "$([[ "$HOME_INSTALL_STATUS" -ne 0 ]] && echo nonzero || echo zero)"
check "rejection names parent authority" 1 \
  "$(grep -q 'Unsafe install-root parent authority' /tmp/r9.log && echo 1 || echo 0)"
check "operator home grants untouched" "$HOME_GRANTS_BEFORE" \
  "$(getfacl -cp "/home/$SERVICE_USER" 2>/dev/null | grep -c '^user:libvirt-qemu:')"
check "operator home custody untouched" "$HOME_MODE_BEFORE" "$(stat -c '%u:%g:%a' "/home/$SERVICE_USER")"
check "rejected install root never created" "0" "$(sudo test -e "$HOME_ROOT" && echo 1 || echo 0)"
check "libexec fully removed" "0" "$(sudo test -d /usr/local/libexec/astinuslab && echo 1 || echo 0)"

check "exact candidate source remained immutable" "$SOURCE_DIGEST_BEFORE" \
  "$(source_tree_digest "$SRC")"
if [[ "$RELEASE_MODE" == upgrade ]]; then
  check "genuine prior source remained immutable" "$PRIOR_SOURCE_DIGEST" \
    "$(source_tree_digest "$PRIOR_SRC")"
fi
CHANGED_SOURCE_PATH="$CHANGED_SRC"
cleanup_matrix_tree "$CHANGED_SOURCE_PATH"
CHANGED_SOURCE_CLEANUP=$?
check "matrix-owned changed source removed" 0 "$CHANGED_SOURCE_CLEANUP"
CHANGED_SRC=""

printf '\n════ TOTAL ════\n  PASS=%s FAIL=%s\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
