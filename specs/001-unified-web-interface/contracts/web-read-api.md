# Contract: Web Read API

**Feature**: `001-unified-web-interface` | **Schema version**: 1

This is the one surface this feature exposes. It is read-only, LAN-bound, and unauthenticated (FR-023,
FR-024, FR-026, FR-028 – FR-030). Every route below is reachable two ways from the same URL: a browser
navigating it gets an HTML page; a machine caller (the directing Claude Code session, `curl`, a test)
gets the identical content as JSON. That equivalence is this contract's whole purpose — it is the
mechanism behind Principle VI (FR-007, UP-002).

## Content negotiation — the single seam

Every route is implemented as: **(1)** build one view model from the store and the Panel Registry,
**(2)** decide HTML or JSON, **(3)** render. Step (2) is the only branch:

- `Accept: application/json` (or a bare fetch/XHR with no `Accept: text/html`) → the view model's JSON
  serialization, verbatim.
- Anything else, or an explicit `?format=json` override for a browser tab a human wants to inspect
  raw → the corresponding Jinja2 template, given the same view model as its context.

**There is no route that computes the HTML response differently from the JSON one.** A field present
in one is present in the other; this is enforced by a contract test that asserts, per route, that the
HTML template's rendered DOM cites every field the JSON body contains (modulo formatting) and no
field the JSON body lacks.

## Routes

| Route | Returns | Serves |
|---|---|---|
| `GET /` | Landing view: the single active run (or a chooser, if several are active — spec edge case "several runs are recorded concurrently") | US1 |
| `GET /runs` | `list[RunSummaryView]`, paginated, filterable and sortable by any `RunSummaryView` field via query params (`?civilization=`, `?model=`, `?sort=turn_count`, `?page=`) | US4, FR-018, FR-019 |
| `GET /runs/{run_id}` | `RunDetailView` — the live/glance view for one run (UP-003) | US1, US2 |
| `GET /runs/{run_id}/turns/{turn_number}` | `TurnCycleView` for the authoritative attempt; `?attempt={n}` selects a specific (including abandoned) attempt | US2, US3 |
| `GET /runs/{run_id}/turns/{turn_number}/steps/{step_index}` | `DecisionStepView` | US2, US3 |
| `GET /runs/{run_id}/turns/{turn_number}/panels/{panel_id}` | The single named panel's data, scoped to that turn (the canonical `ViewReference` resolution target) | US2 |
| `GET /runs/{run_id}/events` | Paginated `list[RunEventView]` — the full timeline | US1, US3 |
| `GET /runs/{run_id}/metrics` | `list[MetricSeriesView]`, `?series=science_output,culture_output` to narrow | US3, US4 |
| `GET /captures/{capture_id}/image` | The image bytes, **only if** `CaptureView.available` for that capture; otherwise `404` with a body naming `unavailable_reason` | US1, US3 |
| `GET /compare?runs={id,id,...}&metrics={name,name,...}` | `ComparisonView` | US4 |
| `GET /healthz` | This service's own liveness plus the configured store's `ping()` result — operational, not a run-state route | Operability |

**`GET /` is the one route with no `Accept: application/json` equivalent of its own** — it is a
redirect to whichever `/runs/{run_id}` is currently active (or to `/runs` if none is), and machine
callers are expected to hit `/runs` directly rather than depend on "whichever run happens to be live
right now" as a stable machine-consumable target.

## View-reference resolution

A `ViewReference` (`data-model.md` §12) *is* one of the path shapes above. Resolving a reference means
issuing the corresponding `GET`; there is no separate resolver endpoint or opaque token to look up.
This is deliberate: FR-008 requires both parties to resolve a reference to identical content, and the
simplest way to guarantee that is for the reference to be the very thing the server already knows how
to serve, rather than an indirection layer that could itself drift or expire.

| Reference shape | Path |
|---|---|
| Run | `/runs/{run_id}` |
| Turn | `/runs/{run_id}/turns/{turn_number}` |
| Step | `/runs/{run_id}/turns/{turn_number}/steps/{step_index}` |
| Panel | `/runs/{run_id}/turns/{turn_number}/panels/{panel_id}` |
| Run-level panel (e.g. intervention info, catalog row) | `/runs/{run_id}/panels/{panel_id}` |

**FR-009 — superseded turns**: `GET /runs/{run_id}/turns/{turn_number}` with no `?attempt=` always
resolves to the current authoritative attempt. If the *specific* attempt a reference names (via
`?attempt=`) has since been superseded, the response still returns that attempt's `TurnCycleView`
(`is_authoritative = false`, `superseded_by` set) rather than silently substituting the new
authoritative one — a reference someone shared five minutes ago must still explain itself, not
quietly point somewhere else.

## Access (FR-028 – FR-030)

- No authentication on any route. No session, no cookie, no login page.
- Bound only to detected private-range interface addresses at startup (research R8) — never a
  wildcard bind — and the bound address list is logged at process start.
- No route ever returns a value sourced from `RunConfiguration.model_config`'s credential material,
  environment variables, or any secrets file. This is asserted by a contract test that scans every
  response schema for field names/value shapes matching common credential patterns (mirroring 002's
  own FR-043 audit, applied at this feature's own boundary per FR-030).

## Error responses

| Condition | Result |
|---|---|
| `run_id` never existed | `404`, generic "no such run" |
| `turn_number` never existed for a run that did | `404` naming the run's actual turn range, distinct from a superseded-turn response (see above) |
| `panel_id` not in the Panel Registry | `404` — this is a client-side reference error, not a "field unavailable" case, since an unregistered panel is not a valid reference at all |
| Store unreachable (`ping()` fails) | `503` on every route except `/healthz`, with a body distinguishing "store unreachable" from "run not found" so a client does not confuse the two |
| Capture requested that is not `screened_clean` | `404` on `/captures/{id}/image`, naming `unavailable_reason`; never a 200 with a placeholder image standing in for the real one, which could be mistaken for content |
| A route's underlying record predates a field in the current schema version | The field renders `null`/absent with an explicit "unavailable for this run's recorded schema version" marker, never `200` with a silently zeroed value (FR-025) |

## Conformance tests

`tests/contract/test_web_read_api.py` (this feature's analogue of 002's port-conformance suite) runs
against the `MatchStore` fake (research R3) and asserts: JSON/HTML field-parity per route; the
`CaptureView.available` fail-closed rule (data-model V2) via a matrix of every `screening_status`
value including an intentionally-unrecognized one; that no `ComparisonView` response contains an image
byte or capture reference anywhere in its JSON; that a superseded-turn reference never silently
resolves to the current authoritative turn; and that the credential-scan audit above finds zero
matches across every route's response schema.
