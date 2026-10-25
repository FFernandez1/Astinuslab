#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 || ( "$1" != "--probe" && "$1" != /* ) ]]; then
  echo "Usage: $0 {--probe|<absolute-target-directory>}" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# -B: never write bytecode into the privileged helper directory. A root-run
# import leaves a root-owned __pycache__ there, which is needless mutable
# state inside the trust boundary and trips the installer's orphan removal.
exec python3 -B "$SCRIPT_DIR/fix_libvirt_path_acl.py" "$1"
