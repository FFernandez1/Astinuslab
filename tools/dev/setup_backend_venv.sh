#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV_DIR="$BASE_DIR/.venv"

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r "$BASE_DIR/requirements.lock"
"$VENV_DIR/bin/python" -m pip install -e "$BASE_DIR" --no-deps

echo "venv_ready=$VENV_DIR"
echo "activate_cmd=source $VENV_DIR/bin/activate"

