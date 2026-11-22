#!/bin/bash
# One-line installer:
#   curl -fsSL https://raw.githubusercontent.com/thatguy-za/couchpi/main/get.sh | sudo bash
set -euo pipefail

REPO="${COUCHPI_REPO:-thatguy-za/couchpi}"
[ "$(id -u)" -eq 0 ] || { echo "Please run as root:  curl -fsSL https://raw.githubusercontent.com/$REPO/main/get.sh | sudo bash"; exit 1; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "==> Downloading CouchPi"
curl -fsSL "https://github.com/$REPO/archive/refs/heads/main.tar.gz" | tar xz -C "$tmp"
sha="$(curl -fsSL -H 'Accept: application/vnd.github.sha' "https://api.github.com/repos/$REPO/commits/main" || true)"

COUCHPI_REPO="$REPO" COUCHPI_COMMIT="$sha" bash "$tmp"/*/install.sh

if [ "${COUCHPI_NO_REBOOT:-0}" != "1" ]; then
    echo
    echo "Rebooting into CouchPi in 10 seconds (press Ctrl+C to cancel)..."
    sleep 10
    reboot
fi
