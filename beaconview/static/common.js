/* BeaconView shared helpers: DOM, fetch, formatting, theme, series colors, the header's ingest status. */
"use strict";
var BV = window.BV || {};
window.BV = BV;

(function () {
  /* ---------- DOM ---------- */
  // el("div", {class: "x", onclick: fn}, child, "text", ...): text is always text, never parsed as HTML
  BV.el = function (tag, attrs) {
    var node = tag.indexOf("svg:") === 0
      ? document.createElementNS("http://www.w3.org/2000/svg", tag.slice(4))
      : document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v === null || v === undefined || v === false) return;
        if (k.indexOf("on") === 0 && typeof v === "function") node.addEventListener(k.slice(2), v);
        else if (k === "text") node.textContent = v;
        else if (k === "style") node.style.cssText = v;   // CSSOM, which the page's CSP allows (style attributes it blocks)
        else node.setAttribute(k, v === true ? "" : String(v));
      });
    }
    for (var i = 2; i < arguments.length; i++) BV.append(node, arguments[i]);
    return node;
  };
  BV.append = function (node, child) {
    if (child === null || child === undefined || child === false) return;
    if (Array.isArray(child)) { child.forEach(function (c) { BV.append(node, c); }); return; }
    node.appendChild(typeof child === "object" ? child : document.createTextNode(String(child)));
  };
  BV.clear = function (node) { while (node.firstChild) node.removeChild(node.firstChild); return node; };
  BV.$ = function (id) { return document.getElementById(id); };

  /* ---------- fetch ---------- */
  BV.api = function (path, params, signal) {
    var qs = params ? params.toString() : "";
    return fetch(path + (qs ? "?" + qs : ""), { cache: "no-store", signal: signal }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (body) {
        if (!r.ok) throw new Error(body.error || ("HTTP " + r.status));
        return body;
      });
    });
  };

  /* ---------- formatting ---------- */
  var nf = new Intl.NumberFormat();
  BV.int = function (n) { return n === null || n === undefined ? "–" : nf.format(Math.round(n)); };
  BV.compact = function (n) {
    if (n === null || n === undefined) return "–";
    var a = Math.abs(n);
    if (a < 1000) return String(Math.round(n * 10) / 10);
    var units = [["B", 1e9], ["M", 1e6], ["K", 1e3]];
    for (var i = 0; i < units.length; i++) {
      if (a >= units[i][1]) {
        var v = n / units[i][1];
        return (v >= 100 ? Math.round(v) : Math.round(v * 10) / 10) + units[i][0];
      }
    }
    return String(n);
  };
  BV.dur = function (us) {
    if (us === null || us === undefined) return "–";
    if (us < 1000) return Math.round(us) + " µs";
    if (us < 1e6) { var ms = us / 1000; return (ms < 10 ? ms.toFixed(1) : Math.round(ms)) + " ms"; }
    var s = us / 1e6;
    return (s < 10 ? s.toFixed(2) : s < 100 ? s.toFixed(1) : Math.round(s)) + " s";
  };
  BV.bytes = function (b) {
    if (b === null || b === undefined) return "–";
    var u = ["B", "KB", "MB", "GB", "TB"], i = 0;
    while (b >= 1000 && i < u.length - 1) { b /= 1000; i++; }
    return (i === 0 ? Math.round(b) : b < 10 ? b.toFixed(1) : Math.round(b)) + " " + u[i];
  };
  BV.pct = function (x) { return x === null || x === undefined ? "–" : (x * 100 < 1 && x > 0 ? (x * 100).toFixed(2) : (x * 100).toFixed(1)) + "%"; };
  var two = function (n) { return (n < 10 ? "0" : "") + n; };
  BV.clock = function (ms, withSeconds) {
    var d = new Date(ms);
    return two(d.getHours()) + ":" + two(d.getMinutes()) + (withSeconds ? ":" + two(d.getSeconds()) : "");
  };
  var months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  BV.day = function (ms) { var d = new Date(ms); return months[d.getMonth()] + " " + d.getDate(); };
  BV.stamp = function (ms, withDate) {   // "14:03:22.120", or "Oct 5 14:03:22.120"
    var d = new Date(ms);
    return (withDate ? BV.day(ms) + " " : "") + BV.clock(ms, true) + "." + String(d.getMilliseconds()).padStart(3, "0");
  };
  BV.when = function (ms, spanMs) {   // a time, with the date when the span crosses days
    return spanMs > 20 * 3600e3 ? BV.day(ms) + " " + BV.clock(ms) : BV.clock(ms, spanMs < 30 * 60e3);
  };
  BV.ago = function (ms) {
    var s = Math.max(0, (Date.now() - ms) / 1000);
    if (s < 60) return Math.round(s) + " s ago";
    if (s < 3600) return Math.round(s / 60) + " min ago";
    if (s < 86400) return Math.round(s / 3600) + " h ago";
    return Math.round(s / 86400) + " d ago";
  };
  BV.span = function (ms) {
    var m = ms / 60e3;
    if (m < 60) return Math.round(m) + " min";
    var h = m / 60;
    if (h < 48) return (Math.round(h * 10) / 10) + " h";
    return (Math.round(h / 24 * 10) / 10) + " days";
  };

  /* ---------- colors ---------- */
  var css = function (name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); };
  BV.statusClassColor = function (key) {
    return { "2xx": css("--c2xx"), "3xx": css("--c3xx"), "4xx": css("--c4xx"), "5xx": css("--c5xx") }[key] || css("--cnone");
  };
  BV.statusColorVar = function (code) {
    var c = Math.floor((code || 0) / 100);
    return c >= 2 && c <= 5 ? "var(--c" + c + "xx)" : "var(--cnone)";
  };
  // Categorical slots follow the entity, not its rank: a key keeps its slot while it stays on screen, so a filter
  // that changes the top five never repaints the survivors.
  BV.Slots = function () { this.byKey = {}; };
  BV.Slots.prototype.assign = function (keys) {
    var self = this, keep = {}, used = {};
    keys.forEach(function (k) { if (self.byKey[k] !== undefined) { keep[k] = self.byKey[k]; used[keep[k]] = true; } });
    keys.forEach(function (k) {
      if (keep[k] !== undefined) return;
      for (var s = 1; s <= 5; s++) if (!used[s]) { keep[k] = s; used[s] = true; return; }
    });
    this.byKey = keep;
  };
  BV.Slots.prototype.color = function (key) {
    if (key === "Other") return css("--s-other");
    return css("--s" + (this.byKey[key] || 1));
  };

  /* ---------- theme ---------- */
  var THEMES = ["system", "light", "dark"];
  function storedTheme() { try { return localStorage.getItem("bv-theme") || "system"; } catch (e) { return "system"; } }
  function applyTheme(t) {
    if (t === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", t);
  }
  applyTheme(storedTheme());
  BV.initTheme = function (button, onChange) {
    function label() {
      var t = storedTheme();
      button.textContent = t === "dark" ? "☾" : t === "light" ? "☀" : "◐";
      button.title = "Theme: " + t + " (click to change)";
      button.setAttribute("aria-label", button.title);
    }
    label();
    button.addEventListener("click", function () {
      var next = THEMES[(THEMES.indexOf(storedTheme()) + 1) % THEMES.length];
      try { localStorage.setItem("bv-theme", next); } catch (e) { /* private mode: just this page */ }
      applyTheme(next);
      label();
      if (onChange) onChange();
    });
    if (window.matchMedia) {
      window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () { if (onChange) onChange(); });
    }
  };

  /* ---------- header ingest status ---------- */
  BV.initIngestStatus = function (dot, text) {
    function tick() {
      BV.api("/api/status").then(function (s) {
        var ing = s.ingest, behind = ing.newest_ms ? (s.now_ms - ing.newest_ms) / 1000 : null;
        var unread = ing.sources.reduce(function (a, x) { return a + (x.unread_bytes || 0); }, 0);
        var errors = ing.sources.filter(function (x) { return x.error; });
        dot.className = "dot " + (!ing.alive || errors.length ? "bad" : "good");
        var msg = !ing.alive ? "Ingest stopped" : errors.length ? "Can't read " + errors[0].path
          : unread > 1e6 ? "Catching up (" + BV.bytes(unread) + " behind)"
          : behind === null ? "Waiting for the first request" : "Newest request " + BV.ago(ing.newest_ms);
        text.textContent = msg;
        dot.parentNode.title = msg + " · " + BV.int(s.storage.rows) + " requests stored · " + BV.bytes(s.storage.db_bytes);
      }).catch(function () {
        dot.className = "dot bad";
        text.textContent = "BeaconView unreachable";
      });
    }
    tick();
    setInterval(tick, 30000);
  };
})();
