#!/usr/bin/env bash
set -euo pipefail

# Prepare a stock playground TARGET base image so its cloud-init re-runs on the
# next boot and consumes the per-VM NoCloud seed (SSH key + qemu-guest-agent)
# that the orchestrator stages. Stock cloud images otherwise ignore the seed and
# never become SSH-reachable; only pre-baked images (e.g. ubuntu-24.04) work.
#
# Unlike prebake_attacker_image.sh, this does NOT bake a user/key into the image:
# the per-VM seed still owns user/key/agent. We only reset cloud-init state and
# ensure ssh + qemu-guest-agent are present and enabled.
#
# The load-bearing repair is `cloud-init clean`: it wipes the image's stale
# cloud-init state so cloud-init re-runs on next boot and consumes the per-VM
# NoCloud (cidata) seed. This needs NO network and is why an air-gapped lab host
# still works. Installing openssh-server/qemu-guest-agent is OPT-IN (--with-
# packages) because it needs apt/network; Ubuntu server cloud images already
# ship openssh-server, and the catalog marks the guest agent optional.
#
# Usage:
#   prepare_target_image.sh <target-id|image-ref> [--resolve-only] [--with-packages]
#
#   <target-id>     an id from config/catalog/targets.yaml (e.g. ubuntu-20-04-legacy)
#   <image-ref>     or a raw image filename under images/definitions/
#   --resolve-only  print the resolved image path + username and exit (no root,
#                   CI-safe; does not touch the image)
#   --with-packages also apt-install openssh-server + qemu-guest-agent (needs
#                   network; for online hosts or images lacking ssh)

usage() {
  echo "Usage: $0 <target-id|image-ref> [--resolve-only] [--with-packages]" >&2
  exit 1
}

[[ $# -lt 1 ]] && usage

ARG="$1"
RESOLVE_ONLY="false"
WITH_PACKAGES="false"
shift
for opt in "$@"; do
  case "$opt" in
    --resolve-only) RESOLVE_ONLY="true" ;;
    --with-packages) WITH_PACKAGES="true" ;;
    *) usage ;;
  esac
done

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEFS_DIR="$BASE_DIR/images/definitions"
# Prefer the real catalog; fall back to the committed example (the live
# targets.yaml is gitignored and absent in CI), mirroring catalog.py.
TARGETS_YAML="$BASE_DIR/config/catalog/targets.yaml"
if [[ ! -f "$TARGETS_YAML" ]]; then
  TARGETS_YAML="$BASE_DIR/config/catalog/targets.example.yaml"
fi

# Resolve <arg> to (image_ref, username). If arg is a target id, read its
# image.image_ref + image.default_username from the catalog; otherwise treat arg
# as a literal image filename.
read -r IMAGE_REF USERNAME < <(
  python3 - "$ARG" "$TARGETS_YAML" <<'PY'
import sys, yaml
arg, targets_yaml = sys.argv[1], sys.argv[2]
ref, user = arg, "ubuntu"
try:
    with open(targets_yaml, encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    for t in doc.get("targets", []):
        if t.get("id") == arg:
            img = t.get("image", {}) or {}
            ref = img.get("image_ref", arg)
            user = img.get("default_username", "ubuntu")
            break
except FileNotFoundError:
    pass
print(ref, user)
PY
)

IMG="$DEFS_DIR/$IMAGE_REF"

if [[ "$RESOLVE_ONLY" == "true" ]]; then
  echo "image_ref: $IMAGE_REF"
  echo "path:      $IMG"
  echo "username:  $USERNAME"
  exit 0
fi

require_bin() {
  command -v "$1" >/dev/null 2>&1 || { echo "Missing required command: $1" >&2; exit 1; }
}
require_bin qemu-img
require_bin virt-customize

[[ -f "$IMG" ]] || { echo "Base image not found: $IMG" >&2; exit 1; }

WORK="$IMG.preparing.$$"
cleanup() { rm -f "$WORK"; }
trap cleanup EXIT

cp --reflink=auto "$IMG" "$WORK"

# Required, network-free "target" recipe: reset cloud-init so the per-VM seed is
# consumed on next boot, regenerate host keys on first boot, and enable ssh +
# guest agent if those units already exist (enabling is a symlink, no network).
CUSTOMIZE_ARGS=(
  --run-command "cloud-init clean --logs --seed || cloud-init clean --logs || true"
  --run-command "rm -f /etc/ssh/ssh_host_*"
  --run-command "systemctl enable ssh || systemctl enable ssh.service || true"
  --run-command "systemctl enable qemu-guest-agent || systemctl enable qemu-guest-agent.service || true"
)
# Opt-in package install (needs network/apt). Kept separate so an offline host
# still gets the load-bearing cloud-init reset above.
if [[ "$WITH_PACKAGES" == "true" ]]; then
  # shellcheck disable=SC2054  # virt-customize --install wants a comma list
  CUSTOMIZE_ARGS+=(--install openssh-server,qemu-guest-agent)
fi

if ! virt-customize -a "$WORK" "${CUSTOMIZE_ARGS[@]}"; then
  echo >&2
  echo "virt-customize failed." >&2
  if [[ "$WITH_PACKAGES" == "true" ]]; then
    echo "If apt could not reach the network, re-run WITHOUT --with-packages:" >&2
    echo "  the cloud-init reset alone makes the per-VM seed apply, and Ubuntu" >&2
    echo "  server cloud images already ship openssh-server." >&2
  fi
  echo "If libguestfs's appliance is broken (supermin error):" >&2
  echo "  sudo apt install -y libguestfs-tools" >&2
  echo "  sudo chmod 0644 /boot/vmlinuz-*    # supermin must read the kernel" >&2
  echo "  libguestfs-test-tool              # must exit 0" >&2
  exit 1
fi

mv -f "$WORK" "$IMG"
trap - EXIT

# Record prepared state in the per-image meta.json (provenance stays truthful).
META="$IMG.meta.json"
if [[ -f "$META" ]]; then
  python3 - "$META" <<'PY'
import json, sys, datetime
meta_path = sys.argv[1]
with open(meta_path, encoding="utf-8") as f:
    meta = json.load(f)
meta["prepared"] = True
meta["prepared_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
with open(meta_path, "w", encoding="utf-8") as f:
    json.dump(meta, f, indent=2)
    f.write("\n")
PY
fi

echo "Prepared target base image:"
echo "  image:    $IMG"
echo "  username: $USERNAME (provided per-VM via cloud-init seed)"
echo
echo "Next steps:"
echo "  Boot this target via the Playground; the per-VM seed will now be"
echo "  consumed, injecting the active SSH key and starting qemu-guest-agent."
