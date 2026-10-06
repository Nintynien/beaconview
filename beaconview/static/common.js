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
  // Server clock minus this browser's, from the last /api/status; "ago" is measured on the server's clock so a
  // visitor whose clock is off still sees the right age.
  BV.skew = 0;
  BV.ago = function (ms) {
    var s = Math.max(0, (Date.now() + BV.skew - ms) / 1000);
    if (s < 60) return Math.floor(s) + " s ago";       // floor: the text counts up, never jumps ahead
    if (s < 3600) return Math.floor(s / 60) + " min ago";
    if (s < 86400) return Math.floor(s / 3600) + " h ago";
    return Math.floor(s / 86400) + " d ago";
  };
  // A text node that keeps its age current: "Newest request 14 s ago" counts up between refreshes instead of
  // showing the age it had when the data arrived.
  BV.agoText = function (node, ms, prefix, suffix) {
    node.setAttribute("data-ago", ms);
    node.setAttribute("data-ago-prefix", prefix || "");
    node.setAttribute("data-ago-suffix", suffix || "");
    node.textContent = (prefix || "") + BV.ago(ms) + (suffix || "");
    return node;
  };
  setInterval(function () {
    document.querySelectorAll("[data-ago]").forEach(function (n) {
      n.textContent = n.getAttribute("data-ago-prefix") + BV.ago(Number(n.getAttribute("data-ago"))) +
        n.getAttribute("data-ago-suffix");
    });
  }, 1000);
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

  /* ---------- time range ---------- */
  // A range is {range: "1h"} (relative, slides with now) or {from, to} (absolute ms).
  BV.PRESETS = [["15m", "Last 15 minutes", 15 * 60e3], ["1h", "Last hour", 3600e3], ["6h", "Last 6 hours", 6 * 3600e3],
    ["24h", "Last 24 hours", 86400e3], ["7d", "Last 7 days", 7 * 86400e3], ["30d", "Last 30 days", 30 * 86400e3]];
  function preset(key) { return BV.PRESETS.find(function (x) { return x[0] === key; }); }
  BV.rangeFromUrl = function (p, fallback) {
    var from = Number(p.get("from")), to = Number(p.get("to"));
    if (p.get("range") && preset(p.get("range"))) return { range: p.get("range"), from: null, to: null };
    if (from < to) return { range: null, from: from, to: to };
    return { range: fallback || "1h", from: null, to: null };
  };
  BV.rangeToUrl = function (r, p) {
    if (r.range) p.set("range", r.range); else { p.set("from", r.from); p.set("to", r.to); }
  };
  // A relative range ends on the last whole 5 seconds, so every open tab asks the server the identical question
  // and they share one cached answer (AnswerCache); the cost is data up to 5 s behind.
  BV.rangeWindow = function (r) {
    if (r.range) {
      var now = Math.floor(Date.now() / 5000) * 5000;
      return { from: now - (preset(r.range) || preset("1h"))[2], to: now };
    }
    return { from: r.from, to: r.to };
  };
  BV.rangeLabel = function (r) {
    if (r.range) return (preset(r.range) || preset("1h"))[1];
    var span = r.to - r.from;
    return BV.when(r.from, span) + " – " + BV.when(r.to, span);
  };
  // Presets (longer than the retention window are left out) and a custom from/to; onPick gets the new range.
  BV.buildRangeMenu = function (menu, r, retentionDays, onPick) {
    BV.clear(menu);
    var maxMs = retentionDays ? retentionDays * 86400e3 : Infinity;
    BV.PRESETS.forEach(function (p) {
      if (p[2] > maxMs * 1.01 && p[0] !== "1h") return;
      menu.appendChild(BV.el("button", { class: "item", type: "button", onclick: function () {
        BV.closeMenus(); onPick({ range: p[0], from: null, to: null });
      } }, BV.el("span", { class: "check", text: r.range === p[0] ? "✓" : "" }), p[1]));
    });
    menu.appendChild(BV.el("hr"));
    var w = BV.rangeWindow(r);
    var toLocal = function (ms) { return new Date(ms - new Date(ms).getTimezoneOffset() * 60e3).toISOString().slice(0, 16); };
    var fromIn = BV.el("input", { type: "datetime-local", value: toLocal(w.from) });
    var toIn = BV.el("input", { type: "datetime-local", value: toLocal(w.to) });
    menu.appendChild(BV.el("div", { class: "custom" },
      BV.el("label", null, "From", fromIn), BV.el("label", null, "To", toIn),
      BV.el("button", { class: "btn", type: "button", onclick: function () {
        var f = new Date(fromIn.value).getTime(), t = new Date(toIn.value).getTime();
        if (!(f < t)) return;
        BV.closeMenus(); onPick({ range: null, from: f, to: t });
      } }, "Apply custom range")));
  };

  /* ---------- popover menus ---------- */
  var menus = [];
  BV.menu = function (btn, menu, onOpen) {
    menus.push([btn, menu]);
    btn.addEventListener("click", function (ev) {
      ev.stopPropagation();
      var open = menu.hidden;
      BV.closeMenus();
      menu.hidden = !open;
      btn.setAttribute("aria-expanded", String(open));
      if (open && onOpen) onOpen();
    });
    menu.addEventListener("click", function (ev) { ev.stopPropagation(); });
  };
  BV.closeMenus = function () {
    menus.forEach(function (m) { m[1].hidden = true; m[0].setAttribute("aria-expanded", "false"); });
  };
  document.addEventListener("click", function () { BV.closeMenus(); });

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

  /* ---------- ingest status: the header dot, and a banner only when something is wrong ---------- */
  var QUIET_MS = 5 * 60e3;          // no request for this long turns the dot yellow (quiet sites are allowed to be quiet)
  var BEHIND_BYTES = 50e6;          // a backlog this big gets a banner: recent numbers are incomplete until it clears
  BV.initIngestStatus = function (dot, text, banner) {
    function setBanner(kind, message) {
      if (!banner) return;
      banner.hidden = !message;
      banner.className = "banner " + (kind || "");
      banner.textContent = message || "";
    }
    function tick() {
      BV.api("/api/status").then(function (s) {
        BV.skew = s.now_ms - Date.now();
        var ing = s.ingest, st = s.storage;
        var unread = ing.sources.reduce(function (a, x) { return a + (x.unread_bytes || 0); }, 0);
        var broken = ing.sources.filter(function (x) { return x.error; });
        var quiet = ing.newest_ms && s.now_ms - ing.newest_ms > QUIET_MS;
        text.removeAttribute("data-ago");
        if (!ing.alive) {
          dot.className = "dot bad"; text.textContent = "Ingest stopped";
          setBanner("error", "BeaconView has stopped recording requests" +
            (ing.last_error ? " (" + ing.last_error + ")" : "") + ". What you see ends there; restarting the container resumes from where it stopped.");
        } else if (broken.length) {
          dot.className = "dot bad"; text.textContent = "Can't read a log";
          setBanner("error", broken.map(function (x) { return "Can't read " + x.path + ": " + x.error; }).join(" · ") +
            ". Requests in that log are not being recorded.");
        } else if (unread > BEHIND_BYTES) {
          dot.className = "dot warn"; text.textContent = "Catching up";
          setBanner("info", "Catching up on " + BV.bytes(unread) + " of log. The newest numbers are incomplete until it finishes.");
        } else {
          dot.className = "dot " + (quiet ? "warn" : "good");
          if (ing.newest_ms) BV.agoText(text, ing.newest_ms, "Newest request ");
          else text.textContent = "Waiting for the first request";
          setBanner(null, null);
        }
        dot.parentNode.title = BV.int(st.rows) + " requests stored · " + BV.bytes(st.db_bytes) +
          (st.oldest_ms ? " · since " + BV.day(st.oldest_ms) : "") + " · keeping " + s.retention.raw_days + " days" +
          (quiet ? " · nothing new for over 5 minutes: a quiet site, or Traefik is not writing its log" : "");
      }).catch(function () {
        dot.className = "dot bad";
        text.textContent = "BeaconView unreachable";
        setBanner("error", "Can't reach BeaconView. What's on this page may be out of date.");
      });
    }
    tick();
    setInterval(function () { if (!document.hidden) tick(); }, 15000);
    document.addEventListener("visibilitychange", function () { if (!document.hidden) tick(); });
  };
})();
