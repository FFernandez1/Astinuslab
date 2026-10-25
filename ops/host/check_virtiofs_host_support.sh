#!/usr/bin/env bash
set -euo pipefail

FOUND=""

for CANDIDATE in \
  /usr/libexec/virtiofsd \
  /usr/lib/qemu/virtiofsd \
  /usr/lib/virtiofsd \
  "$(command -v virtiofsd 2>/dev/null || true)"; do
  if [[ -n "${CANDIDATE:-}" && -x "$CANDIDATE" ]]; then
    FOUND="$CANDIDATE"
    break
  fi
done

if [[ -z "$FOUND" ]]; then
  echo "virtiofsd_present=false"
  echo "hint=instala_virtiofsd_o_qemu-system-common_si_tu_distro_lo_separa"
  exit 1
fi

echo "virtiofsd_present=true"
echo "virtiofsd_path=$FOUND"

