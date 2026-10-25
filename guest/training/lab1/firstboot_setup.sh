#!/usr/bin/env bash
set -Eeuo pipefail
trap 'printf "firstboot_setup.sh failed at line %s\n" "$LINENO" >&2' ERR

stamp=/var/lib/astinuslab/training-dvwa-configured
[[ ! -e "$stamp" ]] || exit 0

systemctl start mariadb apache2
for _ in $(seq 1 30); do
  mariadb-admin --protocol=socket ping --silent >/dev/null 2>&1 && break
  sleep 1
done
mariadb-admin --protocol=socket ping --silent >/dev/null

mariadb --protocol=socket <<'SQL'
CREATE USER IF NOT EXISTS 'dvwa'@'localhost' IDENTIFIED BY 'p@ssw0rd';
CREATE USER IF NOT EXISTS 'dvwa'@'127.0.0.1' IDENTIFIED BY 'p@ssw0rd';
GRANT ALL PRIVILEGES ON dvwa.* TO 'dvwa'@'localhost';
GRANT ALL PRIVILEGES ON dvwa.* TO 'dvwa'@'127.0.0.1';
FLUSH PRIVILEGES;
SQL

cookie_jar="$(mktemp /run/astinuslab-dvwa-cookie.XXXXXX)"
response="$(mktemp /run/astinuslab-dvwa-response.XXXXXX)"
cleanup() { rm -f -- "$cookie_jar" "$response"; }
trap cleanup EXIT

for _ in $(seq 1 30); do
  if setup_page="$(curl -fsS -c "$cookie_jar" http://127.0.0.1/dvwa/setup.php 2>/dev/null)"; then
    break
  fi
  sleep 1
done
token="$(printf '%s\n' "$setup_page" | sed -n "s/.*name='user_token' value='\([^']*\)'.*/\1/p" | head -n 1)"
[[ "$token" =~ ^[0-9a-f]{32}$ ]]
curl -fsS -L -b "$cookie_jar" -c "$cookie_jar" \
  --data-urlencode 'create_db=Create / Reset Database' \
  --data-urlencode "user_token=$token" \
  http://127.0.0.1/dvwa/setup.php >"$response"
grep -q 'Setup successful' "$response"
[[ "$(mariadb --protocol=socket --batch --skip-column-names --execute='SELECT COUNT(*) FROM dvwa.users')" == "5" ]]
login_page="$(curl -fsS http://127.0.0.1/dvwa/login.php)"
grep -q 'Damn Vulnerable Web Application' <<<"$login_page"

install -d -m 0750 -o root -g root /var/lib/astinuslab
touch "$stamp"
chmod 0600 "$stamp"
