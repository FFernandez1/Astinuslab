#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <job_id>"
  exit 1
fi

JOB_ID="$1"
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STAGING_ROOT="$BASE_DIR/storage/tmp/$JOB_ID/virtiofs-staging"
XML_PATH="$BASE_DIR/storage/tmp/$JOB_ID/virtiofs-${JOB_ID}.xml"
TAG_NAME="astinuslab_out"

if [[ ! -d "$STAGING_ROOT" ]]; then
  echo "staging_root does not exist. Run prepare_host_dump_share.sh first" >&2
  exit 1
fi

cat > "$XML_PATH" <<EOF
<filesystem type='mount' accessmode='passthrough'>
  <driver type='virtiofs'/>
  <source dir='${STAGING_ROOT}'/>
  <target dir='${TAG_NAME}'/>
</filesystem>
<filesystem type='mount' accessmode='passthrough'>
  <source dir='${STAGING_ROOT}'/>
  <target dir='${TAG_NAME}_9p'/>
</filesystem>
EOF

echo "xml_path=$XML_PATH"
echo "tag_name=$TAG_NAME"
echo "staging_root=$STAGING_ROOT"

