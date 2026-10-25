#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <job_id>"
  exit 1
fi

JOB_ID="$1"
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STAGING_ROOT="$BASE_DIR/storage/tmp/$JOB_ID/virtiofs-staging"

mkdir -p "$STAGING_ROOT"
chmod 0755 "$BASE_DIR/storage/tmp/$JOB_ID" "$STAGING_ROOT"

echo "staging_root=$STAGING_ROOT"

