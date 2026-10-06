/* Overview: the whole proxy at a glance for a time range (and optionally one instance). Every row links into Explore
   with that value as a filter and the same range, so the overview is where a question starts, Explore where it is
   answered.

   Live (on by default for the preset ranges) keeps the page current in two tiers: what the per-minute rollups
   answer (chart, traffic table, status codes, hosts, recent 5xx) refreshes often; what scans raw rows (tiles with
   unique IPs, top paths, IPs, user agents) refreshes at a rate that falls with the range. Updates land in place, and
   nothing refreshes while the tab is hidden. */
"use strict";
(function () {
  var el = BV.el, $ = BV.$;
  var meta = null;
  var state = { range: "24h", from: null, to: null, instance: null, by: "router", sort: "requests", live: true };
  var chart = null, chartOpts = null, timers = {};
  var seenErrors = null;            // seq numbers in the recent-5xx table, to mark the rows that arrive live
  var inflight = {}, errors = {};

  /* ---------- URL ---------- */
  function readUrl() {
    var p = new URLSearchParams(location.search);
    var r = BV.rangeFromUrl(p, "24h");
    state.range = r.range; state.from = r.from; state.to = r.to;
    state.instance = p.get("instance");
    state.by = p.get("by") || "router";
    state.sort = p.get("sort") || "requests";
    state.live = !!state.range && p.get("live") !== "0";
  }
  function writeUrl(push) {
    var p = new URLSearchParams();
    BV.rangeToUrl(state, p);
    if (state.instance) p.set("instance", state.instance);
    if (state.by !== "router") p.set("by", state.by);
    if (state.sort !== "requests") p.set("sort", state.sort);
    if (state.range && !state.live) p.set("live", "0");
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
  function load(name, card, path, p, onData, quiet) {
    if (inflight[name]) {
      if (quiet) return;            // a live refresh never interrupts a load already on its way
      inflight[name].abort();
    }
    var ctl = new AbortController();
    inflight[name] = ctl;
    if (card && !quiet) card.classList.add("loading");
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
  function loadTiles(quiet) {
    var w = win(), span = w.to - w.from;
    var prevW = { from: w.from - span, to: w.from };
    if (!quiet) $("tiles").classList.add("loading");
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
  function loadSeries(quiet) {
    var width = chart ? chart.width() : $("seriesChart").clientWidth - 60;
    var buckets = Math.max(20, Math.min(240, Math.floor(width / 7)));
    load("series", $("seriesCard"), "/api/series", params({ buckets: buckets, split: "status_class" }), function (d) {
      var series = d.series.map(function (s) {
        return { key: s.key, label: s.key, color: BV.statusClassColor(s.key), values: s.values };
      });
      chartOpts = { kind: "columns", start: d.start, step: d.step, n: d.buckets, series: series, height: 220,
        label: "Requests over time by status class", onZoom: function (f, t) {
          state.range = null; state.from = f; state.to = t; state.live = false; writeUrl(true); reloadAll();
        } };
      $("seriesSub").textContent = "per " + BV.span(d.step).replace(/^1 /, "") + " · " + BV.rangeLabel(state);
      BV.legend($("seriesLegend"), series, "columns");
      if (chart) chart.update(chartOpts); else chart = BV.chart($("seriesChart"), chartOpts);
      if (!$("seriesTable").hidden) BV.chartTable($("seriesTable"), chartOpts);
    }, quiet);
  }

  /* ---------- tables ---------- */
  function link(text, href, title) { return el("a", { href: href, title: title || text, text: text }); }
  function shareCell(share) {
    return el("span", { class: "share" }, el("i", { style: "width:" + Math.max(1, Math.round(share * 100)) + "%" }));
  }
  function loadBreakdown(quiet) {
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
    }, quiet);
  }
  function loadTop(field, table, card, quiet) {
    load("top-" + field, card, "/api/top", params({ field: field, limit: 8 }), function (d) {
      var t = BV.clear(table), body = el("tbody");
      if (!d.values.length) body.appendChild(el("tr", null, el("td", { class: "hint", text: "No requests in this range." })));
      d.values.forEach(function (v) {
        var name = v.value;
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
    }, quiet);
  }
  function loadErrors(quiet) {
    load("errors", $("errorsCard"), "/api/logs", params({ status: "5xx", limit: 10 }), function (d) {
      var seqs = d.rows.map(function (r) { return r[d.columns.indexOf("seq")]; });
      var before = seenErrors;
      seenErrors = seqs;
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
        var fresh = before && before.indexOf(col(r, "seq")) < 0;
        body.appendChild(el("tr", { class: fresh ? "fresh" : null },
          el("td", { class: "mono", text: BV.stamp(col(r, "ts"), multiDay) }),
          el("td", null, el("span", { class: "status" }, el("i", { style: "background:" + BV.statusColorVar(col(r, "status")) }), String(col(r, "status")))),
          el("td", { text: col(r, "host") || "–" }),
          el("td", { class: "mono name" }, link(path, exploreUrl([["status", "5xx"], ["path", path]]))),
          el("td", { class: "mono" }, col(r, "ip") ? link(col(r, "ip"), exploreUrl([["ip", col(r, "ip")]])) : "–"),
          el("td", { text: col(r, "router") || "–" }),
          el("td", { class: "num", text: BV.dur(col(r, "duration_us")) })));
      });
      t.appendChild(body);
    }, quiet);
    $("errorsLink").href = exploreUrl([["status", "5xx"]]);
  }

  /* ---------- orchestration ---------- */
  function reloadAll() {
    $("rangeBtn").textContent = "◷ " + BV.rangeLabel(state);
    BV.buildRangeMenu($("rangeMenu"), state, meta && meta.retention.raw_days, function (r) {
      state.range = r.range; state.from = r.from; state.to = r.to;
      state.live = !!r.range;
      writeUrl(true); reloadAll();
    });
    $("byRouter").value = state.by;
    $("bySort").value = state.sort;
    $("exploreNav").href = exploreUrl([]);
    document.querySelectorAll("a.go[data-top]").forEach(function (a) {
      a.href = exploreUrl([], { top: a.getAttribute("data-top") });
    });
    $("liveBtn").setAttribute("aria-pressed", String(state.live));
    $("liveDot").className = "dot" + (state.live ? " live" : "");
    $("liveBtn").title = state.range ? "Keep this page up to date" : "Live follows the latest data: switches to the last hour";
    seenErrors = null;
    refreshFast(false);
    refreshSlow(false);
    schedule();
  }
  // what the rollups answer: cheap at any range
  function refreshFast(quiet) {
    loadSeries(quiet); loadBreakdown(quiet);
    loadTop("status", $("statusTable"), $("statusCard"), quiet);
    loadTop("host", $("hostsTable"), $("hostsCard"), quiet);
    loadErrors(quiet);
  }
  // what scans raw rows: unique IPs in the tiles, the raw-field top lists
  function refreshSlow(quiet) {
    loadTiles(quiet);
    loadTop("path", $("pathsTable"), $("pathsCard"), quiet);
    loadTop("ip", $("ipsTable"), $("ipsCard"), quiet);
    loadTop("user_agent", $("uasTable"), $("uasCard"), quiet);
  }
  function intervals() {
    var w = win(), span = w.to - w.from;
    if (span <= 3600e3) return { fast: 10e3, slow: 10e3 };
    if (span <= 86400e3) return { fast: 15e3, slow: 30e3 };
    return { fast: 30e3, slow: 60e3 };
  }
  function schedule() {
    clearInterval(timers.fast); clearInterval(timers.slow);
    if (!state.live || !state.range) return;
    var iv = intervals();
    timers.fast = setInterval(function () { if (!document.hidden) refreshFast(true); }, iv.fast);
    timers.slow = setInterval(function () { if (!document.hidden) refreshSlow(true); }, iv.slow);
  }

  function init() {
    BV.initTheme($("themeBtn"), function () { if (chartOpts) loadSeries(); });
    BV.initIngestStatus($("ingestDot"), $("ingestText"), $("statusBanner"));
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
    $("liveBtn").addEventListener("click", function () {
      state.live = !state.live;
      if (state.live && !state.range) { state.range = "1h"; state.from = state.to = null; }
      writeUrl(false);
      reloadAll();
    });
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden && state.live && state.range) { refreshFast(true); refreshSlow(true); }
    });
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
