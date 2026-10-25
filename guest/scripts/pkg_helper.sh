#!/usr/bin/env bash
set -euo pipefail

# Find the directory of this script to locate detect_environment.sh
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source detect_environment.sh in env mode
eval "$("${SCRIPT_DIR}/detect_environment.sh" env)"

# Translate basic package names if needed
_translate_pkg() {
  local pkg="$1"
  case "$PACKAGE_MANAGER" in
    dnf|yum)
      case "$pkg" in
        apache2) echo "httpd" ;;
        libmariadbclient-dev|libmariadb-dev) echo "mariadb-devel" ;;
        php-mysql) echo "php-mysqlnd" ;;
        libcurl4-openssl-dev) echo "libcurl-devel" ;;
        zlib1g-dev) echo "zlib-devel" ;;
        libxml2-dev) echo "libxml2-devel" ;;
        libffi-dev) echo "libffi-devel" ;;
        libssl-dev) echo "openssl-devel" ;;
        build-essential) echo "gcc-c++ make" ;;
        perl) echo "perl" ;;
        *) echo "$pkg" ;;
      esac
      ;;
    *)
      echo "$pkg" ;;
  esac
}

# Wait for cloud-init / unattended-upgrades to release the apt+dpkg locks.
# On a freshly booted cloud image, first-boot apt runs in the background while
# sshd is already up, so an install started immediately dies with
# "Could not get lock ... held by process N (apt-get)". Bounded to ~5 min.
_apt_wait_lock() {
  local deadline=$(( $(date +%s) + 300 ))
  while sudo fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 \
    || sudo fuser /var/lib/apt/lists/lock >/dev/null 2>&1; do
    if (( $(date +%s) >= deadline )); then
      echo "[pkg_helper] timed out waiting for apt/dpkg lock" >&2
      return 1
    fi
    echo "[pkg_helper] waiting for apt/dpkg lock to be released..."
    sleep 3
  done
}

pkg_update() {
  case "$PACKAGE_MANAGER" in
    apt)
      _apt_wait_lock
      if [[ -z "$(find /var/lib/apt/lists -maxdepth 1 -name '*.lz4' -o -name '*_Packages' -newer /proc/1 -mmin -10 2>/dev/null | head -1)" ]]; then
        sudo apt-get update -qq
      else
        echo "[pkg_helper] apt package lists are fresh, skipping update."
      fi
      ;;
    dnf) sudo dnf makecache -q ;;
    yum) sudo yum makecache -q ;;
    zypper) sudo zypper --non-interactive refresh ;;
    pacman) sudo pacman -Sy --noconfirm ;;
    apk) sudo apk update ;;
    *) echo "[pkg_helper] Unsupported package manager: $PACKAGE_MANAGER" >&2; return 1 ;;
  esac
}

pkg_install() {
  local translated_pkgs=()
  for pkg in "$@"; do
    # Simple split if _translate_pkg returns multiple space-separated packages
    for p in $(_translate_pkg "$pkg"); do
      translated_pkgs+=("$p")
    done
  done

  case "$PACKAGE_MANAGER" in
    apt) _apt_wait_lock; sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${translated_pkgs[@]}" ;;
    dnf) sudo dnf install -y -q "${translated_pkgs[@]}" ;;
    yum) sudo yum install -y -q "${translated_pkgs[@]}" ;;
    zypper) sudo zypper --non-interactive install -y "${translated_pkgs[@]}" ;;
    pacman) sudo pacman -S --noconfirm "${translated_pkgs[@]}" ;;
    apk) sudo apk add --no-cache "${translated_pkgs[@]}" ;;
    *) echo "[pkg_helper] Unsupported package manager: $PACKAGE_MANAGER" >&2; return 1 ;;
  esac
}
