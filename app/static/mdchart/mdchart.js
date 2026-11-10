/*
 * mdchart - charts from a Markdown-shaped fence.
 *
 * Compiles a ```chart fence into inline SVG. No dependencies, no network, no canvas, no
 * build step: drop in the script and the stylesheet and call MdChart.renderAll().
 *
 * SVG rather than canvas because it scales, it prints, and it can be drawn from CSS
 * custom properties - which is what lets a chart follow a page's light/dark switch with
 * no re-render at all. Mermaid and PlantUML cannot: they bake their colours into the SVG
 * they emit, which is why diagrams from those tools usually sit on a pinned white plate.
 *
 * The syntax is meant to survive being read as plain text. Someone looking at the raw
 * Markdown should see the numbers, not markup:
 *
 *     ```chart
 *     type: column
 *     title: Requests handled
 *     x: Mon, Tue, Wed
 *     ---
 *     auth    | 120, 140, 131
 *     gateway | 340, 352, 377
 *     ```
 *
 * Rules that are enforced rather than documented, because a chart that breaks them is
 * misleading rather than ugly:
 *
 *   - Categorical hues are assigned in a fixed order and never cycled. Past eight series
 *     the smallest fold into "Other" rather than being given a ninth indistinguishable
 *     hue, and every original row stays in the table view.
 *   - No dual axis, ever. Two measures of different scale are two charts.
 *   - One series gets one colour; bar length is never double-encoded as hue.
 *   - Every chart carries a table view, so no value is reachable only by hovering.
 *
 * A form that does not suit the data is drawn as the nearest one that does, with a line
 * saying what changed - a pie past six slices becomes a bar. Refusing to draw does not
 * make a pie readable; it just leaves the reader with nothing.
 *
 * The palette is a validated categorical set, checked with a contrast/CVD validator
 * against the plate surfaces it is actually drawn on (#EFF3F7 light, #131C26 dark) rather
 * than against white. Both modes pass; four light-mode hues sit below 3:1 against the
 * plate, which is why direct labels and the table view are not optional here.
 *
 * Colours are read from CSS custom properties (--mdc-series-1 ...) declared in
 * mdchart.css. This file never hard-codes a hue.
 *
 * MIT licensed. https://github.com/mainul35/mdchart
 */
(function (global) {
  "use strict";

  /* ---------------------------------------------------------------- constants */

  var SERIES_SLOTS = 8;

  /* Cap the mark, never fill the slot: the leftover band is the air that keeps a chart
     from reading as a solid block. */
  var MAX_BAR = 24;
  var BAR_RADIUS = 4;
  var LINE_WIDTH = 2;
  var MARKER_R = 4;
  var SURFACE_GAP = 2;

  var PAD = { top: 18, right: 18, bottom: 34, left: 48 };

  var TYPES = ["bar", "column", "line", "area", "pie", "donut", "stat"];

  /* ------------------------------------------------------------------ parsing */

  /**
   * Splits a fence into its settings block and its data rows.
   *
   * A line of three or more dashes separates the two. Without one, every line that looks
   * like "key: value" before the first data row is taken as a setting - so the simplest
   * useful chart is a bare list of "label | value" with no header at all.
   */
  function parse(source) {
    var lines = String(source == null ? "" : source).split(/\r?\n/);
    var settings = {};
    var rows = [];
    var inData = false;

    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      var trimmed = line.trim();
      if (!trimmed || trimmed.charAt(0) === "#") continue;

      if (!inData && /^-{3,}$/.test(trimmed)) { inData = true; continue; }

      if (!inData && trimmed.indexOf("|") < 0) {
        var colon = trimmed.indexOf(":");
        if (colon > 0) {
          settings[trimmed.slice(0, colon).trim().toLowerCase()] =
            trimmed.slice(colon + 1).trim();
          continue;
        }
      }
      inData = true;
      rows.push(trimmed);
    }
    return { settings: settings, rows: rows };
  }

  function splitList(text) {
    if (!text) return [];
    return String(text).split(",").map(function (s) { return s.trim(); })
      .filter(function (s) { return s.length > 0; });
  }

  /** A number, or NaN. Accepts thousands separators and a trailing % so units can be typed. */
  function num(text) {
    if (text == null) return NaN;
    var cleaned = String(text).replace(/,/g, "").replace(/%$/, "").trim();
    if (cleaned === "") return NaN;
    return Number(cleaned);
  }

  /**
   * Turns rows into series.
   *
   * One value per row is a single series over categories - the row label is the category.
   * Several values per row is one series per row, plotted against the x: setting.
   */
  function readData(parsed) {
    var rows = parsed.rows;
    var xs = splitList(parsed.settings.x);
    var series = [];
    var categories = [];
    var multi = false;

    for (var i = 0; i < rows.length; i++) {
      var bar = rows[i].indexOf("|");
      var name = bar >= 0 ? rows[i].slice(0, bar).trim() : "";
      var rest = bar >= 0 ? rows[i].slice(bar + 1) : rows[i];
      var values = splitList(rest).map(num);
      if (!values.length) continue;
      if (values.length > 1) multi = true;
      series.push({ name: name, values: values });
    }

    if (!multi) {
      /* Every row is one category of a single series. The row labels become the axis,
         and the chart gets exactly one colour - length already encodes magnitude, so
         colouring each bar differently would spend the only free channel saying it twice. */
      categories = series.map(function (s) { return s.name; });
      var flat = series.map(function (s) { return s.values[0]; });
      series = [{ name: parsed.settings.series || "", values: flat }];
    } else {
      var longest = 0;
      series.forEach(function (s) { longest = Math.max(longest, s.values.length); });
      categories = xs.length ? xs : countUp(longest);
    }
    return { series: series, categories: categories, singleSeries: !multi };
  }

  function countUp(n) {
    var out = [];
    for (var i = 1; i <= n; i++) out.push(String(i));
    return out;
  }

  /* ------------------------------------------------------------------- format */

  function fmt(value) {
    if (!isFinite(value)) return "";
    var abs = Math.abs(value);
    if (abs >= 1e9) return trimZero(value / 1e9) + "B";
    if (abs >= 1e6) return trimZero(value / 1e6) + "M";
    if (abs >= 1e4) return trimZero(value / 1e3) + "K";
    return String(Math.round(value * 100) / 100).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  function trimZero(n) {
    return String(Math.round(n * 10) / 10);
  }

  /** Axis ticks on clean numbers, so the reader is not decoding 1387.4. */
  function ticks(min, max, count) {
    if (min === max) { max = min + 1; }
    var span = max - min;
    var raw = span / Math.max(1, count);
    var mag = Math.pow(10, Math.floor(Math.log(raw) / Math.LN10));
    var step = mag;
    [1, 2, 2.5, 5, 10].some(function (m) {
      if (mag * m >= raw) { step = mag * m; return true; }
      return false;
    });
    var start = Math.floor(min / step) * step;
    var out = [];
    /* Up to and including the first tick at or above the maximum, which is the tick that
       decides how tall the plot is. Stopping at the last tick below it leaves the top of
       the scale short of the data: 377 against a scale ending at 300 drew a column taller
       than the plot it was in, off the top edge, with its value label somewhere above the
       SVG entirely. The chart looked deliberate and was wrong by a quarter. */
    for (var v = start; out.length < 200; v += step) {
      out.push(Math.round(v * 1e6) / 1e6);
      if (v >= max - step * 1e-9) break;
    }
    return out;
  }

  function esc(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  /** Rough text width, for deciding whether a label fits before drawing it. */
  function textWidth(text, size) {
    return String(text).length * size * 0.56;
  }

  function color(i) { return "var(--mdc-series-" + ((i % SERIES_SLOTS) + 1) + ")"; }

  /* -------------------------------------------------------------------- build */

  /**
   * The SVG is drawn at the width it will occupy, not scaled into it.
   *
   * <p>A fixed viewBox stretched to 100% scales the text with the geometry: an 11px label
   * in a 720-wide viewBox rendered into a 430px pane comes out near 7px, which is the
   * difference between a chart and a picture of one. Laying out at the measured width
   * keeps every label at the size it was specified in.
   */
  function svgOpen(w, h, title) {
    return '<svg class="mdc-svg" viewBox="0 0 ' + w + " " + h + '" width="' + w + '" '
      + 'height="' + h + '" role="img" '
      + 'aria-label="' + esc(title || "chart") + '">';
  }

  /**
   * A legend for two or more series, never for one.
   *
   * One series has one colour, and the title already says what is plotted; a box with a
   * single swatch restates it and costs space. For two or more the legend is the
   * dependable identity channel - direct labels supplement it, they do not replace it.
   */
  function legend(series) {
    if (series.length < 2) return "";
    var items = series.map(function (s, i) {
      return '<span class="mdc-key"><span class="mdc-swatch" style="background:'
        + color(i) + '"></span>' + esc(s.name || "series " + (i + 1)) + "</span>";
    }).join("");
    return '<div class="mdc-legend">' + items + "</div>";
  }

  /**
   * The table twin.
   *
   * Every value stays reachable without hovering, which is what keeps the tooltip an
   * enhancement rather than the only way to read the chart - and it is the relief for the
   * light-mode hues that sit below 3:1 against the plate.
   */
  function table(data, unit) {
    var head = "<tr><th>" + (data.singleSeries ? "Item" : "Series") + "</th>"
      + data.categories.map(function (c) { return "<th>" + esc(c) + "</th>"; }).join("")
      + "</tr>";
    var body = data.series.map(function (s, i) {
      var cells = data.categories.map(function (_, j) {
        return "<td>" + esc(fmt(s.values[j])) + "</td>";
      }).join("");
      return "<tr><th>" + esc(s.name || "series " + (i + 1)) + "</th>" + cells + "</tr>";
    }).join("");

    if (data.singleSeries) {
      var rows = data.categories.map(function (c, j) {
        return "<tr><th>" + esc(c) + "</th><td>"
          + esc(fmt(data.series[0].values[j])) + "</td></tr>";
      }).join("");
      return '<details class="mdc-table"><summary>Table' + (unit ? " (" + esc(unit) + ")" : "")
        + "</summary><table><tr><th>Item</th><th>Value</th></tr>" + rows + "</table></details>";
    }
    return '<details class="mdc-table"><summary>Table' + (unit ? " (" + esc(unit) + ")" : "")
      + "</summary><table>" + head + body + "</table></details>";
  }

  /* ----------------------------------------------------------- bar and column */

  function drawBars(data, opts, vertical, width) {
    var cats = data.categories;
    var series = data.series;
    var groups = cats.length;
    var perGroup = series.length;

    var all = [];
    series.forEach(function (s) {
      s.values.forEach(function (v) { if (isFinite(v)) all.push(v); });
    });
    var max = Math.max.apply(null, all.concat([0]));
    var min = Math.min.apply(null, all.concat([0]));
    var scaleTicks = ticks(min, max, 4);
    var lo = scaleTicks[0];
    var hi = scaleTicks[scaleTicks.length - 1];

    var labelRoom = vertical ? PAD.left : Math.min(190, longestLabel(cats, 12) + 14);
    var pad = {
      top: PAD.top,
      right: vertical ? PAD.right : 54,
      bottom: vertical ? PAD.bottom : 26,
      left: vertical ? labelRoom : labelRoom
    };

    var height = vertical
      ? Math.max(180, Math.min(340, 40 + groups * 34))
      : Math.max(120, pad.top + pad.bottom + groups * (perGroup * 22 + 16));

    var plotW = width - pad.left - pad.right;
    var plotH = height - pad.top - pad.bottom;
    var svg = [svgOpen(width, height, opts.title)];

    var pos = function (v) {
      var t = (v - lo) / (hi - lo || 1);
      return vertical ? pad.top + plotH - t * plotH : pad.left + t * plotW;
    };
    var zero = pos(Math.max(lo, Math.min(0, hi)));

    /* Gridlines: solid hairlines one step off the surface. Dashes read as a threshold. */
    scaleTicks.forEach(function (t) {
      var p = pos(t);
      svg.push(vertical
        ? '<line class="mdc-grid" x1="' + pad.left + '" y1="' + p + '" x2="'
            + (width - pad.right) + '" y2="' + p + '"/>'
        : '<line class="mdc-grid" x1="' + p + '" y1="' + pad.top + '" x2="' + p
            + '" y2="' + (pad.top + plotH) + '"/>');
      svg.push(vertical
        ? '<text class="mdc-tick" x="' + (pad.left - 8) + '" y="' + (p + 4)
            + '" text-anchor="end">' + esc(fmt(t)) + "</text>"
        : '<text class="mdc-tick" x="' + p + '" y="' + (pad.top + plotH + 16)
            + '" text-anchor="middle">' + esc(fmt(t)) + "</text>");
    });

    var band = (vertical ? plotW : plotH) / Math.max(1, groups);
    var thick = Math.min(MAX_BAR, Math.max(6, (band - SURFACE_GAP * (perGroup + 1)) / perGroup));

    for (var g = 0; g < groups; g++) {
      var bandStart = (vertical ? pad.left : pad.top) + g * band;
      var used = perGroup * thick + (perGroup - 1) * SURFACE_GAP;
      var start = bandStart + (band - used) / 2;

      for (var s = 0; s < perGroup; s++) {
        var value = series[s].values[g];
        if (!isFinite(value)) continue;
        var at = start + s * (thick + SURFACE_GAP);
        var end = pos(value);
        var label = fmt(value);
        var name = series[s].name || cats[g];

        if (vertical) {
          var top = Math.min(end, zero);
          var h = Math.abs(end - zero);
          svg.push(roundedBar(at, top, thick, h, true, value >= 0, color(s))
            + "<title>" + esc(name + " — " + label) + "</title></path>");
          svg.push('<text class="mdc-value" x="' + (at + thick / 2) + '" y="'
            + (top - 6) + '" text-anchor="middle">' + esc(label) + "</text>");
        } else {
          var left = Math.min(end, zero);
          var w = Math.abs(end - zero);
          svg.push(roundedBar(left, at, w, thick, false, value >= 0, color(s))
            + "<title>" + esc(name + " — " + label) + "</title></path>");
          svg.push('<text class="mdc-value" x="' + (Math.max(end, zero) + 6) + '" y="'
            + (at + thick / 2 + 4) + '">' + esc(label) + "</text>");
        }
      }

      var catLabel = cats[g];
      if (vertical) {
        svg.push('<text class="mdc-cat" x="' + (bandStart + band / 2) + '" y="'
          + (pad.top + plotH + 18) + '" text-anchor="middle">' + esc(catLabel) + "</text>");
      } else {
        svg.push('<text class="mdc-cat" x="' + (pad.left - 10) + '" y="'
          + (bandStart + band / 2 + 4) + '" text-anchor="end">' + esc(catLabel) + "</text>");
      }
    }

    svg.push("</svg>");
    return svg.join("");
  }

  function longestLabel(list, size) {
    var w = 0;
    list.forEach(function (t) { w = Math.max(w, textWidth(t, size)); });
    return w;
  }

  /**
   * A bar with its data-end rounded and its baseline end square.
   *
   * Rounding both ends detaches the bar from the axis and makes short bars read as pills;
   * the square end is what keeps it anchored to the baseline it grows from.
   */
  function roundedBar(x, y, w, h, vertical, positive, fill) {
    var r = Math.min(BAR_RADIUS, vertical ? w / 2 : h / 2, vertical ? h : w);
    var d;
    if (vertical) {
      d = positive
        ? "M" + x + "," + (y + h) + "V" + (y + r) + "q0," + -r + " " + r + "," + -r
          + "h" + (w - 2 * r) + "q" + r + ",0 " + r + "," + r + "V" + (y + h) + "z"
        : "M" + x + "," + y + "V" + (y + h - r) + "q0," + r + " " + r + "," + r
          + "h" + (w - 2 * r) + "q" + r + ",0 " + r + "," + -r + "V" + y + "z";
    } else {
      d = positive
        ? "M" + x + "," + y + "h" + (w - r) + "q" + r + ",0 " + r + "," + r
          + "v" + (h - 2 * r) + "q0," + r + " " + -r + "," + r + "H" + x + "z"
        : "M" + (x + w) + "," + y + "h" + -(w - r) + "q" + -r + ",0 " + -r + "," + r
          + "v" + (h - 2 * r) + "q0," + r + " " + r + "," + r + "H" + (x + w) + "z";
    }
    return '<path class="mdc-bar" fill="' + fill + '" d="' + d + '">';
  }

  /* ------------------------------------------------------------ line and area */

  function drawLines(data, opts, filled, width) {
    var cats = data.categories;
    var series = data.series;
    var height = 300;
    var pad = { top: PAD.top, right: 64, bottom: PAD.bottom, left: PAD.left };
    var plotW = width - pad.left - pad.right;
    var plotH = height - pad.top - pad.bottom;

    var all = [];
    series.forEach(function (s) {
      s.values.forEach(function (v) { if (isFinite(v)) all.push(v); });
    });
    var scaleTicks = ticks(Math.min.apply(null, all.concat([0])),
      Math.max.apply(null, all.concat([0])), 4);
    var lo = scaleTicks[0];
    var hi = scaleTicks[scaleTicks.length - 1];

    var x = function (i) {
      return pad.left + (cats.length < 2 ? plotW / 2 : (i / (cats.length - 1)) * plotW);
    };
    var y = function (v) { return pad.top + plotH - ((v - lo) / (hi - lo || 1)) * plotH; };

    var svg = [svgOpen(width, height, opts.title)];

    scaleTicks.forEach(function (t) {
      svg.push('<line class="mdc-grid" x1="' + pad.left + '" y1="' + y(t) + '" x2="'
        + (width - pad.right) + '" y2="' + y(t) + '"/>');
      svg.push('<text class="mdc-tick" x="' + (pad.left - 8) + '" y="' + (y(t) + 4)
        + '" text-anchor="end">' + esc(fmt(t)) + "</text>");
    });

    cats.forEach(function (c, i) {
      svg.push('<text class="mdc-cat" x="' + x(i) + '" y="' + (pad.top + plotH + 18)
        + '" text-anchor="middle">' + esc(c) + "</text>");
    });

    series.forEach(function (s, si) {
      var pts = [];
      s.values.forEach(function (v, i) {
        if (isFinite(v)) pts.push([x(i), y(v), v, i]);
      });
      if (!pts.length) return;

      if (filled) {
        /* A wash, never a saturated block: the line carries the series, the fill only
           says "area under it". */
        var area = "M" + pts[0][0] + "," + (pad.top + plotH)
          + pts.map(function (p) { return "L" + p[0] + "," + p[1]; }).join("")
          + "L" + pts[pts.length - 1][0] + "," + (pad.top + plotH) + "z";
        svg.push('<path class="mdc-area" fill="' + color(si) + '" d="' + area + '"/>');
      }

      svg.push('<path class="mdc-line" stroke="' + color(si) + '" d="M'
        + pts.map(function (p) { return p[0] + "," + p[1]; }).join("L") + '"/>');

      pts.forEach(function (p) {
        svg.push('<circle class="mdc-dot" fill="' + color(si) + '" cx="' + p[0]
          + '" cy="' + p[1] + '" r="' + MARKER_R + '"><title>'
          + esc((s.name || "value") + " — " + cats[p[3]] + ": " + fmt(p[2]))
          + "</title></circle>");
      });

      /* End labels only while they stay attached to their own line. Past four series
         they converge and nudging them apart reads as noise - the legend carries it. */
      if (series.length <= 4) {
        var last = pts[pts.length - 1];
        svg.push('<text class="mdc-value" x="' + (last[0] + 8) + '" y="' + (last[1] + 4)
          + '">' + esc(fmt(last[2])) + "</text>");
      }
    });

    svg.push("</svg>");
    return svg.join("");
  }

  /* ------------------------------------------------------------- pie and donut */

  function drawPie(data, opts, donut) {
    var values = data.series[0].values;
    var labels = data.categories;
    var total = values.reduce(function (a, b) { return a + (isFinite(b) ? b : 0); }, 0);
    if (total <= 0) return errorSvg("every slice is zero");

    var size = 300;
    var cx = size / 2;
    var cy = size / 2;
    var outer = 110;
    var inner = donut ? 62 : 0;
    var svg = [svgOpen(size, size, opts.title)];
    var angle = -Math.PI / 2;

    values.forEach(function (v, i) {
      if (!isFinite(v) || v <= 0) return;
      var sweep = (v / total) * Math.PI * 2;
      /* The 2px separation is a gap in the surface, not a stroke: a stroke would add ink
         that is not data. Converted to radians at the outer edge so it stays 2px wide. */
      var gap = Math.min(sweep / 3, SURFACE_GAP / outer);
      var a0 = angle + gap / 2;
      var a1 = angle + sweep - gap / 2;
      svg.push('<path class="mdc-slice" fill="' + color(i) + '" d="'
        + arc(cx, cy, outer, inner, a0, a1) + '"><title>'
        + esc(labels[i] + " — " + fmt(v) + " (" + Math.round((v / total) * 100) + "%)")
        + "</title></path>");
      angle += sweep;
    });

    if (donut) {
      svg.push('<text class="mdc-hero" x="' + cx + '" y="' + (cy + 6)
        + '" text-anchor="middle">' + esc(fmt(total)) + "</text>");
    }
    svg.push("</svg>");
    return svg.join("");
  }

  function arc(cx, cy, outer, inner, a0, a1) {
    var large = a1 - a0 > Math.PI ? 1 : 0;
    var x0 = cx + outer * Math.cos(a0), y0 = cy + outer * Math.sin(a0);
    var x1 = cx + outer * Math.cos(a1), y1 = cy + outer * Math.sin(a1);
    if (!inner) {
      return "M" + cx + "," + cy + "L" + x0 + "," + y0 + "A" + outer + "," + outer
        + " 0 " + large + " 1 " + x1 + "," + y1 + "z";
    }
    var xi1 = cx + inner * Math.cos(a1), yi1 = cy + inner * Math.sin(a1);
    var xi0 = cx + inner * Math.cos(a0), yi0 = cy + inner * Math.sin(a0);
    return "M" + x0 + "," + y0 + "A" + outer + "," + outer + " 0 " + large + " 1 "
      + x1 + "," + y1 + "L" + xi1 + "," + yi1 + "A" + inner + "," + inner
      + " 0 " + large + " 0 " + xi0 + "," + yi0 + "z";
  }

  /* --------------------------------------------------------------- stat tile */

  /**
   * Sometimes the answer is not a chart.
   *
   * A single number plotted as one bar is a bar chart that says less than the number
   * would on its own, so "type: stat" is offered and a one-value chart suggests it.
   */
  function drawStat(data, opts) {
    var value = data.series[0].values[0];
    var label = opts.title || data.categories[0] || "";
    var delta = opts.delta;
    var parts = ['<div class="mdc-stat">'];
    if (label) parts.push('<p class="mdc-stat-label">' + esc(label) + "</p>");
    parts.push('<p class="mdc-stat-value">' + esc(fmt(value))
      + (opts.unit ? '<span class="mdc-stat-unit">' + esc(opts.unit) + "</span>" : "")
      + "</p>");
    if (delta) {
      var down = /^-/.test(delta.trim());
      parts.push('<p class="mdc-stat-delta ' + (down ? "is-down" : "is-up") + '">'
        + esc(delta) + "</p>");
    }
    parts.push("</div>");
    return parts.join("");
  }

  /* ------------------------------------------------------------------- errors */

  /**
   * A chart this library will not draw, and why.
   *
   * <p>Presented as a note rather than an error, because it is not one. Nothing has gone
   * wrong here: a rule was applied - a seventh pie slice, a ninth series - and the honest
   * outcome is to say which rule and what to do instead. The first version painted these
   * in error red, and a reader seeing one in their own document reasonably read it as the
   * app having failed rather than as advice about the chart.
   *
   * <p>The fence body stays underneath either way, so what is on screen is still the
   * numbers rather than an empty plate.
   */
  function refuse(message, source) {
    return '<div class="mdc-note"><p class="mdc-note-msg">' + esc(message)
      + '</p><pre class="mdc-note-src">' + esc(source) + "</pre></div>";
  }

  /** A genuine failure, which is a different thing and looks like one. */
  function errorBlock(message, source) {
    return '<div class="mdc-error"><p class="mdc-error-msg">' + esc(message)
      + '</p><pre class="mdc-error-src">' + esc(source) + "</pre></div>";
  }

  function errorSvg(message) {
    return '<div class="mdc-error"><p class="mdc-error-msg">' + esc(message) + "</p></div>";
  }

  /* -------------------------------------------------------------------- render */

  /**
   * @param width the pixel width to lay out for; defaults to a readable page column when
   *              the caller cannot measure one (rendering outside a document, say).
   */
  function render(source, width) {
    width = Math.max(320, Math.round(width || 680));
    var parsed = parse(source);
    var opts = parsed.settings;
    var type = (opts.type || "").toLowerCase();

    var data = readData(parsed);
    if (!data.series.length || !data.categories.length) {
      return refuse("this chart has no data rows yet - add lines like "
        + '"label | value" under the ---', source);
    }

    /* Past eight series there are no more colours that can be told apart, so the tail is
       folded into one "Other" row rather than given a ninth hue nobody could distinguish.
       The full data is kept for the table below, so folding changes what is plotted and
       never what is recorded. */
    var full = data;
    var folded = null;
    if (data.series.length > SERIES_SLOTS) {
      folded = foldTail(data);
      data = folded.data;
    }

    var chosen = fit(type, data);
    type = chosen.type;

    var body;
    if (type === "stat") {
      body = drawStat(data, opts);
      return '<figure class="mdc-figure mdc-figure-stat">'
        + swapNote(chosen) + body + "</figure>";
    }

    var notes = foldNote(folded) + swapNote(chosen);

    if (type === "pie" || type === "donut") {
      body = drawPie(data, opts, type === "donut");
    } else if (type === "line" || type === "area") {
      body = drawLines(data, opts, type === "area", width);
    } else {
      body = drawBars(data, opts, type === "column", width);
    }

    var pieLegend = (type === "pie" || type === "donut")
      ? legendFromCategories(data.categories)
      : legend(data.series);

    /* A pie is square at a fixed size, so on a wide plate it has to be told where to sit;
       a plotted chart fills the plate and has no such question. */
    var round = type === "pie" || type === "donut";
    return '<figure class="mdc-figure' + (round ? " mdc-figure-round" : "") + '">'
      + (opts.title ? '<figcaption class="mdc-title">' + esc(opts.title)
          + (opts.unit ? ' <span class="mdc-unit">(' + esc(opts.unit) + ")</span>" : "")
          + "</figcaption>" : "")
      + notes
      + pieLegend
      + '<div class="mdc-plot">' + body + "</div>"
      /* The full data, not the folded set: the table is where nothing is allowed to be
         lost, and a reader checking what went into "Other" has to find it here. */
      + table(full, opts.unit)
      + "</figure>";
  }

  /**
   * Folds everything past the seventh series into one "Other" row.
   *
   * <p>Ranked by total size, so what survives as its own colour is what is actually big
   * enough to look at, and the rest is summed rather than dropped - the "Other" bar is the
   * true total of the tail, and every original row is still listed in the table below.
   *
   * <p>This is what the house guidance says to do with a ninth series, and it is better
   * than the alternatives on both sides: a generated ninth hue would be indistinguishable
   * from one already in use, and refusing to draw at all leaves a reader with numbers they
   * asked to see as a chart and a paragraph explaining why they cannot.
   */
  function foldTail(data) {
    var keep = SERIES_SLOTS - 1;
    var ranked = data.series.slice().sort(function (a, b) {
      return total(b) - total(a);
    });
    var kept = ranked.slice(0, keep);
    var tail = ranked.slice(keep);

    var other = { name: "Other", values: data.categories.map(function (_, j) {
      return tail.reduce(function (sum, s) {
        return sum + (isFinite(s.values[j]) ? s.values[j] : 0);
      }, 0);
    }) };

    /* Back into the order they were written in, so a reader's own row order survives; the
       ranking was only ever about deciding which ones to fold. */
    var order = data.series.map(function (s) { return s; });
    var keptInOrder = order.filter(function (s) { return kept.indexOf(s) >= 0; });

    return {
      data: {
        series: keptInOrder.concat([other]),
        categories: data.categories,
        singleSeries: false
      },
      count: tail.length,
      /* Named in the order they were written, not the order they were ranked in: the
         reader is looking for these rows in their own document. */
      names: order.filter(function (s) { return tail.indexOf(s) >= 0; })
        .map(function (s) { return s.name; })
    };
  }

  function total(series) {
    return series.values.reduce(function (sum, v) {
      return sum + (isFinite(v) ? Math.abs(v) : 0);
    }, 0);
  }

  /** Says which series were folded away, so "Other" is never an unexplained bar. */
  function foldNote(folded) {
    if (!folded) return "";
    return '<p class="mdc-swap">The ' + folded.count + " smallest series ("
      + esc(folded.names.join(", ")) + ') are drawn together as "Other" - past '
      + SERIES_SLOTS + " there are no more colours that can be told apart. "
      + "Every row is in the table below.</p>";
  }

  /**
   * The form this data will actually be drawn as, and why it is not the one asked for.
   *
   * <p>A rule that a chart breaks used to mean no chart: a seven-slice pie printed its
   * reason and the numbers, and nothing was plotted. The reason was right and the outcome
   * was still wrong - the reader wanted to see their data, and refusing to draw it does
   * not make a pie readable, it just leaves them with nothing.
   *
   * <p>So the rule is kept and the chart is drawn in the nearest form that honours it,
   * with a line saying what changed. The guardrail still holds - no misleading pie is ever
   * produced - and the document still has a chart in it. The only rules that survive as
   * outright refusals are the two that cannot be met by changing form: more series than
   * there are colours, and no data at all.
   */
  function fit(type, data) {
    var cats = data.categories.length;
    var count = data.series.length;
    var values = 0;
    var negative = false;
    data.series.forEach(function (s) {
      s.values.forEach(function (v) {
        if (isFinite(v)) { values++; if (v < 0) negative = true; }
      });
    });

    /* What this shape of data wants to be when nothing usable was asked for. */
    var natural = values === 1 ? "stat" : data.singleSeries ? "bar" : "column";

    if (!type) {
      return { type: natural, why: 'no "type:" was given' };
    }
    if (TYPES.indexOf(type) < 0) {
      return { type: natural, why: 'there is no "' + type + '" chart here' };
    }
    if (type === "pie" || type === "donut") {
      if (!data.singleSeries) {
        return { type: "column", why: "a pie takes one value per row and these have several" };
      }
      if (cats > 6) {
        return { type: "bar", why: "a pie is unreadable past 6 slices and this has " + cats };
      }
      if (negative) {
        return { type: "bar", why: "a negative value has no share of a whole" };
      }
    }
    if ((type === "line" || type === "area") && cats < 2) {
      return { type: natural, why: "a line needs at least two points and this has " + cats };
    }
    if (type === "area" && count > 3) {
      return { type: "line", why: "filled areas stop being readable stacked " + count + " deep" };
    }
    if (type === "stat" && values !== 1) {
      return { type: data.singleSeries ? "bar" : "column",
               why: "a stat shows one number and this has " + values };
    }
    return { type: type, why: null };
  }

  /** Says what was drawn instead, and why. Nothing at all when nothing was substituted. */
  function swapNote(chosen) {
    if (!chosen.why) return "";
    return '<p class="mdc-swap">Drawn as a ' + esc(chosen.type) + " chart - "
      + esc(chosen.why) + ".</p>";
  }

  /**
   * Which forms this data could take, and why the rest cannot have it.
   *
   * Asked by an editor before it offers "change chart type", so a menu can say what a
   * form would cost rather than silently omitting it or - worse - accepting it and
   * dropping the values that no longer fit. Every answer here is derived from the same
   * rules render() enforces; nothing decides twice.
   *
   * Returned as lines of "key=value" rather than as an object, because the caller is
   * often on the other side of a string-only bridge into a host application.
   */
  function describe(source) {
    var parsed = parse(source);
    var data = readData(parsed);
    var cats = data.categories.length;
    var count = data.series.length;
    var values = 0;
    var negative = false;
    data.series.forEach(function (s) {
      s.values.forEach(function (v) {
        if (isFinite(v)) { values++; if (v < 0) negative = true; }
      });
    });

    var out = ["type=" + (parsed.settings.type || "").toLowerCase().trim()];
    out.push("series=" + count);
    out.push("values=" + values);

    function say(name, reason) { out.push(name + "=" + (reason || "ok")); }

    /* A long tail is not a reason to withhold a form: past eight series the smallest are
       folded into "Other" and the chart is drawn either way, so every plotted form is
       still on offer here. */
    say("bar", null);
    say("column", null);
    /* A line needs somewhere to go. Through one category it is a dot with an axis. */
    var line = cats < 2 ? "needs at least two points" : null;
    say("line", line);
    /* Filled areas stack up visually even when they are not stacked, so past three the
       ones underneath stop being readable. */
    say("area", line || (count > 3 ? "too many series to overlay" : null));
    var part = !data.singleSeries ? "needs one value per row"
      : cats > 6 ? "unreadable past 6 slices (" + cats + ")"
      : negative ? "a negative value has no share of a whole"
      : null;
    say("pie", part);
    say("donut", part);
    /* A stat tile shows one number. Offering it for a table of them would not be a
       different chart, it would be a chart with the rest of the data thrown away. */
    say("stat", values === 1 ? null : "shows one number; this has " + values);
    return out.join("\n");
  }

  /**
   * The fence, taken apart into the grid an editor can put on screen.
   *
   * <p>Asked for when a chart is opened for editing. The editor needs rows, columns and
   * settings as separate things, and this is the only place that knows how to get them
   * out of the text - working it out a second time in the editor is how two readings of
   * the same fence start to disagree.
   *
   * <p>Values come back as they were written rather than as numbers, so "1,200" and
   * "12.5%" survive being edited and put back. Tab-separated, because a label may well
   * contain a comma and every one of these fields is free text.
   */
  function model(source) {
    var parsed = parse(source);
    var rows = [];
    var widest = 0;
    for (var i = 0; i < parsed.rows.length; i++) {
      var bar = parsed.rows[i].indexOf("|");
      var name = bar >= 0 ? parsed.rows[i].slice(0, bar).trim() : "";
      var rest = bar >= 0 ? parsed.rows[i].slice(bar + 1) : parsed.rows[i];
      var values = splitList(rest);
      if (!values.length) continue;
      widest = Math.max(widest, values.length);
      rows.push({ name: name, values: values });
    }

    /* The same test render() uses: one value per row is a list of items, several is a
       series per row plotted against the categories. */
    var single = widest <= 1;
    var categories = single ? [] : (splitList(parsed.settings.x).length
      ? splitList(parsed.settings.x) : countUp(widest));

    var out = [];
    out.push("type=" + (parsed.settings.type || ""));
    out.push("title=" + (parsed.settings.title || ""));
    out.push("unit=" + (parsed.settings.unit || ""));
    out.push("delta=" + (parsed.settings.delta || ""));
    out.push("single=" + single);
    out.push("categories=" + categories.join("\t"));
    rows.forEach(function (r) {
      out.push("row=" + r.name + "\t" + r.values.join("\t"));
    });
    return out.join("\n");
  }

  /** A pie's slices are its categories, so its legend is built from those, not series. */
  function legendFromCategories(cats) {
    var items = cats.map(function (c, i) {
      return '<span class="mdc-key"><span class="mdc-swatch" style="background:'
        + color(i) + '"></span>' + esc(c) + "</span>";
    }).join("");
    return '<div class="mdc-legend">' + items + "</div>";
  }

  /**
   * Replaces every un-rendered chart block on the page.
   *
   * Marked done rather than removed, so a re-render of the same DOM does not compile a
   * chart twice, and so a failure leaves the source visible instead of a blank plate.
   */
  /**
   * Where a chart fence ends up in HTML, by the time it reaches this library.
   *
   * <p>Different Markdown renderers emit it differently: most produce
   * {@code <pre><code class="language-chart">}, some tag the {@code <pre>} itself, and an
   * application that renders its own can use whatever it likes. All of the usual shapes
   * are matched by default and a caller can pass its own selector instead.
   */
  var DEFAULT_SELECTOR = "pre.mdchart, pre.language-chart, pre.mdv-chart, "
    + "pre > code.language-chart, pre > code.language-mdchart";

  function renderAll(root, selector) {
    var scope = root || document;
    var found = scope.querySelectorAll(selector || DEFAULT_SELECTOR);
    var blocks = [];
    for (var b = 0; b < found.length; b++) {
      /* A matched <code> means the <pre> around it is the block to replace: replacing the
         code alone would leave an empty <pre> wrapped around the chart. */
      var block = found[b].tagName === "CODE" ? found[b].parentNode : found[b];
      if (block && !block.hasAttribute("data-mdc-done")
          && blocks.indexOf(block) < 0) {
        blocks.push(block);
      }
    }
    for (var i = 0; i < blocks.length; i++) {
      var pre = blocks[i];
      var source = pre.textContent || "";
      var host = document.createElement("div");
      host.className = "mdv-chart-out";
      /* The source is kept on the element so a resize can lay the same chart out again
         at the new width. Reading it back off the rendered SVG would be guesswork. */
      host.setAttribute("data-mdc-src", source);
      host.setAttribute("data-mdc-done", "1");
      /* The fence's offsets travel with the chart. The <pre> that carried them is about
         to be replaced, and without them on the survivor the editor could render a chart
         it can never point back at. */
      copyAttr(pre, host, "data-md-start");
      copyAttr(pre, host, "data-md-end");
      if (pre.parentNode) pre.parentNode.replaceChild(host, pre);
      draw(host, source);
    }
    return blocks.length;
  }

  /**
   * Lays one chart out, at {@code width} if given and at its container's width otherwise.
   *
   * <p>A caller supplies the width when the width that matters is not the one on screen -
   * printing being the case that exists: paper is a different measure from the pane, and a
   * chart laid out for the pane and then scaled to fit the page takes its type size down
   * with it.
   *
   * <p>What the caller passes is the width of the space the chart has, not the width to
   * draw at: the plate's own padding comes off it here, so a caller never has to know how
   * thick this chart's chrome happens to be.
   */
  function draw(host, source, width) {
    try {
      host.innerHTML = render(source, width ? usable(width) : measure(host));
    } catch (e) {
      host.innerHTML = errorBlock("chart failed to render: " + (e && e.message), source);
    }
  }

  /**
   * Draws every chart on the page again, at {@code width} if one is given.
   *
   * <p>Unconditional, unlike relayout(): the caller has a reason the page cannot see.
   */
  function redrawAll(width) {
    var hosts = document.querySelectorAll(".mdv-chart-out[data-mdc-src]");
    for (var i = 0; i < hosts.length; i++) {
      draw(hosts[i], hosts[i].getAttribute("data-mdc-src") || "", width);
    }
    /* Forget the last measured width, or the next resize check would compare against a
       width no chart on the page is currently drawn at and decide there is nothing to do. */
    lastWidth = -1;
    return hosts.length;
  }

  /**
   * The usable width inside the chart plate.
   *
   * <p>The host's own width, not its parent's. An earlier version asked the parent on the
   * grounds that the host is empty at this point and an empty block is as wide as its
   * parent anyway - which is true of a block with no width of its own, and false of this
   * one: the plate is sized by the page's own rules, so in full-preview the host was 640
   * and the body it was asked about was 1223. The chart was drawn at 1183 and displayed at
   * 520, and the browser scaled the difference out of the type.
   *
   * <p>Reading clientWidth forces layout, so the empty host has a real width by the time
   * the answer comes back.
   */
  function measure(host) {
    return usable(host.clientWidth || (host.parentNode && host.parentNode.clientWidth) || 0);
  }

  /** What is left of a container once the figure's padding and border are taken off. */
  function usable(w) {
    return w > 80 ? w - 40 : 680;
  }

  /**
   * Re-lays every chart after the pane changes width.
   *
   * <p>Debounced, because dragging the split divider fires continuously and each redraw
   * rebuilds an SVG. Compared against the last width used so that a resize in the other
   * axis - or one that rounds to the same width - costs nothing.
   */
  var resizeTimer = null;
  var lastWidth = 0;
  function watchResize() {
    if (typeof window === "undefined" || window.__mdcWatching) return;
    window.__mdcWatching = true;
    window.addEventListener("resize", function () {
      if (resizeTimer) clearTimeout(resizeTimer);
      resizeTimer = setTimeout(relayout, 120);
    });
  }

  function relayout() {
    var hosts = document.querySelectorAll(".mdv-chart-out[data-mdc-src]");
    if (!hosts.length) return;
    var w = measure(hosts[0]);
    if (Math.abs(w - lastWidth) < 8) return;
    lastWidth = w;
    for (var i = 0; i < hosts.length; i++) {
      draw(hosts[i], hosts[i].getAttribute("data-mdc-src") || "");
    }
  }

  watchResize();

  function copyAttr(from, to, name) {
    var value = from.getAttribute(name);
    if (value !== null) to.setAttribute(name, value);
  }

  global.MdChart = {
    render: render, renderAll: renderAll, relayout: relayout, redrawAll: redrawAll,
    parse: parse, describe: describe, model: model, types: TYPES
  };
})(typeof window !== "undefined" ? window : this);
