#!/usr/bin/env bash
# GitLab CE 11.4.7 lab installation script.
# Runs inside the guest VM via SSH.
# Exposes the SSRF+CSRF → RCE chain (CVE-2018-19571 / CVE-2018-19585).
# Credentials: root / labpassword
set -euo pipefail

GITLAB_VERSION="11.4.7"
ROOT_PASS="labpassword"
EXTERNAL_URL="http://$(hostname -I | awk '{print $1}')"

export DEBIAN_FRONTEND=noninteractive

# pkg_helper.sh is staged into the guest at /tmp by the host before this
# script runs, so it does not exist at lint time. The real file is linted
# on its own as guest/scripts/pkg_helper.sh.
# shellcheck source=/dev/null
source /tmp/pkg_helper.sh

echo "[gitlab-install] Installing dependencies..."
pkg_update
pkg_install curl openssh-server ca-certificates tzdata perl

echo "[gitlab-install] Adding GitLab package repository for version ${GITLAB_VERSION}..."
curl -sSL https://packages.gitlab.com/install/repositories/gitlab/gitlab-ce/script.deb.sh | sudo bash

echo "[gitlab-install] Installing GitLab CE ${GITLAB_VERSION}..."
pkg_install gitlab-ce="${GITLAB_VERSION}-ce.0"

echo "[gitlab-install] Configuring external URL..."
sudo sed -i "s|external_url '.*'|external_url '${EXTERNAL_URL}'|" /etc/gitlab/gitlab.rb

echo "[gitlab-install] Running gitlab-ctl reconfigure (this takes a few minutes)..."
sudo gitlab-ctl reconfigure

echo "[gitlab-install] Setting root password..."
sudo gitlab-rails runner "
user = User.find_by(username: 'root')
user.password = '${ROOT_PASS}'
user.password_confirmation = '${ROOT_PASS}'
user.save!
puts 'Root password set.'
"

echo "[gitlab-install] GitLab CE ${GITLAB_VERSION} installed successfully."
echo "[gitlab-install] URL: ${EXTERNAL_URL}/users/sign_in"
echo "[gitlab-install] Admin: root / ${ROOT_PASS}"
echo "[gitlab-install] CVEs: CVE-2018-19571 (SSRF), CVE-2018-19585 (CSRF -> RCE)"
