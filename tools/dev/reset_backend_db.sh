#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DB_PATH="$BASE_DIR/storage/astinuslab.db"

rm -f "$DB_PATH"
echo "db_reset=$DB_PATH"

