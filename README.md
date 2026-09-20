# CivSim — Civilization-Playing Harness

An unattended agentic harness that plays Civilization VI (BBG-balanced ruleset) through
Firaxis's FireTuner scripting interface, under the constraint that the agent sees and does only
what a human player could see and do through the game's standard UI. This is deliverable 2
(`002-civ-playing-harness`) of the CivSim research platform. The full requirement set lives in
[`specs/002-civ-playing-harness/spec.md`](specs/002-civ-playing-harness/spec.md); the governing
rules for the whole project live in
[`.specify/memory/constitution.md`](.specify/memory/constitution.md).

This README covers setup, what a healthy (and an unhealthy) `civsim doctor` run actually looks
like, and which parts of the test suite run without a game client. For the full walkthrough —
starting a run, auditing it, branching, crash recovery, swapping models — see
[`specs/002-civ-playing-harness/quickstart.md`](specs/002-civ-playing-harness/quickstart.md).

## Requirements

- A native **Windows, macOS, or Linux** desktop. Not Proton/Wine — the tuner interface is built
  into the native binary and is not exposed under a compatibility layer, so a Linux host needs the
  native Aspyr port.
- Civilization VI, with the ruleset and mod set a given run's seed set expects, installed and
  active.
- Python 3.12+ and [`uv`](https://docs.astral.sh/uv/). **`uv` manages the project end to end —
  there is no supported system-Python install path.** Every command in this README is run through
  `uv run`.
- An OpenRouter API key in the environment (`OPENROUTER_API_KEY`) to actually play a run. Never
  put it in a run configuration file (FR-043) — see
  [Provider credentials](#provider-credentials-never-in-configuration) below.

## Setup

```bash
uv sync --extra windows   # on Windows
uv sync --extra macos     # on macOS
uv sync --extra linux     # on Linux
```

**Linux prerequisite**: `uv sync --extra linux` builds `dbus-python` from source, which needs the
D-Bus development headers already present on the system — install them first, or the build fails
with a meson error that never actually names the missing package:

```bash
sudo apt-get install -y libdbus-1-dev
```

`uv sync` alone installs the portable core only. The per-OS extra pulls in that platform's host
adapter dependencies (`pywin32`/`winsdk` on Windows, `pyobjc-framework-*` on macOS,
`python-xlib`/`dbus-python` on Linux) — each also carries a `sys_platform` marker as a second line
of defence, so requesting the wrong extra on the wrong host still won't install an unusable wheel.
A macOS or Linux install never pulls `pywin32`; a Windows install never pulls `pyobjc` or `Xlib`.
This split exists because the portable core (catalog loading, the run loop, the store, the
provider adapter, the parity filter) must run unchanged on every target platform — only
`src/civsim_harness/host/` is allowed to import a platform-specific library at all, and `ruff`
enforces that boundary on every build (see `pyproject.toml`'s
`flake8-tidy-imports.banned-api` section).

```bash
uv run civsim doctor
```

Run this before anything else. It must report all green before a real run is worth attempting.

### Enabling the tuner

The tuner is off by default and the harness cannot talk to a client that hasn't enabled it.

| Platform | How |
|---|---|
| Windows | In-game **Options → Tuner (disables achievements)** |
| macOS | Set `EnableTuner 1` in `~/Library/Application Support/Sid Meier's Civilization VI/Firaxis Games/Sid Meier's Civilization VI/AppOptions.txt` — not exposed in the menu |
| Linux | Set `EnableTuner 1` in `~/.local/share/aspyr-media/Sid Meier's Civilization VI/AppOptions.txt` — not exposed in the menu |

On macOS and Linux this means hand-editing `AppOptions.txt`. **Edit it with the game closed.** The
client rewrites this file on exit, so an edit made while the game is still running is silently
overwritten the moment you quit it. Restart the client after editing; it then listens on TCP
`127.0.0.1:4318`. The Windows-only FireTuner SDK/Development Tools are **not** required — the
harness speaks the wire protocol directly and never opens the FireTuner GUI itself.

## `civsim doctor`: what it actually reports

`doctor` probes the environment rather than trusting configuration or documentation, and it is
designed to report honestly on a machine with **no game installed at all** rather than crash. Here
is a real run, taken on a Windows development machine with no Civ VI client running and no
provider key set:

```text
platform          : ok  (windows 10.0.26200, tier UNSUPPORTED)
tuner connection  : unreachable  (Could not connect to the Nexus tuner interface)
client            : not running
store             : ok
catalog           : ok  (version 2026.09.1, 51 declarations, 0 undeclared)
capture path      : none (runs will be visually degraded)
provider key      : MISSING
disk headroom     : 899.1 GB free
```

Nothing here is an exception or a stack trace — every line is a check that ran and reported its
own result, even the ones that failed. That is deliberate: an operator (or CI) needs to be able to
run `doctor` on a bare checkout and get a diagnosis, not a traceback. Compare this to the fully
healthy example from `quickstart.md`, taken against a live client:

```text
platform          : ok  (macos 15.3, tier VALIDATED)
tuner connection  : ok  (GameCore_Tuner=2, InGame=5)
client            : ok  (pid 18244, build mac/1.0.12.9)
store             : ok
catalog           : ok  (version 2026.09.1, 215 declarations, 0 undeclared)
capture path      : ok  (screencapturekit, hygiene spike PASSED)
provider key      : present
disk headroom     : 41.2 GB free
```

A few lines are worth more than a glance:

- **`catalog: N undeclared`** — a non-zero count means a capability exists without a parity
  declaration, and no run may start (FR-023). See
  [`docs/catalog-authoring.md`](docs/catalog-authoring.md) for how declarations get added.
- **`capture path`** — `none (runs will be visually degraded)` is a legitimate operating state, not
  a blocker, until the capture-hygiene spike (research R6) has passed on that host. What it must
  never read is a capture path in use *without* a passing spike behind it.
- **`client: build ...`** — a composite of platform and version, checked against a seed set's
  pinned build at every run's preflight. A differing build *or* platform fails the run before
  turn 1 unless an operator has explicitly accepted that exact transition for the set.
- **`platform: tier ...`** — see [Platform support tiers](#platform-support-tiers) below.

## Platform support tiers

Platform support in this harness is a **tier**, not a pass/fail boolean, and the tier is resolved
by probing the host — never trusted from documentation or configuration:

| Tier | Meaning |
|---|---|
| `VALIDATED` | Full capability, with a passing capture-hygiene spike on record for this host |
| `SUPPORTED` | The harness will run and record honestly, but visually degraded — no verified screened-capture path yet |
| `UNSUPPORTED` | Preflight refuses to start a run — no verified quicksave path exists, and a turn without its quicksave is not permitted (FR-007) |

**A platform is `UNSUPPORTED` until it has actually been probed.** This is why the Windows sample
above reads `tier UNSUPPORTED` even though Windows is a fully in-scope target platform for this
project — that specific machine simply hasn't had its capture-hygiene spike run against a live
client yet. `UNSUPPORTED` is a statement about verification state on *this host*, not a judgment
about the platform in general, and it is expected to change to `VALIDATED` once that host's spike
passes.

## Provider credentials: never in configuration

`OPENROUTER_API_KEY` is read from the environment (or a secrets file) at call time. It must never
appear in a `RunConfiguration`, and the harness is built so it structurally cannot leak into a
request record, log line, error message, or exception trace (FR-043) — `civsim audit secrets
<run_id>` is the record-side check for this once a run has completed.

## Testing

```bash
uv run pytest tests/unit tests/contract tests/integration
```

**This is the CI-runnable subset**, and it is what actually gates every build, on all three
platforms as a matrix (`.github/workflows/ci.yml`). It covers: Nexus codec round-trips and
sentinel correlation; catalog load-time validation (including that a missing `parity_basis` and an
undocumented bespoke path both fail the load); the parity red-team suite; `MatchStore`,
`ModelProvider`, and `HostPlatform` port conformance against fakes; the full turn cycle against a
recorded-transcript fake game, fake provider, and fake host; run and turn state machines;
no-progress accounting; retention eligibility; platform and build pin comparison; and credential
redaction. None of it needs a running Civ VI client.

```bash
uv run pytest tests/live -m live
```

**`tests/live` is excluded from the default run** (`pyproject.toml`'s `addopts =
"--ignore=tests/live"`) and needs a real, running Civilization VI client with the tuner enabled.
It is marked `live` rather than deleted from discovery specifically so this command still works
when you do have a client to point it at. A pass on one host says nothing about another — the
live tier is meant to be run per platform, not once.

```bash
uv run ruff check .
uv run mypy src
```

`ruff` also enforces the portable-core/host-adapter import boundary described under
[Setup](#setup) above; a change that imports `pywin32` (or any other OS-specific library) outside
`src/civsim_harness/host/` fails this check regardless of which platform runs it.

## Project layout

- `src/civsim_harness/` — the harness itself. `host/{windows,macos,linux}/` is the only directory
  permitted to import a platform-specific library; everything else is portable.
- `catalogs/` — the capability catalog: every observation, view, and action the agent may use, each
  with a parity declaration. See [`docs/catalog-authoring.md`](docs/catalog-authoring.md) before
  adding to it.
- `lua/` — the FireTuner Lua bodies the catalog dispatches into (`gamecore/` read-only,
  `ingame/` action-capable).
- `tests/{unit,contract,integration,fakes,live}/` — the four test tiers described above.
- `specs/002-civ-playing-harness/` — the full spec, plan, data model, contracts, research notes,
  and validation results for this deliverable.
- `src/civsim_web/` — deliverable 1, the read-only web interface (`civsim-web`). Imports nothing
  from `civsim_harness`; reads the match store through a port and nothing else.
- `panels/` — the Panel Registry the web interface renders through, versioned and frozen by
  `panels/VERSION.lock`. See [`civsim-web`](#civsim-web-the-unified-web-interface) above.

## `civsim-web`: the unified web interface

Deliverable 1 (`001-unified-web-interface`) ships alongside the harness in this repository as a
second console script, `civsim-web`. It is a **read-only** view of run data: it never talks to the
game client, never writes to the match store, and offers no control of any kind — starting,
stopping, or intervening in a run is `civsim`'s job and stays there. Its whole purpose is
Principle VI: the user and the directing Claude Code session see the *same* run state, from the
same URLs, so neither has to read the other's screen.

```bash
uv run civsim-web doctor      # preflight: store, panel registry, coverage, routes, bind addresses
uv run civsim-web serve       # start the service; logs every bound address
```

`doctor` must report all green before `serve` is worth running:

```text
store             : ok (ping succeeded)
panel registry    : ok (version 1, 37 panels, frozen)
registry coverage : ok (167 fields scanned, 91 marked out-of-game, 27 registered, 49 unregistered and unrendered, 0 unregistered fields reachable from a view model)
routes            : ok (13 registered, all 13 contract routes present)
bind address(es)  : 192.168.1.42:8420, 127.0.0.1:8420   (LAN + loopback — no wildcard)
```

As with `civsim doctor`, every line is a check that ran — a failure is reported, not raised, and
the exit code is non-zero so it works as a CI gate. Three of these lines are worth more than a
glance:

- **`panel registry: ... frozen`** — the Panel Registry (`panels/*.yaml`) is this feature's own
  parity gate, the second one behind the harness's capability catalog: a store field with no panel
  declared over it is never read by any view model, so a field 002 fails to filter still cannot
  reach a screen here. `frozen` means `panels/VERSION.lock` records the current version's
  declaration hashes, so a declaration cannot be edited in place — changing one means bumping
  `panels/VERSION`. `NOT FROZEN` is legitimate only while a registry version is still being
  authored, and says so.
- **`registry coverage: ... 0 unregistered fields reachable`** — computed, not asserted. It walks
  002's `data-model.md` and this feature's view models and counts fields that could render with no
  panel permitting them. Anything but `0` fails the preflight.
- **`bind address(es)`** — resolved by enumerating the host's own interfaces and keeping the
  RFC1918 private ranges plus loopback. **A wildcard (`0.0.0.0`) bind is refused outright**, not
  merely discouraged: this service is unauthenticated by design (it is a LAN tool on the machine
  running the game), so the bind address is the entire access boundary. Override with `--bind`
  (repeatable) and `--port`; the default port is `8420`.

```bash
uv run civsim-web --store fake serve          # the bundled read-only fake; no harness needed
uv run civsim-web --store mypkg:make_store serve
```

Which store to read is **configuration, not code** (`--store`, or `CIVSIM_WEB_STORE`): `fake` for
the bundled in-memory store, or `module:attribute` resolving to anything satisfying the read-only
`MatchStore` protocol. Every route and view model is written against that protocol, so pointing
this at a real store should never require editing `viewmodels/` or `routes/` — if it does, that is
a finding, not a configuration problem.

This feature's tests run in the same CI-runnable subset as the harness's
(`uv run pytest tests/unit tests/contract tests/integration`) and need no game client and no
running harness — they all run against the fake. Its own walkthrough, including what each route is
for, is [`specs/001-unified-web-interface/quickstart.md`](specs/001-unified-web-interface/quickstart.md).

## Further reading

- [`specs/001-unified-web-interface/spec.md`](specs/001-unified-web-interface/spec.md) and
  [`quickstart.md`](specs/001-unified-web-interface/quickstart.md) — the web interface's
  requirements and its scenario walkthrough.
- [`specs/002-civ-playing-harness/spec.md`](specs/002-civ-playing-harness/spec.md) — requirements
  and success criteria.
- [`specs/002-civ-playing-harness/quickstart.md`](specs/002-civ-playing-harness/quickstart.md) —
  the full scenario walkthrough, including everything that needs a live client.
- [`specs/002-civ-playing-harness/validation-results.md`](specs/002-civ-playing-harness/validation-results.md)
  — recorded validation outcomes, live-client findings, and the constitutional compliance audit.
- [`docs/catalog-authoring.md`](docs/catalog-authoring.md) — how to add a catalog declaration.
- [`.specify/memory/constitution.md`](.specify/memory/constitution.md) — the project's governing
  principles.
