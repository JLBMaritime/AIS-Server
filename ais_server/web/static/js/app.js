/* JLBMaritime AIS-Server – front-end glue.
 * Every page calls one entrypoint: ais.<page>() which wires that page's
 * DOM to the /api/* JSON endpoints and (where relevant) the /live Socket.IO
 * namespace.
 */
(function () {
  const api = async (path, opts = {}) => {
    const res = await fetch('/api' + path, {
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
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
    if (n < 1024) return n + 'B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + 'KB';
    return (n / 1024 / 1024).toFixed(2) + 'MB';
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
  async function refreshEndpoints() {
    const list = await api('/endpoints');
    const body = el('#ep-tbl tbody');
    body.innerHTML = list.map(e =>
      `<tr><td>${e.name}</td><td>${e.protocol}</td>
         <td>${e.host}</td><td>${e.port}</td>
         <td>${e.enabled ? 'yes' : 'no'}</td>
         <td>
           <button class="small" onclick="ais.epEdit(${e.id})">Edit</button>
           <button class="small" onclick="ais.epTest(${e.id})">Test</button>
           <button class="small" onclick="ais.epToggle(${e.id}, ${e.enabled ? 0 : 1})">${e.enabled ? 'Disable' : 'Enable'}</button>
           <button class="small danger" onclick="ais.epDelete(${e.id})">Delete</button>
         </td></tr>`
    ).join('') || '<tr><td colspan="6" class="muted">No endpoints yet.</td></tr>';
  }

  // ------------------- LIVE STREAMS -------------------
  function startStream(kind) {
    const pre = el('#stream-' + kind);
    let paused = false;
    el('#toggle-pause').addEventListener('click', (ev) => {
      paused = !paused;
      ev.target.textContent = paused ? 'Resume' : 'Pause';
    });

    // Prime with recent buffer.
    api('/recent/' + kind).then(items => {
      items.forEach(i => append(i));
    });

    const sock = io('/live');
    sock.on(kind, (payload) => { if (!paused) append(payload); });

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
    dashboard() { refreshStatus(); setInterval(refreshStatus, 2000); },
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
      form.addEventListener('submit', async (ev) => {
        ev.preventDefault();
        const f = new FormData(form);
        const body = {
          name: f.get('name'), host: f.get('host'),
          port: parseInt(f.get('port'), 10),
          protocol: f.get('protocol'),
          enabled: f.get('enabled') === 'on',
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
