#!/bin/bash
# CouchPi installer for Raspberry Pi OS Lite (64-bit).
#   From a clone:  sudo ./install.sh
#   One-liner:     see README (get.sh downloads this repo and runs this script)
# Env: COUCHPI_UPDATE=1 (self-update: skip app installs), COUCHPI_REPO=owner/name,
#      COUCHPI_COMMIT=<sha>, COUCHPI_SKIP_APPS=1
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Please run with sudo:  sudo ./install.sh"; exit 1; }
[ "$(uname -m)" = "aarch64" ] || echo "Warning: this is not a 64-bit OS (aarch64); Jellyfin/Chiaki won't install."

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST=/opt/couchpi
CONF=/etc/couchpi
UPDATE="${COUCHPI_UPDATE:-0}"

echo "==> Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y cage python3-pygame python3-evdev cec-utils sudo flatpak \
    unattended-upgrades curl ca-certificates
command -v nmcli >/dev/null || echo "Note: NetworkManager not found; Wi-Fi settings need it (default on Pi OS Bookworm+)."

echo "==> Creating the 'couchpi' user"
id couchpi >/dev/null 2>&1 || useradd -m -s /usr/sbin/nologin couchpi
usermod -aG video,render,input,audio couchpi

echo "==> Copying files to $DEST"
mkdir -p "$DEST/apps.d" "$CONF" /var/lib/couchpi
cp -r "$SRC"/launcher "$SRC"/cec "$SRC"/bin "$DEST"/
cp -r "$SRC"/apps.d/. "$DEST/apps.d/"      # your own app files in apps.d are left alone
chmod +x "$DEST"/bin/* "$DEST"/launcher/launcher.py "$DEST"/cec/cecd.py
ln -sf "$DEST/bin/couchpi-apps" /usr/local/bin/couchpi-apps
[ -n "${COUCHPI_REPO:-}" ] && echo "$COUCHPI_REPO" > "$DEST/repo"
[ -n "${COUCHPI_COMMIT:-}" ] && echo "$COUCHPI_COMMIT" > "$DEST/.commit"
[ -f "$CONF/config.json" ] || echo '{}' > "$CONF/config.json"
chown -R couchpi:couchpi "$CONF"

echo "==> Permissions and services"
cat > /etc/sudoers.d/couchpi <<EOF
couchpi ALL=(root) NOPASSWD: $DEST/bin/apply-display *, $DEST/bin/couchpi-sys *, $DEST/bin/couchpi-update *, /usr/bin/systemctl reboot
EOF
chmod 440 /etc/sudoers.d/couchpi

cat > /etc/pam.d/couchpi <<EOF
auth       required     pam_unix.so nullok
account    required     pam_unix.so
session    required     pam_unix.so
session    required     pam_systemd.so
EOF

echo 'd /run/couchpi 0755 couchpi couchpi -' > /etc/tmpfiles.d/couchpi.conf
systemd-tmpfiles --create /etc/tmpfiles.d/couchpi.conf

cp "$SRC"/systemd/* /etc/systemd/system/
systemctl daemon-reload
systemctl set-default graphical.target
systemctl enable couchpi.service couchpi-cec.service couchpi-update.timer
systemctl start couchpi-update.timer

if [ "$UPDATE" != "1" ] && [ "${COUCHPI_SKIP_APPS:-0}" != "1" ]; then
    echo "==> Installing apps (Jellyfin Desktop + Chiaki-ng; this can take a few minutes)"
    "$DEST/bin/couchpi-apps" install --all
    "$DEST/bin/couchpi-update" check || true
fi

echo
echo "CouchPi is installed."
[ "$UPDATE" = "1" ] || echo "Reboot to start it:  sudo reboot"
echo "Add more apps later with:  sudo couchpi-apps add myapp.toml"
