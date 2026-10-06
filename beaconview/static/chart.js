/* BeaconView charts: time-bucketed stacked columns and lines, drawn as SVG.

   Marks follow the data-viz spec: columns <= 24px with a 2px surface gap between stacked segments and neighbours,
   4px rounded data-end, 2px lines with a ringed end dot, hairline solid grid. Hover is a per-bucket band (columns) or
   crosshair (lines) with one tooltip listing every series; arrow keys step through buckets when the chart has focus;
   dragging across the plot zooms to that range, a click (or Enter on a focused bucket) calls onClick(bucket).
   mark(i) highlights a bucket from outside, without a tooltip (a hovered log row showing where it sits). opts.partial
   is the index of a bucket still filling (the newest one on a live range): drawn lighter and labelled "so far", so
   its short bar does not read as a drop in traffic. Every value is also in the table view. */
"use strict";
(function () {
  var el = BV.el;
  var TIME_STEPS = [1e3, 5e3, 10e3, 15e3, 30e3, 60e3, 120e3, 300e3, 600e3, 900e3, 1800e3, 3600e3, 7200e3,
    10800e3, 21600e3, 43200e3, 86400e3, 172800e3, 604800e3];

  function niceStep(raw) {
    var p = Math.pow(10, Math.floor(Math.log10(raw)));
    var f = raw / p;
    return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
  }
  function yTicks(max) {
    if (!(max > 0)) return { max: 1, ticks: [0, 1] };
    var step = niceStep(max / 4);
    var top = Math.ceil(max / step) * step;
    var ticks = [];
    for (var v = 0; v <= top + step / 2; v += step) ticks.push(v);
    return { max: top, ticks: ticks };
  }
  function roundTop(x, y, w, h, r) {   // a rect whose top corners are rounded (the data end)
    r = Math.max(0, Math.min(r, w / 2, h));
    return "M" + x + "," + (y + h) + "V" + (y + r) + "Q" + x + "," + y + " " + (x + r) + "," + y +
      "H" + (x + w - r) + "Q" + (x + w) + "," + y + " " + (x + w) + "," + (y + r) + "V" + (y + h) + "Z";
  }

  BV.chart = function (container, opts) {
    var state = { opts: opts, hover: -1, marked: -1, drag: null };
    var tip = el("div", { class: "tip", hidden: true, role: "status" });
    container.classList.add("chart");
    container.setAttribute("tabindex", "0");
    var svgHolder = el("div");
    BV.append(container, [svgHolder, tip]);
    var geo = null;

    function render() {
      // hidden (a display:none panel reports width 0): keep the last drawing; the resize observer redraws on show
      if (!container.clientWidth) return;
      var o = state.opts;
      var W = Math.max(200, container.clientWidth);
      var H = o.height || 200;
      var n = o.n, series = o.series;
      var stacked = o.kind === "columns";
      var max = 0;
      for (var i = 0; i < n; i++) {
        var tot = 0;
        series.forEach(function (s) {
          var v = s.values[i];
          if (v === null || v === undefined) return;
          if (stacked) tot += v; else tot = Math.max(tot, v);
        });
        max = Math.max(max, tot);
      }
      var yt = yTicks(max);
      var fmt = o.yFormat || BV.compact;
      var labelW = Math.max.apply(null, yt.ticks.map(function (t) { return fmt(t).length; })) * 6.6 + 10;
      var L = Math.ceil(labelW), R = 8, T = 8, B = 22;
      var pw = W - L - R, ph = H - T - B;
      var slot = pw / n;
      var y = function (v) { return T + ph - (v / yt.max) * ph; };
      var xAt = function (t) { return L + (t - o.start) / o.step * slot; };
      geo = { L: L, T: T, pw: pw, ph: ph, slot: slot, W: W, H: H, y: y };

      var svg = el("svg:svg", { viewBox: "0 0 " + W + " " + H, height: H, role: "img",
        "aria-label": o.label || "chart" });
      yt.ticks.forEach(function (t) {
        var yy = Math.round(y(t)) + 0.5;
        svg.appendChild(el("svg:line", { x1: L, x2: W - R, y1: yy, y2: yy, class: t === 0 ? "baseline" : "gridline" }));
        svg.appendChild(el("svg:text", { x: L - 6, y: yy + 3.5, "text-anchor": "end", text: fmt(t) }));
      });

      // x ticks: a round interval, at most one label per ~90px, aligned to local time
      var span = n * o.step;
      var interval = TIME_STEPS.find(function (s) { return span / s <= pw / 90; }) || TIME_STEPS[TIME_STEPS.length - 1];
      var tz = new Date(o.start).getTimezoneOffset() * 60e3;
      var first = Math.ceil((o.start - tz) / interval) * interval + tz;
      for (var t = first; t < o.start + span; t += interval) {
        var xx = xAt(t);
        if (xx < L + 12 || xx > W - R - 12) continue;
        var midnight = new Date(t).getHours() === 0 && new Date(t).getMinutes() === 0;
        var lab = interval >= 86400e3 || (midnight && span > 6 * 3600e3) ? BV.day(t) : BV.clock(t, interval < 60e3);
        svg.appendChild(el("svg:text", { x: xx, y: H - 6, "text-anchor": "middle", text: lab }));
      }

      var band = el("svg:rect", { class: "hover-band", y: T, height: ph, width: 0, visibility: "hidden" });
      if (stacked) svg.appendChild(band);

      if (stacked) {
        var bw = Math.max(1, Math.min(24, slot - 2));
        for (var j = 0; j < n; j++) {
          var x0 = L + j * slot + (slot - bw) / 2, base = T + ph, topIndex = -1;
          series.forEach(function (s, k) { if (s.values[j] > 0) topIndex = k; });
          series.forEach(function (s, k) {
            var v = s.values[j];
            if (!(v > 0)) return;
            var h = v / yt.max * ph;
            var gap = base < T + ph ? Math.min(2, h / 2) : 0;   // surface gap above the segment below
            var top = base - h;
            var d = k === topIndex
              ? roundTop(x0, top, bw, Math.max(0.5, h - gap), 4)
              : "M" + x0 + "," + top + "H" + (x0 + bw) + "V" + (base - gap) + "H" + x0 + "Z";
            svg.appendChild(el("svg:path", { d: d, fill: s.color, "fill-opacity": j === o.partial ? 0.45 : null }));
            base = top;
          });
        }
      } else {
        series.forEach(function (s) {
          var d = "", last = -1;
          s.values.forEach(function (v, k) {
            if (v === null || v === undefined) { last = -1; return; }
            var px = L + (k + 0.5) * slot, py = y(v);
            d += (last >= 0 && last === k - 1 ? "L" : "M") + px.toFixed(1) + "," + py.toFixed(1);
            last = k;
          });
          if (d) svg.appendChild(el("svg:path", { d: d, fill: "none", stroke: s.color, "stroke-width": 2,
            "stroke-linejoin": "round", "stroke-linecap": "round" }));
          for (var e = s.values.length - 1; e >= 0; e--) {
            if (s.values[e] !== null && s.values[e] !== undefined) {
              svg.appendChild(el("svg:circle", { cx: L + (e + 0.5) * slot, cy: y(s.values[e]), r: 4, fill: s.color,
                stroke: "var(--surface)", "stroke-width": 2 }));
              break;
            }
          }
        });
      }
      var cross = el("svg:line", { class: "crosshair", y1: T, y2: T + ph, visibility: "hidden" });
      if (!stacked) svg.appendChild(cross);
      var dots = el("svg:g");
      svg.appendChild(dots);
      var sel = el("svg:rect", { class: "select", y: T, height: ph, width: 0, visibility: "hidden" });
      svg.appendChild(sel);
      if (max === 0) {
        svg.appendChild(el("svg:text", { x: L + pw / 2, y: T + ph / 2, "text-anchor": "middle", class: "empty",
          text: o.emptyText || "No requests in this range" }));
      }
      geo.band = band; geo.cross = cross; geo.dots = dots; geo.sel = sel;
      BV.clear(svgHolder).appendChild(svg);
      if (state.hover >= 0) show(state.hover);
      else if (state.marked >= 0) show(state.marked, true);
    }

    function show(i, quiet) {
      var o = state.opts;
      if (!geo || i < 0 || i >= o.n) return hide();
      if (!quiet) state.hover = i;
      var cx = geo.L + (i + 0.5) * geo.slot;
      geo.band.setAttribute("class", "hover-band" + (quiet ? " marked" : ""));
      geo.cross.setAttribute("class", "crosshair" + (quiet ? " marked" : ""));
      if (o.kind === "columns") {
        geo.band.setAttribute("x", geo.L + i * geo.slot);
        geo.band.setAttribute("width", Math.max(1, geo.slot));
        geo.band.setAttribute("visibility", "visible");
      } else {
        geo.cross.setAttribute("x1", Math.round(cx) + 0.5);
        geo.cross.setAttribute("x2", Math.round(cx) + 0.5);
        geo.cross.setAttribute("visibility", "visible");
        BV.clear(geo.dots);
        o.series.forEach(function (s) {
          var v = s.values[i];
          if (v === null || v === undefined) return;
          geo.dots.appendChild(el("svg:circle", { cx: cx, cy: geo.y(v), r: 4, fill: s.color, stroke: "var(--surface)", "stroke-width": 2 }));
        });
      }
      if (quiet) { tip.hidden = true; return; }
      var t0 = o.start + i * o.step, span = o.n * o.step;
      BV.clear(tip);
      tip.appendChild(el("div", { class: "when", text: BV.when(t0, span) + " – " + BV.when(t0 + o.step, span) +
        (i === o.partial ? " (so far)" : "") }));
      var rows = o.series.slice();
      if (o.kind === "columns") rows.reverse();          // top of the stack first, as on screen
      var total = 0, fmt = o.tipFormat || BV.int;
      rows.forEach(function (s) {
        var v = s.values[i];
        if (v !== null && v !== undefined) total += v;
        tip.appendChild(el("div", { class: "r" },
          el("i", { class: "key", style: "background:" + s.color }),
          el("b", { text: v === null || v === undefined ? "–" : fmt(v) }),
          o.series.length > 1 || s.label ? el("span", { text: s.label || "" }) : null));
      });
      if (o.kind === "columns" && o.series.length > 1) {
        tip.appendChild(el("div", { class: "r total" }, el("i", { class: "key" }), el("b", { text: fmt(total) }), el("span", { text: "total" })));
      }
      tip.hidden = false;
      var tw = tip.offsetWidth, left = cx + 14;
      if (left + tw > geo.W) left = cx - 14 - tw;
      tip.style.left = Math.max(0, left) + "px";
      tip.style.top = (geo.T + 4) + "px";
    }
    function hide() {
      state.hover = -1;
      tip.hidden = true;
      if (!geo) return;
      if (state.marked >= 0) { show(state.marked, true); return; }
      geo.band.setAttribute("visibility", "hidden");
      geo.cross.setAttribute("visibility", "hidden");
      BV.clear(geo.dots);
    }
    function indexAt(clientX) {
      var r = container.getBoundingClientRect();
      return Math.floor((clientX - r.left - geo.L) / geo.slot);
    }

    container.addEventListener("pointermove", function (ev) {
      if (!geo) return;
      var i = indexAt(ev.clientX);
      if (state.drag) {
        var r = container.getBoundingClientRect();
        var x = Math.max(geo.L, Math.min(geo.L + geo.pw, ev.clientX - r.left));
        var a = Math.min(state.drag.x, x), b = Math.max(state.drag.x, x);
        geo.sel.setAttribute("x", a);
        geo.sel.setAttribute("width", b - a);
        geo.sel.setAttribute("visibility", b - a > 4 ? "visible" : "hidden");
      }
      if (i >= 0 && i < state.opts.n) show(i); else hide();
    });
    container.addEventListener("pointerleave", function () { if (!state.drag) hide(); });
    container.addEventListener("pointerdown", function (ev) {
      if (!geo || !(state.opts.onZoom || state.opts.onClick) || ev.button !== 0) return;
      var r = container.getBoundingClientRect();
      state.drag = { x: Math.max(geo.L, Math.min(geo.L + geo.pw, ev.clientX - r.left)) };
      container.setPointerCapture(ev.pointerId);
    });
    container.addEventListener("pointerup", function (ev) {
      if (!state.drag) return;
      var r = container.getBoundingClientRect();
      var x = Math.max(geo.L, Math.min(geo.L + geo.pw, ev.clientX - r.left));
      var a = Math.min(state.drag.x, x), b = Math.max(state.drag.x, x);
      state.drag = null;
      geo.sel.setAttribute("visibility", "hidden");
      var o = state.opts;
      if (b - a > 6 && o.onZoom && ev.pointerType !== "touch") {
        var toT = function (px) { return o.start + (px - geo.L) / geo.slot * o.step; };
        o.onZoom(Math.round(toT(a)), Math.round(toT(b)));
      } else if (b - a <= 6 && o.onClick) {
        var i = Math.floor((x - geo.L) / geo.slot);
        if (i >= 0 && i < o.n) o.onClick(i);
      }
    });
    container.addEventListener("keydown", function (ev) {
      if (ev.key === "ArrowRight" || ev.key === "ArrowLeft") {
        var i = state.hover < 0 ? (ev.key === "ArrowRight" ? 0 : state.opts.n - 1)
          : state.hover + (ev.key === "ArrowRight" ? 1 : -1);
        show(Math.max(0, Math.min(state.opts.n - 1, i)));
        ev.preventDefault();
      } else if (ev.key === "Enter" && state.hover >= 0 && state.opts.onClick) {
        state.opts.onClick(state.hover);
      } else if (ev.key === "Escape") hide();
    });
    container.addEventListener("blur", hide);

    var ro = new ResizeObserver(function () { render(); });
    ro.observe(container);
    render();
    return {
      update: function (next) { state.opts = next; state.marked = -1; render(); },
      mark: function (i) {
        state.marked = i === undefined || i === null ? -1 : i;
        if (state.hover >= 0) return;               // the pointer's own hover wins
        if (state.marked >= 0) show(state.marked, true); else hide();
      },
      width: function () { return geo ? geo.pw : container.clientWidth; },
    };
  };

  BV.legend = function (node, series, kind) {
    BV.clear(node);
    if (series.length < 2) { node.hidden = true; return; }
    node.hidden = false;
    series.forEach(function (s) {
      node.appendChild(el("span", null, el("i", { class: kind === "lines" ? "line" : "", style: "background:" + s.color }),
        el("span", { text: s.label })));
    });
  };

  BV.chartTable = function (node, opts) {
    var span = opts.n * opts.step, fmt = opts.tipFormat || BV.int;
    var head = el("tr", null, el("th", { text: "Time" }), opts.series.map(function (s) {
      return el("th", { class: "num", text: s.label || "Value" });
    }));
    var body = el("tbody");
    for (var i = opts.n - 1; i >= 0; i--) {
      body.appendChild(el("tr", null, el("td", { text: BV.when(opts.start + i * opts.step, span) }),
        opts.series.map(function (s) {
          var v = s.values[i];
          return el("td", { class: "num", text: v === null || v === undefined ? "–" : fmt(v) });
        })));
    }
    BV.clear(node).appendChild(el("table", null, el("thead", null, head), body));
  };
})();
