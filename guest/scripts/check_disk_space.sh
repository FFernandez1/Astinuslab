#!/usr/bin/env bash
set -euo pipefail

PATHS=("/" "/home" "/tmp" "/var/tmp")

for P in "${PATHS[@]}"; do
  if [[ -d "$P" ]]; then
    df -h "$P"
    echo
  fi
done

