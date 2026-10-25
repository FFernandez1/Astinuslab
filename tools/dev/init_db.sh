#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV_DIR="$BASE_DIR/.venv"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "No existe venv. Ejecuta primero: bash tools/dev/setup_backend_venv.sh" >&2
  exit 1
fi

exec "$VENV_DIR/bin/python" -m astinuslab.dbcli init
