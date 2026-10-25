#!/usr/bin/env bash
set -euo pipefail

# precook_image.sh — one command to make ANY image compatible with AstinusLab.
#
# Users should not have to know which of the two role-specific recipes an image
# needs. This wraps both and picks the right one:
#   * TARGET images (vulnerable victims): reset cloud-init so the per-VM NoCloud
#     (cidata) seed is consumed on next boot, injecting the active lab SSH key +
#     qemu-guest-agent. Delegates to prepare_target_image.sh (network-free).
#   * ATTACKER images (offensive, e.g. Kali/Parrot — no cloud-init): bake the
#     active lab SSH key + user + openssh-server/qemu-guest-agent/DHCP into the
#     image. Delegates to prebake_attacker_image.sh.
#
# Role is auto-detected (override with --role). The active lab SSH public key is
# resolved from storage/ssh_keys/.active — the same key the orchestrator injects
# — so attacker prebaking needs no key argument.
#
# Usage:
#   precook_image.sh <image> [--role target|attacker] [--user U] [--key PUB]
#                    [--output OUT] [--with-packages] [--resolve-only]
#
#   <image>         a filename under images/definitions/, a path to a .qcow2/.img,
#                   or a catalog id (target id or attacker id).
#   --role          force target|attacker (default: auto-detect).
#   --user          attacker login to bake (default: catalog/meta/distro default).
#   --key PUB       SSH public key to inject for attacker (default: active lab key).
#   --output OUT    attacker output image (default: <name>-attacker-base.qcow2).
#   --with-packages target-only: also apt-install openssh-server + qemu-guest-agent.
#   --resolve-only  print the resolved plan and exit (no root, CI-safe, no writes).

usage() {
  cat >&2 <<'EOF'
Usage: precook_image.sh <image> [--role target|attacker] [--user U] [--key PUB]
                        [--output OUT] [--with-packages] [--resolve-only]

  <image>         filename under images/definitions/, a path to a .qcow2/.img,
                  or a catalog id (target id or attacker id).
  --role          force target|attacker (default: auto-detect).
  --user U        attacker login to bake (default: catalog/meta/distro default).
  --key PUB       SSH public key to inject (default: active lab key).
  --output OUT    attacker output image (default: <name>-attacker-base.qcow2).
  --with-packages target-only: also apt-install openssh-server + qemu-guest-agent.
  --resolve-only  print the resolved plan and exit (no root, CI-safe).
EOF
  exit 1
}

[[ $# -lt 1 ]] && usage
ARG="$1"
shift

ROLE=""
USER_OVERRIDE=""
KEY_OVERRIDE=""
OUTPUT_OVERRIDE=""
WITH_PACKAGES="false"
RESOLVE_ONLY="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --role) [[ $# -ge 2 ]] || usage; ROLE="$2"; shift 2 ;;
    --user) [[ $# -ge 2 ]] || usage; USER_OVERRIDE="$2"; shift 2 ;;
    --key) [[ $# -ge 2 ]] || usage; KEY_OVERRIDE="$2"; shift 2 ;;
    --output) [[ $# -ge 2 ]] || usage; OUTPUT_OVERRIDE="$2"; shift 2 ;;
    --with-packages) WITH_PACKAGES="true"; shift ;;
    --resolve-only) RESOLVE_ONLY="true"; shift ;;
    *) usage ;;
  esac
done

case "$ROLE" in
  ""|target|attacker) ;;
  *) echo "Invalid --role: $ROLE (expected target or attacker)" >&2; usage ;;
esac

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEFS_DIR="$BASE_DIR/images/definitions"
HOST_DIR="$BASE_DIR/ops/host"

pick_catalog() {
  # Prefer the live catalog; fall back to the committed example (the live
  # *.yaml is gitignored and absent in CI), mirroring catalog.py.
  if [[ -f "$1" ]]; then echo "$1"; else echo "$2"; fi
}
TARGETS_YAML="$(pick_catalog "$BASE_DIR/config/catalog/targets.yaml" "$BASE_DIR/config/catalog/targets.example.yaml")"
ATTACKERS_YAML="$(pick_catalog "$BASE_DIR/config/catalog/attackers.yaml" "$BASE_DIR/config/catalog/attackers.example.yaml")"

# Resolve, from the catalog + any .meta.json sidecar: the catalog kind
# (attacker|target|none), the image filename, the catalog username, and the
# detected distro. Tab-separated so empty fields survive the read.
IFS=$'\t' read -r CATALOG_KIND IMAGE_REF CAT_USERNAME DISTRO < <(
  python3 - "$ARG" "$TARGETS_YAML" "$ATTACKERS_YAML" "$DEFS_DIR" <<'PY'
import json, sys
from pathlib import Path

try:
    import yaml
except Exception:
    yaml = None

arg, targets_yaml, attackers_yaml, defs_dir = sys.argv[1:5]


def load(path):
    if yaml is None:
        return {}
    try:
        return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}


kind, image_ref, username, distro = "none", arg, "", ""

for a in load(attackers_yaml).get("attackers", []):
    if a.get("id") == arg:
        kind = "attacker"
        image_ref = a.get("image_ref", arg)
        username = a.get("username", "")
        break

if kind == "none":
    for t in load(targets_yaml).get("targets", []):
        if t.get("id") == arg:
            kind = "target"
            img = t.get("image", {}) or {}
            image_ref = img.get("image_ref", arg)
            username = img.get("default_username", "")
            distro = ((t.get("distro", {}) or {}).get("slug") or "").lower()
            break

ref_name = Path(image_ref).name
if "/" in image_ref or Path(image_ref).exists():
    candidate = Path(image_ref)
else:
    candidate = Path(defs_dir) / ref_name
meta = candidate.with_suffix(candidate.suffix + ".meta.json")
if not distro and meta.exists():
    try:
        distro = (json.loads(meta.read_text(encoding="utf-8")).get("distro") or "").lower()
    except Exception:
        pass

print("\t".join([kind, ref_name, username, distro]))
PY
)

# Resolve the on-disk image path (a raw path arg wins over catalog resolution).
if [[ "$ARG" == */* || -f "$ARG" ]]; then
  IMG_PATH="$ARG"
  IMAGE_REF="$(basename "$ARG")"
else
  IMG_PATH="$DEFS_DIR/$IMAGE_REF"
fi

# Decide the role: explicit flag > catalog membership > distro > filename hint.
role="$ROLE"
if [[ -z "$role" ]]; then
  lc_ref="${IMAGE_REF,,}"
  if [[ "$CATALOG_KIND" == "attacker" ]]; then
    role="attacker"
  elif [[ "$CATALOG_KIND" == "target" ]]; then
    role="target"
  elif [[ "$DISTRO" == "kali" || "$DISTRO" == "parrot" ]]; then
    role="attacker"
  elif [[ "$lc_ref" == *kali* || "$lc_ref" == *parrot* ]]; then
    role="attacker"
  else
    role="target"
  fi
fi

# ── TARGET ─────────────────────────────────────────────────────────────────────
if [[ "$role" == "target" ]]; then
  if [[ "$RESOLVE_ONLY" == "true" ]]; then
    tcmd="prepare_target_image.sh $ARG"
    [[ "$WITH_PACKAGES" == "true" ]] && tcmd+=" --with-packages"
    printf 'role:       %s\n' "target"
    printf 'image_ref:  %s\n' "$IMAGE_REF"
    printf 'path:       %s\n' "$IMG_PATH"
    printf 'username:   %s\n' "${CAT_USERNAME:-ubuntu} (per-VM cloud-init seed)"
    printf 'key:        %s\n' "(per-VM seed; nothing baked into the image)"
    printf 'output:     %s\n' "(prepared in place)"
    printf 'command:    %s\n' "$tcmd"
    exit 0
  fi
  args=("$HOST_DIR/prepare_target_image.sh" "$ARG")
  [[ "$WITH_PACKAGES" == "true" ]] && args+=(--with-packages)
  exec "${args[@]}"
fi

# ── ATTACKER ───────────────────────────────────────────────────────────────────
distro_default_user() {
  case "$1" in
    parrot) echo "user" ;;
    *) echo "kali" ;;
  esac
}
ATTACKER_USER="${USER_OVERRIDE:-${CAT_USERNAME:-$(distro_default_user "$DISTRO")}}"

resolve_key() {
  if [[ -n "$KEY_OVERRIDE" ]]; then echo "$KEY_OVERRIDE"; return; fi
  local active="$BASE_DIR/storage/ssh_keys/.active"
  if [[ -f "$active" ]]; then
    local name; name="$(tr -d '[:space:]' < "$active")"
    if [[ -n "$name" && -f "$BASE_DIR/storage/ssh_keys/$name.pub" ]]; then
      echo "$BASE_DIR/storage/ssh_keys/$name.pub"; return
    fi
  fi
  # Legacy default used by prebake_attacker_image.sh before keystore existed.
  if [[ -f "$BASE_DIR/storage/ssh_keys/1.pub" ]]; then
    echo "$BASE_DIR/storage/ssh_keys/1.pub"; return
  fi
  echo ""
}
KEY="$(resolve_key)"

# Default output: <name>-attacker-base.qcow2, stripping any existing suffix so a
# re-run stays idempotent instead of producing …-attacker-base-attacker-base.
stem="${IMAGE_REF%.*}"
stem="${stem%-attacker-base}"
OUTPUT="${OUTPUT_OVERRIDE:-$DEFS_DIR/${stem}-attacker-base.qcow2}"

if [[ "$RESOLVE_ONLY" == "true" ]]; then
  printf 'role:       %s\n' "attacker"
  printf 'image_ref:  %s\n' "$IMAGE_REF"
  printf 'path:       %s\n' "$IMG_PATH"
  printf 'username:   %s\n' "$ATTACKER_USER"
  printf 'key:        %s\n' "${KEY:-(none — generate one on the Security page)}"
  printf 'output:     %s\n' "$OUTPUT"
  printf 'command:    %s\n' "prebake_attacker_image.sh $IMG_PATH $OUTPUT $ATTACKER_USER ${KEY:-<key>}"
  exit 0
fi

[[ -f "$IMG_PATH" ]] || { echo "Image not found: $IMG_PATH" >&2; exit 1; }

img_real="$(readlink -f "$IMG_PATH" 2>/dev/null || echo "$IMG_PATH")"
out_real="$(readlink -f "$OUTPUT" 2>/dev/null || echo "$OUTPUT")"
if [[ "$img_real" == "$out_real" ]]; then
  echo "Refusing to overwrite the source image in place ($IMG_PATH)." >&2
  echo "It already looks like an attacker base. Pass --output OUT to force a new file." >&2
  exit 1
fi

if [[ -z "$KEY" || ! -f "$KEY" ]]; then
  echo "No SSH public key available to inject." >&2
  echo "Generate/activate one on the Security page of the web UI, or pass --key PUB." >&2
  exit 1
fi

exec "$HOST_DIR/prebake_attacker_image.sh" "$IMG_PATH" "$OUTPUT" "$ATTACKER_USER" "$KEY"
