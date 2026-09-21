# Contract: Web Read API

**Feature**: `001-unified-web-interface` | **Schema version**: 1 | **Amended**: 2026-09-20
(see [Amendment: collection responses](#amendment-2026-09-20--collection-responses-are-wrappers-not-bare-lists))

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
| `GET /` | **One active run**: `307` to `/runs/{run_id}`, preserving the query string. **Any other number**: a `LandingView` — `active_runs: list[RunSummaryView]`, `empty_state_reason`, `last_confirmed_current_at`, and the Panel Registry version — rendered as HTML or JSON like every other route (see the amendment below) | US1 |
| `GET /runs` | `CatalogListingView` — `runs: list[RunSummaryView]` plus the page state (`page`, `page_size`, `total`, `has_more`), the applied `sort`/`order`/`filters`, `sortable_fields`, `quarantined_run_ids`, `listing_is_partial`/`partial_reason`, and provenance. Filterable and sortable by any `RunSummaryView` field via query params (`?civilization=`, `?model=`, `?sort=turn_count`, `?page=`) | US4, FR-018, FR-019 |
| `GET /runs/{run_id}` | `RunDetailView` — the live/glance view for one run (UP-003) | US1, US2 |
| `GET /runs/{run_id}/turns/{turn_number}` | `TurnCycleView` for the authoritative attempt; `?attempt={n}` selects a specific (including abandoned) attempt; `?step_offset=`/`?step_limit=` page the step window (`data-model.md` §5 — the default response carries a **bounded** window, with `step_window` stating `total`, `has_more` and every `skipped_step_indices`); `?focus={panel_id}` carries panel focus across navigation | US2, US3 |
| `GET /runs/{run_id}/turns/{turn_number}/steps/{step_index}` | `DecisionStepView` | US2, US3 |
| `GET /runs/{run_id}/turns/{turn_number}/panels/{panel_id}` | The single named panel's data, scoped to that turn (the canonical `ViewReference` resolution target) | US2 |
| `GET /runs/{run_id}/events` | `RunEventPage` — `events: list[RunEventView]` plus `run_id`, the page state (`page`, `page_size`, `total`, `has_more`), and provenance. The full timeline, paginated | US1, US3 |
| `GET /runs/{run_id}/metrics` | `MetricSeriesPage` — `series: list[MetricSeriesView]` plus `run_id`, the common `axes` each series must be drawn to, `metric_names`, the `requested_series`/`unrecorded_series` split for `?series=science_output,culture_output`, `turn_count`, `gapped_turns`, `trend_eligibility`, and provenance | US3, US4 |
| `GET /captures/{capture_id}/image` | The image bytes, **only if** `CaptureView.available` for that capture; otherwise `404` with a body naming `unavailable_reason` | US1, US3 |
| `GET /compare?runs={id,id,...}&metrics={name,name,...}` | `ComparisonView` | US4 |
| `GET /healthz` | This service's own liveness plus the configured store's `ping()` result — operational, not a run-state route | Operability |

### Amendment 2026-09-20 — collection responses are wrappers, not bare lists

The three rows above that return a collection previously read `list[RunSummaryView]`,
`Paginated list[RunEventView]`, and `list[MetricSeriesView]`. All three shipped as wrapper objects
instead, and each was recorded as a local deviation by the story that built it (US1 note 4, US4
note 5, US3 note 1). **Three routes differing from the same table in the same way is a defect in the
table, not three independent deviations**, so the table is amended to say what these routes must
actually return.

A bare list cannot carry:

- **the pagination the same table asked for in prose.** `GET /runs` is specified as paginated and
  `GET /runs/{id}/events` literally says "Paginated"; a JSON array has nowhere to put `page`,
  `total`, or `has_more`, so a caller could not tell a last page from a truncated one.
- **the provenance stamp `data-model.md` §13 requires.** Invariant V10 requires every top-level
  response to carry the Panel Registry version and the catalog versions it was rendered under, and
  "catalog listings" are named there explicitly. A bare array has no top level to stamp.
- **the interpretation the collection is meaningless without.** `axes` is the clearest case: a metric
  series drawn to axes the client recomputed is a chart that can disagree with the response that
  produced it, so the axes belong in the same body as the points.
- **the honest statement of what the store could not do.** `listing_is_partial` says out loud that
  the published port could only enumerate *active* runs. Dropping it would leave a truncated catalog
  looking exactly like a complete one — which is the one failure here capable of quietly shrinking
  the population a trend is drawn from.

Only these three routes changed shape. Every single-object route still returns its view model
directly, and this amendment does not introduce an envelope convention: a wrapper exists where a
collection genuinely needs one, and nowhere else.

### Amendment 2026-09-21 — `GET /` and `LandingView`

This section previously read: *"`GET /` is the one route with no `Accept: application/json`
equivalent of its own — it is a redirect to whichever `/runs/{run_id}` is currently active (or to
`/runs` if none is)."* That was never what shipped, and the route table above disagreed with it in a
second way by promising a chooser the prose denied.

What the route actually does, and what both statements were reaching for:

- **Exactly one active run** → `307` to `/runs/{run_id}`, carrying the query string through so an
  explicit `?format=json` survives the hop. This much the old prose had right, and the reasoning
  stands: "whichever run happens to be live right now" is not a stable machine-consumable target, so
  a machine caller wanting the catalog should ask `/runs` for it.
- **Several active runs** → a `LandingView` listing them. Redirecting would pick one silently, and
  the spec's Edge Cases require that "the user can tell which run they are looking at at all times"
  and that "the live view never mixes turns from different runs".
- **No active run** → the same `LandingView`, carrying `empty_state_reason`. Redirecting to `/runs`
  was the old prose's answer and is the wrong one: the spec's Edge Cases ask for "an explanatory
  empty state, not an error or a blank screen", and an empty catalog table explains nothing about
  *why* there is nothing to watch.

So `GET /` is **not** an exception to content negotiation. In the two cases that render, it is
negotiated like every other route, which is why it sits in the JSON/HTML parity matrix alongside
them. The one thing it does not have is a stable response *shape* across all three cases — a caller
that must not follow a redirect should ask `/runs`.

`LandingView` is the response model for the two rendering cases. It carries `active_runs`,
`empty_state_reason`, `last_confirmed_current_at` (FR-002) and the Panel Registry version; each
`RunSummaryView` in `active_runs` carries its own run's catalog versions, which is how invariant V10
is satisfied without a second provenance stamp.

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
| Step-level panel | `/runs/{run_id}/turns/{turn_number}/steps/{step_index}/panels/{panel_id}` |

*(Amended 2026-09-20.)* The step-level panel shape was described in `data-model.md` §12 and appeared
in neither table here. It is not optional: **twenty-two of the registry's thirty-seven shipped panels
are `scope: step`**, so without this row a `ViewReference` to a step-scoped panel — the majority of
them —
has no resolvable URL, which would break FR-008 for exactly the panels that carry the agent's own
observations. The route exists; the table now says so.

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
| `?step_offset=` starts past the last step of a turn that has steps | `404 step_window_out_of_range`, naming the turn's step count — **not** an empty page. A page of no steps for a turn that has steps is indistinguishable from a turn whose steps were never recorded, and `data-model.md` §5 spends its whole Validation clause on those two never being confusable. `?step_offset=0` on a turn with genuinely no recorded steps is *not* this error |
| `?series=` names a metric this run recorded no values under | `200`, with the name listed in `unrecorded_series` — **not** a `400`. spec Assumptions make the metric set open-ended, so a name this run has nothing under is a fact about the run rather than a client error. Deliberately unlike `GET /runs`'s unknown *filter field*, which is a `400`: there the field set is closed (whatever `RunSummaryView` carries), so a name outside it is provably a mistake. The alternative — drawing an empty chart — would read as a score of zero |

*(The last two rows added 2026-09-20.)* Both conventions were decided at implementation time and
recorded only in the code; a machine caller cannot be expected to infer from a route's silence that
one unknown name is a `400` and another is a `200`.

## Conformance tests

`tests/contract/test_web_read_api.py` (this feature's analogue of 002's port-conformance suite) runs
against the `MatchStore` fake (research R3) and asserts: JSON/HTML field-parity per route; the
`CaptureView.available` fail-closed rule (data-model V2) via a matrix of every `screening_status`
value including an intentionally-unrecognized one; that no `ComparisonView` response contains an image
byte or capture reference anywhere in its JSON; that a superseded-turn reference never silently
resolves to the current authoritative turn; and that the credential-scan audit above finds zero
matches across every route's response schema.
