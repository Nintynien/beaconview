/* Explore: a time range plus filters (all in the URL, exactly the API's parameters, so a view is a shareable link),
   one switchable panel on top and the matching requests below it.

   The panel is a Timeseries (one measure over time: requests or bytes split by a field, response time, unique IPs,
   error rate) or a Breakdown (one row per value of a field with every measure; grouping by nothing gives the
   totals). The two halves are linked: hovering a request marks its bucket in the chart, or its group in the
   breakdown; clicking a bar jumps the log to that moment; a breakdown row filters to or excludes its value.

   Explore answers a question and holds still; ↻ re-asks it. The one moving part is Follow: new matching requests
   join the top of the log as they arrive (a filtered tail -f). The live dashboard is the Overview. */
"use strict";
(function () {
  var el = BV.el, $ = BV.$;
  var FOLLOW_MS = 5000;
  var NEWEST = 9e15;               // a seq larger than any: "everything at or before this time"
  var MEASURES = [["requests", "Requests"], ["bytes", "Bytes sent"], ["latency", "Response time"],
    ["unique_ips", "Unique IPs"], ["error_rate", "Error rate"]];
  var SPLITTABLE = { requests: true, bytes: true };
  var LOG_COLS = [
    ["ts", "Time"], ["status", "Status"], ["method", "Method"], ["host", "Host"], ["path", "Path"],
    ["ip", "Client IP"], ["duration_us", "Duration", "num"], ["bytes", "Size", "num"],
  ];
  var DETAIL_ORDER = ["ts", "instance", "status", "origin_status", "method", "host", "path", "query", "ip",
    "duration_us", "bytes", "router", "service", "entrypoint", "protocol", "tls", "user_agent", "referer", "seq"];
  var BD_COLS = [   // [sort key, header, cell]
    ["requests", "Requests", function (r) { return BV.int(r.requests); }],
    [null, "Req/s", function (r) { return r.rps < 10 ? String(Math.round(r.rps * 100) / 100) : BV.compact(r.rps); }],
    ["unique_ips", "Unique IPs", function (r, d) { return r.unique_ips === null ? "–" : (d.unique_ips_approx ? "≈ " : "") + BV.int(r.unique_ips); }],
    ["status_4xx", "4xx", function (r) { return r.requests ? BV.pct(r.status_4xx / r.requests) : "–"; }],
    ["status_5xx", "5xx", function (r) { return r.requests ? BV.pct(r.status_5xx / r.requests) : "–"; }],
    ["p50", "p50", function (r) { return BV.dur(r.p50_us); }],
    ["p95", "p95", function (r) { return BV.dur(r.p95_us); }],
    ["bytes", "Sent", function (r) { return BV.bytes(r.bytes); }],
  ];

  var meta = null;                 // /api/fields
  var state = {
    range: "1h", from: null, to: null, filters: [], live: false,
    view: "timeseries", measure: "requests", split: "status_class", group: "path", sort: "requests",
  };
  var slots = new BV.Slots();
  var chart = null, chartOpts = null, bd = null;
  // jump: the moment a bar click took the log to (null: the log starts at the newest); newer: the cursor for the
  // page just above the top row while looking at an earlier moment
  var logs = { rows: [], columns: null, next: null, maxSeq: null, total: null, jump: null, newer: null };
  var inflight = {}, errors = {}, timers = {};

  /* ---------- URL state ---------- */
  function isFilterKey(k) {
    return (meta && meta.fields.some(function (f) { return f.name === k; })) || k.indexOf("h.") === 0 ||
      k === "min_ms" || k === "max_ms";
  }
  function readUrl() {
    var p = new URLSearchParams(location.search);
    var r = BV.rangeFromUrl(p);
    state.range = r.range; state.from = r.from; state.to = r.to;
    state.view = p.get("view") === "breakdown" ? "breakdown" : "timeseries";
    state.measure = MEASURES.some(function (m) { return m[0] === p.get("measure"); }) ? p.get("measure") : "requests";
    state.split = p.has("split") ? p.get("split") : "status_class";
    state.group = p.has("group") ? p.get("group") : p.get("top") || "path";      // top=: links from before the redesign
    state.sort = p.get("sort") || "requests";
    state.live = (p.get("follow") === "1" || p.get("live") === "1") && !!state.range;
    state.filters = [];
    p.forEach(function (v, k) { if (isFilterKey(k)) state.filters.push([k, v]); });
  }
  function writeUrl(push) {
    var p = new URLSearchParams();
    BV.rangeToUrl(state, p);
    state.filters.forEach(function (f) { p.append(f[0], f[1]); });
    if (state.view !== "timeseries") p.set("view", state.view);
    if (state.measure !== "requests") p.set("measure", state.measure);
    if (state.split !== "status_class") p.set("split", state.split);
    if (state.group !== "path") p.set("group", state.group);
    if (state.sort !== "requests") p.set("sort", state.sort);
    if (state.live) p.set("follow", "1");
    var url = location.pathname + "?" + p.toString();
    if (push) history.pushState(null, "", url); else history.replaceState(null, "", url);
  }
  function win() { return BV.rangeWindow(state); }
  function apiParams(extra) {
    var w = win(), p = new URLSearchParams();
    p.set("from", Math.floor(w.from));
    p.set("to", Math.ceil(w.to));
    state.filters.forEach(function (f) { p.append(f[0], f[1]); });
    Object.keys(extra || {}).forEach(function (k) { if (extra[k] !== null && extra[k] !== undefined) p.set(k, extra[k]); });
    return p;
  }

  /* ---------- labels and formats ---------- */
  function fieldLabel(name) {
    if (!name) return "Nothing (totals)";
    if (name.indexOf("h.") === 0) return name.slice(2);
    if (name === "min_ms") return "Duration ≥";
    if (name === "max_ms") return "Duration ≤";
    if (name === "status_class") return "Status class";
    var f = meta && meta.fields.find(function (x) { return x.name === name; });
    return f ? f.label : name;
  }
  function filterField(field) { return field === "status_class" ? "status" : field; }   // 4xx is a status filter
  function formats(unit) {
    if (unit === "bytes") return { axis: BV.bytes, tip: BV.bytes };
    if (unit === "us") return { axis: function (v) { return v === 0 ? "0" : BV.dur(v); }, tip: BV.dur };
    if (unit === "percent") return { axis: function (v) { return v + "%"; }, tip: function (v) { return v.toFixed(2) + "%"; } };
    return { axis: BV.compact, tip: BV.int };
  }
  function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }

  /* ---------- range and filters ---------- */
  function buildRangeMenu() {
    BV.buildRangeMenu($("rangeMenu"), state, meta && meta.retention.raw_days, function (r) {
      state.range = r.range; state.from = r.from; state.to = r.to;
      if (!r.range) state.live = false;
      writeUrl(true); reloadAll();
    });
  }
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
      if (errors[name]) { delete errors[name]; renderBanner(); }
      onData(d);
    }).catch(function (e) {
      if (e.name === "AbortError") return;
      if (card) card.classList.remove("loading");
      inflight[name] = null;
      errors[name] = e.message; renderBanner();
    });
  }
  function renderBanner() {
    var msgs = Object.keys(errors).map(function (k) { return errors[k]; });
    var uniq = msgs.filter(function (m, i) { return msgs.indexOf(m) === i; });
    $("banner").hidden = !uniq.length;
    $("banner").textContent = uniq.join(" · ");
  }

  /* ---------- the top panel ---------- */
  function renderControls() {
    var ts = state.view === "timeseries";
    $("viewTs").setAttribute("aria-selected", String(ts));
    $("viewBd").setAttribute("aria-selected", String(!ts));
    $("tsPanel").hidden = !ts;
    $("bdPanel").hidden = ts;
    document.querySelectorAll(".ts-only").forEach(function (n) { n.hidden = !ts; });
    document.querySelectorAll(".bd-only").forEach(function (n) { n.hidden = ts; });
    $("splitCtl").hidden = !ts || !SPLITTABLE[state.measure];
    $("measureSel").value = state.measure;
    $("splitSel").value = state.split;
    $("groupSel").value = state.group;
  }
  function loadViz() {
    renderControls();
    if (state.view === "timeseries") loadSeries(); else loadBreakdown();
  }
  function buckets() {
    // the chart area's width right now (the panel may just have been shown), less the y-axis labels
    var w = $("tsChart").clientWidth - 60;
    if (w <= 0) w = $("vizCard").clientWidth - 92;
    return Math.max(20, Math.min(240, Math.floor(w / 7)));
  }
  function loadSeries() {
    var split = SPLITTABLE[state.measure] ? state.split || null : null;
    load("viz", $("vizCard"), "/api/series", apiParams({ buckets: buckets(), split: split, measure: state.measure }),
      function (d) {
        if (split && split !== "status_class") {
          slots.assign(d.series.map(function (s) { return s.key; }).filter(function (k) { return k !== "Other"; }));
        }
        var lineColors = { p50: cssVar("--s1"), p95: cssVar("--s2"), p99: cssVar("--s3"),
          "4xx": BV.statusClassColor("4xx"), "5xx": BV.statusClassColor("5xx") };
        var series = d.series.map(function (s) {
          var color = d.kind === "lines" ? lineColors[s.key]
            : split === "status_class" ? BV.statusClassColor(s.key)
            : split ? slots.color(s.key) : cssVar("--s1");
          var label = s.key === "all" ? MEASURES.find(function (m) { return m[0] === d.measure; })[1]
            : s.key === "p50" ? "p50 (median)" : s.key;
          return { key: s.key, label: label, color: color, values: s.values };
        });
        var f = formats(d.unit);
        chartOpts = { kind: d.kind, start: d.start, step: d.step, n: d.buckets, series: series, height: 260,
          yFormat: f.axis, tipFormat: f.tip, label: "Matching requests over time", onZoom: zoom, onClick: jumpTo,
          emptyText: d.measure === "latency" ? "No timed requests in this range" : "No requests in this range" };
        $("vizSub").textContent = "per " + BV.span(d.step).replace(/^1 /, "") + " · " + BV.rangeLabel(state) +
          (d.approx ? " · estimated" : "");
        BV.legend($("vizLegend"), series, d.kind);
        if (chart) chart.update(chartOpts); else { chart = BV.chart($("tsChart"), chartOpts); $("tsChart").classList.add("clickable"); }
        if (!$("tsTable").hidden) BV.chartTable($("tsTable"), chartOpts);
        if (logs.jump) chart.mark(bucketOf(logs.jump - 1));
      });
  }
  function bucketOf(ms) {
    if (!chartOpts) return -1;
    var i = Math.floor((ms - chartOpts.start) / chartOpts.step);
    return i >= 0 && i < chartOpts.n ? i : -1;
  }
  function zoom(from, to) {
    if (to - from < 2000) return;
    state.range = null; state.from = from; state.to = to; state.live = false;
    writeUrl(true);
    reloadAll();
  }
  function jumpTo(i) {
    // the requests behind a bar: the log, starting from the end of that bucket and going back
    var end = Math.min(chartOpts.start + (i + 1) * chartOpts.step, win().to);
    logs.jump = end;
    chart.mark(i);
    loadLogs();
  }

  function loadBreakdown() {
    var by = state.group || "none";
    load("viz", $("vizCard"), "/api/breakdown", apiParams({ by: by, sort: state.sort, limit: 50, unique: 1 }), function (d) {
      bd = d;
      $("vizSub").textContent = (d.by ? BV.int(d.rows.length) + " " + (d.rows.length === 1 ? "group" : "groups") +
        (d.rows.length === 50 ? " shown (top 50)" : "") + " · " : "") + BV.rangeLabel(state);
      var t = BV.clear($("bdTable"));
      var head = el("tr", null, el("th", { text: d.by ? fieldLabel(d.by) : "" }));
      BD_COLS.forEach(function (c) {
        var th = el("th", { class: "num" + (c[0] ? " sortable" : ""), text: c[1],
          "aria-sort": c[0] && (state.sort === c[0] || (state.sort === "errors" && c[0] === "status_5xx")) ? "descending" : null });
        if (c[0]) {
          th.tabIndex = 0;
          th.title = "Sort by " + c[1];
          var sortBy = function () { state.sort = c[0]; writeUrl(false); loadBreakdown(); };
          th.addEventListener("click", sortBy);
          th.addEventListener("keydown", function (ev) { if (ev.key === "Enter") sortBy(); });
        }
        head.appendChild(th);
      });
      if (d.by) head.appendChild(el("th"));
      t.appendChild(el("thead", null, head));
      var body = el("tbody");
      if (!d.rows.length) body.appendChild(el("tr", null, el("td", { colspan: 10, class: "hint", text: "No requests match." })));
      d.rows.forEach(function (r) {
        var name = el("td", { class: "name" + (d.by === "user_agent" ? "" : " mono") },
          el("span", { title: r.value, text: d.by ? r.value : "All matching requests" }),
          d.by ? el("span", { class: "share" }, el("i", { style: "width:" + Math.max(1, Math.round(r.share * 100)) + "%" })) : null);
        var tr = el("tr", { "data-value": r.value }, name, BD_COLS.map(function (c) {
          return el("td", { class: "num", text: c[2](r, d) });
        }));
        if (d.by) {
          tr.appendChild(el("td", { class: "acts" },
            el("button", { type: "button", title: "Only " + r.value, "aria-label": "Filter to " + r.value,
              onclick: function () { addFilter(filterField(d.by), r.value, false); } }, "="),
            el("button", { type: "button", title: "Exclude " + r.value, "aria-label": "Exclude " + r.value,
              onclick: function () { addFilter(filterField(d.by), r.value, true); } }, "≠")));
        }
        body.appendChild(tr);
      });
      t.appendChild(body);
    });
  }

  /* ---------- the log ---------- */
  function multiInstance() { return meta && meta.instances.length > 1; }
  function logsSpanDays() {
    var w = win();
    return new Date(w.from).toDateString() !== new Date(w.to).toDateString();
  }
  function renderLogHead() {
    var head = BV.clear($("logsHead"));
    var cols = multiInstance() ? [["instance", "Instance"]].concat(LOG_COLS) : LOG_COLS;
    cols.forEach(function (c) { head.appendChild(el("th", { class: c[2] || "", text: c[1] })); });
  }
  function groupValue(r) {   // this request's value for the breakdown's grouping, as the breakdown shows it
    if (!bd || !bd.by) return null;
    var v = r[logs.columns.indexOf(bd.by === "status_class" ? "status" : bd.by)];
    if (v === null || v === undefined) return "(none)";
    return bd.by === "status_class" ? Math.floor(v / 100) + "xx" : String(v);
  }
  function highlight(r) {
    if (state.view === "timeseries") {
      if (chart) chart.mark(r ? bucketOf(r[logs.columns.indexOf("ts")]) : (logs.jump ? bucketOf(logs.jump - 1) : -1));
      return;
    }
    var want = r ? groupValue(r) : null;
    document.querySelectorAll("#bdTable tbody tr").forEach(function (tr) {
      tr.classList.toggle("hl", want !== null && tr.getAttribute("data-value") === want);
    });
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
    tr.addEventListener("click", function () { openDetail(r); });
    tr.addEventListener("keydown", function (ev) { if (ev.key === "Enter") openDetail(r); });
    tr.addEventListener("mouseenter", function () { highlight(r); });
    tr.addEventListener("focus", function () { highlight(r); });
    tr.addEventListener("mouseleave", function () { highlight(null); });
    return tr;
  }
  function renderLogs() {
    var body = BV.clear($("logsBody"));
    logs.rows.forEach(function (r) { body.appendChild(logRow(r, false)); });
    if (!logs.rows.length) {
      body.appendChild(el("tr", null, el("td", { colspan: 9, class: "hint",
        text: logs.jump ? "No matching requests at or before that moment." : "No requests match." })));
    }
    $("moreBtn").hidden = !logs.next;
    $("newestBtn").hidden = !logs.jump;
    $("newerBtn").hidden = !logs.jump;
    var parts = [];
    if (logs.total !== null && logs.total !== undefined) parts.push(BV.int(logs.total) + " matching");
    parts.push(logs.jump ? "from " + BV.stamp(logs.jump, logsSpanDays()).replace(/\.\d+$/, "") + " back" : "newest first");
    if (state.live && !logs.jump) parts.push("following");
    $("logsSub").textContent = parts.join(" · ");
  }
  function loadLogs() {
    var extra = { limit: 100 };
    if (logs.jump) extra.before = logs.jump + ":" + NEWEST;
    load("logs", $("logsCard"), "/api/logs", apiParams(extra), function (d) {
      logs.columns = d.columns; logs.rows = d.rows; logs.next = d.next;
      if (!logs.jump) { logs.maxSeq = d.max_seq; logs.total = d.total; }
      // above the top row; with no rows, above the moment itself
      logs.newer = d.rows.length ? [d.rows[0][logs.columns.indexOf("ts")], d.rows[0][0]] : [logs.jump, NEWEST];
      renderLogs();
      if (logs.jump) $("logsCard").scrollIntoView({ block: "nearest", behavior: "smooth" });
    });
  }
  function loadNewer() {
    if (!logs.jump || !logs.newer) return;
    load("logsNewer", null, "/api/logs", apiParams({ limit: 100, since: logs.newer[0] + ":" + logs.newer[1] }), function (d) {
      var wrap = $("logWrap"), body = $("logsBody"), before = wrap.scrollHeight;
      if (body.firstChild && !body.firstChild.hasAttribute("tabindex")) BV.clear(body);   // the "none" row
      for (var i = d.rows.length - 1; i >= 0; i--) body.insertBefore(logRow(d.rows[i], true), body.firstChild);
      wrap.scrollTop += wrap.scrollHeight - before;          // keep the rows you were reading where they were
      logs.rows = d.rows.concat(logs.rows);
      if (d.rows.length) logs.newer = [d.rows[0][logs.columns.indexOf("ts")], d.rows[0][0]];
      if (!d.newer) {
        // caught up with the newest matching request: back to the normal log (Follow resumes from here)
        logs.jump = null;
        logs.maxSeq = logs.rows.reduce(function (m, r) { return Math.max(m, r[0]); }, logs.maxSeq || 0);
        if (chart) chart.mark(-1);
      }
      // hiding the button bar would pull the log up by its height: scroll the page by the same amount
      var top0 = wrap.getBoundingClientRect().top;
      $("newerBtn").hidden = !logs.jump;
      $("newestBtn").hidden = !logs.jump;
      window.scrollBy(0, wrap.getBoundingClientRect().top - top0);
      var parts = [];
      if (logs.total !== null && logs.total !== undefined) parts.push(BV.int(logs.total) + " matching");
      parts.push(logs.jump ? "from " + BV.stamp(logs.rows[0][logs.columns.indexOf("ts")], logsSpanDays()).replace(/\.\d+$/, "") + " back" : "newest first");
      if (state.live && !logs.jump) parts.push("following");
      $("logsSub").textContent = parts.join(" · ");
    });
  }
  function backToNewest() {
    logs.jump = null;
    if (chart) chart.mark(-1);
    loadLogs();
  }
  function loadOlder() {
    if (!logs.next) return;
    load("logsMore", null, "/api/logs", apiParams({ limit: 100, before: logs.next[0] + ":" + logs.next[1] }), function (d) {
      logs.rows = logs.rows.concat(d.rows); logs.next = d.next;
      renderLogs();
    });
  }
  function pollLive() {
    if (!state.live || logs.jump) return;      // while looking at an earlier moment, new rows wait
    // up to the real now (the range itself ends on a 5 s boundary), so no new row is skipped
    BV.api("/api/logs", apiParams({ limit: 200, after: logs.maxSeq || 0, to: Date.now() + 60000 })).then(function (d) {
      if (!d.rows.length || logs.jump) return;
      logs.maxSeq = d.max_seq;
      if (logs.total !== null) logs.total += d.rows.length;
      logs.rows = d.rows.concat(logs.rows).slice(0, 500);
      var body = $("logsBody");
      if (body.firstChild && !body.firstChild.hasAttribute("tabindex")) BV.clear(body);
      for (var i = d.rows.length - 1; i >= 0; i--) body.insertBefore(logRow(d.rows[i], true), body.firstChild);
      while (body.children.length > 500) body.removeChild(body.lastChild);
      var parts = [];
      if (logs.total !== null) parts.push(BV.int(logs.total) + " matching");
      $("logsSub").textContent = parts.concat(["newest first", "following"]).join(" · ");
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
    $("rangeBtn").textContent = "◷ " + BV.rangeLabel(state);
    renderChips();
    buildRangeMenu();
    $("liveBtn").setAttribute("aria-pressed", String(state.live));
    $("liveDot").className = "dot" + (state.live ? " live" : "");
    logs.jump = null;
    loadViz(); loadLogs();
    clearInterval(timers.logs);
    if (state.live) timers.logs = setInterval(function () { if (!document.hidden) pollLive(); }, FOLLOW_MS);
  }

  function init() {
    BV.initTheme($("themeBtn"), function () { if (state.view === "timeseries") loadSeries(); });
    BV.initIngestStatus($("ingestDot"), $("ingestText"), $("statusBanner"));
    BV.api("/api/fields").then(function (m) {
      meta = m;
      readUrl();
      var measure = BV.clear($("measureSel")), split = BV.clear($("splitSel")), group = BV.clear($("groupSel"));
      MEASURES.forEach(function (x) { measure.appendChild(el("option", { value: x[0], text: x[1] })); });
      split.appendChild(el("option", { value: "", text: "Nothing" }));
      group.appendChild(el("option", { value: "", text: "Nothing (totals)" }));
      meta.splits.forEach(function (s) {
        split.appendChild(el("option", { value: s, text: fieldLabel(s) }));
        group.appendChild(el("option", { value: s, text: fieldLabel(s) }));
      });
      buildAddForm();
      renderLogHead();
      reloadAll();
    }).catch(function (e) { errors.fields = e.message; renderBanner(); });

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
    var setView = function (v) { if (state.view === v) return; state.view = v; writeUrl(false); highlight(null); loadViz(); };
    $("viewTs").addEventListener("click", function () { setView("timeseries"); });
    $("viewBd").addEventListener("click", function () { setView("breakdown"); });
    $("measureSel").addEventListener("change", function () { state.measure = this.value; writeUrl(false); loadViz(); });
    $("splitSel").addEventListener("change", function () { state.split = this.value; writeUrl(false); loadViz(); });
    $("groupSel").addEventListener("change", function () { state.group = this.value; writeUrl(false); loadViz(); });
    $("tableBtn").addEventListener("click", function () {
      var on = this.getAttribute("aria-pressed") !== "true";
      this.setAttribute("aria-pressed", String(on));
      $("tsTable").hidden = !on;
      if (on && chartOpts) BV.chartTable($("tsTable"), chartOpts);
    });
    $("moreBtn").addEventListener("click", loadOlder);
    $("newestBtn").addEventListener("click", backToNewest);
    $("newerBtn").addEventListener("click", loadNewer);
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
      if (!document.hidden && state.live) pollLive();
    });
  }
  init();
})();
