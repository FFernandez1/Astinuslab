#!/usr/bin/env bash
# Build a Volatility 3 ISF (Intermediate Symbol Format) JSON for the running
# kernel using dwarf2json.
set -euo pipefail

REMOTE_TMP_DIR="${1:-/tmp/astinuslab-vol3-$$}"
KERNEL_RELEASE="${KERNEL_RELEASE:-$(uname -r)}"
[[ "$KERNEL_RELEASE" =~ ^[a-zA-Z0-9._+-]+$ ]] \
  || { echo "ERROR: invalid KERNEL_RELEASE value: '$KERNEL_RELEASE'" >&2; exit 1; }
DWARF2JSON_BIN="${REMOTE_TMP_DIR}/dwarf2json"
ISF_PATH="${REMOTE_TMP_DIR}/linux-${KERNEL_RELEASE}.json"
VMLINUX_PATH=""

log() { echo "[build_vol3_symbol] $*" >&2; }

_kernel_build_id() {
    if [[ -r /sys/kernel/notes ]]; then
        python3 -c 'import sys, struct
data = sys.stdin.buffer.read()
o = 0
while o + 12 <= len(data):
    ns, ds, t = struct.unpack_from("<III", data, o); o += 12
    name = data[o:o+ns]; o += (ns + 3) & ~3
    desc = data[o:o+ds]; o += (ds + 3) & ~3
    if t == 3 and name.rstrip(b"\x00") == b"GNU":
        sys.stdout.write(desc.hex()); sys.exit(0)
sys.exit(1)' < /sys/kernel/notes && return 0
    fi

    local path
    for path in "/usr/lib/debug/boot/vmlinux-${KERNEL_RELEASE}" "/usr/lib/debug/lib/modules/${KERNEL_RELEASE}/vmlinux"; do
        if [[ -f "$path" ]]; then
            (eu-readelf -n "$path" 2>/dev/null || readelf -n "$path" 2>/dev/null) \
                | awk '/Build ID/{print $NF; exit}' && return 0
        fi
    done

    if command -v extract-vmlinux >/dev/null 2>&1 && [[ -f "/boot/vmlinuz-${KERNEL_RELEASE}" ]]; then
        local tmp
        tmp="$(mktemp)"
        if extract-vmlinux "/boot/vmlinuz-${KERNEL_RELEASE}" > "$tmp" 2>/dev/null; then
            (eu-readelf -n "$tmp" 2>/dev/null || readelf -n "$tmp" 2>/dev/null) \
                | awk '/Build ID/{print $NF; exit}'
            local rc=$?
            rm -f "$tmp"
            return "$rc"
        fi
        rm -f "$tmp"
    fi
    return 1
}

_find_vmlinux() {
    local candidates=(
        "/usr/lib/debug/boot/vmlinux-${KERNEL_RELEASE}"
        "/usr/lib/debug/lib/modules/${KERNEL_RELEASE}/vmlinux"
        "/boot/vmlinux-${KERNEL_RELEASE}"
    )
    local path
    for path in "${candidates[@]}"; do
        if [[ -f "$path" ]]; then
            echo "$path"
            return 0
        fi
    done
    return 1
}

_try_existing_pkg() {
    local found
    if found="$(_find_vmlinux)"; then
        VMLINUX_PATH="$found"
        log "Found vmlinux: ${VMLINUX_PATH}"
        return 0
    fi
    return 1
}

_try_debuginfod() {
    command -v debuginfod-find >/dev/null 2>&1 || return 1
    local build_id
    build_id="$(_kernel_build_id 2>/dev/null || true)"
    [[ -n "$build_id" ]] || return 1
    local found
    found="$(debuginfod-find debuginfo "$build_id" 2>/dev/null || true)"
    if [[ -n "$found" && -f "$found" ]]; then
        VMLINUX_PATH="$found"
        log "Found vmlinux via debuginfod: ${VMLINUX_PATH}"
        return 0
    fi
    return 1
}

_install_debug_pkg_apt() {
    log "Attempting to install debug symbols via apt..."

    # Add dbgsym keyring if not already present (Ubuntu only)
    if grep -qi 'ID=ubuntu' /etc/os-release && ! dpkg -l ubuntu-dbgsym-keyring &>/dev/null; then
        log "Installing ubuntu-dbgsym-keyring..."
        apt-get install -y ubuntu-dbgsym-keyring >/dev/null 2>&1 || true
    fi

    # Add ddebs/debug repository if not already configured
    if ! grep -qr 'ddebs.ubuntu.com\|debug.mirrors' /etc/apt/sources.list /etc/apt/sources.list.d/ 2>/dev/null; then
        local codename
        # The guarded host file is the OS-family data source.
        # shellcheck disable=SC1091
        codename="$(. /etc/os-release && echo "${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}")"
        if [[ -n "$codename" ]]; then
            if grep -qi 'ID=debian' /etc/os-release; then
                log "Adding debian-debug repository for ${codename}..."
                echo "deb http://deb.debian.org/debian-debug/ ${codename}-debug main" \
                    > /etc/apt/sources.list.d/debian-debug.list
            else
                log "Adding ddebs repository for ${codename}..."
                echo "deb http://ddebs.ubuntu.com ${codename} main restricted universe multiverse
deb http://ddebs.ubuntu.com ${codename}-updates main restricted universe multiverse" \
                    > /etc/apt/sources.list.d/ddebs.list
            fi
            apt-get update -qq >/dev/null 2>&1 || true
        fi
    fi

    local unsigned_pkg="linux-image-unsigned-${KERNEL_RELEASE}-dbgsym"
    local signed_pkg="linux-image-${KERNEL_RELEASE}-dbgsym"
    local debian_dbg_pkg="linux-image-${KERNEL_RELEASE}-dbg"

    log "Trying package: ${unsigned_pkg}"
    if apt-get install -y "$unsigned_pkg" >/dev/null 2>&1; then
        log "Installed ${unsigned_pkg}"
        return 0
    fi

    log "Trying package: ${signed_pkg}"
    if apt-get install -y "$signed_pkg" >/dev/null 2>&1; then
        log "Installed ${signed_pkg}"
        return 0
    fi

    log "Trying package: ${debian_dbg_pkg}"
    if apt-get install -y "$debian_dbg_pkg" >/dev/null 2>&1; then
        log "Installed ${debian_dbg_pkg}"
        return 0
    fi

    log "apt debug package install failed — trying direct ddeb download"
    if _direct_ddeb_download_apt; then
        return 0
    fi
    log "Direct ddeb download also failed (non-fatal, continuing)"
    return 1
}

_direct_ddeb_download_apt() {
    local listing_url="http://ddebs.ubuntu.com/pool/main/l/linux/"
    local tmp_extract
    tmp_extract="$(mktemp -d)"
    local listing
    listing="$(curl -sf --max-time 30 "$listing_url" 2>/dev/null || true)"
    [[ -n "$listing" ]] || { rm -rf "$tmp_extract"; return 1; }
    local kr_escaped="${KERNEL_RELEASE//./\.}"
    local all_matches pkg_file dest
    all_matches="$(printf '%s' "$listing" \
        | grep -oE "linux-image[^\"<> ]*${kr_escaped}[^\"<> ]*-dbgsym[^\"<> ]*amd64\.ddeb" \
        | sort -uV)"
    pkg_file="$(printf '%s' "$all_matches" | grep 'unsigned' | tail -n 1)"
    [[ -n "$pkg_file" ]] || pkg_file="$(printf '%s' "$all_matches" | tail -n 1)"
    [[ -n "$pkg_file" ]] || { rm -rf "$tmp_extract"; return 1; }
    dest="${tmp_extract}/${pkg_file}"
    wget -q --tries=3 --timeout=60 -O "$dest" "${listing_url}${pkg_file}" 2>/dev/null || {
        rm -rf "$tmp_extract"; return 1
    }
    dpkg-deb -x "$dest" / 2>/dev/null
    local rc=$?
    rm -rf "$tmp_extract"
    return "$rc"
}

_install_debug_pkg_dnf() {
    local ver_rel="${KERNEL_RELEASE%.*}"
    dnf install -y --enablerepo='*debug*' "kernel-debuginfo-${ver_rel}" >/dev/null 2>&1
}

_install_debug_pkg_zypper() {
    log "Attempting to install debug symbols via zypper..."

    # openSUSE/SLES kernel debuginfo packages by canonical name.
    # The version-pinned variant is included for cases where the metapackage
    # is unavailable but a specific build is.  KERNEL_RELEASE example:
    # 6.4.0-150500.34-default → trims trailing -default for the pinned form.
    local candidates=(
        "kernel-default-debuginfo"
        "kernel-default-debuginfo-${KERNEL_RELEASE%-default}"
        "kernel-debuginfo"
    )

    local pkg
    for pkg in "${candidates[@]}"; do
        log "Trying package: ${pkg}"
        if zypper --non-interactive install -y "$pkg" >/dev/null 2>&1; then
            log "Installed ${pkg}"
            return 0
        fi
    done

    log "zypper debug package install failed (non-fatal, continuing)"
    return 1
}

_install_debug_pkg_pacman() {
    pacman -S --noconfirm linux-debug >/dev/null 2>&1
}

_install_debug_pkg_apk() {
    log "Attempting to install debug symbols via apk..."
    local flavor
    # Extract flavor from uname -r (e.g. 6.6.32-0-virt -> virt)
    flavor="$(echo "${KERNEL_RELEASE}" | grep -oE '(virt|lts|edge)$' || true)"
    [[ -n "$flavor" ]] || flavor="virt"

    local pkg="linux-${flavor}-dbg"
    log "Trying package: ${pkg}"
    if apk add --no-cache "$pkg" >/dev/null 2>&1; then
        log "Installed ${pkg}"
        return 0
    fi

    log "apk debug package install failed (non-fatal, continuing)"
    return 1
}

main() {
    mkdir -p "$REMOTE_TMP_DIR"

    local arch
    arch="$(uname -m)"
    if [[ "$arch" != "x86_64" ]]; then
        echo "ERROR: live Vol3 symbol build only supports x86_64 guests; detected: ${arch}" >&2
        exit 1
    fi

    if [[ ! -x "$DWARF2JSON_BIN" ]]; then
        echo "ERROR: dwarf2json not found or not executable: ${DWARF2JSON_BIN}" >&2
        exit 1
    fi

    log "Searching for kernel symbols for ${KERNEL_RELEASE}..."
    if [[ -n "${VMLINUX_OVERRIDE:-}" && -f "${VMLINUX_OVERRIDE}" ]]; then
        VMLINUX_PATH="${VMLINUX_OVERRIDE}"
        log "Using vmlinux override: ${VMLINUX_PATH}"
    elif _try_existing_pkg; then
        :
    elif _try_debuginfod; then
        :
    else
        if [[ "${VOL3_ALLOW_GUEST_PACKAGE_INSTALL:-0}" != "1" ]]; then
            echo "ERROR: vmlinux not found and invasive guest debug package install is disabled" >&2
            exit 2
        fi
        if command -v apt-get >/dev/null 2>&1; then
            _install_debug_pkg_apt || true
        elif command -v dnf >/dev/null 2>&1; then
            _install_debug_pkg_dnf || true
        elif command -v zypper >/dev/null 2>&1; then
            _install_debug_pkg_zypper || true
        elif command -v pacman >/dev/null 2>&1; then
            _install_debug_pkg_pacman || true
        elif command -v apk >/dev/null 2>&1; then
            _install_debug_pkg_apk || true
        fi
        _try_existing_pkg || { echo "ERROR: vmlinux not found for kernel ${KERNEL_RELEASE}" >&2; exit 1; }
    fi

    local vmlinux_magic=""
    if ! vmlinux_magic="$(od -An -N4 -tx1 -- "$VMLINUX_PATH" 2>/dev/null | tr -d '[:space:]')" \
        || [[ "$vmlinux_magic" != "7f454c46" ]]; then
        echo "ERROR: vmlinux input is not an ELF file: ${VMLINUX_PATH}" >&2
        exit 1
    fi

    local dwarf2json_args=(linux --elf "$VMLINUX_PATH")
    local system_map="/boot/System.map-${KERNEL_RELEASE}"
    if [[ -f "$system_map" ]]; then
        dwarf2json_args+=(--system-map "$system_map")
    fi

    log "Running dwarf2json: ${DWARF2JSON_BIN} ${dwarf2json_args[*]} > ${ISF_PATH}"
    "$DWARF2JSON_BIN" "${dwarf2json_args[@]}" > "$ISF_PATH"
    log "ISF written to: ${ISF_PATH}"

    echo "vol3_symbol_remote_path=${ISF_PATH}"
    echo "kernel_release=${KERNEL_RELEASE}"
    local build_id
    build_id="$(_kernel_build_id 2>/dev/null || true)"
    [[ -n "$build_id" ]] && echo "build_id=${build_id}"
}

main "$@"
