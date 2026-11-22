#!/bin/bash
# Remove CouchPi and boot back to the normal console.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo"; exit 1; }
systemctl disable --now couchpi.service couchpi-cec.service couchpi-update.timer 2>/dev/null || true
rm -f /etc/systemd/system/couchpi.service /etc/systemd/system/couchpi-cec.service \
      /etc/systemd/system/couchpi-update.service /etc/systemd/system/couchpi-update.timer \
      /etc/sudoers.d/couchpi /etc/pam.d/couchpi /etc/tmpfiles.d/couchpi.conf \
      /usr/local/bin/couchpi-apps
systemctl daemon-reload
systemctl set-default multi-user.target
/opt/couchpi/bin/apply-display auto 2>/dev/null || true
rm -rf /opt/couchpi
echo "Removed. Your settings remain in /etc/couchpi; the 'couchpi' user was kept."
echo "Reboot to return to the console."
