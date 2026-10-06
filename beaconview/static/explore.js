/* Explore: a time range plus filters, all in the URL, scoping the summary, the two charts, top values and the
   request log below them. The URL's filter parameters are exactly the API's, so a view is a shareable link and the
   page passes them through unchanged. */
"use strict";
(function () {
  var el = BV.el, $ = BV.$;
  var SPLIT_LABELS = { status_class: "Status class" };
  var LIVE_LOGS_MS = 5000, LIVE_CHARTS_MS = 15000;
  var LOG_COLS = [
    ["ts", "Time"], ["status", "Status"], ["method", "Method"], ["host", "Host"], ["path", "Path"],
    ["ip", "Client IP"], ["duration_us", "Duration", "num"], ["bytes", "Size", "num"],
  ];
  var DETAIL_ORDER = ["ts", "instance", "status", "origin_status", "method", "host", "path", "query", "ip",
    "duration_us", "bytes", "router", "service", "entrypoint", "protocol", "tls", "user_agent", "referer", "seq"];

  var meta = null;                 // /api/fields
  var state = { range: "1h", from: null, to: null, filters: [], split: "status_class", top: "path", live: false };
  var slots = new BV.Slots();
  var charts = {};
  var logs = { rows: [], columns: null, next: null, maxSeq: null };
  var inflight = {};
  var timers = {};

  /* ---------- URL state ---------- */
  function isFilterKey(k) {
    return (meta && meta.fields.some(function (f) { return f.name === k; })) || k.indexOf("h.") === 0 ||
      k === "min_ms" || k === "max_ms";
  }
  function readUrl() {
    var p = new URLSearchParams(location.search);
    var r = BV.rangeFromUrl(p);
    state.range = r.range; state.from = r.from; state.to = r.to;
    state.split = p.has("split") ? p.get("split") : "status_class";
    state.top = p.get("top") || "path";
    state.live = p.get("live") === "1" && !!state.range;
    state.filters = [];
    p.forEach(function (v, k) { if (isFilterKey(k)) state.filters.push([k, v]); });
  }
  function writeUrl(push) {
    var p = new URLSearchParams();
    BV.rangeToUrl(state, p);
    state.filters.forEach(function (f) { p.append(f[0], f[1]); });
    if (state.split !== "status_class") p.set("split", state.split);
    if (state.top !== "path") p.set("top", state.top);
    if (state.live) p.set("live", "1");
    var url = location.pathname + "?" + p.toString();
    if (push) history.pushState(null, "", url); else history.replaceState(null, "", url);
  }
  function window_() { return BV.rangeWindow(state); }
  function apiParams(extra) {
    var w = window_(), p = new URLSearchParams();
    p.set("from", Math.floor(w.from));
    p.set("to", Math.ceil(w.to));
    state.filters.forEach(function (f) { p.append(f[0], f[1]); });
    Object.keys(extra || {}).forEach(function (k) { if (extra[k] !== null && extra[k] !== undefined) p.set(k, extra[k]); });
    return p;
  }

  /* ---------- labels ---------- */
  function fieldLabel(name) {
    if (name.indexOf("h.") === 0) return name.slice(2);
    if (name === "min_ms") return "Duration ≥";
    if (name === "max_ms") return "Duration ≤";
    if (name === "status_class") return "Status class";
    var f = meta && meta.fields.find(function (x) { return x.name === name; });
    return f ? f.label : name;
  }
  function rangeLabel() { return BV.rangeLabel(state); }

  /* ---------- range menu ---------- */
  function buildRangeMenu() {
    BV.buildRangeMenu($("rangeMenu"), state, meta && meta.retention.raw_days, function (r) {
      state.range = r.range; state.from = r.from; state.to = r.to;
      if (!r.range) state.live = false;
      writeUrl(true); reloadAll();
    });
  }

  /* ---------- filters ---------- */
  var KIND_HINTS = {
    glob: "Exact, or use * : /api/* (starts with), *state* (contains), *.js (ends with).",
    status: "A code like 404, or a class like 4xx.",
    ip: "An address, an IPv4 range like 203.0.113.0/24, or a prefix like 2001:db8:*.",
    contains: "Matches anywhere, ignoring case.",
    str: "Exact match. (none) matches requests without one.",
    header: "Kept request headers only (Traefik's accessLog.fields.headers). Exact, or use * like paths.",
    num: "Milliseconds.",
  };
  function filterKind(name) {
    if (name === "header") return "header";
    if (name === "min_ms" || name === "max_ms") return "num";
    var f = meta.fields.find(function (x) { return x.name === name; });
    return f ? f.kind : "str";
  }
  function buildAddForm() {
    var sel = BV.clear($("addField"));
    meta.fields.forEach(function (f) { sel.appendChild(el("option", { value: f.name, text: f.label })); });
    sel.appendChild(el("option", { value: "min_ms", text: "Duration at least (ms)" }));
    sel.appendChild(el("option", { value: "max_ms", text: "Duration at most (ms)" }));
    sel.appendChild(el("option", { value: "header", text: "Request header…" }));
    sel.value = "path";
    onAddFieldChange();
  }
  function onAddFieldChange() {
    var name = $("addField").value, kind = filterKind(name);
    $("addHeader").hidden = name !== "header";
    $("addOp").disabled = kind === "num";
    $("addHint").textContent = KIND_HINTS[kind] || "";
    $("addValue").value = "";
    $("addValue").type = kind === "num" ? "number" : "text";
    var list = BV.clear($("addValues"));
    if (kind === "num" || name === "header") return;
    var known = meta.values[name];
    if (known && known.length) {
      known.forEach(function (v) { list.appendChild(el("option", { value: v })); });
      return;
    }
    BV.api("/api/top", apiParams({ field: name, limit: 25 })).then(function (t) {
      if ($("addField").value !== name) return;
      t.values.forEach(function (v) { list.appendChild(el("option", { value: v.value })); });
    }).catch(function () { /* suggestions are optional */ });
  }
  function addFilter(name, value, negate) {
    var v = (negate ? "!" : "") + value;
    if (state.filters.some(function (f) { return f[0] === name && f[1] === v; })) return;
    state.filters.push([name, v]);
    writeUrl(true);
    reloadAll();
  }
  function renderChips() {
    var box = BV.clear($("chips"));
    state.filters.forEach(function (f, i) {
      var neg = f[1].charAt(0) === "!", value = neg ? f[1].slice(1) : f[1];
      var op = f[0] === "min_ms" || f[0] === "max_ms" ? "" : neg ? "is not" : "is";
      var text = f[0] === "min_ms" || f[0] === "max_ms" ? value + " ms" : value;
      box.appendChild(el("span", { class: "chip" + (neg ? " neg" : "") },
        el("span", { text: fieldLabel(f[0]) }), op ? el("span", { class: "op", text: op }) : null,
        el("span", { class: "val", title: value, text: text }),
        el("button", { type: "button", "aria-label": "Remove filter " + fieldLabel(f[0]) + " " + op + " " + value,
          title: "Remove", onclick: function () {
            state.filters.splice(i, 1); writeUrl(true); reloadAll();
          } }, "✕")));
    });
  }

  /* ---------- fetching ---------- */
  function load(name, card, path, params, onData) {
    if (inflight[name]) inflight[name].abort();
    var ctl = new AbortController();
    inflight[name] = ctl;
    if (card) card.classList.add("loading");
    return BV.api(path, params, ctl.signal).then(function (d) {
      if (inflight[name] !== ctl) return;
      inflight[name] = null;
      if (card) card.classList.remove("loading");
      clearError(name);
      onData(d);
    }).catch(function (e) {
      if (e.name === "AbortError") return;
      if (card) card.classList.remove("loading");
      inflight[name] = null;
      showError(name, e.message);
    });
  }
  var errors = {};
  function showError(name, msg) { errors[name] = msg; renderBanner(); }
  function clearError(name) { if (errors[name]) { delete errors[name]; renderBanner(); } }
  function renderBanner() {
    var msgs = Object.keys(errors).map(function (k) { return errors[k]; });
    var uniq = msgs.filter(function (m, i) { return msgs.indexOf(m) === i; });
    $("banner").hidden = !uniq.length;
    $("banner").textContent = uniq.join(" · ");
  }

  /* ---------- summary tiles ---------- */
  function loadSummary() {
    load("summary", $("tiles"), "/api/summary", apiParams(), function (s) {
      var tiles = [
        ["Requests", BV.compact(s.requests), BV.int(s.requests)],
        ["Requests/s", s.rps < 10 ? String(Math.round(s.rps * 100) / 100) : BV.compact(s.rps), null],
        ["Unique IPs", (s.unique_ips_approx ? "≈ " : "") + BV.compact(s.unique_ips), s.unique_ips_approx ? "estimated" : null],
        ["4xx", BV.pct(s.requests ? s.status_4xx / s.requests : null), BV.int(s.status_4xx)],
        ["5xx", BV.pct(s.requests ? s.status_5xx / s.requests : null), BV.int(s.status_5xx)],
        ["p50", BV.dur(s.p50_us), "median response"],
        ["p95", BV.dur(s.p95_us), null],
        ["Sent", BV.bytes(s.bytes), null],
      ];
      var box = BV.clear($("tiles"));
      tiles.forEach(function (t) {
        box.appendChild(el("div", { class: "tile" }, el("div", { class: "label", text: t[0] }),
          el("div", { class: "value", text: t[1] }), t[2] ? el("div", { class: "note", text: t[2] }) : null));
      });
    });
  }

  /* ---------- charts ---------- */
  function buckets() {
    var w = charts.series ? charts.series.width() : $("seriesChart").clientWidth - 60;
    return Math.max(20, Math.min(240, Math.floor(w / 7)));
  }
  function seriesLabel(key) {
    if (key === "all") return "Requests";
    return key;
  }
  function loadSeries() {
    load("series", $("seriesCard"), "/api/series", apiParams({ buckets: buckets(), split: state.split || null }),
      function (d) {
        var keys = d.series.map(function (s) { return s.key; }).filter(function (k) { return k !== "Other"; });
        if (state.split && state.split !== "status_class") slots.assign(keys);
        var series = d.series.map(function (s) {
          var color = state.split === "status_class" ? BV.statusClassColor(s.key)
            : !state.split ? getComputedStyle(document.documentElement).getPropertyValue("--s1").trim()
            : slots.color(s.key);
          return { key: s.key, label: seriesLabel(s.key), color: color, values: s.values };
        });
        var opts = { kind: "columns", start: d.start, step: d.step, n: d.buckets, series: series, height: 220,
          label: "Requests over time", onZoom: zoom };
        $("seriesSub").textContent = "per " + BV.span(d.step).replace(/^1 /, "") + " · " + rangeLabel();
        BV.legend($("seriesLegend"), series, "columns");
        if (charts.series) charts.series.update(opts); else charts.series = BV.chart($("seriesChart"), opts);
        if (!$("seriesTable").hidden) BV.chartTable($("seriesTable"), opts);
        charts.seriesOpts = opts;

        var lat = [
          { key: "p50", label: "p50 (median)", color: cssVar("--s1"), values: d.latency.p50_us },
          { key: "p95", label: "p95", color: cssVar("--s2"), values: d.latency.p95_us },
        ];
        var lopts = { kind: "lines", start: d.start, step: d.step, n: d.buckets, series: lat, height: 150,
          yFormat: function (v) { return v === 0 ? "0" : BV.dur(v); }, tipFormat: BV.dur, label: "Response time over time", onZoom: zoom,
          emptyText: "No timed requests in this range" };
        $("latencySub").textContent = d.source === "rollup" ? "from per-minute histograms" : "from matching requests";
        BV.legend($("latencyLegend"), lat, "lines");
        if (charts.latency) charts.latency.update(lopts); else charts.latency = BV.chart($("latencyChart"), lopts);
        if (!$("latencyTable").hidden) BV.chartTable($("latencyTable"), lopts);
        charts.latencyOpts = lopts;
      });
  }
  function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }
  function zoom(from, to) {
    if (to - from < 2000) return;
    state.range = null; state.from = from; state.to = to; state.live = false;
    writeUrl(true);
    reloadAll();
  }
  function toggleTable(btn, table, key) {
    var on = btn.getAttribute("aria-pressed") !== "true";
    btn.setAttribute("aria-pressed", String(on));
    $(table).hidden = !on;
    if (on && charts[key]) BV.chartTable($(table), charts[key]);
  }

  /* ---------- top values ---------- */
  function loadTop() {
    load("top", $("topCard"), "/api/top", apiParams({ field: state.top, limit: 15 }), function (t) {
      var list = BV.clear($("topList"));
      $("topSub").textContent = t.distinct ? "≈ " + BV.int(t.distinct) + " distinct" : "";
      if (!t.values.length) { list.appendChild(el("div", { class: "hint", text: "Nothing in this range." })); return; }
      var maxShare = t.values[0].share || 1;
      t.values.forEach(function (v) {
        var row = el("div", { class: "toprow" },
          el("span", { class: "name", title: v.value, text: v.value }),
          el("span", { class: "n", text: BV.int(v.count) + " · " + BV.pct(v.share) }),
          el("span", { class: "acts" },
            el("button", { type: "button", title: "Only " + v.value, "aria-label": "Filter to " + v.value,
              onclick: function () { addFilter(filterField(state.top), v.value, false); } }, "="),
            el("button", { type: "button", title: "Exclude " + v.value, "aria-label": "Exclude " + v.value,
              onclick: function () { addFilter(filterField(state.top), v.value, true); } }, "≠")),
          el("span", { class: "bar" }, el("i", { style: "width:" + Math.max(1, v.share / maxShare * 100) + "%" })));
        list.appendChild(row);
      });
    });
  }
  function filterField(field) { return field === "status_class" ? "status" : field; }   // 4xx is a status filter

  /* ---------- request log ---------- */
  function logsSpanDays() {     // show the date on log times only when the range crosses days
    var w = window_();
    return new Date(w.from).toDateString() !== new Date(w.to).toDateString();
  }
  function multiInstance() { return meta && meta.instances.length > 1; }
  function renderLogHead() {
    var head = BV.clear($("logsHead"));
    var cols = multiInstance() ? [["instance", "Instance"]].concat(LOG_COLS) : LOG_COLS;
    cols.forEach(function (c) { head.appendChild(el("th", { class: c[2] || "", text: c[1] })); });
  }
  function logRow(r, fresh) {
    var cols = multiInstance() ? [["instance", "Instance"]].concat(LOG_COLS) : LOG_COLS;
    var get = function (name) { return r[logs.columns.indexOf(name)]; };
    var tr = el("tr", { tabindex: "0", class: fresh ? "fresh" : "" });
    cols.forEach(function (c) {
      var v = get(c[0]), td;
      if (c[0] === "ts") td = el("td", { class: "mono", title: BV.stamp(v, true), text: BV.stamp(v, logsSpanDays()) });
      else if (c[0] === "status") {
        td = el("td", null, el("span", { class: "status" },
          el("i", { style: "background:" + BV.statusColorVar(v) }), v === null ? "–" : String(v)));
      } else if (c[0] === "duration_us") td = el("td", { class: "num", text: BV.dur(v) });
      else if (c[0] === "bytes") td = el("td", { class: "num", text: BV.bytes(v) });
      else if (c[0] === "path") {
        var q = get("query");
        td = el("td", { class: "mono path", title: v + (q ? "?" + q : ""), text: (v || "") + (q ? "?" + q : "") });
      } else td = el("td", { class: c[0] === "ip" ? "mono" : "", text: v === null ? "–" : String(v) });
      tr.appendChild(td);
    });
    var open = function () { openDetail(r); };
    tr.addEventListener("click", open);
    tr.addEventListener("keydown", function (ev) { if (ev.key === "Enter") open(); });
    return tr;
  }
  function renderLogs() {
    var body = BV.clear($("logsBody"));
    logs.rows.forEach(function (r) { body.appendChild(logRow(r, false)); });
    if (!logs.rows.length) {
      body.appendChild(el("tr", null, el("td", { colspan: 9, class: "hint", text: "No requests match." })));
    }
    $("moreBtn").hidden = !logs.next;
    $("logsSub").textContent = logs.rows.length ? "newest first · " + BV.int(logs.rows.length) + " shown" : "";
  }
  function loadLogs() {
    load("logs", $("logsCard"), "/api/logs", apiParams({ limit: 100 }), function (d) {
      logs.columns = d.columns; logs.rows = d.rows; logs.next = d.next; logs.maxSeq = d.max_seq;
      renderLogs();
    });
  }
  function loadOlder() {
    if (!logs.next) return;
    load("logsMore", null, "/api/logs", apiParams({ limit: 100, before: logs.next[0] + ":" + logs.next[1] }), function (d) {
      logs.rows = logs.rows.concat(d.rows); logs.next = d.next;
      renderLogs();
    });
  }
  function pollLive() {
    if (!state.live || logs.maxSeq === null || logs.maxSeq === undefined) return;
    BV.api("/api/logs", apiParams({ limit: 200, after: logs.maxSeq })).then(function (d) {
      if (!d.rows.length) return;
      logs.maxSeq = d.max_seq;
      logs.rows = d.rows.concat(logs.rows).slice(0, 500);
      var body = $("logsBody");
      if (body.firstChild && !body.firstChild.hasAttribute("tabindex")) BV.clear(body);
      for (var i = d.rows.length - 1; i >= 0; i--) body.insertBefore(logRow(d.rows[i], true), body.firstChild);
      while (body.children.length > 500) body.removeChild(body.lastChild);
      $("logsSub").textContent = "newest first · " + BV.int(logs.rows.length) + " shown · live";
    }).catch(function () { /* the next tick retries */ });
  }

  /* ---------- detail drawer ---------- */
  var lastFocus = null;
  function openDetail(r) {
    lastFocus = document.activeElement;
    var get = function (name) { return r[logs.columns.indexOf(name)]; };
    var dl = BV.clear($("drawerBody"));
    var filterable = function (name) { return meta.fields.some(function (f) { return f.name === name; }); };
    var row = function (label, value, field, filterVal) {
      dl.appendChild(el("dt", { text: label }));
      dl.appendChild(el("dd", null, el("span", { class: "v", text: value }),
        field ? el("button", { type: "button", title: "Filter to this value", onclick: function () { closeDetail(); addFilter(field, filterVal, false); } }, "=") : null,
        field ? el("button", { type: "button", title: "Exclude this value", onclick: function () { closeDetail(); addFilter(field, filterVal, true); } }, "≠") : null));
    };
    DETAIL_ORDER.forEach(function (name) {
      var v = get(name);
      if (name === "instance" && !multiInstance()) return;
      var shown = v === null || v === undefined ? "–"
        : name === "ts" ? new Date(v).toISOString().replace("T", " ").replace("Z", " UTC") + "  (" + BV.stamp(v, true) + " local)"
        : name === "duration_us" ? BV.dur(v) + "  (" + BV.int(v) + " µs)"
        : name === "bytes" ? BV.bytes(v) + "  (" + BV.int(v) + " bytes)"
        : String(v);
      var canFilter = filterable(name) && v !== null && v !== undefined && name !== "query";
      row(fieldLabel(name === "ts" ? "Time" : name === "duration_us" ? "Duration" : name === "bytes" ? "Size" : name === "seq" ? "Sequence" : name),
        shown, canFilter ? name : null, canFilter ? String(v) : null);
    });
    var headers = get("headers");
    if (headers) {
      var parsed = {};
      try { parsed = JSON.parse(headers); } catch (e) { parsed = {}; }
      Object.keys(parsed).forEach(function (k) { row(k, String(parsed[k]), "h." + k, String(parsed[k])); });
    }
    $("drawerTitle").textContent = (get("method") || "") + " " + (get("path") || "") + " → " + (get("status") || "");
    $("scrim").hidden = false;
    $("drawer").hidden = false;
    $("drawerClose").focus();
  }
  function closeDetail() {
    $("drawer").hidden = true;
    $("scrim").hidden = true;
    if (lastFocus) lastFocus.focus();
  }

  /* ---------- orchestration ---------- */
  function reloadAll() {
    $("rangeBtn").textContent = "◷ " + rangeLabel();
    renderChips();
    buildRangeMenu();
    $("liveBtn").setAttribute("aria-pressed", String(state.live));
    $("liveDot").className = "dot" + (state.live ? " live" : "");
    $("splitSel").value = state.split;
    $("topSel").value = state.top;
    loadSummary(); loadSeries(); loadTop(); loadLogs();
    schedule();
  }
  function schedule() {
    clearInterval(timers.logs); clearInterval(timers.charts);
    if (!state.live) return;
    timers.logs = setInterval(pollLive, LIVE_LOGS_MS);
    timers.charts = setInterval(function () { loadSummary(); loadSeries(); loadTop(); }, LIVE_CHARTS_MS);
  }

  function init() {
    BV.initTheme($("themeBtn"), function () { if (charts.seriesOpts) loadSeries(); });
    BV.initIngestStatus($("ingestDot"), $("ingestText"));
    BV.api("/api/fields").then(function (m) {
      meta = m;
      readUrl();
      var split = BV.clear($("splitSel")), top = BV.clear($("topSel"));
      split.appendChild(el("option", { value: "", text: "Nothing" }));
      meta.splits.forEach(function (s) {
        split.appendChild(el("option", { value: s, text: SPLIT_LABELS[s] || fieldLabel(s) }));
        top.appendChild(el("option", { value: s, text: SPLIT_LABELS[s] || fieldLabel(s) }));
      });
      buildAddForm();
      renderLogHead();
      reloadAll();
    }).catch(function (e) { showError("fields", e.message); });

    BV.menu($("rangeBtn"), $("rangeMenu"));
    BV.menu($("addBtn"), $("addMenu"), function () { $("addValue").focus(); });
    document.addEventListener("keydown", function (ev) {
      if (ev.key !== "Escape") return;
      if (!$("drawer").hidden) closeDetail(); else BV.closeMenus();
    });
    $("addField").addEventListener("change", onAddFieldChange);
    $("addForm").addEventListener("submit", function (ev) {
      ev.preventDefault();
      var name = $("addField").value, value = $("addValue").value.trim();
      if (name === "header") {
        var h = $("addHeader").value.trim();
        if (!h || !value) return;
        name = "h." + h;
      }
      if (!value) return;
      BV.closeMenus();
      addFilter(name, value, $("addOp").value === "not" && !$("addOp").disabled);
    });
    $("splitSel").addEventListener("change", function () { state.split = this.value; writeUrl(false); loadSeries(); });
    $("topSel").addEventListener("change", function () { state.top = this.value; writeUrl(false); loadTop(); });
    $("seriesTableBtn").addEventListener("click", function () { toggleTable(this, "seriesTable", "seriesOpts"); });
    $("latencyTableBtn").addEventListener("click", function () { toggleTable(this, "latencyTable", "latencyOpts"); });
    $("moreBtn").addEventListener("click", loadOlder);
    $("refreshBtn").addEventListener("click", reloadAll);
    $("liveBtn").addEventListener("click", function () {
      state.live = !state.live;
      if (state.live && !state.range) { state.range = "15m"; state.from = state.to = null; }
      writeUrl(false);
      reloadAll();
    });
    $("drawerClose").addEventListener("click", closeDetail);
    $("scrim").addEventListener("click", closeDetail);
    window.addEventListener("popstate", function () { readUrl(); reloadAll(); });
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) { clearInterval(timers.logs); clearInterval(timers.charts); }
      else if (state.live) { reloadAll(); }
    });
  }
  init();
})();
