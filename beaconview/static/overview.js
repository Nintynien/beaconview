/* Overview: the whole proxy at a glance for a time range (and optionally one instance). Every row links into Explore
   with that value as a filter and the same range, so the overview is where a question starts, Explore where it is
   answered.

   A preset range is always live, pushed: the page holds a /api/stream?kind=changes connection and re-fetches when
   the server says the data moved, at the pace of the chart's own bars (BV.cadence: every second on "Last 5
   minutes", once a minute on "Last hour"). What scans raw rows (tiles with unique IPs, top paths, IPs, user agents)
   refreshes no more often than every 5 s. Clicking a bar opens Explore on that bar's time span; dragging across the
   chart zooms the Overview. Pause freezes the window on screen as a fixed range (so does zooming);
   Live returns to the preset. If streaming is not possible it polls at the same pace. Updates land in place, and
   nothing runs while the tab is hidden. */
"use strict";
(function () {
  var el = BV.el, $ = BV.$;
  var meta = null;
  var state = { range: "1h", from: null, to: null, instance: null, by: "router", sort: "requests", resume: "1h" };
  var chart = null, chartOpts = null, timers = {}, changes = null, lastSlow = 0;
  var seenErrors = null;            // seq numbers in the recent-5xx table, to mark the rows that arrive live
  var inflight = {}, errors = {};

  /* ---------- URL ---------- */
  function readUrl() {
    var p = new URLSearchParams(location.search);
    var r = BV.rangeFromUrl(p, "1h");
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
    if (!(extra && extra.from !== undefined)) BV.rangeToUrl(state, p);   // an explicit window replaces the page's
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
    // Compare only when the stored data reaches back over the whole previous period: a fresh install (or one younger
    // than twice the range) would otherwise compare a full period against a sliver and show "up 68,000%".
    var oldest = meta && meta.data ? meta.data.oldest_ms : null;
    var comparable = oldest !== null && oldest !== undefined && oldest <= prevW.from + 60e3;
    if (!quiet) $("tiles").classList.add("loading");
    Promise.all([BV.api("/api/summary", params(null, w)),
      comparable ? BV.api("/api/summary", params(null, prevW)) : Promise.resolve(null)]).then(function (r) {
      $("tiles").classList.remove("loading");
      var s = r[0], p = r[1] || {};
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
      tiles.forEach(function (t, i) {
        // without enough history, say so once rather than on every tile
        var d = comparable ? t[2] : i === 0 ? { text: "nothing earlier to compare yet", cls: "" } : null;
        box.appendChild(el("div", { class: "tile" }, el("div", { class: "label", text: t[0] }),
          el("div", { class: "value", text: t[1] }),
          d ? el("div", { class: "delta " + d.cls, title: comparable ? vs : "BeaconView has not been collecting long enough to compare",
            text: comparable && d.text !== "no change" && d.text !== "new" ? d.text + " " + vs : d.text }) : null));
      });
      if (errors.tiles) { delete errors.tiles; banner(); }
    }).catch(function (e) { $("tiles").classList.remove("loading"); errors.tiles = e.message; banner(); });
  }

  /* ---------- chart ---------- */
  function isLive() { return !!state.range; }
  function buckets() {
    var width = chart ? chart.width() : $("seriesChart").clientWidth - 60;
    return Math.max(20, Math.min(240, Math.floor(width / 7)));
  }
  function loadSeries(quiet) {
    load("series", $("seriesCard"), "/api/series", params({ buckets: buckets(), split: "status_class" }), function (d) {
      var series = d.series.map(function (s) {
        return { key: s.key, label: s.key, color: BV.statusClassColor(s.key), values: s.values };
      });
      chartOpts = { kind: "columns", start: d.start, step: d.step, n: d.buckets, series: series, height: 220,
        partial: isLive() && d.start + d.buckets * d.step > d.to ? d.buckets - 1 : -1,
        label: "Requests over time by status class", onZoom: function (f, t) {
          if (state.range) state.resume = state.range;
          state.range = null; state.from = f; state.to = t; writeUrl(true); reloadAll();
        }, onClick: function (i) {         // drill down: Explore on exactly this bar's requests
          var from = d.start + i * d.step;
          location.href = exploreUrl([], { from: from, to: from + d.step });
        } };
      $("seriesSub").textContent = "per " + BV.span(d.step).replace(/^1 /, "") + " · " + BV.rangeLabel(state);
      BV.legend($("seriesLegend"), series, "columns");
      if (chart) chart.update(chartOpts); else { chart = BV.chart($("seriesChart"), chartOpts); $("seriesChart").classList.add("clickable"); }
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
      renderTop(field, table, d, 8);
    }, quiet);
  }
  function renderTop(field, table, d, n) {
    var t = BV.clear(table), body = el("tbody"), shown = d.values.slice(0, n);
    if (!shown.length) body.appendChild(el("tr", null, el("td", { class: "hint", text: "No requests in this range." })));
    shown.forEach(function (v) {
      var name = BV.display(field, v.value);
      var cell = el("td", { class: "name" + (field === "user_agent" || field === "country" ? "" : " mono") });
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
    if (d.distinct > shown.length) {
      t.appendChild(el("tfoot", null, el("tr", null, el("td", { colspan: 3, class: "hint",
        text: "≈ " + BV.int(d.distinct) + " distinct" }))));
    }
  }

  /* ---------- countries ---------- */
  // one query for the map (map.js) and the table beside it; country is not in the rollups, so this reads raw rows
  function loadCountries(quiet) {
    load("countries", $("countriesCard"), "/api/top", params({ field: "country", limit: 300 }), function (d) {
      renderTop("country", $("countriesTable"), d, 8);
      BV.loadWorld().then(function (w) {
        BV.countryMap($("countriesMap"), w, d, {
          label: "Map of requests by country; the table beside it lists the busiest",
          onPick: function (code) { location.href = exploreUrl([["country", code]]); },
        });
      }, function (e) { errors.world = e.message; banner(); });
    }, quiet);
  }
  // what stands out against the same time on earlier days (the server judges the last 24 h of a longer range)
  var SEVERITY = { critical: ["▲", "Critical"], warning: ["!", "Warning"], info: ["i", "Note"] };
  function loadHighlights(quiet) {
    load("highlights", $("highlightsCard"), "/api/highlights", params(), function (d) {
      var list = BV.clear($("highlightsList")), sub = $("highlightsSub");
      var w = win(), clipped = d.from > w.from + 60e3;
      if (d.state === "learning") {
        sub.textContent = "";
        list.appendChild(el("li", { class: "hint", text: "Learning what normal looks like: highlights compare with the " +
          "same time on earlier days" + (d.ready_ms ? ", so they start around " + BV.day(d.ready_ms) + " " +
          BV.clock(d.ready_ms) : "") + "." }));
        return;
      }
      sub.textContent = (clipped ? "Last 24 hours" : "This range") + " vs the same time on the past " + d.days +
        " day" + (d.days === 1 ? "" : "s");
      if (!d.items.length) {
        list.appendChild(el("li", { class: "hint", text: "Nothing unusual." }));
        return;
      }
      d.items.forEach(function (h) {
        var sev = SEVERITY[h.severity] || SEVERITY.info;
        var href = exploreUrl(h.filters, !clipped ? null : state.range ? { range: "24h" } : { from: Math.floor(d.from), to: Math.ceil(d.to) });
        list.appendChild(el("li", { class: "sev-" + h.severity },
          el("span", { class: "sev", title: sev[1] }, el("i", { "aria-hidden": "true", text: sev[0] }), sev[1]),
          el("span", { class: "what" }, el("a", { href: href, text: h.title }), el("span", { class: "hint", text: h.detail }))));
      });
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
      if (r.range) state.resume = r.range;
      writeUrl(true); reloadAll();
    });
    $("byRouter").value = state.by;
    $("bySort").value = state.sort;
    $("exploreNav").href = exploreUrl([]);
    document.querySelectorAll("a.go[data-top]").forEach(function (a) {
      a.href = exploreUrl([], { top: a.getAttribute("data-top") });
    });
    BV.renderLiveButton($("liveBtn"), $("liveDot"), isLive());
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
    loadHighlights(quiet);
    loadTop("path", $("pathsTable"), $("pathsCard"), quiet);
    loadTop("ip", $("ipsTable"), $("ipsCard"), quiet);
    loadTop("user_agent", $("uasTable"), $("uasCard"), quiet);
    if (meta && meta.geoip && meta.geoip.active) loadCountries(quiet);
  }
  function intervals() {        // fastest refresh per tier, in ms: one bar's worth; raw scans no faster than 5 s
    var w = win(), fast = BV.cadence(w.to - w.from, buckets());
    return { fast: fast, slow: Math.max(5e3, fast) };
  }
  function onChange() {
    refreshFast(true);
    if (Date.now() - lastSlow >= intervals().slow) { lastSlow = Date.now(); refreshSlow(true); }
  }
  function schedule() {
    clearInterval(timers.fast); clearInterval(timers.slow);
    if (changes) { changes.close(); changes = null; }
    if (!isLive() || document.hidden) return;
    var iv = intervals();
    lastSlow = Date.now();
    var p = new URLSearchParams();
    p.set("kind", "changes");
    p.set("min_interval", iv.fast / 1000);
    changes = BV.stream(p, {
      change: onChange,
      fallback: function () {        // no streaming: poll at the same rates (fast tier no quicker than 10 s)
        changes = null;
        timers.fast = setInterval(function () { if (!document.hidden) refreshFast(true); }, Math.max(10e3, iv.fast));
        timers.slow = setInterval(function () { if (!document.hidden) refreshSlow(true); }, iv.slow);
      },
    });
  }

  function init() {
    BV.initTheme($("themeBtn"), function () { if (chartOpts) loadSeries(); });
    BV.initIngestStatus($("ingestDot"), $("ingestText"), $("statusBanner"));
    BV.menu($("rangeBtn"), $("rangeMenu"));
    document.addEventListener("keydown", function (ev) { if (ev.key === "Escape") BV.closeMenus(); });
    readUrl();
    BV.api("/api/fields").then(function (m) {
      meta = m;
      $("countriesCard").hidden = !(m.geoip && m.geoip.active);
      BV.credit($("credit"), m.geoip && m.geoip.attribution);
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
    $("liveBtn").addEventListener("click", function () {
      if (isLive()) {
        // Pause: keep exactly this window as a fixed range; the next load draws it without the "so far" bar
        var w = win();
        state.resume = state.range;
        state.range = null; state.from = w.from; state.to = w.to;
      } else {
        state.range = state.resume || "1h"; state.from = state.to = null;
      }
      writeUrl(true);
      reloadAll();
    });
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) { if (changes) { changes.close(); changes = null; } return; }
      if (isLive()) { refreshFast(true); refreshSlow(true); schedule(); }
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
