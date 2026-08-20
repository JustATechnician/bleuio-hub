const state = {
  mock: false,
  stations: [],
  stationId: null,
  tab: "scan",
  devices: {},
  selectedAddr: null,
  connectedAddr: null,
  services: [],
  notifies: new Set(),
  log: [],
  macros: [],
  macroId: null,
  run: null,
  ws: null,
  writeMode: "hex",
};

const $ = (id) => document.getElementById(id);

async function api(path, opts = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { detail: text };
  }
  if (!res.ok) {
    const detail = data.detail || data.message || res.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

function logLine(msg, cls = "") {
  const ts = new Date().toLocaleTimeString();
  state.log.unshift({ ts, msg, cls });
  state.log = state.log.slice(0, 400);
  if (state.tab === "log" && state.stationId) renderLog();
}

function toast(msg, cls = "") {
  logLine(msg, cls);
}

function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function rssiBar(rssi) {
  if (rssi == null) return "—";
  const n = Number(rssi);
  const pct = Math.max(0, Math.min(100, (n + 100) * 1.4));
  return `${n} dBm`;
}

async function loadLobby() {
  const data = await api("/api/stations");
  state.mock = data.mock;
  state.stations = data.stations || [];
  $("mode-badge").textContent = data.mock ? "Mock stations" : `${state.stations.length} dongle(s)`;
  $("mode-badge").className = "badge" + (data.mock ? " warn" : " ok");
  if (!state.stationId) renderLobby();
}

function renderLobby() {
  $("view-lobby").classList.remove("hidden");
  $("view-station").classList.add("hidden");
  $("btn-lobby").classList.add("hidden");
  const root = $("view-lobby");
  if (!state.stations.length) {
    root.innerHTML = `<div class="card empty">No BleuIO dongles found. Plug them in and click Rescan, or run with mock fallback.</div>`;
    return;
  }
  root.innerHTML = `<div class="grid">${state.stations
    .map((s) => {
      const claim = s.claim || {};
      const status = claim.mine
        ? `<span class="badge ok">Yours</span>`
        : claim.claimed
          ? `<span class="badge busy">In use</span>`
          : `<span class="badge">Available</span>`;
      return `<article class="card">
        <div class="row space">
          <h3>${esc(s.id)}</h3>
          ${status}
        </div>
        <div class="kv">
          <div>Port <span class="mono">${esc(s.port)}</span></div>
          <div>MAC <span class="mono">${esc(s.mac || "—")}</span></div>
          <div>${esc(s.product || "BleuIO")} · ${esc(s.firmware || "fw ?")} · ${esc(s.hardware || "")}</div>
          <div>${s.connected ? "Connected " + esc(s.connected_addr) : "Idle"} ${s.scanning ? "· scanning" : ""}</div>
        </div>
        <div class="row" style="margin-top:12px">
          ${
            claim.mine
              ? `<button class="primary" data-open="${esc(s.id)}">Open</button><button data-release="${esc(s.id)}" class="danger">Release</button>`
              : claim.claimed
                ? `<button disabled>Busy</button>`
                : `<button class="primary" data-claim="${esc(s.id)}">Claim</button>`
          }
        </div>
      </article>`;
    })
    .join("")}</div>`;
}

async function claimStation(id) {
  await api(`/api/stations/${id}/claim`, { method: "POST", body: {} });
  await openStation(id);
}

async function openStation(id) {
  state.stationId = id;
  const data = await api("/api/stations");
  const station = (data.stations || []).find((s) => s.id === id);
  if (!station) return;
  hydrateStation(station);
  $("view-lobby").classList.add("hidden");
  $("view-station").classList.remove("hidden");
  $("btn-lobby").classList.remove("hidden");
  $("station-title").textContent = station.id;
  $("station-meta").textContent = `${station.port} · ${station.mac || "no MAC"} · ${station.firmware || ""}`;
  connectWs(id);
  await loadMacros();
  renderAll();
}

function hydrateStation(station) {
  state.connectedAddr = station.connected_addr;
  state.services = station.services || [];
  state.devices = {};
  for (const d of station.devices || []) state.devices[d.addr] = d;
}

async function releaseStation(id) {
  await api(`/api/stations/${id}/release`, { method: "POST", body: {} });
  closeWs();
  state.stationId = null;
  await loadLobby();
}

function connectWs(id) {
  closeWs();
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/api/stations/${id}/ws`);
  state.ws = ws;
  ws.onmessage = (ev) => {
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    handleEvent(msg);
  };
  ws.onclose = () => {
    if (state.stationId === id) setTimeout(() => connectWs(id), 1500);
  };
}

function closeWs() {
  if (state.ws) {
    state.ws.onclose = null;
    state.ws.close();
    state.ws = null;
  }
}

function handleEvent(msg) {
  const t = msg.type;
  if (t === "ping" && state.ws) {
    state.ws.send(JSON.stringify({ type: "pong" }));
    return;
  }
  if (t === "hello" && msg.station) hydrateStation(msg.station);
  if (t === "scan" && msg.device) {
    state.devices[msg.device.addr] = msg.device;
    if (state.tab === "scan") renderScan();
  }
  if (t === "adv" && msg.device) {
    state.devices[msg.device.addr] = msg.device;
    if (state.selectedAddr === msg.device.addr && state.tab === "scan") renderScan();
  }
  if (t === "scan_complete") {
    toast("Scan complete");
    renderScan();
  }
  if (t === "connection") {
    state.connectedAddr = msg.connected ? msg.address : null;
    if (!msg.connected) {
      state.services = [];
      state.notifies.clear();
    }
    toast(msg.connected ? `Connected ${msg.address}` : "Disconnected");
    renderAll();
  }
  if (t === "gatt") {
    state.services = msg.services || [];
    renderGatt();
  }
  if (t === "notify") {
    logLine(
      `NOTIFY ${msg.handle || ""} ${msg.uuid || ""} ${msg.hex || ""} ${msg.ascii ? '"' + msg.ascii + '"' : ""}`,
      "ok",
    );
    renderLog();
  }
  if (t === "read" || t === "write") {
    const tag = t.toUpperCase();
    logLine(
      `${tag} ${msg.handle || ""} ${msg.ok ? (msg.hex || "ok") : msg.error || "failed"} ${msg.ascii ? '"' + msg.ascii + '"' : ""}`,
      msg.ok ? "ok" : "err",
    );
    if (state.tab === "log") renderLog();
  }
  if (t === "passkey" && msg.needed) showPasskey();
  if (t === "macro_started") {
    state.run = { id: msg.run_id, steps: msg.steps || [], results: [], status: "running" };
    renderMacros();
  }
  if (t === "step_started") {
    if (!state.run) state.run = { results: [] };
    state.run.current = msg.index;
    renderMacros();
  }
  if (t === "step_result") {
    if (state.run) {
      state.run.results[msg.index] = msg;
    }
    logLine(`MACRO step ${msg.index} ${msg.ok ? "ok" : "fail"}`, msg.ok ? "ok" : "err");
    renderMacros();
  }
  if (t === "macro_paused") {
    if (state.run) state.run.status = "paused";
    state.run.pauseMessage = msg.message;
    renderMacros();
  }
  if (t === "macro_finished") {
    if (state.run) {
      state.run.status = msg.status;
      state.run.id = msg.run_id;
    }
    toast(`Macro ${msg.status}`);
    renderMacros();
  }
  if (t === "released") {
    toast("Station released (idle timeout)");
    state.stationId = null;
    loadLobby();
  }
  if (t === "station" && msg.station && msg.station.id === state.stationId) {
    $("station-meta").textContent = `${msg.station.port} · ${msg.station.mac || "no MAC"} · ${
      msg.station.scanning ? "scanning" : msg.station.connected ? "connected" : "idle"
    }`;
  }
  if (t === "error") toast(msg.message || "Error", "err");
}

function setTab(tab) {
  state.tab = tab;
  document.querySelectorAll(".tab").forEach((el) => el.classList.toggle("active", el.dataset.tab === tab));
  ["scan", "gatt", "macros", "log"].forEach((id) => {
    $(`tab-${id}`).classList.toggle("hidden", id !== tab);
  });
  renderAll();
}

function renderAll() {
  if (!state.stationId) return;
  renderScan();
  renderGatt();
  renderMacros();
  renderLog();
}

function deviceList() {
  return Object.values(state.devices).sort((a, b) => (a.rssi ?? -999) - (b.rssi ?? -999)).reverse();
}

function renderScan() {
  const selected = state.devices[state.selectedAddr];
  const rows = deviceList()
    .map((d) => {
      const uuids = (d.uuids || []).map((u) => `<span class="chip">${esc(u)}</span>`).join(" ");
      return `<tr class="clickable ${d.addr === state.selectedAddr ? "selected" : ""}" data-addr="${esc(d.addr)}">
        <td class="mono">${esc(d.addr)}</td>
        <td>${esc(d.name || "—")}</td>
        <td>${esc(rssiBar(d.rssi))}</td>
        <td>${uuids || "—"}</td>
      </tr>`;
    })
    .join("");
  $("tab-scan").innerHTML = `
    <div class="toolbar">
      <button id="scan-start" class="primary" type="button">Start scan</button>
      <button id="scan-stop" type="button">Stop</button>
      <input id="scan-dur" type="number" min="0" value="8" title="Seconds; 0 = until stop" />
      <input id="scan-filter" placeholder="hex filter (optional)" />
      <span class="muted">${Object.keys(state.devices).length} device(s)</span>
    </div>
    <div class="split">
      <div class="drawer" style="overflow:auto; max-height: 560px">
        <table>
          <thead><tr><th>Address</th><th>Name</th><th>RSSI</th><th>Adv UUIDs</th></tr></thead>
          <tbody>${rows || `<tr><td colspan="4" class="muted">No devices yet. Start a scan.</td></tr>`}</tbody>
        </table>
      </div>
      <div class="drawer">${renderAdv(selected)}</div>
    </div>`;
}

function renderAdv(d) {
  if (!d) return `<div class="empty">Select a device to inspect advertised data.</div>`;
  const fields = [...(d.fields || []), ...(d.scan_rsp_fields || []).map((f) => ({ ...f, _rsp: true }))];
  const fieldHtml = fields
    .map((f) => {
      const extra = f.text
        ? esc(f.text)
        : f.uuids
          ? (f.uuids || []).map((u, i) => `${u} ${f.names?.[i] ? "(" + f.names[i] + ")" : ""}`).join(", ")
          : f.company_id
            ? `company ${esc(f.company_id)} data ${esc(f.mfg_data || "")}`
            : f.flags != null
              ? `0x${Number(f.flags).toString(16)} ${JSON.stringify(f.decoded || {})}`
              : esc(f.ascii || f.hex || "");
      return `<div class="field"><b>${f._rsp ? "SCAN_RSP · " : ""}${esc(f.type_name || "Field")}</b>
        <div>${extra}</div>
        <div class="hex">${esc(f.hex || "")}</div></div>`;
    })
    .join("");
  const connected = state.connectedAddr === d.addr;
  return `
    <div class="row space">
      <div>
        <h3 style="margin:0">${esc(d.name || "Unknown")}</h3>
        <div class="mono muted">${esc(d.addr)}</div>
      </div>
      ${
        connected
          ? `<button id="btn-disc" class="danger" type="button">Disconnect</button>`
          : `<button id="btn-conn" class="primary" type="button">Connect</button>`
      }
    </div>
    <p class="muted">RSSI ${esc(rssiBar(d.rssi))}${d.mfsid ? " · MFSID " + esc(d.mfsid) : ""}</p>
    <div class="field"><b>ADV hex</b><div class="hex">${esc(d.adv_hex || "—")}</div></div>
    ${d.scan_rsp_hex ? `<div class="field"><b>SCAN_RSP hex</b><div class="hex">${esc(d.scan_rsp_hex)}</div></div>` : ""}
    ${fieldHtml || `<p class="muted">No decoded AD fields yet. Use Find Scan Data (default scan).</p>`}
    <div class="row"><button id="btn-target" type="button">Watch this advertiser</button></div>
  `;
}

function advertisedSet() {
  const d = state.devices[state.connectedAddr] || state.devices[state.selectedAddr];
  return new Set((d?.uuids || []).map((u) => String(u).toLowerCase()));
}

function renderGatt() {
  if (!state.connectedAddr) {
    $("tab-gatt").innerHTML = `<div class="empty">Connect to a device from the Scan tab to explore services and characteristics.</div>`;
    return;
  }
  const adv = advertisedSet();
  const gattUuids = new Set();
  for (const s of state.services) {
    if (s.uuid) gattUuids.add(String(s.uuid).toLowerCase());
    for (const c of s.characteristics || []) if (c.uuid) gattUuids.add(String(c.uuid).toLowerCase());
  }
  const missing = [...adv].filter((u) => u && !gattUuids.has(u) && u.length <= 8);
  const extra = [...gattUuids].filter((u) => u && !adv.has(u) && !["1800", "1801"].includes(u) && u.length <= 8);
  const hint =
    missing.length || extra.length
      ? `<p class="warn-text">Advertised vs GATT: missing in GATT [${missing.join(", ") || "none"}]; extra in GATT [${extra.join(", ") || "none"}]</p>`
      : `<p class="muted">Advertised UUIDs match the discovered GATT tree (16-bit).</p>`;

  const html = (state.services || [])
    .map((svc, si) => {
      const chars = (svc.characteristics || [])
        .map((ch) => {
          const flags = ch.properties?.flags || {};
          const mask = ch.properties?.mask || "";
          const canR = flags.read;
          const canW = flags.write || flags.write_without_response;
          const canN = flags.notify;
          const canI = flags.indicate;
          const on = state.notifies.has(ch.handle);
          return `<div class="char" data-handle="${esc(ch.handle)}">
            <div class="row space">
              <div>
                <div class="name">${esc(ch.name || "Characteristic")} <span class="mono muted">${esc(ch.uuid)}</span></div>
                <div class="muted">handle ${esc(ch.handle)} · <span class="props">${esc(mask)}</span></div>
              </div>
              <div class="row">
                <button data-read="${esc(ch.handle)}" ${canR ? "" : "disabled"} type="button">Read</button>
                <button data-write="${esc(ch.handle)}" ${canW ? "" : "disabled"} type="button">Write</button>
                <button data-noti="${esc(ch.handle)}" data-ind="0" ${canN ? "" : "disabled"} type="button">${on ? "Notify off" : "Notify"}</button>
                <button data-noti="${esc(ch.handle)}" data-ind="1" ${canI ? "" : "disabled"} type="button">Indicate</button>
              </div>
            </div>
            <div class="row" style="margin-top:8px">
              <input class="write-val" placeholder="${state.writeMode === "hex" ? "hex bytes" : "ascii"}" />
              <label class="muted"><input type="checkbox" class="wr-hex" checked /> hex</label>
              <label class="muted"><input type="checkbox" class="wr-nr" ${flags.write_without_response && !flags.write ? "checked" : ""} /> no response</label>
            </div>
            <div class="hex result"></div>
          </div>`;
        })
        .join("");
      return `<div class="svc">
        <div class="svc-h" data-svc="${si}">
          <div>${esc(svc.name || "Service")} <span class="mono muted">${esc(svc.uuid)}</span></div>
          <div class="muted">${(svc.characteristics || []).length} char(s)</div>
        </div>
        <div>${chars || `<div class="char muted">No characteristics</div>`}</div>
      </div>`;
    })
    .join("");

  $("tab-gatt").innerHTML = `
    <div class="toolbar">
      <strong>${esc(state.connectedAddr)}</strong>
      <button id="btn-disc-2" class="danger" type="button">Disconnect</button>
    </div>
    ${hint}
    ${html || `<div class="empty">No services discovered.</div>`}
  `;
}

async function loadMacros() {
  const data = await api("/api/macros");
  state.macros = data.macros || [];
  if (!state.macroId && state.macros[0]) state.macroId = state.macros[0].id;
}

function selectedMacro() {
  return state.macros.find((m) => m.id === state.macroId);
}

function renderMacros() {
  const m = selectedMacro();
  const list = state.macros
    .map(
      (x) => `<div class="macro-item ${x.id === state.macroId ? "active" : ""}" data-macro="${esc(x.id)}">
        <strong>${esc(x.name)}</strong>
        ${x.builtin ? `<span class="chip">built-in</span>` : `<span class="chip">custom</span>`}
        <div class="muted">${esc(x.description || "")}</div>
      </div>`,
    )
    .join("");
  const params = m?.params || {};
  const fields = Object.entries(params)
    .map(([k, v]) => {
      const val = k === "address" && state.selectedAddr ? state.selectedAddr : v;
      return `<label class="muted">${esc(k)}<br/><input data-param="${esc(k)}" value="${esc(val)}" /></label>`;
    })
    .join("");
  const run = state.run;
  const steps = (run?.steps || m?.steps || [])
    .map((step, i) => {
      const res = run?.results?.[i];
      const cls = res ? (res.ok ? "ok" : "err") : run?.current === i ? "run" : "";
      return `<div class="step ${cls}"><span class="mono">${i}</span> ${esc(step.op)} ${esc(JSON.stringify(step).slice(0, 120))}</div>`;
    })
    .join("");
  const pauseBar =
    run?.status === "paused"
      ? `<div class="card" style="margin-bottom:10px"><p>${esc(run.pauseMessage || "Paused")}</p>
         <button id="macro-cont" class="primary" type="button">Continue</button></div>`
      : "";
  $("tab-macros").innerHTML = `
    <div class="macro-list">
      <div>
        ${list || `<div class="muted">No macros</div>`}
        <button id="macro-new" type="button">New custom…</button>
      </div>
      <div>
        ${pauseBar}
        <div class="toolbar">
          <button id="macro-run" class="primary" type="button" ${run?.status === "running" ? "disabled" : ""}>Run</button>
          <button id="macro-cancel" class="danger" type="button">Cancel</button>
          ${run?.id ? `<a class="btn" href="/api/runs/${esc(run.id)}/log" download>Download log</a>` : ""}
        </div>
        <p class="muted">${esc(m?.description || "Select a macro")}</p>
        <div class="row">${fields}</div>
        <h3>Steps</h3>
        ${steps}
        ${
          m && !m.builtin
            ? `<h3>JSON</h3><textarea id="macro-json">${esc(JSON.stringify({ id: m.id, name: m.name, description: m.description, params: m.params, steps: m.steps }, null, 2))}</textarea>
               <div class="row"><button id="macro-save" type="button">Save</button><button id="macro-del" class="danger" type="button">Delete</button></div>`
            : `<p class="muted">Built-in macros are read-only. Duplicate via New custom to edit.</p>`
        }
      </div>
    </div>`;
}

function renderLog() {
  $("tab-log").innerHTML = `<div class="toolbar"><button id="log-clear" type="button">Clear</button></div>
    <div class="logbox">${state.log.map((l) => `[${esc(l.ts)}] ${esc(l.msg)}`).join("\n") || "No events yet."}</div>`;
}

function showPasskey() {
  const modal = $("modal");
  $("modal-body").innerHTML = `<h3>Passkey required</h3>
    <p class="muted">Enter the 6-digit BLE passkey.</p>
    <input id="pk" maxlength="6" placeholder="123456" />
    <div class="row" style="margin-top:12px">
      <button id="pk-ok" class="primary" type="button">Send</button>
      <button id="pk-no" type="button">Cancel</button>
    </div>`;
  modal.classList.remove("hidden");
  $("pk-no").onclick = () => modal.classList.add("hidden");
  $("pk-ok").onclick = async () => {
    try {
      await api(`/api/stations/${state.stationId}/passkey`, { method: "POST", body: { passkey: $("pk").value } });
      modal.classList.add("hidden");
    } catch (err) {
      toast(err.message, "err");
    }
  };
}

function collectParams() {
  const out = {};
  document.querySelectorAll("[data-param]").forEach((el) => {
    const k = el.getAttribute("data-param");
    let v = el.value;
    if (v === "true") v = true;
    else if (v === "false") v = false;
    out[k] = v;
  });
  return out;
}

document.addEventListener("click", async (ev) => {
  const t = ev.target;
  if (!(t instanceof HTMLElement)) return;
  try {
    if (t.id === "btn-refresh") await api("/api/stations/refresh", { method: "POST", body: {} }).then(loadLobby);
    if (t.id === "btn-lobby") {
      closeWs();
      state.stationId = null;
      await loadLobby();
    }
    if (t.dataset.claim) await claimStation(t.dataset.claim);
    if (t.dataset.open) await openStation(t.dataset.open);
    if (t.dataset.release) await releaseStation(t.dataset.release);
    if (t.id === "btn-release" && state.stationId) await releaseStation(state.stationId);
    if (t.dataset.tab) setTab(t.dataset.tab);
    if (t.id === "scan-start") {
      await api(`/api/stations/${state.stationId}/scan/start`, {
        method: "POST",
        body: { duration: Number($("scan-dur").value || 0), filter: $("scan-filter").value || "" },
      });
    }
    if (t.id === "scan-stop") await api(`/api/stations/${state.stationId}/scan/stop`, { method: "POST", body: {} });
    const addrRow = t.closest("[data-addr]");
    if (addrRow) {
      state.selectedAddr = addrRow.getAttribute("data-addr");
      renderScan();
      return;
    }
    if (t.id === "btn-conn" && state.selectedAddr) {
      await api(`/api/stations/${state.stationId}/connect`, { method: "POST", body: { address: state.selectedAddr } });
      setTab("gatt");
    }
    if (t.id === "btn-disc" || t.id === "btn-disc-2") {
      await api(`/api/stations/${state.stationId}/disconnect`, { method: "POST", body: {} });
    }
    if (t.id === "btn-target" && state.selectedAddr) {
      await api(`/api/stations/${state.stationId}/scantarget`, {
        method: "POST",
        body: { address: state.selectedAddr, duration: 8 },
      });
    }
    if (t.dataset.read) {
      const res = await api(`/api/stations/${state.stationId}/read`, { method: "POST", body: { handle: t.dataset.read } });
      const box = t.closest(".char")?.querySelector(".result");
      if (box) box.textContent = res.ok ? `${res.hex}  "${res.ascii || ""}"` : res.error || "read failed";
    }
    if (t.dataset.write) {
      const wrap = t.closest(".char");
      const val = wrap.querySelector(".write-val")?.value || "";
      const asHex = wrap.querySelector(".wr-hex")?.checked;
      const nr = wrap.querySelector(".wr-nr")?.checked;
      const body = asHex ? { handle: t.dataset.write, hex: val, without_response: nr } : { handle: t.dataset.write, ascii: val, without_response: nr };
      const res = await api(`/api/stations/${state.stationId}/write`, { method: "POST", body });
      const box = wrap.querySelector(".result");
      if (box) box.textContent = res.ok ? `wrote ${res.hex}` : res.error || "write failed";
    }
    if (t.dataset.noti) {
      const enable = !t.textContent.toLowerCase().includes("off");
      const indicate = t.dataset.ind === "1";
      await api(`/api/stations/${state.stationId}/notify`, {
        method: "POST",
        body: { handle: t.dataset.noti, enable, indicate },
      });
      if (enable) state.notifies.add(t.dataset.noti);
      else state.notifies.delete(t.dataset.noti);
      renderGatt();
    }
    const macroItem = t.closest("[data-macro]");
    if (macroItem) {
      state.macroId = macroItem.getAttribute("data-macro");
      renderMacros();
      return;
    }
    if (t.id === "macro-run") {
      const id = state.macroId;
      const run = await api(`/api/stations/${state.stationId}/macros/${id}/run`, {
        method: "POST",
        body: { params: collectParams() },
      });
      state.run = { id: run.id, status: run.status, steps: selectedMacro()?.steps || [], results: [] };
      renderMacros();
    }
    if (t.id === "macro-cancel") await api(`/api/stations/${state.stationId}/macros/cancel`, { method: "POST", body: {} });
    if (t.id === "macro-cont") await api(`/api/stations/${state.stationId}/macros/continue`, { method: "POST", body: {} });
    if (t.id === "macro-new") {
      const id = "custom-" + Math.random().toString(36).slice(2, 7);
      const body = {
        name: "Custom sequence",
        description: "Edit steps in JSON",
        params: { address: state.selectedAddr || state.connectedAddr || "" },
        steps: [
          { op: "connect", address: "{{address}}" },
          { op: "read_all" },
          { op: "disconnect" },
        ],
      };
      await api(`/api/macros/${id}`, { method: "PUT", body });
      await loadMacros();
      state.macroId = id;
      renderMacros();
    }
    if (t.id === "macro-save") {
      const parsed = JSON.parse($("macro-json").value);
      await api(`/api/macros/${parsed.id || state.macroId}`, { method: "PUT", body: parsed });
      await loadMacros();
      toast("Macro saved");
      renderMacros();
    }
    if (t.id === "macro-del") {
      await api(`/api/macros/${state.macroId}`, { method: "DELETE" });
      await loadMacros();
      state.macroId = state.macros[0]?.id || null;
      renderMacros();
    }
    if (t.id === "log-clear") {
      state.log = [];
      renderLog();
    }
  } catch (err) {
    toast(err.message || String(err), "err");
    alert(err.message || String(err));
  }
});

setInterval(() => {
  api("/api/heartbeat", { method: "POST", body: {} }).catch(() => {});
}, 30000);

loadLobby().catch((err) => {
  $("view-lobby").innerHTML = `<div class="card err-text">${esc(err.message)}</div>`;
});
