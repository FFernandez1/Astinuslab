#!/usr/bin/env bash
# WordPress 6.4 lab installation script.
# Runs inside the guest VM via SSH. Installs Apache, PHP, MySQL, and WP 6.4.
# Credentials: WordPress admin:admin, MySQL root:labroot
set -euo pipefail

WP_VERSION="6.4"
WP_CLI_VERSION="2.10.0"
WP_CLI_SHA256="4c6a93cecae7f499ca481fa7a6d6d4299c8b93214e5e5308e26770dbfd3631df"
WP_DB_NAME="wordpress"
WP_DB_USER="wpuser"
WP_DB_PASS="wppass"
MYSQL_ROOT_PASS="labroot"
WP_ADMIN_USER="admin"
WP_ADMIN_PASS="admin"
WP_ADMIN_EMAIL="admin@lab.local"
WP_SITE_URL="http://$(hostname -I | awk '{print $1}')"

export DEBIAN_FRONTEND=noninteractive

# pkg_helper.sh is staged into the guest at /tmp by the host before this
# script runs, so it does not exist at lint time. The real file is linted
# on its own as guest/scripts/pkg_helper.sh.
# shellcheck source=/dev/null
source /tmp/pkg_helper.sh

echo "[wp-install] Updating package index..."
pkg_update

echo "[wp-install] Installing Apache, PHP, MySQL..."
pkg_install \
    apache2 \
    php php-mysql php-curl php-gd php-mbstring php-xml php-xmlrpc php-soap php-intl php-zip \
    mysql-server \
    curl \
    unzip

echo "[wp-install] Configuring MySQL..."
sudo systemctl start mysql
for _ in $(seq 1 30); do sudo mysqladmin ping --silent 2>/dev/null && break; sleep 1; done
sudo mysql --defaults-file=/etc/mysql/debian.cnf <<SQL
ALTER USER 'root'@'localhost' IDENTIFIED WITH mysql_native_password BY '${MYSQL_ROOT_PASS}';
CREATE DATABASE IF NOT EXISTS ${WP_DB_NAME};
CREATE USER IF NOT EXISTS '${WP_DB_USER}'@'localhost' IDENTIFIED BY '${WP_DB_PASS}';
GRANT ALL PRIVILEGES ON ${WP_DB_NAME}.* TO '${WP_DB_USER}'@'localhost';
FLUSH PRIVILEGES;
SQL

echo "[wp-install] Downloading WordPress ${WP_VERSION}..."
cd /tmp
curl -fsSL "https://wordpress.org/wordpress-${WP_VERSION}.tar.gz" -o wordpress.tar.gz
# wordpress.org publishes a SHA-1 beside every release archive.
wp_sha1="$(curl -fsSL "https://wordpress.org/wordpress-${WP_VERSION}.tar.gz.sha1")"
echo "${wp_sha1}  wordpress.tar.gz" | sha1sum -c --quiet -
sudo tar -xzf wordpress.tar.gz -C /var/www/html/ --strip-components=1
sudo chown -R www-data:www-data /var/www/html/
sudo find /var/www/html/ -type d -exec chmod 755 {} \;
sudo find /var/www/html/ -type f -exec chmod 644 {} \;

echo "[wp-install] Configuring WordPress..."
sudo cp /var/www/html/wp-config-sample.php /var/www/html/wp-config.php
sudo sed -i "s/database_name_here/${WP_DB_NAME}/" /var/www/html/wp-config.php
sudo sed -i "s/username_here/${WP_DB_USER}/" /var/www/html/wp-config.php
sudo sed -i "s/password_here/${WP_DB_PASS}/" /var/www/html/wp-config.php

echo "[wp-install] Configuring Apache..."
sudo tee /etc/apache2/sites-available/000-default.conf > /dev/null <<'APACHE'
<VirtualHost *:80>
    DocumentRoot /var/www/html
    <Directory /var/www/html>
        AllowOverride All
        Require all granted
    </Directory>
</VirtualHost>
APACHE

sudo a2enmod rewrite
sudo systemctl restart apache2

echo "[wp-install] Running WordPress install via WP-CLI..."
curl -fsSL "https://github.com/wp-cli/wp-cli/releases/download/v${WP_CLI_VERSION}/wp-cli-${WP_CLI_VERSION}.phar" -o /tmp/wp
echo "${WP_CLI_SHA256}  /tmp/wp" | sha256sum -c --quiet -
sudo install -m 0755 /tmp/wp /usr/local/bin/wp

sudo -u www-data wp core install \
    --path=/var/www/html \
    --url="${WP_SITE_URL}" \
    --title="Lab WordPress" \
    --admin_user="${WP_ADMIN_USER}" \
    --admin_password="${WP_ADMIN_PASS}" \
    --admin_email="${WP_ADMIN_EMAIL}" \
    --skip-email \
    --allow-root

echo "[wp-install] WordPress ${WP_VERSION} installed successfully."
echo "[wp-install] URL: ${WP_SITE_URL}/wp-login.php"
echo "[wp-install] Admin: ${WP_ADMIN_USER} / ${WP_ADMIN_PASS}"
