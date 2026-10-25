#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 5 ]]; then
  echo "Usage: $0 <job_id> <source_path> <distro> <version> <kernel_release>" >&2
  exit 1
fi

JOB_ID="$1"
SOURCE_PATH="$2"
DISTRO="$3"
VERSION="$4"
KERNEL_RELEASE="$5"
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STORAGE_ROOT="${ASTINUSLAB_STORAGE_DIR:-$BASE_DIR/storage}"
PUBLISH_DIR="$STORAGE_ROOT/dumps/$JOB_ID"
LOG_DIR="$STORAGE_ROOT/logs"
STAGING_DIR="$STORAGE_ROOT/tmp/$JOB_ID/published"

mkdir -p "$PUBLISH_DIR" "$LOG_DIR" "$STAGING_DIR"

SAFE_DISTRO="$(echo "$DISTRO" | tr -cd 'a-zA-Z0-9.-')"
SAFE_VERSION="$(echo "$VERSION" | tr -cd 'a-zA-Z0-9.-')"
SAFE_KERNEL="${KERNEL_RELEASE//\//-}"
FINAL_NAME="${SAFE_DISTRO}-${SAFE_VERSION}-kernel-${SAFE_KERNEL}-disk.img.gz"
FINAL_PATH="$PUBLISH_DIR/$FINAL_NAME"
TMP_PATH="$STAGING_DIR/$FINAL_NAME.partial"
VIRTIOFS_STAGING_DIR="$STORAGE_ROOT/tmp/$JOB_ID/virtiofs-staging"
CANONICAL_PUBLISH_DIR="$(realpath -m "$PUBLISH_DIR")"
CANONICAL_FINAL_PATH="$(realpath -m "$FINAL_PATH")"

if [[ "$CANONICAL_FINAL_PATH" != "$CANONICAL_PUBLISH_DIR"/* && "$CANONICAL_FINAL_PATH" != "$CANONICAL_PUBLISH_DIR" ]]; then
  echo "Refusing to publish outside job dump directory: $FINAL_PATH" >&2
  exit 1
fi

# Only remove the partial file this invocation owns, never the source or final.
CLEANUP_PARTIAL=""
cleanup_partial() {
  local status=$?
  if [[ -n "$CLEANUP_PARTIAL" ]]; then
    rm -f -- "$CLEANUP_PARTIAL" || {
      echo "Could not remove disk publication scratch: $CLEANUP_PARTIAL" >&2
      status=1
    }
  fi
  exit "$status"
}
trap cleanup_partial EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "disk_publish_stage=starting"
echo "disk_publish_source=$SOURCE_PATH"

if [[ "$SOURCE_PATH" == "$VIRTIOFS_STAGING_DIR/"* ]]; then
  TMP_PATH="$SOURCE_PATH"
else
  CLEANUP_PARTIAL="$TMP_PATH"
  cp "$SOURCE_PATH" "$TMP_PATH"
fi

if [[ ! -s "$TMP_PATH" ]]; then
  echo "El disk dump copiado es vacio o inexistente" >&2
  exit 1
fi

echo "disk_publish_stage=checksumming"
sha256sum "$TMP_PATH" | tee "$LOG_DIR/${JOB_ID}-disk-dump.sha256"
# Raw (pre-compression) digest: gzip bytes are not canonical across
# compressors/levels, so the raw content hash is the comparable one.
# A .img.gz that cannot be decompressed is unreadable evidence — under
# `set -euo pipefail` a gzip failure aborts publication (fail closed).
RAW_SHA256="$(gzip -dc "$TMP_PATH" | sha256sum | cut -d' ' -f1)"
printf '%s  %s (decompressed)\n' "$RAW_SHA256" "$FINAL_NAME" > "$LOG_DIR/${JOB_ID}-disk-dump.raw.sha256"
echo "disk_raw_sha256=$RAW_SHA256"
echo "disk_publish_stage=publishing"
mv "$TMP_PATH" "$FINAL_PATH"

echo "disk_publish_stage=completed"
echo "disk_final_path=$FINAL_PATH"
echo "disk_filename=$FINAL_NAME"
