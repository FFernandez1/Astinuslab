#!/usr/bin/env bash
# Downloads standard cloud images into <data root>/images/definitions — safe to
# invoke from any CWD. Optional first argument overrides the destination.
set -uo pipefail

# BASE_DIR is the code root; the images are mutable state owned by the data
# root. An installed release lives at <data root>/releases/<id>, so saving
# beside this script would fill a tree no service reads and the next upgrade
# discards. A development checkout is its own data root.
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
# Resolved from this script's code root.
# shellcheck disable=SC1091
source "$BASE_DIR/ops/images/image_download_metadata.sh"
if [[ -n "${ASTINUSLAB_DATA_DIR:-}" ]]; then
  DATA_DIR="$ASTINUSLAB_DATA_DIR"
elif [[ "$(basename "$(dirname "$BASE_DIR")")" == "releases" ]]; then
  DATA_DIR="$(cd "$BASE_DIR/../.." && pwd -P)"
else
  DATA_DIR="$BASE_DIR"
fi
DEST_DIR="${1:-$DATA_DIR/images/definitions}"
mkdir -p "$DEST_DIR"

# The four images use the shared compressed-size and disk-headroom guidance so
# qcow2 overlays and re-downloads do not fill the disk mid-way.
min_free_kb=$((ASTINUSLAB_IMAGE_DOWNLOAD_MIN_FREE_GB * 1024 * 1024))
free_kb="$(df -Pk "$DEST_DIR" | awk 'NR == 2 {print $4}')"
if (( free_kb < min_free_kb )); then
  echo "WARNING: less than $ASTINUSLAB_IMAGE_DOWNLOAD_MIN_FREE_GB GB free under $DEST_DIR — downloads may fail." >&2
fi

failed=0
fetch() {
  local label="$1" url="$2"
  echo "Downloading $label..."
  if ! wget -N --progress=bar:force:noscroll -P "$DEST_DIR" "$url"; then
    echo "FAILED: $label ($url)" >&2
    failed=$((failed + 1))
  fi
}

# Each URL's trailing filename must match the `image_ref` of the corresponding
# target in config/catalog/targets.yaml — wget saves under the remote name, and
# targets resolve images by filename.
fetch "Ubuntu 22.04" "https://cloud-images.ubuntu.com/releases/22.04/release/ubuntu-22.04-server-cloudimg-amd64-disk-kvm.img"
# The 24.04 release channel publishes this build as ubuntu-24.04-server-cloudimg-amd64.img;
# only the per-codename channel still carries the noble-* filename the catalog pins.
fetch "Ubuntu 24.04" "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
fetch "Debian 12" "https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2"
# Fedora 40 is end-of-life and no longer on the mirror network; it lives on the archive host.
fetch "Fedora 40" "https://archives.fedoraproject.org/pub/archive/fedora/linux/releases/40/Cloud/x86_64/images/Fedora-Cloud-Base-Generic.x86_64-40-1.14.qcow2"

if (( failed > 0 )); then
  echo "Done with $failed failure(s). Re-run to retry — wget -N only refreshes stale/incomplete files." >&2
  exit 1
fi
echo "Done! Images downloaded to $DEST_DIR."
