#!/usr/bin/env bash
# ============================================================================
# JLBMaritime AIS-Server — installer
# Target: Raspberry Pi 4B, Raspberry Pi OS Lite (Bookworm 64-bit), headless.
# Idempotent: safe to re-run to upgrade an existing install.
# ============================================================================
set -euo pipefail

INSTALL_DIR="/opt/ais-server"
CONFIG_DIR="/etc/ais-server"
DATA_DIR="/var/lib/ais-server"
LOG_DIR="/var/log/ais-server"
SERVICE_NAME="ais-server"

REPO_URL="${REPO_URL:-https://github.com/JLBMaritime/AIS-Server.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"

INSTALL_TAILSCALE="${INSTALL_TAILSCALE:-1}"   # set to 0 to skip

log()   { echo -e "\033[1;34m[install]\033[0m $*"; }
ok()    { echo -e "\033[1;32m[ok]\033[0m $*"; }
warn()  { echo -e "\033[1;33m[warn]\033[0m $*"; }
die()   { echo -e "\033[1;31m[error]\033[0m $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root (sudo bash install.sh)"

# ----------------------------------------------------------------------------
# 1. APT packages
# ----------------------------------------------------------------------------
log "Updating apt and installing prerequisites…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
    git curl ca-certificates rsync \
    python3 python3-venv python3-pip python3-dev \
    build-essential libffi-dev \
    network-manager iw wireless-tools \
    sqlite3

# ----------------------------------------------------------------------------
# 2. Persistent + bounded systemd journal
# ----------------------------------------------------------------------------
# Default RPi OS Lite keeps the journal in /run (volatile) – every boot the
# previous logs are gone, which is why the original "24-hour freeze" was so
# hard to diagnose.  Make it persistent **and** cap its on-disk size, so a
# long-running install with chatty nodes can't quietly fill the SD card.
JOURNAL_DROPIN_DIR=/etc/systemd/journald.conf.d
JOURNAL_DROPIN=${JOURNAL_DROPIN_DIR}/ais-server.conf
JOURNAL_CHANGED=0
if [[ ! -d /var/log/journal ]]; then
  log "Enabling persistent systemd journal in /var/log/journal…"
  mkdir -p /var/log/journal
  systemd-tmpfiles --create --prefix /var/log/journal
  JOURNAL_CHANGED=1
else
  ok  "Persistent journal already configured"
fi

# Install / refresh our size-cap drop-in.  Compare against the source first
# so we don't pointlessly restart journald on every re-run.
install -d -m 0755 "${JOURNAL_DROPIN_DIR}"
SRC_DROPIN="$(dirname "$0")/config/journald-ais-server.conf"
[[ -f "${INSTALL_DIR}/config/journald-ais-server.conf" ]] \
  && SRC_DROPIN="${INSTALL_DIR}/config/journald-ais-server.conf"
if [[ -f "${SRC_DROPIN}" ]]; then
  if ! cmp -s "${SRC_DROPIN}" "${JOURNAL_DROPIN}"; then
    log "Installing journald size-cap drop-in -> ${JOURNAL_DROPIN}"
    install -m 0644 "${SRC_DROPIN}" "${JOURNAL_DROPIN}"
    JOURNAL_CHANGED=1
  else
    ok  "journald size-cap drop-in already up-to-date"
  fi
else
  warn "${SRC_DROPIN} not found – skipping journald size-cap drop-in"
fi

if [[ "${JOURNAL_CHANGED}" == "1" ]]; then
  systemctl restart systemd-journald || warn "journald restart failed (non-fatal)"
fi

# ----------------------------------------------------------------------------
# 3. Tailscale (optional)
# ----------------------------------------------------------------------------
if [[ "${INSTALL_TAILSCALE}" == "1" ]]; then
  if ! command -v tailscale >/dev/null 2>&1; then
    log "Installing Tailscale…"
    curl -fsSL https://tailscale.com/install.sh | sh
  else
    ok "Tailscale already installed"
  fi
  systemctl enable --now tailscaled || warn "tailscaled not enabled (will need manual 'sudo tailscale up')"
fi

# ----------------------------------------------------------------------------
# 4. Fetch / update source
# ----------------------------------------------------------------------------
if [[ -d "${INSTALL_DIR}/.git" ]]; then
  log "Updating existing checkout in ${INSTALL_DIR}…"
  git -C "${INSTALL_DIR}" fetch --all --prune
  git -C "${INSTALL_DIR}" checkout "${REPO_BRANCH}"
  git -C "${INSTALL_DIR}" pull --ff-only
elif [[ -f "$(dirname "$0")/pyproject.toml" ]]; then
  # Installing from a local checkout (the one containing this script).
  log "Copying local checkout to ${INSTALL_DIR}…"
  mkdir -p "${INSTALL_DIR}"
  rsync -a --delete --exclude .venv --exclude __pycache__ \
        --exclude '*.pyc' "$(dirname "$0")/" "${INSTALL_DIR}/"
else
  log "Cloning ${REPO_URL}@${REPO_BRANCH} -> ${INSTALL_DIR}…"
  git clone --branch "${REPO_BRANCH}" "${REPO_URL}" "${INSTALL_DIR}"
fi

# ----------------------------------------------------------------------------
# 5. Python venv + deps
# ----------------------------------------------------------------------------
if [[ ! -d "${INSTALL_DIR}/.venv" ]]; then
  log "Creating Python venv…"
  python3 -m venv "${INSTALL_DIR}/.venv"
fi
"${INSTALL_DIR}/.venv/bin/pip" install --upgrade pip wheel
# Remove eventlet if a previous install pulled it in – it's incompatible with
# Python 3.13 (RPi OS Trixie) and has been replaced by async_mode="threading".
"${INSTALL_DIR}/.venv/bin/pip" uninstall -y eventlet >/dev/null 2>&1 || true
"${INSTALL_DIR}/.venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"
"${INSTALL_DIR}/.venv/bin/pip" install -e "${INSTALL_DIR}"

# ----------------------------------------------------------------------------
# 6. Directories + config
# ----------------------------------------------------------------------------
install -d -m 0755 "${CONFIG_DIR}"
install -d -m 0750 "${DATA_DIR}"
install -d -m 0755 "${LOG_DIR}"

if [[ ! -f "${CONFIG_DIR}/config.yaml" ]]; then
  log "Seeding default config -> ${CONFIG_DIR}/config.yaml"
  install -m 0644 "${INSTALL_DIR}/config/ais-server.example.yaml" \
          "${CONFIG_DIR}/config.yaml"
else
  ok  "Keeping existing ${CONFIG_DIR}/config.yaml"
fi

# Logs are now journald-managed.  Remove any legacy logrotate.d entry from a
# previous version of this installer – its presence triggered the
# "three-rotators-one-file" stale-FD bug that caused 24h freezes.
if [[ -f /etc/logrotate.d/ais-server ]]; then
  log "Removing obsolete /etc/logrotate.d/ais-server (journald handles rotation now)"
  rm -f /etc/logrotate.d/ais-server
fi

# ----------------------------------------------------------------------------
# 7. Wi-Fi power-save fix (Pi 4B brcmfmac freeze workaround)
# ----------------------------------------------------------------------------
log "Disabling Wi-Fi power-save (Pi 4B brcmfmac freeze workaround)…"
install -d -m 0755 /etc/NetworkManager/conf.d
install -m 0644 "${INSTALL_DIR}/config/wifi-powersave-off.conf" \
        /etc/NetworkManager/conf.d/wifi-powersave-off.conf
install -m 0644 "${INSTALL_DIR}/systemd/ais-wifi-powersave-off.service" \
        /etc/systemd/system/ais-wifi-powersave-off.service
systemctl daemon-reload
systemctl enable --now ais-wifi-powersave-off.service || true
# Reload NetworkManager so the drop-in takes effect immediately.
systemctl reload-or-restart NetworkManager || warn "NetworkManager reload failed (non-fatal)"
# And apply right now to the running radio, in case the reload above hasn't
# rolled the existing connection.
iw dev wlan0 set power_save off 2>/dev/null \
  || iwconfig wlan0 power off 2>/dev/null \
  || true

# ----------------------------------------------------------------------------
# 8. CLI shim
# ----------------------------------------------------------------------------
cat > /usr/local/bin/aisctl <<EOF
#!/usr/bin/env bash
exec ${INSTALL_DIR}/.venv/bin/aisctl "\$@"
EOF
chmod +x /usr/local/bin/aisctl

# ----------------------------------------------------------------------------
# 9. systemd service
# ----------------------------------------------------------------------------
install -m 0644 "${INSTALL_DIR}/systemd/${SERVICE_NAME}.service" \
        "/etc/systemd/system/${SERVICE_NAME}.service"

# Allow binding to port 80 without running as root:
if [[ -x "${INSTALL_DIR}/.venv/bin/python" ]]; then
  setcap 'cap_net_bind_service=+ep' \
         "$(readlink -f "${INSTALL_DIR}/.venv/bin/python")" || true
fi

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

# ----------------------------------------------------------------------------
# 10. Done
# ----------------------------------------------------------------------------
IPADDR=$(hostname -I | awk '{print $1}')
echo
ok  "AIS-Server installed."
echo "  Web UI:   http://${IPADDR}/"
echo "  Login:    JLBMaritime / Admin   (you will be forced to change it)"
echo "  CLI:      aisctl status         (run 'aisctl --help')"
echo "  Logs:     journalctl -u ${SERVICE_NAME} -f"
echo "  Config:   ${CONFIG_DIR}/config.yaml"
if [[ "${INSTALL_TAILSCALE}" == "1" ]]; then
  echo
  echo "  Tailscale: run  sudo tailscale up --ssh  (first time only)."
fi
