#!/usr/bin/env bash
set -euo pipefail

OUTPUT_FORMAT="${1:-json}"

if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
else
  ID="unknown"
  VERSION_ID="unknown"
  PRETTY_NAME="unknown"
fi

ARCH="$(uname -m)"
KERNEL_RELEASE="$(uname -r)"
KERNEL_VERSION="$(uname -v)"
HOSTNAME_FQDN="$(hostname -f 2>/dev/null || hostname)"
PKG_MANAGER="unknown"

if command -v apt-get >/dev/null 2>&1; then
  PKG_MANAGER="apt"
elif command -v dnf >/dev/null 2>&1; then
  PKG_MANAGER="dnf"
elif command -v yum >/dev/null 2>&1; then
  PKG_MANAGER="yum"
elif command -v zypper >/dev/null 2>&1; then
  PKG_MANAGER="zypper"
elif command -v pacman >/dev/null 2>&1; then
  PKG_MANAGER="pacman"
elif command -v apk >/dev/null 2>&1; then
  PKG_MANAGER="apk"
fi

HEADERS_PATH=""
for CANDIDATE in \
  "/lib/modules/$KERNEL_RELEASE/build" \
  "/usr/src/linux-headers-$KERNEL_RELEASE" \
  "/usr/src/kernels/$KERNEL_RELEASE"; do
  if [[ -e "$CANDIDATE" ]]; then
    HEADERS_PATH="$CANDIDATE"
    break
  fi
done

HEADERS_PRESENT=false
if [[ -n "$HEADERS_PATH" ]]; then
  HEADERS_PRESENT=true
fi

case "$OUTPUT_FORMAT" in
  json)
    cat <<EOF
{
  "distro_id": "${ID}",
  "distro_version": "${VERSION_ID}",
  "pretty_name": "${PRETTY_NAME}",
  "hostname": "${HOSTNAME_FQDN}",
  "arch": "${ARCH}",
  "kernel_release": "${KERNEL_RELEASE}",
  "kernel_version": "${KERNEL_VERSION}",
  "package_manager": "${PKG_MANAGER}",
  "headers_present": ${HEADERS_PRESENT},
  "headers_path": "${HEADERS_PATH}"
}
EOF
    ;;
  env)
    # Single-quote every value: fields like PRETTY_NAME ("Ubuntu 24.04.4 LTS")
    # and KERNEL_VERSION contain spaces, and this block is consumed via
    # `eval "$(detect_environment.sh env)"`. Unquoted, eval would run the
    # second word as a command ("24.04.4: command not found").
    cat <<EOF
DISTRO_ID='${ID}'
DISTRO_VERSION='${VERSION_ID}'
PRETTY_NAME='${PRETTY_NAME}'
HOSTNAME_FQDN='${HOSTNAME_FQDN}'
ARCH='${ARCH}'
KERNEL_RELEASE='${KERNEL_RELEASE}'
KERNEL_VERSION='${KERNEL_VERSION}'
PACKAGE_MANAGER='${PKG_MANAGER}'
HEADERS_PRESENT='${HEADERS_PRESENT}'
HEADERS_PATH='${HEADERS_PATH}'
EOF
    ;;
  *)
    echo "Unsupported format: $OUTPUT_FORMAT" >&2
    exit 1
    ;;
esac

