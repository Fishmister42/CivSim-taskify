/*
 * Metric trajectories (T045, extended by T056).
 *
 * T045 (US3): render a single run's SVG trajectory from `MetricSeriesView`
 * JSON and wire each point to its turn (FR-015).
 * T056 (US4): multi-series overlay, and divergence-point click-through to the
 * matching turn in *every* compared run (FR-022).
 *
 * Five properties this file is careful about. The first two are the reason it
 * is written the way it is rather than as a chart library call.
 *
 * 1. **It enhances a chart the server already drew; it never is the chart.**
 *    `templates/history/metrics.html` and `templates/catalog/compare.html` both
 *    render a complete inline SVG with no JavaScript at all (research R4). A
 *    chart that only exists once a script runs is a chart the directing Claude
 *    Code session cannot see, which is the asymmetry Principle VI forbids. The
 *    one figure this file *draws* is the deferred one on the replay page, and
 *    that figure ships as a real link to the page whose server-rendered chart
 *    carries the identical content -- so both readers reach it either way.
 *
 * 2. **A gapped turn is a break in the line, never a join.** The server omits a
 *    gapped turn from `points` (data-model.md SS10, FR-025), so joining
 *    `points` end to end draws a straight line *across* the gap -- silently
 *    bridging the exact hole Principle III's quarantine machinery exists to
 *    surface. `segmentsOf` below starts a new polyline whenever two consecutive
 *    points are not on consecutive turns. It is the same rule
 *    `MetricSeriesView.segments` applies server-side, deliberately in its
 *    fail-closed form: it does not consult `gapped_turns` or `missing_turns`,
 *    so a hole of a third kind still breaks the line.
 *
 * 3. **It draws from `series`, never from `runs`.** A quarantined run is listed
 *    in `runs` (so the user can see why it was excluded) and is absent from
 *    `series` by design. Drawing from `runs` would put a trend line back under
 *    a run whose record has gaps, reintroducing on the client exactly what the
 *    server refuses to construct. `runs` is read for one thing only: a run's
 *    position in it is its colour index, which is how the legend stays honest.
 *
 * 4. **The axes come from the response.** `axes[metric]` carries `min_turn`,
 *    `max_turn`, `min_value`, `max_value`. Recomputing them here would let the
 *    chart and the JSON's own description of it disagree.
 *
 * 5. **It issues nothing but GETs**, like `poll.js`. The interface reports and
 *    never acts (FR-026, UP-010).
 */

(function () {
  "use strict";

  var SVG_NS = "http://www.w3.org/2000/svg";

  /* The palette `catalog/compare.html` uses, in its order. Matching it is what
   * keeps a script-drawn chart and a server-drawn legend agreeing about which
   * colour is which run. */
  var PALETTE = ["#2f6fdb", "#d9822b", "#2f9e6f", "#a64ca6", "#c0392b", "#1b7f8c"];

  var W = 720;
  var H = 260;
  var PAD_L = 44;
  var PAD_R = 16;
  var PAD_T = 18;
  var PAD_B = 34;

  /* ---------------------------------------------------------------- *
   * The break rule. See property 2 in the header.
   * ---------------------------------------------------------------- */

  function segmentsOf(points) {
    var segments = [];
    var current = [];
    for (var i = 0; i < points.length; i += 1) {
      if (current.length && points[i].turn !== current[current.length - 1].turn + 1) {
        segments.push(current);
        current = [];
      }
      current.push(points[i]);
    }
    if (current.length) {
      segments.push(current);
    }
    return segments;
  }

  function breakTurnsOf(segments) {
    var turns = [];
    for (var i = 0; i < segments.length - 1; i += 1) {
      turns.push(segments[i][segments[i].length - 1].turn + 1);
    }
    return turns;
  }

  /* ---------------------------------------------------------------- *
   * Scaling, from the response's own axes (property 4).
   * ---------------------------------------------------------------- */

  function scaler(axis) {
    var spanT = (axis.max_turn - axis.min_turn) || 1;
    var spanV = (axis.max_value - axis.min_value) || 1;
    return {
      x: function (turn) {
        return PAD_L + ((turn - axis.min_turn) / spanT) * (W - PAD_L - PAD_R);
      },
      y: function (value) {
        return (H - PAD_B) - ((value - axis.min_value) / spanV) * (H - PAD_T - PAD_B);
      }
    };
  }

  function el(name, attrs) {
    var node = document.createElementNS(SVG_NS, name);
    var keys = Object.keys(attrs || {});
    for (var i = 0; i < keys.length; i += 1) {
      node.setAttribute(keys[i], String(attrs[keys[i]]));
    }
    return node;
  }

  function titled(node, text) {
    var title = document.createElementNS(SVG_NS, "title");
    title.textContent = text;
    node.appendChild(title);
    return node;
  }

  /* ---------------------------------------------------------------- *
   * Drawing
   * ---------------------------------------------------------------- */

  function drawAxes(svg, axis) {
    svg.appendChild(el("line", {
      "class": "axis", x1: PAD_L, y1: PAD_T, x2: PAD_L, y2: H - PAD_B
    }));
    svg.appendChild(el("line", {
      "class": "axis", x1: PAD_L, y1: H - PAD_B, x2: W - PAD_R, y2: H - PAD_B
    }));
    var labels = [
      ["axis-label", PAD_L, H - PAD_B + 20, "turn " + axis.min_turn],
      ["axis-label axis-label-end", W - PAD_R, H - PAD_B + 20, "turn " + axis.max_turn],
      ["axis-label", 4, PAD_T + 8, String(axis.max_value)],
      ["axis-label", 4, H - PAD_B, String(axis.min_value)]
    ];
    for (var i = 0; i < labels.length; i += 1) {
      var text = el("text", {
        "class": labels[i][0], x: labels[i][1], y: labels[i][2]
      });
      text.textContent = labels[i][3];
      svg.appendChild(text);
    }
  }

  function drawSeries(svg, series, axis, colour) {
    var to = scaler(axis);
    var segments = segmentsOf(series.points || []);
    var i;
    var j;

    for (i = 0; i < segments.length; i += 1) {
      var coordinates = [];
      for (j = 0; j < segments[i].length; j += 1) {
        coordinates.push(
          to.x(segments[i][j].turn).toFixed(2) + "," + to.y(segments[i][j].value).toFixed(2)
        );
      }
      svg.appendChild(el("polyline", {
        "class": "series",
        "data-series-run": series.run_id,
        "data-series-segment": i,
        fill: "none",
        stroke: colour,
        "stroke-width": 2,
        points: coordinates.join(" ")
      }));
    }

    /* The break is drawn, not merely implied by the absence of a line: a hole a
     * reader has to notice is a hole a reader can miss (UP-005). */
    var breaks = breakTurnsOf(segments);
    for (i = 0; i < breaks.length; i += 1) {
      var marker = el("g", {
        "class": "series-break",
        "data-break-turn": breaks[i],
        "data-break-run": series.run_id
      });
      titled(marker,
        series.run_id + " has no recorded value from turn " + breaks[i] +
        " — the line breaks rather than joining across it");
      marker.appendChild(el("line", {
        "class": "series-break-rule",
        x1: to.x(breaks[i]).toFixed(2),
        y1: PAD_T,
        x2: to.x(breaks[i]).toFixed(2),
        y2: H - PAD_B,
        "stroke-dasharray": "3 3"
      }));
      svg.appendChild(marker);
    }

    /* FR-015: every point is a link to its turn. An `<a>` rather than a click
     * handler, so it is followable with scripting disabled once drawn and
     * reachable by keyboard. */
    var points = series.points || [];
    for (i = 0; i < points.length; i += 1) {
      var link = el("a", {
        "class": "turn-point",
        href: "/runs/" + encodeURIComponent(series.run_id) + "/turns/" + points[i].turn,
        "data-point-turn": points[i].turn,
        "data-point-run": series.run_id
      });
      titled(link,
        "Turn " + points[i].turn + " — " + series.metric_name + " " + points[i].value);
      link.appendChild(el("circle", {
        "class": "turn-dot",
        cx: to.x(points[i].turn).toFixed(2),
        cy: to.y(points[i].value).toFixed(2),
        r: 3,
        fill: colour
      }));
      svg.appendChild(link);
    }
  }

  function drawDivergences(svg, divergences, metric, axis) {
    var to = scaler(axis);
    for (var i = 0; i < divergences.length; i += 1) {
      var dp = divergences[i];
      if (dp.metric_name !== metric) {
        continue;
      }
      var refs = dp.refs || {};
      var href = refs[dp.leader_run_id] || ("/runs/" + dp.leader_run_id);
      var marker = el("a", {
        "class": "divergence",
        href: href,
        "data-divergence-turn": dp.turn,
        "data-divergence-kind": dp.kind
      });
      titled(marker,
        "Turn " + dp.turn + " — " + String(dp.kind).replace(/_/g, " ") +
        " — leader " + dp.leader_run_id + " at " + dp.leader_value);
      marker.appendChild(el("line", {
        "class": "divergence-rule",
        x1: to.x(dp.turn).toFixed(2), y1: PAD_T,
        x2: to.x(dp.turn).toFixed(2), y2: H - PAD_B
      }));
      marker.appendChild(el("circle", {
        "class": "divergence-dot",
        cx: to.x(dp.turn).toFixed(2),
        cy: to.y(dp.leader_value).toFixed(2),
        r: 5
      }));
      svg.appendChild(marker);
    }
  }

  /* ---------------------------------------------------------------- *
   * Normalising the two response shapes
   * ---------------------------------------------------------------- */

  /* `/runs/{id}/metrics` carries `series` as a flat list; `/compare` carries it
   * as `{metric: [series]}`. Both are read the same way from here on, and
   * **neither is read from `runs`** (property 3). */
  function seriesByMetric(payload) {
    var byMetric = {};
    var i;
    if (Array.isArray(payload.series)) {
      for (i = 0; i < payload.series.length; i += 1) {
        var found = payload.series[i];
        if (!byMetric[found.metric_name]) {
          byMetric[found.metric_name] = [];
        }
        byMetric[found.metric_name].push(found);
      }
      return byMetric;
    }
    var metrics = Object.keys(payload.series || {});
    for (i = 0; i < metrics.length; i += 1) {
      byMetric[metrics[i]] = payload.series[metrics[i]] || [];
    }
    return byMetric;
  }

  /* A run's colour is its position in `runs` -- the order `compare.html`
   * indexes the palette by. Runs absent from `runs` (the single-run page has no
   * such key) fall back to their position among the drawn series. */
  function colourIndexes(payload) {
    var index = {};
    var runs = payload.runs || [];
    for (var i = 0; i < runs.length; i += 1) {
      index[runs[i].run_id] = i;
    }
    return index;
  }

  function colourFor(runId, byRun, fallback) {
    var position = Object.prototype.hasOwnProperty.call(byRun, runId) ? byRun[runId] : fallback;
    return PALETTE[position % PALETTE.length];
  }

  /* ---------------------------------------------------------------- *
   * The one figure this file draws: the deferred one on the replay page
   * ---------------------------------------------------------------- */

  function renderInto(figure, payload, metric) {
    var byMetric = seriesByMetric(payload);
    var axes = payload.axes || {};
    var chosen = metric || Object.keys(byMetric).sort()[0];
    if (!chosen || !axes[chosen] || !byMetric[chosen] || !byMetric[chosen].length) {
      return false;
    }

    var axis = axes[chosen];
    var svg = el("svg", {
      "class": "trajectory-svg",
      viewBox: "0 0 " + W + " " + H,
      role: "img",
      "aria-label": chosen + " across " + byMetric[chosen].length + " run(s), turns " +
        axis.min_turn + " to " + axis.max_turn
    });
    drawAxes(svg, axis);

    var byRun = colourIndexes(payload);
    for (var i = 0; i < byMetric[chosen].length; i += 1) {
      drawSeries(svg, byMetric[chosen][i], axis, colourFor(byMetric[chosen][i].run_id, byRun, i));
    }
    drawDivergences(svg, payload.divergence_points || [], chosen, axis);

    figure.setAttribute("data-metric", chosen);
    figure.insertBefore(svg, figure.querySelector(".trajectory-link"));
    figure.classList.remove("trajectory-deferred");
    figure.classList.add("trajectory-rendered");
    return true;
  }

  function loadDeferred(figure) {
    var source = figure.getAttribute("data-trajectory-src");
    if (!source) {
      return;
    }
    fetch(source, {
      method: "GET",
      headers: { Accept: "application/json" },
      cache: "no-store",
      credentials: "omit"
    })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("trajectory fetch returned " + response.status);
        }
        return response.json();
      })
      .then(function (payload) {
        renderInto(figure, payload, figure.getAttribute("data-metric"));
        highlightTurn(figure, figure.getAttribute("data-trajectory-turn"));
      })
      .catch(function () {
        /* The link the figure already carries is the fallback, and it points at
         * the same server-rendered chart. A failed enhancement leaves the page
         * exactly as the no-JavaScript reader sees it, which is a correct page
         * rather than a broken one. */
      });
  }

  function highlightTurn(figure, turn) {
    if (!turn) {
      return;
    }
    var here = figure.querySelector('[data-point-turn="' + turn + '"] .turn-dot');
    if (here) {
      here.setAttribute("r", "6");
      here.classList.add("turn-dot-here");
    }
  }

  /* ---------------------------------------------------------------- *
   * T056: enhancing the server-rendered charts
   * ---------------------------------------------------------------- */

  /* Clicking a divergence marker reveals that turn in *every* compared run
   * (FR-022), not only the leader's. The refs are already on the page -- the
   * divergence section lists one `<a>` per compared run -- so this is a
   * reveal, not a second round trip, and following the marker's own `href`
   * stays the behaviour with scripting off. */
  function wireDivergences(root) {
    var markers = root.querySelectorAll(".divergence[data-divergence-turn]");
    for (var i = 0; i < markers.length; i += 1) {
      markers[i].addEventListener("click", function (event) {
        var turn = this.getAttribute("data-divergence-turn");
        var rows = document.querySelectorAll(".divergence-row");
        var target = null;
        for (var r = 0; r < rows.length; r += 1) {
          var cell = rows[r].querySelector('[data-field$=".turn"] .field-value');
          if (cell && cell.textContent.trim() === String(turn)) {
            target = rows[r];
            break;
          }
        }
        if (!target) {
          return; /* nothing to reveal; the marker's own href still navigates */
        }
        event.preventDefault();
        var open = document.querySelectorAll(".divergence-row.revealed");
        for (var o = 0; o < open.length; o += 1) {
          open[o].classList.remove("revealed");
        }
        target.classList.add("revealed");
        target.scrollIntoView({ block: "nearest" });
        var first = target.querySelector(".divergence-refs a");
        if (first) {
          first.focus();
        }
      });
    }
  }

  /* Hovering a legend entry emphasises that run's line across every metric.
   * Reading a five-run overlay is the thing SC-007 puts two minutes on, and the
   * lines are the part that is hard to tell apart. */
  function wireLegend(root) {
    var entries = root.querySelectorAll(".legend-item [data-field$='.run_id']");
    for (var i = 0; i < entries.length; i += 1) {
      (function (entry) {
        var runId = entry.textContent.trim();
        function emphasise(on) {
          var lines = document.querySelectorAll('[data-series-run="' + runId + '"]');
          for (var j = 0; j < lines.length; j += 1) {
            lines[j].classList.toggle("series-emphasised", on);
          }
        }
        entry.addEventListener("mouseenter", function () { emphasise(true); });
        entry.addEventListener("mouseleave", function () { emphasise(false); });
        entry.addEventListener("focus", function () { emphasise(true); });
        entry.addEventListener("blur", function () { emphasise(false); });
      })(entries[i]);
    }
  }

  function start() {
    var figures = document.querySelectorAll("figure.trajectory");
    for (var i = 0; i < figures.length; i += 1) {
      if (figures[i].getAttribute("data-trajectory-src")) {
        loadDeferred(figures[i]);
      }
    }
    wireDivergences(document);
    wireLegend(document);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
