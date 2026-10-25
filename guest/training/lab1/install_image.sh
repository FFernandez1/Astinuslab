#!/usr/bin/env bash
set -Eeuo pipefail
trap 'printf "install_image.sh failed at line %s\n" "$LINENO" >&2' ERR

if [[ $# -ne 1 || ! "$1" =~ ^[0-9a-f]{64}$ ]]; then
  echo "Usage: $0 <dvwa-archive-sha256>" >&2
  exit 2
fi

expected_dvwa_sha256="$1"
image_root="${ASTINUSLAB_IMAGE_ROOT:-}"
if [[ -n "$image_root" && ( "$image_root" != /* || ! -d "$image_root" ) ]]; then
  echo "ASTINUSLAB_IMAGE_ROOT must name an existing absolute directory" >&2
  exit 2
fi
path_in_image() { printf '%s%s' "$image_root" "$1"; }

bundle="$(path_in_image /opt/astinuslab-training/lab1)"
archive="$bundle/dvwa.tar.gz"
actual_dvwa_sha256="$(sha256sum "$archive" | awk '{print $1}')"
if [[ "$actual_dvwa_sha256" != "$expected_dvwa_sha256" ]]; then
  echo "DVWA archive SHA-256 mismatch" >&2
  exit 1
fi

web_root="$(path_in_image /var/www/html/dvwa)"
rm -rf -- "$web_root"
install -d -m 0755 "$web_root"
tar -xzf "$archive" -C "$web_root" --strip-components=1

install -d -m 0755 \
  "$(path_in_image /etc/cloud/cloud.cfg.d)" \
  "$(path_in_image /etc/netplan)" \
  "$(path_in_image /etc/sysctl.d)" \
  "$(path_in_image /etc/systemd/system/apache2.service.d)" \
  "$(path_in_image /etc/systemd/system)" \
  "$(path_in_image /usr/local/sbin)"

cat >"$web_root/config/config.inc.php" <<'PHP'
<?php
$DBMS = 'MySQL';
$_DVWA = array();
$_DVWA[ 'db_server' ] = '127.0.0.1';
$_DVWA[ 'db_database' ] = 'dvwa';
$_DVWA[ 'db_user' ] = 'dvwa';
$_DVWA[ 'db_password' ] = 'p@ssw0rd';
$_DVWA[ 'db_port' ] = '3306';
$_DVWA[ 'recaptcha_public_key' ] = '';
$_DVWA[ 'recaptcha_private_key' ] = '';
$_DVWA[ 'default_security_level' ] = 'low';
$_DVWA[ 'default_phpids_level' ] = 'disabled';
$_DVWA[ 'default_phpids_verbose' ] = 'false';
$_DVWA[ 'default_locale' ] = 'en';
$_DVWA[ 'disable_authentication' ] = false;
define('MYSQL', 'mysql');
define('SQLITE', 'sqlite');
$_DVWA['SQLI_DB'] = MYSQL;
PHP

cat >"$(path_in_image /etc/netplan/60-astinuslab-training.yaml)" <<'YAML'
network:
  version: 2
  renderer: networkd
  ethernets:
    entry:
      match:
        name: "en*"
      dhcp4: false
      dhcp6: false
      accept-ra: false
      link-local: []
      addresses:
        - 10.77.10.10/24
YAML
chmod 0600 "$(path_in_image /etc/netplan/60-astinuslab-training.yaml)"
printf '%s\n' 'network: {config: disabled}' >"$(path_in_image /etc/cloud/cloud.cfg.d/99-astinuslab-disable-network-config.cfg)"
cat >"$(path_in_image /etc/sysctl.d/99-astinuslab-training.conf)" <<'CONF'
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
CONF
cat >"$(path_in_image /etc/systemd/system/apache2.service.d/astinuslab-training.conf)" <<'CONF'
[Service]
PrivateTmp=false
CONF

install -m 0755 "$bundle/readiness.sh" "$(path_in_image /usr/local/sbin/astinuslab-training-readiness)"
install -m 0755 "$bundle/firstboot_setup.sh" "$(path_in_image /usr/local/sbin/astinuslab-training-dvwa-setup)"
install -m 0644 "$bundle/astinuslab-training-dvwa-setup.service" \
  "$(path_in_image /etc/systemd/system/astinuslab-training-dvwa-setup.service)"
install -m 0644 "$bundle/astinuslab-training-readiness.service" \
  "$(path_in_image /etc/systemd/system/astinuslab-training-readiness.service)"
install -m 0644 "$bundle/astinuslab-training-observation-clear.service" \
  "$(path_in_image /etc/systemd/system/astinuslab-training-observation-clear.service)"
install -d -m 0750 "$(path_in_image /var/lib/astinuslab/observations)"
rm -f -- "$(path_in_image /tmp/astinuslab-lab1-proof)" \
  "$(path_in_image /var/lib/astinuslab/observations/readiness.json)"

if [[ -z "$image_root" ]]; then
  chown -R root:root "$web_root"
  chown -R www-data:www-data "$web_root/hackable/uploads" "$web_root/external/phpids/0.6/lib/IDS/tmp"
  chmod 0770 "$web_root/hackable/uploads" "$web_root/external/phpids/0.6/lib/IDS/tmp"
  chown root:root /var/lib/astinuslab /var/lib/astinuslab/observations
  chmod 0750 /var/lib/astinuslab/observations
  find /etc/php -path '*/apache2/php.ini' -type f -exec sed -i \
    -e 's/^allow_url_fopen = .*/allow_url_fopen = On/' \
    -e 's/^allow_url_include = .*/allow_url_include = On/' {} +
  systemctl enable apache2 mariadb qemu-guest-agent \
    astinuslab-training-dvwa-setup.service astinuslab-training-observation-clear.service \
    astinuslab-training-readiness.service
  cloud-init clean --logs --seed || cloud-init clean --logs
fi
