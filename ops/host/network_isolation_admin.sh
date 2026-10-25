#!/usr/bin/env bash
# Privileged helper for AstinusLab guest network isolation.
#
# Manages iptables FORWARD rules tagged with astinuslab-isolate-<job_id>.
# Installed root-owned 0755 at /usr/local/libexec/astinuslab/ and invoked
# exclusively via the NOPASSWD sudoers rule installed by ops/deploy/install.sh
# (and ops/host/install_sudoers_astinuslab.sh).
#
# Subcommands:
#   noop                                         — sanity probe (used by
#                                                  require_noninteractive_sudo).
#   apply  <job_id> <guest_ip> [attacker_ip]     — drop guest forwarded
#                                                  traffic; optionally permit
#                                                  attacker ↔ guest.
#   remove <job_id> <guest_ip> [attacker_ip]     — undo what apply created.
#
# All inputs are validated before any iptables command runs:
#   - job_id  : ^[a-zA-Z0-9_-]{1,64}$ (matches Job.id column constraint)
#   - IPv4    : ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ with each octet ≤ 255
#
# The iptables comment string is built inside the helper from the validated
# job_id; callers cannot inject arbitrary comment text.
set -euo pipefail

SUBCOMMAND="${1:-}"
shift || true

# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------
_assert_job_id() {
  local id="$1"
  [[ "$id" =~ ^[a-zA-Z0-9_-]{1,64}$ ]] \
    || { echo "DENY: invalid job_id (expected /^[a-zA-Z0-9_-]{1,64}$/, got '${id}')" >&2; exit 1; }
}

_assert_ipv4() {
  local label="$1" ip="$2"
  [[ "$ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] \
    || { echo "DENY: invalid ${label} IPv4 (got '${ip}')" >&2; exit 1; }
  local IFS='.'
  # shellcheck disable=SC2206
  local -a parts=( $ip )
  for octet in "${parts[@]}"; do
    if (( octet > 255 )); then
      echo "DENY: invalid ${label} IPv4 octet > 255 (got '${ip}')" >&2
      exit 1
    fi
  done
}

# nft iptables also returns 1 when its netlink socket cannot be opened. A
# failed check is absence only when a separate chain read succeeds and the
# exact owned tag is gone. Tags are generated from the validated Job ID, so
# they contain no regex metacharacters. Never interpret listing text as shell.
_confirm_rule_absent() {
  local chain="$1" comment="$2" listing status
  [[ "$comment" =~ ^astinuslab-isolate-[a-zA-Z0-9_-]+$ ]] || return 2
  if listing=$(iptables -S "$chain"); then
    :
  else
    return "$?"
  fi
  if grep -Eq -- "(^|[[:space:]])--comment[[:space:]]+(\"${comment}\"|${comment})([[:space:]]|$)" <<< "$listing"; then
    echo "DENY: isolation tag remains after failed rule query" >&2
    return 1
  else
    status=$?
    [[ "$status" -eq 1 ]] || return "$status"
  fi
  return 0
}

# Insert a rule at position 1 only after confirming its absence. Every
# internal rule spec ends with its exact --comment argument.
_apply_rule_if_absent() {
  local chain="$1" status
  shift
  if iptables -C "$chain" "$@" 2>/dev/null; then
    return 0
  else
    status=$?
    [[ "$status" -eq 1 ]] || return "$status"
    _confirm_rule_absent "$chain" "${!#}" || return "$?"
  fi
  iptables -I "$chain" 1 "$@"
}

# Delete a rule by spec; tolerate missing rules. Loops to clear duplicates
# left by older non-idempotent runs.
_delete_rule_if_present() {
  local chain="$1" status
  shift
  while true; do
    if iptables -C "$chain" "$@" 2>/dev/null; then
      iptables -D "$chain" "$@" 2>/dev/null || return "$?"
    else
      status=$?
      [[ "$status" -eq 1 ]] || return "$status"
      _confirm_rule_absent "$chain" "${!#}"
      return "$?"
    fi
  done
}

case "$SUBCOMMAND" in
  noop)
    exit 0
    ;;
  apply)
    if [[ $# -lt 2 || $# -gt 3 ]]; then
      echo "Usage: $0 apply <job_id> <guest_ip> [attacker_ip]" >&2
      exit 1
    fi
    JOB_ID="$1"
    GUEST_IP="$2"
    ATTACKER_IP="${3:-}"
    _assert_job_id "$JOB_ID"
    _assert_ipv4 guest "$GUEST_IP"
    [[ -n "$ATTACKER_IP" ]] && _assert_ipv4 attacker "$ATTACKER_IP"

    COMMENT="astinuslab-isolate-${JOB_ID}"

    # Install the deny before touching exceptions so a partial failure keeps
    # other guest forwarding blocked. Position-1 inserts reverse call order.
    _apply_rule_if_absent FORWARD -s "$GUEST_IP" -j DROP -m comment --comment "$COMMENT"
    if [[ -n "$ATTACKER_IP" ]]; then
      # Refresh both exceptions even when present: a legacy DROP or a partial
      # earlier apply may have landed above them. Remove duplicate owned specs.
      _delete_rule_if_present FORWARD -s "$ATTACKER_IP" -d "$GUEST_IP" -j ACCEPT -m comment --comment "${COMMENT}-att-rev"
      _delete_rule_if_present FORWARD -s "$GUEST_IP" -d "$ATTACKER_IP" -j ACCEPT -m comment --comment "${COMMENT}-att-fwd"
      _apply_rule_if_absent FORWARD -s "$ATTACKER_IP" -d "$GUEST_IP" -j ACCEPT -m comment --comment "${COMMENT}-att-rev"
      _apply_rule_if_absent FORWARD -s "$GUEST_IP" -d "$ATTACKER_IP" -j ACCEPT -m comment --comment "${COMMENT}-att-fwd"
    fi
    ;;
  remove)
    if [[ $# -lt 2 || $# -gt 3 ]]; then
      echo "Usage: $0 remove <job_id> <guest_ip> [attacker_ip]" >&2
      exit 1
    fi
    JOB_ID="$1"
    GUEST_IP="$2"
    ATTACKER_IP="${3:-}"
    _assert_job_id "$JOB_ID"
    _assert_ipv4 guest "$GUEST_IP"
    [[ -n "$ATTACKER_IP" ]] && _assert_ipv4 attacker "$ATTACKER_IP"

    COMMENT="astinuslab-isolate-${JOB_ID}"

    _delete_rule_if_present FORWARD -s "$GUEST_IP" -j DROP -m comment --comment "$COMMENT"

    if [[ -n "$ATTACKER_IP" ]]; then
      _delete_rule_if_present FORWARD -s "$ATTACKER_IP" -d "$GUEST_IP" -j ACCEPT -m comment --comment "${COMMENT}-att-rev"
      _delete_rule_if_present FORWARD -s "$GUEST_IP" -d "$ATTACKER_IP" -j ACCEPT -m comment --comment "${COMMENT}-att-fwd"
    fi
    ;;
  *)
    echo "Unsupported subcommand: $SUBCOMMAND" >&2
    exit 1
    ;;
esac
