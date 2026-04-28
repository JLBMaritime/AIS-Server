#!/usr/bin/env bash
# Remove the AIS-Server service, binaries, and (optionally) data.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run as root"; exit 1; }

SERVICE_NAME="ais-server"

systemctl disable --now "${SERVICE_NAME}" 2>/dev/null || true
rm -f "/etc/systemd/system/${SERVICE_NAME}.service"

# Wi-Fi power-save fix shipped with the server – remove it too.
systemctl disable --now ais-wifi-powersave-off.service 2>/dev/null || true
rm -f /etc/systemd/system/ais-wifi-powersave-off.service
rm -f /etc/NetworkManager/conf.d/wifi-powersave-off.conf
systemctl reload-or-restart NetworkManager 2>/dev/null || true

systemctl daemon-reload

rm -f "/usr/local/bin/aisctl"
rm -rf "/opt/ais-server"
rm -f  "/etc/logrotate.d/ais-server"   # legacy – removed since v1.x

if [[ "${1:-}" == "--purge" ]]; then
  rm -rf /etc/ais-server /var/lib/ais-server /var/log/ais-server
  echo "Purged all config, data, and logs."
else
  echo "Kept /etc/ais-server, /var/lib/ais-server, /var/log/ais-server."
  echo "Pass --purge to remove them too."
fi
