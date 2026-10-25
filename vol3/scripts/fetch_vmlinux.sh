#!/usr/bin/env bash
# fetch_vmlinux.sh — Download a DWARF-capable vmlinux for the target kernel.
#
# Runs on the HOST. Downloads debug packages from distro-specific public
# repositories and extracts the vmlinux ELF into the symbols directory.
# No guest SSH access required.
#
# Usage:
#   fetch_vmlinux.sh --case-dir <path> --distro <id> --version <ver> \
#                    --kernel-release <uname-r> [--arch x86_64]
#
# Outputs (on stdout, one per line, key=value):
#   vmlinux_path=<absolute path>
#   system_map_path=<absolute path>   (only if found)
#
# Exit codes:
#   0  success
#   1  argument / config error
#   2  distro not supported
#   3  download failed (package not found upstream)
#   4  extraction failed (vmlinux not inside the package)

set -euo pipefail

analysis_log() {
  echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] $*" >&2
}
analysis_die() {
  analysis_log "ERROR: $*"
  exit 1
}

# ── args ──────────────────────────────────────────────────────────────────────

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
WORK_DIR=""
SYMBOLS_DIR_ARG=""
DISTRO=""
VERSION=""
KERNEL_RELEASE=""
ARCH="${ARCH:-x86_64}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --work-dir)       WORK_DIR="${2:-}";       shift 2 ;;
    --symbols-dir)    SYMBOLS_DIR_ARG="${2:-}"; shift 2 ;;
    --distro)         DISTRO="${2:-}";         shift 2 ;;
    --version)        VERSION="${2:-}";        shift 2 ;;
    --kernel-release) KERNEL_RELEASE="${2:-}"; shift 2 ;;
    --arch)           ARCH="${2:-}";           shift 2 ;;
    *) analysis_die "Unknown argument: $1" ;;
  esac
done

[[ -n "$WORK_DIR" && -n "$DISTRO" && -n "$VERSION" && -n "$KERNEL_RELEASE" ]] \
  || analysis_die "Usage: $0 --work-dir <path> --distro <id> --version <ver> --kernel-release <uname-r> [--arch <arch>] [--symbols-dir <path>]"
[[ "$ARCH" == "x86_64" ]] || analysis_die "Only x86_64 kernels are supported (got: ${ARCH})"

SYMBOLS_DIR="${SYMBOLS_DIR_ARG:-$WORK_DIR/symbols}"
FETCH_WORK_DIR="$WORK_DIR/symbols/_fetch_work"
mkdir -p "$SYMBOLS_DIR" "$FETCH_WORK_DIR"
SYMBOL_PACKAGE_NAME="${SYMBOL_PACKAGE_NAME:-}"
DOWNLOAD_ATTEMPTS="${DOWNLOAD_ATTEMPTS:-3}"
fi

# DEB arch naming for the supported kernel architecture.
_deb_arch() {
  case "$ARCH" in
    x86_64)  echo "amd64"  ;;
    *)       analysis_die "Only x86_64 kernels are supported (got: $ARCH)" ;;
  esac
}

# ── fast path: vmlinux already present ───────────────────────────────────────

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
DEST_ELF="$SYMBOLS_DIR/vmlinux-${KERNEL_RELEASE}"
DEST_MAP="$SYMBOLS_DIR/System.map-${KERNEL_RELEASE}"

if [[ -f "$DEST_ELF" ]]; then
  analysis_log "vmlinux already present: $DEST_ELF"
  echo "vmlinux_path=$DEST_ELF"
  [[ -f "$DEST_MAP" ]] && echo "system_map_path=$DEST_MAP"
  exit 0
fi
fi

# ── helpers ───────────────────────────────────────────────────────────────────

_download() {
  local url="$1" dest="$2"
  analysis_log "Downloading: $url"
  local attempt
  for attempt in $(seq 1 "$DOWNLOAD_ATTEMPTS"); do
    analysis_log "download attempt ${attempt}/${DOWNLOAD_ATTEMPTS} for $url"
    if command -v wget >/dev/null 2>&1; then
      wget -q --show-progress -c -O "$dest" "$url" 2>&1 && return 0
    elif command -v curl >/dev/null 2>&1; then
      curl -fL -C - --progress-bar -o "$dest" "$url" 2>&1 && return 0
    else
      analysis_die "Neither wget nor curl found on host"
    fi
    sleep "$attempt"
  done
  return 1
}

# Find a file matching a glob pattern under a directory; print the first match.
_find_first() {
  local dir="$1" pattern="$2"
  find "$dir" -name "$pattern" -type f | sort | head -n 1
}

_extract_deb() {
  local pkg="$1" extract_dir="$2"
  mkdir -p "$extract_dir"
  dpkg-deb -x "$pkg" "$extract_dir" 2>&1
}

_extract_rpm() {
  local pkg="$1" extract_dir="$2"
  mkdir -p "$extract_dir"
  (cd "$extract_dir" && rpm2cpio "$pkg" | cpio -idm --quiet)
}

_finalise() {
  local elf="$1"
  cp "$elf" "$DEST_ELF"
  analysis_log "vmlinux copied to: $DEST_ELF"
  echo "vmlinux_path=$DEST_ELF"

  # System.map — check standard locations in the extracted tree
  local map
  map="$(_find_first "$FETCH_WORK_DIR" "System.map-${KERNEL_RELEASE}")"
  if [[ -n "$map" ]]; then
    cp "$map" "$DEST_MAP"
    analysis_log "System.map copied to: $DEST_MAP"
    echo "system_map_path=$DEST_MAP"
  fi
}

_cleanup() { rm -rf "${FETCH_WORK_DIR:-}"; }
[[ "${BASH_SOURCE[0]}" == "$0" ]] && trap _cleanup EXIT

# ── ubuntu ────────────────────────────────────────────────────────────────────
# Packages on ddebs.ubuntu.com.
#
# Strategy:
#   Query the Launchpad binary package API to get the exact version and the
#   source package name (which determines the pool path on ddebs.ubuntu.com).
#   Tries both linux-image-unsigned-<kernel>-dbgsym and linux-image-<kernel>-dbgsym
#   so it works for generic, lowlatency (unsigned) AND kvm, gcp, aws etc.
#   Then fetches the ddeb directly from the ddebs.ubuntu.com pool.
#
# The Launchpad binary API is used (not source) because:
#   - It directly returns binary_package_version and source_package_name in one call.
#   - source_package_name gives the exact pool subdirectory (l/<src>/<pkg>.ddeb).
#   - It handles all flavors without a hardcoded flavor→source mapping.
#
# Note: the host-side apt path (apt-get install/update/download) has been
# intentionally removed. It required root to write to /etc/apt and assumed the
# host was running the same Ubuntu codename as the target image — neither is
# reliable in a containerised analysis environment.

# _ubuntu_lookup: outputs "codename|version|source_pkg|pkg_name" or returns 1.
# Tries unsigned variant first, then non-unsigned, filtered by codename + arch.
_ubuntu_lookup() {
  local codename="$1" deb_arch="$2" package_name="$3"
  local lp_attempts="${LP_QUERY_ATTEMPTS:-3}"
  local attempt result
  for attempt in $(seq 1 "$lp_attempts"); do
    result="$(python3 - "${KERNEL_RELEASE:-}" "$codename" "$deb_arch" "$package_name" <<'PY'
import sys, urllib.request, urllib.error, json

kernel, codename, arch, package_name = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
candidates = [package_name] if package_name else [
    f"linux-image-unsigned-{kernel}-dbgsym",
    f"linux-image-{kernel}-dbgsym",
]

for pkg in candidates:
    url = (
        "https://api.launchpad.net/1.0/ubuntu/+archive/primary"
        f"?ws.op=getPublishedBinaries&binary_name={pkg}"
        f"&exact_match=true&order_by_date=true&ws.size=30"
    )
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        for e in data.get("entries", []):
            arch_link = e.get("distro_arch_series_link", "")
            if arch in arch_link:
                link_codename = arch_link.rstrip("/").split("/")[-2]
                if codename and codename != link_codename:
                    continue
                ver = e.get("binary_package_version", "")
                src = e.get("source_package_name", "")
                if ver and src:
                    print(f"{link_codename}|{ver}|{src}|{pkg}")
                    sys.exit(0)
        # Successful HTTP, no matching entry — try next pkg
        break
    except Exception as exc:
        print(f"Launchpad query failed for {pkg}: {exc}", file=sys.stderr)
        sys.exit(1)
sys.exit(1)
PY
    )" && { echo "$result"; return 0; }
    if [[ "$attempt" -lt "$lp_attempts" ]]; then
      sleep $(( 2 ** (attempt - 1) ))
    fi
  done
  return 1
}

_fetch_ubuntu() {
  local deb_arch; deb_arch="$(_deb_arch)"

  # Map guest VERSION → Ubuntu codename
  local codename
  codename="$(python3 - "$VERSION" <<'PY' || true
import sys
mapping = {
    # Unmapped releases (e.g. 26.04+) fall through to "" → Launchpad
    # codename auto-discovery below. Never map a release to a non-empty
    # placeholder: _ubuntu_lookup filters by codename and would never match.
    "25.10": "questing", "25.04": "plucky",
    "24.10": "oracular", "24.04": "noble", "23.10": "mantic", "23.04": "lunar",
    "22.04": "jammy", "21.10": "impish", "21.04": "hirsute",
    "20.04": "focal", "18.04": "bionic", "16.04": "xenial",
}
print(mapping.get(sys.argv[1], ""))
PY
)"

  # If codename is unknown (e.g. auto-discovered image), query Launchpad to discover it
  local lookup_result="" ver="" src_pkg="" pkg_name=""
  if [[ -z "$codename" ]]; then
    analysis_log "Ubuntu version '${VERSION}' codename unknown. Querying Launchpad to discover it..."
    lookup_result="$(_ubuntu_lookup "" "$deb_arch" "$SYMBOL_PACKAGE_NAME")" || true
    if [[ -n "$lookup_result" ]]; then
      IFS='|' read -r codename ver src_pkg pkg_name <<< "$lookup_result"
      analysis_log "Discovered Ubuntu codename via Launchpad: ${codename}"
    else
      analysis_log "WARNING: Launchpad codename discovery failed; proceeding without codename"
    fi
  fi

  # Launchpad binary API lookup — returns "codename|version|source_pkg|pkg_name"
  if [[ -z "$lookup_result" ]]; then
    lookup_result="$(_ubuntu_lookup "$codename" "$deb_arch" "$SYMBOL_PACKAGE_NAME")" || true
    if [[ -n "$lookup_result" ]]; then
      IFS='|' read -r codename ver src_pkg pkg_name <<< "$lookup_result"
    fi
  fi
  if [[ -n "$lookup_result" ]]; then
    analysis_log "Ubuntu dbgsym (Launchpad): pkg=${pkg_name} ver=${ver} source=${src_pkg}"
    local pkg_file="${pkg_name}_${ver}_${deb_arch}.ddeb"
    local url="https://ddebs.ubuntu.com/pool/main/l/${src_pkg}/${pkg_file}"
    local dest="$WORK_DIR/${pkg_file}"
    _download "$url" "$dest" || exit 3
  else
    analysis_log "Launchpad unreachable; scraping ddebs.ubuntu.com directory listing"
    # Assumes source package is 'linux'; HWE/backport kernels may use linux-hwe-X.Y
    # and would live under a different pool subdir — Launchpad discovery is needed for those.
    local listing_url="https://ddebs.ubuntu.com/pool/main/l/linux/"
    local listing
    listing="$(curl -sf --max-time 30 "$listing_url" || true)"
    [[ -n "$listing" ]] || analysis_die "Cannot fetch ddebs index for ${KERNEL_RELEASE}"
    local pkg_file
    local kr_escaped="${KERNEL_RELEASE//./\\.}"
    local all_matches
    all_matches="$(printf '%s' "$listing" \
      | grep -oE "linux-image[^\"<> ]*${kr_escaped}[^\"<> ]*-dbgsym[^\"<> ]*${deb_arch}\.ddeb" \
      | sort -uV)"
    # Prefer unsigned variant; fall back to any match
    pkg_file="$(printf '%s' "$all_matches" | grep 'unsigned' | tail -n 1)"
    [[ -n "$pkg_file" ]] || pkg_file="$(printf '%s' "$all_matches" | tail -n 1)"
    [[ -n "$pkg_file" ]] || analysis_die "No ddeb matching ${KERNEL_RELEASE} in ddebs index"
    local url="${listing_url}${pkg_file}"
    local dest="$WORK_DIR/${pkg_file}"
    _download "$url" "$dest" || exit 3
  fi

  _extract_deb "$dest" "$WORK_DIR/extract"
  local elf; elf="$(_find_first "$WORK_DIR/extract" "vmlinux-${KERNEL_RELEASE}")"
  [[ -n "$elf" ]] || exit 4
  _finalise "$elf"
}

# ── debian ────────────────────────────────────────────────────────────────────

_fetch_debian() {
  local deb_arch; deb_arch="$(_deb_arch)"

  # Determine codename from version
  local codename
  codename="$(python3 - "$VERSION" <<'PY'
import sys
ver = sys.argv[1]
mapping = {
    "12": "bookworm", "11": "bullseye", "10": "buster",
    "9":  "stretch",  "8":  "jessie",
}
# Accept "12" or "12.x"
major = ver.split(".")[0]
print(mapping.get(major, ""))
PY
)"
  [[ -n "$codename" ]] || analysis_die "Unknown Debian version: ${VERSION}"

  # Host-side apt mutation is intentionally disabled; use snapshot.debian.org only.

  # Use the snapshot.debian.org binfiles API to download by file hash (ISF-04).
  # This avoids hardcoded archive timestamps that become stale as new kernels
  # are released and old snapshot URLs return 404.
  #
  # Strategy:
  #   1. GET /mr/binary/<pkg>/ to find the binary_version for this arch.
  #   2. GET /mr/binary/<pkg>/<version>/binfiles to retrieve the file hash.
  #   3. Download via https://snapshot.debian.org/file/<hash> (content-addressed,
  #      always resolvable regardless of archive timestamp).
  local pkg_name="${SYMBOL_PACKAGE_NAME:-linux-image-${KERNEL_RELEASE}-dbgsym}"
  local snap_result
  snap_result="$(python3 - "$pkg_name" "$deb_arch" <<'PY'
import sys, urllib.request, json
pkg_name, arch = sys.argv[1], sys.argv[2]
try:
    req = urllib.request.Request(
        f"https://snapshot.debian.org/mr/binary/{pkg_name}/",
        headers={"User-Agent": "astinuslab/1.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
    entry = next(
        (e for e in d.get("result", []) if e.get("architecture") == arch),
        None,
    )
    if not entry:
        print(f"no result for arch={arch}", file=sys.stderr)
        sys.exit(1)
    binary_version = entry["binary_version"]
    pkg_file = entry["name"]
    # Step 2: fetch the file hash from the binfiles endpoint
    req2 = urllib.request.Request(
        f"https://snapshot.debian.org/mr/binary/{pkg_name}/{binary_version}/binfiles",
        headers={"User-Agent": "astinuslab/1.0"},
    )
    with urllib.request.urlopen(req2, timeout=30) as r2:
        d2 = json.load(r2)
    file_entry = next(
        (f for f in d2.get("result", []) if f.get("architecture") == arch),
        None,
    )
    if not file_entry:
        print(f"no binfiles entry for arch={arch}", file=sys.stderr)
        sys.exit(1)
    print(pkg_file)
    print(file_entry["hash"])
except Exception as e:
    print(str(e), file=sys.stderr)
    sys.exit(1)
PY
  )" || analysis_die "Cannot locate ddeb in Debian snapshot for ${pkg_name}"

  local pkg_file snap_hash
  pkg_file="$(echo "$snap_result" | head -1)"
  snap_hash="$(echo "$snap_result" | tail -1)"

  local dest="$WORK_DIR/${pkg_file}"
  analysis_log "Debian snapshot: downloading by hash ${snap_hash}"
  _download "https://snapshot.debian.org/file/${snap_hash}" "$dest" || exit 3
  _extract_deb "$dest" "$WORK_DIR/extract"
  local elf; elf="$(_find_first "$WORK_DIR/extract" "vmlinux-${KERNEL_RELEASE}")"
  [[ -n "$elf" ]] || exit 4
  _finalise "$elf"
}

# ── kali / parrot ─────────────────────────────────────────────────────────────
# Kali and Parrot ship custom kernels that do not exist in snapshot.debian.org.
# This function scrapes the distro's own pool directory for the dbgsym package.

_fetch_kali() {
  local deb_arch; deb_arch="$(_deb_arch)"
  local pkg_name="${SYMBOL_PACKAGE_NAME:-linux-image-${KERNEL_RELEASE}-dbgsym}"

  local base_url
  case "$DISTRO" in
    kali)   base_url="https://http.kali.org/kali/pool/main/l/linux-kali/" ;;
    parrot) base_url="https://archive.parrotsec.org/parrot/pool/main/l/linux-parrot/" ;;
    *)      analysis_die "Unexpected distro in _fetch_kali: ${DISTRO}" ;;
  esac

  local pkg_file
  pkg_file="$(python3 - "$base_url" "$pkg_name" "$deb_arch" <<'PY'
import sys, urllib.request, re
base_url, pkg_name, arch = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with urllib.request.urlopen(base_url, timeout=30) as r:
        html = r.read().decode("utf-8", errors="replace")
    # Match <pkg_name>*_<arch>.{ddeb,deb}
    pattern = re.compile(
        rf'href="({re.escape(pkg_name)}[^"]*_{re.escape(arch)}\.d?deb)"'
    )
    m = pattern.search(html)
    if m:
        print(m.group(1))
        sys.exit(0)
    print(f"no match for {pkg_name} arch={arch}", file=sys.stderr)
except Exception as e:
    print(str(e), file=sys.stderr)
sys.exit(1)
PY
  )" || analysis_die "Cannot locate ${pkg_name} at ${base_url}"

  local dest="$WORK_DIR/${pkg_file}"
  _download "${base_url}${pkg_file}" "$dest" || exit 3
  _extract_deb "$dest" "$WORK_DIR/extract"
  local elf; elf="$(_find_first "$WORK_DIR/extract" "vmlinux-${KERNEL_RELEASE}")"
  [[ -n "$elf" ]] || exit 4
  _finalise "$elf"
}

# ── rpm family (centos / rhel / rocky / almalinux / ol / fedora) ──────────────
# Tries debuginfod first (requires elfutils-debuginfod-client).
# Falls back to direct RPM download from the distro's debuginfo mirror.

_debuginfod_try() {
  command -v debuginfod-find >/dev/null 2>&1 || return 1

  # Use GUEST_BUILD_ID injected by Python (ISF-02).  Never read the host's
  # /boot: this script runs on the host hypervisor, not inside the VM, so
  # /boot/vmlinuz-<release> is the hypervisor's own kernel, not the guest's.
  # If the build_id is absent the caller should fail gracefully — we return 1
  # immediately rather than silently fetching the wrong debuginfo.
  local build_id="${GUEST_BUILD_ID:-}"
  [[ -n "$build_id" ]] || return 1

  analysis_log "Trying debuginfod for build-id: ${build_id}"
  local dest="$WORK_DIR/vmlinux-debuginfod"
  DEBUGINFOD_URLS="${DEBUGINFOD_URLS:-}" \
    debuginfod-find debuginfo "$build_id" -o "$dest" 2>&1 || return 1

  [[ -f "$dest" ]] || return 1
  _finalise "$dest"
  return 0
}

_rpm_debuginfo_url() {
  # Print the base URL for the kernel-debuginfo RPM.
  # Accepts: distro version arch
  local distro="$1" ver="$2" arch="$3"
  local major="${ver%%.*}"

  case "$distro" in
    centos)
      # CentOS 7 uses debuginfo.centos.org; CentOS Stream 8/9 uses different path
      if [[ "$major" == "7" ]]; then
        echo "https://debuginfo.centos.org/${major}/${arch}"
      else
        echo "https://debuginfo.centos.org/${major}-stream/${arch}"
      fi
      ;;
    rhel | ol)
      # RHEL uses cdn.redhat.com (requires subscription); OL uses oss repos
      # Point at Oracle Linux's public debuginfo as fallback for RHEL-compatible kernels
      echo "https://oss.oracle.com/ol${major}/debuginfo"
      ;;
    rocky)
      echo "https://dl.rockylinux.org/pub/rocky/${ver}/BaseOS/${arch}/debug/tree/Packages/k"
      ;;
    almalinux)
      echo "https://repo.almalinux.org/almalinux/${ver}/BaseOS/${arch}/debug/tree/Packages/k"
      ;;
    fedora)
      echo "https://dl.fedoraproject.org/pub/fedora/linux/releases/${major}/Everything/${arch}/debug/tree/Packages/k"
      ;;
    *)
      return 1
      ;;
  esac
}

_fetch_rpm() {
  local rpm_arch
  rpm_arch="${ARCH}"

  # Try debuginfod first (universal, no distro-specific logic)
  if _debuginfod_try; then
    return 0
  fi

  # Build package name: kernel-debuginfo-<version>-<release>.<arch>.rpm
  # kernel_release e.g.: 5.14.0-362.8.1.el9_3.x86_64
  # → rpm_version=5.14.0  rpm_release=362.8.1.el9_3
  local rpm_version rpm_release
  # The inner expansions are quoted so they are matched literally: an unquoted
  # ${rpm_version} would be read as a glob pattern, and a kernel release always
  # contains dots.
  rpm_version="${KERNEL_RELEASE%%[-_]*}"            # 5.14.0
  rpm_release="${KERNEL_RELEASE#"${rpm_version}"-}" # 362.8.1.el9_3.x86_64
  rpm_release="${rpm_release%".${rpm_arch}"}"       # 362.8.1.el9_3

  local pkg_name="${SYMBOL_PACKAGE_NAME:-kernel-debuginfo-${rpm_version}-${rpm_release}.${rpm_arch}.rpm}"
  local base_url; base_url="$(_rpm_debuginfo_url "$DISTRO" "$VERSION" "$rpm_arch")"
  local url="${base_url}/${pkg_name}"
  local dest="$WORK_DIR/${pkg_name}"

  _download "$url" "$dest" || exit 3
  _extract_rpm "$dest" "$WORK_DIR/extract"

  # vmlinux location varies:
  #   RHEL/CentOS: usr/lib/debug/lib/modules/<kernel>/vmlinux
  #   Fedora:      usr/lib/debug/lib/modules/<kernel>/vmlinux
  # Prefer the kernel-qualified path; a debuginfo RPM bundling several
  # kernels (or a mismatched package) must never hand back another
  # kernel's vmlinux — that is caught only at analysis time otherwise.
  local elf
  elf="$(find "$WORK_DIR/extract" -path "*/modules/${KERNEL_RELEASE}/vmlinux" -type f | sort | head -n 1)"
  if [[ -z "$elf" ]]; then
    elf="$(_find_first "$WORK_DIR/extract" "vmlinux")"
    [[ -n "$elf" ]] || exit 4
    if [[ "$elf" != *"/${KERNEL_RELEASE}/"* ]]; then
      analysis_die "Extracted vmlinux does not match requested kernel ${KERNEL_RELEASE}: ${elf}"
    fi
  fi
  _finalise "$elf"
}

# ── dispatch ──────────────────────────────────────────────────────────────────

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
analysis_log "Fetching vmlinux for ${DISTRO} ${VERSION} kernel ${KERNEL_RELEASE} (${ARCH})"

case "$DISTRO" in
  ubuntu)
    _fetch_ubuntu
    ;;
  debian)
    _fetch_debian
    ;;
  kali | parrot)
    _fetch_kali
    ;;
  centos | rhel | rocky | almalinux | ol)
    _fetch_rpm
    ;;
  fedora)
    DEBUGINFOD_URLS="${DEBUGINFOD_URLS:-https://debuginfod.fedoraproject.org}"
    _fetch_rpm
    ;;
  opensuse*)
    DEBUGINFOD_URLS="${DEBUGINFOD_URLS:-https://debuginfod.opensuse.org}"
    _debuginfod_try || exit 3
    ;;
  arch*)
    DEBUGINFOD_URLS="${DEBUGINFOD_URLS:-https://debuginfod.archlinux.org}"
    _debuginfod_try || exit 3
    ;;
  *)
    analysis_log "Distro '${DISTRO}' not natively supported — trying debuginfod as last resort"
    if _debuginfod_try; then
      exit 0
    fi
    analysis_log "ERROR: Cannot fetch vmlinux for distro '${DISTRO}'" >&2
    exit 2
    ;;
esac
fi
