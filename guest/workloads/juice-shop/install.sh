#!/usr/bin/env bash
# OWASP Juice Shop lab installation script.
# Runs inside the guest VM via SSH. Installs Node.js and Juice Shop.
# Credentials: admin@juice-sh.op / admin123
set -euo pipefail

JUICE_VERSION="17.1.1"
JUICE_ASSET="juice-shop-${JUICE_VERSION}_node20_linux_x64.tgz"
JUICE_SHA256="61fb11dc95f69fae8a0500cfc0dd28327ea5ebd37376c906010136163879234c"

export DEBIAN_FRONTEND=noninteractive

# pkg_helper.sh is staged into the guest at /tmp by the host before this
# script runs, so it does not exist at lint time. The real file is linted
# on its own as guest/scripts/pkg_helper.sh.
# shellcheck source=/dev/null
source /tmp/pkg_helper.sh

echo "[juice-shop] Updating package index..."
pkg_update

echo "[juice-shop] Installing Node.js 20.x..."
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
pkg_install nodejs

echo "[juice-shop] Downloading Juice Shop ${JUICE_VERSION}..."
cd /opt
sudo curl -fsSL "https://github.com/juice-shop/juice-shop/releases/download/v${JUICE_VERSION}/${JUICE_ASSET}" \
    -o "${JUICE_ASSET}"
echo "${JUICE_SHA256}  ${JUICE_ASSET}" | sha256sum -c --quiet -
sudo tar -xzf "${JUICE_ASSET}"
sudo mv juice-shop_* juice-shop 2>/dev/null || true
sudo rm -f "${JUICE_ASSET}"

echo "[juice-shop] Creating systemd service..."
sudo tee /etc/systemd/system/juice-shop.service > /dev/null <<'SERVICE'
[Unit]
Description=OWASP Juice Shop
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/juice-shop
ExecStart=/usr/bin/npm start
Restart=on-failure
RestartSec=5
Environment=HOST=0.0.0.0
Environment=NODE_ENV=production
Environment=PORT=3000

[Install]
WantedBy=multi-user.target
SERVICE

sudo systemctl daemon-reload
sudo systemctl enable juice-shop
sudo systemctl start juice-shop

echo "[juice-shop] Waiting for Juice Shop to be ready..."
for _ in $(seq 1 60); do
    curl -sf http://localhost:3000/ > /dev/null 2>&1 && break
    sleep 2
done

echo "[juice-shop] Juice Shop ${JUICE_VERSION} installed successfully."
echo "[juice-shop] URL: http://localhost:3000"
echo "[juice-shop] Admin: admin@juice-sh.op / admin123"
