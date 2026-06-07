/* JLBMaritime AIS-Server – front-end glue.
 * Every page calls one entrypoint: ais.<page>() which wires that page's
 * DOM to the /api/* JSON endpoints and (where relevant) the /live Socket.IO
 * namespace.
 */
(function () {
  /* Belt-and-braces cache-buster: iOS WebKit (Safari + "Chrome" on iPhone)
   * applies heuristic freshness to any same-origin GET that lacks an
   * explicit Cache-Control header and serves stale JSON for ~30 s.  The
   * server now sets `Cache-Control: no-store` on every /api/* response,
   * but we also append a `_=<ms>` query param to GETs as a second line of
   * defence – the URL changes every call so even an over-eager cache can't
   * match a previous entry. */
  const api = async (path, opts = {}) => {
    const method = (opts.method || 'GET').toUpperCase();
    let url = '/api' + path;
    if (method === 'GET') {
      url += (url.includes('?') ? '&' : '?') + '_=' + Date.now();
    }
    const res = await fetch(url, {
      headers: {
        'Content-Type': 'application/json',
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache',
      },
      credentials: 'same-origin',
      cache: 'no-store',
      ...opts,
    });
    const ct = res.headers.get('content-type') || '';
    if (ct.includes('application/json')) return res.json();
    return res.text();
  };


  const fmtTime = ts => {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString();
  };
  const fmtUptime = s => {
    if (!s) return '0s';
    const d = Math.floor(s / 86400); s %= 86400;
    const h = Math.floor(s / 3600);  s %= 3600;
    const m = Math.floor(s / 60);    s %= 60;
    return (d ? d + 'd ' : '') + (h ? h + 'h ' : '') + (m ? m + 'm ' : '') + s + 's';
  };
  const fmtBytes = n => {
    if (n == null) return '—';
    if (n < 1024) return n + 'B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + 'KB';
    if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(2) + 'MB';
    return (n / 1024 / 1024 / 1024).toFixed(2) + 'GB';
  };
  const fmtRel = ts => {
    if (!ts) return '—';
    const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
    if (s < 60)  return s + 's ago';
    if (s < 3600) return Math.floor(s / 60) + 'm ago';
    return Math.floor(s / 3600) + 'h ago';
  };
  const el = sel => document.querySelector(sel);
  const setText = (sel, v) => { const e = el(sel); if (e) e.textContent = v; };

  // ------------------- SYSTEM-INFO HELPERS -------------------
  // Banner colours come from the diskcheck "state" field: ok | warn | low.
  // We re-use these on both the Dashboard (top-of-page) and System pages.
  function renderDiskBanner(targetSel, info) {
    const t = el(targetSel);
    if (!t) return;
    const state = (info && info.state) || 'ok';
    const disk = (info && info.disk) || {};
    if (state === 'ok' || disk.free == null) { t.innerHTML = ''; return; }
    const cls = state === 'low' ? 'banner err' : 'banner warn';
    const msg = state === 'low'
      ? `LOW DISK SPACE: only ${fmtBytes(disk.free)} free on ${disk.path}.`
      : `Disk space getting tight – ${fmtBytes(disk.free)} free on ${disk.path}.`;
    t.innerHTML = `<div class="card ${cls}">${msg}</div>`;
  }

  async function refreshDashboardBanner() {
    if (!el('#disk-banner')) return;
    try {
      const info = await api('/system/info');
      renderDiskBanner('#disk-banner', info);
    } catch (e) { /* ignore – banner is best-effort */ }
  }

  async function refreshSystemInfo() {
    let info;
    try { info = await api('/system/info'); }
    catch (e) { console.warn(e); return; }
    renderDiskBanner('#sys-banner', info);
    const svc = info.service || {};
    const disk = info.disk || {};
    const jrn = info.journal || {};
    const dbi = info.db || {};
    const mem = info.memory || {};
    const proc = info.process || {};
    setText('#sys-uptime', fmtUptime(svc.uptime_seconds));
    setText('#sys-disk', disk.free == null ? '—'
      : `${fmtBytes(disk.free)} free / ${fmtBytes(disk.total)} `
        + `(${disk.percent}% used, ${disk.path})`);
    setText('#sys-journal', jrn.bytes == null ? '—'
      : `${fmtBytes(jrn.bytes)} (${jrn.path})`);
    setText('#sys-db', dbi.db_bytes == null ? '—'
      : `${fmtBytes(dbi.db_bytes)} DB + ${fmtBytes(dbi.wal_bytes)} WAL`
        + (dbi.path ? ` (${dbi.path})` : ''));
    setText('#sys-mem', mem.percent == null ? '—'
      : `${fmtBytes(mem.used)} / ${fmtBytes(mem.total)} (${mem.percent}% used)`);
    const la = info.loadavg ? info.loadavg.join(' / ') : '—';
    setText('#sys-cpu', `${info.cpu_percent == null ? '—' : info.cpu_percent + '%'} `
                       + `(load ${la})`);
    setText('#sys-proc', proc.rss == null ? '—'
      : `RSS ${fmtBytes(proc.rss)}, ${proc.threads} threads`
        + (proc.fds != null ? `, ${proc.fds} fds` : ''));
    setText('#sys-conn', `${svc.nodes_connected}/${svc.nodes_total} nodes, `
                        + `${svc.endpoints_total} endpoints`);
  }

  async function refreshBackupsList() {
    const body = el('#backups-tbl tbody');
    if (!body) return;
    try {
      const list = await api('/system/backups');
      body.innerHTML = list.map(b =>
        `<tr><td>${b.name}</td><td>${fmtBytes(b.size)}</td>
          <td>${new Date(b.mtime * 1000).toLocaleString()}</td></tr>`).join('')
        || '<tr><td colspan="3" class="muted">No on-disk backups yet.</td></tr>';
    } catch (e) { /* ignore */ }
  }

  // ------------------- DASHBOARD -------------------
  async function refreshStatus() {
    try {
      const s = await api('/status');
      const p = s.pipeline;
      setText('#msgs_per_sec',   p.msgs_per_sec);
      setText('#unique_mmsi',    p.unique_mmsi);
      setText('#nodes_connected', p.nodes_connected + ' / ' + p.nodes);
      setText('#uptime',         fmtUptime(p.uptime_seconds));
      setText('#dedup_rate',     (p.dedup.dedup_rate * 100).toFixed(1) + '%');
      setText('#queue_size',     p.reorder.queue_size);

      const epBody = el('#ep-tbl tbody');
      if (epBody) {
        epBody.innerHTML = s.endpoints.map(e =>
          `<tr><td>${e.name}</td><td>${e.host}:${e.port}</td>
             <td>${e.connected
                ? '<span class="pill ok">UP</span>'
                : '<span class="pill err">DOWN</span>'}</td>
             <td>${e.sent}</td><td>${e.queue_depth}</td>
             <td class="muted small">${e.last_error || '—'}</td></tr>`).join('') ||
          '<tr><td colspan="6" class="muted">No endpoints configured.</td></tr>';
      }

      const ndBody = el('#node-tbl tbody');
      if (ndBody) {
        ndBody.innerHTML = s.nodes.map(n =>
          `<tr><td>${n.peer}</td>
             <td>${n.connected
                ? '<span class="pill ok">ON</span>'
                : '<span class="pill warn">OFF</span>'}</td>
             <td>${n.messages}</td><td>${n.invalid}</td>
             <td>${fmtRel(n.last_seen)}</td></tr>`).join('') ||
          '<tr><td colspan="5" class="muted">No nodes connected.</td></tr>';
      }
    } catch (e) { console.warn(e); }
  }

  // ------------------- NODES -------------------
  async function refreshNodes() {
    const s = await api('/status');
    const body = el('#nodes-tbl tbody');
    body.innerHTML = s.nodes.map(n => {
      const label = n.source_id
        ? `${n.source_id} <span class="muted small">(${n.host})</span>`
        : n.host;
      const state = n.connected
        ? `<span class="pill ok">ON</span>`
        : `<span class="pill warn">OFF</span>`;
      const sessions = `${n.active_sessions || 0} / ${n.sessions || 0}`;
      return `<tr>
         <td>${label}</td>
         <td>${n.host}</td>
         <td>${state}</td>
         <td title="active / total">${sessions}</td>
         <td>${n.messages}</td>
         <td>${n.invalid}</td>
         <td>${fmtBytes(n.bytes_rx)}</td>
         <td>${fmtRel(n.first_seen)}</td>
         <td>${fmtRel(n.last_seen)}</td>
       </tr>`;
    }).join('') ||
    '<tr><td colspan="9" class="muted">No nodes connected.</td></tr>';
  }

  // ------------------- WI-FI -------------------
  async function refreshWifiCurrent() {
    const c = await api('/wifi/current');
    el('#wifi-current').textContent = c && c.ssid
      ? `${c.ssid}  —  ${c.ip || 'no IP'}  (${c.state || '—'})`
      : 'Not connected.';
  }
  async function refreshWifiScan() {
    const nets = await api('/wifi/scan');
    const body = el('#wifi-scan tbody');
    body.innerHTML = nets.map(n =>
      `<tr><td>${n.ssid}</td><td>${n.signal}%</td><td>${n.security}</td>
         <td><button class="small" onclick="ais.wifiConnect('${n.ssid.replace(/'/g,"\\'")}', '${n.security}')">Connect</button></td></tr>`
    ).join('') || '<tr><td colspan="4" class="muted">No networks found.</td></tr>';
  }
  async function refreshWifiSaved() {
    const nets = await api('/wifi/saved');
    const body = el('#wifi-saved tbody');
    body.innerHTML = nets.map(n =>
      `<tr><td>${n.ssid}</td>
        <td><button class="small danger" onclick="ais.wifiForget('${n.ssid.replace(/'/g,"\\'")}')">Forget</button></td></tr>`
    ).join('') || '<tr><td colspan="2" class="muted">None.</td></tr>';
  }

  // ------------------- ENDPOINTS -------------------
  /* The Protocol column shows "udp (bcast)" when an endpoint is UDP with
   * broadcast enabled.  We keep that flag inside the Protocol cell rather
   * than adding a new column so the table layout doesn't change. */
  async function refreshEndpoints() {
    const list = await api('/endpoints');
    const body = el('#ep-tbl tbody');
    body.innerHTML = list.map(e => {
      const proto = e.protocol === 'udp' && e.broadcast
        ? 'udp <span class="muted small">(bcast)</span>'
        : e.protocol;
      return `<tr><td>${e.name}</td><td>${proto}</td>
         <td>${e.host}</td><td>${e.port}</td>
         <td>${e.enabled ? 'yes' : 'no'}</td>
         <td>
           <button class="small" onclick="ais.epEdit(${e.id})">Edit</button>
           <button class="small" onclick="ais.epTest(${e.id})">Test</button>
           <button class="small" onclick="ais.epToggle(${e.id}, ${e.enabled ? 0 : 1})">${e.enabled ? 'Disable' : 'Enable'}</button>
           <button class="small danger" onclick="ais.epDelete(${e.id})">Delete</button>
         </td></tr>`;
    }).join('') || '<tr><td colspan="6" class="muted">No endpoints yet.</td></tr>';
  }

  /* Show / hide the "Broadcast" checkbox depending on whether the user has
   * selected the UDP protocol.  When the field becomes hidden we also
   * untick it so a stale value can't be submitted accidentally. */
  function syncBroadcastVisibility(form) {
    const wrap = el('#bcast-wrap');
    if (!wrap) return;
    const isUdp = form.protocol && form.protocol.value === 'udp';
    wrap.hidden = !isUdp;
    if (!isUdp && form.broadcast) form.broadcast.checked = false;
  }


  // ------------------- LIVE STREAMS -------------------
  /* Both data pages share this implementation; `kind` is "incoming" or
   * "outgoing".  The dropdown is populated from /api/status (nodes for
   * incoming, endpoints for outgoing) and the filter is applied at
   * *append* time – so flipping the dropdown takes effect on the next
   * sentence without re-subscribing or re-priming.
   *
   * Payload-vs-status matching:
   *   incoming  →  payload.peer is "ip[:port]"   status.nodes[].peer    is "ip"
   *   outgoing  →  payload.endpoint is the name  status.endpoints[].name is the name
   * For incoming we therefore split on ":" and compare the host part so
   * we don't have to care about the ephemeral source port.
   */
  function startStream(kind) {
    const pre        = el('#stream-' + kind);
    const isIncoming = (kind === 'incoming');
    const selectSel  = isIncoming ? '#filter-node' : '#filter-endpoint';
    const select     = el(selectSel);
    const allLabel   = isIncoming ? 'All nodes' : 'All endpoints';
    let paused = false;

    el('#toggle-pause').addEventListener('click', (ev) => {
      paused = !paused;
      ev.target.textContent = paused ? 'Resume' : 'Pause';
    });

    // ----- dropdown population (initial + 5 s refresh) ------------------
    let lastOptionsKey = '';
    async function refreshFilterOptions() {
      let entries = [];
      try {
        const s = await api('/status');
        if (isIncoming) {
          // De-dup by peer (IP); merge source_id label when known.
          const byPeer = new Map();
          (s.nodes || []).forEach(n => {
            if (!n.peer) return;
            const prev = byPeer.get(n.peer);
            const label = n.source_id
              ? `${n.source_id} (${n.peer})` : n.peer;
            // Prefer the one with a connected session if there are dupes.
            if (!prev || (n.connected && !prev.connected)) {
              byPeer.set(n.peer, { value: n.peer, label, connected: !!n.connected });
            }
          });
          entries = Array.from(byPeer.values())
            .sort((a, b) => a.label.localeCompare(b.label));
        } else {
          entries = (s.endpoints || [])
            .filter(e => e && e.name)
            .map(e => ({ value: e.name,
                         label: `${e.name} (${e.host}:${e.port})`,
                         connected: !!e.connected }))
            .sort((a, b) => a.label.localeCompare(b.label));
        }
      } catch (e) { /* ignore – we'll retry on next tick */ }

      // Only redraw when the set actually changed, so the dropdown
      // doesn't flicker / collapse while the user is choosing.
      const key = entries.map(e => e.value).join('|');
      if (key === lastOptionsKey) return;
      lastOptionsKey = key;

      const keep = select.value;
      select.innerHTML =
        `<option value="">${allLabel}</option>` +
        entries.map(e => `<option value="${e.value}">${e.label}</option>`)
               .join('');
      // Preserve the user's selection across refreshes when possible.
      if (keep && entries.some(e => e.value === keep)) {
        select.value = keep;
      } else {
        select.value = '';
      }
    }
    refreshFilterOptions();
    setInterval(refreshFilterOptions, 5000);

    // ----- filter predicate (read at append time) -----------------------
    function matchesFilter(p) {
      const want = select.value;
      if (!want) return true;
      if (isIncoming) {
        // payload.peer is "ip:port"; status.peer is just "ip"
        const peer = (p.peer || '').split(':')[0];
        return peer === want;
      }
      return (p.endpoint || '') === want;
    }

    // ----- prime + live stream ------------------------------------------
    api('/recent/' + kind).then(items => {
      items.forEach(i => { if (matchesFilter(i)) append(i); });
    });
    const sock = io('/live');
    sock.on(kind, (payload) => {
      if (paused) return;
      if (!matchesFilter(payload)) return;
      append(payload);
    });

    function append(p) {
      const peer = p.peer || p.endpoint || '';
      const line = `[${fmtTime(p.ts)}] ${peer.padEnd(24)} ${p.sentence || ''}\n`;
      pre.textContent += line;
      if (pre.textContent.length > 200000) {
        pre.textContent = pre.textContent.slice(-120000);
      }
      pre.scrollTop = pre.scrollHeight;
    }
  }

  // ------------------- API wrappers -------------------
  const ais = {
    dashboard() {
      refreshStatus();
      refreshDashboardBanner();
      setInterval(refreshStatus, 2000);
      // Disk-state changes slowly – polling every 30 s is plenty.
      setInterval(refreshDashboardBanner, 30000);
    },
    nodes()     { refreshNodes();   setInterval(refreshNodes,   2000); },
    wifi() {
      refreshWifiCurrent(); refreshWifiScan(); refreshWifiSaved();
      setInterval(refreshWifiCurrent, 5000);
    },
    wifiScan()  { refreshWifiScan(); },
    async wifiConnect(ssid, security) {
      let password = '';
      if (security && security !== 'Open' && security !== '--')
        password = prompt('Password for "' + ssid + '"') || '';
      const r = await api('/wifi/connect', {
        method: 'POST', body: JSON.stringify({ssid, password}) });
      alert(r.ok ? 'Connected.' : 'Failed: ' + r.message);
      refreshWifiCurrent(); refreshWifiSaved();
    },
    async wifiForget(ssid) {
      if (!confirm('Forget "' + ssid + '"?')) return;
      const r = await api('/wifi/forget', {
        method: 'POST', body: JSON.stringify({ssid}) });
      alert(r.ok ? 'Forgotten.' : 'Failed: ' + r.message);
      refreshWifiSaved();
    },
    endpoints() {
      refreshEndpoints();
      const form = el('#ep-form');
      // Show / hide the "Broadcast" checkbox the moment the user flips
      // the protocol picker (also runs once now so the initial TCP state
      // is reflected correctly when the page loads).
      syncBroadcastVisibility(form);
      form.protocol.addEventListener('change',
                                    () => syncBroadcastVisibility(form));
      form.addEventListener('submit', async (ev) => {
        ev.preventDefault();
        const f = new FormData(form);
        const proto = f.get('protocol');
        const body = {
          name: f.get('name'), host: f.get('host'),
          port: parseInt(f.get('port'), 10),
          protocol: proto,
          enabled: f.get('enabled') === 'on',
          // Only send broadcast for UDP rows; the server stores 0 on
          // TCP/HTTP rows anyway but this keeps the PATCH payload clean.
          broadcast: proto === 'udp' && f.get('broadcast') === 'on',
        };
        const id = f.get('id');
        let r;
        if (id) r = await api('/endpoints/' + id,
                              { method: 'PATCH', body: JSON.stringify(body) });
        else    r = await api('/endpoints',
                              { method: 'POST',  body: JSON.stringify(body) });
        if (!r.ok) alert('Save failed: ' + (r.error || 'unknown'));
        ais.clearForm();
        refreshEndpoints();
      });
    },
    async epEdit(id) {
      const list = await api('/endpoints');
      const ep = list.find(e => e.id === id);
      if (!ep) return;
      const f = el('#ep-form');
      f.id.value = ep.id; f.name.value = ep.name; f.host.value = ep.host;
      f.port.value = ep.port; f.protocol.value = ep.protocol;
      f.enabled.checked = !!ep.enabled;
      // Populate the broadcast checkbox + show/hide its wrap to match the
      // newly-selected protocol.
      if (f.broadcast) f.broadcast.checked = !!ep.broadcast;
      syncBroadcastVisibility(f);
    },
    async epTest(id) {
      const r = await api('/endpoints/' + id + '/test', { method: 'POST' });
      alert((r.ok ? 'OK: ' : 'Failed: ') + r.message);
    },
    async epToggle(id, enabled) {
      await api('/endpoints/' + id,
                { method: 'PATCH', body: JSON.stringify({enabled: !!enabled}) });
      refreshEndpoints();
    },
    async epDelete(id) {
      if (!confirm('Delete this endpoint?')) return;
      await api('/endpoints/' + id, { method: 'DELETE' });
      refreshEndpoints();
    },
    clearForm() {
      const f = el('#ep-form');
      f.reset(); f.id.value = ''; f.enabled.checked = true;
      if (f.broadcast) f.broadcast.checked = false;
      syncBroadcastVisibility(f);
    },
    dataIn()  { startStream('incoming'); },
    dataOut() { startStream('outgoing'); },
    clearStream(kind) { el('#stream-' + kind).textContent = ''; },
    exportCsv(kind) {
      const data = el('#stream-' + kind).textContent;
      const blob = new Blob([data], {type: 'text/plain'});
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'ais-' + kind + '-' + Date.now() + '.log';
      a.click();
    },
    system() {
      el('#pw-form').addEventListener('submit', async (ev) => {
        ev.preventDefault();
        const f = new FormData(ev.target);
        const r = await api('/system/change-password', {
          method: 'POST',
          body: JSON.stringify({
            current_password: f.get('current_password'),
            new_password: f.get('new_password'),
          }),
        });
        alert(r.ok ? 'Password updated.' : 'Failed: ' + (r.error || 'unknown'));
        if (r.ok) ev.target.reset();
      });

      // Host vitals + on-disk backups (refresh every 5 s; 30 s for backups).
      refreshSystemInfo();
      refreshBackupsList();
      setInterval(refreshSystemInfo, 5000);
      setInterval(refreshBackupsList, 30000);

      // "save copy on Pi" checkbox – rewrites the backup link's URL.
      const dl = el('#dl-backup');
      const cb = el('#save-backup');
      if (dl && cb) {
        const base = dl.getAttribute('href');
        const update = () => {
          dl.setAttribute('href', base + (cb.checked ? '?save=1' : ''));
        };
        cb.addEventListener('change', update);
        // Re-list backups shortly after a download, in case ?save=1 was set.
        dl.addEventListener('click', () => {
          setTimeout(refreshBackupsList, 1500);
        });
        update();
      }
    },
    async restart() {
      if (!confirm('Restart the AIS-Server service?')) return;
      const r = await api('/system/restart', { method: 'POST' });
      alert(r.message || 'done');
    },
    async reboot() {
      if (!confirm('Reboot the Raspberry Pi?')) return;
      const r = await api('/system/reboot', { method: 'POST' });
      alert(r.message || 'done');
    },
  };
  window.ais = ais;
})();
