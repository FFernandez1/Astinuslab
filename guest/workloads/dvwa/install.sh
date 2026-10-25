#!/usr/bin/env bash
# DVWA (Damn Vulnerable Web App) lab installation script.
# Runs inside the guest VM via SSH. Installs Apache, PHP, MySQL, and DVWA.
# Credentials: DVWA admin:password, MySQL root:labroot
set -euo pipefail

MYSQL_ROOT_PASS="labroot"
DVWA_DB_NAME="dvwa"
DVWA_DB_USER="dvwa"
DVWA_DB_PASS="p@ssw0rd"
# Same commit as guest/training/lab1/build.py. A clone of the default branch
# would install whatever upstream pushed last.
DVWA_COMMIT="3663d72c4c4f51ac5bcd9f0c8a16b9d890383347"

export DEBIAN_FRONTEND=noninteractive

# pkg_helper.sh is staged into the guest at /tmp by the host before this
# script runs, so it does not exist at lint time. The real file is linted
# on its own as guest/scripts/pkg_helper.sh.
# shellcheck source=/dev/null
source /tmp/pkg_helper.sh

echo "[dvwa-install] Updating package index..."
pkg_update

echo "[dvwa-install] Installing Apache, PHP, MySQL..."
pkg_install \
    apache2 \
    php php-mysql php-gd php-xml \
    mysql-server \
    git

echo "[dvwa-install] Configuring MySQL..."
sudo systemctl start mysql
# Wait for MySQL to be ready (up to 30s)
for _ in $(seq 1 30); do sudo mysqladmin ping --silent 2>/dev/null && break; sleep 1; done
# /etc/mysql/debian.cnf holds debian-sys-maint credentials that always work on Ubuntu
sudo mysql --defaults-file=/etc/mysql/debian.cnf <<SQL
ALTER USER 'root'@'localhost' IDENTIFIED WITH mysql_native_password BY '${MYSQL_ROOT_PASS}';
CREATE DATABASE IF NOT EXISTS ${DVWA_DB_NAME};
CREATE USER IF NOT EXISTS '${DVWA_DB_USER}'@'localhost' IDENTIFIED BY '${DVWA_DB_PASS}';
GRANT ALL PRIVILEGES ON ${DVWA_DB_NAME}.* TO '${DVWA_DB_USER}'@'localhost';
FLUSH PRIVILEGES;
SQL

echo "[dvwa-install] Fetching DVWA ${DVWA_COMMIT}..."
sudo rm -rf /var/www/html/dvwa
sudo git init -q /var/www/html/dvwa
sudo git -C /var/www/html/dvwa fetch -q --depth=1 https://github.com/digininja/DVWA.git "${DVWA_COMMIT}"
sudo git -C /var/www/html/dvwa checkout -q FETCH_HEAD
if [[ "$(sudo git -C /var/www/html/dvwa rev-parse HEAD)" != "${DVWA_COMMIT}" ]]; then
    echo "[dvwa-install] DVWA checkout does not match ${DVWA_COMMIT}" >&2
    exit 1
fi

echo "[dvwa-install] Configuring DVWA..."
sudo cp /var/www/html/dvwa/config/config.inc.php.dist /var/www/html/dvwa/config/config.inc.php
sudo sed -i "s/'db_password'.*/'db_password'] = '${DVWA_DB_PASS}';/" /var/www/html/dvwa/config/config.inc.php
sudo sed -i "s/'db_database'.*/'db_database'] = '${DVWA_DB_NAME}';/" /var/www/html/dvwa/config/config.inc.php
sudo sed -i "s/'db_user'.*/'db_user'] = '${DVWA_DB_USER}';/" /var/www/html/dvwa/config/config.inc.php

# Enable file uploads and allow_url_include for lab purposes.
# -exec ... + rather than a for-loop over find output: the loop word-splits on
# paths and the exit status of find is lost under set -e.
sudo find /etc/php -name php.ini -exec sed -i \
    -e 's/allow_url_include = Off/allow_url_include = On/' \
    -e 's/allow_url_fopen = Off/allow_url_fopen = On/' {} + 2>/dev/null || true

sudo chown -R www-data:www-data /var/www/html/dvwa
sudo chmod -R 755 /var/www/html/dvwa
sudo chmod 777 /var/www/html/dvwa/hackable/uploads
sudo chmod 777 /var/www/html/dvwa/config

sudo systemctl restart apache2

echo "[dvwa-install] DVWA installed successfully."
echo "[dvwa-install] URL: http://localhost/dvwa/login.php"
echo "[dvwa-install] Admin: admin / password"
echo "[dvwa-install] NOTE: After first login, visit /dvwa/setup.php to initialize the database."
