"use strict";
// Oberflaeche von atprog. Kommuniziert ausschliesslich mit dem lokalen Server.

const COLUMNS = {
  channels: [
    ["name", "Kanalname", "text", 150],
    ["rx", "RX (MHz)", "text", 100],
    ["tx", "TX (MHz)", "text", 100],
    ["ch_type", "Typ", ["A-Analog", "D-Digital", "A+D", "D+A"]],
    ["power", "Leistung", ["Turbo", "High", "Mid", "Low"]],
    ["bandwidth", "Bandbreite", ["12.5K", "25K"]],
    ["color_code", "CC", "num", 50],
    ["slot", "TS", ["1", "2"]],
    ["contact", "Kontakt", "text", 130],
    ["radio_id", "Radio-ID", "text", 110],
    ["rx_group", "RX-Gruppe", "text", 110],
    ["scan_list", "Scanliste", "text", 110],
    ["ctcss_decode", "CTCSS RX", "text", 80],
    ["ctcss_encode", "CTCSS TX", "text", 80],
    ["tx_permit", "Sendefreigabe", ["Always", "ChannelFree", "Different Colorcode", "Same Colorcode"]],
    ["ptt_prohibit", "Nur RX", ["Off", "On"]],
  ],
  talkgroups: [
    ["name", "Name", "text", 180],
    ["tg_id", "TG / DMR-ID", "num", 120],
    ["call_type", "Ruftyp", ["Group Call", "Private Call", "All Call"]],
    ["call_alert", "Rufton", ["None", "Ring", "Online Alert"]],
  ],
  radio_ids: [
    ["name", "Name", "text", 200],
    ["radio_id", "DMR-ID", "num", 140],
  ],
  zones: [
    ["name", "Zonenname", "text", 180],
    ["count", "Kanäle", "ro", 70],
    ["channels", "Mitglieder (mit | getrennt)", "list", 520],
    ["a_channel", "A-Kanal", "text", 140],
    ["b_channel", "B-Kanal", "text", 140],
  ],
  scanlists: [
    ["name", "Name", "text", 180],
    ["count", "Kanäle", "ro", 70],
    ["channels", "Mitglieder (mit | getrennt)", "list", 560],
  ],
  rxgroups: [
    ["name", "Name", "text", 180],
    ["count", "Kontakte", "ro", 80],
    ["contacts", "Mitglieder (mit | getrennt)", "list", 560],
  ],
};

const PAGE = 100;
const DB_PAGE = 200;
let dbState = { page: 0, query: "", total: 0 };
let state = { tab: "channels", page: 0, total: 0, query: "", rows: [], selected: new Set() };

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

async function api(path, body) {
  const opts = body === undefined ? {} :
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error("Serverfehler " + res.status);
  return res.json();
}

function toast(message, isError) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.toggle("err", !!isError);
  el.classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.add("hidden"), isError ? 6000 : 3000);
}

function ask(title, text, value) {
  return new Promise((resolve) => {
    const modal = $("#modal");
    $("#modal-title").textContent = title;
    $("#modal-text").textContent = text || "";
    const input = $("#modal-input");
    input.value = value || "";
    modal.classList.remove("hidden");
    input.focus();
    input.select();
    const finish = (val) => {
      modal.classList.add("hidden");
      modal.onclick = null;
      input.onkeydown = null;
      resolve(val);
    };
    modal.onclick = (ev) => {
      const act = ev.target.dataset.act;
      if (act === "modal-ok") finish(input.value.trim());
      else if (act === "modal-cancel" || ev.target === modal) finish(null);
    };
    input.onkeydown = (ev) => {
      if (ev.key === "Enter") finish(input.value.trim());
      if (ev.key === "Escape") finish(null);
    };
  });
}

function report(title, html) {
  $("#report-title").textContent = title;
  $("#report-body").innerHTML = html;
  $("#report").classList.remove("hidden");
}

// ---------------------------------------------------------------- Zustand
async function refreshState() {
  const st = await api("/api/state");
  $("#subtitle").textContent =
    `${st.app.version} · Projekt: ${st.project.name}${st.project.path ? " (" + st.project.path + ")" : ""}` +
    ` · Adressplan ${st.layout.id} r${st.layout.revision} · Port-Backend ${st.app.backend}`;
  for (const [key, value] of Object.entries(st.project.stats)) {
    const el = document.getElementById("n-" + key);
    if (el) el.textContent = value;
  }
  const sel = $("#port");
  const current = sel.value;
  sel.innerHTML = "";
  (st.ports.length ? st.ports : ["(kein Port gefunden)"]).forEach((p) => {
    const opt = document.createElement("option");
    opt.value = p; opt.textContent = p;
    sel.appendChild(opt);
  });
  if (current && st.ports.includes(current)) sel.value = current;
  $("#log").textContent = (st.log || []).join("\n");
  $("#siminfo").textContent = st.simulator ? "läuft auf " + st.simulator : "nicht aktiv";
  return st;
}

// ---------------------------------------------------------------- Tabelle
async function loadRows() {
  const kind = state.tab;
  if (!COLUMNS[kind]) return;
  const params = new URLSearchParams({
    kind, q: state.query, offset: state.page * PAGE, limit: PAGE,
  });
  const data = await api("/api/rows?" + params.toString());
  state.rows = data.rows;
  state.total = data.total;
  state.selected.clear();
  renderTable();
}

function renderTable() {
  const cols = COLUMNS[state.tab];
  const thead = $("#grid thead");
  const tbody = $("#grid tbody");
  thead.innerHTML = "<tr><th class='sel'></th><th>#</th>" +
    cols.map((c) => `<th>${c[1]}</th>`).join("") + "</tr>";
  tbody.innerHTML = "";
  for (const row of state.rows) {
    const tr = document.createElement("tr");
    tr.dataset.index = row.i;
    tr.innerHTML = `<td class="sel"><input type="checkbox" data-role="sel"></td>
                    <td class="muted">${row.i + 1}</td>` +
      cols.map(([key, , kind, width]) => cell(row, key, kind, width)).join("");
    tbody.appendChild(tr);
  }
  const from = state.total ? state.page * PAGE + 1 : 0;
  const to = Math.min(state.total, (state.page + 1) * PAGE);
  $("#pageinfo").textContent = `${from}–${to} von ${state.total}`;
}

function cell(row, key, kind, width) {
  let value = row[key];
  if (Array.isArray(value)) value = value.join(" | ");
  value = value === undefined || value === null ? "" : String(value);
  const esc = value.replace(/"/g, "&quot;");
  const style = width ? ` style="min-width:${width}px"` : "";
  if (kind === "ro") return `<td class="muted">${value}</td>`;
  if (Array.isArray(kind)) {
    const options = kind.map((o) =>
      `<option${String(o) === value ? " selected" : ""}>${o}</option>`).join("");
    return `<td><select data-key="${key}" data-orig="${esc}"${style}>${options}</select></td>`;
  }
  const cls = kind === "num" ? ' class="num"' : "";
  return `<td${cls}><input data-key="${key}" data-orig="${esc}" value="${esc}"${style}></td>`;
}

// Speichert eine Zelle, sobald sich ihr Wert gegenueber dem geladenen Stand
// unterscheidet. Wird sowohl von "change" als auch von "focusout" aufgerufen,
// damit keine Eingabe verloren geht.
async function saveCell(el) {
  const tr = el.closest("tr");
  if (!tr || el.value === el.dataset.orig) return;
  const key = el.dataset.key;
  const index = Number(tr.dataset.index);
  const previous = el.dataset.orig;
  el.dataset.orig = el.value;
  const res = await api("/api/row/save", { kind: state.tab, index, row: { [key]: el.value } });
  if (res.ok === false) {
    el.dataset.orig = previous;
    el.value = previous;
    toast(res.error, true);
    return;
  }
  el.classList.add("saved");
  setTimeout(() => el.classList.remove("saved"), 600);
  if (key === "channels" || key === "contacts") await loadRows();
  refreshState();
}

// ---------------------------------------------------------------- Aktionen
const ACTIONS = {
  async "project-new"() {
    const name = await ask("Neues Projekt", "Name des Projekts", "Neues Projekt");
    if (name === null) return;
    await api("/api/project/new", { name });
    await loadRows(); refreshState(); toast("Neues Projekt angelegt");
  },
  async "project-open"() {
    const path = await ask("Projekt öffnen", "Pfad zur .json-Projektdatei", "codeplug.json");
    if (!path) return;
    const res = await api("/api/project/open", { path });
    res.ok ? (await loadRows(), refreshState(), toast("Projekt geladen"))
           : toast(res.error, true);
  },
  async "project-save"() {
    const path = await ask("Projekt speichern", "Zieldatei (.json)", "codeplug.json");
    if (!path) return;
    const res = await api("/api/project/save", { path });
    res.ok ? toast("Gespeichert: " + res.path) : toast(res.error, true);
    refreshState();
  },
  async "csv-import"() {
    const dir = await ask("CPS-CSV einlesen",
      "Verzeichnis mit Channel.CSV, TalkGroups.CSV, Zone.CSV …", "csv");
    if (!dir) return;
    const res = await api("/api/csv/import", { dir });
    if (!res.ok) return toast(res.error, true);
    await loadRows(); refreshState();
    toast("Eingelesen: " + res.loaded.join(", "));
  },
  async "csv-export"() {
    const dir = await ask("CPS-CSV schreiben", "Zielverzeichnis", "csv");
    if (!dir) return;
    const res = await api("/api/csv/export", { dir });
    res.ok ? report("CSV geschrieben", `<p>${res.dir}</p><ul>` +
        res.files.map((f) => `<li>${f}</li>`).join("") + "</ul>")
      : toast(res.error, true);
  },
  async "csv-userdb"() {
    const path = await ask("DMR-ID-Liste einlesen",
      "CSV von radioid.net (Spalten RADIO_ID, CALLSIGN, NAME, COUNTRY)", "user.csv");
    if (!path) return;
    const country = await ask("Auf ein Land beschränken?",
      "Landesname wie in der CSV, z. B. Germany — leer lassen für alle", "Germany");
    if (country === null) return;
    const res = await api("/api/csv/userdb", { path, country, limit: 0 });
    if (!res.ok) return toast(res.error, true);
    await loadRows(); refreshState();
    report("DMR-ID-Liste eingelesen",
      `<p>${res.added} Kontakte ergänzt${res.skipped ? ` · ${res.skipped} übersprungen` : ""}.</p>
       <p class="muted">Insgesamt jetzt ${res.total} Kontakte (Gerätegrenze 10 000).</p>`);
  },

  async validate() {
    const res = await api("/api/validate", {});
    const list = (items, cls) => items.length
      ? `<ul class="${cls}">` + items.slice(0, 300).map((t) => `<li>${t}</li>`).join("") + "</ul>"
      : "<p class='ok'>keine</p>";
    report("Prüfergebnis",
      `<h3>Fehler (${res.errors.length})</h3>${list(res.errors, "err")}
       <h3>Warnungen (${res.warnings.length})</h3>${list(res.warnings, "")}`);
  },
  async "row-add"() {
    const res = await api("/api/row/add", { kind: state.tab, row: {} });
    if (!res.ok) return toast(res.error, true);
    state.page = Math.floor(state.total / PAGE);
    await loadRows(); refreshState();
  },
  async "row-delete"() {
    const indexes = Array.from(state.selected);
    if (!indexes.length) return toast("Keine Zeilen markiert", true);
    if (!confirm(`${indexes.length} Einträge löschen?`)) return;
    await api("/api/row/delete", { kind: state.tab, indexes });
    await loadRows(); refreshState();
  },
  "page-prev"() { if (state.page > 0) { state.page--; loadRows(); } },
  "page-next"() { if ((state.page + 1) * PAGE < state.total) { state.page++; loadRows(); } },
  async "ports-refresh"() { await refreshState(); toast("Portliste aktualisiert"); },
  async "radio-info"() {
    const res = await api("/api/radio/info", { port: $("#port").value });
    if (!res.ok) { $("#radioinfo").innerHTML = `<span class="err">${res.error}</span>`; return; }
    $("#radioinfo").innerHTML =
      `<b>${res.model}</b> · Firmware ${res.version} · Layout <code>${res.layout}</code>` +
      (res.matches ? ` <span class="ok">passend</span>`
                   : ` <span class="err">kein exakt passendes Layout</span>`);
  },
  async "radio-read"() {
    const res = await api("/api/radio/read", {
      port: $("#port").value, out: $("#readout").value, full: $("#readfull").checked });
    if (!res.ok) return toast(res.error, true);
    watchTask((result) => {
      $("#checks").textContent = (result.checks || []).join("\n");
      $("#writeimg").value = result.path;
      $("#imgpath").value = result.path;
      loadRows();
      report("Codeplug gesichert",
        `<p>${result.path} · ${result.bytes} Byte</p><pre>${(result.checks || []).join("\n")}</pre>`);
    });
  },
  async "radio-write"() {
    if (!$("#writeok").checked) return toast("Bitte zuerst bestätigen", true);
    if (!confirm("Codeplug wirklich ins Funkgerät schreiben?")) return;
    const res = await api("/api/radio/write", {
      port: $("#port").value, image: $("#writeimg").value, confirm: true });
    if (!res.ok) return toast(res.error, true);
    watchTask((result) => {
      report("Schreiben abgeschlossen",
        `<p>${result.summary}</p><p class="muted">Sicherheitskopie: ${result.backup}</p>` +
        (result.mismatches && result.mismatches.length
          ? `<p class="err">Abweichungen: ${result.mismatches.join(", ")}</p>` : ""));
    });
  },
  async "image-open"() {
    const res = await api("/api/image/open", { path: $("#imgpath").value });
    if (!res.ok) return toast(res.error, true);
    $("#checks").textContent = (res.checks || []).join("\n");
    toast(`Abbild geladen (${res.bytes} Byte)`);
    refreshState();
  },
  async "image-decode"() {
    const res = await api("/api/image/decode", {});
    if (!res.ok) return toast(res.error, true);
    await loadRows(); refreshState();
    toast("Übernommen: " + Object.entries(res.stats).map(([k, v]) => `${k} ${v}`).join(", "));
  },
  async "image-apply"() {
    const out = await ask("Änderungen ins Abbild übernehmen",
      "Zieldatei für das geänderte Abbild", "geaendert.atbin");
    if (!out) return;
    const res = await api("/api/image/apply", { out });
    if (!res.ok) return toast(res.error, true);
    $("#writeimg").value = res.path;
    $("#imgpath").value = res.path;
    report("Abbild aktualisiert",
      `<p>${res.spots} geänderte Stellen in <code>${res.path}</code></p>
       <p class="muted">${res.reason}</p>
       <p>Jetzt mit „Ins Gerät schreiben" übertragen – es werden nur die
          geänderten Blöcke gesendet.</p>`);
    refreshState();
  },
  async "userdb-info"() {
    $("#userdb-status").textContent = "Gerät wird gelesen …";
    const res = await api("/api/userdb/info", { port: $("#port").value, samples: 6 });
    if (!res.ok) { $("#userdb-status").innerHTML = `<span class="err">${res.error}</span>`; return; }
    const rows = (res.entries || []).map((e) =>
      `${String(e.id).padStart(9)}  ${(e.callsign || "").padEnd(10)} ${(e.name || "").padEnd(18)} ${e.city || ""} ${e.country ? "· " + e.country : ""}`);
    $("#userdb-status").textContent =
      `Geometrie ${res.geometry} · ${res.count.toLocaleString("de-DE")} Einträge · ID-Kodierung ${res.encoding}\n\n`
      + (rows.length ? rows.join("\n") : "keine lesbaren Einträge");
  },
  async "userdb-load"() {
    const res = await api("/api/userdb/load", { path: $("#userdb-file").value });
    if (!res.ok) return toast(res.error, true);
    toast(`${res.count.toLocaleString("de-DE")} Einträge geladen`);
    dbState.page = 0;
    loadUserdbRows();
  },
  "userdb-prev"() { if (dbState.page > 0) { dbState.page--; loadUserdbRows(); } },
  "userdb-next"() {
    if ((dbState.page + 1) * DB_PAGE < dbState.total) { dbState.page++; loadUserdbRows(); }
  },
  async "userdb-verify"() {
    const res = await api("/api/userdb/verify", { port: $("#port").value, samples: 40 });
    if (!res.ok) return toast(res.error, true);
    watchTask((r) => {
      const heil = r.bad.length === 0;
      report(heil ? "Datenbank vollständig" : "Datenbank unvollständig",
        `<p>${r.ok} von ${r.checked} Stichproben in Ordnung (bei ${r.count.toLocaleString("de-DE")} Einträgen).</p>`
        + (heil ? "<p class='ok'>Die Liste ist durchgängig lesbar.</p>"
                : `<p class="err">Ab Eintrag ${r.first_bad.toLocaleString("de-DE")}
                   (${Math.round((100 * r.first_bad) / r.count)} % der Liste) fehlen Datensätze.</p>
                   <p>Das deutet auf einen abgebrochenen Schreibvorgang hin — die Liste
                      noch einmal schreiben.</p>`));
    }, "#userdb-progress");
  },
  async "userdb-fetch"() {
    const out = await ask("Liste von radioid.net holen",
      "Die Datei ist einige zehn Megabyte groß und wird hier abgelegt:",
      "radioid-users.json");
    if (!out) return;
    const res = await api("/api/userdb/fetch", { out });
    if (!res.ok) return toast(res.error, true);
    watchTask((r) => {
      $("#userdb-csv").value = r.path;
      $("#userdb-file").value = r.path;
      dbState.page = 0; dbState.query = ""; $("#userdb-search").value = "";
      loadUserdbRows();
      report("Liste geladen",
        `<p>${(r.bytes / 1048576).toFixed(1)} MB · ${r.entries.toLocaleString("de-DE")} Einträge</p>
         <p class="muted">${r.path}</p>
         <p>Sie ist unten zum Durchsuchen geladen und kann direkt geschrieben werden.</p>`);
    }, "#userdb-progress");
  },
  async "userdb-export"() {
    const res = await api("/api/userdb/export", {
      port: $("#port").value, out: $("#userdb-out").value,
      limit: Number($("#userdb-limit-read").value) || 0 });
    if (!res.ok) return toast(res.error, true);
    watchTask((r) => {
      $("#userdb-file").value = r.path;
      dbState.page = 0;
      dbState.query = "";
      $("#userdb-search").value = "";
      loadUserdbRows();
      report("Rufzeichendatenbank gesichert",
        `<p>${r.entries} von ${r.count} Einträgen in <code>${r.path}</code></p>
         <p class="muted">Die Liste ist unten zum Durchsuchen geladen.</p>`);
    }, "#userdb-progress");
  },
  async "userdb-write"() {
    if (!$("#userdb-ok").checked) return toast("Bitte zuerst bestätigen", true);
    if (!confirm("Rufzeichendatenbank im Gerät ersetzen?")) return;
    const res = await api("/api/userdb/write", {
      port: $("#port").value, csv: $("#userdb-csv").value,
      country: $("#userdb-country").value, prefix: $("#userdb-prefix").value,
      limit: Number($("#userdb-limit").value) || 0, confirm: true });
    if (!res.ok) return toast(res.error, true);
    watchTask((r) => {
      const rows = (r.samples || []).map((e) => `${e.id} ${e.callsign} ${e.name}`);
      report("Rufzeichendatenbank geschrieben",
        `<p>${r.count} Einträge übertragen (vorher ${r.before}).</p>
         <p>Kopfblock ${r.header_ok ? "stimmt" : "<b class='err'>weicht ab</b>"} ·
            ${r.verified} Stichproben geprüft
            ${r.stale_cleared ? `· ${r.stale_cleared} alte Einträge gelöscht` : ""}</p>
         <pre>${rows.join("\n")}</pre>`);
    }, "#userdb-progress");
  },
  async "sim-start"() {
    const res = await api("/api/simulator", { action: "start" });
    if (res.ok) { await refreshState(); $("#port").value = res.port; toast("Simulator auf " + res.port); }
  },
  async "sim-stop"() { await api("/api/simulator", { action: "stop" }); refreshState(); },
  "report-close"() { $("#report").classList.add("hidden"); },
  "aprs-reload"() { loadAprs(); },
};

const APRS_FIELDS = ["source", "source_ssid", "destination", "destination_ssid", "path",
                     "fm_frequency", "symbol_table", "symbol", "fm_power",
                     "auto_interval", "tx_delay", "manual_interval", "message"];

async function loadAprs() {
  const res = await api("/api/settings/aprs", {});
  const a = res.aprs || {};
  for (const key of APRS_FIELDS) {
    const el = document.getElementById("aprs-" + key);
    if (!el) continue;
    let value = a[key];
    if (key === "fm_frequency" && value) value = (value / 1e6).toFixed(5);
    el.value = value === undefined || value === null ? "" : value;
  }
  const s = $("#aprs-summary");
  s.textContent = a.source
    ? `Bake: ${a.source}-${a.source_ssid} → ${a.destination}-${a.destination_ssid}`
      + ` über ${a.path || "(kein Pfad)"}, alle ${(a.auto_interval || 0) * 30} s`
    : "Keine APRS-Daten im geladenen Abbild.";
}

async function saveAprs(key, value) {
  const res = await api("/api/settings/aprs", { values: { [key]: value } });
  if (res.ok) { loadAprs(); refreshState(); }
}

async function loadUserdbRows() {
  const params = new URLSearchParams({
    q: dbState.query, offset: dbState.page * DB_PAGE, limit: DB_PAGE });
  const data = await api("/api/userdb/rows?" + params.toString());
  dbState.total = data.total;
  const body = $("#userdb-grid tbody");
  body.innerHTML = data.rows.map((r) =>
    `<tr><td class="muted">${r.id}</td><td><b>${r.callsign || ""}</b></td>
         <td>${r.name || ""}</td><td>${r.city || ""}</td>
         <td>${r.state || ""}</td><td>${r.country || ""}</td></tr>`).join("");
  const from = data.total ? dbState.page * DB_PAGE + 1 : 0;
  const to = Math.min(data.total, (dbState.page + 1) * DB_PAGE);
  $("#userdb-pageinfo").textContent = data.total
    ? `${from.toLocaleString("de-DE")}–${to.toLocaleString("de-DE")} von ${data.total.toLocaleString("de-DE")}`
    : "keine Liste geladen";
}

function watchTask(onDone, boxId) {
  const box = $(boxId || "#progress");
  box.classList.remove("hidden");
  const timer = setInterval(async () => {
    const task = await api("/api/task");
    const pct = task.total ? Math.round((task.done / task.total) * 100) : 0;
    const fill = box.querySelector(".bar span");
    const label = box.querySelector("p");
    if (fill) fill.style.width = pct + "%";
    // Restzeit aus der gemessenen Geschwindigkeit, nicht geschätzt
    let left = "";
    if (task.done > 0 && task.seconds > 2 && task.total > task.done) {
      const secs = ((task.total - task.done) * task.seconds) / task.done;
      left = secs < 90 ? ` · noch ${Math.round(secs)} s`
           : secs < 5400 ? ` · noch ${Math.round(secs / 60)} min`
                         : ` · noch ${(secs / 3600).toFixed(1)} h`;
    }
    if (label) label.textContent =
      `${task.name}: ${pct}%${left} – ${task.label || ""}`;
    if (task.finished) {
      clearInterval(timer);
      setTimeout(() => box.classList.add("hidden"), 1200);
      refreshState();
      if (task.error) toast(task.error, true);
      else if (onDone) onDone(task.result || {});
    }
  }, 400);
}

// ---------------------------------------------------------------- Ereignisse
document.addEventListener("click", (ev) => {
  const act = ev.target.dataset && ev.target.dataset.act;
  if (act && ACTIONS[act]) { ev.preventDefault(); ACTIONS[act](); return; }
  const tab = ev.target.closest && ev.target.closest(".tab");
  if (tab) {
    $$(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    state.tab = tab.dataset.tab;
    state.page = 0;
    const isTable = !!COLUMNS[state.tab];
    $("#view-table").classList.toggle("hidden", !isTable);
    $("#view-device").classList.toggle("hidden", state.tab !== "device");
    $("#view-userdb").classList.toggle("hidden", state.tab !== "userdb");
    $("#view-aprs").classList.toggle("hidden", state.tab !== "aprs");
    if (state.tab === "aprs") loadAprs();
    $("#view-log").classList.toggle("hidden", state.tab !== "log");
    if (isTable) loadRows(); else refreshState();
  }
  if (ev.target === $("#report")) $("#report").classList.add("hidden");
});

document.addEventListener("change", (ev) => {
  const el = ev.target;
  if (el.dataset.role === "sel") {
    const index = Number(el.closest("tr").dataset.index);
    el.checked ? state.selected.add(index) : state.selected.delete(index);
    return;
  }
  if (el.dataset.key) saveCell(el);
});

document.addEventListener("focusout", (ev) => {
  if (ev.target.dataset && ev.target.dataset.key) saveCell(ev.target);
  const id = ev.target.id || "";
  if (id.startsWith("aprs-") && ev.target.tagName !== "BUTTON") {
    saveAprs(id.slice(5), ev.target.value);
  }
});

document.addEventListener("keydown", (ev) => {
  if (ev.target.dataset && ev.target.dataset.key && ev.key === "Enter") ev.target.blur();
  if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === "s") {
    ev.preventDefault(); ACTIONS["project-save"]();
  }
});

let dbSearchTimer = null;
$("#userdb-search").addEventListener("input", (ev) => {
  clearTimeout(dbSearchTimer);
  dbSearchTimer = setTimeout(() => {
    dbState.query = ev.target.value;
    dbState.page = 0;
    loadUserdbRows();
  }, 250);
});

let searchTimer = null;
$("#search").addEventListener("input", (ev) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.query = ev.target.value;
    state.page = 0;
    loadRows();
  }, 200);
});

(async function start() {
  await refreshState();
  await loadRows();
  setInterval(refreshState, 5000);
})();
