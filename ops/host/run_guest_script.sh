#!/usr/bin/env bash
# run_guest_script.sh — copy a script to the guest and execute it
# Usage: run_guest_script.sh <guest_ip> <ssh_key> <script_path> [timeout_seconds]
set -euo pipefail

GUEST_IP="${1:?guest_ip required}"
SSH_KEY="${2:?ssh_key required}"
SCRIPT_PATH="${3:?script_path required}"
TIMEOUT="${4:-300}"

SSH_OPTS=("-i" "$SSH_KEY" "-o" "StrictHostKeyChecking=no" "-o" "ConnectTimeout=10" "-o" "BatchMode=yes")

if [[ ! -f "${SCRIPT_PATH}" ]]; then
    echo "ERROR: script not found: ${SCRIPT_PATH}" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GUEST_SCRIPTS_DIR="$(realpath "${SCRIPT_DIR}/../../guest/scripts")"

# Wait for guest SSH before the first scp. A freshly booted VM reports a DHCP
# lease (so the caller has an IP) long before sshd is up and cloud-init has
# authorized the key, which otherwise yields "connect: Connection refused".
# Poll a trivial remote command until it succeeds or the deadline passes.
SSH_READY_TIMEOUT="${SSH_READY_TIMEOUT:-180}"
echo "[run_guest_script] Waiting up to ${SSH_READY_TIMEOUT}s for SSH on ${GUEST_IP}..."
ssh_deadline=$(( $(date +%s) + SSH_READY_TIMEOUT ))
until ssh "${SSH_OPTS[@]}" "${GUEST_USER:-ubuntu}@${GUEST_IP}" true 2>/dev/null; do
    if (( $(date +%s) >= ssh_deadline )); then
        echo "ERROR: guest SSH not ready on ${GUEST_IP} after ${SSH_READY_TIMEOUT}s" >&2
        exit 1
    fi
    sleep 3
done

echo "[run_guest_script] Copying scripts to guest ${GUEST_IP}..."
scp "${SSH_OPTS[@]}" "${SCRIPT_PATH}" "${GUEST_USER:-ubuntu}@${GUEST_IP}:/tmp/astinuslab_scenario_script.sh"
scp "${SSH_OPTS[@]}" "${GUEST_SCRIPTS_DIR}/detect_environment.sh" "${GUEST_SCRIPTS_DIR}/pkg_helper.sh" "${GUEST_USER:-ubuntu}@${GUEST_IP}:/tmp/"

echo "[run_guest_script] Executing script (timeout=${TIMEOUT}s)..."
REMOTE_ENV=()
for name in TARGET_DISTRO TARGET_VERSION TARGET_FAMILY TARGET_ARCH; do
    value="${!name:-}"
    if [[ -n "${value}" ]]; then
        REMOTE_ENV+=("${name}=${value}")
    fi
done

REMOTE_PREFIX=""
if [[ ${#REMOTE_ENV[@]} -gt 0 ]]; then
    printf -v REMOTE_PREFIX "%q " "${REMOTE_ENV[@]}"
fi

timeout "${TIMEOUT}" ssh "${SSH_OPTS[@]}" "${GUEST_USER:-ubuntu}@${GUEST_IP}" "${REMOTE_PREFIX}bash /tmp/astinuslab_scenario_script.sh"
EXIT_CODE=$?

if [[ ${EXIT_CODE} -eq 124 ]]; then
    echo "ERROR: scenario script timed out after ${TIMEOUT}s" >&2
    exit 124
fi

echo "[run_guest_script] Script completed (exit=${EXIT_CODE})"
exit ${EXIT_CODE}
