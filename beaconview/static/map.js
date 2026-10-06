/* Requests by country on a world map, shared by the Overview's Countries card and Explore's breakdown by country.

   Outlines are Natural Earth 1:110m (public domain), projected ahead of time by tools/build_world.py into
   static/world.json; fetched once per page, only when a map shows. Shading is one blue ramp in five steps on a log
   scale (a few countries usually dwarf the rest); no requests is the neutral surface; a legend gives the ranges.
   Hover (or a first tap) shows the count; a click on a country with requests calls opts.onPick(code). Places too
   small for this scale and unknown addresses are listed under the map. The page keeps a table of the same data. */
"use strict";
(function () {
  var el = BV.el, world = null;

  BV.loadWorld = function () {
    if (!world) {
      world = fetch("/static/world.json").then(function (r) {
        if (!r.ok) throw new Error("world map: HTTP " + r.status);
        return r.json();
      });
      world.catch(function () { world = null; });
    }
    return world;
  };

  function bounds(max) {                 // lower bound of each step: 1, then max^(k/5), whole and increasing
    var out = [1];
    for (var k = 1; k < 5; k++) {
      var b = Math.ceil(Math.pow(max, k / 5));
      if (b > out[out.length - 1]) out.push(b);
    }
    return out;
  }

  // box: an element to fill; w: BV.loadWorld()'s answer; d: {values: [{value, count}], total} as /api/top gives it.
  // Returns {mark(code)}: outline one country from outside (a hovered log row), null to clear.
  BV.countryMap = function (box, w, d, opts) {
    opts = opts || {};
    var counts = {}, max = 0, tipCode = null, lastPointer = "mouse";
    d.values.forEach(function (v) { counts[v.value] = v.count; });
    Object.keys(w.countries).forEach(function (c) { max = Math.max(max, counts[c] || 0); });
    var b = bounds(Math.max(1, max)), offset = 5 - b.length;
    var step = function (n) {
      if (!n) return 0;
      var i = 0;
      while (i + 1 < b.length && n >= b[i + 1]) i++;
      return offset + i + 1;
    };
    var svg = el("svg:svg", { class: "worldmap", viewBox: "0 0 " + w.width + " " + w.height, role: "img",
      "aria-label": opts.label || "Map of requests by country; the table lists the same" });
    Object.keys(w.countries).forEach(function (c) {
      svg.appendChild(el("svg:path", { d: w.countries[c], class: "q" + step(counts[c]), "data-c": c }));
    });
    var tip = el("div", { class: "tip", hidden: true, role: "status" });

    function hideTip() {
      tipCode = null;
      tip.hidden = true;
      svg.querySelectorAll("path.on").forEach(function (p) { p.classList.remove("on"); });
    }
    function showTip(e) {
      var code = e.target.getAttribute && e.target.getAttribute("data-c");
      svg.querySelectorAll("path.on").forEach(function (p) { if (p !== e.target) p.classList.remove("on"); });
      if (!code) { hideTip(); return; }
      if (tipCode !== code) {
        e.target.classList.add("on");
        svg.appendChild(e.target);                     // on top, so the outline is not hidden by neighbours
        tipCode = code;
        var n = counts[code] || 0;
        BV.clear(tip);
        tip.appendChild(el("div", { class: "when", text: BV.countryName(code) }));
        tip.appendChild(el("div", { class: "r" }, el("b", { text: BV.int(n) }),
          el("span", { text: n ? "requests · " + BV.pct(d.total ? n / d.total : 0) : "requests" })));
        tip.hidden = false;
      }
      var r = box.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top, tw = tip.offsetWidth;
      tip.style.left = Math.max(0, Math.min(r.width - tw, x + 14 + tw > r.width ? x - tw - 14 : x + 14)) + "px";
      tip.style.top = Math.max(0, y - 10) + "px";
    }
    svg.addEventListener("pointerdown", function (e) { lastPointer = e.pointerType; });
    svg.addEventListener("mousemove", function (e) { if (lastPointer === "mouse") showTip(e); });
    svg.addEventListener("mouseleave", hideTip);
    svg.addEventListener("click", function (e) {
      var code = e.target.getAttribute && e.target.getAttribute("data-c");
      if (!code) { hideTip(); return; }
      if (lastPointer !== "mouse" && tipCode !== code) { showTip(e); return; }    // touch: first tap explains
      if (counts[code] && opts.onPick) { hideTip(); opts.onPick(code); }
    });

    var legend = el("div", { class: "legend maplegend" },
      el("span", null, el("i", { style: "background:var(--q0)" }), "none"),
      b.map(function (lo, i) {
        var hi = i + 1 < b.length ? b[i + 1] - 1 : max;
        return el("span", null, el("i", { style: "background:var(--q" + (offset + i + 1) + ")" }),
          lo >= hi ? BV.int(lo) : BV.int(lo) + "–" + BV.int(hi));
      }));
    var off = d.values.filter(function (v) { return !w.countries[v.value] && v.value !== "ZZ" && v.value !== "(none)"; });
    var unknown = d.values.filter(function (v) { return v.value === "ZZ"; })[0];
    var notes = [];
    if (off.length) notes.push("Too small for the map: " + off.slice(0, 4).map(function (v) {
      return BV.countryName(v.value) + " " + BV.int(v.count);
    }).join(" · ") + (off.length > 4 ? " · " + (off.length - 4) + " more" : ""));
    if (unknown) notes.push("Unknown location: " + BV.int(unknown.count));
    BV.clear(box);
    BV.append(box, [svg, tip, legend, notes.length ? el("p", { class: "hint", text: notes.join(". ") + "." }) : null]);

    return {
      mark: function (code) {
        svg.querySelectorAll("path.marked").forEach(function (p) { p.classList.remove("marked"); });
        var p = code && svg.querySelector('path[data-c="' + code.replace(/[^A-Z]/g, "") + '"]');
        if (p) { p.classList.add("marked"); svg.appendChild(p); }
      },
    };
  };
})();
