/*
 * Live polling for the run detail view (T031).
 *
 * FR-002: the view updates as new turns are recorded, without the user
 * reloading or re-navigating, and indicates when it was last confirmed current.
 * SC-003 puts a 5-second bound on a newly recorded turn appearing. Research R5
 * fixes the interval at 2 seconds, well inside that bound, so a single missed
 * poll still lands the next one comfortably within the window.
 *
 * Three properties this file is careful about:
 *
 * 1. **It polls the same endpoint the directing Claude Code session reads.**
 *    `GET /runs/{run_id}` with `Accept: application/json` is the machine
 *    surface; the page updates itself from exactly that, so what the browser
 *    shows can never drift from what the session retrieves (Principle VI,
 *    FR-007). There is no private "UI endpoint".
 *
 * 2. **It issues nothing but GETs.** The interface reports and never acts
 *    (FR-026, UP-010). No POST, no PUT, no DELETE appears anywhere below.
 *
 * 3. **A missed poll marks the view possibly stale and keeps going.** Spec Edge
 *    Cases: "the connection between the browser and the interface drops -- the
 *    view marks itself as possibly stale and recovers to live without a manual
 *    reload." Staleness is a *display* state about this browser's connection,
 *    never a judgment about the run's health, which only the store's own events
 *    may decide (research R7, invariant V3).
 *
 * Updating works off the `data-field` attributes the templates emit, whose
 * values are the JSON paths of the response body. That is why a field added to
 * a view model by a later story starts updating live with no change here.
 */

(function () {
  "use strict";

  var POLL_INTERVAL_MS = 2000;
  var root = document.documentElement;
  var runId = root.getAttribute("data-run-id");
  if (!runId || root.getAttribute("data-live") !== "true") {
    return;
  }

  var staleMarker = document.getElementById("stale-marker");
  var consecutiveFailures = 0;

  function setStale(isStale) {
    if (!staleMarker) {
      return;
    }
    staleMarker.hidden = !isStale;
    root.setAttribute("data-stale", isStale ? "true" : "false");
  }

  /* Flatten a JSON body into { "dotted.path": scalar }, matching the paths the
   * templates put in `data-field`. Empty containers are leaves, exactly as the
   * `node` macro treats them, so "nothing recorded" stays visible rather than
   * quietly disappearing on the first poll. */
  function flatten(value, prefix, out) {
    if (value === null || typeof value !== "object") {
      out[prefix] = value;
      return out;
    }
    if (Array.isArray(value)) {
      if (value.length === 0) {
        out[prefix] = null;
        return out;
      }
      for (var i = 0; i < value.length; i += 1) {
        flatten(value[i], prefix ? prefix + "." + i : String(i), out);
      }
      return out;
    }
    var keys = Object.keys(value);
    if (keys.length === 0) {
      out[prefix] = null;
      return out;
    }
    for (var k = 0; k < keys.length; k += 1) {
      var key = keys[k];
      flatten(value[key], prefix ? prefix + "." + key : key, out);
    }
    return out;
  }

  function render(value) {
    if (value === null || value === undefined) {
      return "—";
    }
    if (value === true) {
      return "yes";
    }
    if (value === false) {
      return "no";
    }
    return String(value);
  }

  function applyField(element, value) {
    if (element.tagName === "IMG") {
      /* A capture that became unavailable between polls must stop being shown,
       * not linger as the last image that happened to load (FR-034). */
      if (value) {
        if (element.getAttribute("src") !== value) {
          element.setAttribute("src", value);
        }
      } else {
        element.removeAttribute("src");
      }
      return;
    }
    if (element.tagName === "A") {
      element.setAttribute("href", value || "#");
      element.textContent = render(value);
      return;
    }
    var target = element.querySelector(".field-value");
    (target || element).textContent = render(value);
  }

  /* The structure of a response can change between polls -- a run's first turn
   * arrives, a step is added. Fields the page has no element for are a signal
   * that the shape changed, and a full reload is the honest way to pick up new
   * markup rather than half-updating a page that no longer matches the data. */
  function apply(body) {
    var flat = flatten(body, "", {});
    var elements = document.querySelectorAll("[data-field]");
    var seen = {};
    for (var i = 0; i < elements.length; i += 1) {
      var element = elements[i];
      var path = element.getAttribute("data-field");
      seen[path] = true;
      if (Object.prototype.hasOwnProperty.call(flat, path)) {
        applyField(element, flat[path]);
      }
    }
    var paths = Object.keys(flat);
    for (var p = 0; p < paths.length; p += 1) {
      if (!seen[paths[p]]) {
        window.location.reload();
        return;
      }
    }
  }

  function poll() {
    fetch("/runs/" + encodeURIComponent(runId), {
      method: "GET",
      headers: { Accept: "application/json" },
      cache: "no-store",
      credentials: "omit"
    })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("poll returned " + response.status);
        }
        return response.json();
      })
      .then(function (body) {
        consecutiveFailures = 0;
        setStale(false);
        apply(body);
      })
      .catch(function () {
        /* Silently continue: a missed poll is not an error the user must act
         * on, and no manual reload is required to recover. */
        consecutiveFailures += 1;
        setStale(consecutiveFailures > 1);
      });
  }

  poll();
  window.setInterval(poll, POLL_INTERVAL_MS);
})();
