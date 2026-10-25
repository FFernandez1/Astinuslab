#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "${ASTINUSLAB_PYTHON:-python3}" "$SCRIPT_DIR/collect_lab_package_host_evidence.py" "$@"
