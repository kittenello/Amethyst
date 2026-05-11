let state = null;
let selectedBrawler = null;
let selectedType = "trophies";
let selectedPlaystyle = "";
let historySort = "matches";
let lang = "en";
let playerTrophies = {};
let playerPowers = {};
let playerName = "Player";
let botRunning = false;
let multiState = { devices: [], instances: [] };
let selectedMultiLogId = null;
let selectedMultiPort = Number(localStorage.getItem('amethyst.multi.selectedPort') || 0) || null;
let multiPollTimer = null;
let brawlersMultiMode = false;
let activeInstanceKey = "main";
let instanceProfiles = {};

const tr = {
  en: { dashboard: "Dashboard", multi: "Multi-Instance", brawlers: "Brawlers", playstyles: "Playstyles", history: "History", logging: "Logging", settings: "Settings", multi: "Multi-Instance" },
  ru: { dashboard: "\u041f\u0430\u043d\u0435\u043b\u044c", multi: "Multi-Instance", brawlers: "\u0411\u043e\u0439\u0446\u044b", playstyles: "\u0421\u0442\u0438\u043b\u0438", history: "\u0418\u0441\u0442\u043e\u0440\u0438\u044f", logging: "\u041b\u043e\u0433\u0438", settings: "\u041d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438" }
};

const $ = (id) => document.getElementById(id);

function normKey(value) {
  return String(value || "")
    .replace(/&amp;/gi, "&")
    .toLowerCase()
    .replace(/[^a-z0-9]/g, "");
}

function trophyFor(brawler) {
  if (!brawler) return undefined;
  const candidates = [brawler.id, brawler.name, normKey(brawler.id), normKey(brawler.name)];
  for (const key of candidates) {
    if (playerTrophies[key] !== undefined) return playerTrophies[key];
  }
  const wanted = normKey(brawler.id) || normKey(brawler.name);
  for (const [key, value] of Object.entries(playerTrophies || {})) {
    if (normKey(key) === wanted) return value;
  }
  return undefined;
}

function normalizeTrophyMap(raw) {
  const out = {};
  if (!raw) return out;
  if (Array.isArray(raw)) {
    raw.forEach(row => {
      const name = row?.id || row?.name || row?.brawler;
      const trophies = row?.trophies;
      if (name !== undefined && trophies !== undefined) {
        out[String(name)] = Number(trophies) || 0;
        out[normKey(name)] = Number(trophies) || 0;
      }
    });
    return out;
  }
  Object.entries(raw).forEach(([key, value]) => {
    out[String(key)] = Number(value) || 0;
    out[normKey(key)] = Number(value) || 0;
  });
  return out;
}

async function api(path, options = {}) {
  const res = await fetch(path, { cache: "no-store", headers: { "Content-Type": "application/json" }, ...options });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "Request failed");
  return data;
}

async function init() {
  state = await api("/api/state");
  console.log("[WEBAPP][STATE]", state);
  selectedPlaystyle = state.currentPlaystyle || "";
  renderAll();
  if (state.playerTag) {
    loadPlayerData(true).catch(err => console.warn("[WEBAPP][PLAYER] Auto-load failed:", err));
  }
}

function nameOf(id) {
  return state.brawlers.find(b => b.id === id)?.name || id;
}


function profileKeyForPort(port) { return port ? `ldp_${Number(port)}` : "main"; }
function profileLabel(key) { return key === "main" ? "Main" : `LDPlayer ${String(key).replace("ldp_", "")}`; }
function defaultProfile(key = "main") {
  return { key, label: profileLabel(key), playerTag: "", playerName: "Player", trophies: {}, powers: {}, queue: [] };
}
function loadInstanceProfiles() {
  try { instanceProfiles = JSON.parse(localStorage.getItem("amethyst.multi.brawlerProfiles") || "{}"); } catch (_) { instanceProfiles = {}; }
  if (!instanceProfiles.main) {
    instanceProfiles.main = defaultProfile("main");
    instanceProfiles.main.playerTag = state?.playerTag || "";
    instanceProfiles.main.playerName = state?.playerName || "Player";
    instanceProfiles.main.trophies = state?.playerTrophies || playerTrophies || {};
    instanceProfiles.main.queue = state?.queue || [];
  }
}
function saveInstanceProfiles() {
  try { localStorage.setItem("amethyst.multi.brawlerProfiles", JSON.stringify(instanceProfiles)); } catch (_) {}
}
function currentProfile() {
  loadInstanceProfiles();
  if (!instanceProfiles[activeInstanceKey]) instanceProfiles[activeInstanceKey] = defaultProfile(activeInstanceKey);
  return instanceProfiles[activeInstanceKey];
}
function applyProfileToUi() {
  if (!brawlersMultiMode) {
    activeInstanceKey = "main";
    playerTrophies = normalizeTrophyMap(state.playerTrophies || playerTrophies || {});
    playerName = state.playerName || playerName || "Player";
    return;
  }
  const profile = currentProfile();
  state.queue = Array.isArray(profile.queue) ? profile.queue : [];
  state.playerTag = profile.playerTag || "";
  playerName = profile.playerName || "Player";
  playerTrophies = normalizeTrophyMap(profile.trophies || {});
  playerPowers = profile.powers || {};
  if ($("playerName")) $("playerName").textContent = playerName;
  if ($("playerTag")) $("playerTag").textContent = profile.playerTag || "";
  if ($("playerTagInput")) $("playerTagInput").value = profile.playerTag || "";
}
function persistActiveProfile() {
  if (!brawlersMultiMode) return;
  const profile = currentProfile();
  profile.playerTag = $("playerTagInput")?.value || profile.playerTag || "";
  profile.playerName = playerName || profile.playerName || "Player";
  profile.trophies = playerTrophies || {};
  profile.powers = playerPowers || {};
  profile.queue = state.queue || [];
  saveInstanceProfiles();
}
function availableInstanceTabs() {
  const devices = multiState?.devices || [];
  const ports = new Set();
  devices.forEach(d => { if (d.port) ports.add(Number(d.port)); });
  (multiState?.instances || []).forEach(i => { if (i.port) ports.add(Number(i.port)); });
  if (!ports.size) ports.add(5555);
  return [...ports].sort((a,b)=>a-b).map(port => ({ key: profileKeyForPort(port), port, label: `LDPlayer ${port}` }));
}
function renderBrawlerInstanceTabs() {
  const toggle = $("brawlersMultiMode");
  const host = $("brawlerInstanceTabs");
  if (toggle) toggle.checked = brawlersMultiMode;
  if (!host) return;
  host.hidden = !brawlersMultiMode;
  if (!brawlersMultiMode) { host.innerHTML = ""; return; }
  loadInstanceProfiles();
  const tabs = availableInstanceTabs();
  if (!tabs.some(t => t.key === activeInstanceKey)) activeInstanceKey = tabs[0]?.key || "main";
  for (const tab of tabs) if (!instanceProfiles[tab.key]) instanceProfiles[tab.key] = defaultProfile(tab.key);
  saveInstanceProfiles();
  host.innerHTML = tabs.map(tab => {
    const p = instanceProfiles[tab.key] || defaultProfile(tab.key);
    const ready = Array.isArray(p.queue) ? p.queue.length : 0;
    return `<button type="button" class="${activeInstanceKey === tab.key ? "active" : ""}" data-brawler-instance="${tab.key}" data-port="${tab.port}"><b>${escapeHtml(tab.label)}</b><small>${ready} ready</small></button>`;
  }).join("");
  document.querySelectorAll("[data-brawler-instance]").forEach(btn => btn.onclick = () => {
    persistActiveProfile();
    activeInstanceKey = btn.dataset.brawlerInstance;
    applyProfileToUi();
    selectedBrawler = null;
    renderBrawlers();
    renderQueue();
    renderBrawlerInstanceTabs();
    updateStartState();
  });
}
function queueForPort(port) {
  if (!brawlersMultiMode) return state?.queue || [];
  const profile = instanceProfiles[profileKeyForPort(port)] || currentProfile();
  return Array.isArray(profile.queue) ? profile.queue : [];
}

function instanceIdForPort(port) {
  const p = Number(port);
  const common = [5555, 5557, 5559, 5561];
  const idx = common.indexOf(p);
  if (idx >= 0) return idx + 1;
  const online = (multiState.devices || [])
    .filter(d => String(d.status || '').toLowerCase() === 'device' && Number(d.port))
    .map(d => Number(d.port))
    .sort((a, b) => a - b);
  const pos = online.indexOf(p);
  return pos >= 0 ? pos + 1 : 1;
}

function selectMultiPort(port) {
  // Multi-Instance page selection must only choose the target emulator for Start Next.
  // Do NOT switch Brawlers tabs / queues here, otherwise clicking LDPlayer chips mutates
  // the bottom queue UI and feels like brawlers disappeared.
  selectedMultiPort = Number(port) || null;
  if (selectedMultiPort) {
    try { localStorage.setItem('amethyst.multi.selectedPort', String(selectedMultiPort)); } catch (_) {}
    loadInstanceProfiles();
    const key = profileKeyForPort(selectedMultiPort);
    if (!instanceProfiles[key]) instanceProfiles[key] = defaultProfile(key);
    saveInstanceProfiles();
  }
  renderMulti();
}

function renderAll() {
  loadInstanceProfiles();
  applyProfileToUi();
  $("playerName").textContent = playerName || state.playerName || "Player";
  $("playerTag").textContent = state.playerTag || "";
  $("playerTagInput").value = state.playerTag || "";
  if ($("playerLoadStatus")) $("playerLoadStatus").textContent = state.playerTag ? "Will auto-load player data..." : "Write Player Tag and click Sync Player.";
  renderDashboard();
  renderBrawlers();
  renderPlaystyles();
  renderHistory();
  renderLogging();
  renderSettings();
  renderQueue();
  renderBrawlerInstanceTabs();
  setupMultiHub();
  updateStartState(false);
}

function switchView(view) {
  document.querySelectorAll(".view").forEach(el => el.classList.toggle("active", el.id === view));
  document.querySelectorAll(".nav").forEach(el => el.classList.toggle("active", el.dataset.view === view));
  $("pageTitle").textContent = tr[lang][view] || view;
}

function renderDashboard() {
  const play = currentPlaystyleMeta();
  $("activePlaystyle").innerHTML = play ? playstyleMarkup(play, "ACTIVE PLAYSTYLE") : `<div class="eyebrow">ACTIVE PLAYSTYLE</div><h2>No playstyle selected</h2><p>Pick one in Playstyles before starting.</p>`;
  renderRuntimePanel();
}

function renderRuntimePanel() {
  const runtime = state.runtime || {};
  const current = state.queue[0] || {};
  const metricKey = current.type || "trophies";
  const progressCurrent = Number(current[metricKey] ?? 0);
  const progressTarget = Number(current.push_until ?? 0);
  const percent = progressTarget > 0 ? Math.max(0, Math.min(100, Math.round(progressCurrent * 100 / progressTarget))) : 0;
  const currentId = runtime.currentBrawler && runtime.currentBrawler !== "none" ? runtime.currentBrawler : current.brawler;
  const currentName = currentId ? nameOf(currentId) : "none";
  const playstyle = currentPlaystyleMeta()?.name || "none";
  $("runtimePanel").innerHTML = `
    <div class="session-cards">
      <div class="metric wide-metric"><small>ACTIVE SESSION</small><b>${currentName} / ${metricKey}</b><div class="progress"><span style="width:${percent}%"></span></div><strong>${progressCurrent} / ${progressTarget || 0}</strong></div>
      <div class="metric"><small>CURRENT BRAWLER</small><b>${currentName}</b></div>
      <div class="metric"><small>STATE</small><b>${runtime.state || (botRunning ? "running" : "idle")}</b></div>
      <div class="metric"><small>IPS</small><b>${runtime.ips ?? "0.0"}</b></div>
      <div class="metric"><small>PLAYSTYLE</small><b>${playstyle}</b></div>
    </div>
  `
}

function renderBrawlers() {
  const query = ($("brawlerSearch").value || "").toLowerCase();
  $("brawlerGrid").innerHTML = state.brawlers
    .filter(b => b.name.toLowerCase().includes(query) || b.id.includes(query))
    .map(b => `<button class="brawler-card ${selectedBrawler?.id === b.id ? "active" : ""}" data-brawler="${b.id}"><img src="${b.icon}" onerror="this.style.visibility='hidden'"><span>${b.name}</span></button>`)
    .join("");
  document.querySelectorAll("[data-brawler]").forEach(btn => btn.onclick = () => {
    selectedBrawler = state.brawlers.find(b => b.id === btn.dataset.brawler);
    const existing = state.queue.find(q => q.brawler === selectedBrawler.id);
    selectedType = existing?.type || "trophies";
    renderBrawlers();
  });
  renderBrawlerEditor();
}

function renderBrawlerEditor() {
  const host = $("brawlerEditor");
  if (!selectedBrawler) {
    host.innerHTML = `<div class="eyebrow">SELECTED BRAWLER</div><h2>No brawler selected</h2><p>Choose a brawler from the list.</p>`;
    return;
  }
  const existing = state.queue.find(q => q.brawler === selectedBrawler.id) || {};
  const type = selectedType;
  const syncedTrophies = trophyFor(selectedBrawler);
  host.innerHTML = `
    <div class="selected-brawler"><img src="${selectedBrawler.icon}"><div><div class="eyebrow">SELECTED BRAWLER</div><h2>${selectedBrawler.name}</h2><p>Live values synced from Player Tag when available</p></div></div>
    <div class="tabs"><button class="${type === "trophies" ? "active" : ""}" data-type="trophies">Target Trophies</button><button class="${type === "wins" ? "active" : ""}" data-type="wins">Target Wins</button></div>
    <div class="field-row"><div><label>TARGET AMOUNT</label><input id="targetAmount" class="input wide" value="${existing.push_until || (type === "trophies" ? 1000 : 300)}"></div><div><label>${type === "trophies" ? "CURRENT TROPHIES" : "CURRENT WINS"}</label><input id="currentValue" class="input wide" value="${type === "trophies" ? (syncedTrophies ?? existing.trophies ?? 0) : (existing.wins ?? 0)}"></div></div>
    ${type === "trophies" ? `<label>CURRENT WIN STREAK</label><input id="winStreak" class="input" value="${existing.win_streak ?? 0}">` : ""}
    <label class="check"><input id="autoPick" type="checkbox" ${existing.automatically_pick ? "checked" : ""}><span><b>Automatically pick this brawler</b><br>Enabled by default once another brawler is queued ahead of it.</span></label>
    <button id="updateQueue" class="primary">Update Queue Entry</button>`;
  document.querySelectorAll("[data-type]").forEach(btn => btn.onclick = () => { selectedType = btn.dataset.type; renderBrawlerEditor(); });
  ["targetAmount", "currentValue", "winStreak"].forEach(id => $(id)?.addEventListener("input", updateQueueButton));
  $("updateQueue").onclick = saveQueueEntry;
  updateQueueButton();
}

function updateQueueButton() {
  const ok = selectedBrawler && $("targetAmount")?.value.trim() && $("currentValue")?.value.trim();
  $("updateQueue")?.classList.toggle("disabled", !ok);
  if ($("updateQueue")) $("updateQueue").disabled = !ok;
}

async function saveQueueEntry() {
  const payload = {
    brawler: selectedBrawler.id,
    type: selectedType,
    push_until: $("targetAmount").value,
    trophies: selectedType === "trophies" ? $("currentValue").value : 0,
    wins: selectedType === "wins" ? $("currentValue").value : 0,
    win_streak: $("winStreak")?.value || 0,
    automatically_pick: $("autoPick").checked
  };
  if (brawlersMultiMode) {
    const row = { ...payload, push_until: Number(payload.push_until) || 0, trophies: Number(payload.trophies) || 0, wins: Number(payload.wins) || 0, win_streak: Number(payload.win_streak) || 0 };
    state.queue = (state.queue || []).filter(x => x.brawler !== row.brawler);
    state.queue.push(row);
    persistActiveProfile();
    renderQueue();
    renderBrawlerInstanceTabs();
    updateStartState();
    return;
  }
  const res = await api("/api/queue", { method: "POST", body: JSON.stringify(payload) });
  state.queue = res.queue;
  renderQueue();
  updateStartState();
}

function buildPushAllQueue(targetTrophies) {
  const target = Number.parseInt(targetTrophies, 10);
  if (!Number.isFinite(target) || target <= 0) throw new Error("Write a valid trophy target.");

  const rows = [];
  for (const b of state.brawlers || []) {
    const trophies = trophyFor(b);
    if (trophies === undefined) continue;
    const current = Number(trophies) || 0;
    if (current >= target) continue;
    rows.push({
      brawler: b.id,
      push_until: target,
      trophies: current,
      wins: 0,
      type: "trophies",
      automatically_pick: true,
      selection_method: "lowest_trophies",
      win_streak: 0
    });
  }

  rows.sort((a, b) => (a.trophies - b.trophies) || nameOf(a.brawler).localeCompare(nameOf(b.brawler)));
  // Webapp cannot pre-click the first lowest-trophy brawler like the old Tk GUI,
  // so every Push All row must be auto-picked by the runner, including row #1.
  rows.forEach(row => row.automatically_pick = true);
  return rows;
}

async function applyPushAll(targetTrophies) {
  const status = $("pushAllStatus");
  if (status) status.textContent = "Preparing queue...";
  if (!Object.keys(playerTrophies || {}).length) {
    if (status) status.textContent = "Syncing player first...";
    await loadPlayerData(true);
  }

  const rows = buildPushAllQueue(targetTrophies);
  if (!rows.length) {
    const message = `No synced brawlers below ${targetTrophies} trophies.`;
    if (status) status.textContent = message;
    throw new Error(message);
  }

  if (brawlersMultiMode) {
    state.queue = rows;
    persistActiveProfile();
  } else {
    const res = await api("/api/queue/bulk", { method: "POST", body: JSON.stringify({ queue: rows }) });
    state.queue = res.queue;
  }
  selectedBrawler = state.brawlers.find(b => b.id === rows[0].brawler) || selectedBrawler;
  selectedType = "trophies";
  renderBrawlers();
  renderBrawlerEditor();
  renderQueue();
  updateStartState();
  if (status) status.textContent = `Queued ${rows.length} brawlers to ${targetTrophies} trophies. First: ${nameOf(rows[0].brawler)} (${rows[0].trophies}).`;
}

function openPushAllModal() {
  console.log("[WEBAPP][PUSH_ALL] Open modal clicked");
  const modal = $("pushAllModal");
  if (!modal) {
    console.error("[WEBAPP][PUSH_ALL] Modal element not found");
    alert("Push all modal not found. Reload page with Ctrl+F5.");
    return;
  }
  const input = $("pushAllTarget");
  const status = $("pushAllStatus");
  if (input && !input.value) input.value = "1000";
  if (status) {
    const count = Object.keys(playerTrophies || {}).length;
    status.textContent = count
      ? `Ready: ${Math.floor(count / 2) || count} synced brawlers.`
      : "No synced player data yet. Push all will sync automatically.";
  }
  modal.hidden = false;
  modal.removeAttribute("hidden");
  modal.style.display = "grid";
}

function closePushAllModal() {
  const modal = $("pushAllModal");
  if (modal) {
    modal.hidden = true;
    modal.setAttribute("hidden", "");
    modal.style.display = "none";
  }
}

function renderPlaystyles() {
  const query = ($("playstyleSearch").value || "").toLowerCase();
  $("selectedPlaystyle").innerHTML = currentPlaystyleMeta() ? playstyleMarkup(currentPlaystyleMeta()) : "Drag one playstyle here";
  $("playstyleGrid").innerHTML = state.playstyles
    .filter(p => `${p.name} ${p.description} ${p.gamemodes?.join(" ")}`.toLowerCase().includes(query))
    .map(p => `<div class="playstyle-card ${selectedPlaystyle === p.file ? "active" : ""}" draggable="true" data-playstyle="${p.file}"><button class="dots" data-menu="${p.file}" aria-label="Playstyle menu">...</button>${playstyleMarkup(p)}<div class="playstyle-menu" data-menu-panel="${p.file}"><button data-delete-playstyle="${p.file}">Delete playstyle</button></div></div>`)
    .join("");
  document.querySelectorAll("[data-playstyle]").forEach(card => {
    card.ondragstart = ev => ev.dataTransfer.setData("text/plain", card.dataset.playstyle);
    card.onclick = (ev) => {
      if (ev.target.closest(".dots") || ev.target.closest(".playstyle-menu")) return;
      selectPlaystyle(card.dataset.playstyle);
    };
  });
  document.querySelectorAll("[data-menu]").forEach(btn => btn.onclick = (ev) => {
    ev.stopPropagation();
    const file = btn.dataset.menu;
    document.querySelectorAll(".playstyle-menu").forEach(menu => {
      menu.classList.toggle("open", menu.dataset.menuPanel === file && !menu.classList.contains("open"));
    });
  });
  document.querySelectorAll("[data-delete-playstyle]").forEach(btn => btn.onclick = (ev) => {
    ev.stopPropagation();
    deletePlaystyle(btn.dataset.deletePlaystyle);
  });
}

function playstyleMarkup(p, eyebrow = "") {
  const modes = (p.gamemodes || ["all"]).map(m => `<span class="tag">${String(m).replace("_", " ")}</span>`).join("");
  return `${eyebrow ? `<div class="eyebrow">${eyebrow}</div>` : ""}<h2>${p.name}</h2><p>${p.author || "Official"} ${p.date ? "| " + p.date : ""}</p><p>${p.description || ""}</p><div class="mode-tags">${modes}</div>`;
}

function currentPlaystyleMeta() {
  return state.playstyles.find(p => p.file === selectedPlaystyle);
}

function selectPlaystyle(file) {
  selectedPlaystyle = file;
  state.currentPlaystyle = file;
  renderPlaystyles();
  renderDashboard();
  updateStartState();
}

function renderHistory() {
  const totals = state.history.total;
  const total = (totals.victory || 0) + (totals.defeat || 0) + (totals.draw || 0);
  const wr = total ? Math.round((totals.victory || 0) * 100 / total) : 0;
  const lr = total ? Math.round((totals.defeat || 0) * 100 / total) : 0;
  $("historyTotal").textContent = `${total} total matches`;
  $("historySummary").textContent = `${totals.victory || 0} wins | ${totals.defeat || 0} losses | ${wr}% win rate | ${lr}% loss rate`;
  const query = ($("historySearch").value || "").toLowerCase();
  let rows = state.history.brawlers.filter(r => r.name.toLowerCase().includes(query));
  rows.sort((a, b) => historySort === "name" ? a.name.localeCompare(b.name) : historySort === "rate" ? winRate(b) - winRate(a) : b.total - a.total);
  $("historyGrid").innerHTML = rows.map(row => {
    const win = winRate(row), loss = row.total ? Math.round(row.defeat * 100 / row.total) : 0;
    return `<div class="history-card"><div class="history-top"><img src="${row.icon}"><div><h2>${row.name}</h2><p>${row.total} tracked matches</p></div></div><div class="stats"><div><small>WINS</small><b class="win">${row.victory}</b></div><div><small>LOSSES</small><b class="loss">${row.defeat}</b></div><div><small>WIN%</small><b class="win">${win}%</b></div><div><small>LOSS%</small><b class="loss">${loss}%</b></div></div></div>`;
  }).join("");
}

function winRate(row) { return row.total ? Math.round(row.victory * 100 / row.total) : 0; }

function renderSettings() {
  const groups = [
    ["general", "Core", state.settings.general],
    ["bot", "Detection", state.settings.bot],
    ["timers", "Timers", pick(state.settings.timers, ["super", "hypercharge", "gadget", "wall_detection", "no_detection_proceed"])]
  ];
  $("settingsGrid").innerHTML = groups.map(([section, title, data]) => `<div class="setting-group"><div class="eyebrow">${title.toUpperCase()}</div>${Object.entries(data).map(([key, value]) => settingRow(section, key, value)).join("")}</div>`).join("");
  document.querySelectorAll("#settingsGrid [data-setting]").forEach(input => input.onchange = saveSetting);
}

function renderLogging() {
  const groups = [
    ["discord", "Discord", state.logging?.discord || {}],
    ["telegram", "Telegram", state.logging?.telegram || {}]
  ];
  $("loggingGrid").innerHTML = groups.map(([section, title, data]) => {
    const test = section === "telegram" ? `<button id="telegramTest" class="secondary full-button">Check Test</button><p id="telegramTestStatus" class="tiny-status"></p>` : "";
    return `<div class="setting-group"><div class="eyebrow">${title.toUpperCase()}</div>${Object.entries(data).filter(([key]) => !key.startsWith("#")).map(([key, value]) => settingRow(section, key, value)).join("")}${test}</div>`;
  }).join("");
  document.querySelectorAll("#loggingGrid [data-setting]").forEach(input => input.onchange = saveSetting);
  if ($("telegramTest")) $("telegramTest").onclick = sendTelegramTest;
}

async function sendTelegramTest() {
  $("telegramTestStatus").textContent = "Sending...";
  try {
    const res = await api("/api/logging/telegram-test", { method: "POST", body: "{}" });
    $("telegramTestStatus").textContent = res.ok ? "Test message sent." : "Telegram returned an error.";
  } catch (err) {
    $("telegramTestStatus").textContent = err.message;
  }
}

function pick(obj, keys) { return Object.fromEntries(keys.map(k => [k, obj?.[k] ?? ""])); }

function settingRow(section, key, value) {
  const label = key.replaceAll("_", " ").replace(/\b\w/g, c => c.toUpperCase());
  if (typeof value === "boolean") return `<div class="setting"><div><b>${label}</b><p>Auto-save</p></div><input data-setting="${section}:${key}" type="checkbox" ${value ? "checked" : ""}></div>`;
  return `<div class="setting"><div><b>${label}</b><p>Auto-save</p></div><input data-setting="${section}:${key}" class="input" value="${value ?? ""}"></div>`;
}

async function saveSetting(ev) {
  const [section, key] = ev.target.dataset.setting.split(":");
  const value = ev.target.type === "checkbox" ? ev.target.checked : coerce(ev.target.value);
  const res = await api("/api/settings", { method: "POST", body: JSON.stringify({ section, key, value }) });
  if (section === "discord" || section === "telegram") {
    state.logging[section][key] = value;
  } else {
    state.settings = res.settings;
  }
}

function coerce(value) {
  if (value === "true") return true;
  if (value === "false") return false;
  if (value !== "" && !Number.isNaN(Number(value))) return Number(value);
  return value;
}

function renderQueue() {
  $("queueCount").textContent = `${state.queue.length} brawler${state.queue.length === 1 ? "" : "s"} ready`;
  $("queueItems").innerHTML = state.queue.map(row => `<div class="queue-item"><button class="queue-remove" data-remove="${row.brawler}">x</button><img src="/assets/brawler_icons/${row.brawler}.png"><div><b>${nameOf(row.brawler)}</b><small>Current ${row.type}: ${row[row.type] || 0}</small><small>Target ${row.type}: ${row.push_until}</small></div></div>`).join("");
  document.querySelectorAll("[data-remove]").forEach(btn => btn.onclick = () => {
    state.queue = state.queue.filter(row => row.brawler !== btn.dataset.remove);
    renderQueue();
    updateStartState();
  });
}

function updateStartState(updatePanel = true) {
  const ready = state.queue.length > 0 && selectedPlaystyle;
  $("startBtn").disabled = !ready && !botRunning;
  $("startBtn").querySelector("span").textContent = botRunning ? "STOP" : "START";
  $("startHint").textContent = botRunning ? "Bot is running. Stop it from here when needed." : ready ? "Queue is ready. Start PylaAI from here." : "Select a brawler and a playstyle before starting.";
  if (updatePanel) renderRuntimePanel();
}

async function startBot() {
  if (botRunning) {
    await api("/api/stop", { method: "POST", body: "{}" });
    botRunning = false;
    await refreshRuntime();
    return;
  }
  $("startBtn").disabled = true;
  $("startBtn").querySelector("span").textContent = "STARTING";
  await api("/api/start", { method: "POST", body: JSON.stringify({ queue: state.queue, playstyle: selectedPlaystyle }) });
  botRunning = true;
  await refreshRuntime();
}

async function refreshRuntime() {
  const fresh = await api("/api/state");
  state.runtime = fresh.runtime;
  renderRuntimePanel();
  updateStartState(false);
}

async function importPlaystyleFile(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".pyla")) {
    alert("Only .pyla files can be imported.");
    return;
  }
  const content = await file.text();
  const res = await api("/api/playstyles/import", {
    method: "POST",
    body: JSON.stringify({ filename: file.name, content })
  });
  state.playstyles = res.playstyles;
  selectPlaystyle(res.file);
}

async function deletePlaystyle(file) {
  const res = await api("/api/playstyles/delete", {
    method: "POST",
    body: JSON.stringify({ filename: file })
  });
  state.playstyles = res.playstyles;
  if (selectedPlaystyle === file) {
    selectedPlaystyle = state.playstyles[0]?.file || "";
    state.currentPlaystyle = selectedPlaystyle;
  }
  renderPlaystyles();
  renderDashboard();
  updateStartState();
}

document.addEventListener("click", ev => {
  const nav = ev.target.closest("[data-view]");
  if (nav) switchView(nav.dataset.view);
  if (!ev.target.closest(".playstyle-card")) {
    document.querySelectorAll(".playstyle-menu").forEach(menu => menu.classList.remove("open"));
  }
});
$("startBtn").onclick = startBot;
$("brawlerSearch").oninput = renderBrawlers;
$("playstyleSearch").oninput = renderPlaystyles;
$("historySearch").oninput = renderHistory;
document.querySelectorAll("[data-sort]").forEach(btn => btn.onclick = () => { historySort = btn.dataset.sort; document.querySelectorAll("[data-sort]").forEach(b => b.classList.toggle("active", b === btn)); renderHistory(); });
$("selectedPlaystyle").ondragover = ev => ev.preventDefault();
$("selectedPlaystyle").ondrop = ev => { ev.preventDefault(); selectPlaystyle(ev.dataTransfer.getData("text/plain")); };
$("importPlaystyle").onclick = () => $("playstyleFile").click();
$("playstyleFile").onchange = ev => importPlaystyleFile(ev.target.files[0]);
$("langToggle").onclick = () => {
  lang = lang === "en" ? "ru" : "en";
  $("langToggle").textContent = lang === "en" ? "English" : "\u0420\u0443\u0441\u0441\u043a\u0438\u0439";
  document.querySelectorAll("[data-i18n]").forEach(el => el.textContent = tr[lang][el.dataset.i18n]);
  const active = document.querySelector(".view.active").id;
  $("pageTitle").textContent = tr[lang][active] || active;
};
$("clearQueue").onclick = async () => {
  if (brawlersMultiMode) {
    state.queue = [];
    persistActiveProfile();
    renderQueue();
    renderBrawlerInstanceTabs();
    updateStartState();
    return;
  }
  const res = await api("/api/queue/clear", { method: "POST", body: "{}" });
  state.queue = res.queue;
  renderQueue();
  updateStartState();
};
async function loadPlayerData(silent = false) {
  const button = $("loadQueue");
  if (button) button.textContent = "Syncing...";
  if ($("playerLoadStatus")) $("playerLoadStatus").textContent = "Syncing player from /api/player...";
  console.log("[WEBAPP][PLAYER] Sync started. Current tag:", $("playerTagInput")?.value || state.playerTag);
  try {
    const tagParam = encodeURIComponent($("playerTagInput")?.value || state.playerTag || "");
    const data = await api(`/api/player?t=${Date.now()}${tagParam ? `&tag=${tagParam}` : ""}`);
    console.log("[WEBAPP][PLAYER] Loaded raw:", data);

    playerName = data.player || data.name || "Player";
    playerTrophies = normalizeTrophyMap(data.trophies || data.brawlers || {});
    playerPowers = data.powers || {};

    if (data.tag) state.playerTag = data.tag;
    state.playerName = playerName;
    state.playerTrophies = playerTrophies;

    $("playerName").textContent = playerName;
    $("playerTag").textContent = state.playerTag || "";
    $("playerTagInput").value = state.playerTag || $("playerTagInput").value || "";

    for (const b of state.brawlers || []) {
      const trophies = trophyFor(b);
      if (trophies === undefined) continue;
      const existing = state.queue.find(q => q.brawler === b.id);
      if (existing) existing.trophies = trophies;
    }

    const count = Object.keys(playerTrophies).length;
    if ($("playerLoadStatus")) $("playerLoadStatus").textContent = `Synced ${Math.floor(count / 2) || count} brawlers. Player: ${playerName}`;
    renderBrawlers();
    renderBrawlerEditor();
    renderQueue();
    persistActiveProfile();
    renderBrawlerInstanceTabs();
    console.log("[WEBAPP][PLAYER] UI updated:", { playerName, keys: Object.keys(playerTrophies).slice(0, 20) });
  } catch (err) {
    console.error("[WEBAPP][PLAYER] Sync failed:", err);
    if ($("playerLoadStatus")) $("playerLoadStatus").textContent = "Sync failed: " + err.message;
    if (!silent) alert(err.message);
    throw err;
  } finally {
    if (button) button.textContent = "Sync Player";
  }
}

$("loadQueue").onclick = () => loadPlayerData(false).catch(err => {
  console.error("[WEBAPP][PLAYER] Sync failed:", err);
  if ($("playerLoadStatus")) $("playerLoadStatus").textContent = "Sync failed: " + err.message;
  alert(err.message);
});

function initQueueResize() {
  const bar = $("queuebar");
  const handle = $("queueResizeHandle");
  if (!bar || !handle) return;

  const root = document.documentElement;
  const storageKey = "amethyst.queue.height";
  const minHeight = 86;
  const defaultHeight = 132;
  const maxHeight = () => Math.max(160, Math.min(360, Math.floor(window.innerHeight * 0.38)));

  function setHeight(value, save = true) {
    const height = Math.max(minHeight, Math.min(maxHeight(), Math.round(Number(value) || defaultHeight)));
    root.style.setProperty("--queue-height", `${height}px`);
    if (save) {
      try { localStorage.setItem(storageKey, String(height)); } catch (_) {}
    }
    return height;
  }

  try {
    const saved = Number(localStorage.getItem(storageKey));
    if (saved) setHeight(saved, true);
  } catch (_) {}

  let startY = 0;
  let startHeight = 0;
  let dragging = false;

  function stopResize() {
    if (!dragging) return;
    dragging = false;
    bar.classList.remove("resizing");
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    document.removeEventListener("pointermove", onMove);
    document.removeEventListener("pointerup", stopResize);
    document.removeEventListener("pointercancel", stopResize);
  }

  function onMove(ev) {
    if (!dragging) return;
    // Панель закреплена снизу: тащишь вверх -> высота растёт, вниз -> уменьшается.
    setHeight(startHeight - (ev.clientY - startY));
  }

  handle.addEventListener("dblclick", ev => {
    ev.preventDefault();
    setHeight(defaultHeight, true);
  });

  handle.addEventListener("pointerdown", ev => {
    if (window.matchMedia("(max-width: 900px)").matches) return;
    ev.preventDefault();
    dragging = true;
    startY = ev.clientY;
    startHeight = bar.getBoundingClientRect().height;
    bar.classList.add("resizing");
    document.body.style.cursor = "ns-resize";
    document.body.style.userSelect = "none";
    document.addEventListener("pointermove", onMove);
    document.addEventListener("pointerup", stopResize);
    document.addEventListener("pointercancel", stopResize);
  });

  window.addEventListener("resize", () => {
    const current = bar.getBoundingClientRect().height;
    setHeight(current, false);
  });
}

function bindPushAllUi() {
  const pushBtn = $("pushAllBtn");
  if (pushBtn) {
    pushBtn.onclick = (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      openPushAllModal();
    };
  } else {
    console.warn("[WEBAPP][PUSH_ALL] pushAllBtn not found at bind time");
  }
  const closeBtn = $("pushAllClose");
  if (closeBtn) closeBtn.onclick = closePushAllModal;
  const modal = $("pushAllModal");
  if (modal) modal.onclick = ev => { if (ev.target.id === "pushAllModal") closePushAllModal(); };
  document.querySelectorAll("[data-push-target]").forEach(btn => {
    btn.onclick = () => {
      if ($("pushAllTarget")) $("pushAllTarget").value = btn.dataset.pushTarget;
      applyPushAll(btn.dataset.pushTarget).catch(err => {
        console.error("[WEBAPP][PUSH_ALL] Failed:", err);
        if ($("pushAllStatus")) $("pushAllStatus").textContent = err.message;
      });
    };
  });
  const applyBtn = $("pushAllApply");
  if (applyBtn) applyBtn.onclick = () => {
    applyPushAll($("pushAllTarget")?.value || "1000").catch(err => {
      console.error("[WEBAPP][PUSH_ALL] Failed:", err);
      if ($("pushAllStatus")) $("pushAllStatus").textContent = err.message;
    });
  };
}

try { brawlersMultiMode = localStorage.getItem("amethyst.multi.brawlers.enabled") === "1"; } catch (_) {}
initQueueResize();
bindPushAllUi();
document.addEventListener("click", ev => {
  const push = ev.target.closest && ev.target.closest("#pushAllBtn");
  if (push) {
    ev.preventDefault();
    openPushAllModal();
  }
});
$("playerTagInput").onchange = async () => {
  if (brawlersMultiMode) {
    const profile = currentProfile();
    profile.playerTag = $("playerTagInput").value;
    state.playerTag = profile.playerTag;
    $("playerTag").textContent = state.playerTag;
    persistActiveProfile();
    await loadPlayerData(false);
    return;
  }
  const res = await api("/api/player-tag", { method: "POST", body: JSON.stringify({ player_tag: $("playerTagInput").value }) });
  state.playerTag = res.playerTag;
  $("playerTag").textContent = state.playerTag;
  console.log("[WEBAPP][PLAYER] Saved player tag:", state.playerTag);
  await loadPlayerData(false);
};
$("brawlersMultiMode")?.addEventListener("change", async ev => {
  brawlersMultiMode = !!ev.target.checked;
  try { localStorage.setItem("amethyst.multi.brawlers.enabled", brawlersMultiMode ? "1" : "0"); } catch (_) {}
  if (brawlersMultiMode) {
    await refreshMulti().catch(()=>{});
    const tabs = availableInstanceTabs();
    activeInstanceKey = tabs[0]?.key || "main";
  } else {
    activeInstanceKey = "main";
    state = await api("/api/state");
  }
  applyProfileToUi();
  selectedBrawler = null;
  renderBrawlers();
  renderQueue();
  renderBrawlerInstanceTabs();
});
init().catch(err => alert(err.message));
setInterval(() => {
  if (document.visibilityState === "visible" && state) {
    refreshRuntime().catch(() => {});
  }
}, 3000);


async function refreshMulti() {
  if (!$('multiGrid')) return;
  try {
    multiState = await api('/api/multi/state');
    renderMulti();
    renderBrawlerInstanceTabs();
  } catch (e) {
    $('multiGrid').innerHTML = `<div class="panel"><b>Multi-Instance error</b><p>${escapeHtml(e.message)}</p></div>`;
  }
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
}

function portForNextInstance() {
  const used = new Set((multiState.instances || []).filter(i => i.running).map(i => Number(i.port)).filter(Boolean));
  const ports = (multiState.devices || [])
    .filter(d => String(d.status || '').toLowerCase() === 'device')
    .map(d => Number(d.port)).filter(Boolean);
  if (selectedMultiPort && ports.includes(Number(selectedMultiPort))) return Number(selectedMultiPort);
  return ports.find(p => !used.has(p)) || ports[0] || null;
}

function renderMulti() {
  const devicesHost = $('multiDevices');
  if (devicesHost) {
    const devices = multiState.devices || [];
    const onlineDevices = devices.filter(d => String(d.status || '').toLowerCase() === 'device');
    devicesHost.innerHTML = onlineDevices.length ? onlineDevices.map(d => `
      <button type="button" class="device-pill ${Number(d.port) === Number(selectedMultiPort) ? 'active' : ''}" data-port="${d.port || ''}" title="Use this LDPlayer for Start Next">
        <b>${escapeHtml(d.emulator || 'ADB')}</b>
        <span>${escapeHtml(d.serial || '')}</span>
        <em>${Number(d.port) === Number(selectedMultiPort) ? 'selected' : escapeHtml(d.status || '')}</em>
      </button>`).join('') : `<div class="empty-devices">No online ADB devices. Start LDPlayer and press Scan ADB.</div>`;
    devicesHost.querySelectorAll('.device-pill[data-port]').forEach(btn => {
      btn.onclick = () => {
        const port = Number(btn.dataset.port || 0);
        if (!port) return;
        selectMultiPort(port);
      };
    });
  }
  const host = $('multiGrid');
  const instances = (multiState.instances || []).filter(inst => inst && (inst.running || inst.state === 'paused' || inst.state === 'running'));
  if (!instances.length) {
    host.innerHTML = `<div class="empty-card"><b>No active bot instances</b><p>Scan ADB, choose an LDPlayer tab, add a brawler to its queue, then press Start Next. Emulator chips above are only available ADB devices, not running bots.</p></div>`;
    if ($('multiLogTitle')) $('multiLogTitle').textContent = 'Select instance';
    if ($('multiLogs')) $('multiLogs').textContent = 'Start an instance to see logs here...';
    return;
  }
  host.innerHTML = instances.map(inst => {
    const running = !!inst.running;
    const paused = inst.state === 'paused';
    const stateClass = running ? (paused ? 'paused' : 'running') : 'stopped';
    const current = Number(inst.progressCurrent || 0);
    const target = Number(inst.progressTarget || 0);
    const pct = target > 0 ? Math.max(0, Math.min(100, current / target * 100)) : 0;
    return `
      <article class="mi-card ${stateClass} ${profileKeyForPort(inst.port) === activeInstanceKey ? 'selected' : ''}" data-instance="${inst.id}">
        <div class="mi-head">
          <div class="mi-title"><span class="mi-dot"></span><span class="mi-index">#${inst.id}</span><b>${escapeHtml(inst.name || ('LDPlayer #' + (inst.id - 1)))}</b><em>${escapeHtml(inst.state || 'stopped')}</em></div>
          <div class="mi-buttons">
            ${running ? `<button class="mini danger" data-mi="stop" data-id="${inst.id}">■ Stop</button>` : `<button class="mini" data-mi="start" data-id="${inst.id}" data-port="${inst.port || ''}">▶ Start</button>`}
            ${running && !paused ? `<button class="mini" data-mi="pause" data-id="${inst.id}">Ⅱ Pause</button>` : ''}
            ${running && paused ? `<button class="mini" data-mi="resume" data-id="${inst.id}">▶ Resume</button>` : ''}
            <button class="mini" data-mi="logs" data-id="${inst.id}">Logs</button>
          </div>
        </div>
        <div class="mi-sub">${escapeHtml(inst.emulator || 'LDPlayer')}:${escapeHtml(inst.port || '')} ${inst.pid ? ' PID ' + inst.pid : ''}</div>
        <div class="mi-metrics">
          <div class="metric wide-metric"><small>ACTIVE SESSION</small><b>${escapeHtml(inst.session || 'none')} · trophies</b><div class="progress"><span style="width:${pct}%"></span></div><b>${current} / ${target || 0}</b></div>
          <div class="metric"><small>CURRENT BRAWLER</small><b>${escapeHtml(inst.currentBrawler || 'none')}</b></div>
          <div class="metric"><small>STATE</small><b>${escapeHtml(inst.state || 'stopped')}</b></div>
          <div class="metric"><small>IPS</small><b>${Number(inst.ips || 0).toFixed(1)}</b></div>
        </div>
      </article>`;
  }).join('');
  document.querySelectorAll('[data-mi]').forEach(btn => btn.onclick = handleMultiAction);
}

async function handleMultiAction(ev) {
  const action = ev.currentTarget.dataset.mi;
  const id = Number(ev.currentTarget.dataset.id || 1);
  if (action === 'logs') return loadMultiLogs(id);
  const inst = (multiState.instances || []).find(x => Number(x.id) === id) || {};
  const port = Number(ev.currentTarget.dataset.port || inst.port || (5555 + (id - 1) * 2));
  const body = { id, port, queue: queueForPort(port) };
  await api(`/api/multi/${action}`, { method:'POST', body: JSON.stringify(body) });
  await refreshMulti();
}

async function loadMultiLogs(id) {
  selectedMultiLogId = id;
  const data = await api(`/api/multi/logs?id=${encodeURIComponent(id)}&t=${Date.now()}`);
  if ($('multiLogTitle')) $('multiLogTitle').textContent = `Instance #${id}`;
  if ($('multiLogs')) $('multiLogs').textContent = data.log || 'No logs yet.';
}

function setupMultiHub() {
  if (window.__multiHubBound) return;
  window.__multiHubBound = true;
  $('multiScan')?.addEventListener('click', async () => {
    const btn = $('multiScan');
    if (btn) btn.textContent = 'Scanning...';
    try {
      const scanned = await api('/api/multi/scan');
      multiState.devices = scanned.devices || [];
      const onlinePorts = (multiState.devices || []).filter(d => String(d.status || '').toLowerCase() === 'device').map(d => Number(d.port)).filter(Boolean);
      if (!selectedMultiPort || !onlinePorts.includes(Number(selectedMultiPort))) selectedMultiPort = onlinePorts[0] || null;
      renderMulti();
      renderBrawlerInstanceTabs();
    } finally {
      if (btn) btn.textContent = 'Scan ADB';
    }
  });
  $('multiStartNext')?.addEventListener('click', async () => {
    const btn = $('multiStartNext');
    if (btn) btn.textContent = 'Starting...';
    try {
      persistActiveProfile();
      const onlinePorts = (multiState.devices || []).filter(d => String(d.status || '').toLowerCase() === 'device').map(d => Number(d.port)).filter(Boolean);
      const activePort = selectedMultiPort || (activeInstanceKey.startsWith('ldp_') ? Number(activeInstanceKey.replace('ldp_', '')) : null);
      const port = onlinePorts.includes(Number(activePort)) ? Number(activePort) : portForNextInstance();
      if (!port) throw new Error('No online ADB device found. Start LDPlayer and press Scan ADB.');
      selectedMultiPort = port;
      try { localStorage.setItem('amethyst.multi.selectedPort', String(selectedMultiPort)); } catch (_) {}
      const instanceId = instanceIdForPort(port);
      await api('/api/multi/start', { method:'POST', body: JSON.stringify({ id: instanceId, port, queue: queueForPort(port) }) });
      await refreshMulti();
      await loadMultiLogs(instanceId).catch(()=>{});
    } finally {
      if (btn) btn.textContent = 'Start Next';
    }
  });
  $('multiStopAll')?.addEventListener('click', async () => { await api('/api/multi/stop-all', { method:'POST', body:'{}' }); await refreshMulti(); });
  $('multiPauseAll')?.addEventListener('click', async () => { await api('/api/multi/pause-all', { method:'POST', body:'{}' }); await refreshMulti(); });
  $('multiResumeAll')?.addEventListener('click', async () => { await api('/api/multi/resume-all', { method:'POST', body:'{}' }); await refreshMulti(); });
  $('multiCopyLog')?.addEventListener('click', async () => { const txt = $('multiLogs')?.textContent || ''; try { await navigator.clipboard.writeText(txt); } catch(e) {} });
  refreshMulti();
  if (!multiPollTimer) multiPollTimer = setInterval(() => {
    if (document.querySelector('#multi.view.active')) { refreshMulti(); if (selectedMultiLogId) loadMultiLogs(selectedMultiLogId); }
  }, 3000);
}
