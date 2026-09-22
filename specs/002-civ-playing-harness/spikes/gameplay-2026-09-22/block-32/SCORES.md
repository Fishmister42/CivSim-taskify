# Block 32 — predictions scored

Scored 2026-09-22 ~15:55 EDT against two launches from `/home/matt/CivSolver-live` at
`0e91a10` (verified `git rev-parse HEAD` in the worktree, not by commit message):

- **block-32** — `run-fd895f91c5df43baa80394a72de401eb`, launched 15:25:06, stopped by me 15:33:39.
- **block-33** — `run-10253c8e6e8846f0a50230deff42af01`, launched 15:36:06, stopped by me 15:37.
  Same goal, same flags; a re-attempt of block 32, kept as its own directory.

Both: `--goal build_a_builder --provider stochastic --provider-policy uniform --turns 6`.

## All three predictions are UNEVALUABLE. None is confirmed and none is falsified.

Neither run completed a single decision step. Both died **1.4 seconds in**, on the *first*
observation read, before any turn, any capture, any model call and any prompt.

```
store, run-fd895f91c5df43baa80394a72de401eb: turn_cycles 0, captures 0, model_calls 0
store, run-10253c8e6e8846f0a50230deff42af01: turn_cycles 0, captures 0, model_calls 0
```

| | prediction | score | why |
|---|---|---|---|
| P1 | reaches turn cap, `parts` populated | **unevaluable** | the cap was never approached; turn 1 never completed |
| P2 | captures withheld, `image_count` 0 | **unevaluable on this head** | zero captures were produced to check |
| P3 | prompts still apply at `scale [1.875, 1.5625]` | **unevaluable** | no prompt was reached |

## Why turn 1 does not complete: BLOCKED, not "work that was not durable"

The harness attempted **zero** decision steps. The exception is raised at
`decision_loop.py:684`, `initial = await _observe_or_wrap(...)` — the read that happens *before*
the first decision request exists — and terminates in `nexus/codec.py:137`. Durability cannot
explain an empty store here, because nothing was ever produced to lose. `926fca2`'s absence from
this head is real and is not the cause.

**And zero captures is not "the gate withheld them".** `_observe` (`decision_loop.py:493`) calls
`read_observation_inputs()` as its *first* statement; `capture_for_step` is called ~30 lines
later. The read raised, so the capture was never attempted and the screening gate never ran.
Absence and unobservability, separated by line number rather than by argument.

## The blocker: a great person's name kills the tuner connection

```
UnicodeDecodeError: 'utf-8' codec can't decode byte 0xc4 in position 1663
  at src/civsim_harness/nexus/codec.py:137 -> NexusFrame(payload=body[:-1].decode("utf-8"))
```

Byte-identical across **three** occurrences: block-31 (at the previous head `c211605`,
15:09:33), block-32 (15:25:08), block-33 (15:36:08). Same byte, same offset, every time.

**Identified by direct measurement, not inference.** `tests.live.probe_observation_bodies` was
run against the live client with the frame decoder made lenient *in the probe process only*
(runtime monkeypatch; no file in the worktree was modified). All 16 observation declarations
then answered with schema-valid values, and exactly one frame carried the bad byte:

```
tag=-1 length=1899 bad_at=1663 byte=0xc4    <- great_people.state
'...,"name":"Kam\xc4\u0081l ud-D\xc4\xabn Behz\xc4\u0081d","individual_id":191,...'
```

The recruitable Great Artist is **Kamāl ud-Dīn Behzād**. `ā` is U+0101 = UTF-8 `C4 81`.
The lead byte `C4` is emitted raw and the trailing byte `81` is emitted as the six literal
characters `\u0081`, splitting the sequence and leaving a lone lead byte.

**Root cause, in our own Lua, with its own positive and negative control inside one string:**

```lua
-- lua/ingame/great_people.lua:31-37 and 26 other copies
local escaped = value:gsub('[%c"\\]', function(c)
    ...
    else return string.format('\\u%04x', string.byte(c)) end
end)
```

Lua patterns match **bytes**, and `%c` is `iscntrl()` under the client's locale, which includes
the C1 range `0x80-0x9F`. So:

- `ā` = `C4 81` — the trailing byte `0x81` **is** C1, gets escaped, sequence split. **Breaks.**
- `ī` = `C4 AB` — the trailing byte `0xAB` is **not** C1, passes through. **Valid, survives.**

Both characters are in the same name, in the same frame. That pair is the control: the escaper
mangles exactly the bytes in `0x80-0x9F` and nothing else.

**Scope: 27 Lua bodies carry a copy of this escaper** (`lua/ingame/*.lua`, `lua/gamecore/*.lua`),
identical in the shared tree at `live/linux`/`4925fdb`. Any game string whose UTF-8 encoding
contains a byte in `0x80-0x9F` — most of Latin Extended-A, so a large share of leader, city,
city-state and great-person names — kills the tuner connection and therefore the run.

**No configuration workaround exists.** The generated `run-config.yaml` has no observation-set
selector; `great_people.state` is read on every observation cycle.

**This is not attributable to the advance.** `nexus/codec.py` is unchanged between `c211605`
and `0e91a10`, and block-31 hit the identical exception while running at `c211605`. What I
cannot establish without moving the worktree (which I was told not to do) is whether the advance
changed *when* it fires — block 31 played two turns first, block 32 and 33 died on read one.
Stated as unverified, not as a finding.

## 🛑 P2's PREMISE IS FALSE AT THIS HEAD, and the error points toward delivery

P2 assumes `483017c` closes image delivery because the production call site supplies no text
evidence. **`0e91a10` also contains the T265 plumbing that supplies it**, at
`run/decision_loop.py:513`:

```python
detected_text_tokens = ctx.host.list_window_titles().text_tokens()
step_capture = capture_for_step(..., detected_text_tokens=detected_text_tokens, ...)
```

Measured on this box, at this head, by running the production code (`get_host_platform()` →
`LinuxHostPlatform.list_window_titles()`):

```
listing.available = True
reason  = 'enumerated 14 top-level window(s) via _NET_CLIENT_LIST on X11; 14 carried a readable _NET_WM_NAME/WM_NAME'
text_tokens() is None ? False        <- 34 tokens; the declared-text technique IS available
```

Every view in `catalogs/observations/views.yaml` declares `screening_profile: platform`, which on
Linux resolves to the Linux profile. Asking `unaddressed_reject_categories` directly:

```
profile linux/platform, WITH text evidence (this head, live desktop)
  reject                : ['debug_overlay','developer_console','firetuner_window',
                           'harness_owned_ui','linux_notification_toast','linux_panel']
  structurally excluded : ['firetuner_window','harness_owned_ui','linux_notification_toast','linux_panel']
  unaddressed           : []          => coverage SATISFIED -> a clean frame CAN be cleared
```

(For contrast, the same call with no text evidence leaves `['developer_console']` unaddressed and
withholds; and the `default` profile withholds either way. The Linux exclusions are declared in
`screening_profiles.yaml` on the ground that the X11 path reads the game window's own off-screen
backing store, so another application's window structurally cannot be in the frame.)

**And the sibling HIGH is closed too.** `observe/capture.py:309` now reads
`expected_process if expected_process is not None else host.locate_game_process()` — the source
gate's process-identity check runs on a real capture at this head. Both halves of loop-state's
"an optional parameter the single production call site never supplies" finding are closed here.

**So the standing order's stated mechanism — "the gate fails closed and images do not reach the
agent" — does not hold at `0e91a10` on this Linux box.** The content gate no longer withholds on
coverage grounds.

**What I have NOT established, and it is the whole remaining question:** whether a real frame
would then pass the provenance, geometry and image-statistic gates and actually be delivered.
Block 31's 18 frames all failed `provenance_failure` at the *previous* head, but `act/camera.py`,
`lua/ingame/camera.lua` and `views.yaml`'s new `hud_corners` all changed in this advance. I could
not test it because the decode blocker produced zero captures. **Treat image delivery as
plausibly OPEN until someone gets one capture at this head and reads its record.**

## A defect in P2 itself, independent of the blocker

P2 asks for `shown_to_agent: false` + a `withheld_reason` + `image_count: 0`. Block 31, at
`c211605` — a head that does **not** contain `483017c` (`git merge-base --is-ancestor 483017c
c211605` → exit 1) — already satisfies all three:

```
block-31 captures: 18 | shown_to_agent {False: 18} | screening_status {withheld: 18}
                      | withheld_reason {provenance_failure: 18}
block-31 model_calls: 7, sum(image_count) = 0
```

Those frames were withheld by the **provenance** gate, not by the content-screening coverage gate
that `483017c` fixed. So P2 as written **cannot discriminate** between "the fail-closed gate
works" and "the provenance gate was withholding everything anyway" — it has no positive control
on the dimension it claims to constrain. A discriminating form has to assert on the *reason
value*, not merely on its presence.

## What did land, and is worth keeping

- **`0e91a10`'s `reached_reason` machinery fired on the live path.** Both blocks published
  `"reached": false, "evaluated": false, "reached_reason": "no_parts_recorded"`, where block-31
  at `c211605` published a bare `"reached": false` beside `"parts": []`. Absence has stopped
  impersonating a negative. This is **not** P1 — P1 needs a populated `parts` at the cap — but it
  is a real live confirmation of the other half of that commit.
- **The run lock released itself on both clean stops.** `/tmp/civsim_harness/run_locks/` was
  empty after each SIGINT teardown. Recorded verbatim before each stop regardless:
  `{"run_id": "run-fd895f91c5df43baa80394a72de401eb", "client_pid": 2459457, "acquired_at": "2026-09-22T19:25:07.683693+00:00"}`
  `{"run_id": "run-10253c8e6e8846f0a50230deff42af01", "client_pid": 2459457, "acquired_at": "2026-09-22T19:36:07.439370+00:00"}`

## A second defect, found on the way

When the observation read raised, `run/runner.py`'s own error-recording path raised the *same*
`UnicodeDecodeError` ("unhandled failure while recording why a run stopped playing"), so the run
was **left `lifecycle_state: playing` forever** with the driver polling a status that would never
change. Both runs are orphans in the store to this minute. Without a manual stop each would have
run to its outer `timeout 1800` — the wall-clock kill the standing rules exist to prevent.

## Retracted, mine, in flight

I inferred from `tests/live/demo_landed_run.py:421` (`"primary": {"provider": "openrouter", ...}`
hard-coded regardless of `--provider`) that this was the mechanism behind the open
provider-misattribution finding. **Wrong.** `model_calls` carries both fields and they are
correct: `model_requested {openrouter, anthropic/claude-sonnet-5}` and
`model_served {stochastic, uniform-v1}`. The store does record that a stub served the call. Only
a consumer reading `model_requested` alone would misattribute.
