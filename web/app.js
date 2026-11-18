import { Scene3D } from "./scene3d.js";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const h = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "style") el.style.cssText = v;
    else if (v !== false && v !== null && v !== undefined) el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false) el.append(kid);
  return el;
};
const esc = (s) => String(s ?? "");
const fmtTime = (s) => (s == null ? "–" : s < 60 ? s.toFixed(2) + "s" : `${Math.floor(s / 60)}:${(s % 60).toFixed(1).padStart(4, "0")}`);

// ------------------------------------------------------------------ state
let ws = null;
let catalog = { tracks: {}, racers: {}, modes: {}, garage: {}, adverts: [] };
let state = { cars: [], game: null, track_key: null, scan: null };
let currentTrackKey = undefined;
const scene = new Scene3D($("#view"));
const selected = new Set(JSON.parse(localStorageGet("odhub.selected") || "[]"));
let editingRacer = null, editingMode = null;

function localStorageGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function localStorageSet(k, v) { try { localStorage.setItem(k, v); } catch { /* ignore */ } }

function send(op, data = {}) {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify({ op, ...data }));
}

function connect() {
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = () => { $("#conn").textContent = "engine online"; $("#conn").className = "pill ok"; };
  ws.onclose = () => { $("#conn").textContent = "reconnecting…"; $("#conn").className = "pill bad"; setTimeout(connect, 1000); };
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === "state") onState(m);
    else if (m.type === "catalog") onCatalog(m);
    else if (m.type === "notice") toast(m.text, m.level);
    else if (m.type === "game_event") onGameEvent(m);
  };
}

function toast(text, level = "info") {
  const el = h("div", { class: `toast ${level}` }, text);
  $("#toasts").append(el);
  setTimeout(() => el.remove(), level === "error" ? 9000 : 4500);
  while ($("#toasts").children.length > 5) $("#toasts").firstChild.remove();
}

// ------------------------------------------------------------------ tabs
$$("#tabs button").forEach((b) => b.addEventListener("click", () => {
  $$("#tabs button").forEach((x) => x.classList.toggle("active", x === b));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${b.dataset.tab}`));
}));
$$("#view-buttons button").forEach((b) => b.addEventListener("click", () => {
  $$("#view-buttons button").forEach((x) => x.classList.toggle("active", x === b));
  scene.setCamera(b.dataset.cam);
}));

// ------------------------------------------------------------------ incoming
function onCatalog(c) {
  catalog = c;
  renderGarage();
  renderTracks();
  renderModes();
  renderBuilder();
  if (c.ble_error) $("#garage-hint").innerHTML = `<span style="color:var(--err)">${esc(c.ble_error)}</span>`;
  applyTrack(true);
  if (editorKey && !$("#track-editor").classList.contains("hidden")) openTrackEditor(editorKey);
}

let lastCarsSig = "";
function onState(s) {
  state = s;
  if (s.scan && s.scan.track) {
    // place the scanning car on the newest piece of the live track
    s = { ...s, cars: s.cars.map((c) => c.address === s.scan.car
      ? { ...c, index: s.scan.index, frac: s.scan.frac, direction: 1, offset: 0, delocalized: false }
      : { ...c, index: null }) };
  }
  scene.syncCars(s.cars);
  scene.highlight(s.scan && s.scan.track ? s.scan.index : null);
  scene.setGame(s.game);
  applyTrack(false);
  // re-render car-dependent panels only when the set of cars or their settings change
  const sig = JSON.stringify(s.cars.map((c) => [c.address, c.nickname, c.color, c.connected, c.controller]));
  if (sig !== lastCarsSig) { lastCarsSig = sig; renderGarage(); renderParticipants(); renderScanCars(); }
  updateLive();
  renderBoard();
  renderScan();
}

let scanRev = -1;
function applyTrack(force) {
  // live scan: draw the track as the car discovers it
  const scan = state.scan;
  if (scan && scan.track) {
    if (scan.rev !== scanRev || currentTrackKey !== "__scan") {
      scanRev = scan.rev;
      currentTrackKey = "__scan";
      scene.setTrack(scan.track, { live: true });
      $("#track-name").textContent = `Scanning… ${scan.pieces} pieces${scan.closed ? " · loop closed, checking lap 2" : ""}`;
      $("#empty-hint").classList.add("hidden");
    }
    return;
  }
  if (!force && state.track_key === currentTrackKey) return;
  const t = catalog.tracks[state.track_key];
  if (state.track_key && !t) return;          // catalog not here yet: try again on the next update
  currentTrackKey = state.track_key;
  scene.setTrack(t || null);
  $("#track-name").textContent = t ? `Track: ${t.name} · ${t.pieces.length} pieces · ${(t.length_mm / 1000).toFixed(1)} m` +
    (t.pieces.some((p) => p.kind === "special") ? " · F&F bonus piece" : "") +
    (t.pieces.some((p) => p.kind === "crisscross") ? " · crossover" : "") +
    (t.start_reversed === true ? " · cars drive against the start line arrows" : "") : "";
  $("#empty-hint").classList.toggle("hidden", !!t);
  renderTracks();
}

// ------------------------------------------------------------------ garage
function controllerOptions(sel) {
  const opts = [h("option", { value: "none" }, "Not driving"), h("option", { value: "human" }, "🎮 Me (human)")];
  for (const [k, r] of Object.entries(catalog.racers)) {
    for (const d of ["easy", "normal", "hard", "extreme"]) {
      opts.push(h("option", { value: `ai:${k}:${d}` }, `🤖 ${r.name} · ${d}`));
    }
  }
  const s = h("select", {}, opts);
  s.value = sel || "none";
  return s;
}

function renderGarage() {
  const list = $("#car-list");
  list.replaceChildren(...state.cars.map((c) => {
    const ctrl = controllerOptions(c.controller);
    ctrl.addEventListener("change", () => send("set_controller", { car: c.address, spec: ctrl.value }));
    const name = h("input", { class: "name", value: c.nickname, title: "Rename" });
    name.addEventListener("change", () => send("update_car", { car: c.address, nickname: name.value }));
    const color = h("input", { type: "color", value: c.color, title: "Engine light colour" });
    color.addEventListener("change", () => send("update_car", { car: c.address, color: color.value }));
    return h("div", { class: "car", "data-car": c.address },
      h("span", { class: "dot", style: `color:${c.color};background:${c.color}` }),
      h("div", {}, name, h("div", { class: "meta", "data-live": "meta" })),
      h("div", { class: "ctrls" }, color,
        h("button", { class: "small", onclick: () => send("blink", { car: c.address }), title: "Flash this car's lights" }, "Blink"),
        h("button", { class: "small", onclick: () => send("disconnect", { car: c.address }) }, "✕")),
      h("div", { class: "extra" }, ctrl,
        h("button", { class: "small", onclick: () => send("uturn", { car: c.address }), title: "U-turn" }, "↶")));
  }));
  const connected = new Set(state.cars.map((c) => c.address));
  const adverts = catalog.adverts.filter((a) => !connected.has(a.address));
  $("#advert-list").replaceChildren(...(adverts.length ? adverts.map((a) => {
    const g = catalog.garage[a.address] || {};
    const name = h("input", { class: "name", value: g.nickname || a.model, title: "Rename (clear it to go back to the automatic name)" });
    name.addEventListener("change", () => send("update_car", { car: a.address, nickname: name.value }));
    const color = h("input", { type: "color", value: g.color || "#888888", title: "Engine light colour" });
    color.addEventListener("change", () => send("update_car", { car: a.address, color: color.value }));
    return h("div", { class: "advert" },
      h("div", { style: "flex:1;min-width:0" }, name,
        h("div", { class: "meta hint" }, h("span", { class: "tag" }, a.tag), ` ${a.model}${a.simulated ? " · simulated" : ""}`,
          chargeBadge(a.charge))),
      color,
      h("button", { class: "small", onclick: () => send("connect", { car: a.address }) }, "Connect"));
  }) : [h("p", { class: "hint" }, "Nothing found yet.")]));
  renderRoster();
}

function chargeBadge(ch) {
  if (!ch) return null;
  if (ch.error || ch.on_charger == null) return h("span", { class: "charge bad" }, " · check failed");
  const batt = ch.battery != null ? ` ${ch.battery}%` : "";
  if (ch.on_charger) return h("span", { class: `charge ${ch.full ? "full" : "on"}` }, ch.full ? ` · 🔌 charged${batt}` : ` · 🔌 charging${batt}`);
  return h("span", { class: `charge ${ch.low ? "bad" : "off"}` }, ` · off charger${batt}${ch.low ? " (low)" : ""}`);
}

$("#btn-check-charge").addEventListener("click", () => send("check_charge"));

function renderRoster() {
  const counts = {};
  for (const g of Object.values(catalog.garage)) counts[g.model || "Car"] = (counts[g.model || "Car"] || 0) + 1;
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const checked = catalog.adverts.filter((a) => a.charge && a.charge.on_charger != null);
  const charging = checked.filter((a) => a.charge.on_charger).length;
  const chargeLine = checked.length
    ? h("div", { class: "hint" }, `🔌 ${charging} on a charger, ${checked.length - charging} off (of ${checked.length} checked)`) : null;
  $("#roster").replaceChildren(chargeLine || "",
    h("div", { class: "hint" }, `${total} car(s) remembered: `,
      ...Object.entries(counts).sort().map(([m, n]) => h("span", { class: "tag", style: "margin:2px" }, `${n}× ${m}`))));
}

function updateLive() {
  for (const c of state.cars) {
    const el = $(`.car[data-car="${CSS.escape(c.address)}"] [data-live=meta]`);
    if (!el) continue;
    const b = c.battery;
    const battColor = b == null ? "var(--muted)" : b < 20 ? "var(--err)" : b < 45 ? "var(--warn)" : "var(--ok)";
    const where = c.delocalized ? "off track" : c.index != null ? `piece ${c.index + 1}` : c.piece_id ? `id ${c.piece_id}` : "not on track";
    el.innerHTML = `<span class="tag">${c.tag}</span> <span class="batt"><i style="width:${b ?? 0}%;background:${battColor}"></i></span>${b ?? "?"}%` +
      `${c.on_charger ? (c.battery_full ? " · 🔌 charged" : " · 🔌 charging") : ""}${c.battery_low ? " · <b style='color:var(--err)'>low</b>" : ""} · ${c.model} · ${c.speed} mm/s · ${where}${c.connected ? "" : " · <b style='color:var(--err)'>offline</b>"}`;
  }
}

$("#btn-discover").addEventListener("click", (e) => {
  e.target.disabled = true; e.target.textContent = "Searching…";
  send("discover", { seconds: 5 });
  setTimeout(() => { e.target.disabled = false; e.target.textContent = "Find cars"; }, 5500);
});
$("#btn-connect-all").addEventListener("click", () => send("connect_all"));
$("#btn-stop-all").addEventListener("click", () => { humanReset(); send("stop_all"); });

// ------------------------------------------------------------------ tracks
function renderScanCars() {
  const sel = $("#scan-car"), keep = sel.value;
  sel.replaceChildren(...state.cars.map((c) => h("option", { value: c.address }, c.nickname)));
  if (keep) sel.value = keep;
}
$("#btn-scan").addEventListener("click", () => {
  const car = $("#scan-car").value;
  if (!car) return toast("Connect a car first", "warn");
  send("scan_track", { car });
});
$("#btn-cancel-scan").addEventListener("click", () => send("cancel_scan"));

function renderScan() {
  const s = state.scan;
  $("#btn-scan").classList.toggle("hidden", !!s);
  $("#btn-cancel-scan").classList.toggle("hidden", !s);
  $("#scan-status").textContent = s ? `Scanning… ${s.pieces} pieces seen, lap ${Math.min(2, s.laps + 1)} of 2` : "";
}

function renderTracks() {
  const entries = Object.entries(catalog.tracks);
  $("#track-list").replaceChildren(...(entries.length ? entries.map(([k, t]) =>
    h("div", { class: `track ${k === state.track_key ? "active" : ""}`, onclick: () => send("select_track", { key: k }) },
      h("div", { class: "t-name" }, t.name, !t.valid ? h("span", { class: "warn-tag" }, "  ⚠ check turns") : null,
        h("div", { class: "t-meta" }, `${t.pieces.length} pieces · ${(t.length_mm / 1000).toFixed(1)} m · ${new Date(t.created * 1000).toLocaleDateString()}`)),
      h("button", { class: "small", onclick: (e) => { e.stopPropagation(); openTrackEditor(k); } }, "Edit"))
  ) : [h("p", { class: "hint" }, "No saved tracks yet.")]));
}

let editorKey = null;
function openTrackEditor(key) {
  editorKey = key;
  const t = catalog.tracks[key];
  const box = $("#track-editor");
  if (!t) return box.classList.add("hidden");
  const name = h("input", { value: t.name });
  const turns = {};
  const pieces = t.pieces.map((p, i) => {
    const label = { special: "F&F", crisscross: "+", start: p.reversed ? "start ←" : "start →" }[p.kind] || p.kind;
    const el = h("span", { class: `piece ${p.kind}`, title: `piece id ${p.id}` }, p.kind === "turn" ? `${i + 1}:${p.turn || "?"}` : `${i + 1}:${label}`);
    if (p.kind === "turn") el.addEventListener("click", () => {
      const cur = turns[i] || p.turn;
      turns[i] = cur === "R" ? "L" : "R";
      el.textContent = `${i + 1}:${turns[i]}`;
    });
    return el;
  });
  box.replaceChildren(
    h("h3", {}, "Edit track"), h("label", {}, "Name", name),
    h("p", { class: "hint" }, "If the 3D map does not match your track: try ", h("b", {}, "Mirror"),
      " first, then click individual turns to flip them left/right. ",
      t.valid ? "This layout is physically possible." : h("b", { style: "color:var(--warn)" }, "Pieces overlap or the loop does not close.")),
    h("div", { class: "piece-list" }, pieces),
    h("div", { class: "row" },
      h("button", { onclick: () => send("edit_track", { key, mirror: true }) }, "Mirror"),
      h("button", { onclick: () => send("edit_track", { key, fix: true }), title: "Pick the closest layout where nothing overlaps" }, "Auto-fix")),
    h("div", { class: "row" },
      h("button", { class: "primary", onclick: () => { send("edit_track", { key, name: name.value, turns }); box.classList.add("hidden"); } }, "Save"),
      h("button", { onclick: () => box.classList.add("hidden") }, "Close"),
      h("button", { onclick: () => { if (confirm(`Delete track "${t.name}"?`)) { send("delete_track", { key }); box.classList.add("hidden"); } } }, "Delete")));
  box.classList.remove("hidden");
}

// ------------------------------------------------------------------ play
function renderModes() {
  const sel = $("#mode-select"), keep = sel.value || "race";
  sel.replaceChildren(...Object.entries(catalog.modes).map(([k, m]) => h("option", { value: k }, m.name + (m.builtin ? "" : " (custom)"))));
  sel.value = catalog.modes[keep] ? keep : "race";
  modeChanged();
}
function modeChanged() {
  const m = catalog.modes[$("#mode-select").value];
  if (!m) return;
  $("#mode-desc").textContent = m.description || "";
  $("#ov-laps").value = m.laps; $("#ov-time").value = m.time_limit_s; $("#ov-cap").value = m.speed_cap;
}
$("#mode-select").addEventListener("change", modeChanged);

const seenCars = new Set(JSON.parse(localStorageGet("odhub.seen") || "[]"));
function renderParticipants() {
  for (const c of state.cars) {           // newly connected cars start ticked
    if (!seenCars.has(c.address)) { seenCars.add(c.address); selected.add(c.address); }
  }
  localStorageSet("odhub.seen", JSON.stringify([...seenCars]));
  $("#participants").replaceChildren(...(state.cars.length ? state.cars.map((c) => {
    const box = h("input", { type: "checkbox" });
    box.checked = selected.has(c.address);
    box.addEventListener("change", () => {
      box.checked ? selected.add(c.address) : selected.delete(c.address);
      localStorageSet("odhub.selected", JSON.stringify([...selected]));
    });
    const ctrl = controllerOptions(c.controller);
    ctrl.addEventListener("change", () => send("set_controller", { car: c.address, spec: ctrl.value }));
    return h("div", { class: "participant" }, box, h("span", { class: "dot", style: `background:${c.color}` }),
      h("span", { title: c.model, style: "overflow:hidden;text-overflow:ellipsis;white-space:nowrap" }, c.nickname), ctrl);
  }) : [h("p", { class: "hint" }, "Connect cars in the Garage first.")]));
}

$("#btn-fill-ai").addEventListener("click", () => {
  const roster = Object.keys(catalog.racers);
  let i = 0;
  for (const c of state.cars) {
    if (selected.has(c.address) && (c.controller === "none" || !c.controller)) {
      send("set_controller", { car: c.address, spec: `ai:${roster[i++ % roster.length]}:normal` });
    }
  }
});
$("#btn-start").addEventListener("click", () => {
  const cars = state.cars.filter((c) => selected.has(c.address) && c.connected).map((c) => c.address);
  send("start_game", { mode: $("#mode-select").value, cars, overrides: {
    laps: +$("#ov-laps").value, time_limit_s: +$("#ov-time").value, speed_cap: +$("#ov-cap").value } });
});
$("#btn-stop-game").addEventListener("click", () => { humanReset(); send("stop_game"); });

// ------------------------------------------------------------------ leaderboard
function renderBoard() {
  const g = state.game, board = $("#board");
  board.classList.toggle("hidden", !g);
  $("#countdown").classList.toggle("hidden", !(g && g.state === "countdown"));
  if (!g) { $("#game-banner").textContent = ""; return; }
  if (g.state === "countdown") $("#countdown").textContent = Math.ceil(g.countdown) || "GO";
  const carBy = Object.fromEntries(state.cars.map((c) => [c.address, c]));
  const remain = g.time_limit ? Math.max(0, g.time_limit - g.elapsed) : null;
  $("#game-banner").textContent = g.state === "finished"
    ? `🏁 ${g.winner ? (carBy[g.winner]?.nickname || "?") + " wins!" : "Game over"}`
    : `${g.name} · ${remain != null ? fmtTime(remain) + " left" : fmtTime(g.elapsed)}${g.laps ? ` · ${g.laps} laps` : ""}`;
  board.replaceChildren(h("div", { class: "hint" }, g.name), ...g.players.map((p) => {
    const c = carBy[p.address] || {};
    let stat;
    if (g.base === "battle") stat = `${p.knockouts} KO`;
    else if (g.base === "king") stat = `${p.score.toFixed(0)} pts${g.king === p.address ? " 👑" : ""}`;
    else if (g.base === "time_trial") stat = `best ${fmtTime(p.best_lap)}`;
    else stat = `lap ${Math.min(p.laps + 1, g.laps || 99)}${g.laps ? "/" + g.laps : ""} · ${fmtTime(p.last_lap)}`;
    if (p.finished) stat = "🏁 " + stat;
    const kids = [h("span", { class: "pos" }, p.out && !p.finished ? "✕" : p.place),
      h("span", { class: "dot", style: `width:10px;height:10px;border-radius:50%;background:${c.color}` }),
      h("span", {}, c.nickname || p.name), h("span", { class: "stat" }, stat)];
    if (g.weapons) kids.push(h("div", { class: "bar" },
      h("i", { style: `width:${(p.hp / p.max_hp) * 70}%;background:${p.disabled ? "#555" : "var(--ok)"}` }),
      h("i", { style: `width:${p.energy * 0.3}%;background:#3b82f6` })));
    return h("div", { class: `pl ${p.out && !p.finished ? "out" : ""}` }, kids);
  }));
}

function onGameEvent(ev) {
  if (ev.kind === "fire") scene.laser(ev.car, ev.hit, !!ev.hit);
  if (ev.kind === "lap") {
    const c = state.cars.find((x) => x.address === ev.car);
    if (c && c.controller === "human") toast(`${c.nickname}: lap ${ev.lap} in ${fmtTime(ev.time)}`);
  }
}

// ------------------------------------------------------------------ builder
const RACER_FIELDS = [
  ["top_speed", "Top speed (mm/s)", 400, 1800, 25], ["curve_speed", "Turn speed (mm/s)", 300, 1200, 25],
  ["accel", "Acceleration", 300, 2500, 50], ["aggression", "Aggression", 0, 1, 0.05],
  ["defense", "Blocking", 0, 1, 0.05], ["consistency", "Consistency", 0, 1, 0.05], ["trigger_happy", "Trigger happy", 0, 1, 0.05],
];
function renderBuilder() {
  $("#racer-list").replaceChildren(...Object.entries(catalog.racers).map(([k, r]) =>
    h("span", { class: `chip ${editingRacer === k ? "sel" : ""}`, style: `border-left:3px solid ${r.color}`, onclick: () => { editingRacer = k; renderBuilder(); } },
      r.name, r.builtin ? h("span", { class: "b" }, "built-in") : null)),
    h("span", { class: "chip", onclick: () => { editingRacer = "__new"; renderBuilder(); } }, "+ New racer"));
  const r = editingRacer === "__new" ? { name: "My Racer", color: "#00e5ff", lane: "center", top_speed: 1000, curve_speed: 720,
    accel: 900, aggression: 0.5, defense: 0.3, consistency: 0.85, trigger_happy: 0.5, description: "" } : catalog.racers[editingRacer];
  const rf = $("#racer-form");
  if (!r) { rf.replaceChildren(h("p", { class: "hint full" }, "Pick a racer to view or tweak it, or make a new one. Editing a built-in saves a copy.")); }
  else {
    const fields = RACER_FIELDS.map(([k, label, min, max, step]) => {
      const out = h("output", {}, r[k]);
      const inp = h("input", { type: "range", name: k, min, max, step, value: r[k] });
      inp.addEventListener("input", () => (out.textContent = inp.value));
      return h("label", {}, h("span", {}, label, " ", out), inp);
    });
    const lane = h("select", { name: "lane" }, ["center", "inside", "outside", "left", "right", "any"].map((x) => h("option", { value: x }, x)));
    lane.value = r.lane;
    rf.replaceChildren(
      h("label", {}, "Name", h("input", { name: "name", value: r.name })),
      h("label", {}, "Colour", h("input", { name: "color", type: "color", value: r.color })),
      h("label", { class: "full" }, "Description", h("input", { name: "description", value: r.description || "" })),
      ...fields, h("label", {}, "Lane habit", lane),
      h("div", { class: "row full" },
        h("button", { class: "primary", type: "submit" }, r.builtin ? "Save as copy" : "Save"),
        !r.builtin && editingRacer !== "__new" ? h("button", { type: "button", onclick: () => { send("delete_racer", { key: editingRacer }); editingRacer = null; } }, "Delete") : null));
    rf.onsubmit = (e) => {
      e.preventDefault();
      const d = Object.fromEntries(new FormData(rf));
      for (const [k] of RACER_FIELDS) d[k] = +d[k];
      send("save_racer", { data: d, key: editingRacer === "__new" ? null : editingRacer });
      toast(`Saved racer ${d.name}`, "success");
    };
  }

  $("#mode-list").replaceChildren(...Object.entries(catalog.modes).map(([k, m]) =>
    h("span", { class: `chip ${editingMode === k ? "sel" : ""}`, onclick: () => { editingMode = k; renderBuilder(); } },
      m.name, m.builtin ? h("span", { class: "b" }, "built-in") : null)),
    h("span", { class: "chip", onclick: () => { editingMode = "__new"; renderBuilder(); } }, "+ New mode"));
  const m = editingMode === "__new" ? { ...catalog.modes.race, name: "My Mode", description: "", builtin: false } : catalog.modes[editingMode];
  const mf = $("#mode-form");
  if (!m) { mf.replaceChildren(h("p", { class: "hint full" }, "Pick a mode to change its rules, or start from scratch.")); return; }
  const num = (k, label, step = 1) => h("label", {}, label, h("input", { name: k, type: "number", step, value: m[k] }));
  const base = h("select", { name: "base" }, ["race", "time_trial", "battle", "king", "elimination", "free"].map((x) => h("option", { value: x }, x)));
  base.value = m.base;
  const weapons = h("input", { type: "checkbox", name: "weapons" });
  weapons.checked = !!m.weapons;
  mf.replaceChildren(
    h("label", {}, "Name", h("input", { name: "name", value: m.name })), h("label", {}, "Based on", base),
    h("label", { class: "full" }, "Description", h("input", { name: "description", value: m.description || "" })),
    num("laps", "Laps"), num("time_limit_s", "Time limit (s)", 15), num("speed_cap", "Speed cap", 50), num("countdown_s", "Countdown (s)"),
    h("label", { class: "check full" }, weapons, "Weapons (blasters and mines)"),
    num("hp", "HP"), num("blaster_damage", "Blaster damage"), num("blaster_range_mm", "Blaster range (mm)", 50),
    num("fire_cost", "Energy per shot"), num("energy_regen", "Energy regen /s"), num("mine_damage", "Mine damage"),
    num("knockout_s", "Knockout time (s)"), num("score_to_win", "Score to win (0 = off)"), num("king_points_per_s", "King points /s", 0.5),
    h("div", { class: "row full" },
      h("button", { class: "primary", type: "submit" }, m.builtin ? "Save as copy" : "Save"),
      !m.builtin && editingMode !== "__new" ? h("button", { type: "button", onclick: () => { send("delete_mode", { key: editingMode }); editingMode = null; } }, "Delete") : null));
  mf.onsubmit = (e) => {
    e.preventDefault();
    const d = Object.fromEntries(new FormData(mf));
    for (const k of Object.keys(d)) if (!["name", "description", "base"].includes(k) && d[k] !== "") d[k] = +d[k];
    d.weapons = weapons.checked;
    send("save_mode", { data: d, key: editingMode === "__new" ? null : editingMode });
    toast(`Saved mode ${d.name}`, "success");
  };
}

// ------------------------------------------------------------------ human driving
const keys = new Set();
const human = {};       // address -> {throttle}
function humanCars() { return state.cars.filter((c) => c.controller === "human" && c.connected); }
function humanReset() { for (const k in human) human[k].throttle = 0; }
function typing() { const a = document.activeElement; return a && ["INPUT", "SELECT", "TEXTAREA"].includes(a.tagName); }

addEventListener("keydown", (e) => {
  if (e.key === "Escape") { humanReset(); send("stop_all"); return; }
  if (typing()) return;
  keys.add(e.key.toLowerCase());
  const car = humanCars()[0];
  if (!car || e.repeat) return;
  const k = e.key.toLowerCase();
  if (k === "a" || k === "arrowleft") send("input", { car: car.address, lane_step: -1 });
  if (k === "d" || k === "arrowright") send("input", { car: car.address, lane_step: 1 });
  if (k === " ") { e.preventDefault(); send("input", { car: car.address, fire: true }); }
  if (k === "m") send("input", { car: car.address, mine: true });
});
addEventListener("keyup", (e) => keys.delete(e.key.toLowerCase()));
addEventListener("blur", () => keys.clear());

const padPrev = {};
let lastSent = {};
setInterval(() => {
  const cars = humanCars();
  const pads = (navigator.getGamepads ? [...navigator.getGamepads()] : []).filter(Boolean);
  cars.forEach((car, i) => {
    const st = (human[car.address] ||= { throttle: 0 });
    const pad = pads[i];
    if (pad) {
      const rt = pad.buttons[7]?.value || 0, lt = pad.buttons[6]?.value || 0;
      st.throttle = lt > 0.1 ? 0 : rt;
      const prev = padPrev[pad.index] || {};
      const pressed = (b) => pad.buttons[b]?.pressed && !prev[b];
      const stickX = pad.axes[0] || 0;
      if (pressed(4) || pressed(14) || (stickX < -0.7 && !prev.left)) send("input", { car: car.address, lane_step: -1 });
      if (pressed(5) || pressed(15) || (stickX > 0.7 && !prev.right)) send("input", { car: car.address, lane_step: 1 });
      if (pressed(0)) send("input", { car: car.address, fire: true });
      if (pressed(1)) send("input", { car: car.address, mine: true });
      padPrev[pad.index] = Object.fromEntries(pad.buttons.map((b, k) => [k, b.pressed]));
      padPrev[pad.index].left = stickX < -0.7; padPrev[pad.index].right = stickX > 0.7;
    } else if (i === 0 && !typing()) {
      const up = keys.has("w") || keys.has("arrowup"), down = keys.has("s") || keys.has("arrowdown");
      if (keys.has("x")) st.throttle = 0;
      else if (up) st.throttle = Math.min(1, st.throttle + 0.04);
      else if (down) st.throttle = Math.max(0, st.throttle - 0.06);
    }
    const t = Math.round(st.throttle * 100) / 100;
    if (lastSent[car.address] !== t) { lastSent[car.address] = t; send("input", { car: car.address, throttle: t }); }
  });
}, 50);

connect();
