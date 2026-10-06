/* Overview: the whole proxy at a glance for a time range (and optionally one instance). Every row links into Explore
   with that value as a filter and the same range, so the overview is where a question starts, Explore where it is
   answered. Relative ranges refresh themselves every minute while the tab is visible. */
"use strict";
(function () {
  var el = BV.el, $ = BV.$;
  var REFRESH_MS = 60000;
  var meta = null;
  var state = { range: "24h", from: null, to: null, instance: null, by: "router", sort: "requests" };
  var chart = null, chartOpts = null, timer = null;
  var inflight = {}, errors = {};

  /* ---------- URL ---------- */
  function readUrl() {
    var p = new URLSearchParams(location.search);
    var r = BV.rangeFromUrl(p, "24h");
    state.range = r.range; state.from = r.from; state.to = r.to;
    state.instance = p.get("instance");
    state.by = p.get("by") || "router";
    state.sort = p.get("sort") || "requests";
  }
  function writeUrl(push) {
    var p = new URLSearchParams();
    BV.rangeToUrl(state, p);
    if (state.instance) p.set("instance", state.instance);
    if (state.by !== "router") p.set("by", state.by);
    if (state.sort !== "requests") p.set("sort", state.sort);
    var url = location.pathname + "?" + p.toString();
    if (push) history.pushState(null, "", url); else history.replaceState(null, "", url);
  }
  function win() { return BV.rangeWindow(state); }
  function params(extra, w) {
    w = w || win();
    var p = new URLSearchParams();
    p.set("from", Math.floor(w.from));
    p.set("to", Math.ceil(w.to));
    if (state.instance) p.set("instance", state.instance);
    Object.keys(extra || {}).forEach(function (k) { if (extra[k] !== null && extra[k] !== undefined) p.set(k, extra[k]); });
    return p;
  }
  // a link into Explore: same range, this instance, plus the given filters ([field, value] pairs) and options
  function exploreUrl(filters, extra) {
    var p = new URLSearchParams();
    BV.rangeToUrl(state, p);
    if (state.instance) p.append("instance", state.instance);
    (filters || []).forEach(function (f) { p.append(f[0], f[1]); });
    Object.keys(extra || {}).forEach(function (k) { p.set(k, extra[k]); });
    return "/explore?" + p.toString();
  }

  /* ---------- fetching ---------- */
  function load(name, card, path, p, onData) {
    if (inflight[name]) inflight[name].abort();
    var ctl = new AbortController();
    inflight[name] = ctl;
    if (card) card.classList.add("loading");
    return BV.api(path, p, ctl.signal).then(function (d) {
      if (inflight[name] !== ctl) return;
      inflight[name] = null;
      if (card) card.classList.remove("loading");
      if (errors[name]) { delete errors[name]; banner(); }
      onData(d);
    }).catch(function (e) {
      if (e.name === "AbortError") return;
      inflight[name] = null;
      if (card) card.classList.remove("loading");
      errors[name] = e.message; banner();
    });
  }
  function banner() {
    var msgs = Object.keys(errors).map(function (k) { return errors[k]; });
    msgs = msgs.filter(function (m, i) { return msgs.indexOf(m) === i; });
    $("banner").hidden = !msgs.length;
    $("banner").textContent = msgs.join(" · ");
  }

  /* ---------- status strip ---------- */
  function loadStrip() {
    BV.api("/api/status").then(function (s) {
      var ing = s.ingest, st = s.storage;
      var bad = ing.sources.reduce(function (a, x) { return a + x.bad_lines; }, 0);
      var unread = ing.sources.reduce(function (a, x) { return a + (x.unread_bytes || 0); }, 0);
      var errs = ing.sources.filter(function (x) { return x.error; });
      var ok = ing.alive && !errs.length;
      var box = BV.clear($("strip"));
      var item = function () {
        var span = el("span", { class: "it" });
        for (var i = 0; i < arguments.length; i++) BV.append(span, arguments[i]);
        box.appendChild(span);
      };
      item(el("span", { class: "dot " + (ok ? "good" : "bad") }),
        el("b", { text: !ing.alive ? "Ingest stopped" : errs.length ? "Can't read a log" : unread > 1e6 ? "Catching up" : "Ingesting" }),
        ing.newest_ms ? " · newest request " + BV.ago(ing.newest_ms) : " · no requests yet");
      if (errs.length) item(el("span", { text: errs[0].path + ": " + errs[0].error }));
      item(el("b", { text: BV.compact(ing.rows_per_sec) }), " req/s ingested");
      if (unread > 1e6) item(el("b", { text: BV.bytes(unread) }), " behind");
      item(el("b", { text: BV.int(bad) }), bad === 1 ? " unreadable line" : " unreadable lines");
      item(el("b", { text: BV.compact(st.rows) }), " requests stored · ", el("b", { text: BV.bytes(st.db_bytes) }));
      if (st.oldest_ms) item("since ", el("b", { text: BV.day(st.oldest_ms) }), " · keeping " + s.retention.raw_days + " days");
      if (ing.sources.length > 1) item(el("b", { text: String(ing.sources.length) }), " logs");
    }).catch(function () {
      BV.clear($("strip")).appendChild(el("span", { class: "it" }, el("span", { class: "dot bad" }), "Status unavailable"));
    });
  }

  /* ---------- tiles ---------- */
  function delta(cur, prev, kind) {
    // kind: "neutral" (more is neither good nor bad), "bad-up" (more is worse); rates compare in percentage points
    if (prev === null || prev === undefined || cur === null || cur === undefined) return null;
    var text, dir;
    if (kind === "rate") {
      var pp = (cur - prev) * 100;
      if (Math.abs(pp) < 0.05) return { text: "no change", cls: "" };
      dir = pp > 0 ? 1 : -1;
      text = (dir > 0 ? "▲ " : "▼ ") + Math.abs(pp).toFixed(Math.abs(pp) < 1 ? 2 : 1) + " pp";
      return { text: text, cls: dir > 0 ? "bad" : "good" };
    }
    if (!prev) return cur ? { text: "new", cls: "" } : null;
    var pct = (cur - prev) / prev * 100;
    if (Math.abs(pct) < 0.5) return { text: "no change", cls: "" };
    dir = pct > 0 ? 1 : -1;
    text = (dir > 0 ? "▲ " : "▼ ") + (Math.abs(pct) >= 100 ? Math.round(Math.abs(pct)) : Math.abs(pct).toFixed(1)) + "%";
    return { text: text, cls: kind === "bad-up" ? (dir > 0 ? "bad" : "good") : "" };
  }
  function loadTiles() {
    var w = win(), span = w.to - w.from;
    var prevW = { from: w.from - span, to: w.from };
    $("tiles").classList.add("loading");
    Promise.all([BV.api("/api/summary", params(null, w)), BV.api("/api/summary", params(null, prevW))]).then(function (r) {
      $("tiles").classList.remove("loading");
      var s = r[0], p = r[1];
      var rate = function (x, n) { return x.requests ? n / x.requests : null; };
      var vs = "vs previous " + BV.span(span);
      var tiles = [
        ["Requests", BV.compact(s.requests), delta(s.requests, p.requests, "neutral")],
        ["Requests/s", s.rps < 10 ? String(Math.round(s.rps * 100) / 100) : BV.compact(s.rps), delta(s.rps, p.rps, "neutral")],
        ["Unique IPs", (s.unique_ips_approx ? "≈ " : "") + BV.compact(s.unique_ips), delta(s.unique_ips, p.unique_ips, "neutral")],
        ["5xx rate", BV.pct(rate(s, s.status_5xx)), delta(rate(s, s.status_5xx), rate(p, p.status_5xx), "rate")],
        ["4xx rate", BV.pct(rate(s, s.status_4xx)), delta(rate(s, s.status_4xx), rate(p, p.status_4xx), "rate")],
        ["p95", BV.dur(s.p95_us), delta(s.p95_us, p.p95_us, "bad-up")],
        ["Sent", BV.bytes(s.bytes), delta(s.bytes, p.bytes, "neutral")],
      ];
      var box = BV.clear($("tiles"));
      tiles.forEach(function (t) {
        var d = t[2];
        box.appendChild(el("div", { class: "tile" }, el("div", { class: "label", text: t[0] }),
          el("div", { class: "value", text: t[1] }),
          d ? el("div", { class: "delta " + d.cls, title: vs, text: d.text + " " + (d.text === "no change" || d.text === "new" ? "" : vs) }) : null));
      });
      if (errors.tiles) { delete errors.tiles; banner(); }
    }).catch(function (e) { $("tiles").classList.remove("loading"); errors.tiles = e.message; banner(); });
  }

  /* ---------- chart ---------- */
  function loadSeries() {
    var width = chart ? chart.width() : $("seriesChart").clientWidth - 60;
    var buckets = Math.max(20, Math.min(240, Math.floor(width / 7)));
    load("series", $("seriesCard"), "/api/series", params({ buckets: buckets, split: "status_class" }), function (d) {
      var series = d.series.map(function (s) {
        return { key: s.key, label: s.key, color: BV.statusClassColor(s.key), values: s.values };
      });
      chartOpts = { kind: "columns", start: d.start, step: d.step, n: d.buckets, series: series, height: 220,
        label: "Requests over time by status class", onZoom: function (f, t) {
          state.range = null; state.from = f; state.to = t; writeUrl(true); reloadAll();
        } };
      $("seriesSub").textContent = "per " + BV.span(d.step).replace(/^1 /, "") + " · " + BV.rangeLabel(state);
      BV.legend($("seriesLegend"), series, "columns");
      if (chart) chart.update(chartOpts); else chart = BV.chart($("seriesChart"), chartOpts);
      if (!$("seriesTable").hidden) BV.chartTable($("seriesTable"), chartOpts);
    });
  }

  /* ---------- tables ---------- */
  function link(text, href, title) { return el("a", { href: href, title: title || text, text: text }); }
  function shareCell(share) {
    return el("span", { class: "share" }, el("i", { style: "width:" + Math.max(1, Math.round(share * 100)) + "%" }));
  }
  function loadBreakdown() {
    load("breakdown", $("servicesCard"), "/api/breakdown", params({ by: state.by, sort: state.sort, limit: 15 }), function (b) {
      var t = BV.clear($("servicesTable"));
      t.appendChild(el("thead", null, el("tr", null,
        el("th", { text: { router: "Router", service: "Service", host: "Host" }[b.by] }),
        el("th", { class: "num", text: "Req/s" }), el("th", { class: "num", text: "Requests" }),
        el("th", { class: "num", text: "4xx" }), el("th", { class: "num", text: "5xx" }),
        el("th", { class: "num", text: "p50" }), el("th", { class: "num", text: "p95" }),
        el("th", { class: "num", text: "Sent" }))));
      var body = el("tbody");
      if (!b.rows.length) body.appendChild(el("tr", null, el("td", { colspan: 8, class: "hint", text: "No requests in this range." })));
      b.rows.forEach(function (r) {
        var errPct = function (n) { return r.requests ? BV.pct(n / r.requests) : "–"; };
        body.appendChild(el("tr", null,
          el("td", { class: "name" }, link(r.value, exploreUrl([[b.by, r.value]])), shareCell(r.share)),
          el("td", { class: "num", text: r.rps < 10 ? String(Math.round(r.rps * 100) / 100) : BV.compact(r.rps) }),
          el("td", { class: "num", text: BV.int(r.requests) }),
          el("td", { class: "num" }, r.status_4xx ? link(errPct(r.status_4xx), exploreUrl([[b.by, r.value], ["status", "4xx"]]), BV.int(r.status_4xx) + " requests") : "0"),
          el("td", { class: "num" }, r.status_5xx ? link(errPct(r.status_5xx), exploreUrl([[b.by, r.value], ["status", "5xx"]]), BV.int(r.status_5xx) + " requests") : "0"),
          el("td", { class: "num", text: BV.dur(r.p50_us) }),
          el("td", { class: "num", text: BV.dur(r.p95_us) }),
          el("td", { class: "num", text: BV.bytes(r.bytes) })));
      });
      t.appendChild(body);
    });
  }
  function loadTop(field, table, card, label) {
    load("top-" + field, card, "/api/top", params({ field: field, limit: 8 }), function (d) {
      var t = BV.clear(table), body = el("tbody");
      if (!d.values.length) body.appendChild(el("tr", null, el("td", { class: "hint", text: "No requests in this range." })));
      d.values.forEach(function (v) {
        var name = label ? label(v.value) : v.value;
        var cell = el("td", { class: "name" + (field === "user_agent" ? "" : " mono") });
        if (field === "status") {
          cell.appendChild(el("span", { class: "status" }, el("i", { style: "background:" + BV.statusColorVar(Number(v.value)) }),
            link(name, exploreUrl([["status", v.value]]))));
        } else {
          cell.appendChild(link(name, exploreUrl([[field, v.value]]), v.value));
        }
        cell.appendChild(shareCell(v.share));
        body.appendChild(el("tr", null, cell, el("td", { class: "num", text: BV.int(v.count) }),
          el("td", { class: "num", text: BV.pct(v.share) })));
      });
      t.appendChild(body);
      if (d.distinct > d.values.length) {
        t.appendChild(el("tfoot", null, el("tr", null, el("td", { colspan: 3, class: "hint",
          text: "≈ " + BV.int(d.distinct) + " distinct" }))));
      }
    });
  }
  function loadErrors() {
    load("errors", $("errorsCard"), "/api/logs", params({ status: "5xx", limit: 10 }), function (d) {
      var col = function (r, name) { return r[d.columns.indexOf(name)]; };
      var w = win(), multiDay = new Date(w.from).toDateString() !== new Date(w.to).toDateString();
      var t = BV.clear($("errorsTable"));
      t.appendChild(el("thead", null, el("tr", null, ["Time", "Status", "Host", "Path", "Client IP", "Router", "Duration"].map(function (h, i) {
        return el("th", { class: i === 6 ? "num" : "", text: h });
      }))));
      var body = el("tbody");
      if (!d.rows.length) body.appendChild(el("tr", null, el("td", { colspan: 7, class: "hint", text: "No server errors in this range." })));
      d.rows.forEach(function (r) {
        var path = col(r, "path") || "";
        body.appendChild(el("tr", null,
          el("td", { class: "mono", text: BV.stamp(col(r, "ts"), multiDay) }),
          el("td", null, el("span", { class: "status" }, el("i", { style: "background:" + BV.statusColorVar(col(r, "status")) }), String(col(r, "status")))),
          el("td", { text: col(r, "host") || "–" }),
          el("td", { class: "mono name" }, link(path, exploreUrl([["status", "5xx"], ["path", path]]))),
          el("td", { class: "mono" }, col(r, "ip") ? link(col(r, "ip"), exploreUrl([["ip", col(r, "ip")]])) : "–"),
          el("td", { text: col(r, "router") || "–" }),
          el("td", { class: "num", text: BV.dur(col(r, "duration_us")) })));
      });
      t.appendChild(body);
    });
    $("errorsLink").href = exploreUrl([["status", "5xx"]]);
  }

  /* ---------- orchestration ---------- */
  function reloadAll() {
    $("rangeBtn").textContent = "◷ " + BV.rangeLabel(state);
    BV.buildRangeMenu($("rangeMenu"), state, meta && meta.retention.raw_days, function (r) {
      state.range = r.range; state.from = r.from; state.to = r.to;
      writeUrl(true); reloadAll();
    });
    $("byRouter").value = state.by;
    $("bySort").value = state.sort;
    $("exploreNav").href = exploreUrl([]);
    document.querySelectorAll("a.go[data-top]").forEach(function (a) {
      a.href = exploreUrl([], { top: a.getAttribute("data-top") });
    });
    loadStrip(); loadTiles(); loadSeries(); loadBreakdown();
    loadTop("status", $("statusTable"), $("statusCard"));
    loadTop("path", $("pathsTable"), $("pathsCard"));
    loadTop("ip", $("ipsTable"), $("ipsCard"));
    loadTop("user_agent", $("uasTable"), $("uasCard"));
    loadTop("host", $("hostsTable"), $("hostsCard"));
    loadErrors();
    clearInterval(timer);
    if (state.range) timer = setInterval(function () { if (!document.hidden) reloadAll(); }, REFRESH_MS);
  }

  function init() {
    BV.initTheme($("themeBtn"), function () { if (chartOpts) loadSeries(); });
    BV.initIngestStatus($("ingestDot"), $("ingestText"));
    BV.menu($("rangeBtn"), $("rangeMenu"));
    document.addEventListener("keydown", function (ev) { if (ev.key === "Escape") BV.closeMenus(); });
    readUrl();
    BV.api("/api/fields").then(function (m) {
      meta = m;
      var sel = $("instanceSel");
      if (m.instances.length > 1) {
        sel.hidden = false;
        sel.appendChild(el("option", { value: "", text: "All instances" }));
        m.instances.forEach(function (i) { sel.appendChild(el("option", { value: i.instance, text: i.instance })); });
        sel.value = state.instance || "";
        sel.addEventListener("change", function () { state.instance = this.value || null; writeUrl(true); reloadAll(); });
      }
      reloadAll();
    }).catch(function (e) { errors.fields = e.message; banner(); reloadAll(); });
    $("byRouter").addEventListener("change", function () { state.by = this.value; writeUrl(false); loadBreakdown(); });
    $("bySort").addEventListener("change", function () { state.sort = this.value; writeUrl(false); loadBreakdown(); });
    $("refreshBtn").addEventListener("click", reloadAll);
    $("seriesTableBtn").addEventListener("click", function () {
      var on = this.getAttribute("aria-pressed") !== "true";
      this.setAttribute("aria-pressed", String(on));
      $("seriesTable").hidden = !on;
      if (on && chartOpts) BV.chartTable($("seriesTable"), chartOpts);
    });
    window.addEventListener("popstate", function () { readUrl(); reloadAll(); });
  }
  init();
})();
