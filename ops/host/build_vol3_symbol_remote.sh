#!/usr/bin/env bash
# Build and fetch a Volatility 3 ISF symbol table from a running guest VM.
#
# Runs guest/scripts/build_vol3_symbol.sh on the guest via SSH (stdin pipe),
# then SCPs the resulting .json to the local output directory.
#
# Usage:
#   build_vol3_symbol_remote.sh <guest_ip> <ssh_private_key> \
#                               <remote_tmp_dir> <local_output_dir>
#
# Environment:
#   GUEST_USER  (default: ubuntu)
#
# Outputs keyed lines for orchestrator parsing:
#   vol3_symbol_path=<local_path>
#   vol3_symbol_filename=<filename>
#   kernel_release=<release>
set -euo pipefail

if [[ $# -lt 4 ]]; then
    echo "Usage: $0 <guest_ip> <ssh_private_key> <remote_tmp_dir> <local_output_dir>" >&2
    exit 1
fi

: "${DWARF2JSON_LOCAL:?DWARF2JSON_LOCAL is required}"
: "${DWARF2JSON_SHA256:?DWARF2JSON_SHA256 is required}"

EXPECTED_HASH="${DWARF2JSON_SHA256,,}"
if [[ ! "$EXPECTED_HASH" =~ ^[0-9a-f]{64}$ ]]; then
    echo "ERROR: DWARF2JSON_SHA256 must be a bare 64-character hexadecimal hash" >&2
    exit 1
fi
if [[ ! -f "$DWARF2JSON_LOCAL" ]]; then
    echo "ERROR: DWARF2JSON_LOCAL must be a regular file" >&2
    exit 1
fi
if [[ ! -x "$DWARF2JSON_LOCAL" ]]; then
    echo "ERROR: DWARF2JSON_LOCAL must be executable" >&2
    exit 1
fi

actual_sha256="$(sha256sum -- "$DWARF2JSON_LOCAL" | awk '{print $1}')"
if [[ "$actual_sha256" != "$EXPECTED_HASH" ]]; then
    echo "ERROR: local dwarf2json sha256 mismatch" >&2
    echo "  expected: $EXPECTED_HASH" >&2
    echo "  actual:   $actual_sha256" >&2
    exit 1
fi
DWARF2JSON_LOCAL="$(realpath -- "$DWARF2JSON_LOCAL")"

GUEST_IP="$1"
SSH_KEY="$2"
REMOTE_TMP_DIR="$3"
LOCAL_OUTPUT_DIR="$4"
GUEST_USER="${GUEST_USER:-ubuntu}"

# `ssh` has no `--` end-of-options terminator (OpenSSH prints its usage for it),
# so the two scp calls below can be hardened with `--` but SSH_COMMAND cannot.
# A GUEST_USER or GUEST_IP that begins with `-` would therefore be read by ssh
# as an option (e.g. -oProxyCommand=...). Both come from operator-authored
# catalog YAML, so this is a defence-in-depth check, not a trust boundary.
#
# Username pattern: leading character may not be `-`; the rest allows the
# hyphen, dot and underscore that real cloud-image accounts use (cloud-user,
# opc, almalinux, ubuntu, ...). Deliberately no spaces or shell metacharacters.
if [[ ! "$GUEST_USER" =~ ^[A-Za-z0-9._][A-Za-z0-9._-]*$ ]]; then
    echo "ERROR: invalid GUEST_USER: ${GUEST_USER}" >&2
    exit 1
fi
# Address pattern: the caller passes a libvirt DHCP-lease address, so IPv4
# dotted-quad and IPv6 hex/colon forms both pass while anything starting with
# `-` is refused. IPv6 zone indices (fe80::1%eth0) and hostnames are out of
# scope — nothing in the tree supplies them here.
if [[ ! "$GUEST_IP" =~ ^[0-9a-fA-F.:]+$ ]]; then
    echo "ERROR: invalid guest address: ${GUEST_IP}" >&2
    exit 1
fi
# The EXIT trap installed below runs `rm -rf` on REMOTE_TMP_DIR *on the guest*,
# so "/" or "/tmp" here would wipe the guest. Confine it to a non-empty path
# beneath /tmp or /var/tmp, which is what the only in-tree caller passes
# (vol3/scripts/build_symbol_from_live_guest.sh: /tmp/astinuslab-vol3-$$).
case "$REMOTE_TMP_DIR" in
    /tmp/?* | /var/tmp/?*) ;;
    *)
        echo "ERROR: remote tmp dir must be a non-empty path under /tmp/ or /var/tmp/: ${REMOTE_TMP_DIR}" >&2
        exit 1
        ;;
esac
# ...and reject the forms that walk back out of it: /tmp/.. and /tmp//.
case "$REMOTE_TMP_DIR" in
    */../* | */.. | *//*)
        echo "ERROR: remote tmp dir must not contain '..' or empty path components: ${REMOTE_TMP_DIR}" >&2
        exit 1
        ;;
esac

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GUEST_SCRIPT="$BASE_DIR/guest/scripts/build_vol3_symbol.sh"

SSH_OPTS=(-i "${SSH_KEY}" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 -o BatchMode=yes)
SSH_COMMAND=(ssh "${SSH_OPTS[@]}" "${GUEST_USER}@${GUEST_IP}")

GUEST_STDOUT_FILE=""
GUEST_STDERR_FILE=""
SCP_STDERR_FILE=""

cleanup() {
    local exit_status="$?"
    local tmp_file

    set +e
    printf -v REMOTE_CLEANUP_COMMAND 'rm -rf %q' "$REMOTE_TMP_DIR"
    # Remote output stays fully suppressed: the orchestrator parses keyed
    # `name=value` lines from this script's stdout, and a compromised guest
    # must not be able to inject them from a cleanup command. Report only our
    # own %q-quoted warning on stderr so a leaked guest temp dir is visible.
    if ! "${SSH_COMMAND[@]}" "$REMOTE_CLEANUP_COMMAND" >/dev/null 2>&1; then
        printf 'WARNING: could not remove remote temp dir on guest; it may need manual cleanup: %q\n' \
            "$REMOTE_TMP_DIR" >&2
    fi
    for tmp_file in "$GUEST_STDOUT_FILE" "$GUEST_STDERR_FILE" "$SCP_STDERR_FILE"; do
        if [[ -n "$tmp_file" ]]; then
            rm -f -- "$tmp_file"
        fi
    done
    exit "$exit_status"
}

# Create remote working directory.
#
# Stream discipline for every setup step below (mkdir, upload, chmod), matching
# the cleanup trap above: guest output is captured, never relayed on the success
# path. Both of this script's consumers parse keyed `name=value` lines out of a
# stream the guest could otherwise write into — vol3/scripts/build_symbol_from_
# live_guest.sh greps this script's stdout, and the orchestrator greps a job log
# in which this script's stdout and stderr are merged — so `2>&1` (guest text on
# our stdout) and `1>&2` (guest text on our stderr) are both injection channels.
# On failure the capture is dumped to stderr so the operator keeps the ssh/scp
# diagnostics, and the script exits non-zero, which aborts the wrapper before
# any keyed line is emitted or parsed.
printf -v REMOTE_MKDIR_COMMAND 'mkdir -p %q' "$REMOTE_TMP_DIR"
if ! REMOTE_MKDIR_OUTPUT="$("${SSH_COMMAND[@]}" "$REMOTE_MKDIR_COMMAND" 2>&1)"; then
    echo "ERROR: could not create remote tmp dir: ${REMOTE_TMP_DIR}" >&2
    if [[ -n "$REMOTE_MKDIR_OUTPUT" ]]; then
        echo "--- guest output ---" >&2
        printf '%s\n' "$REMOTE_MKDIR_OUTPUT" >&2
    fi
    exit 1
fi
trap cleanup EXIT

# Transfer the locally verified operator-selected dwarf2json to the guest.
REMOTE_DWARF2JSON_PATH="${REMOTE_TMP_DIR}/dwarf2json"
printf -v REMOTE_DWARF2JSON_WORD '%q' "$REMOTE_DWARF2JSON_PATH"
if ! SCP_UPLOAD_OUTPUT="$(scp "${SSH_OPTS[@]}" -- \
    "$DWARF2JSON_LOCAL" "${GUEST_USER}@${GUEST_IP}:${REMOTE_DWARF2JSON_WORD}" 2>&1)"; then
    echo "ERROR: could not transfer dwarf2json to guest" >&2
    if [[ -n "$SCP_UPLOAD_OUTPUT" ]]; then
        echo "--- scp output ---" >&2
        printf '%s\n' "$SCP_UPLOAD_OUTPUT" >&2
    fi
    exit 1
fi

# Scope of this check: transfer integrity, NOT tamper evidence. The hash is
# computed by the guest's own sha256sum on the guest's own PATH, and the guest
# is the analysis subject (see the host-side validation note below) — a
# compromised guest can report the expected value for a substituted file, and
# nothing prevents a swap between this check and the execution that follows.
# It catches a truncated or corrupted scp, and nothing stronger. The real
# control is that the host verified this exact file against the operator's
# configured pin before the transfer above.
printf -v REMOTE_HASH_COMMAND 'sha256sum -- %q' "$REMOTE_DWARF2JSON_PATH"
if ! REMOTE_HASH_OUTPUT="$("${SSH_COMMAND[@]}" "$REMOTE_HASH_COMMAND")"; then
    echo "ERROR: could not verify dwarf2json on guest" >&2
    exit 1
fi
REMOTE_HASH="$(printf '%s' "$REMOTE_HASH_OUTPUT" | awk '{print $1}')"
REMOTE_HASH="${REMOTE_HASH,,}"
if [[ "$REMOTE_HASH" != "$EXPECTED_HASH" ]]; then
    echo "ERROR: dwarf2json sha256 mismatch on guest." >&2
    echo "  expected: $EXPECTED_HASH" >&2
    echo "  actual:   $REMOTE_HASH" >&2
    echo "  remote:   $(printf '%q' "${REMOTE_TMP_DIR}")/dwarf2json" >&2
    exit 1
fi

printf -v REMOTE_CHMOD_COMMAND 'chmod +x %q' "$REMOTE_DWARF2JSON_PATH"
if ! REMOTE_CHMOD_OUTPUT="$("${SSH_COMMAND[@]}" "$REMOTE_CHMOD_COMMAND" 2>&1)"; then
    echo "ERROR: could not chmod dwarf2json on guest" >&2
    if [[ -n "$REMOTE_CHMOD_OUTPUT" ]]; then
        echo "--- guest output ---" >&2
        printf '%s\n' "$REMOTE_CHMOD_OUTPUT" >&2
    fi
    exit 1
fi

# Run the guest build script via stdin pipe
GUEST_STDOUT_FILE="$(mktemp /tmp/vol3-stdout.XXXXXX)"
GUEST_STDERR_FILE="$(mktemp /tmp/vol3-stderr.XXXXXX)"
SCP_STDERR_FILE="$(mktemp /tmp/vol3-scp-stderr.XXXXXX)"

if [[ -n "${VMLINUX_OVERRIDE:-}" ]]; then
    printf -v REMOTE_BUILD_COMMAND \
        "KERNEL_RELEASE=\$(uname -r) sudo VMLINUX_OVERRIDE=%q VOL3_ALLOW_GUEST_PACKAGE_INSTALL=%q bash -s -- %q" \
        "$VMLINUX_OVERRIDE" "${VOL3_ALLOW_GUEST_PACKAGE_INSTALL:-0}" "$REMOTE_TMP_DIR"
else
    printf -v REMOTE_BUILD_COMMAND \
        "KERNEL_RELEASE=\$(uname -r) sudo VOL3_ALLOW_GUEST_PACKAGE_INSTALL=%q bash -s -- %q" \
        "${VOL3_ALLOW_GUEST_PACKAGE_INSTALL:-0}" "$REMOTE_TMP_DIR"
fi

if ! "${SSH_COMMAND[@]}" "$REMOTE_BUILD_COMMAND" \
    < "$GUEST_SCRIPT" >"$GUEST_STDOUT_FILE" 2> >(tee "$GUEST_STDERR_FILE" >&2); then
    echo "ERROR: guest Vol3 build script failed" >&2
    if [[ -s "$GUEST_STDERR_FILE" ]]; then
        echo "--- guest stderr ---" >&2
        cat "$GUEST_STDERR_FILE" >&2
    fi
    if [[ -s "$GUEST_STDOUT_FILE" ]]; then
        echo "--- guest stdout ---" >&2
        cat "$GUEST_STDOUT_FILE" >&2
    fi
    exit 1
fi

GUEST_OUTPUT="$(cat "$GUEST_STDOUT_FILE")"

# Last occurrence wins: the legitimate guest script emits its keyed lines
# at the very end, so a hostile line printed earlier in guest output loses.
VOL3_REMOTE_PATH="$(echo "$GUEST_OUTPUT" | grep '^vol3_symbol_remote_path=' | tail -n 1 | cut -d= -f2- || true)"
KERNEL_RELEASE="$(echo "$GUEST_OUTPUT" | grep '^kernel_release=' | tail -n 1 | cut -d= -f2- || true)"
BUILD_ID="$(echo "$GUEST_OUTPUT" | grep '^build_id=' | tail -n 1 | cut -d= -f2- || true)"

if [[ -z "$VOL3_REMOTE_PATH" ]]; then
    echo "ERROR: guest script did not emit vol3_symbol_remote_path" >&2
    if [[ -s "$GUEST_STDOUT_FILE" ]]; then
        echo "--- guest stdout ---" >&2
        cat "$GUEST_STDOUT_FILE" >&2
    fi
    if [[ -s "$GUEST_STDERR_FILE" ]]; then
        echo "--- guest stderr ---" >&2
        cat "$GUEST_STDERR_FILE" >&2
    fi
    exit 1
fi

if [[ -z "$KERNEL_RELEASE" ]]; then
    echo "ERROR: guest script did not emit kernel_release" >&2
    if [[ -s "$GUEST_STDOUT_FILE" ]]; then
        echo "--- guest stdout ---" >&2
        cat "$GUEST_STDOUT_FILE" >&2
    fi
    if [[ -s "$GUEST_STDERR_FILE" ]]; then
        echo "--- guest stderr ---" >&2
        cat "$GUEST_STDERR_FILE" >&2
    fi
    exit 1
fi

# Host-side validation of guest-reported values. The guest is the analysis
# subject and may be compromised; nothing it prints is trusted unchecked.
if [[ ! "$KERNEL_RELEASE" =~ ^[a-zA-Z0-9._+-]+$ ]]; then
    echo "ERROR: guest reported invalid kernel_release: ${KERNEL_RELEASE}" >&2
    exit 1
fi
if [[ -n "$BUILD_ID" && ! "$BUILD_ID" =~ ^[0-9a-fA-F]+$ ]]; then
    echo "ERROR: guest reported invalid build_id: ${BUILD_ID}" >&2
    exit 1
fi
# The legitimate guest script always writes the ISF to exactly this path
# (guest/scripts/build_vol3_symbol.sh: ISF_PATH="${REMOTE_TMP_DIR}/linux-${KERNEL_RELEASE}.json"),
# so any other reported path is forged or broken — refuse to scp it.
EXPECTED_REMOTE_PATH="${REMOTE_TMP_DIR}/linux-${KERNEL_RELEASE}.json"
if [[ "$VOL3_REMOTE_PATH" != "$EXPECTED_REMOTE_PATH" ]]; then
    echo "ERROR: guest reported unexpected ISF path: ${VOL3_REMOTE_PATH} (expected ${EXPECTED_REMOTE_PATH})" >&2
    exit 1
fi

SYMBOL_FILENAME="$(basename "$VOL3_REMOTE_PATH")"
mkdir -p "$LOCAL_OUTPUT_DIR"

printf -v VOL3_REMOTE_PATH_WORD '%q' "$VOL3_REMOTE_PATH"
if ! scp "${SSH_OPTS[@]}" -- "${GUEST_USER}@${GUEST_IP}:${VOL3_REMOTE_PATH_WORD}" "${LOCAL_OUTPUT_DIR}/${SYMBOL_FILENAME}" 2>"$SCP_STDERR_FILE"; then
    echo "ERROR: could not download Vol3 symbol table from guest: ${VOL3_REMOTE_PATH}" >&2
    if [[ -s "$SCP_STDERR_FILE" ]]; then
        echo "--- scp stderr ---" >&2
        cat "$SCP_STDERR_FILE" >&2
    fi
    exit 1
fi

LOCAL_SYMBOL_PATH="${LOCAL_OUTPUT_DIR}/${SYMBOL_FILENAME}"
if [[ ! -s "$LOCAL_SYMBOL_PATH" ]]; then
    echo "ERROR: downloaded Vol3 symbol table is empty: ${LOCAL_SYMBOL_PATH}" >&2
    exit 1
fi

echo "vol3_symbol_path=${LOCAL_SYMBOL_PATH}"
echo "vol3_symbol_filename=${SYMBOL_FILENAME}"
echo "kernel_release=${KERNEL_RELEASE}"
if [[ -n "$BUILD_ID" ]]; then
    echo "build_id=${BUILD_ID}"
fi
