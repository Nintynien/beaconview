/* Settings: how ingest and storage are doing (refreshed every 5 s), the settings that can change while running,
   and, read-only, the ones that need a restart. A setting given in the environment is shown locked: the compose
   file stays the source of truth for what it sets. Lowering a retention asks first, with what it would delete. */
"use strict";
(function () {
  var el = BV.el, $ = BV.$;
  var current = null;              // /api/settings

  /* ---------- status ---------- */
  function fact(dl, label, value, note) {
    dl.appendChild(el("dt", { text: label }));
    dl.appendChild(el("dd", null, value, note ? el("span", { class: "sub", text: " " + note }) : null));
  }
  function loadStatus() {
    BV.api("/api/status").then(function (s) {
      BV.skew = s.now_ms - Date.now();
      var ing = s.ingest, st = s.storage;
      var t = BV.clear($("logsTable"));
      t.appendChild(el("thead", null, el("tr", null, ["Instance", "File", "State", "Newest request", "Read", "Unreadable",
        "Rotations"].map(function (h, i) { return el("th", { class: i >= 4 ? "num" : "", text: h }); }))));
      var body = el("tbody"), lastBad = null;
      ing.sources.forEach(function (x) {
        var bf = x.backfill, state;
        if (!ing.alive) state = "stopped";
        else if (x.error) state = "can't read: " + x.error;
        else if (bf && bf.state === "running") state = "loading older logs (" + Math.min(bf.files_done + 1, bf.files_total) + " of " + bf.files_total + ")";
        else if (x.unread_bytes > 1e6) state = "catching up (" + BV.bytes(x.unread_bytes) + " behind)";
        else state = "reading";
        var ok = ing.alive && !x.error;
        body.appendChild(el("tr", null,
          el("td", { text: x.instance }),
          el("td", { class: "mono name", title: x.reading || x.path, text: x.reading || x.path }),
          el("td", null, el("span", { class: "status" }, el("i", { class: "dot " + (ok ? "good" : "bad") }), state)),
          el("td", null, x.newest_ms ? BV.agoText(el("span"), x.newest_ms) : "–"),
          el("td", { class: "num", text: BV.int(x.lines) + " lines" }),
          el("td", { class: "num", text: BV.int(x.bad_lines) }),
          el("td", { class: "num", text: BV.int(x.rotations) })));
        if (x.last_bad_line) lastBad = x.instance + ": " + x.last_bad_line;
      });
      t.appendChild(body);
      $("logsSub").textContent = BV.compact(ing.rows_per_sec) + " requests/s written";
      $("badLine").hidden = !lastBad;
      if (lastBad) $("badLineText").textContent = lastBad;

      var db = BV.clear($("dbFacts"));
      fact(db, "Size on disk", BV.bytes(st.db_bytes));
      fact(db, "Requests stored", BV.int(st.rows));
      fact(db, "Oldest", st.oldest_ms ? BV.stamp(st.oldest_ms, true).replace(/\.\d+$/, "") : "–",
        "kept " + s.retention.raw_days + " days");
      fact(db, "Per-minute totals", BV.int(st.rollup_rows) + " rows", "kept " + s.retention.rollup_days + " days");
      fact(db, "Last cleanup", ing.last_prune ? BV.agoText(el("span"), ing.last_prune.at * 1000) : "not yet",
        ing.last_prune ? "took " + (ing.last_prune.ms < 1 ? "under 1 ms" : BV.dur(ing.last_prune.ms * 1000)) : "");

      var pr = BV.clear($("procFacts"));
      fact(pr, "Version", s.version);
      fact(pr, "Running for", BV.span(s.uptime_s * 1000));
      fact(pr, "Memory", s.process.rss_bytes ? BV.bytes(s.process.rss_bytes) : "–");
      fact(pr, "CPU time", BV.dur(s.process.cpu_s * 1e6));
      fact(pr, "Live connections", String(s.streams), s.streams === 1 ? "page receiving pushed updates" : "pages receiving pushed updates");
      if (ing.write_errors) fact(pr, "Write errors", BV.int(ing.write_errors), ing.last_error || "");
    }).catch(function (e) { showError(e.message); });
  }

  /* ---------- settings ---------- */
  function renderFields() {
    var box = BV.clear($("fields"));
    current.editable.forEach(function (f) {
      var id = "f_" + f.name, input;
      if (f.kind === "bool") {
        input = el("input", { type: "checkbox", id: id, name: f.name });
        input.checked = !!f.value;
      } else {
        input = el("input", { type: f.kind === "number" ? "number" : "text", id: id, name: f.name,
          step: f.kind === "number" ? "any" : null, min: f.kind === "number" ? "0.01" : null,
          value: f.value === null ? "" : String(f.value), placeholder: f.kind === "text" ? "not set" : null });
      }
      input.disabled = f.locked;
      var defaultText = f.kind === "bool" ? (f.default ? "on" : "off") : f.default === "" ? "not set" : String(f.default);
      box.appendChild(el("div", { class: "field" + (f.locked ? " locked" : "") },
        el("label", { for: id, text: f.label }),
        el("div", { class: "control" }, input,
          f.locked ? el("span", { class: "lock", title: "Remove it from the environment to change it here",
            text: "set by " + f.env }) : null),
        el("p", { class: "sub", text: f.note + " Default: " + defaultText + ". Environment variable: " + f.env + "." })));
    });
    $("saveBtn").disabled = current.editable.every(function (f) { return f.locked; });

    var t = BV.clear($("restartTable")), body = el("tbody");
    current.restart_only.forEach(function (r) {
      body.appendChild(el("tr", null, el("th", { text: r.label }), el("td", { class: "mono name", title: r.value, text: r.value }),
        el("td", { class: "sub", text: r.env })));
    });
    t.appendChild(body);
  }
  function loadSettings() {
    return BV.api("/api/settings").then(function (d) { current = d; renderFields(); });
  }
  function collect() {
    var changes = {};
    current.editable.forEach(function (f) {
      if (f.locked) return;
      var input = $("f_" + f.name), v;
      if (f.kind === "bool") v = input.checked;
      else if (f.kind === "number") v = input.value === "" ? f.value : Number(input.value);
      else v = input.value.trim();
      if (v !== f.value) changes[f.name] = v;
    });
    return changes;
  }
  function save(changes) {
    $("saveMsg").textContent = "Saving…";
    return fetch("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(changes) }).then(function (r) {
      return r.json().then(function (d) {
        if (!r.ok) throw new Error(d.error || "HTTP " + r.status);
        current = d;
        renderFields();
        $("saveMsg").textContent = d.changed.length ? "Saved. " + d.changed.length + (d.changed.length === 1 ? " setting" : " settings") +
          " changed, in effect now." : "Nothing changed.";
        loadStatus();
      });
    }).catch(function (e) { $("saveMsg").textContent = ""; showError(e.message); });
  }
  function submit(ev) {
    ev.preventDefault();
    hideError();
    var changes = collect();
    if (!Object.keys(changes).length) { $("saveMsg").textContent = "Nothing changed."; return; }
    var shorter = ["retention_days", "rollup_retention_days"].some(function (k) {
      var f = current.editable.find(function (x) { return x.name === k; });
      return k in changes && changes[k] < f.value;
    });
    if (!shorter) { save(changes); return; }
    // keeping data for less time deletes what is now too old: say how much before doing it
    var p = new URLSearchParams();
    ["retention_days", "rollup_retention_days"].forEach(function (k) { if (k in changes) p.set(k, changes[k]); });
    $("saveMsg").textContent = "Checking what that would delete…";
    BV.api("/api/settings/preview", p).then(function (d) {
      $("saveMsg").textContent = "";
      if (!d.requests_deleted && !d.rollup_rows_deleted) { save(changes); return; }
      var parts = [];
      if (d.requests_deleted) parts.push(BV.int(d.requests_deleted) + " requests from before " + BV.stamp(d.requests_cutoff_ms, true).replace(/\.\d+$/, ""));
      if (d.rollup_rows_deleted) parts.push(BV.int(d.rollup_rows_deleted) + " per-minute totals from before " + BV.stamp(d.rollups_cutoff_ms, true).replace(/\.\d+$/, ""));
      $("confirmText").textContent = "This deletes " + parts.join(" and ") + ". It can't be undone.";
      $("confirmBox").hidden = false;
      $("confirmYes").focus();
      $("confirmYes").onclick = function () { $("confirmBox").hidden = true; save(changes); };
      $("confirmNo").onclick = function () { $("confirmBox").hidden = true; $("saveBtn").focus(); };
    }).catch(function (e) { $("saveMsg").textContent = ""; showError(e.message); });
  }

  function showError(msg) { $("banner").hidden = false; $("banner").textContent = msg; }
  function hideError() { $("banner").hidden = true; }

  BV.initTheme($("themeBtn"));
  BV.initIngestStatus($("ingestDot"), $("ingestText"), $("statusBanner"));
  $("settingsForm").addEventListener("submit", submit);
  loadSettings().catch(function (e) { showError(e.message); });
  loadStatus();
  setInterval(function () { if (!document.hidden) loadStatus(); }, 5000);
})();
