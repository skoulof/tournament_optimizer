"use strict";
/* Badminton tournament planner - web interface (no framework, no build step). */

// ------------------------------------------------------------------ basics
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
let LANG = localStorage.getItem("lang") || "en";
if (!I18N[LANG]) LANG = "en";

function t(key, p = {}) {
  let s = (I18N[LANG] || {})[key] ?? I18N.en[key] ?? key;
  for (const [k, v] of Object.entries(p)) s = s.split("{" + k + "}").join(v);
  return s;
}
function applyI18n(root = document) {
  $$("[data-i18n]", root).forEach(el => { el.textContent = t(el.dataset.i18n); });
  $$("[data-i18n-placeholder]", root).forEach(el => { el.placeholder = t(el.dataset.i18nPlaceholder); });
  $$("[data-i18n-title]", root).forEach(el => { el.title = t(el.dataset.i18nTitle); });
  document.title = t("app.title");
  document.documentElement.lang = LANG;
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function toast(msg, isErr = false) {
  const d = document.createElement("div");
  d.className = "t" + (isErr ? " err" : "");
  d.textContent = msg;
  $("#toast").appendChild(d);
  setTimeout(() => d.remove(), isErr ? 8000 : 3500);
}
async function api(path, opts = {}) {
  const o = { ...opts };
  if (o.json !== undefined) {
    o.method = o.method || "POST";
    o.headers = { "Content-Type": "application/json" };
    o.body = JSON.stringify(o.json);
    delete o.json;
  }
  const r = await fetch(path, o);
  if (r.status === 401) { window.location.href = "/login"; throw new Error("Not logged in"); }
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = j.detail || msg; } catch (_) { /* not json */ }
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  const ct = r.headers.get("content-type") || "";
  return ct.includes("json") ? r.json() : r.text();
}
async function upload(path, file) {
  const fd = new FormData();
  fd.append("file", file);
  return api(path, { method: "POST", body: fd });
}
function downloadText(name, text, type = "text/plain") {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type }));
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
const guard = fn => async (...a) => { try { await fn(...a); } catch (e) { toast(e.message || String(e), true); } };

// formatting
const numOrNull = v => (v === "" || v === null || v === undefined ? null : Number(v));
const fmtDur = m => { m = Math.round(m ?? 0); return `${Math.floor(m / 60)}h${String(m % 60).padStart(2, "0")}`; };
const fmtHHMM = m => { m = Math.round(m); const d = Math.floor(m / 1440); m -= d * 1440;
  return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}${d ? "+" + d + "d" : ""}`; };
const hhmmToMin = s => { if (!s) return null; const [h, m] = String(s).split(":").map(Number); return h * 60 + m; };
const pct = x => (x == null ? "–" : Math.round(x * 100) + "%");
const spct = x => (x == null ? "–" : (x >= 0 ? "+" : "") + Math.round(x * 100) + "%");
const mins = x => (x == null ? "–" : Math.round(x) + " " + t("common.minutes"));
const gradeHtml = (g, v) => `<span class="grade ${esc(g)}">${esc(g)}</span>${v != null ? " " + v.toFixed(1) : ""}`;
const LOCALE = navigator.language || undefined;          // dates and numbers follow the browser
const fmtDate = iso => { try { return new Date(iso).toLocaleString(LOCALE, { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }); } catch (_) { return iso; } };

// ------------------------------------------------------------------ generic forms
// field: {k: "path.in.object", type, label, hint, step, min, ph (placeholder key), options: [[value, labelKey]]}
function renderFields(container, fields) {
  container.innerHTML = fields.map(f => {
    const id = container.id + "-" + f.k.replace(/\./g, "_");
    const hint = f.hint ? `<div class="hint">${esc(t(f.hint))}</div>` : "";
    if (f.type === "check") {
      return `<div class="field check"><label><input type="checkbox" id="${id}" data-k="${f.k}"> ${esc(t(f.label))}</label>${hint}</div>`;
    }
    let input;
    if (f.type === "select") {
      input = `<select id="${id}" data-k="${f.k}">${f.options.map(([v, l]) => `<option value="${esc(v)}">${esc(t(l))}</option>`).join("")}</select>`;
    } else {
      const type = f.type === "list" ? "text" : f.type;
      const attrs = [f.step ? `step="${f.step}"` : "", f.min !== undefined ? `min="${f.min}"` : "",
        f.ph ? `placeholder="${esc(t(f.ph))}"` : ""].join(" ");
      input = `<input type="${type}" id="${id}" data-k="${f.k}" ${attrs}>`;
    }
    return `<div class="field"><label for="${id}">${esc(t(f.label))}</label>${input}${hint}</div>`;
  }).join("");
}
const getPath = (o, p) => p.split(".").reduce((a, k) => (a == null ? undefined : a[k]), o);
function setPath(o, p, v) {
  const ks = p.split(".");
  let a = o;
  ks.slice(0, -1).forEach(k => { a[k] = a[k] ?? {}; a = a[k]; });
  a[ks[ks.length - 1]] = v;
}
function fillFields(container, fields, obj) {
  fields.forEach(f => {
    const el = $(`[data-k="${f.k}"]`, container);
    let v = getPath(obj, f.k);
    if (f.invert) v = !v;
    if (f.type === "check") el.checked = !!v;
    else if (f.type === "list") el.value = Array.isArray(v) ? v.join(",") : (v ?? "");
    else el.value = v ?? "";
  });
}
function readFields(container, fields) {
  const out = {};
  fields.forEach(f => {
    const el = $(`[data-k="${f.k}"]`, container);
    let v;
    if (f.type === "check") v = f.invert ? !el.checked : el.checked;
    else if (f.type === "number") v = numOrNull(el.value);
    else if (f.type === "list") v = el.value.split(/[,; ]+/).filter(Boolean).map(Number).filter(x => !isNaN(x));
    else v = el.value.trim();
    setPath(out, f.outKey || f.k, v);
  });
  return out;
}

// ------------------------------------------------------------------ tabs & language
function showTab(name) {
  $$(".tab").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  $$(".panel").forEach(p => p.classList.toggle("active", p.id === "tab-" + name));
  if (name === "history") loadHistory();
  if (name === "schedule") loadSetupRuns();
}

// ------------------------------------------------------------------ step 1: setup
const EVENT_FIELDS = [
  { k: "name", type: "text", label: "f.name" },
  { k: "start_time", type: "time", label: "f.start_time" },
  { k: "end_time", type: "time", label: "f.end_time", hint: "h.end_time" },
  { k: "courts", type: "number", label: "f.courts", min: 1 },
  { k: "match_duration", type: "number", label: "f.match_duration", min: 1, hint: "h.match_duration" },
  { k: "min_rest", type: "number", label: "f.min_rest", min: 0, hint: "h.min_rest" },
  { k: "convocation_lead", type: "number", label: "f.convocation_lead", min: 0 },
  { k: "convocation_rounding", type: "number", label: "f.convocation_rounding", min: 1 },
];
const RULE_FIELDS = [
  { k: "pool_sizes", type: "list", label: "f.pool_sizes", hint: "h.pool_sizes" },
  { k: "preferred_pool_size", type: "number", label: "f.preferred_pool_size", min: 2 },
  { k: "qualifiers_per_pool", type: "list", label: "f.qualifiers_per_pool", hint: "h.qualifiers_per_pool" },
  { k: "preferred_qualifiers", type: "number", label: "f.preferred_qualifiers", min: 1 },
  { k: "single_pool_max", type: "number", label: "f.single_pool_max", min: 2 },
  { k: "min_matches_per_team", type: "number", label: "f.min_matches_per_team", min: 1 },
  { k: "planning_efficiency", type: "number", label: "f.planning_efficiency", step: "0.01", hint: "h.planning_efficiency" },
  { k: "weights.pool_size", type: "number", label: "f.w_pool_size", step: "0.1", hint: "h.w_pool_size" },
  { k: "weights.qualifiers", type: "number", label: "f.w_qualifiers", step: "0.1", hint: "h.w_qualifiers" },
  { k: "weights.byes", type: "number", label: "f.w_byes", step: "0.1", hint: "h.w_byes" },
];
const S = { defaults: null, picks: {}, result: null, showAll: {}, run: null };

function seriesRow(s = {}) {
  const tr = document.createElement("tr");
  const names = Array.isArray(s.teams) ? s.teams : null;
  tr.innerHTML = `
    <td><input type="text" class="s-name" value="${esc(s.name ?? "")}"></td>
    <td><div class="teams-cell"><input type="number" min="2" class="s-teams" value="${names ? names.length : esc(s.teams ?? "")}" ${names ? "disabled" : ""}>
        <button class="btn small s-toggle" type="button"></button></div>
        <textarea class="s-names" placeholder="${esc(t("names.placeholder"))}" hidden>${esc(names ? names.join("\n") : "")}</textarea></td>
    <td><input type="number" min="1" class="s-dur" placeholder="${esc(t("h.series_duration"))}" value="${esc(s.match_duration ?? "")}"></td>
    <td><input type="number" min="1" class="s-pools" placeholder="${esc(t("h.series_pools"))}" value="${esc(s.pools ?? "")}"></td>
    <td><input type="number" min="1" class="s-qual" placeholder="${esc(t("h.series_pools"))}" value="${esc(s.qualifiers ?? "")}"></td>
    <td><select class="s-single">
        <option value="">${esc(t("sp.auto"))}</option><option value="true">${esc(t("sp.force"))}</option>
        <option value="false">${esc(t("sp.forbid"))}</option></select></td>
    <td><button class="icon-btn s-del" type="button" title="${esc(t("common.remove"))}">✕</button></td>`;
  $(".s-single", tr).value = s.single_pool === true ? "true" : s.single_pool === false ? "false" : "";
  const ta = $(".s-names", tr), num = $(".s-teams", tr), btn = $(".s-toggle", tr);
  const syncNames = () => {          // names win over the count; clear them to go back to a plain count
    const lines = ta.value.split("\n").map(x => x.trim()).filter(Boolean);
    if (lines.length) { num.value = lines.length; num.disabled = true; } else num.disabled = false;
    btn.textContent = lines.length ? `${t("names.toggle")} (${lines.length}) ${ta.hidden ? "▸" : "▾"}` : `${t("names.toggle")} ${ta.hidden ? "▸" : "▾"}`;
  };
  btn.onclick = () => { ta.hidden = !ta.hidden; syncNames(); };
  ta.oninput = () => { syncNames(); markStale(); };
  syncNames();
  $(".s-del", tr).onclick = () => { tr.remove(); markStale(); };
  return tr;
}
function readSeries() {
  return $$("#series-body tr").map(tr => {
    const s = { name: $(".s-name", tr).value.trim() };
    const ta = $(".s-names", tr);
    const lines = ta.value.split("\n").map(x => x.trim()).filter(Boolean);
    s.teams = lines.length ? lines : numOrNull($(".s-teams", tr).value);
    const dur = numOrNull($(".s-dur", tr).value), pools = numOrNull($(".s-pools", tr).value),
      qual = numOrNull($(".s-qual", tr).value), single = $(".s-single", tr).value;
    if (dur) s.match_duration = dur;
    if (pools) s.pools = pools;
    if (qual) s.qualifiers = qual;
    if (single) s.single_pool = single === "true";
    return s;
  });
}
function readSetup() {
  return { event: readFields($("#setup-event"), EVENT_FIELDS), setup: readFields($("#setup-rules"), RULE_FIELDS), series: readSeries() };
}
function fillSetup(cfg) {
  fillFields($("#setup-event"), EVENT_FIELDS, cfg.event);
  fillFields($("#setup-rules"), RULE_FIELDS, cfg.setup);
  const body = $("#series-body");
  body.innerHTML = "";
  (cfg.series || []).forEach(s => body.appendChild(seriesRow(s)));
}
function markStale() {
  if (S.result) { $("#proposals-stale").hidden = false; $("#proposals").classList.add("stale"); }
}

async function propose(scroll = false) {
  const res = await api("/api/setup/propose", { json: { config: readSetup(), picks: S.picks, no_fit: !$("#setup-fit").checked } });
  S.result = res;
  renderProposals();
  if (scroll) $("#proposals-card").scrollIntoView({ behavior: "smooth", block: "start" });
}
function renderProposals() {
  const res = S.result;
  if (!res) { $("#proposals-card").hidden = true; return; }
  $("#proposals-card").hidden = false;
  $("#proposals-stale").hidden = true;
  $("#proposals").classList.remove("stale");
  const e = res.estimate, ev = res.config.event;
  let html = `<p>${esc(t("setup.estimate", { matches: e.matches, court: fmtDur(e.court_minutes), lb: fmtDur(e.lower_bound),
    courts: ev.courts, day: fmtDur(e.estimated_day), eff: pct(e.efficiency) }))}</p>`;
  if (e.available) {
    html += `<p>${esc(t("setup.est_end", { end: e.estimated_end, limit: ev.end_time }))} <span class="badge ${e.fits ? "ok" : "bad"}">${esc(t(e.fits ? "setup.fits" : "setup.not_fit"))}</span>`;
    if (!e.fits) {
      html += " " + esc(e.longest_series > e.available ? t("setup.series_too_long", { d: fmtDur(e.longest_series) }) : t("setup.need_courts", { n: e.courts_needed }));
      if (e.impossible) html += " " + esc(t("setup.impossible"));
    }
    html += "</p>";
  }
  res.changes.forEach(c => { html += `<div class="banner warn">${esc(t("setup.changed", { series: c.series, from: c.from, to: c.to }))}</div>`; });
  $("#estimate").innerHTML = html;

  $("#proposals").innerHTML = res.series.map(s => {
    const all = S.showAll[s.name];
    let rows = all ? s.options : s.options.slice(0, 5);
    if (!rows.some(o => o.index === s.chosen)) rows = rows.concat([s.options[s.chosen - 1]]);
    const trs = rows.map(o => `
      <tr class="clickable ${o.index === s.chosen ? "chosen" : ""}" data-series="${esc(s.name)}" data-desc="${esc(o.description)}">
        <td><input type="radio" name="pick-${esc(s.name)}" ${o.index === s.chosen ? "checked" : ""}></td>
        <td class="num">${o.index}</td><td>${esc(o.description)}</td><td class="num">${o.matches}</td>
        <td class="num">${o.min_matches === o.max_matches ? o.min_matches : o.min_matches + "–" + o.max_matches}</td>
        <td class="num">${fmtDur(o.court_minutes)}</td><td class="num">${fmtDur(o.critical_path)}</td>
        <td class="num">${o.penalty.toFixed(2)}</td></tr>`).join("");
    const more = s.options.length > 5
      ? `<button class="btn small more" data-series="${esc(s.name)}">${esc(all ? t("setup.show_less") : t("setup.show_all", { n: s.options.length }))}</button>` : "";
    return `<div class="proposal"><h3>${esc(s.name)} <span class="muted">(${s.n_teams} ${esc(t("col.teams").toLowerCase())})</span>
        ${s.forced ? `<span class="badge">${esc(t("setup.forced"))}</span>` : ""}</h3>
      <div class="table-wrap"><table class="table"><thead><tr><th></th><th class="num">#</th><th>${esc(t("col.format"))}</th>
        <th class="num">${esc(t("col.matches"))}</th><th class="num">${esc(t("col.per_team"))}</th><th class="num">${esc(t("col.court_time"))}</th>
        <th class="num">${esc(t("col.min_length"))}</th><th class="num">${esc(t("col.penalty"))}</th></tr></thead><tbody>${trs}</tbody></table></div>${more}</div>`;
  }).join("");
  $$("#proposals tr.clickable").forEach(tr => {
    tr.onclick = guard(async () => { S.picks[tr.dataset.series] = tr.dataset.desc; await propose(); });
  });
  $$("#proposals .more").forEach(b => { b.onclick = () => { S.showAll[b.dataset.series] = !S.showAll[b.dataset.series]; renderProposals(); }; });
  $("#setup-report").textContent = res.report || "";
}
async function generate() {
  const name = $("#setup-run-name").value.trim();
  const r = await api("/api/setup/generate", { json: { config: readSetup(), picks: S.picks, no_fit: !$("#setup-fit").checked, name } });
  S.run = r.run_id;
  showGenerated(r.run_id, r.name);
  toast(t("common.saved"));
}
function showGenerated(runId, name) {
  const b = $("#generated");
  b.hidden = false;
  b.innerHTML = `<span>${esc(t("setup.generated", { name }))}</span>
    <a class="btn small" href="/api/runs/${runId}/files/tournament.json?download=1">${esc(t("setup.download_tournament"))}</a>
    <a class="btn small" href="/api/runs/${runId}/files/config.toml?download=1">${esc(t("setup.export"))}</a>
    <button class="btn small primary" id="to-schedule">${esc(t("setup.to_schedule"))}</button>`;
  $("#to-schedule").onclick = guard(async () => { await loadTournamentFromRun(runId); showTab("schedule"); });
}

// example generator
const EX_DISC = ["SH", "SD", "DH", "DD", "MX"];
const EX = { ranks: ["N1", "N2", "N3", "R4", "R5", "R6", "D7", "D8", "D9", "P10", "P11", "P12", "NC"], disciplines: {} };
function exampleFields() {
  const rankOpts = EX.ranks.map(r => [r, r]);
  return [
    { k: "best", type: "select", label: "ex.best", options: rankOpts },
    { k: "worst", type: "select", label: "ex.worst", options: rankOpts },
    { k: "ranks_per_series", type: "select", label: "ex.per_series", options: [["1", "1"], ["2", "2"], ["3", "3"]] },
    { k: "n_series", type: "number", label: "ex.n_series", min: 1, hint: "ex.n_series_hint" },
    { k: "min_teams", type: "number", label: "ex.min_teams", min: 2 },
    { k: "max_teams", type: "number", label: "ex.max_teams", min: 2 },
    { k: "seed", type: "number", label: "ex.seed", hint: "ex.seed_hint" },
    { k: "names", type: "check", label: "ex.names" },
  ];
}
function renderExample(values) {
  $("#ex-disc").innerHTML = EX_DISC.map(d => `<label><input type="checkbox" value="${d}" checked> <b>${d}</b>
    <span class="muted">${esc(EX.disciplines[d] || "")}</span></label>`).join("");
  renderFields($("#ex-fields"), exampleFields());
  fillFields($("#ex-fields"), exampleFields(), values || { best: "R4", worst: "NC", ranks_per_series: "2", n_series: 8,
    min_teams: 5, max_teams: 20, names: true, seed: "" });
}
async function generateExample() {
  const hasSeries = $$("#series-body .s-name").some(i => i.value.trim());
  if (hasSeries && !confirm(t("ex.confirm"))) return;
  const v = readFields($("#ex-fields"), exampleFields());
  v.ranks_per_series = Number(v.ranks_per_series);
  v.disciplines = $$("#ex-disc input:checked").map(i => i.value);
  const r = await api("/api/setup/example", { json: v });
  const body = $("#series-body");
  body.innerHTML = "";
  r.series.forEach(s => body.appendChild(seriesRow(s)));
  S.picks = {}; S.result = null; renderProposals();
  $("#generated").hidden = true;
  $("#example-result").textContent = t("ex.done", { n: r.series.length, t: r.teams });
}

// ------------------------------------------------------------------ step 2: schedule
const SCH_EVENT_FIELDS = [
  { k: "start_time", type: "time", label: "f.start_time" },
  { k: "end_time", type: "time", label: "f.end_time", hint: "h.end_time" },
  { k: "courts", type: "number", label: "f.courts", min: 1 },
  { k: "match_duration", type: "number", label: "f.match_duration", min: 1, hint: "h.match_duration" },
  { k: "min_rest", type: "number", label: "f.min_rest", min: 0, hint: "h.min_rest" },
  { k: "convocation_lead", type: "number", label: "f.convocation_lead", min: 0 },
  { k: "convocation_rounding", type: "number", label: "f.convocation_rounding", min: 1 },
];
const OPT_FIELDS = [
  { k: "solver", type: "select", label: "f.solver", hint: "h.solver", options: [["auto", "solver.auto"], ["heuristic", "solver.heuristic"], ["cpsat", "solver.cpsat"]] },
  { k: "time_limit", type: "number", label: "f.time_limit", min: 1, hint: "h.time_limit" },
  { k: "jobs", type: "number", label: "f.jobs", min: 1, ph: "common.auto", hint: "h.jobs" },
  { k: "workers", type: "number", label: "f.workers", min: 1, ph: "common.auto", hint: "h.workers" },
  { k: "seed", type: "number", label: "f.seed" },
  { k: "free_round_order", type: "check", label: "f.keep_round_order", invert: true },
];
const WEIGHT_FIELDS = [
  { k: "free_wait", type: "number", label: "f.free_wait", min: 0, hint: "h.free_wait" },
  { k: "makespan_weight", type: "number", label: "f.makespan_weight", step: "any", ph: "common.auto", hint: "h.makespan_weight" },
  { k: "overtime_weight", type: "number", label: "f.overtime_weight", step: "any", ph: "common.auto", hint: "h.overtime_weight" },
  { k: "ko_wait_weight", type: "number", label: "f.ko_wait_weight", step: "any", hint: "h.ko_wait_weight" },
  { k: "pool_spread_weight", type: "number", label: "f.pool_spread_weight", step: "any", hint: "h.pool_spread_weight" },
  { k: "series_spread_weight", type: "number", label: "f.series_spread_weight", step: "any", hint: "h.series_spread_weight" },
];
const SIM_FIELDS = [
  { k: "simulate", type: "number", label: "f.simulate", min: 0 },
  { k: "duration_cv", type: "number", label: "f.duration_cv", step: "0.05", min: 0, hint: "h.duration_cv" },
  { k: "max_advance", type: "number", label: "f.max_advance", min: 0, ph: "common.none", hint: "h.max_advance" },
  { k: "csv_sep", type: "select", label: "f.csv_sep", options: [[",", ", (comma)"], [";", "; (semicolon)"]] },
];
const R = { doc: null, sourceRun: null, sourceLabel: "", summary: null, run: null, timer: null, logOffset: 0 };

function pairRow(body, a = "", b = "") {
  const tr = document.createElement("tr");
  tr.innerHTML = `<td><input type="number" min="0" class="pa" value="${esc(a)}"></td>
    <td><input type="number" step="any" min="0" class="pb" value="${esc(b)}"></td>
    <td><button class="icon-btn" type="button" title="${esc(t("common.remove"))}">✕</button></td>`;
  $("button", tr).onclick = () => { tr.remove(); updateCurve(); };
  $$("input", tr).forEach(i => { i.oninput = updateCurve; });
  body.appendChild(tr);
}
const readPairs = body => $$("tr", body).map(tr => [numOrNull($(".pa", tr).value), numOrNull($(".pb", tr).value)])
  .filter(([a, b]) => a !== null && b !== null).sort((x, y) => x[0] - y[0]);
function fillPairs(body, pairs) {
  body.innerHTML = "";
  let list = pairs;
  if (typeof pairs === "string") list = pairs === "none" ? [] : pairs.split(",").map(p => p.split(":").map(Number));
  (list || []).forEach(([a, b]) => pairRow(body, a, b));
}
function readSettings() {
  const s = {
    event: readFields($("#sch-event"), SCH_EVENT_FIELDS),
    ...readFields($("#sch-optimizer"), OPT_FIELDS),
    ...readFields($("#sch-weights"), WEIGHT_FIELDS),
    ...readFields($("#sch-simulation"), SIM_FIELDS),
    wait_tiers: readPairs($("#tiers-body")),
    wait_steps: readPairs($("#steps-body")),
  };
  if (s.event.end_time === undefined) s.event.end_time = "";
  return s;
}
function fillSettings(s, withEvent = true) {
  if (withEvent && s.event && Object.keys(s.event).length) fillFields($("#sch-event"), SCH_EVENT_FIELDS, { ...readFields($("#sch-event"), SCH_EVENT_FIELDS), ...s.event });
  fillFields($("#sch-optimizer"), OPT_FIELDS, s);
  fillFields($("#sch-weights"), WEIGHT_FIELDS, s);
  fillFields($("#sch-simulation"), SIM_FIELDS, s);
  fillPairs($("#tiers-body"), s.wait_tiers);
  fillPairs($("#steps-body"), s.wait_steps);
  updateCurve();
}
function updateCurve() {
  const free = numOrNull($('#sch-weights [data-k="free_wait"]').value) ?? 0;
  const tiers = readPairs($("#tiers-body")), steps = readPairs($("#steps-body"));
  const cost = g => (g > free ? g - free : 0) + tiers.reduce((c, [b, w]) => c + (g > b ? w * (g - b) : 0), 0)
    + steps.reduce((c, [b, v]) => c + (g > b ? v : 0), 0);
  $("#curve").innerHTML = esc(t("sch.curve")) + " " + [30, 60, 90, 120, 150, 180, 240]
    .map(g => `${g} ${esc(t("common.minutes"))}: <b>${Math.round(cost(g)).toLocaleString(LOCALE)}</b>`).join(" · ");
  updateRates(free, tiers, steps);
}
function updateRates(free, tiers, steps) {
  const w = k => numOrNull($(`#sch-weights [data-k="${k}"]`).value);
  const teams = R.summary ? R.summary.teams : null;
  const mw = w("makespan_weight") ?? teams;
  const ot = w("overtime_weight") ?? (mw != null ? 10 * mw : null);
  const nf = v => (v == null ? t("rates.teams_unknown") : Math.round(v * 10) / 10).toLocaleString(LOCALE);
  const marginal = g => (g > free ? 1 : 0) + tiers.reduce((c, [b, x]) => c + (g > b ? x : 0), 0);
  const items = [
    t("rates.day", { v: nf(mw) }),
    t("rates.overtime", { v: nf(ot) }),
    t("rates.series", { v: nf(w("series_spread_weight")) }),
    t("rates.pool", { v: nf(w("pool_spread_weight")) }),
    t("rates.ko", { v: nf(w("ko_wait_weight")) }),
    t("rates.marginal", { list: [45, 75, 100, 150, 200].map(g => `${g} min: <b>${nf(marginal(g))}</b>`).join(", ") }),
    ...steps.map(([b, v]) => t("rates.step", { b, v: nf(v) })),
  ];
  let ex = "";
  if (mw != null && marginal(150) > 0) {
    const x = 10 * mw;
    ex = `<div>${esc(t("rates.example", { x: nf(x), y: nf(x / marginal(150)) }))}</div>`;
  }
  $("#rates").innerHTML = `<div>${esc(t("rates.title"))}</div><ul>${items.map(s => `<li>${s}</li>`).join("")}</ul>${ex}`;
}
function setTournament(doc, summary, sourceRun, label) {
  R.doc = doc; R.summary = summary; R.sourceRun = sourceRun; R.sourceLabel = label;
  const ev = summary.event;
  fillFields($("#sch-event"), SCH_EVENT_FIELDS, ev);
  const rows = summary.series.map(s => `<tr><td>${esc(s.name)}</td><td class="num">${s.teams}</td><td>${esc(s.format)}</td><td class="num">${s.matches}</td></tr>`).join("");
  const box = $("#tournament-summary");
  box.removeAttribute("data-i18n");
  box.classList.remove("muted");
  box.innerHTML = `<p><b>${esc(ev.name || "")}</b> · ${esc(t("sch.totals", { matches: summary.matches, teams: summary.teams }))}
      · <span class="muted">${esc(t("sch.source", { src: label }))}</span></p>
    <div class="table-wrap"><table class="table"><thead><tr><th>${esc(t("col.series"))}</th><th class="num">${esc(t("col.teams"))}</th>
      <th>${esc(t("col.format"))}</th><th class="num">${esc(t("col.matches"))}</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  if (!$("#sch-run-name").value) $("#sch-run-name").value = ev.name || "";
  updateCurve();
}
async function loadTournamentFromRun(runId) {
  const r = await api(`/api/runs/${runId}/tournament`);
  const runs = await api("/api/runs");
  const meta = runs.find(x => x.id === runId);
  setTournament(r.doc, r.summary, runId, meta ? meta.name : runId);
}
async function loadSetupRuns() {
  const runs = await api("/api/runs");
  const sel = $("#setup-runs");
  const keep = sel.value;
  sel.innerHTML = `<option value="">${esc(t("sch.choose_run"))}</option>` + runs.filter(r => r.type === "setup" && r.status === "done")
    .map(r => `<option value="${r.id}">${esc(r.name)} — ${esc(fmtDate(r.created))}</option>`).join("");
  sel.value = keep;
}
async function runSchedule() {
  if (!R.doc) { toast(t("sch.need_tournament"), true); return; }
  const body = { settings: readSettings(), name: $("#sch-run-name").value.trim() };
  if (R.sourceRun) body.source_run = R.sourceRun; else body.tournament = R.doc;
  const r = await api("/api/schedule/run", { json: body });
  toast(t("sch.submitted"));
  await showRun(r.run_id);
  $("#run-card").scrollIntoView({ behavior: "smooth", block: "start" });
}

// run progress & results
async function showRun(runId) {
  clearTimeout(R.timer);
  R.run = runId; R.logOffset = 0;
  $("#run-log").textContent = "";
  $("#results").hidden = true;
  await pollRun();
}
async function pollRun() {
  const runId = R.run;
  if (!runId) return;
  const meta = await api(`/api/runs/${runId}`);
  if (runId !== R.run) return;
  $("#run-card").hidden = false;
  $("#run-name").textContent = meta.name;
  const st = $("#run-status");
  st.className = "badge " + meta.status;
  st.textContent = meta.status === "queued" && meta.queue_position ? t("sch.queued", { n: meta.queue_position }) : t("status." + meta.status);
  const active = meta.status === "queued" || meta.status === "running";
  $("#run-cancel").hidden = !active;
  $("#run-stop").hidden = meta.status !== "running" || !!meta.stop_requested;
  $("#run-stopping").hidden = !(meta.status === "running" && meta.stop_requested);
  const limit = meta.time_limit || 60;
  let frac = 0, el = "";
  if (meta.started) {
    const end = meta.finished ? new Date(meta.finished) : new Date();
    const secs = Math.max(0, (end - new Date(meta.started)) / 1000);
    frac = meta.status === "done" ? 1 : Math.min(0.97, secs / (limit * 1.05 + 5));
    el = t("sch.elapsed", { e: fmtSecs(secs), l: fmtSecs(limit) });
  }
  $("#run-progress > div").style.width = Math.round(frac * 100) + "%";
  $("#run-elapsed").textContent = el;
  const err = $("#run-error");
  err.hidden = meta.status !== "failed";
  err.textContent = meta.error || "";
  try {
    const log = await api(`/api/runs/${runId}/log?offset=${R.logOffset}`);
    if (log.text) {
      const pre = $("#run-log");
      pre.textContent += log.text;
      pre.scrollTop = pre.scrollHeight;
    }
    R.logOffset = log.offset;
    // best objective so far = lowest "best N" / "objective(s) N" reported in the log
    const vals = [...$("#run-log").textContent.matchAll(/\b(?:best|objectives?) ([\d,]+)/g)]
      .map(m => Number(m[1].replace(/,/g, ""))).filter(v => v > 0);
    $("#run-best").textContent = vals.length && meta.status !== "done" ? t("sch.best_so_far", { v: Math.min(...vals).toLocaleString(LOCALE) }) : "";
  } catch (_) { /* no log yet */ }
  if (meta.status === "done" && meta.statistics) renderResults(meta);
  if (active) R.timer = setTimeout(guard(pollRun), 1500);
}
const fmtSecs = s => (s >= 60 ? `${Math.floor(s / 60)} min ${String(Math.round(s % 60)).padStart(2, "0")} s` : `${Math.round(s)} s`);

function tile(label, value, sub = "", bad = false) {
  return `<div class="tile${bad ? " bad" : ""}"><div class="tl">${esc(label)}</div><div class="tv">${value}</div><div class="ts">${esc(sub)}</div></div>`;
}
function renderResults(meta) {
  const st = meta.statistics, g = st.plan, info = st.info || {};
  $("#results").hidden = false;
  $("#results-note").hidden = !(info.solver || "").includes("stopped early");
  const limitMin = info.end_limit ? hhmmToMin(info.end_limit) - info.t0 : null;
  const over = limitMin != null && g.makespan > limitMin;
  const daySub = fmtDur(g.makespan) + (info.end_limit ? " · " + (over ? t("m.over", { d: fmtDur(g.makespan - limitMin) }) : t("m.limit", { l: info.end_limit })) : "");
  const tiles = [
    tile(t("m.rating"), gradeHtml(g.grade), t("m.rating_sub", { v: g.wait_index.toFixed(1) })),
    tile(t("m.day"), `${esc(g.day_start)}–${esc(g.day_end)}`, daySub, over),
    tile(t("m.court_use"), pct(g.court_use), t("m.busy_until", { t: g.full_until })),
  ];
  if (g.all) tiles.push(tile(t("m.mean_gap"), mins(g.all.mean), t("m.mean_gap_sub", { m: Math.round(g.all.median), p: Math.round(g.all.p90) })));
  tiles.push(tile(t("m.worst_gap"), mins(g.worst_any), Object.entries(g.over || {}).map(([b, n]) => `>${b}: ${n}`).join(" · ")));
  if (g.entry_wait) tiles.push(tile(t("m.ko"), mins(g.entry_wait.max), t("m.ko_sub", { m: Math.round(g.entry_exp.mean) })));
  if (g.rounds) tiles.push(tile(t("m.ko_rounds"), mins(g.rounds.max)));
  tiles.push(tile(t("m.compactness"), spct(g.series_stretch_rel), t("m.compactness_sub", { s: spct(g.series_stretch_rel), p: spct(g.pool_stretch_rel) })));
  tiles.push(tile(t("m.longest"), fmtDur(g.longest.span), t("m.longest_sub", { team: g.longest.team, idle: fmtDur(g.longest.idle) })));
  tiles.push(tile(t("m.on_site"), fmtDur(g.stay_out.mean), g.stay_fin ? t("m.on_site_sub", { f: fmtDur(g.stay_fin.mean) }) : ""));
  $("#tiles").innerHTML = tiles.join("");
  renderBreakdown(g);
  $("#hours").innerHTML = (g.hours || []).map(h => `<div class="h"><span class="pct">${pct(h.use)}</span>
    <div class="bar" style="height:${Math.round(h.use * 48)}px"></div><span>${esc(h.hour.slice(0, 2))}h</span></div>`).join("");

  const head = ["col.series", "col.teams", "col.matches", "col.pool_phase", "col.stretch", "col.last", "col.mean_gap",
    "col.worst_gap", "col.ko_wait", "col.ko_rounds", "col.longest_stay", "col.rating"];
  $("#series-results").innerHTML = `<thead><tr>${head.map((h, i) => `<th class="${i ? "num" : ""}">${esc(t(h))}</th>`).join("")}</tr></thead><tbody>` +
    st.series.map(s => `<tr><td>${esc(s.name)}</td><td class="num">${s.teams}</td><td class="num">${s.matches}</td>
      <td class="num">${esc(s.pools_start)}–${esc(s.pools_done)}</td><td class="num">${spct(s.stretch_rel)}</td><td class="num">${esc(s.last)}</td>
      <td class="num">${s.all ? Math.round(s.all.mean) : "–"}</td><td class="num">${Math.round(s.worst_any)}</td>
      <td class="num">${s.entry_wait ? Math.round(s.entry_wait.max) : "–"}</td><td class="num">${s.rounds ? Math.round(s.rounds.max) : "–"}</td>
      <td class="num">${fmtDur(s.span_fin ? s.span_fin.max : hhmmToMin(s.last) - hhmmToMin(s.first))}</td><td class="num">${gradeHtml(s.grade, s.wait_index)}</td></tr>`).join("") + "</tbody>";

  const sim = st.simulation;
  if (sim && sim.makespan) {
    const th = g.thresholds || [];
    const top = th[Math.min(2, th.length - 1)];
    const fmt = (k, v) => k === "makespan" ? fmtHHMM(info.t0 + v) : k === "court_use" ? pct(v)
      : k === "wait_index" ? v.toFixed(1) : ["stay_out_mean", "stay_fin_max", "longest"].includes(k) ? fmtDur(v)
      : k === "over_top" ? (Math.round(v * 10) / 10).toString() : mins(v);
    const keys = Object.keys(sim).filter(k => sim[k] && typeof sim[k] === "object" && "plan" in sim[k]);
    $("#sim-results").innerHTML = `<h3>${esc(t("sch.sim_title", { n: sim.runs }))}</h3><div class="table-wrap"><table class="table">
      <thead><tr><th>${esc(t("col.metric"))}</th><th class="num">${esc(t("col.plan"))}</th><th class="num">${esc(t("col.mean"))}</th>
      <th class="num">${esc(t("col.p90"))}</th><th class="num">${esc(t("col.worst"))}</th></tr></thead><tbody>` +
      keys.map(k => `<tr><td>${esc(k === "over_top" && top ? t("col.long_gaps", { t: top }) : t("sim." + k))}</td>
        ${["plan", "mean", "p90", "worst"].map(c => `<td class="num">${fmt(k, sim[k][c])}</td>`).join("")}</tr>`).join("") + "</tbody></table></div>";
  } else $("#sim-results").innerHTML = "";

  const src = `/api/runs/${meta.id}/files/schedule.html`;
  if ($("#timeline").dataset.src !== src) { $("#timeline").src = src; $("#timeline").dataset.src = src; }
  $("#timeline-open").href = src;
  $("#downloads").innerHTML = meta.files
    .map(f => `<a class="btn small" href="/api/runs/${meta.id}/files/${f}?download=1">${esc(f)}</a>`).join("") +
    `<a class="btn small primary" href="/api/runs/${meta.id}/zip">${esc(t("sch.download_all"))}</a>`;
}

const BD_KEYS = [["day_excess", "#7a8a84"], ["overtime", "#a32020"], ["pool_gaps", "#2a78d6"], ["ko_waits", "#1fa6b8"],
  ["tiers", "#d6532a"], ["steps", "#c9a227"], ["pool_spread", "#9b59b6"], ["series_spread", "#2fa36b"]];
function stackHtml(comp, mini = false) {
  const tot = BD_KEYS.reduce((s, [k]) => s + (comp[k] || 0), 0) || 1;
  return `<div class="stack${mini ? " mini" : ""}">` + BD_KEYS.filter(([k]) => comp[k] > 0.5).map(([k, c]) =>
    `<div style="width:${(100 * comp[k] / tot).toFixed(2)}%;background:${c}" title="${esc(t("bd." + k))}: ${Math.round(100 * comp[k] / tot)}% (${Math.round(comp[k]).toLocaleString(LOCALE)})"></div>`).join("") + "</div>";
}
function legendHtml(comp) {
  const tot = BD_KEYS.reduce((s, [k]) => s + (comp ? comp[k] || 0 : 0), 0) || 1;
  return `<div class="legend">` + BD_KEYS.filter(([k]) => !comp || comp[k] > 0.5).map(([k, c]) =>
    `<span style="--c:${c}">${esc(t("bd." + k))}${comp ? ` <b>${Math.round(100 * comp[k] / tot)}%</b> <span class="muted">(${Math.round(comp[k]).toLocaleString(LOCALE)})</span>` : ""}</span>`).join("") + "</div>";
}
function renderBreakdown(g) {
  const comp = g.components;
  if (!comp) { $("#breakdown").innerHTML = ""; return; }
  const fixed = Math.round(comp.day_length - comp.day_excess).toLocaleString(LOCALE);
  $("#breakdown").innerHTML = stackHtml(comp) + legendHtml(comp) + `<p class="muted small">${esc(t("bd.note", { v: fixed }))}</p>`;
}

// ------------------------------------------------------------------ history
let histTimer = null;
async function loadHistory() {
  clearTimeout(histTimer);
  const runs = await api("/api/runs");
  const table = $("#hist-table");
  if (!runs.length) { table.innerHTML = `<tr><td class="muted">${esc(t("hist.empty"))}</td></tr>`; return; }
  const th = "120";
  table.innerHTML = `<thead><tr><th></th><th>${esc(t("common.name"))}</th><th>${esc(t("col.type"))}</th><th>${esc(t("col.created"))}</th>
    <th>${esc(t("col.status"))}</th><th class="num">${esc(t("col.end"))}</th><th class="num">${esc(t("col.rating"))}</th>
    <th class="num">${esc(t("col.mean_gap"))}</th><th class="num">${esc(t("col.worst_gap"))}</th>
    <th class="num">${esc(t("col.long_gaps", { t: th }))}</th><th class="num">${esc(t("col.court_use"))}</th><th></th></tr></thead><tbody>` +
    runs.map(r => {
      const s = r.summary || {};
      const sched = r.type === "schedule";
      const status = r.status === "queued" && r.queue_position ? t("sch.queued", { n: r.queue_position }) : t("status." + r.status);
      const endTxt = sched ? (s.day_end || "–") : (s.estimated_end ? "~" + s.estimated_end : "–");
      const over = sched && s.end_limit && s.day_end && hhmmToMin(s.day_end) > hhmmToMin(s.end_limit);
      return `<tr data-id="${r.id}">
        <td><input type="checkbox" class="sel" data-type="${r.type}" data-status="${r.status}"></td>
        <td><span class="editable rn" title="${esc(t("hist.rename"))}">${esc(r.name)}</span></td>
        <td><span class="badge">${esc(t("type." + r.type))}</span></td><td>${esc(fmtDate(r.created))}</td>
        <td><span class="badge ${r.status}">${esc(status)}</span></td>
        <td class="num ${over ? "bad" : ""}">${esc(endTxt)}</td>
        <td class="num">${sched && s.grade ? gradeHtml(s.grade, s.wait_index) : "–"}</td>
        <td class="num">${sched && s.mean_gap != null ? Math.round(s.mean_gap) : "–"}</td>
        <td class="num">${sched && s.worst_gap != null ? Math.round(s.worst_gap) : "–"}</td>
        <td class="num">${sched && s.over_top != null ? s.over_top : "–"}</td>
        <td class="num">${sched && s.court_use != null ? pct(s.court_use) : "–"}</td>
        <td style="white-space:nowrap"><button class="btn small op">${esc(t("common.open"))}</button>
          <a class="btn small" href="/api/runs/${r.id}/zip">${esc(t("common.download"))}</a>
          <button class="icon-btn del" title="${esc(t("common.delete"))}">✕</button></td></tr>`;
    }).join("") + "</tbody>";
  $$("#hist-table tr[data-id]").forEach(tr => {
    const id = tr.dataset.id;
    $(".op", tr).onclick = guard(() => openRun(id));
    $(".del", tr).onclick = guard(async () => {
      if (!confirm(t("common.confirm_delete"))) return;
      await api(`/api/runs/${id}`, { method: "DELETE" });
      loadHistory();
    });
    $(".rn", tr).onclick = guard(async () => {
      const name = prompt(t("hist.rename"), $(".rn", tr).textContent);
      if (name === null) return;
      await api(`/api/runs/${id}`, { method: "PATCH", json: { name } });
      loadHistory();
    });
  });
  if (runs.some(r => r.status === "queued" || r.status === "running") && $("#tab-history").classList.contains("active")) {
    histTimer = setTimeout(guard(loadHistory), 3000);
  }
}
async function openRun(id) {
  const meta = await api(`/api/runs/${id}`);
  if (meta.type === "setup") {
    const res = meta.result;
    fillSetup(res.config);
    $("#setup-fit").checked = !meta.no_fit;
    S.picks = meta.picks || {};
    S.result = res;
    renderProposals();
    showGenerated(id, meta.name);
    showTab("setup");
  } else {
    await loadTournamentFromRun(id);
    fillSettings(meta.settings);
    $("#sch-run-name").value = meta.name;
    showTab("schedule");
    await showRun(id);
  }
}
async function compareRuns() {
  const ids = $$("#hist-table .sel:checked").filter(c => c.dataset.type === "schedule" && c.dataset.status === "done")
    .map(c => c.closest("tr").dataset.id);
  if (ids.length < 2) { toast(t("hist.compare_need"), true); return; }
  const metas = (await Promise.all(ids.map(id => api(`/api/runs/${id}`))))
    .sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
  const rows = [
    ["m.day", m => m.statistics.plan.makespan, (v, m) => m.statistics.plan.day_end, "low"],
    ["m.rating", m => m.statistics.plan.wait_index, (v, m) => gradeHtml(m.statistics.plan.grade, v), "low"],
    ["m.mean_gap", m => m.statistics.plan.all?.mean, v => mins(v), "low"],
    ["m.worst_gap", m => m.statistics.plan.worst_any, v => mins(v), "low"],
  ];
  const ths = metas[0].statistics.plan.thresholds || [];
  ths.forEach(b => rows.push([t("col.long_gaps", { t: b }), m => (m.statistics.plan.over || {})[String(b)], v => v ?? "–", "low", true]));
  rows.push(
    ["m.ko", m => m.statistics.plan.entry_wait?.max, v => mins(v), "low"],
    ["m.ko_rounds", m => m.statistics.plan.rounds?.max, v => mins(v), "low"],
    ["m.compactness", m => m.statistics.plan.series_stretch_rel, v => spct(v), "low"],
    ["m.court_use", m => m.statistics.plan.court_use, v => pct(v), "high"],
    ["m.on_site", m => m.statistics.plan.stay_out?.mean, v => fmtDur(v), "low"],
    ["m.longest", m => m.statistics.plan.longest?.span, v => fmtDur(v), "low"],
    ["m.sim_end", m => m.statistics.simulation?.makespan?.mean, (v, m) => v == null ? "–" : fmtHHMM(m.statistics.info.t0 + v), "low"],
    ["bd.row", () => null, (v, m) => m.statistics.plan.components ? stackHtml(m.statistics.plan.components, true) : "–", null],
    ["m.solver", () => null, (v, m) => esc(m.statistics.info?.solver || ""), null],
    ["m.time_limit", m => m.settings.time_limit, v => fmtSecs(v), null],
    ["m.settings", () => null, (v, m) => `<span class="small">${esc((m.statistics.info?.weights || "").replace(/^weights: /, ""))}</span>`, null],
  );
  const html = rows.map(([label, get, show, better, raw]) => {
    const vals = metas.map(get);
    const nums = vals.filter(v => typeof v === "number");
    const best = better && nums.length > 1 ? (better === "low" ? Math.min(...nums) : Math.max(...nums)) : null;
    return `<tr><td>${esc(raw ? label : t(label))}</td>` + metas.map((m, i) =>
      `<td class="num ${best !== null && vals[i] === best ? "best" : ""}">${show(vals[i], m)}</td>`).join("") + "</tr>";
  }).join("");
  $("#compare-table").innerHTML = `<thead><tr><th>${esc(t("col.metric"))}</th>${metas.map(m => `<th class="num">${esc(m.name)}<br><span class="muted small">${esc(fmtDate(m.created))}</span></th>`).join("")}</tr></thead><tbody>${html}</tbody>`;
  $("#compare-legend")?.remove();
  $("#compare-table").closest(".table-wrap").insertAdjacentHTML("afterend", `<div id="compare-legend">${legendHtml(null)}</div>`);
  $("#compare-card").hidden = false;
  $("#compare-card").scrollIntoView({ behavior: "smooth" });
}

// ------------------------------------------------------------------ sweep
const SWEEP_PARAMS = [
  ["series_spread_weight", "f.series_spread_weight"], ["pool_spread_weight", "f.pool_spread_weight"],
  ["makespan_weight", "f.makespan_weight"], ["overtime_weight", "f.overtime_weight"],
  ["ko_wait_weight", "f.ko_wait_weight"], ["free_wait", "f.free_wait"], ["tiers_scale", "sw.tiers_scale"],
  ["steps_scale", "sw.steps_scale"], ["event.courts", "f.courts"], ["time_limit", "f.time_limit"],
];
function sweepDefaults(param) {
  const s = readSettings();
  const r = x => Math.round(x * 100) / 100;
  let v;
  if (param === "tiers_scale" || param === "steps_scale") return "0.5, 1, 2, 4";
  if (param === "event.courts") { v = s.event.courts || 7; return [v - 1, v, v + 1, v + 2].filter(x => x > 0).join(", "); }
  if (param === "time_limit") return "30, 60, 120, 300";
  if (param === "free_wait") return "30, 45, 60, 75, 90";
  v = s[param];
  if (v == null && param === "makespan_weight") v = R.summary ? R.summary.teams : 80;
  if (v == null && param === "overtime_weight") v = 10 * (s.makespan_weight ?? (R.summary ? R.summary.teams : 80));
  if (!v) return "0, 1, 2, 5, 10";
  return [0, r(v / 2), r(v), r(2 * v), r(4 * v)].join(", ");
}
async function runSweep() {
  if (!R.doc) { toast(t("sch.need_tournament"), true); return; }
  const param = $("#sweep-param").value;
  const values = $("#sweep-values").value.split(/[,; ]+/).filter(Boolean).map(Number).filter(x => !isNaN(x));
  if (values.length < 2) { toast(t("sw.need_values"), true); return; }
  const base = $("#sch-run-name").value.trim() || (R.summary?.event?.name || "sweep");
  const label = t(SWEEP_PARAMS.find(p => p[0] === param)[1]);
  const ids = [];
  for (const v of values) {
    const s = readSettings();
    if (param === "tiers_scale") s.wait_tiers = s.wait_tiers.map(([b, w]) => [b, w * v]);
    else if (param === "steps_scale") s.wait_steps = s.wait_steps.map(([b, c]) => [b, c * v]);
    else if (param === "event.courts") s.event.courts = v;
    else s[param] = v;
    const body = { settings: s, name: `${base} · ${label} = ${v}` };
    if (R.sourceRun) body.source_run = R.sourceRun; else body.tournament = R.doc;
    ids.push((await api("/api/schedule/run", { json: body })).run_id);
  }
  localStorage.setItem("lastSweep", JSON.stringify(ids));
  const b = $("#sweep-done");
  b.hidden = false;
  b.innerHTML = `<span>${esc(t("sw.queued", { n: ids.length }))}</span><button class="btn small primary" id="sweep-go">${esc(t("sw.go"))}</button>`;
  $("#sweep-go").onclick = guard(async () => { showTab("history"); await loadHistory(); selectLastSweep(); });
}
function selectLastSweep() {
  let ids = [];
  try { ids = JSON.parse(localStorage.getItem("lastSweep") || "[]"); } catch (_) { /* ignore */ }
  $$("#hist-table tr[data-id]").forEach(tr => { $(".sel", tr).checked = ids.includes(tr.dataset.id); });
}
function renderSweepParams() {
  const sel = $("#sweep-param");
  const keep = sel.value;
  sel.innerHTML = SWEEP_PARAMS.map(([k, l]) => `<option value="${k}">${esc(t(l))}</option>`).join("");
  sel.value = keep || "series_spread_weight";
  if (!$("#sweep-values").value) $("#sweep-values").value = sweepDefaults(sel.value);
}

// ------------------------------------------------------------------ init
function renderForms() {
  renderFields($("#setup-event"), EVENT_FIELDS);
  renderFields($("#setup-rules"), RULE_FIELDS);
  renderFields($("#sch-event"), SCH_EVENT_FIELDS);
  renderFields($("#sch-optimizer"), OPT_FIELDS);
  renderFields($("#sch-weights"), WEIGHT_FIELDS);
  renderFields($("#sch-simulation"), SIM_FIELDS);
  renderExample();
  renderSweepParams();
}
function wire() {
  $$(".tab").forEach(b => { b.onclick = guard(() => showTab(b.dataset.tab)); });
  // step 1
  $("#setup-import").onclick = () => $("#setup-file-input").click();
  $("#setup-file-input").onchange = guard(async e => {
    const f = e.target.files[0];
    if (!f) return;
    const cfg = await upload("/api/setup/parse", f);
    fillSetup(cfg);
    S.picks = {}; S.result = null; S.showAll = {};
    renderProposals();
    $("#generated").hidden = true;
    $("#setup-file").textContent = t("common.loaded", { name: f.name });
    e.target.value = "";
  });
  $("#setup-export").onclick = guard(async () => {
    const cfg = readSetup();
    const txt = await api("/api/setup/toml", { json: { config: cfg } });
    downloadText((cfg.event.name || "event").replace(/[^\w-]+/g, "_") + ".toml", txt, "application/toml");
  });
  $("#setup-reset").onclick = () => {
    if (!confirm(t("common.confirm_reset"))) return;
    fillSetup(S.defaults.setup);
    S.picks = {}; S.result = null; renderProposals();
    $("#generated").hidden = true;
    $("#setup-file").textContent = "";
  };
  $("#add-series").onclick = () => { $("#series-body").appendChild(seriesRow({ name: "", teams: 8 })); markStale(); };
  $("#tab-setup").addEventListener("input", e => { if (!e.target.closest("#proposals-card")) markStale(); });
  $("#tab-setup").addEventListener("change", e => { if (!e.target.closest("#proposals-card") && e.target.type !== "file") markStale(); });
  $("#propose").onclick = guard(() => propose(true));
  $("#reset-picks").onclick = guard(async () => { S.picks = {}; await propose(); });
  $("#generate").onclick = guard(generate);
  // step 2
  $("#tournament-import").onclick = () => $("#tournament-file-input").click();
  $("#tournament-file-input").onchange = guard(async e => {
    const f = e.target.files[0];
    if (!f) return;
    const r = await upload("/api/schedule/parse-tournament", f);
    setTournament(r.doc, r.summary, null, f.name);
    e.target.value = "";
  });
  $("#load-setup-run").onclick = guard(async () => { const id = $("#setup-runs").value; if (id) await loadTournamentFromRun(id); });
  $("#settings-import").onclick = () => $("#settings-file-input").click();
  $("#settings-file-input").onchange = guard(async e => {
    const f = e.target.files[0];
    if (!f) return;
    const s = await upload("/api/schedule/parse-settings", f);
    fillSettings(s);
    $("#settings-file").textContent = t("common.loaded", { name: f.name });
    e.target.value = "";
  });
  $("#settings-export").onclick = () => downloadText("scheduler-settings.json", JSON.stringify({ settings: readSettings() }, null, 1), "application/json");
  $("#settings-reset").onclick = () => {
    if (!confirm(t("common.confirm_reset"))) return;
    fillSettings(S.defaults.settings, false);
    if (R.summary) fillFields($("#sch-event"), SCH_EVENT_FIELDS, R.summary.event);
    $("#settings-file").textContent = "";
  };
  $("#add-tier").onclick = () => { pairRow($("#tiers-body")); };
  $("#add-step").onclick = () => { pairRow($("#steps-body")); };
  $("#sch-weights").addEventListener("input", updateCurve);
  $("#run-schedule").onclick = guard(runSchedule);
  $("#run-cancel").onclick = guard(async () => { await api(`/api/runs/${R.run}/cancel`, { method: "POST" }); await pollRun(); });
  $("#run-stop").onclick = guard(async () => { await api(`/api/runs/${R.run}/stop`, { method: "POST" }); await pollRun(); });
  // history
  $("#hist-refresh").onclick = guard(loadHistory);
  $("#hist-select-sweep").onclick = selectLastSweep;
  $("#example-toggle").onclick = () => { $("#example-card").hidden = !$("#example-card").hidden; };
  $("#example-close").onclick = () => { $("#example-card").hidden = true; };
  $("#example-generate").onclick = guard(generateExample);
  $("#sweep-param").onchange = () => { $("#sweep-values").value = sweepDefaults($("#sweep-param").value); };
  $("#sweep-run").onclick = guard(runSweep);
  $("#hist-compare").onclick = guard(compareRuns);
  $("#hist-delete").onclick = guard(async () => {
    const ids = $$("#hist-table .sel:checked").map(c => c.closest("tr").dataset.id);
    if (!ids.length || !confirm(t("common.confirm_delete_n", { n: ids.length }))) return;
    for (const id of ids) { try { await api(`/api/runs/${id}`, { method: "DELETE" }); } catch (e) { toast(e.message, true); } }
    loadHistory();
  });
  // language
  const langs = Object.keys(I18N);
  const sel = $("#lang");
  sel.innerHTML = langs.map(l => `<option value="${l}">${esc(I18N[l]["lang.name"] || l)}</option>`).join("");
  sel.value = LANG;
  sel.hidden = langs.length < 2;
  sel.onchange = () => {
    const cfg = readSetup(), settings = readSettings();
    LANG = sel.value;
    localStorage.setItem("lang", LANG);
    applyI18n();
    renderForms();
    fillSetup(cfg);
    fillSettings(settings);
    renderProposals();
    if (R.summary) setTournament(R.doc, R.summary, R.sourceRun, R.sourceLabel);
    if (R.run) pollRun();
    if ($("#tab-history").classList.contains("active")) loadHistory();
  };
}
async function init() {
  try {
    const opts = await api("/api/setup/example-options");
    EX.ranks = opts.ranks; EX.disciplines = opts.disciplines;
  } catch (_) { /* defaults */ }
  applyI18n();
  renderForms();
  wire();
  api("/api/session").then(s => { $("#logout").hidden = !s.auth; }).catch(() => {});
  S.defaults = await api("/api/defaults");
  fillSetup(S.defaults.setup);
  fillSettings(S.defaults.settings, false);
}
init().catch(e => toast(e.message || String(e), true));
