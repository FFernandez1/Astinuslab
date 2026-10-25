#!/usr/bin/env bash
set -Eeuo pipefail
trap 'printf "readiness.sh failed at line %s\n" "$LINENO" >&2' ERR

observation_dir="${ASTINUSLAB_OBSERVATION_DIR:-/var/lib/astinuslab/observations}"
observation_owner="${ASTINUSLAB_OBSERVATION_OWNER:-root}"
observation_group="${ASTINUSLAB_OBSERVATION_GROUP:-root}"
lease_dir="${ASTINUSLAB_NETWORKD_LEASE_DIR:-/run/systemd/netif/leases}"
output="$observation_dir/readiness.json"

if [[ -L "$observation_dir" || -L "$output" ]]; then
  echo "Observation path must not be a symlink" >&2
  exit 1
fi
install -d -o "$observation_owner" -g "$observation_group" -m 0750 "$observation_dir"
rm -f -- "$output"

systemctl is-active --quiet apache2
systemctl is-active --quiet mariadb
systemctl is-enabled --quiet qemu-guest-agent
[[ "$(mariadb --protocol=socket --batch --skip-column-names --execute='SELECT COUNT(*) FROM dvwa.users')" == "5" ]]
login_page="$(curl -fsS http://127.0.0.1/dvwa/login.php)"
grep -q 'Damn Vulnerable Web Application' <<<"$login_page"

ipv4_addresses="$(ip -4 -o address show scope global)"
[[ "$(printf '%s\n' "$ipv4_addresses" | awk '$4 == "10.77.10.10/24" { count++ } END { print count + 0 }')" == "1" ]]
[[ "$(printf '%s\n' "$ipv4_addresses" | awk 'NF { count++ } END { print count + 0 }')" == "1" ]]
[[ -z "$(ip -4 route show default)" ]]
[[ -z "$(ip -6 -o address show scope global)" ]]
[[ "$(sysctl -n net.ipv6.conf.all.disable_ipv6)" == "1" ]]
[[ "$(sysctl -n net.ipv6.conf.default.disable_ipv6)" == "1" ]]
lease_file=""
if [[ -d "$lease_dir" ]]; then
  lease_file="$(find "$lease_dir" -type f -size +0c -print -quit)"
fi
if [[ -n "$lease_file" ]]; then
  echo "Unexpected DHCP lease state" >&2
  exit 1
fi

temporary="$(mktemp "$observation_dir/.readiness.XXXXXX")"
cleanup() { rm -f -- "$temporary"; }
trap cleanup EXIT
printf '%s\n' '"v1:training-dvwa-single:ready"' >"$temporary"
chown "$observation_owner:$observation_group" "$temporary"
chmod 0640 "$temporary"
mv -f -- "$temporary" "$output"
trap - EXIT
