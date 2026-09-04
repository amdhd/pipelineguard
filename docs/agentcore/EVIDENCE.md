# Phase 1–2 — evidence record

Phase 1's exit criteria are quantitative. Until each row below carries a
measurement, the agent's output is **plausible, not demonstrated** — and that
distinction is load-bearing rather than pedantic, because everything downstream
inherits it:

- **Phase 2** patches whatever Phase 1 reports. A false positive becomes a patch
  to code that was never broken.
- **Phase 3** decides convergence by comparing finding sets between rounds. If
  the finding set is noisy, the shrink/stall signal is noise too, and the loop
  either stops early or runs rounds it should not have.

So this file exists to be *filled in*, and to make an unfilled row visible
rather than absent. `agents/qa/harness/score.py` computes the two rates; this
document records the protocol and the results.

---

## Status

First measurements taken **2026-08-27**, against agent runtime **version 7**
(the first build carrying prompt caching, the salvage path, derived budgets and
the CDP idle-socket fix).

| # | Exit criterion | Result |
|---|---|---|
| 1 | Comment carries token cost, session seconds and runner minutes | ⚠️ **PARTIAL (first measurement, 2026-08-27)** → **RESOLVED (P1.2, 2026-09-01)** — cost and seconds were present; runner minutes were absent and inference rendered `unpriced` only because the workflow did not yet pass `--runner-minutes` and the price table was unmerged. Both closed by P1.2 (AUDIT.md). |
| 2 | Stack up from a cold cache in under ~5 min | ✅ **PASS** — 2m47s and 2m30s end-to-end, including the agent |
| 3 | Health gate refuses to invoke when `seed` is broken | ✅ **PASS** — verified by negative test, agent step skipped |
| 4 | False-positive rate | ✅ **BEST MEASURED — 0 findings on healthy `main` (twelfth pass, 33535141706); the merged-pass Leg B (33529796841) produced only the known `/voyage` savings fixture FP, labelled false positive.** The carve-out that reopened the baseline is withdrawn; the candidate layer + coverage rule measure clean on the FP side — see the twelfth pass |
| 5 | False-negative rate against seeded bugs | ⚠️ **6/9 (twelfth pass, N=3)** — S-1 3/3, S-2 3/3, S-3 0/3. S-3 is **0/7** across all candidate-layer passes: the `status_case_leak` detector is correct but its signal renders only on the `/sire` Findings tab, which the agent never materializes even under the mandatory open-every-view rule (PR #52). **P1.1 closed via the written-decision branch** — see the twelfth pass |
| 6 | Two rungs benchmarked | ✅ **MEASURED** — haiku 0/3, sonnet caught the semantic seed; sonnet is now the default, haiku an explicit opt-in |

**The headline is criterion 5, and the candidate layer finally moved it — but not
all the way.** The deterministic candidate layer caught S-2 (which no model-rung or
rubric change had moved across earlier runs) and turned S-3's miss from perceptual
into mechanical-and-unreachable: the `status_case_leak` detector is right, and its
signal sits behind a tab the agent will not open. Twelfth pass: S-3 0/3 again,
recall still 6/9 — see below.

### Runs

| Run | Branch | Turns | Wall | Cache hit | Cost | Outcome |
|---|---|---|---|---|---|---|
| [33075312063](https://github.com/amdhd/vesselAI/actions/runs/33075312063) | `main` | 11 | 39s | 74% | $0.03 | PASS, 0 findings |
| [33076107421](https://github.com/amdhd/vesselAI/actions/runs/33076107421) | `qa-corpus-1` | 15 | 46s | 82% | $0.04 | FAIL, 1 finding (S-1) |

Two things worth recording about cost, because both were wrong beforehand:

- **A run costs ~$0.03, not the ~$0.23 estimated.** The estimate assumed 32
  turns and 390s; the real agent batches tool calls and finishes 8 routes in
  11–15 turns and under 50 seconds.
- **Prompt caching works in production**: 74% and 82% of input served from
  cache. That is measured from `cacheReadInputTokens`, not inferred.

### Why criterion 1 is only partial

Both gaps are mechanical, and neither is the agent's fault:

- `unpriced` — the workflow checks the **harness** out of pipelineguard `main`
  (`PIPELINEGUARD_REF`), and the populated price table had not been merged when
  these runs happened. The *agent* was version 7 and did report cache tokens;
  the old harness simply had nowhere to look them up. Re-runs after merge should
  price cleanly. **A good demonstration of the two-programs split being real:**
  agent and harness were at different versions in the same run.
- runner minutes — the workflow never passes `--runner-minutes`. See the
  workflow review in this directory.

**Both gaps were closed by P1.2 (2026-09-01)** — the workflow now passes a real
`--runner-minutes` and prices from the merged table, so criterion 1 is
**RESOLVED** (AUDIT.md P1.2). The bullets above are the record of the first
measurement, not the current state.

---

## Criterion 3 — the health gate must refuse ✅

**Measured 2026-08-27** — [run 33078550865](https://github.com/amdhd/vesselAI/actions/runs/33078550865), vesselAI PR #97.

The gate is the guard against paying for a browser session to discover the
pipeline's own bug, and a gate that has never been seen to fail is not known to
work. It is also the cheapest criterion here: no agent is invoked, so it costs
runner minutes and nothing else.

**Method.** On a scratch branch, the demo account was seeded under a different
address so the gate's login assertion could not succeed. The seed still **exits
0** deliberately — otherwise `docker compose --wait` fails at *Start the stack*
and the test proves the healthcheck works rather than the gate.

**Result — exactly as specified:**

```
success   Start the stack        <- --wait was satisfied; the stack is UP
success   Open tunnel
failure   Health gate            <- login returned no token — the database is
                                    probably not seeded
skipped   Run the QA agent       <- THE ASSERTION THAT MATTERS
success   Tear down
```

The agent step was **skipped**, so no AgentCore session was paid for. Confirm
that part specifically on any re-test: a gate that fails *and invokes anyway* is
worse than no gate, and the two are indistinguishable in a red job.

**One defect fell out of this test.** The run's PR comment read *"harness did
not produce a report … the cause is in this job's Run the QA agent step, and its
CloudWatch log group"* — a step that never executed and a log group with nothing
in it. The gate did its job and the comment blamed the agent. Recorded as defect
7 in the workflow review and fixed.

## Criteria 4 and 5 — the two rates

They answer different questions from different inputs, and neither substitutes
for the other.

### False positives — from real runs, labelled by a human

Only a human can label these. Deciding whether a finding is a defect or an
intentional design choice is precisely the judgement the agent is imitating, so
a scorer that decided it automatically would be marking its own homework.

1. Run on three real PRs. Keep each `findings.json`.
2. For every finding, write a label into the corpus file:
   - `true-positive` — a real defect
   - `false-positive` — not a defect; the rubric's severity ladder or its
     "report only what you observe" rule failed
   - `by-design` — real behaviour, correctly observed, that the rubric should
     have excluded. **Distinct on purpose:** it means the rubric has drifted
     from the target's own "What's Real vs. Mocked" list, which is a different
     fix from a bad severity call.
3. `python agents/qa/harness/score.py --findings findings.json --corpus corpus.json`

Both labels count against the rate — the reviewer's time went the same way.
Unlabelled findings are reported as unlabelled and excluded, so a partially
reviewed run cannot produce a flattering number.

### False negatives — from seeded bugs

The measurement no amount of watching real PRs will give you: **a run that finds
nothing looks identical whether the app is clean or the agent is blind.**

Seed the corpus below on a branch of the QA target, run the agent against it,
and score. Each bug is drawn from the contract-drift class the target's own
audit found — the class this project exists to catch, and one that mostly does
*not* 404: it renders `NaN`, an empty chart, or an unguarded crash.

| Seed | Where | Break | Observed symptom |
|---|---|---|---|
| **S-1** | `GET /api/voyage/history` | Returns `actual_fuel`; the client reads `actualFuel` | `formatFuel` calls `.toFixed()` on `undefined` → the Voyage History tab throws and hits the error boundary |
| **S-2** | equipment list response | `healthScore` dropped entirely | Blank health score and miscoloured ring on every `/maintenance` card |
| **S-3** | SIRE findings fixture | **[corrected]** The backend's `summary.open` filter compares `=== 'OPEN'` against data that is still lowercase. The finding *data* is untouched. | **NOTHING renders.** `SirePage` never reads `summary` — it recomputes the count itself from the findings list, correctly. See the eighth pass |

Seeded on branch `qa-corpus-1` (vesselAI PR #95), which must never be merged.
The backend stays internally type-consistent in each case; the drift is at the
client boundary, which is how this class of bug actually occurs — and it is why
the seeds still compile, without which the build fails and the agent is never
invoked at all.

Keep them on their own branch, never on `main`, and never more than one seed per
route — two on one page make a miss ambiguous.

A starter corpus file is at `agents/qa/harness/corpus.example.json`.

---

## Criterion 6 — benchmarking the two rungs

The quality rung is now the default, and that is a measured decision: the
discriminator run showed the cheap rung cannot connect a pristine mechanical
signal to a finding — haiku scored zero on a run where two real bugs were
present — and the quiet-blank class this project exists to catch is exactly the
one that needs a model that connects. The question is no longer "does the cheap
rung clear the bar" but "does the quality rung clear it for the price". Haiku
remains an explicit `--model` opt-in for cost-constrained runs.

Run the **same PR** twice, changing only `--model`:

```
global.anthropic.claude-sonnet-4-6                # quality rung, the default
global.anthropic.claude-haiku-4-5-20251001-v1:0   # cheap rung, explicit opt-in
```

Then:

```
python agents/qa/harness/score.py --compare cheap.json quality.json
```

Findings reported by one rung and not the other are where the noise usually
lives; read those first. Record the per-run cost from each PR comment alongside
the rates — the decision is rate-per-dollar, not rate alone, and a rung that
costs 3x for one extra true positive is not obviously the better buy.

---

## Results

### 2026-08-27 — `qa-corpus-1` (PR #95), Haiku 4.5, runtime v7

- Findings: 1 (labelled 1, unlabelled 0)
- **False-positive rate: 0%**
- **False-negative rate: 67%** — S-2 and S-3 missed
- Cost: $0.04 · session 46s · cache hit 82% · 15 turns

**S-1 — DETECTED, and detected well.** The agent navigated to `/voyage`, clicked
into the Voyage History tab, and reported:

> Cannot read properties of undefined (reading 'toFixed') … error boundary
> showing 'Something went wrong'

That is exactly the seeded defect, with the actual runtime error as evidence and
correct steps to reproduce. Its `suspected_source` guess even named the right
bundle. Note it had to *click a tab* to find it — the bug is not visible on the
route's initial render, so this was not a free catch.

**S-2 — MISSED, and this is a fair miss to charge against the agent.** Dropping
`healthScore` leaves every equipment card on `/maintenance` with a blank score
and a miscoloured ring. It is visible without interaction. The agent visited the
route and reported nothing.

**S-3 — MISSED, but the seed was weak and should not count cleanly.** Changing
the finding status to `OPEN` only causes a CSS colour class to stop being
applied; no text changes, nothing renders as `NaN` or `undefined`. That is close
to unobservable through a browser, so this measures the *seed's* design more
than the agent's eyesight. **Redesign S-3** so the mismatch reaches rendered
text — for example have the status label itself fall through to a default — and
re-run before quoting a 3-seed rate.

**What this means, and it is the point of the whole exercise:** the agent
reliably catches *loud* failures — thrown exceptions, error boundaries — and
missed the *quiet* one. That is precisely backwards from what this project needs.
The target's own audit found that most of its breakage "did NOT 404 — it
rendered NaN, blank charts, or crashed the React tree", and the rubric's own
MEDIUM examples are `NaN`, `undefined`, `[object Object]`. The agent is not
being asked to look hard enough at *values*.

**Recommended next change (rubric, not code):** instruct the agent explicitly to
read the rendered numbers on each route and report blank, zero, `NaN` or absent
values where a figure is expected — not only to notice when the page breaks.
Then re-run this corpus and compare. That is a rubric edit, which is exactly the
tuning loop Phase 1's exit criteria anticipated.

**Honest caveat on the false-positive rate.** 0% over a single labelled finding
is not a rate, it is one data point. Criterion 4 needs the three real PRs before
it can be quoted.

---

### 2026-08-27 (second run) — same corpus, revised rubric + pacing, runtime v9

- **False-negative rate: 67% — UNCHANGED.** S-1 caught again; S-2 and S-3 missed again.
- False positives: 0% of 1 labelled finding. The "look harder" instruction did
  **not** cause the false-positive spike it risked, which is the one clearly
  good result here.
- 24 turns · 144s · cache hit 89% · **$0.08** · 83.6s of that wall-clock was
  spent waiting on the request quota, not on the model.
- Both seeded routes **were visited** (`/maintenance` and `/sire` are in
  `routes_visited`). Only `/` was skipped. So these are perception misses, not
  coverage misses.

**The rubric change did not work, and the reason is not the rubric.**

The agent's entire model of a page is `document.body.innerText`, plus console
errors and failed requests (`browser_tools._state`). An absent value leaves **no
textual trace**: `<span>{score}</span>` with `score` undefined renders
`<span></span>` and contributes nothing to `innerText`. There is no gap to
notice, no label sitting next to an empty space — the number is simply not in
the agent's input at all.

So "read the values" asked the agent to read something it cannot see. The
instruction is not wrong; it is unactionable through this tool surface.

S-1 is caught reliably for the mirror-image reason: a thrown `TypeError`
surfaces **twice** in what the agent receives — once as a
`Runtime.exceptionThrown` console error, and once as the error boundary's
visible text. Loud failures are loud *in the agent's input*, which is the only
place loudness counts.

**A correction to the previous entry.** It called S-2 "a fair miss to charge
against the agent". That was wrong. A miss is only fair if the agent could have
perceived the defect, and through `innerText` it could not. S-2 and S-3 are both
weak seeds, for the same underlying reason — their symptom is not textual. S-3
is the more extreme case (a CSS class that stops applying); S-2 is visible to a
human eye on a screenshot but invisible in text.

**This moves the next change from the prompt to the TOOL.** Options, cheapest
first:

1. **A structural read.** Add a tool (or extend `read_page`) that returns
   label/value pairs harvested from the DOM — `aria-label`, `<dt>/<dd>`, table
   headers with cells, elements whose text is empty but whose siblings suggest a
   figure belongs. A slot with no value then *exists* in the agent's input and
   becomes reportable. This is the change that would actually move the number.
2. **Screenshot-and-look on every route.** Rejected by PLAN.md 1d on cost —
   screenshots are the single largest contributor to input tokens — and it would
   be a large regression in run cost for a narrow gain.
3. **Accept the limitation and say so in the rubric**, so nobody re-tunes a
   prompt against a wall.

Take (1). Until then the honest claim about this agent is narrower than the
project has been assuming: **it detects failures that reach the text layer —
exceptions, error boundaries, and values that render as literal `NaN`,
`undefined` or `[object Object]` — and is blind to a value that is simply
absent.** That is worth writing on the tin, because it is exactly the class the
target's own audit says dominates.

### Operational findings from taking this measurement

Two, both of which cost runs before they were understood:

- **Bedrock's request quota binds, not its token quota.** Both rungs are capped
  at **10 requests per minute** here, against 5,000,000 tokens per minute. One
  Converse call per turn at ~3.5s per turn is ~17 RPM, so the agent breaches the
  request quota while using a fraction of a percent of the token quota. The
  agent now paces itself; `paced_seconds` reports the cost of that, because time
  on a quota is otherwise indistinguishable from a slow agent and the two want
  opposite responses.
- **A slow run used to amplify itself.** With botocore's default retry
  behaviour on `InvokeAgentRuntime`, a run that outlived the read timeout was
  retried — starting a second and third agent **while the first was still
  running**. Run 33081262493's log shows three invocations under one
  `sessionId`, each opening its own browser session, all competing for that same
  10 RPM quota and throttling each other. Retries are now disabled and the read
  timeout is sized to the agent.

---

### 2026-08-27 (third pass) — the structural read, and what eleven runs showed

**The false-positive side worked. The false-negative side did not.**

#### False positives — criterion 4

Five runs against healthy `main`:

| Run | Agent | Findings | Verdict |
|---|---|---|---|
| 33075312063 | v7 | 0 | — |
| 33084265061 | v9 | 0 | — |
| 33084694776 | v9 | 0 | — |
| 33085124374 | v9 | 1 — "Speed Optimizer shows empty input fields" | **FALSE POSITIVE** |
| 33087787184 | v12 | 0 | — |

Plus one finding from the v12 corpus run, on the dashboard route:

- **"Good afternoon, Captain Captain"** — a **TRUE POSITIVE** in unmodified code.
  `firstName = user.name.split(' ')[0]` where the seeded name is
  *"Captain Ahmad Fauzi"*, rendered as `{greeting}, Captain {firstName}`.
  Cosmetic, correctly rated LOW, and genuinely there. The agent finds real bugs.

**One false positive in five runs, and it had the same root cause as the false
negatives.** `<input value={650}>` contributes nothing to `innerText`, because a
value is a DOM property rather than text — so the agent saw two labels with no
adjacent text and reported a working form as empty. One blind spot, both
directions. After the structural read exposed input values, it did not recur.

#### False negatives — criterion 5, still failing

| Run | Agent | S-1 | S-2 | S-3 |
|---|---|---|---|---|
| 33076107421 | v7 | ✅ | ❌ | ❌ |
| 33082234580 | v9 | ✅ | ❌ | ❌ |
| 33083682588 | v9 | ✅ | ❌ | ❌ |
| 33085953383 | v10 | ✅ | ❌ | ❌ |
| 33086619846 | v11 | ✅ | ❌ | ❌ |
| 33087220606 | v12 | ❌ | ❌ | ❌ |

Two confounds were removed along the way, and neither was the cause:

- **The turn cap was binding.** Runs were truncating at 32 turns. Measurement
  said 3.4 turns per route against a formula assuming 2.5, so the cap was raised
  to a measured 3.5 (42 turns for 8 routes). Runs now complete without
  truncating, and the number did not move.
- **My own harvester had the bug it was built to find.** The empty score renders
  as `<span/>` inside a `<div>` holding an `<svg>` and nothing else, so the
  PARENT's `innerText` is empty too — and the parent-only context filter dropped
  exactly the slot it existed to surface. Fixed to climb up to four ancestors,
  with duplicate slots counted rather than repeated. The number still did not
  move.

**The most likely remaining cause is a rubric instruction I wrote.** Guarding
against a false-positive spike, the prompt says `empty_slots` "is a HINT, not a
finding on its own. Confirm what the slot is for before reporting it." That is
probably suppressing the very report it was meant to enable — the agent cannot
confirm what a blank slot is *for* without the source, so it declines. Next
thing to try: allow a reported empty slot when it repeats across every item in a
list, which is evidence in itself and is exactly S-2's shape.

#### The methodological finding, which matters more than either rate

**Run-to-run variance is large enough that a single run is not a measurement.**
S-1 was caught in five runs and missed in the sixth. Route coverage varied
between seven and eight, and `/` was skipped in most runs — which is why the
"Captain Captain" bug went unseen for ten runs and then appeared.

This has a direct consequence for **Phase 3**, whose convergence check compares
finding sets between rounds and stops when the set stops shrinking. At this
variance, a set that shrinks between two rounds may be noise rather than
progress. Convergence needs either repeated runs per round or a tolerance band,
and the plan currently assumes neither.

It also means every rate in this document should be read as N=1 per
configuration unless stated otherwise. The corpus is cheap (~$0.10) and
repeatable; rates worth quoting need three runs each, not one.

---

### 2026-08-28 (fourth pass) — repeated-empty-slot relaxation, three runs at v13

The third pass ended on a hypothesis about my own prompt: `empty_slots` is "a
HINT, not a finding on its own — confirm what the slot is for", and the agent
cannot confirm what a blank is *for* without the source, so it declines. The
relaxation: an empty slot that repeats across every item in a list is evidence
in itself and may be reported without the confirmation. This was the last
rubric idea before Phase 2. It is now falsified.

Three runs at runtime **v13** (structural read + relaxation), against
`qa-corpus-1`:

| Run | Turns | S-1 | S-2 | S-3 |
|---|---|---|---|---|
| 33091966725 | 13 | ❌ | ❌ | ❌ |
| 33092691895 | 14 | ❌ | ❌ | ❌ |
| 33093035488 | 21 | ❌ | ❌ | ❌ |

All three authenticated and all three visited `/maintenance` and `/sire` — these
are perception misses, not coverage misses. The relaxation did not move the
number: **S-2 0/3, S-3 0/3, and S-1 0/3.** Even the loud bug — a thrown
`TypeError` behind an error boundary, caught 5-of-6 across the earlier batches —
was missed in all three runs here. That is the run-to-run variance from the
previous section doing what it said it would, and it is exactly why the
three-run protocol mattered.

One true positive fell out anyway: run 33093035488 reported the "Captain
Captain" greeting duplicate (LOW, `/`) — the real defect in unmodified code,
still there.

**The guard was not the blocker.** The agent now *may* report a repeated empty
slot and still does not, on pages it demonstrably reads. Two plausible causes
remain, and the cheapest discriminator between them is one run on the quality
rung (which criterion 6 needs anyway):

> **[resolved]** The discriminator run settled both hypotheses at once. Hypothesis
> 1 was real but not the whole story: sonnet caught the semantic seed (S-3) that
> haiku missed, so rung matters — but sonnet ALSO missed S-2 on a pristine
> harvest (a single `repeated_slots` svg-adjacent group, count 13), which rules
> out hypothesis 2: the harvest surfaced the shape perfectly. The fix is neither
> pure model choice nor another prompt edit, but a deterministic candidate layer
> (agents/qa/agent/candidates.py) that makes the mechanical signal unskippable,
> with the quality rung as the default.

1. **The cheap rung cannot connect the input to the finding.** Haiku may read
   `empty_slots: [{"context": "...", "count": N}]` and not infer "a dropped
   field." Run the corpus once on `global.anthropic.claude-sonnet-4-6`. If it
   catches S-2, the fix is model choice, not another prompt edit — and the
   model-decision exit criterion gets its measurement.
2. **The harvest still does not surface S-2's shape.** The blank may be present
   in the DOM but produce no entry, or an entry whose context reads as
   decorative. This needs the agent's tool results, which the runtime does not
   log; a stopgap is to have the harness log `_state` for one manual run.

**Consequence for Phase 2.** A detector that misses every seed 0/3 on its own
corpus should not drive auto-fix yet. Phase 2 inherits whatever Phase 1 reports,
and a false negative at Phase 1 is invisible at Phase 2 — the fix agent never
sees a finding it could have patched. Before Phase 2, criterion 5 needs to
move, or the honest claim to carry in is: the QA agent catches thrown
exceptions and cosmetic defects, and is blind to absent values — which is
exactly the class the target's own audit says dominates.

**The quality-rung discriminator ran, and it answered — but not cleanly.**
Two `workflow_dispatch` runs on `qa-corpus-1` with `--model
global.anthropic.claude-sonnet-4-6` (the second had to be fired because the
first degenerated before the browser loaded any page — 4 turns, `auth: null`,
empty PASS, a `SecurityError` reading `localStorage` on an opaque-origin
document; a second run is required to distinguish a flake from a signature):

| Run | Model | Auth | Pages | Turns | S-1 | S-2 | S-3 | Findings |
|---|---|---|---|---|---|---|---|---|
| 33094308107 | sonnet | ❌ null | 0 | 4 | ❌ | ❌ | ❌ | 0 (degenerate) |
| 33094961054 | sonnet | ✅ | 7 | 17 | ❌ | ❌ | ❌ | 2 (neither is a seed) |

The valid sonnet run **did** do what no Haiku run did: it read the values. It
reported two systematic-value anomalies, both on the exact "repeated blank
across a list" shape the relaxation targeted:

- **F-002 (`/maintenance`, MEDIUM):** "9 of 12 equipment items display '0 hrs'."
  The reading is *accurate* — vessel-002's fixture genuinely lacks `runningHours`
  on 9 of 12 units and the route defaults them to 0. But it is **pre-existing
  fixture design**, not the S-2 seed (`healthScore: undefined` → blank score in
  the SVG circle). The agent read the numbers precisely and still did not name
  the blank health score.
- **F-001 (`/sire`, HIGH):** "0 GREEN chapters despite 4 chapters having 0
  findings." This is a **false positive**. The readiness API maps
  `good→green / attention→amber / critical→red`, so GREEN counts *status*; the
  demo vessel has no `good` chapter, so `GREEN=0` is correct. The agent inferred
  "should be at least 4" from the findings counts — the rubric's "what you infer
  must be broken without seeing it fail" guard — and it was wrong.

**Both hypotheses were partly right, and the sharper conclusion is the harvest
one.** The model clearly matters: sonnet engaged with the values (17 turns,
precise reads, systematic-pattern reasoning) while Haiku produced 0/3. But the
specific S-2 symptom — the blank score inside the health circle — was **still not
reported by sonnet**, on a page it read accurately. That points at the blank
score never reaching the model as a reportable shape: the span is empty, lives
in `innerText`-invisible SVG-adjacent markup, and the empty-slot harvest caps
at 20 leaves per page. Model choice is necessary but not sufficient; the
harvest is the remaining blocker for S-2's exact shape.

**Next step (criterion 5):** instrument the harness to log `_state` for one run,
and confirm whether the `/maintenance` health-circle blank appears in it. **[done —
see the fifth pass below; the harvest was not the blocker]**. The relaxation's
false-positive finding (F-001 on `/sire`) is addressed in the same pass by the
candidate layer's mandatory assessment, which refuted the same shape five times
over on healthy pages.

---

### 2026-08-28 (fifth pass) — deterministic candidate layer + quality-rung default

The discriminator run above left two facts on the table: sonnet catches the
semantic seed haiku cannot, **and** sonnet still misses S-2 on a pristine harvest
(a single `repeated_slots` svg-adjacent group, count 13). S-2 recall is therefore
a *signal-design* problem, not a model-rung problem — the mechanical signal has to
be unskippable, not merely present. The fix, shipped as runtime v2 (code
`00b3n…`):

- **`agents/qa/agent/candidates.py`** — deterministic detectors run at every page
  read: `repeated_svg_empty` (an `svg-adjacent` group with count ≥ 2), `console_error`
  (warnings excluded), `failed_request`. The runtime emits each candidate once per
  `(type, url)` with an auto-captured screenshot.
- **Mandatory assessment.** Every candidate the agent saw must appear in
  `candidate_assessments` as `confirmed` (→ a real finding, cross-referenced by
  `finding_id`) or `refuted` (one-line reason). One bounded retry if any are
  missing; a still-missing candidate marks the run `incomplete`, never a clean PASS.
- **`DEFAULT_MODEL = global.anthropic.claude-sonnet-4-6`** — the quality rung is the
  default; haiku stays available as an explicit `--model` opt-in.

**The corpus run (33140269258, `qa-corpus-1`, model blank → sonnet):**

| Run | Model | Auth | Pages | Turns | S-1 | S-2 | S-3 | Findings |
|---|---|---|---|---|---|---|---|---|
| 33140269258 | sonnet | ✅ | 8 | 18 | ❌ | ✅ | ❌ | 1 (S-2) |

Six candidates fired; **cand-2 confirmed → F-001 (`/maintenance`, HIGH)** — the
S-2 blank ring on all 12 equipment cards, screenshot auto-attached. The other five
were **refuted with correct reasons**: dashboard legend colour swatches, the CII
chart's SVG container, Leaflet map anchor icons, the knowledge-chat input icon,
and the analytics bar-chart canvases. No `incomplete`, no `unassessed_candidates`,
no `stop_reason`. Cost: **~$0.18** (18 turns, 137 s session).

**The negative check (33140664097, healthy `main`, sonnet) — PASS:**

Seven candidates fired and **every one was refuted** — including 20 console errors
correctly attributed to a browser extension (`chrome-extension://…`), which is the
CII-phantom guard working as designed. One finding survived, the real "Captain
Captain" greeting duplicate (MEDIUM, `/`). No false-positive spike. Cost: ~$0.15.

**What this proves, and what it does not.** The deterministic layer closes the
S-2 recall gap for good — a mechanical signal can no longer be read and ignored,
because the runtime names it and the schema makes assessing it mandatory. But the
corpus score is still 1/3 this run:

- **S-1 missed — a coverage miss, not a detection miss.** The seed is
  *click-triggered* ("voyage history tab crashes when clicked"); the agent read
  `/voyage` but never performed the click, so the crash never fired — zero genuine
  console errors and zero failed requests across the whole session. The
  `console_error`/`failed_request` detectors are in place and would catch it the
  moment it manifests; the agent just did not provoke it.
- **S-3 missed — model variance.** Sonnet caught the semantic seed in the
  discriminator run but not this one. Nothing in this pass makes S-3 deterministic;
  it remains the model-driven member of the set.

**Consequence for Phase 2.** The honest claim has improved materially: the QA agent
now *reliably* catches the mechanical class (thrown exceptions, repeated blanks,
failed requests) and reports zero false positives on healthy pages — the candidate
layer turns the CII phantom into a refuted assessment instead of a reported
finding. S-1 still needs an interaction-coverage nudge (click before reading), and
S-3 remains model-dependent, before Phase 1 is "the agent catches every seed."
Phase 2 can start on this basis — its first real target is the "Captain Captain"
MEDIUM, which is a genuine, reproducible, trivially patchable defect that exercises
the whole loop end to end.


---

### 2026-08-28 (sixth pass) — the coverage half: provoking S-1

The fifth pass closed S-2 by making a mechanical signal unskippable, and left
S-1 explicitly unaddressed: *"a coverage miss, not a detection miss — the agent
read `/voyage` but never performed the click, so the crash never fired."* Zero
genuine console errors and zero failed requests across the whole session. The
`console_error` and `failed_request` detectors were in place the entire time and
had nothing to see.

That is worth stating precisely, because it bounds what any prompt change can
do. Every recall fix so far has been about **reading harder** — the structural
DOM read, the repeated-blank harvest, the candidate layer. None of them can
touch a defect that has not been provoked. The remaining lever is the agent's
own behaviour in the browser.

**Shipped:** a bounded interaction pass in the rubric — "EXERCISE each route, do
not only read it". Up to **two** in-page controls per route that change what is
displayed (tabs, expanders, view switches), read the page again after each, and
three explicit prohibitions: nothing that **writes**, nothing that leaves the
route allow-list, and never log out.

The write prohibition is not incidental. One database backs everything after it,
so a single write makes every later observation on every later route suspect —
the same problem PLAN.md Phase 3 raises across rounds, arriving one phase early
if the rule is left unstated.

**What it costs, stated before the run rather than after.** The interaction pass
is budgeted as its own constant, `_INTERACTION_TURNS_PER_ROUTE = 2.0`, kept
separate from the measured `_TURNS_PER_ROUTE = 3.5` so it stays reviewable and
removable. At eight routes the derived ceilings rise from **42 turns / 1.55M
tokens** to **62 turns / 3.34M**.

That is a 2.2x rise in a stop-loss, and it deserves the scrutiny a cost-first
project owes it. Two things make it acceptable, and both are checkable:

- **A ceiling is not a spend.** Run 33140269258 used 18 turns of a cap of 42 and
  cost ~$0.18. The cap has never been the binding constraint since it was raised
  from 32.
- **The wall clock still bites first.** At the measured ~7.6 s/turn (137 s over
  18 turns), 62 turns is ~470 s against `DEFAULT_DEADLINE_SECONDS = 600`. If that
  ever stops being true, the change traded a real control for a nominal one, so
  there is a test asserting it.

**What this pass has NOT shown.** Nothing here is a measurement — it is a
hypothesis with a cost attached. The claim to test on the next corpus run is
narrow and falsifiable:

1. S-1 is caught, because the click now happens and the crash fires into
   detectors that were always ready for it.
2. Turns per route rises by roughly the budgeted 2.0 and no further.
3. The healthy-`main` negative check produces no new findings — an agent that
   clicks more has more chances to misread what it sees, and the interaction
   pass is exactly the shape of change that could reopen the false-positive
   side that the candidate layer just closed.

If (1) holds and (3) does not, the pass is not a win. Re-measure both, three runs
each per the variance finding above, and pull
`_INTERACTION_TURNS_PER_ROUTE` down to whatever the interaction actually costs.

---

### 2026-09-01 (seventh pass) — the interaction pass measured, N=3

The sixth pass shipped the S-1 interaction pass as an explicitly falsifiable
hypothesis with three claims, then deployed it and moved on. This is the
measurement it asked for: three runs on `qa-corpus-1` against runtime v2, at
$0.75 total.

| Run | Turns | s/turn | Routes | S-1 | S-2 | S-3 | Cost |
|---|---|---|---|---|---|---|---|
| [33416330680](https://github.com/amdhd/vesselAI/actions/runs/33416330680) | 22 | 6.8 | 7 | ✅ | ❌ | ❌ | $0.22 |
| [33416898732](https://github.com/amdhd/vesselAI/actions/runs/33416898732) | 24 | 7.0 | 8 | ✅ | ✅ | ❌ | $0.24 |
| [33417399893](https://github.com/amdhd/vesselAI/actions/runs/33417399893) | 27 | 7.3 | 7 | ✅ | ✅ | ❌ | $0.28 |

**Claim 1 holds: S-1 is 3/3.** The click-triggered crash that thirteen earlier
runs never provoked is now caught every time. In run 33417399893 it arrives
through the deterministic path — `cand-2 confirmed`, a genuine console error
raised by a click the interaction pass performed — which is the detector and the
new behaviour working together rather than either alone. Recall moved from 1/3
to a mean of 5/9.

**Claim 2 holds, generously.** Pace was 6.8–7.3 s/turn against the 7.6 the budget
assumed, and runs used 22–27 turns of a 62 cap. The wall-clock worry that raising
the cap would move the binding constraint to the deadline does not survive: the
worst observed pace needs 453s of a 600s deadline, and a breach would take
9.7 s/turn. The residual is the opposite — 5.5 budgeted turns per route against
~2.3 observed makes the ceiling ~2.4x the work, which is safe but loose.

**Claim 3 remains untested.** These were corpus runs; the false-positive claim
needs healthy `main`. Supporting evidence only: across all three runs every
non-seed candidate was correctly refuted and no spurious finding appeared.

#### The finding that outranks the recall number

`cand-4` is S-2 — the blank health rings on `/maintenance`. Against identical
code:

| Run | Detection | Count | Model verdict |
|---|---|---|---|
| 33416330680 | fired | 12 | **refuted** |
| 33416898732 | fired | 13 | confirmed |
| 33417399893 | fired | 12 | confirmed |

**The detection is deterministic. The assessment is not.** The fifth pass closed
the S-2 gap by making a mechanical signal impossible to walk past, and that
worked — the signal was present in all three runs. What varied was the model's
verdict on it, and a refuted candidate produces no finding.

So "the candidate layer is deterministic" is true of only half of it, and the
half that reaches the findings list is the model-driven half. That is a sharper
statement of the fifth pass's own conclusion than the fifth pass made, and it has
a direct consequence for Phase 3: a convergence loop must compare raw candidate
DETECTIONS, never findings and never assessments. See `REMEDIATION.md`.

---

### 2026-09-01 (ninth pass) — the eighth pass was wrong about S-3

**Read this before the eighth pass below, which is retained unedited because
deleting a wrong diagnosis is how the S-3 problem happened in the first place.**

The eighth pass claimed S-3 is structurally unobservable — that the seed breaks
only `summary.open`, that the finding *data* is untouched, and that the page
never renders the broken field. **Every one of those three claims is false.** It
was written after reading the seed's diff filtered to one file, `sire.ts`, and
generalising from it.

#### What the seed actually changes

`backend/src/mock/findings.ts` — the **data and its type**:

```diff
-  status: 'open' | 'closed';
+  status: 'OPEN' | 'closed';
-        status: 'open',      (x4 records)
+        status: 'OPEN',
```

`backend/src/routes/sire.ts` — the summary filter, **to match**:

```diff
-      open: findings.filter(f => f.status === 'open').length,
+      open: findings.filter(f => f.status === 'OPEN').length,
```

Data and backend filter were changed **together**, so `summary.open` is
*correct* on the corpus branch. The eighth pass had this exactly backwards.

#### The real symptom, and it is visible

The break is in the **frontend**, which compares lowercase everywhere:

| `SirePage` | on the corpus branch | visible effect |
|---|---|---|
| `findings.filter(f => f.status === 'open')` | 0 | the "N open" badge disappears |
| `'text-status-red': finding.status === 'open'` | false | open findings lose their red styling |
| `{finding.status}` | renders `"OPEN"` | raw uppercase enum leaks into the UI |

Three symptoms, all on screen. S-3 is a **catchable** seed. It has been missed
0/16 for some other reason — most likely that all three symptoms are quiet, and
the rubric explicitly tells the agent that `/sire` is a fixture surface whose odd
values are the demo working as intended.

#### The correction to the numbers

Recall is **5/9, not 5/6**. The eighth pass's headline is withdrawn. Every
`/3` rate in this document stands as originally written.

#### And a change that made it worse

`amdhd/vesselAI#109` moved the page onto the server's `summary.open`. That was
worth doing on its own merits — one source of truth for a number the backend
already publishes — but it does **not** make S-3 more catchable, as its PR
claimed. It makes it **less** so: `summary.open` is correct on the corpus branch,
so the badge returns and symptom one disappears. Two of the three remain.

#### What actually went wrong here

Twice now, on the same seed. The second pass diagnosed it correctly ("weak seed,
redesign it"), later passes overwrote that with "model variance", and the eighth
pass overwrote *that* with a confident structural claim built on a partial diff.
Each step was more certain than the last and further from the code.

The lesson is narrow and worth keeping: **this document's claims about seeds must
be checked against the seed's full diff**, not against a filtered view of it, and
not against what an earlier entry said. The second pass's original prescription
is still the outstanding one — redesign S-3 so its symptom is loud enough to
survive a rubric that is correctly sceptical about fixture surfaces.

---

### 2026-09-01 (tenth pass) — the rubric change that makes S-3's render breaks reportable

**The prescription above is implemented, but not yet measured.** The ninth pass
established that S-3's symptom is a RENDER break — the seed speaks `'OPEN'` while
the frontend compares lowercase — and that the miss was the rubric, which told the
agent that odd values on `/sire` are "the demo working as intended." That wording
swallowed the one reportable class along with the seeded statistical noise.

`agents/qa/agent/rubric.py` now draws the line the exemption was always missing:

- **The fixture exemption protects the NUMBERS.** Outliers, spikes, trends — the
  seeded statistical anomalies — are still explicitly not findings.
- **It no longer protects how the UI renders them.** Three shapes are now named as
  reportable even on a fixture surface: a raw enum or key leaking into the text
  (the `"OPEN"` case), a derived count reading zero while the items carrying that
  value are on screen (the `0 open` badge), and styling or a status colour that
  never applies (the lost `text-status-red`). "Report the break, never the numbers."

This is the in-repo half of P1.1. It does **not** close the gap on its own: recall
is a *measured* number, and this change is unmeasured — a corpus run is required to
see whether the agent now reports S-3 without the false-positive spike the carve-out
risks. The seed itself is untouched (it was never the problem; see above).

The next entry, whenever it is written, must be a *measurement* of this change, not
another claim. The eighth pass is the warning: read the seed's full diff, and don't
let an entry's confidence outrun its evidence.

---

### 2026-09-01 (eleventh pass) — the P1.1 measurement: the carve-out does not close S-3 and reopens the FP baseline

**This is the measurement the tenth pass demanded.** PR #49's rubric carve-out was
merged to `pipelineguard` `main`, the vesselAI workflow gained an
`actions/upload-artifact` step for `findings.json` (PR #113), and the full P1.1
protocol ran per [MEASUREMENT-P1-1.md](MEASUREMENT-P1-1.md) on 2026-09-01. The
corpus was `qa-corpus-1` @ `de96b34`, healthy `main` @ `a478451`, model
`global.anthropic.claude-sonnet-4-6`, `max_routes` 8.

**Provenance:** the first `workflow_dispatch` (run 33522662004) failed at
*Configure AWS credentials* — the QA role's trust policy had been narrowed to
`pull_request` + `main` since the seventh pass, silently revoking the corpus branch
(which had authenticated fine on the seventh pass). The branch subject was restored
via `iam update-assume-role-policy`; the failed run produced no findings and is
excluded. Runs 33523494711 and 33524121532 then dispatched clean. Run 33522593136
was PR-triggered — pushing to `qa-corpus-1` fires PR #95's `synchronize`, which is
expected, not an error, and it ran the same corpus commit.

#### Leg A — recall (three corpus runs)

| Run | Trigger | Turns | Wall | S-1 `/voyage` | S-2 `/maintenance` | S-3 `/sire` |
|---|---|---|---|---|---|---|
| 33522593136 | `pull_request` sync | 24 | 166 s | ✅ | ✅ | ❌ |
| 33523494711 | `workflow_dispatch` | 23 | 168 s | ✅ | ✅ | ❌ |
| 33524121532 | `workflow_dispatch` | 21 | 153 s | ✅ | ✅ | ❌ |

**Recall 6/9.** S-1 and S-2 held at 3/3 and 3/3 — no regression. **S-3 is 0/3**,
exactly where it stood before the carve-out. Every run visited `/sire`; none
reported the render break. Leg A's pass bar (S-3 ≥ 2/3) **fails**.

#### Leg B — false positives (one healthy-`main` run, no seeds)

| Run | Turns | Wall | Findings on fixture surfaces |
|---|---|---|---|
| 33524720902 | 25 | 175 s (paced 61.8 s) | **2** — F-001 `/voyage`, F-002 `/sire` |

Leg B's pass bar is **0 findings on fixture surfaces**, and the last measured
healthy-`main` run (criterion 4) was exactly that. This run produced two. **Leg B
fails.**

**F-001 (`/voyage`, HIGH) — labelled false positive.** The agent reported the
Voyage History savings figures as "unrealistically inflated" and "economically
implausible" (rows where actual > planned fuel yet positive savings; totals in the
hundreds of millions). Those figures are **fixture data**, not a computation:
`getVoyagesByVesselId` serves `backend/src/mock/voyages.ts`, whose rows deliberately
carry anomalies (e.g. `plannedFuel: 3200, actualFuel: 3245` → `savings: 29500`;
savings magnitudes that exceed the voyage's total fuel, `38400` on an 1850 MT
voyage). This is the runbook's named regression class verbatim: *"A finding that
says 'the numbers look odd' is a regression from the carve-out and must go back to
the rubric."* It also crosses the carve-out's own remaining line — "never report a
figure because the values look surprising."

**F-002 (`/sire`, MEDIUM) — labelled false positive.** The agent claimed "0 GREEN
chapters despite 4 chapters having 0 findings." The cited scores (Crew 70, Safety
68, Electrical 66, Mooring 72) are **vessel-002's** chapters, and on that vessel
**0 GREEN is correct**: the readiness route maps `good→green / attention→amber /
critical→red` (sire.ts `STATUS_MAP`), and all ten vessel-002 chapters score ≤72, so
none is green. Chapter status keys on score, not on open-finding count; the agent
conflated "0 findings" with "should be green." The carve-out's third shape — "a
derived count reading zero while the items carrying that value are on screen" —
gave the agent a template to construct this claim from healthy data, but no chapter
carrying a green status was on screen. The count was correct.

Both findings are labelled in `corpus.example.json`, so the Leg B false-positive
rate is now measurable (2/2 labelled → 100 % of the run's findings were not
defects).

#### Why the carve-out could not have closed S-3

The candidate layer is the bottleneck, and it is not on the reporting path the
rubric governs. Across all three Leg A runs the agent generated 24 candidates;
**not one mentioned `/sire`, `OPEN`, `status`, or the badge** — every candidate was
either an "svg-adjacent empty slot" or a browser-extension console error, and all
of those were correctly refuted. The agent *visits* `/sire` and never forms a
candidate for the render break. The carve-out changed what may be *reported*; the
agent's perception gate never *saw* the break to report. A permission cannot fix a
failure to look.

#### Verdict — P1.1 does not flip to DONE

The carve-out fails both acceptance criteria at once: it did not close S-3 (0/3,
recall still 6/9 < 8/9) **and** it regressed the false-positive baseline (0 → 2 on
healthy `main`). The rubric change should go back — restore the fixture exemption
or narrow the carve-out — while S-3 moves to the layer that actually decides the
miss: candidate generation. The seed is observable on-screen (ninth pass) and the
break is machine-checkable from the DOM (a raw status word in rendered text, a
badge count reading zero). That is the next lever, and it is a mechanical one, not
another wording change to the rubric.

---

### 2026-09-02 (twelfth pass) — the P1.2 measurement: the S-3 candidate layer closes neither half of S-3

**This is the measurement the tenth pass demanded.** PR #50 (`feat/s3-candidate-layer`)
merged the `status_case_leak` detector into the harness; the first measurement of it
caught a deterministic detector bug, fixed by PR #51 (`fix/s3-twin-check`); and the
coverage diagnosis (S-3's leak renders only on a non-default tab) produced PR #52
(`feat/open-every-view`), which made opening every view a route offers REQUIRED
reading. The P1.2 protocol ran per [MEASUREMENT-P1-2.md](MEASUREMENT-P1-2.md): three
corpus runs on `qa-corpus-1` @ `de96b34` and one healthy-`main` run @ `a478451`,
model `global.anthropic.claude-sonnet-4-6`, `max_routes` 8. The workflow checks the
harness out at `PIPELINEGUARD_REF: main` (`ui-qa-agent.yml` line 74), so every run
below executed `main` @ `5454e922` — the coverage rule was live.

#### Leg A — recall (three corpus runs)

| Run | Turns | Wall | S-1 `/voyage` | S-2 `/maintenance` | S-3 `/sire` |
|---|---|---|---|---|---|
| 33533100509 | 28 | 186 s | ✅ | ✅ | ❌ |
| 33533958227 | 24 | 165 s | ✅ | ✅ | ❌ |
| 33534532566 | 24 | 166 s | ✅ | ✅ | ❌ |

**Recall 6/9, Leg A FAILS — identical to the second pass.** S-1 and S-2 held at 3/3
and 3/3 (no regression); S-3 is again 0/3. The coverage rule changed nothing
measurable on any seed.

#### Leg B — false positives (one healthy-`main` run, no seeds)

| Run | Turns | Wall | Findings on fixture surfaces |
|---|---|---|---|
| 33535141706 | 21 | 138 s | **0** — clean, overall PASS |

**Leg B PASSES.** The fixed detector plus the open-every-view rule produced **zero
findings on healthy `main`**, including zero `status_case_leak` candidates. The
coverage rule did not reopen the FP baseline the carve-out (PR #49) had regressed.

#### Why the coverage rule could not close S-3

The miss is a coverage-within-route failure, now pinned down from every side:

- **The detector is correct and provably live.** `_HARVEST` pass 4
  (`browser_tools.py` lines 162-181) collects all-caps words from `nodeValue` — the
  SOURCE case, which the `/sire` status span's `capitalize` CSS class never touches —
  into `case_words`, and `detect()` is unit-tested to emit `status_case_leak` on the
  exact `OPEN`/`Closed` DOM shape. The same harvest's `repeated_slots` fired in all
  three runs, so the machinery is running; if the Findings tab were in the DOM,
  `OPEN` would deterministically reach `case_words` and the candidate would fire.
- **`/sire` was visited in all three runs** (`routes_visited` includes it in each),
  yet **no `status_case_leak` candidate has EVER fired in any corpus run** — 0/7
  across the merged, fixed, and coverage passes, and 0/6 on the fixed detector.
- **The `/sire` Findings tab — the only view that renders the raw `OPEN` — was never
  materialized.** The leak's signal never entered the DOM, so the detector had
  nothing to see. This is the eleventh pass's perception-gate failure one level
  deeper: not "the agent visited `/sire` and did not notice the break" but "the agent
  visited `/sire` and did not render the view the break lives on."
- **The mandatory open-every-view rule did not move it.** The agent read the Readiness
  default tab and moved on in every run. The rubric lever is exhausted: two designs —
  optional "up to two controls", then mandatory "open EVERY view ... Switching views
  is READING" — both produced S-3 0/3.

#### Verdict — P1.1 closes via the written-decision branch

Leg A fails (S-3 0/3, recall 6/9 < 8/9) and Leg B passes (0 findings on healthy
`main`). The candidate layer is correct and the FP side is clean, but **the candidate
is unreachable: the raw-enum signal renders only on a non-default tab the agent will
not open, so `status_case_leak` can never fire.** The candidate layer moved the miss
out of the model's *judgment* and into the runtime's *harvest* — but the harvest only
sees the DOM the model chooses to materialize, so the miss re-emerged at the
*materialization* step.

The human decision (2026-09-02) exercises the written-decision branch of P1.1's
acceptance criterion: **a defect whose symptom renders only behind a non-default
view is declared OUT OF SCOPE for this browser-driving agent while the runtime
harvests only the DOM the model materializes.** The miss is owned: recall 6/9,
S-3 0/7, the materialization gate named, the `status_case_leak` detector retained
in the harness, and the reopening condition recorded — any layer, current or future,
that materializes the `/sire` Findings tab will fire it. S-3 is a named limitation,
not a silent one.

---

### 2026-09-01 (eighth pass) — S-3 was never catchable, and we knew

**S-3 is 0/16.** Not 0/16 because the model is weak. Because there is nothing on
the screen to see.

#### What the seed actually does

The corpus branch changes one line in `backend/src/routes/sire.ts`:

```diff
-      open: findings.filter(f => f.status === 'open').length,
+      open: findings.filter(f => f.status === 'OPEN').length,
```

That breaks the API's `summary.open`, which becomes 0. The finding **data** is
untouched — every record still carries lowercase `'open'`.

And `summary.open` is never rendered. `frontend/src/lib/api.ts` discards it:

```ts
const { data } = await api.get<{ findings: SireFinding[] }>(`/sire/findings/${vesselId}`)
return data.findings          // summary thrown away
```

…and `SirePage.tsx` computes its own count from the list, with the correct
comparison:

```tsx
const open = findings?.filter((f) => f.status === 'open').length ?? 0
{open > 0 && <span>{open} open</span>}
```

So the page is correct, because it *is* correct. No crash, no blank, no wrong
number. A browser-driving agent cannot observe this defect by any means, and
sixteen runs have scored it as a miss.

#### The part that stings

**The second pass already found this and said so:**

> **S-3 — MISSED, but the seed was weak and should not count cleanly.** … That
> is close to unobservable through a browser, so this measures the *seed's*
> design more than the agent's eyesight. **Redesign S-3** … and re-run before
> quoting a 3-seed rate.

It was not redesigned. Five subsequent passes quoted 3-seed rates anyway, and
the label decayed from "weak seed, redesign it" to "**model variance**" — a
phrase this document uses twice, most recently in the seventh pass, written by
someone who had not read the seed and inherited the wrong explanation.

Two failures, and the second is worse. Missing something is ordinary. Recording
it, prescribing the fix, and then overwriting the diagnosis with a plausible
wrong one is how a document stops being evidence.

Even the seed table's own description was wrong: it said the status *data* was
`OPEN` and the frontend compared `'open'`, which would at least break a colour
class. The real seed touches only an unrendered backend field.

#### What the numbers actually are

| Seed | Symptom in the browser | Catchable | Result (N=3) |
|---|---|---|---|
| S-1 | tab crashes — console error | yes, deterministic | **3/3** |
| S-2 | 12 blank rings — repeated empty slot | yes, deterministic | **2/3** |
| S-3 | none | **no** | n/a |

**Recall on catchable seeds is 5/6, not 5/9.** Every rate in this document
before this pass is scored against a denominator containing an impossible test —
which made the agent look worse than it is, consistently, in a number that has
been used to argue about readiness.

#### The fix, and why this shape

`SirePage` should display the API's `summary.open` rather than recomputing it.
That is the better application regardless: the backend already computes the
figure, and having the frontend derive it independently is duplicated logic whose
first drift is exactly this bug. On `main` the rendered output is unchanged,
because both computations agree.

On the corpus branch it turns S-3 into a real semantic test — a plausible-looking
`0 open` beside a list of findings the page itself styles as open. That
contradiction is visible, is not a crash or a blank, and belongs to no
deterministic detector. It is precisely the class the candidate layer cannot
reach and the model must judge, which is what S-3 was always supposed to measure.

The count must also render unconditionally. `{open > 0 && …}` hides the badge
when the number is zero, so the seeded symptom would be an *absence* — and the
rubric correctly tells the agent that an absent element is a missing feature, not
a defect. A wrong number is reportable; a missing badge is not.

---

## Phase 2 — the fix-agent loop, and the demonstration P1.4 asked for

Phase 2's demonstration criterion (AUDIT.md **P1.4**) is: *a real fix-agent PR
(compiles, CI green, no human intervention on the agent's side) exists and is
referenced, not claimed.* It is met here by primary-source record. The two runs
below are the current `agent-fix` workflow in `amdhd/vesselAI`
(`workflow_dispatch` on `main`, harness checked out at pinned
`PIPELINEGUARD_REF`, PRs opened with the GitHub App token).

### 2026-08-31 — the fix loop ran live, twice: one PR merged CI-green, one closed as wrong

| Run | Input | PR | Outcome |
|---|---|---|---|
| [33409654638](https://github.com/amdhd/vesselAI/actions/runs/33409654638) | `fixtures/findings-33140664097.json` | [amdhd/vesselAI#102](https://github.com/amdhd/vesselAI/pull/102) | **MERGED** — 11 checks green |
| [33412966705](https://github.com/amdhd/vesselAI/actions/runs/33412966705) | `fixtures/findings-33137979741.json` | [amdhd/vesselAI#105](https://github.com/amdhd/vesselAI/pull/105) | **CLOSED** — never merged |

**PR #102 — the merged demonstration.** Authored by `app/pipelineguard-fix-agent`
(the GitHub App identity), produced by run `33409654638`, patching
`frontend/src/pages/DashboardPage.tsx` (+2/−2) — the "Captain Captain" greeting
duplicate reported by the 2026-08-28 negative-check run. Every CI check it
needed to merge was green with no human re-runs: Backend, Frontend and Angular
(typecheck + test + build), the data platform, the Docker image build, all five
image build/scan/publish jobs, and the Kubernetes manifest render. The one
non-green entry, "QA agent", concluded **SKIPPED** — that workflow is
`workflow_dispatch`-only, so its PR-time guard correctly declines to run; it is
not a failure. The PR was merged 2026-08-31 (merge commit `5d91f6b3`).

**Why this satisfies the criterion, clause by clause:**

- **compiles** — the Backend/Frontend/Angular typecheck + test + build checks are
  green; the patch passed the compile-and-test gate before the PR was even opened.
- **CI green** — all 11 checks that ran on the PR's own commit succeeded. That the
  checks ran *at all* is itself the load-bearing fact: a PR opened with the
  default `GITHUB_TOKEN` arrives with zero checks and is permanently unmergeable
  (PLAN.md, the "`GITHUB_TOKEN` trap"). The rollup proves the GitHub App token
  path, which the criterion exists to catch.
- **no human intervention on the agent's side** — finding → patch → PR were
  produced by the harness and workflow unassisted. The only human step was the
  final merge click, which is the designed terminal gate ("branch protection
  provides the human gate", PLAN.md).
- **referenced, not claimed** — this entry, the PR link, the run id, and the
  check rollup above *are* the reference. REMEDIATION.md asserted the merge in
  prose; the audit correctly refused to count an unreferenced claim.

**PR #105 — the honest counterpoint.** The second run (the two-finding fixture,
which includes the by-then-already-fixed greeting) patched
`backend/src/routes/sire.ts` and `frontend/src/pages/DashboardPage.tsx` (+3/−3),
opened as a PR, and was closed eight minutes later, unmerged. The pipeline also
produces patches a reviewer rejects — which is why the loop never auto-merges.

**Caveats, recorded not hidden:**

- The inputs were **committed fixtures**, not live QA findings — the PLAN
  prescribed exactly this for the first smoke tests (a fixture in the repo keeps
  resolving after the 7-day S3 reports bucket expires). The underlying finding is
  real (the greeting defect was reproduced against `main`); the frozen copy is
  what makes the run reproducible.
- The merged patch was **two lines** — it exercised the whole loop (selection,
  edit, gate, PR, merge) but not the hardest editing shapes.
- A **human clicked merge** (#102 was merged by `amdhd`, not the bot). The
  criterion's "no human intervention on the agent's side" is about the agent's
  work, and that was unassisted; the human gate is a feature, not a failure.

**Phase 2 status after this record:** the demonstration criterion (P1.4) is
DONE. Written 2026-08-31, this note kept P2.3 (fork-PR guard) and P2.4 (version
pinning) open; both closed 2026-09-02 (AUDIT.md), and P2.5 (hash-locked
closure) closed 2026-09-04 — **no P2.x item remains open**. Phase 3's live run
(P1.3) follows in the next section; the one item still open across the phases is
Phase 1's **P1.7** — a real FP rate over ≥3 human-labelled PRs (runway READY
2026-09-04, `MEASUREMENT-P1-7.md`).

---

## Phase 3 — the convergence loop ran live

Repeated runs per round were the audit's allowed answer to its own blocked
methodological risk (noted above: "a set that shrinks between two rounds may be
noise rather than progress"). `convergence.aggregate_findings` + a thin
`aggregate.py` CLI (pipelineguard#55 `817852a`) make each round QA the corpus K
times and keep a finding only when a strict majority of the runs report it,
graded at the most severe of its reporters, under a prose-tolerant identity
(page + Sorensen-Dice over significant tokens). Six live runs then taught six
mechanism lessons in turn — a false PASS, two mislabelled REGRESSEDs, an honest
stop whose ledger mislabelled its last patch, a truthful stop, and finally the
first live PASS. The demo target was `qa-corpus-1` @ `de96b34`, seeded with two
deterministic rendering defects: S-1 (`/voyage` HIGH crash) and S-2
(`/maintenance` blank score rings).

| run | pipelineguard pin | outcome | lesson → fix |
|---|---|---|---|
| 33586824290 | `817852a` (#55) | **false PASS** (0 findings) | within-round aggregation key must survive prose → #56 `01924da` |
| 33588304974 | `01924da` (#56) | REGRESSED | between-round comparison must survive prose → #57 `1446cd3` |
| 33590568978 | `1446cd3` (#57) | REGRESSED | a round's own patch must earn a verification round → #59 `4acc325` |
| 33593197606 | `4acc325` (#59) | honest MAX_ROUNDS — but the ledger called its last patch "ineffective" | a final-round patch is unverified, not ineffective → #60 `630f3ea` |
| 33596263397 | `630f3ea` (#60) | truthful MAX_ROUNDS | — (#60 held live) |
| 33599986790 | `630f3ea` (#60) | **PASS** — first live convergence | — |

vesselAI carried each pin: #114 (→ #55), #115 (→ #56), #117 (→ #57), #119
(→ #59), #121 (→ #60). The agent PRs each run opened against `qa-corpus-1`
(#116, #118, #120, #122, #123) were closed unmerged, so the corpus stayed at
`de96b34a` throughout.

### 2026-09-02 (first live run) — the false PASS that proved identity must survive prose

Run
[33586824290](https://github.com/amdhd/vesselAI/actions/runs/33586824290):
target `qa-corpus-1` @ `de96b34`, repeats=3, max_rounds=3, token budget 6 M,
severities CRITICAL,HIGH.

The loop ran end-to-end for the first time — stack, tunnel, health gate, three
QA sessions (632 s, $0.78), the majority aggregate, a decision, a stop. But the
decision was a false **PASS** on 0 findings, and the run is more valuable as a
failure than the success it claimed:

- **The mechanics were right.** `repeats: {total: 3, completed: 3, threshold: 2}`
  was written; tokens (1,096,392) and session_seconds summed across the three
  runs; `observed_at_commit` preserved.
- **The identity was wrong.** All three QA runs found the same two seeded
  defects — S-1 `/voyage` HIGH and S-2 `/maintenance` MEDIUM — each phrasing
  them differently ("crashes with TypeError on .toFixed() of undefined" vs
  "crashes React tree with TypeError on 'toFixed' call" vs "Cannot read
  properties of undefined (reading 'toFixed')"). The aggregation key was
  `page::summary` verbatim, so each phrasing was its own 1-of-3 group and every
  finding fell below the strict majority. The round recorded PASS on a corpus
  with two real defects in it.

This is the run-to-run variance this document predicted, one layer deeper than
measured before: it is not just whether a defect is caught (S-1 was caught 5/6
historically) — it is that the *summary* a model writes for the same defect is
prose that changes every run. A convergence key built on that prose cannot
recognise the same defect twice. The fix (pipelineguard#56 `01924da`, carried by
vesselAI#115) groups by page and Dice-similarity over significant tokens, within
a round and (after #57) between rounds.

### 2026-09-02 (second live run) — REGRESSED: the between-round comparison did not survive prose

Run
[33588304974](https://github.com/amdhd/vesselAI/actions/runs/33588304974):
target `qa-corpus-1` @ `de96b34`, repeats=3, max_rounds=3, token budget 6 M,
severities CRITICAL,HIGH.

| round | aggregated blocking set | decision | patched | commit observed | tokens | seconds |
|---|---|---|---|---|---|---|
| 1 | S-1 `/voyage` HIGH | CONTINUE | 1 (S-1) | `de96b34a` | 1,201,968 | 674 |
| 2 | S-1 `/voyage` HIGH (rephrased) | **REGRESSED** | 1 (S-1) | `0bcbea1c` | 1,113,215 | 596 |

**Decision history:** CONTINUE → REGRESSED. **Cost:** 2,369,311 tokens (~$1.78),
1,050 s session. Agent PR amdhd/vesselAI#116 closed unmerged.

Round 1 patched the S-1 crash (its patch committed as `0bcbea1c`). Round 2 —
measuring that patch — reported the same `/voyage` crash, but phrased
differently ("crashes with TypeError on toFixed, triggering error boundary" vs
round 1's "Cannot read properties of undefined (reading 'toFixed')"). The
between-round comparison still used the exact fingerprint (`page::severity::
summary.strip().lower()`), so the rephrased-but-persistent blocker did not match
its predecessor: the set "gained a member" that round 1 "never had", and the
verdict was REGRESSED — the patch is the suspect — for a defect round 2 was
looking straight at. Replaying this run's state through the fixed mechanism
(#57 prose-tolerant cross-round matching, #59 verification) reads round 2 as
CONTINUE: the blocker was rephrased, not new, and round 2 patched it again.

### 2026-09-02 (third live run) — REGRESSED: a round's own patch was never measured

Run
[33590568978](https://github.com/amdhd/vesselAI/actions/runs/33590568978):
target `qa-corpus-1` @ `de96b34`, repeats=3, max_rounds=3, token budget 6 M,
severities CRITICAL,HIGH.

| round | aggregated blocking set | decision | patched | fix | commit observed | tokens | seconds |
|---|---|---|---|---|---|---|---|
| 1 | S-1 `/voyage` CRITICAL | CONTINUE | 0 | **skipped** | `de96b34a` | 1,179,499 | 642 |
| 2 | S-1 `/voyage` HIGH + S-2 `/maintenance` HIGH | **REGRESSED** | 2 (both) | 2 edits/2 files | `de96b34a` | 1,051,921 | 590 |

**Decision history:** CONTINUE → REGRESSED. **Cost:** 2,318,040 tokens (~$1.86),
1,047 s session. Agent PR amdhd/vesselAI#118 closed unmerged.

Two failures in one run. Round 1's fix agent **skipped** S-1, reasoning that the
crash was `ciiImpact.toFixed` on an undefined value and that — since the backend
maps `ciiImpact ?? 0` — the undefined value must originate in the withheld mock
files. That diagnosis was wrong: the crash is `formatFuel(voyage.actualFuel)` on
a field the backend `/history/:vesselId` returns as `actual_fuel` (snake_case)
while the frontend reads `voyage.actualFuel` (camelCase). The fix is a two-line
change to reachable `backend/src/routes/voyage.ts`, and round 2 made it (plus a
three-line `maintenance.ts` change for S-2).

And the verdict the loop reported was false in the worse direction. Both rounds
observed `de96b34a` — round 1 patched nothing, so the code never changed — yet
round 2's **pre-fix** finding set was the input to the decision, and that set had
*grown* (S-2's aggregate grade flipped MEDIUM→HIGH on a 1-of-3 outlier, under the
conservative most-severe grading rule). The loop therefore reported REGRESSED —
"the patch is the suspect" — and stopped one round before round 2's correct
fixes could be verified. A round records its findings BEFORE its fix runs; the
stopping rule was passing sentence on a patch that had never been measured. The
fix (pipelineguard#59 `4acc325`) makes STALL and REGRESSED fire only when the
round applied no patch — the one state where another round would re-measure
identical code — and requires a real patch to have run before blaming one.
Replaying this run's state through #59 reads round 2 as CONTINUE: "round 2
patched 2 blocking finding(s); the next round measures whether the fixes took."

### 2026-09-02 (fourth live run) — an honest MAX_ROUNDS, then the ledger mislabelled its last patch

Run
[33593197606](https://github.com/amdhd/vesselAI/actions/runs/33593197606):
target `qa-corpus-1` @ `de96b34`, repeats=3, max_rounds=3, token budget 6 M,
severities CRITICAL,HIGH.

| round | aggregated blocking set | decision | patched | commit observed | tokens | seconds |
|---|---|---|---|---|---|---|
| 1 | S-1 `/voyage` HIGH | CONTINUE | 1 | `de96b34a` | 1,080,694 | 647 |
| 2 | S-1 `/voyage` HIGH + S-2 `/maintenance` MEDIUM | CONTINUE | 1 | `b5a7b8a7` | 1,152,968 | 601 |
| 3 | SAVINGS `/voyage` CRITICAL + S-2 `/maintenance` MEDIUM | MAX_ROUNDS | 1 | `1ce22c0b` | 1,446,657 | 656 |

**Decision history:** CONTINUE → CONTINUE → MAX_ROUNDS. **Cost:** 3,680,319
tokens ($2.75), 1,904 s loop wall. Agent PR amdhd/vesselAI#120 closed unmerged.

The loop stopped honestly at its 3-round cap while still shrinking, and its
ledger then lied about the last round. Round 3 patched a newly-surfaced SAVINGS
CRITICAL — the crash fix had let the savings table render, unmasking its wrong
arithmetic. A round records its findings BEFORE its own fixes run, so round 3's
finding set — measured against the pre-fix code — still contained the SAVINGS
CRITICAL; the loop then hit its round cap, and nothing ever re-measured the
round-3 patch. `reconcile()` saw a finding that was patched yet still present in
the final round and labelled it "patch did not fix it": a verdict of ineffective
passed on a patch no round had measured. The fix (pipelineguard#60 `630f3ea`,
carried by vesselAI#121) added `PATCH_UNVERIFIED` — "patch unverified (run ended
before a re-measure)" — which requires a later round to have measured a finding
before calling its patch ineffective. Replaying this run's state through #60
reads the SAVINGS row as patch unverified, not ineffective. The stopping
decision itself (MAX_ROUNDS, "finding set is still shrinking") was already
correct — the mechanism that lied was the reconciliation ledger, not
`decide()`.

### 2026-09-02 (fifth live run) — MAX_ROUNDS with a truthful ledger

Run
[33596263397](https://github.com/amdhd/vesselAI/actions/runs/33596263397):
target `qa-corpus-1` @ `de96b34`, repeats=3, max_rounds=3, token budget 6 M,
severities CRITICAL,HIGH, on pipelineguard `630f3ea` (carried by vesselAI#121).

| round | aggregated blocking set | decision | patched | commit observed | tokens | seconds |
|---|---|---|---|---|---|---|
| 1 | S-1 `/voyage` HIGH + S-2 `/maintenance` MEDIUM | CONTINUE | 1 (S-1) | `de96b34a` | 1,046,753 | 616 |
| 2 | S-1 `/voyage` HIGH + S-2 `/maintenance` MEDIUM | CONTINUE | 1 (S-1) | `c8680c4a` | 1,112,052 | 599 |
| 3 | SAVINGS `/voyage` CRITICAL + S-2 `/maintenance` HIGH | MAX_ROUNDS | 2 (both) | `36f2485f` | 1,422,223 | 670 |

**Decision history:** CONTINUE → CONTINUE → MAX_ROUNDS. **Cost:** 3,581,028
tokens ($2.78), 1,885 s loop wall. Agent PR amdhd/vesselAI#122 closed unmerged.

This is run 4 on the #60 fix, and the ledger now tells the truth. Round 3
patched the SAVINGS CRITICAL and the S-2 maintenance ring, hit its round cap,
and `reconcile()` reads both patches as **"patch unverified (run ended before a
re-measure)"** — the correct verdict for a patch applied in the final round —
while S-1's crash, patched in rounds 1 and 2 and measured absent in round 3,
reads **fixed**. The vote absorbed run-to-run variance in the visible shape the
aggregate was built for: round 1's repeat x2 alone reported a LOW "Captain
Captain" greeting (a third finding neither x1 nor x3 saw), so it was 1-of-3 and
dropped from the aggregate. A single-run loop would have PASSed or stalled on
whichever run it happened to draw.

### 2026-09-02 (sixth live run) — PASS, the first live convergence

Run
[33599986790](https://github.com/amdhd/vesselAI/actions/runs/33599986790):
target `qa-corpus-1` @ `de96b34`, repeats=3, **max_rounds=5**, token budget 10 M,
severities CRITICAL,HIGH, on pipelineguard `630f3ea` (carried by vesselAI#121).

| round | aggregated blocking set | decision | patched | commit observed | tokens | seconds |
|---|---|---|---|---|---|---|
| 1 | S-1 `/voyage` HIGH + S-2 `/maintenance` MEDIUM | CONTINUE | 1 (S-1) | `de96b34a` | 1,146,587 | 677 |
| 2 | S-1 `/voyage` HIGH + S-2 `/maintenance` MEDIUM | CONTINUE | 1 (S-1) | `a7640566` | 1,081,291 | 581 |
| 3 | S-2 `/maintenance` (S-1 gone) | CONTINUE | 1 (S-2) | `4beb2391` | 1,276,785 | 601 |
| 4 | none | **PASS** | — | `a0087e4b` | 1,179,639 | 556 |

**Decision history:** CONTINUE → CONTINUE → CONTINUE → PASS. **Cost:** 4,684,302
tokens ($3.44), 2,415 s loop wall. Agent PR amdhd/vesselAI#123 closed unmerged.

The first live convergence PASS. Rounds 1 and 2 patched the S-1 `/voyage` crash
twice before it held — round 1's `VoyageHistory.tsx` guard on `ciiImpact` did
not reach the actual undefined (the backend `/history/:vesselId` returns
`actual_fuel`, snake_case, while the frontend reads `voyage.actualFuel`), and
round 2's two-line `voyage.ts` change fixed the real cause. Round 3 measured the
crash gone and patched the last blocker — the S-2 `/maintenance` health ring
(`maintenance.ts` no longer overwrites the mock's `healthScore` with
`undefined`). Round 4 measured both fixes held: the aggregated blocking set was
empty, and `decide()` returned PASS.

Round 4 is also the vote doing exactly its job, and the clearest statement of
what the strict majority costs. Its three runs each reported a *different*
finding, none shared: x1 a LOW greeting duplicate, x2 a **CRITICAL** `/voyage`
SAVINGS anomaly, x3 a LOW `/analytics` axis-ticks artifact. Each was 1-of-3 and
every one fell below the ≥2-of-3 threshold, so the aggregate recorded zero
blockers and the round PASSed. A single-run loop would have passed or failed on
a coin flip. The honest caveat: the SAVINGS anomaly x2 reported is real — it was
CRITICAL in the two prior runs' round 3, once the crash fix let the savings
table render — but it is intermittently visible, surfacing only in runs that
materialize that table, and the strict majority drops exactly that. The PASS
certifies the two seeded blockers converged (S-1 and S-2 were absent from all
three round-4 runs); it does not certify the corpus is clean. That is the
tolerance the repeated-runs choice buys, and its cost, stated plainly.

**Honest caveats across the six runs:**
- Loop QA ran in `ai_fallback_mode`; the seeds are deterministic rendering
  defects, so this does not claim the live model's full surface.
- The agent's patches live only on the closed-unmerged `agent-converge/run-*`
  branches (PRs #116, #118, #120, #122, #123); `qa-corpus-1` is untouched at
  `de96b34a`. A converged loop is evidence the majority-reported blockers are
  gone, not that the patches are good.
- S-3 remains out of scope by P1.1's written decision.
- Runs 4 and 5 stopped honestly at MAX_ROUNDS while still shrinking; the 3-round
  cap was the limiter, not the defects. Run 6 needed max_rounds=5 to earn its
  verification round.

---

## Deploy verification — runtime v4 (P2.4's pinned zip)

### 2026-09-02 — v4 boots, imports and reports; NOT a recall measurement

This entry exists to close a gap the P2.4 deploy opened, and it is deliberately
filed apart from the corpus passes above. **Do not read it as a recall data
point.** It ran on haiku over 2 routes against healthy `main`, so it is not
comparable to the sonnet corpus runs, and no seeded defect was in scope.

**Why it was needed.** P2.4 pinned the agent's direct deps and re-packaged, and
`dev.tfvars` moved the runtime to v4 (pipelineguard#66). The staged zip vendored
the same *direct* versions as v3, but not the same bytes — `boto3`/`botocore`
moved 1.43.83 → 1.43.86 between the two builds. Rule 4 in CLAUDE.md exists
because a bad zip fails as an `ImportError` at INVOKE time, which nothing before
an invocation detects. v4 had been applied and never invoked; the next party to
discover a broken runtime would have been a vesselAI PR author.

[Run 33618547097](https://github.com/amdhd/vesselAI/actions/runs/33618547097),
`workflow_dispatch` on `main` at `5c3ee9d4`, haiku, `max_routes=2`:

| | |
|---|---|
| overall | **PASS** |
| authenticated | `true` (`auth_probe = "measured"`) |
| routes | `/voyage`, `/maintenance` |
| turns / session | 13 / 84 s (paced 37.3 s) |
| findings | 0 |
| candidates | 3, all assessed (3 refuted) |
| archive | `reports/gh-33618547097-1/findings.json` |

**What that actually proves.** Every module in the zip executed: `candidates.py`
generated three candidates, `browser_tools.py` + `cdp.py` drove the browser
through two authenticated routes, the auth probe measured rather than guessed,
and the payload validated against `schema.py`. The import path is the thing under
test and it is model-independent, which is why the cheap rung was sufficient.
0 findings on healthy `main` also matches the Leg B baseline, so the new zip
brings no false-positive spike.

**Cost: $0.0487** — 63 in / 2,357 out / 120,488 cache-read / 15,794 cache-write
on haiku ($0.0436) + 84 s of the two concurrent sessions the compute meter bills
($0.0051). Predicted $0.035–0.05 before dispatch; the estimate held, which is a
calibration check on `prices.json` as much as on the runtime.

**A cancelled paid session, recorded because it cost money.** Three dispatches
landed within three minutes. `cancel-in-progress` is on, so each killed the
previous: 33618302728 died *inside* **Run the QA agent** — past the health gate,
runtime already invoked — and 33618483720 died during **Start the stack** ($0).
The spend on the first is small and unknowable (no report was produced, and
cancelling the job kills the harness, not necessarily the server-side session,
which can bill to its 600 s deadline). The workflow's own concurrency comment
predicts exactly this. **Check for an in-flight run before dispatching.**

**Not done, deliberately.** A sonnet/8-route run (~$0.26) would put v4 on the
record next to the v2/v3 corpus passes. It was declined: it buys a measurement,
not confidence in the runtime, and the runtime question is answered.
