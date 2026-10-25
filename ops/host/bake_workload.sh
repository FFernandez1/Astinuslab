#!/usr/bin/env bash
# Provision a workload on a temporary Job, then flatten its powered-off disk.
set -euo pipefail

fail() { echo "[bake] $*" >&2; exit 1; }
[[ $# -ge 2 && $# -le 3 ]] || fail "Usage: $0 <target_id> <workload_id> [output_image_name]"
TARGET_ID="$1"
WORKLOAD_ID="$2"
OUTPUT_NAME="${3:-${TARGET_ID}-${WORKLOAD_ID}.qcow2}"
[[ "$TARGET_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || fail "Invalid target id"
[[ "$WORKLOAD_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || fail "Invalid workload id"
[[ "$OUTPUT_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*\.qcow2$ ]] || fail "Output must be a .qcow2 filename without a directory"

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
if [[ -n "${ASTINUSLAB_DATA_DIR:-}" ]]; then
  BAKE_DATA_DIR="$ASTINUSLAB_DATA_DIR"
elif [[ "$(basename "$(dirname "$BASE_DIR")")" == "releases" ]]; then
  BAKE_DATA_DIR="$(cd "$BASE_DIR/../.." && pwd -P)"
else
  BAKE_DATA_DIR="$BASE_DIR"
fi
if [[ -r "$BAKE_DATA_DIR/config/astinuslab.env" ]]; then
  set -a
  # Same trusted operator configuration used by astinuslab.sh.
  # shellcheck source=/dev/null
  source "$BAKE_DATA_DIR/config/astinuslab.env"
  set +a
fi
export ASTINUSLAB_DATA_DIR="$BAKE_DATA_DIR"
PYTHON="${ASTINUSLAB_PYTHON:-$BASE_DIR/.venv/bin/python}"
[[ -x "$PYTHON" ]] || fail "Application Python is missing: $PYTHON"
for tool in curl jq virsh sudo; do command -v "$tool" >/dev/null || fail "Missing dependency: $tool"; done
CONFIG=$(PYTHONPATH="$BASE_DIR/src:$BASE_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON" -c '
import json
from astinuslab.core.config import load_settings
s = load_settings()
print(json.dumps(dict(images=str(s.images_dir), disks=str(s.libvirt_image_base_dir), uri=s.libvirt_uri, port=s.api_port)))
')
IMAGES_DIR=$(jq -er '.images' <<<"$CONFIG")
STORAGE_ROOT=$(jq -er '.disks' <<<"$CONFIG")
LIBVIRT_URI=$(jq -er '.uri' <<<"$CONFIG")
BASE_URL="${BASE_URL:-http://localhost:$(jq -er '.port' <<<"$CONFIG")}"
BASE_URL="${BASE_URL%/}"
API_URL="$BASE_URL/api/v1"
DEST_IMAGE="$IMAGES_DIR/$OUTPUT_NAME"
[[ ! -e "$DEST_IMAGE" && ! -L "$DEST_IMAGE" ]] || fail "Output already exists; choose a new filename"
HELPER="/usr/local/libexec/astinuslab/libvirt_storage_admin.sh"
COOKIE_JAR=$(mktemp "${TMPDIR:-/tmp}/astinuslab-bake-cookies.XXXXXX")
JOB_ID=""
cleanup() {
  local status=$?
  rm -f -- "$COOKIE_JAR"
  if [[ $status -ne 0 && -n "$JOB_ID" ]]; then
    echo "[bake] Interrupted: inspect Job $JOB_ID and any output at $DEST_IMAGE before retrying. No automatic destructive cleanup was attempted." >&2
  fi
}
trap cleanup EXIT
AUTH_ARGS=()
if [[ -n "${ASTINUSLAB_TOKEN:-}" ]]; then
  AUTH_ARGS=(-H "Authorization: Bearer $ASTINUSLAB_TOKEN")
else
  curl --fail --silent --show-error --connect-timeout 10 --max-time 60 -c "$COOKIE_JAR" "$BASE_URL/playground" >/dev/null
  CSRF_TOKEN=$(awk '$6 == "astinuslab_csrf" {value=$7} END {print value}' "$COOKIE_JAR")
  [[ -n "$CSRF_TOKEN" ]] || fail "No CSRF cookie; set ASTINUSLAB_TOKEN for authenticated installs"
  AUTH_ARGS=(-b "$COOKIE_JAR" -H "X-CSRF-Token: $CSRF_TOKEN")
fi
api_curl() { curl --fail-with-body --silent --show-error --connect-timeout 10 --max-time 60 "${AUTH_ARGS[@]}" "$@"; }

PAYLOAD=$(jq -n --arg target "$TARGET_ID" --arg workload "$WORKLOAD_ID" '{target_id:$target, workload_id:$workload, scenario_id:"lab-session", execution_mode:"manual_trigger"}')
JOB_RESP=$(api_curl -X POST "$API_URL/jobs" -H 'Content-Type: application/json' -d "$PAYLOAD")
JOB_ID=$(jq -er '.id | select(type == "string" and length > 0)' <<<"$JOB_RESP")
[[ "$JOB_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || fail "Invalid Job id returned by API"
echo "[bake] Job $JOB_ID created; waiting for provisioning (up to 30 minutes)."
DEADLINE=$((SECONDS + 1800))
while true; do
  STATUS_RESP=$(api_curl "$API_URL/jobs/$JOB_ID")
  STATUS=$(jq -er '.status' <<<"$STATUS_RESP")
  case "$STATUS" in
    waiting_for_capture) break ;;
    failed|cancelled|completed) fail "Job ended in $STATUS; inspect it in Jobs" ;;
  esac
  (( SECONDS < DEADLINE )) || fail "Provisioning timed out; inspect Job $JOB_ID"
  sleep 5
done

VM_NAME="astinuslab-$JOB_ID"
virsh -c "$LIBVIRT_URI" shutdown "$VM_NAME" >/dev/null
DEADLINE=$((SECONDS + 180))
while true; do
  POWER_STATE=$(LC_ALL=C virsh -c "$LIBVIRT_URI" domstate "$VM_NAME")
  [[ "$POWER_STATE" == "shut off" ]] && break
  (( SECONDS < DEADLINE )) || fail "Shutdown timed out; disk was not flattened"
  sleep 2
done
OVERLAY_PATH="$STORAGE_ROOT/$JOB_ID/$TARGET_ID-job.qcow2"
sudo -n "$HELPER" flatten_image "$OVERLAY_PATH" "$DEST_IMAGE"
echo "[bake] Image written: $DEST_IMAGE. Cleaning up temporary Job $JOB_ID."
api_curl -X POST "$API_URL/jobs/$JOB_ID/force-fail" >/dev/null
DEADLINE=$((SECONDS + 180))
while true; do
  if DELETE_RESPONSE=$(api_curl -X DELETE "$API_URL/jobs/$JOB_ID"); then break; fi
  # Teardown is dispatched asynchronously. Retry only that explicit refusal;
  # authentication, custody and transport errors require inspection.
  [[ "$(jq -r '.code // empty' <<<"$DELETE_RESPONSE" 2>/dev/null)" == "cleanup_pending" ]] \
    || fail "Job cleanup was refused or unavailable; inspect Job $JOB_ID"
  (( SECONDS < DEADLINE )) || fail "Job cleanup timed out; inspect Job $JOB_ID"
  sleep 2
done
printf '\n[bake] Baking complete! New image: %s\n' "$DEST_IMAGE"
printf 'Inspect the image in Images before use. A curated target needs image.source_type: qcow2 and image.image_ref: %s\n' "$OUTPUT_NAME"
