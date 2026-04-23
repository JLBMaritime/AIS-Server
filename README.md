# JLBMaritime AIS-Server

Central aggregation server for a distributed fleet of AIS receiver nodes.
Receives NMEA 0183 AIS sentences (`!AIVDM` / `!AIVDO`) over TCP from multiple
remote nodes (connected via a [Tailscale](https://tailscale.com) tailnet),
**deduplicates** them, **re-orders them chronologically**, and forwards the
clean stream to one or more configurable endpoints (OpenCPN, chart plotters,
custom consumers, etc.).

Designed to run on a **Raspberry Pi 4B (4 GB) + Raspberry Pi OS Lite**.

> Repository: <https://github.com/JLBMaritime/AIS-Server>

---

## ✨ Features

- 🌐 **Multi-node ingest** — accepts TCP connections from any number of AIS nodes on port `10110`
- 🔒 **Private by default** — Tailscale (WireGuard) tailnet, firewall locked to `100.64.0.0/10`
- 🧹 **Dedup** — 30-second TTL cache, SHA-1 of canonicalised sentence, O(1) lookups
- ⏱ **Chronological re-order** — 2 s bounded min-heap jitter buffer, guarantees **< 10 s** end-to-end latency
- 📤 **Configurable endpoints** — TCP forwarders with per-endpoint queues, rate-limit, enable/disable, live test
- 🖥 **Web UI** — Flask + Socket.IO, 8 pages, JLBMaritime theme
- ⌨️ **CLI** (`aisctl`) — full parity with the web UI for headless operation
- 📡 **Wi-Fi configuration** — scan / connect / forget via NetworkManager (`nmcli`)
- ♻️ **Self-healing** — every task supervised, `systemd` watchdog, auto-restart
- 💾 **Backup & restore** — signed config backups, one-click download
- 🔐 **Forced password change on first login** — default creds only valid until first use

---

## 🧭 Architecture

```
                ┌──────────── Tailnet (WireGuard) ────────────┐
   AIS Node 1 ──┤                                             │
   AIS Node 2 ──┼──► RPi 4B "ais-server" (100.x.x.x)          │
   AIS Node N ──┤         │                                   │
                │         ├─ Ingest     (TCP :10110)          │
                │         ├─ Dedup      (TTLCache 30 s)       │
                │         ├─ Re-order   (min-heap, 2 s hold)  │
                │         ├─ Forwarder  (per-endpoint queues) │
                │         ├─ Web UI     (Flask + Socket.IO)   │
                │         └─ CLI        (aisctl)              │
                └─────────────────────────────────────────────┘
```

---

## 🚀 One-Line Install (fresh Raspberry Pi OS Lite)

```bash
curl -fsSL https://raw.githubusercontent.com/JLBMaritime/AIS-Server/main/install.sh | sudo bash
```

The installer is idempotent — re-run it any time to upgrade.

### What it does

1. Verifies the OS (warns on non-Pi-OS).
2. Installs APT deps: `git python3-venv python3-pip sqlite3 network-manager logrotate curl ufw`.
3. Installs **Tailscale** (`curl -fsSL https://tailscale.com/install.sh | sh`).
4. Clones `https://github.com/JLBMaritime/AIS-Server` to `/opt/ais-server`.
5. Creates a Python venv and installs `requirements.txt`.
6. Writes `/etc/ais-server/config.yaml` (from the example) and seeds the user
   `JLBMaritime` / `Admin` (must be changed on first login).
7. Installs & enables the `ais-server.service` systemd unit.
8. Configures `logrotate` for `/var/log/ais-server/`.
9. Locks UFW: allows ports `22, 80, 443, 10110` **only** from the tailnet
   subnet `100.64.0.0/10`.
10. Prints the final summary with tailnet IP and web URL.

---

## 🪜 Manual Step-by-Step Install

If you prefer to install by hand (useful for development):

```bash
# --- 1. Update OS ------------------------------------------------------------
sudo apt update && sudo apt full-upgrade -y

# --- 2. Install base packages ------------------------------------------------
sudo apt install -y git python3 python3-venv python3-pip sqlite3 \
                    network-manager logrotate curl ufw

# --- 3. Install Tailscale ----------------------------------------------------
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh --hostname=ais-server --advertise-tags=tag:ais-server
# Follow the URL printed, sign in, done.
# Note the server's tailnet IP:
tailscale ip -4

# --- 4. Clone the repository -------------------------------------------------
sudo mkdir -p /opt
sudo git clone https://github.com/JLBMaritime/AIS-Server.git /opt/ais-server
cd /opt/ais-server

# --- 5. Python environment ---------------------------------------------------
sudo python3 -m venv .venv
sudo .venv/bin/pip install --upgrade pip wheel
sudo .venv/bin/pip install -r requirements.txt

# --- 6. Configuration --------------------------------------------------------
sudo mkdir -p /etc/ais-server /var/log/ais-server /var/lib/ais-server
sudo cp config/ais-server.example.yaml /etc/ais-server/config.yaml
sudo cp config/logrotate.conf /etc/logrotate.d/ais-server

# --- 7. Seed database (creates default JLBMaritime / Admin user) -------------
sudo /opt/ais-server/.venv/bin/python -m ais_server.cli init

# --- 8. Install systemd unit -------------------------------------------------
sudo cp systemd/ais-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ais-server

# --- 9. Firewall -------------------------------------------------------------
sudo ufw default deny incoming
sudo ufw allow from 100.64.0.0/10 to any port 22 proto tcp
sudo ufw allow from 100.64.0.0/10 to any port 80 proto tcp
sudo ufw allow from 100.64.0.0/10 to any port 443 proto tcp
sudo ufw allow from 100.64.0.0/10 to any port 10110 proto tcp
sudo ufw --force enable

# --- 10. Check ---------------------------------------------------------------
sudo systemctl status ais-server
aisctl status
```

Open `http://<tailnet-ip>/` in a browser (or `http://ais-server/` with
Tailscale MagicDNS enabled).

---

## 🌐 Tailscale — Recommended Configuration

### Server

```bash
sudo tailscale up \
    --ssh \
    --hostname=ais-server \
    --advertise-tags=tag:ais-server
```

### Nodes (remote AIS Pis)

Generate a **reusable, pre-approved auth key** tagged `tag:ais-node` at
<https://login.tailscale.com/admin/settings/keys> then:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up \
    --authkey=tskey-auth-xxxxxxxxxxxx \
    --hostname=ais-node-<location> \
    --advertise-tags=tag:ais-node
```

### ACLs (Tailscale admin → Access Controls)

This ACL ensures nodes can **only** reach the server's ingest port, admins can
reach everything else, and nodes can never pivot into each other or into
your laptop.

```jsonc
{
  "tagOwners": {
    "tag:ais-server": ["autogroup:admin"],
    "tag:ais-node":   ["autogroup:admin"]
  },
  "acls": [
    { "action": "accept", "src": ["tag:ais-node"],    "dst": ["tag:ais-server:10110"] },
    { "action": "accept", "src": ["autogroup:admin"], "dst": ["tag:ais-server:22,80,443"] }
  ],
  "ssh": [
    { "action": "accept",
      "src":    ["autogroup:admin"],
      "dst":    ["tag:ais-server"],
      "users":  ["root", "JLBMaritime"] }
  ]
}
```

Enable **MagicDNS** so nodes/endpoints use the hostname `ais-server` rather
than the raw 100.x IP.

---

## ⚙️ Configuration Reference

All runtime knobs live in `/etc/ais-server/config.yaml`. See
`config/ais-server.example.yaml` for the authoritative list.

| Section    | Key                      | Default | Purpose |
|------------|--------------------------|---------|---------|
| ingest     | `tcp_port`               | 10110   | Port AIS nodes push to |
| ingest     | `max_clients`            | 64      | Hard cap on simultaneous nodes |
| dedup      | `ttl_seconds`            | 30      | Window inside which duplicates are squashed |
| dedup      | `max_entries`            | 200000  | Hard cap on dedup cache |
| reorder    | `hold_ms`                | 2000    | Jitter-buffer hold (end-to-end <10 s guaranteed) |
| reorder    | `max_queue`              | 50000   | Safety cap |
| forwarder  | `default_protocol`       | tcp     | `udp` / `http` scaffolded for future use |
| web        | `host`                   | 0.0.0.0 |  |
| web        | `port`                   | 80      |  |
| security   | `force_password_change_on_first_login` | true |  |
| logging    | `path`                   | /var/log/ais-server | |
| logging    | `level`                  | INFO    |  |

---

## 🖥 Web UI Pages

| URL | Page | Purpose |
|-----|------|---------|
| `/login` | Login | Default `JLBMaritime` / `Admin` – forced change on first login |
| `/` | Dashboard | Msgs/s, unique MMSI, queue depth, node & endpoint status |
| `/wifi` | Wi-Fi | Scan / connect / forget networks (`nmcli`) |
| `/nodes` | Nodes | Auto-discovered from incoming TCP connections |
| `/data/in` | Incoming Data | Live stream, filter by node, pause, CSV export |
| `/data/out` | Outgoing Data | Per-endpoint live stream |
| `/endpoints` | Endpoints | Add / edit / delete / enable / disable / test |
| `/system` | System | Password change, backup/restore, reboot, service restart |

---

## ⌨️ CLI — `aisctl`

```bash
aisctl status                          # msgs/sec, queue depth, node count
aisctl nodes list
aisctl endpoint list
aisctl endpoint add    --name OpenCPN --type tcp --host 100.64.1.5 --port 10110
aisctl endpoint test   <id>
aisctl endpoint enable|disable <id>
aisctl wifi scan
aisctl wifi connect   <ssid> --password <pw>
aisctl wifi forget    <ssid>
aisctl passwd
aisctl backup         /tmp/backup.tar.gz
aisctl restore        /tmp/backup.tar.gz
aisctl logs -f
```

Run `aisctl --help` or `aisctl <subcommand> --help` for details.

---

## 🛠 Service Management

```bash
sudo systemctl status ais-server
sudo systemctl restart ais-server
sudo systemctl stop ais-server
sudo journalctl -u ais-server -f          # live logs
```

Log files: `/var/log/ais-server/ais-server.log` (rotated daily, 14 days kept).

---

## 🧪 Testing

```bash
cd /opt/ais-server
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -v
```

Replay a captured NMEA file against the server:

```bash
.venv/bin/python scripts/replay_nmea.py tests/fixtures/sample.nmea \
    --host 127.0.0.1 --port 10110 --rate 20
```

Stress test (simulates N nodes × M msg/s):

```bash
.venv/bin/python scripts/stress_test.py --nodes 10 --rate 50 --duration 60
```

---

## 🧯 Troubleshooting

| Symptom | Check |
|---------|-------|
| Node cannot connect to `10110` | `tailscale status` on both sides; `sudo ufw status` on the server |
| No messages on dashboard | Check the Nodes page; `journalctl -u ais-server -f` for ingest errors |
| High queue depth | Lower `reorder.hold_ms` or confirm endpoints are draining |
| Web UI unreachable | `sudo systemctl status ais-server`; check port 80 not claimed by another service |
| Forgot password | `sudo aisctl passwd --reset JLBMaritime` |

---

## 🔁 Uninstall

```bash
sudo /opt/ais-server/uninstall.sh
```

Removes the service, venv, and `/opt/ais-server`. Keeps `/etc/ais-server/`
and `/var/lib/ais-server/` so you don't lose config/backups accidentally;
pass `--purge` to remove them too.

---

## 📝 License

MIT — see `LICENSE`.

---

**JLBMaritime** — part of the integrated maritime monitoring & tracking
solution.
