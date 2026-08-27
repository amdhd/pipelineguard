# Phase 1 — evidence record

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
| 1 | Comment carries token cost, session seconds and runner minutes | ⚠️ **PARTIAL** — cost and seconds present; runner minutes absent, and inference rendered `unpriced` |
| 2 | Stack up from a cold cache in under ~5 min | ✅ **PASS** — 2m47s and 2m30s end-to-end, including the agent |
| 3 | Health gate refuses to invoke when `seed` is broken | ✅ **PASS** — verified by negative test, agent step skipped |
| 4 | False-positive rate | ✅ **0%** — 1 of 1 labelled finding was a real defect |
| 5 | False-negative rate against seeded bugs | ❌ **67%** — 2 of 3 seeded bugs missed |
| 6 | Two rungs benchmarked | ⛔ **NOT MEASURED** |

**The headline is criterion 5, and it is not good news.** See below — it is also
the most useful thing this exercise produced.

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
| **S-3** | SIRE findings fixture | Status is `OPEN`; frontend still compares `=== 'open'` | *Only* a CSS colour class stops applying — **too subtle, see Results** |

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

The cheap rung is the default, so the question is whether it clears the bar, not
which model is best. The reference implementation ran one round on a weaker rung
and got findings stronger models never reproduced — they were agent noise, and
noise costs human review time, which is the expensive resource.

Run the **same PR** twice, changing only `--model`:

```
global.anthropic.claude-haiku-4-5-20251001-v1:0   # cheap rung, the default
global.anthropic.claude-sonnet-4-6                # quality rung
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
